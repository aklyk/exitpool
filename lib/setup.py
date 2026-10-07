#!/usr/bin/env python3
"""Мастер exitpool: что нашёл на сервере → что поставить (с бюджетом памяти) → вопросы → итог → установка.

  setup.py            новая установка (если exitpool уже стоит — добавление частей, как --add)
  setup.py --add      добавить AWG-выходы, Happ, веб или связь с 3x-ui к установленному exitpool
  setup.py --upgrade  обновить exitpool или перенести happ-service 1.x
  setup.py --plan     только показать сканирование и бюджет, ничего не меняя
Секреты (подписка, профили AWG, токен панели, пароли) остаются в памяти или во временных файлах 0600.
"""
import argparse
from collections import Counter
from contextlib import ExitStack
import getpass
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402
import memory_budget as mb  # noqa: E402
import awg_profile  # noqa: E402
import fetch  # noqa: E402
import ops  # noqa: E402
from configuration import (DEFAULTS, ID_PATTERN, TAG_PATTERN, make_outbounds,  # noqa: E402
                           validate_instances, validate_subscription, validate_catalog)
from cli import ask, yes, number, choose_memory, read_hidden_block, show_status  # noqa: E402

SOURCE = Path(__file__).resolve().parent.parent
LIB = SOURCE/'lib'
FAST_OBSERVATORY = ('Для быстрого переключения балансера: наблюдатель (burstObservatory) 10s × 2. '
                    'При 30s × 6 переключение медленное и нестабильное: реле закрывается за ~30 с, '
                    'но 3x-ui может держать мёртвый выход ещё 1–2 минуты.')


class SetupError(RuntimeError):
    pass


def say(text=''):
    print(text, flush=True)


def run(args, timeout=30):
    try:
        return subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, -1, '', str(exc))


def private_file(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(text)
    return path


# ---------------------------------------------------------------- step 1: what is on this server

def docker_info():
    if not shutil.which('docker'):
        return 'missing', ''
    r = run(['docker', 'version', '--format', '{{.Server.Version}}'], timeout=20)
    if r.returncode:
        return 'down', ''
    version = r.stdout.strip()
    try:
        return ('ready' if int(version.split('.')[0]) >= 28 else 'old'), version
    except ValueError:
        return 'down', version


def find_xui():
    if run(['systemctl', 'is-active', 'x-ui']).stdout.strip() == 'active':
        return 'служба x-ui работает'
    if 'x-ui.service' in run(['systemctl', 'list-unit-files', 'x-ui.service', '--no-legend']).stdout:
        return 'служба x-ui установлена, сейчас выключена'
    if shutil.which('docker'):
        r = run(['docker', 'ps', '--format', '{{.Names}}\t{{.Image}}'], timeout=15)
        for line in r.stdout.splitlines():
            if '3x-ui' in line or 'x-ui' in line.split('\t')[0]:
                return 'в Docker: '+line.split('\t')[0]
    return ''


def scan(self_test=True):
    import preflight
    release = {}
    try:
        release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    except OSError:
        pass
    meminfo = {}
    try:
        meminfo = {line.split(':', 1)[0]: int(line.split()[1])//1024
                   for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line}
    except (OSError, ValueError):
        pass
    budget = mb.read_memory()
    docker, docker_version = docker_info()
    facts = {
        'os': release.get('PRETTY_NAME', '?').strip('"'),
        'tested': release.get('ID', '').strip('"') == 'ubuntu' and release.get('VERSION_ID', '').strip('"') == '24.04'
        and platform.machine() == 'x86_64',
        'cpu': os.cpu_count() or 1, 'virt': run(['systemd-detect-virt']).stdout.strip() or 'нет',
        'total': budget.total_mib if budget else None, 'available': budget.available_mib if budget else None,
        'swap_total': meminfo.get('SwapTotal', 0), 'swap_used': meminfo.get('SwapTotal', 0)-meminfo.get('SwapFree', 0),
        'docker': docker, 'docker_version': docker_version, 'tun': Path('/dev/net/tun').exists(),
        'xui': find_xui(), 'disk_free_gb': shutil.disk_usage('/').free//2**30,
    }
    say('\nЧто нашёл на сервере')
    say(f"  Сервер: {facts['os']}, {platform.machine()}, {facts['cpu']} CPU, виртуализация: {facts['virt']}"
        + ('' if facts['tested'] else ' — проверялась только Ubuntu 24.04 amd64, решают проверки ниже'))
    if facts['available'] is not None:
        say(f"  Память: всего {facts['total']} МиБ, доступно сейчас {facts['available']} МиБ"
            + (f" (swap {facts['swap_used']}/{facts['swap_total']} МиБ занят)" if facts['swap_total'] else ''))
    docker_text = {'missing': 'не установлен (нужен только для Happ, около 90 МиБ)',
                   'down': 'установлен, но служба недоступна', 'old': f'{docker_version} — для Happ нужен 28+',
                   'ready': f'{docker_version}'}[docker]
    say('  Docker: '+docker_text)
    say('  TUN: '+('есть — AWG возможен' if facts['tun'] else 'нет — AWG невозможен (часто на OpenVZ/LXC VPS)'))
    say('  3x-ui: '+(('найдена на этом сервере ('+facts['xui']+')') if facts['xui'] else 'на этом сервере не найдена'))
    say(f"  Диск: свободно {facts['disk_free_gb']} ГБ")
    if facts['tun'] and self_test:
        say('  Проверяю ядро для AWG: временные netns, veth и TUN (убираются сразу)…')
    checks = [c for c in preflight.collect([mb.AWG_DEFAULT_MODE] if facts['tun'] else [], self_test=self_test)['checks']
              if c['check'] in ('tools', 'socket_proxyd', 'systemd', 'kernel', 'subnet', 'neighbors')]
    for c in checks:
        say(f"  [{preflight.NAMES[c['status']]}] {c['text']}")
    facts['awg_ok'] = facts['tun'] and not any(c['status'] == 'FAIL' for c in checks)
    if facts['tun'] and not facts['awg_ok']:
        say('  AWG на этом сервере не получится: причина в строке «не подходит» выше.')
    return facts


# ---------------------------------------------------------------- step 2: parts with a live budget

def budget_lines(facts, parts, awg_modes, happ_count):
    plan = mb.plan(facts['available'] or 0, happ=happ_count if parts['happ'] else 0,
                   awg_modes=awg_modes if parts['awg'] else [], web=parts['web'],
                   docker_missing=parts['happ'] and facts['docker'] == 'missing')
    lines = [f'  {name:34} ~{mib} МиБ' for name, mib in plan['items']]
    lines.append(f"  {'запас системе':34} {plan['reserve']} МиБ")
    if facts['available'] is None:
        lines.append('  Доступную память определить не удалось — проверьте её сами.')
        plan['status'] = 'WARN'
        return plan, lines
    total = plan['expected']+plan['reserve']
    verdict = {'PASS': f"хватает, останется ~{facts['available']-total} МиБ",
               'WARN': f"помещается без запаса системе (останется ~{plan['remaining']} МиБ)",
               'FAIL': f"не хватает ~{plan['expected']-facts['available']} МиБ"}[plan['status']]
    lines.append(f"  Итого ~{plan['expected']} + запас {plan['reserve']} из {facts['available']} МиБ — {verdict}")
    return plan, lines


def memory_hints(facts, parts):
    say('Чем помочь (мастер ничего не меняет сам):')
    if parts['web']:
        say('  — выключить веб-интерфейс (управлять можно командой exitpool);')
    if parts['happ']:
        say('  — обойтись без Happ или взять меньше Happ-выходов (каждый ~256 МиБ);')
    if parts['awg']:
        say('  — режим памяти 32 МиБ для AWG-выхода или меньше выходов;')
    r = run(['ps', '-eo', 'rss=,comm=', '--sort=-rss'])
    top = [line.split(None, 1) for line in r.stdout.splitlines()[:5] if line.strip()]
    if top:
        say('  — освободить память; крупнейшие процессы: '+', '.join(f'{c.strip()} {int(m)//1024} МиБ' for m, c in top))


def choose_parts(facts, installed=None, force=False):
    installed = installed or {}
    has_happ = any(c.get('kind', 'happ') == 'happ' for c in installed.values())
    web_configured = (paths.ETC/'web.json').exists()
    web_active = web_configured and run(['systemctl', 'is-active', 'exitpool-web.service']).stdout.strip() == 'active'
    parts = {'awg': facts['awg_ok'], 'happ': False, 'web': False}
    happ_note = 'нужен Docker ~90 МиБ, его нет; образ ~1 ГБ' if facts['docker'] == 'missing' else 'образ ~1 ГБ'
    while True:
        rows = [('awg', 'AWG-выходы (профили .conf), без Docker',
                 f'~{mb.awg_budget(mb.AWG_DEFAULT_MODE)} МиБ каждый (режим 64)' if facts['awg_ok'] else 'недоступно здесь'),
                ('happ', 'Happ-выходы (подписка happ://crypt5)',
                 'уже установлены — подписка: exitpool subscription' if has_happ else f'~256 МиБ каждый, {happ_note}'),
                ('web', 'веб-интерфейс (управление с телефона в LAN)',
                 'уже включён' if web_active else 'настроен, служба выключена' if web_configured else f'~{mb.WEB_MIB} МиБ')]
        say('\nЧто поставить?')
        for index, (key, title, cost) in enumerate(rows, 1):
            say(f"  {index} [{'x' if parts[key] else ' '}] {title:46} {cost}")
        plan, lines = budget_lines(facts, parts, [mb.AWG_DEFAULT_MODE], 1)
        for line in lines:
            say(line)
        choice = ask('Номер — включить/выключить, Enter — дальше', '')
        if choice == '':
            if not any(parts.values()):
                say('Ничего не выбрано.')
                if yes('Выйти', True):
                    raise KeyboardInterrupt
                continue
            if plan['status'] == 'FAIL' and not force:
                say('\nПо оценке памяти не хватает.')
                memory_hints(facts, parts)
                say('Для опытных: --force-budget (на свой риск).')
                continue
            return parts
        if choice not in ('1', '2', '3'):
            continue
        key = rows[int(choice)-1][0]
        if key == 'awg' and not facts['awg_ok']:
            say('AWG здесь недоступен (см. сканирование выше).')
        elif key == 'happ' and has_happ:
            say('Happ уже установлен. Профили и подписка меняются командой: sudo exitpool subscription')
        elif key == 'happ' and facts['docker'] in ('down', 'old') and not parts['happ']:
            say('Docker есть, но '+('служба недоступна' if facts['docker'] == 'down' else 'версия старше 28')
                + ': исправьте его отдельно, мастер Docker не переустанавливает.')
        elif key == 'web' and web_active:
            say('Веб уже включён: sudo exitpool web show|disable|passwd')
        elif key == 'web' and web_configured:
            say('Веб настроен, но служба выключена. Включить с прежним адресом и паролем: sudo exitpool web enable')
        else:
            parts[key] = not parts[key]


# ---------------------------------------------------------------- step 3: questions

def ask_name(taken, default):
    while True:
        name = ask('Имя выхода (латиница: fi, de2, awg-se)', default).lower()
        if not ID_PATTERN.fullmatch(name):
            say('Маленькие латинские буквы, цифры, дефис или _, начинается с буквы, до 24 символов.')
        elif name in taken:
            say('Такое имя уже занято.')
        else:
            return name


def ask_awg_exits(taken_names, taken_tags, fingerprints):
    exits = []
    while True:
        if not yes('\nДобавить AWG-выход сейчас (профиль .conf)' if not exits else 'Добавить ещё один AWG-выход',
                   not exits):
            return exits
        name = ask_name(taken_names, f'awg{len(exits)+1}' if f'awg{len(exits)+1}' not in taken_names else '')
        while True:
            path = ask('Путь к файлу .conf (пусто — вставить текст)', '')
            try:
                text = Path(path).expanduser().read_text() if path else read_hidden_block(
                    'Вставьте текст профиля и нажмите Ctrl+D на новой строке (ввод скрыт).')
                facts = awg_profile.parse(text)
            except OSError:
                say('Не удалось прочитать файл.')
                continue
            except awg_profile.ProfileError as exc:
                say(str(exc))
                continue
            if facts['fingerprint'] in fingerprints:
                say(f"Этот ключ уже у выхода {fingerprints[facts['fingerprint']]}: один ключ — один туннель.")
                continue
            break
        say(f"  Профиль принят: сервер {facts['endpoint_host']}:{facts['endpoint_port']}, "
            f"{'AmneziaWG' if facts['awg'] else 'WireGuard без обфускации'}, полный туннель, "
            f"отпечаток ключа {facts['fingerprint']}")
        while True:
            tag = ask('Тег исходящего в 3x-ui', name if name.startswith('awg') else 'awg-'+name)
            if TAG_PATTERN.fullmatch(tag) and tag not in taken_tags:
                break
            say('Тег: латиница, цифры, _ . -, уникальный.')
        while True:
            country = ask('Только страна выхода (код FI, DE…; пусто — любая)', '').upper()
            if not country or re.fullmatch(r'[A-Z]{2}', country):
                break
        memory = choose_memory(name)
        exits.append({'name': name, 'profile': text, 'tag': tag, 'country': country, 'memory_mib': memory,
                      'endpoint': f"{facts['endpoint_host']}:{facts['endpoint_port']}", 'fingerprint': facts['fingerprint']})
        taken_names.add(name)
        taken_tags.add(tag)
        fingerprints[facts['fingerprint']] = name


def ask_proxy():
    say('Прокси для скачивания: http://, https://, socks5h:// (имена разрешает прокси), socks5://, socks4a://, socks4://.')
    say('Если на сервере есть 3x-ui, подойдёт её локальный socks/http-вход (127.0.0.1:порт).')
    while True:
        try:
            proxy = fetch.parse_proxy(ask('Адрес прокси (например socks5h://127.0.0.1:1080)'))
        except fetch.FetchError as exc:
            say(str(exc))
            continue
        if not proxy:
            say('Укажите адрес прокси или нажмите Ctrl+C для отмены.')
            continue
        if not proxy['user']:
            user = ask('Логин (пусто — без авторизации)', '')
            if user:
                proxy['user'] = user
                proxy['password'] = getpass.getpass('Пароль прокси (скрытый ввод): ')
        ok, error = fetch.probe_proxy(proxy)
        say(f'  Проверка {fetch.masked(proxy)} → github.com: '+('OK' if ok else 'ошибка — '+error))
        if ok or yes('Всё равно использовать этот прокси', False):
            return proxy


def acquire_binaries(args, workdir, proxy, keep_existing=True):
    """Binaries before anything is installed. Returns (mode, directory, local_build, proxy)."""
    if args.binaries_dir:
        state = fetch.check_dir(args.binaries_dir)
        if 'missing' in state.values():
            raise SetupError(f'В {args.binaries_dir} нет: '+', '.join(n for n, s in state.items() if s == 'missing'))
        local = 'local' in state.values()
        if local:
            say('Файлы в '+args.binaries_dir+' не совпадают с релизом по SHA-256 (своя сборка?).')
            if not yes('Установить их как свою сборку', False):
                raise KeyboardInterrupt
        return 'dir', args.binaries_dir, local, proxy
    if args.build_binaries:
        return 'build', None, True, proxy
    if keep_existing and ops.awg_binaries_ready():
        return 'keep', None, False, proxy
    say(f'\nБинарники AWG (amneziawg-go, awg, xray) — из релиза exitpool {paths.VERSION} с проверкой SHA-256.')
    if proxy:
        say('Использую прокси: '+fetch.masked(proxy))
    while True:
        try:
            fetch.download_files(workdir, proxy, progress=lambda t: say('  '+t))
            say('  Скачаны и проверены.')
            return 'dir', workdir, False, proxy
        except fetch.FetchError as exc:
            say('\nБинарники AWG не скачались:\n  '+str(exc))
            say('\n  1 — повторить\n  2 — скачать через прокси (http, https или socks, с логином или без)\n'
                '  3 — собрать из исходников на этом сервере (~1 ГБ диска временно, несколько минут;\n'
                '      пакеты компилятора ставятся и после сборки удаляются)\n'
                '  4 — указать папку с готовыми файлами\n  0 — выйти, ничего не меняя')
            choice = number('Вариант', 1, 0, 4)
            if choice == 0:
                raise KeyboardInterrupt
            if choice == 2:
                proxy = ask_proxy()
            elif choice == 3:
                return 'build', None, True, proxy
            elif choice == 4:
                args.binaries_dir = ask('Папка с amneziawg-go, awg и xray')
                return acquire_binaries(args, workdir, proxy, keep_existing=keep_existing)


def docker_install_preflight():
    info = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            info[key] = value.strip('"')
    if info.get('ID') != 'ubuntu' or info.get('VERSION_ID') not in ('22.04', '24.04', '26.04'):
        raise SetupError('Автоустановка Docker предусмотрена для Ubuntu 22.04/24.04/26.04. Установите Docker отдельно.')
    codename = info.get('UBUNTU_CODENAME') or info.get('VERSION_CODENAME', '')
    if not re.fullmatch(r'[a-z]+', codename):
        raise SetupError('Не удалось определить кодовое имя Ubuntu.')
    for path in ('/etc/apt/sources.list.d/docker.sources', '/etc/apt/sources.list.d/docker.list', '/etc/apt/keyrings/docker.asc'):
        if Path(path).exists():
            raise SetupError('Настройки репозитория Docker уже существуют. Завершите установку Docker отдельно.')
    conflicts = []
    for package in ('docker.io', 'docker-compose', 'docker-compose-v2', 'docker-doc', 'docker-buildx', 'podman-docker',
                    'containerd', 'runc'):
        r = run(['dpkg-query', '-W', '-f=${db:Status-Status}', package])
        if r.returncode == 0 and r.stdout.strip() == 'installed':
            conflicts.append(package)
    if conflicts:
        raise SetupError('Есть пакеты, требующие отдельной миграции Docker: '+', '.join(conflicts)+'. Мастер их не удаляет.')
    return codename


def install_docker():
    codename = docker_install_preflight()
    say('\nУстановка Docker из официального apt-репозитория Docker…')
    subprocess.run(['apt-get', 'update'], check=True)
    subprocess.run(['apt-get', 'install', '-y', 'ca-certificates', 'curl'], check=True)
    subprocess.run(['install', '-m', '0755', '-d', '/etc/apt/keyrings'], check=True)
    with urllib.request.urlopen('https://download.docker.com/linux/ubuntu/gpg', timeout=30) as response:
        key = response.read()
    if not key.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----'):
        raise SetupError('Не получен ключ официального репозитория Docker.')
    target = Path('/etc/apt/keyrings/docker.asc')
    target.write_bytes(key)
    target.chmod(0o644)
    source = Path('/etc/apt/sources.list.d/docker.sources')
    source.write_text('Types: deb\nURIs: https://download.docker.com/linux/ubuntu\n'
                      f'Suites: {codename}\nComponents: stable\nArchitectures: amd64\n'
                      'Signed-By: /etc/apt/keyrings/docker.asc\n')
    source.chmod(0o644)
    subprocess.run(['apt-get', 'update'], check=True)
    subprocess.run(['apt-get', 'install', '-y', 'docker-ce', 'docker-ce-cli', 'containerd.io',
                    'docker-buildx-plugin', 'docker-compose-plugin'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'docker'], check=True)
    if docker_info()[0] != 'ready':
        raise SetupError('Docker установлен, но не отвечает.')


def read_subscription():
    say('\nВставьте happ://crypt5/... или абсолютный путь к файлу с этой ссылкой (ввод скрыт).')
    while True:
        raw = getpass.getpass('Подписка: ').strip()
        if raw.startswith('/'):
            try:
                raw = Path(raw).read_text().strip()
            except OSError:
                say('Не удалось прочитать файл.')
                continue
        try:
            return validate_subscription(raw)
        except ValueError as exc:
            say(str(exc))


def choose_happ(catalog, memory, used_ports, used_tags):
    catalog = validate_catalog(catalog)
    counts = Counter(catalog)
    selectable = [str(i) for i, name in enumerate(catalog, 1) if counts[name] == 1]
    if not selectable:
        raise SetupError('В подписке нет профилей с уникальными названиями; однозначный выбор невозможен.')
    say('\nПрофили из подписки:')
    for index, name in enumerate(catalog, 1):
        say(f'  {index}. {name}'+(' — одинаковые названия, выбор недоступен' if counts[name] > 1 else ''))
    while True:
        budget = memory() if memory else None
        if budget:
            say(f'Доступно сейчас {budget.available_mib} МиБ; Happ-выход ~{mb.PER_INSTANCE_MIB} МиБ, '
                f'запас системе {mb.RESERVE_MIB} МиБ (первый Happ уже запущен и учтён).')
        selected = ask('Номера через пробел (до 8)', selectable[0]).split()
        if (not 1 <= len(selected) <= 8 or len(set(selected)) != len(selected)
                or any(n not in selectable for n in selected)):
            say('Укажите до 8 разных номеров доступных профилей.')
            continue
        if budget and not budget.estimate(len(selected))['fits']:
            say('По оценке памяти не хватает с запасом для системы. Выберите меньше профилей.')
            continue
        break
    instances, port = {}, 11808
    for choice in selected:
        name, label = 'p'+choice, catalog[int(choice)-1]
        while port in used_ports or port in (10808, 10809) or not ops.port_free(port):
            port += 10
        say('\n'+label)
        while True:
            value = number('Локальный SOCKS-порт', port, 1025, 65535)
            if value not in used_ports and value not in (10808, 10809) and ops.port_free(value):
                break
            say('Порт занят или уже выбран.')
        used_ports.add(value)
        instances[name] = {'profile_name': label, 'country': '', 'port': value}
    hours = number('\nОбновлять подписку каждые N часов', 6, 1, 168)
    while True:
        prefix = ask('Префикс тегов исходящих в панели', 'happ')
        tags = {prefix+'-'+n for n in instances}
        if all(TAG_PATTERN.fullmatch(t) for t in tags) and not tags & used_tags:
            break
        say('Латиница, цифры, дефис, _ или точка; теги не должны совпадать с уже выбранными.')
    advanced = dict(DEFAULTS)
    if yes('Изменить проверки доступности, лимит памяти Happ или страну', False):
        advanced['health_interval'] = number('Период проверок, секунд', 20, 10, 60)
        advanced['failure_threshold'] = number('Неудачных проверок подряд до перезапуска', 3, 2, 10)
        advanced['memory_mib'] = number('Лимит RAM на Happ-выход, МиБ (лимит, не резерв)', 384, 256, 2048)
        for cfg in instances.values():
            if yes('Ограничить страну выхода для '+cfg['profile_name'], False):
                while True:
                    country = ask('Код страны, например DE').upper()
                    if re.fullmatch(r'[A-Z]{2}', country):
                        cfg['country'] = country
                        break
    for name, cfg in instances.items():
        cfg.update(advanced, update_hours=hours, tag=prefix+'-'+name)
    return validate_instances(instances)


def lan_defaults():
    """The address used for the default route is the LAN address in a home network."""
    try:
        route = json.loads(run(['ip', '-j', '-4', 'route', 'get', '1.1.1.1'], timeout=5).stdout)[0]
        address = route['prefsrc']
        for link in json.loads(run(['ip', '-j', '-4', 'addr', 'show'], timeout=5).stdout):
            for item in link.get('addr_info', []):
                if item.get('local') == address:
                    return address, str(ipaddress.ip_network(f"{address}/{item['prefixlen']}", strict=False))
        return address, str(ipaddress.ip_network(address+'/24', strict=False))
    except (ValueError, KeyError, IndexError, TypeError):
        return '127.0.0.1', '127.0.0.0/8'


def bindable(address, port):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((address, port))
            return True
        except OSError:
            return False


def ask_web(force=False, plan=False):
    """Address, client subnet and password of the optional web interface; None if declined."""
    import web
    say('\nВеб-интерфейс: статус, настройки и добавление выходов с телефона в домашней сети.')
    say('Слушает только выбранный адрес LAN. Не пробрасывайте его порт в интернет.')
    if not force and not yes('Включить веб-интерфейс', False):
        return None
    address, network = lan_defaults()
    while True:
        listen = ask('IP этого сервера в локальной сети', address)
        try:
            listen = str(ipaddress.ip_address(listen))
        except ValueError:
            say('Нужен IP-адрес, например 192.168.1.10.')
            continue
        port = number('Порт веб-интерфейса', 1338, 1025, 65535)
        if plan or bindable(listen, port):
            break
        say('Этот адрес/порт занят или не принадлежит серверу.')
    while True:
        try:
            allow = str(ipaddress.ip_network(ask('С какой подсети пускать клиентов', network), strict=False))
            break
        except ValueError:
            say('Нужна подсеть, например 192.168.1.0/24.')
    config = {'listen': listen, 'port': port, 'allow': [allow], 'hosts': [], 'session_hours': 72, 'password': ''}
    if not plan:
        config['password'] = web.hash_password(web.read_new_password(False))
    return config


def web_url(cfg):
    host = f"[{cfg['listen']}]" if ':' in cfg['listen'] else cfg['listen']
    return f"http://{host}:{cfg['port']}/"


def ask_panel(planned_tags):
    """3x-ui: 0 — do not link, 1 — add outbounds now (token in memory), 2 — add and keep the token for status."""
    from panel_outbounds import PanelClient, PanelError, normalize_url
    say('\nСвязь с 3x-ui:')
    say('  1 — не связывать: в конце покажу готовый JSON исходящих, вставите в панель сами')
    say('  2 — добавить исходящие сейчас (API-токен нужен только на время установки и не сохраняется)')
    say('  3 — добавить и показывать на карточках, какой балансер выбрал выход (токен хранится на сервере, root 0600)')
    mode = number('Вариант', 1, 1, 3)
    if mode == 1:
        return None
    while True:
        try:
            url = normalize_url(ask('Адрес панели (можно скопировать страницу …/panel/…)', 'http://127.0.0.1:2053'))
            token = getpass.getpass('API-токен панели (скрытый ввод): ').strip()
            if token.lower().startswith('bearer '):
                token = token[7:].strip()
            client = PanelClient(url, token)
            template = client.setting(client.read())
            clash = sorted({o.get('tag') for o in template['outbounds']} & set(planned_tags))
            if clash:
                say('В панели уже есть исходящие с такими тегами: '+', '.join(clash)+'. '
                    'Совпадающие с нашими настройками останутся, другие — ошибка при добавлении.')
            say('API панели доступен.')
            return {'mode': mode, 'url': url, 'token': token, 'client': client}
        except (PanelError, ValueError) as exc:
            say(str(exc))
            say('1 — повторить ввод; 2 — не связывать с панелью; 0 — выйти.')
            action = number('Действие', 1, 0, 2)
            if action == 0:
                raise KeyboardInterrupt
            if action == 2:
                return None


def panel_settings(url, token):
    parsed = urllib.parse.urlsplit(url)
    return {'host': parsed.hostname, 'port': parsed.port or (443 if parsed.scheme == 'https' else 80),
            'base_path': parsed.path, 'https': parsed.scheme == 'https', 'token': token}


# ---------------------------------------------------------------- step 4: summary and apply

def summarize(plan):
    say('\nБудет выполнено:')
    if plan['fresh']:
        say('  Установить exitpool '+paths.VERSION+': /opt/exitpool, /etc/exitpool, команда sudo exitpool.')
    bins = plan.get('binaries')
    if bins:
        say('  Бинарники AWG: '+{'dir': 'проверенные файлы'+(' (своя сборка)' if bins[2] else ' релиза'),
                                 'build': 'собрать из исходников (пакеты компилятора временно)',
                                 'keep': 'уже установлены'}[bins[0]]+'.')
    if plan.get('install_docker'):
        say('  Установить Docker из официального apt-репозитория.')
    for item in plan['awg']:
        say(f"  AWG {item['name']}: сервер {item['endpoint']}, тег {item['tag']}, "
            f"страна {item['country'] or 'любая'}, режим памяти {item['memory_mib']} МиБ "
            f"(ожидаемо ~{mb.awg_budget(item['memory_mib'])} МиБ; жёсткие лимиты "
            f"{'/'.join(map(str, mb.awg_caps(item['memory_mib'])))} МиБ).")
    if plan['awg']:
        say('    Адрес для 3x-ui — 10.250.N.2:ПОРТ (свой у каждого выхода); доступ к реле — только с этого сервера.')
    for name, cfg in (plan.get('happ') or {}).items():
        say(f"  Happ {name}: «{cfg['profile_name']}», страна {cfg['country'] or 'любая'}, 127.0.0.1:{cfg['port']} → {cfg['tag']}")
    if plan.get('web'):
        say(f"  Веб-интерфейс: {web_url(plan['web'])} для {', '.join(plan['web']['allow'])}, вход по паролю.")
    panel = plan.get('panel')
    if panel:
        parsed = urllib.parse.urlsplit(panel['url'])
        say(f"  3x-ui: добавить исходящие в {parsed.scheme}://{parsed.netloc}{'/[путь]' if parsed.path else ''}"
            + ('; токен сохранить для статуса балансеров.' if panel['mode'] == 3 else '; токен не сохраняется.'))
    else:
        say('  3x-ui не меняется: JSON исходящих покажу в конце.')
    if plan.get('save_proxy'):
        say('  Запомнить прокси для обновлений (/etc/exitpool/proxy.json, root 0600).')
    say('  Работающие маршруты, DNS, firewall и Docker сервера не перенастраиваются.')


def wait_happ(names, since, timeout=300):
    say('\nОжидание подключения Happ-выходов…')
    results = {}
    for name in names:
        results[name] = ops.wait_running(name, since, timeout)
        say(f"  {name}: {'работает' if results[name] == 'running' else 'пока не работает ('+results[name]+')'}")
    return results


def apply(plan, discovery=None):
    env = dict(os.environ)
    if plan.get('proxy'):
        env['EXITPOOL_PROXY'] = fetch.full_url(plan['proxy'])
    with tempfile.TemporaryDirectory(prefix='exitpool-setup-') as directory:
        temp = Path(directory)
        mode, bin_dir, local_build = plan.get('binaries') or ('skip', None, False)
        if plan['fresh']:
            cmd = ['python3', str(LIB/'installer.py'), 'base', '--binaries', mode]
            if bin_dir:
                cmd += ['--binaries-dir', str(bin_dir)]
            if local_build and mode == 'dir':
                cmd.append('--binaries-local-build')
            if plan.get('web'):
                cmd += ['--web-config', str(private_file(temp/'web.json', json.dumps(plan['web'])))]
            subprocess.run(cmd, check=True, env=env)
        else:
            if mode == 'dir':
                fetch.install_from_dir(bin_dir, strict=not local_build, progress=say)
            elif mode == 'build':
                fetch.build_from_source(progress=say, proxy=plan.get('proxy'))
            if plan.get('web'):
                import web
                web.save_config(plan['web'])
                subprocess.run(['systemctl', 'enable', '--now', 'exitpool-web.service'], check=True)
                subprocess.run(['systemctl', 'restart', 'exitpool-web.service'])
        if plan.get('save_proxy'):
            fetch.save_proxy(plan['proxy'])
        problems = []
        if plan.get('happ'):
            started = time.time()
            cmd = ['python3', str(LIB/'installer.py'), 'happ',
                   '--subscription', str(private_file(temp/'subscription.secret', plan['subscription']+'\n')),
                   '--instances', str(private_file(temp/'instances.json', json.dumps(plan['happ'], ensure_ascii=False)))]
            if discovery:
                cmd += ['--seed-state', str(discovery.state)]
            subprocess.run(cmd, check=True, env=env)
            problems += [n for n, r in wait_happ(plan['happ'], started).items() if r != 'running']
        added = []
        for item in plan['awg']:
            say(f"\nAWG {item['name']}:")
            data = {k: item[k] for k in ('name', 'profile', 'tag', 'country', 'memory_mib')}
            job = ops.Jobs().awg_add_sync(data, progress=lambda t: say('  '+t),
                                          force_budget=plan.get('force_budget', False))
            if job['phase'] == 'failed':
                say('  Не добавлен: '+(job.get('error') or 'ошибка'))
                problems.append(item['name'])
                continue
            added.append(item['name'])
            problems += job.get('warnings') or []
    return problems


def finish_panel(plan, problems):
    from panel_outbounds import PanelError
    cfg = ops.installed()
    outbounds = make_outbounds(cfg)
    panel = plan.get('panel')
    if not panel:
        say('\nИсходящие для 3x-ui (панель → Xray → Исходящие → JSON), файл /opt/exitpool/3xui-outbounds.json:')
        say(json.dumps(outbounds, ensure_ascii=False, indent=2))
        return
    working = [o for o in outbounds if o['tag'] not in {cfg[n]['tag'] for n in problems if n in cfg}]
    try:
        for item in panel['client'].test(working):
            say(f"  Проверка Xray в панели: {item['tag']} OK")
    except PanelError as exc:
        say('  '+str(exc)+' (если панель на другом сервере, адрес 10.250.N.2 ей недоступен).')
        if not yes('Всё равно добавить исходящие в панель', False):
            say('Панель не изменялась. Позже: sudo exitpool outbounds --add')
            return
    try:
        tags = panel['client'].add(outbounds, paths.VAR/'panel-backups')
        say('Добавлены исходящие: '+', '.join(tags) if tags else 'Исходящие уже есть в панели.')
    except PanelError as exc:
        say('Шаг панели не завершён: '+str(exc)+'. Повторить: sudo exitpool outbounds --add')
        return
    if panel['mode'] == 3:
        try:
            ops.save_panel(panel_settings(panel['url'], panel['token']))
            say('Связь с панелью сохранена: карточки покажут, какой балансер выбрал выход.')
        except ops.OpsError as exc:
            say('Связь с панелью не сохранена: '+str(exc))


def final_words(plan, facts, problems):
    say('')
    try:
        show_status()
    except ops.OpsError:
        pass
    if plan.get('web'):
        say('\nВеб-интерфейс: '+web_url(plan['web']))
    say('\nДальше в 3x-ui: включите новые исходящие в нужный балансер (панель → Xray → Балансеры).')
    say(FAST_OBSERVATORY)
    if facts.get('xui', '').startswith('в Docker') and plan['awg']:
        say('3x-ui работает в Docker: если не в сети хоста, разрешите её сети доступ к реле — '
            'sudo exitpool access allow <мост> <подсеть> (по умолчанию доступ только у самого сервера).')
    say('Управление: sudo exitpool (меню) или sudo exitpool --help.')
    if problems:
        say('Требуют внимания: '+', '.join(sorted(set(problems)))+' — sudo exitpool status, sudo exitpool log ИМЯ.')
        return 2
    return 0


def budget_check(facts, plan, force):
    while facts['available'] is not None:
        parts = {'awg': bool(plan['awg']), 'happ': bool(plan.get('happ')), 'web': bool(plan.get('web'))}
        result, lines = budget_lines(facts, parts, [i['memory_mib'] for i in plan['awg']], len(plan.get('happ') or {}))
        if result['status'] != 'FAIL' or force:
            if result['status'] == 'WARN':
                say('\nПамять впритык:\n'+'\n'.join(lines))
            return
        say('\nС выбранным памяти не хватает:\n'+'\n'.join(lines))
        memory_hints(facts, parts)
        options = []
        if plan['awg']:
            options.append(('Убрать последний AWG-выход', lambda: plan['awg'].pop()))
        if plan.get('web'):
            options.append(('Без веб-интерфейса', lambda: plan.update(web=None)))
        for index, (title, _) in enumerate(options, 1):
            say(f'  {index} — {title}')
        say('  0 — выйти, ничего не меняя')
        choice = number('Вариант', 0, 0, len(options))
        if choice == 0:
            raise KeyboardInterrupt
        options[choice-1][1]()


def install_or_add(args, proxy, add_mode):
    try:
        cfg = ops.installed()
    except ops.OpsError:
        cfg = {}
    facts = scan(self_test=not args.plan)
    parts = choose_parts(facts, cfg if add_mode else None, force=args.force_budget)
    if args.plan:
        say('\nЭто просмотр: секреты не запрашивались, изменений не было.')
        return 0
    plan = {'fresh': not add_mode, 'awg': [], 'happ': None, 'web': None, 'panel': None, 'proxy': proxy, 'force_budget': args.force_budget}
    taken_names, taken_tags = set(cfg), {c['tag'] for c in cfg.values()}
    fingerprints = {}
    for name, values in cfg.items():
        if values.get('kind') == 'awg':
            try:
                fingerprints[awg_profile.parse((paths.AWG_ETC/f'{name}.conf').read_text())['fingerprint']] = name
            except (OSError, awg_profile.ProfileError):
                pass
    with ExitStack() as stack:
        discovery = None
        if parts['happ']:
            if facts['docker'] == 'missing':
                docker_install_preflight()
                if not yes('Docker не установлен. Установить его из официального репозитория сейчас', True):
                    raise SetupError('Установите Docker 28+ и запустите мастер снова.')
                plan['install_docker'] = True
            plan['subscription'] = read_subscription()
            say('Сначала подготовлю образ Happ (если его нет — сборка ~1 ГБ, несколько минут) и запущу один временный Happ,')
            say('чтобы прочитать профили. Порты на хосте не открываются; при отмене временный Happ удаляется.')
            if not yes('Получить список профилей', True):
                return 0
            if plan.pop('install_docker', False):
                install_docker()
            from discovery import Discovery
            discovery = stack.enter_context(Discovery(SOURCE/'happ', plan['subscription']))
            plan['happ'] = choose_happ(discovery.catalog, discovery.memory, {c['port'] for c in cfg.values()}, taken_tags)
            taken_names |= set(plan['happ'])
            taken_tags |= {c['tag'] for c in plan['happ'].values()}
        if parts['awg']:
            plan['awg'] = ask_awg_exits(taken_names, taken_tags, fingerprints)
            if plan['awg'] or not ops.awg_binaries_ready():
                workdir = stack.enter_context(tempfile.TemporaryDirectory(prefix='exitpool-bin-'))
                mode, directory, local, proxy = acquire_binaries(args, workdir, proxy)
                plan['binaries'], plan['proxy'] = (mode, directory, local), proxy
                if proxy and not fetch.load_saved_proxy():
                    plan['save_proxy'] = yes('Запомнить этот прокси для будущих обновлений', False)
        if parts['web']:
            plan['web'] = ask_web(force=True)
        planned = [i['tag'] for i in plan['awg']]+[c['tag'] for c in (plan['happ'] or {}).values()]
        if planned:
            plan['panel'] = ask_panel(planned)
        budget_check(facts, plan, args.force_budget)
        summarize(plan)
        if not yes('\nНачать', False):
            say('Отменено: ничего не менялось.')
            return 0
        if discovery:
            discovery.stop()
        problems = apply(plan, discovery)
    if plan['awg'] or plan.get('happ'):
        finish_panel(plan, problems)
    return final_words(plan, facts, problems)


# ---------------------------------------------------------------- upgrade / migration

def upgrade(args, proxy):
    import installer
    try:
        pending = installer.pending_migration()
    except installer.InstallError as exc:
        raise SetupError(str(exc)) from None
    legacy = pending == 'resume' or (paths.LEGACY_ETC.exists() and not (paths.OPT/'instances.json').exists())
    kept = not legacy and installer.kept_config()
    if legacy:
        manifest = paths.LEGACY_OPT/'instances.json'
        if not manifest.exists():
            raise SetupError('У happ-service нет /opt/happ-service/instances.json — перенос вручную.')
        old = json.loads(manifest.read_text())
        if pending == 'resume':
            say('\nПрошлый перенос happ-service остановился на полпути. Продолжу его: уже перенесённые выходы не трогаю, '
                'остальные переключаю по одному.')
        else:
            say('\nНайдена установка happ-service. Перенос в exitpool '+paths.VERSION+':')
        for name, cfg in old.items():
            if cfg.get('kind') == 'awg':
                say(f"  {name}: AWG «{cfg.get('label') or name}» — из Docker в режим без Docker, режим памяти 64 МиБ; "
                    f"адрес для 3x-ui прежний 127.0.0.1:{cfg['port']} → {cfg['tag']}")
            else:
                say(f"  {name}: Happ «{cfg.get('profile_name', cfg.get('profile_query'))}», "
                    f"127.0.0.1:{cfg['port']} → {cfg['tag']} — тот же образ, новая служба exitpool-happ@{name}")
        say('Выходы переключаются по одному (у каждого ~10–60 с без связи); если новый не заработал — возвращается прежний.')
        say('Ключи, профили, порты и теги сохраняются; 3x-ui не меняется. Старые файлы остаются до sudo exitpool cleanup-legacy.')
        need_bins = any(c.get('kind') == 'awg' for c in old.values()) and not ops.awg_binaries_ready()
        has_web = (paths.LEGACY_ETC/'web.json').exists() or (paths.ETC/'web.json').exists()
    elif kept:
        cfg = ops.installed()
        say('\nНайдены настройки exitpool, оставленные после удаления программы (uninstall.sh --purge): выходы '
            + (', '.join(cfg) or 'нет')+'. Восстановлю программу и службы с этими настройками, профилями и ключами.')
        need_bins = any(c.get('kind') == 'awg' for c in cfg.values())
        has_web = (paths.ETC/'web.json').exists()
    else:
        cfg = ops.installed()
        say(f"\nОбновление exitpool {(paths.OPT/'VERSION').read_text().strip() if (paths.OPT/'VERSION').exists() else '?'}"
            f' → {paths.VERSION}. Сохраняются профили, ключ подписки, порты, теги и настройки.')
        new_manifest = fetch.manifest()
        try:
            old_manifest = json.loads((paths.OPT/'release'/'binaries.json').read_text())
        except (OSError, ValueError):
            old_manifest = None
        need_bins = any(c.get('kind') == 'awg' for c in cfg.values()) and (
            not ops.awg_binaries_ready() or old_manifest is None or old_manifest.get('files') != new_manifest['files'])
        has_web = (paths.ETC/'web.json').exists()
    web_cfg = None if has_web else ask_web()
    bins = ('keep', None, False)
    with ExitStack() as stack:
        if need_bins:
            say('Нужны новые бинарники AWG.')
            workdir = stack.enter_context(tempfile.TemporaryDirectory(prefix='exitpool-bin-'))
            mode, directory, local, proxy = acquire_binaries(args, workdir, proxy, keep_existing=False)
            bins = (mode, directory, local)
        if not yes('\nНачать', False):
            say('Отменено: ничего не менялось.')
            return 0
        env = dict(os.environ)
        if proxy:
            env['EXITPOOL_PROXY'] = fetch.full_url(proxy)
        with tempfile.TemporaryDirectory(prefix='exitpool-upgrade-') as directory:
            cmd = ['python3', str(LIB/'installer.py'), 'upgrade', '--binaries', bins[0]]
            if bins[1]:
                cmd += ['--binaries-dir', str(bins[1])]
            if bins[2] and bins[0] == 'dir':
                cmd.append('--binaries-local-build')
            if web_cfg:
                cmd += ['--web-config', str(private_file(Path(directory)/'web.json', json.dumps(web_cfg)))]
            subprocess.run(cmd, check=True, env=env)
    if not legacy and not kept and cfg and yes('Перезапустить выходы по одному сейчас (каждый ~10–60 с без связи)', True):
        failed = ops.rolling_restart(list(cfg), progress=lambda t: say('  '+t))
        if failed:
            say(f'Остановлено на {failed}; остальные работают. Копия прежней версии — sudo exitpool backups.')
            return 2
    say('')
    show_status()
    return 0


# ---------------------------------------------------------------- entry point

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--plan', action='store_true', help='только сканирование и бюджет, без изменений')
    parser.add_argument('--add', action='store_true', help='добавить части к установленному exitpool')
    parser.add_argument('--upgrade', action='store_true', help='обновить exitpool или перенести happ-service')
    parser.add_argument('--proxy', help='прокси для скачиваний (лучше через переменную EXITPOOL_PROXY)')
    parser.add_argument('--binaries-dir', help='папка с amneziawg-go, awg и xray вместо скачивания')
    parser.add_argument('--build-binaries', action='store_true', help='собрать бинарники из исходников, не скачивая')
    parser.add_argument('--force-budget', action='store_true', help='продолжать, даже если памяти по оценке не хватает')
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.exit(1, 'Нужен Python 3.10 или новее.\n')
    if not sys.stdin.isatty():
        parser.exit(1, 'Мастеру нужен терминал: ssh -t …, а из gist — bash <(curl …) или curl … | sudo bash -s -- …\n')
    os.umask(0o077)

    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if platform.system() != 'Linux' or platform.machine() != 'x86_64':
            raise SetupError('exitpool работает на Linux amd64.')
        if not args.plan and os.geteuid() != 0:
            raise SetupError('Запустите через sudo.')
        if not shutil.which('systemctl') or not Path('/run/systemd/system').is_dir():
            raise SetupError('Нужна система, запущенная под systemd.')
        proxy = fetch.parse_proxy(args.proxy) if args.proxy else fetch.env_proxy() or fetch.load_saved_proxy()
        say(f'exitpool {paths.VERSION} — выходы Happ и AmneziaWG для балансеров 3x-ui.')
        import installer
        installed = (paths.OPT/'instances.json').exists()
        legacy = paths.LEGACY_ETC.exists() and not installed
        kept = installer.kept_config() and not legacy
        try:
            resume = installed and installer.pending_migration() == 'resume'
        except installer.InstallError as exc:
            raise SetupError(str(exc)) from None
        if args.upgrade or ((legacy or kept or resume) and not args.plan):
            if not installed and not legacy and not kept:
                raise SetupError('Обновлять нечего: exitpool не установлен. Запустите мастер без --upgrade.')
            if legacy and not args.upgrade and not yes('Найдена установка happ-service. Перенести её в exitpool', True):
                return 0
            if kept and not args.upgrade and not yes('Найдены сохранённые настройки exitpool (программа удалена). '
                                                     'Восстановить их', True):
                return 0
            if resume and not args.upgrade and not yes('Прошлый перенос happ-service не завершён. Продолжить его', True):
                return 0
            return upgrade(args, proxy)
        if installed and not args.add and not args.plan:
            say('exitpool уже установлен: добавляем части. Обновление — --upgrade, управление — sudo exitpool.')
        return install_or_add(args, proxy, add_mode=installed)
    except (KeyboardInterrupt, EOFError):
        say('\nОтменено.')
        return 130
    except subprocess.CalledProcessError:
        say('\nКоманда установки завершилась с ошибкой (вывод выше). Автоматического удаления настроек нет; '
            'состояние: sudo exitpool status.')
        return 1
    except (SetupError, ops.OpsError, fetch.FetchError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        say('\n'+str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
