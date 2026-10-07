#!/usr/bin/env python3
"""exitpool — управление выходами (Happ и AmneziaWG) для балансеров 3x-ui.

Без аргументов — меню. С аргументами — команды (sudo exitpool --help).
Секреты (профили, подписка, токены, пароли) вводятся скрыто или берутся из файлов и не печатаются.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402
import ops  # noqa: E402
import memory_budget  # noqa: E402
from fetch import FetchError  # noqa: E402

REASONS = {'upstream': 'сервер не отвечает', 'profile_missing': 'профиль пропал из подписки',
           'country_mismatch': 'сменилась страна', 'manual': 'переподключение по запросу'}
PHASES = {'running': 'работает', 'degraded': 'нет связи', 'connecting': 'подключается', 'starting': 'запускается',
          'stopped': 'остановлен', 'failed': 'сбой', 'stale': 'нет данных', 'refreshing': 'обновляет подписку',
          'stopping': 'останавливается', 'unknown': 'нет данных'}


class CliError(RuntimeError):
    pass


def need_root():
    if os.geteuid() != 0:
        raise CliError('Нужны права root: sudo exitpool …')


def ask(label, default=''):
    suffix = f' [{default}]' if default != '' else ''
    return input(label+suffix+': ').strip() or str(default)


def yes(label, default=False):
    while True:
        value = ask(label+' (да/нет)', 'да' if default else 'нет').lower()
        if value in ('да', 'д', 'yes', 'y'):
            return True
        if value in ('нет', 'н', 'no', 'n'):
            return False


def number(label, default, low, high):
    while True:
        try:
            value = int(ask(label, default))
            if low <= value <= high:
                return value
        except ValueError:
            pass
        print(f'Нужно целое число от {low} до {high}.')


def read_hidden_block(prompt):
    """Multi-line secret (an AWG profile) without echo; ends with Ctrl+D."""
    print(prompt)
    import termios
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        return sys.stdin.read()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~termios.ECHO
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        text = sys.stdin.read()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print(f'Принято {len(text.splitlines())} строк.')
    return text


def read_profile(path=None, paste=False):
    if path == '-':
        return sys.stdin.read()
    if path:
        try:
            return Path(path).read_text()
        except OSError:
            raise CliError(f'Не удалось прочитать файл профиля: {path}') from None
    if paste:
        return read_hidden_block('Вставьте текст профиля .conf и нажмите Ctrl+D (ввод скрыт).')
    raise CliError('Укажите --profile ФАЙЛ (или - для stdin) или --paste.')


def choose_memory(name, default=memory_budget.AWG_DEFAULT_MODE):
    print(f'\nПамять для выхода {name}:')
    for index, mode in enumerate(memory_budget.AWG_MODES, 1):
        mark = ' (по умолчанию)' if mode == default else ''
        print(f'  {index} — {mode} МиБ: {memory_budget.AWG_MODE_TEXT[mode]}{mark}')
    print(f'  {len(memory_budget.AWG_MODES)+1} — своё значение')
    default_choice = memory_budget.AWG_MODES.index(default)+1 if default in memory_budget.AWG_MODES else len(memory_budget.AWG_MODES)+1
    choice = number('Вариант', default_choice, 1, len(memory_budget.AWG_MODES)+1)
    if choice <= len(memory_budget.AWG_MODES):
        return memory_budget.AWG_MODES[choice-1]
    return number('Своё значение, МиБ', default, memory_budget.AWG_MIN_MODE, 1024)


def jobs():
    return ops.Jobs()


def finish_job(job, ok_text=None):
    if job['phase'] == 'failed':
        raise CliError(job.get('error') or 'Не получилось.')
    if job.get('warnings'):
        print('Требуют внимания: '+', '.join(job['warnings']))
    elif ok_text:
        print(ok_text)


# ---------------------------------------------------------------- status

def dur(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return f'{s} с'
    if s < 3600:
        return f'{s//60} мин'
    if s < 172800:
        return f'{s//3600} ч {s%3600//60} мин'
    return f'{s//86400} д {s%86400//3600} ч'


def show_status(names=(), as_json=False):
    memory = ops.MemoryStats()
    memory.refresh()
    data = ops.status(memory, None)
    items = [i for i in data['instances'] if not names or i['name'] in names]
    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    now = time.time()
    host = data['host']
    print(f"exitpool {paths.VERSION} · выходов: {len(data['instances'])}, работают: "
          f"{sum(1 for i in data['instances'] if i['healthy'])} · RAM свободно {host.get('mem_available', '?')} МиБ")
    if not items:
        print('Выходов пока нет. Добавить AWG: sudo exitpool add awg ИМЯ --profile ФАЙЛ (или меню: sudo exitpool)')
        return
    for s in items:
        phase = PHASES.get(s['phase'], s['phase'])
        line = f"{s['name']:10} {phase:12} {s['kind'].upper():4} {s['tag']} → socks {s['address']}"
        print(line)
        if s['phase'] == 'running':
            print(f"    выход {s.get('exit_country') or '?'} {s.get('exit_ip') or ''}"
                  + (f" · задержка {s['latency']['last']} мс" if s['latency'].get('last') else ''))
        elif s['phase'] == 'degraded':
            down = dur(now-s['down_since']) if s.get('down_since') else '?'
            print(f"    нет связи {down}: {REASONS.get(s.get('degraded_reason'), s.get('degraded_reason') or '')}")
        if s.get('error') and s['phase'] not in ('running',):
            print('    ошибка:', s['error'])
        if s['kind'] == 'awg':
            t = s.get('tunnel') or {}
            parts = [f"AWG: {s['profile']}", f"сервер {t.get('endpoint') or '?'}"]
            if 'rx' in t:   # live values are present only while the exit runs
                hs = f"{dur(t['handshake_age'])} назад" if t.get('handshake_age') is not None else '—'
                parts += [f'рукопожатие {hs}', f"↓{(t.get('rx') or 0)//2**20} МиБ ↑{(t.get('tx') or 0)//2**20} МиБ"]
            parts.append(f"режим памяти {s['memory_mode']} МиБ (ожидаемо ~{s['budget_mib']})"
                         + (f", сейчас {s['memory_mib']} МиБ" if s.get('memory_mib') else ''))
            print('    '+' · '.join(parts))
        else:
            print(f"    Happ: «{s['profile']}»" + (f" · RAM {s['memory_mib']} МиБ" if s.get('memory_mib') else '')
                  + f" · подписка {s.get('subscription_updated') or 'не загружена'}")
        day = s['day']
        print(f"    24 ч: {day['availability'] if day['availability'] is not None else '?'}% · "
              f"простоев {day['outages']} ({dur(day['downtime'])}) · перезапусков {s['unit']['restarts']}")


# ---------------------------------------------------------------- AWG exits

def add_awg(args):
    need_root()
    text = read_profile(args.profile, args.paste)
    memory = args.memory if args.memory else (choose_memory(args.name) if sys.stdin.isatty() and not args.yes
                                               else memory_budget.AWG_DEFAULT_MODE)
    data = {'name': args.name, 'profile': text, 'tag': args.tag or '', 'country': args.country or '',
            'label': args.label or '', 'memory_mib': memory, 'outbound': args.outbound or ''}
    if args.port:
        data['port'] = args.port
    job = jobs().awg_add_sync(data, progress=lambda t: print('  '+t), force_budget=args.force_budget)
    finish_job(job)


def replace_profile(args):
    need_root()
    text = read_profile(args.file, args.paste)
    j = jobs()
    j.awg_replace(args.name, text)
    finish_job(j.wait(lambda t: print('  '+t)))


def remove(args):
    need_root()
    cfg = ops.installed()
    if args.name not in cfg:
        raise CliError('Такого выхода нет.')
    if not args.yes and not yes(f"Удалить выход {args.name}? Исходящий {cfg[args.name]['tag']} в 3x-ui останется — "
                                'уберите его из балансеров', False):
        return
    result = ops.remove_instance(args.name)
    print(f"Удалён. Копия профиля: {result['backup_id']}. Не забудьте убрать {result['tag']} из панели 3x-ui.")


def set_values(args):
    need_root()
    changes = {}
    for item in args.values:
        key, _, value = item.partition('=')
        if not _:
            raise CliError(f'Ожидается ключ=значение: {item}')
        if value.lstrip('-').isdigit():
            value = int(value)
        elif value in ('', 'none', 'null'):
            value = None
        changes[key.strip()] = value
    ops.save_instance(args.name, changes)
    print(f'{args.name}: сохранено, выход перезапускается.')


def service_action(args):
    need_root()
    cfg = ops.installed()
    names = list(cfg) if args.all else [args.name]
    if not names or names == [None]:
        raise CliError('Укажите имя выхода или --all.')
    if args.action == 'restart' and args.all:
        failed = ops.rolling_restart(names, progress=lambda t: print('  '+t))
        if failed:
            raise CliError(f'Остановлено на {failed}; остальные работают.')
        return
    for name in names:
        if args.action in ('reconnect', 'probe'):
            ops.command(name, args.action)
        else:
            with ops.locked():
                ops.service(name, args.action)
        print(f'{name}: {args.action} — выполнено.')


def show_log(args):
    for line in ops.journal(args.name, args.lines):
        print(line)


# ---------------------------------------------------------------- 3x-ui

def outbounds(args):
    from configuration import make_outbounds
    items = make_outbounds(ops.installed())
    if not args.add:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        # The hint goes to stderr so that `exitpool outbounds > file.json` stays valid JSON.
        print('\nДобавьте эти socks-исходящие в 3x-ui (или: sudo exitpool outbounds --add).', file=sys.stderr)
        return
    need_root()
    from panel_outbounds import PanelClient, PanelError
    cfg = ops.load_panel()
    if cfg:
        url, token = ops.panel_url(cfg), cfg['token']
    else:
        url = ask('Адрес панели (http://127.0.0.1:ПОРТ/базовый-путь)')
        token = getpass.getpass('API-токен панели (скрытый ввод): ').strip()
    try:
        client = PanelClient(url, token)
        tags = client.add(items, paths.VAR/'panel-backups')
    except (PanelError, ValueError) as exc:
        raise CliError(str(exc)) from None
    print('Добавлены исходящие: '+', '.join(tags) if tags else 'Все исходящие уже есть в панели.')
    print('В балансеры их нужно включить отдельно (панель → Xray → Балансеры).')


def panel(args):
    if args.what == 'show':
        print(json.dumps(ops.panel_public(ops.load_panel()), ensure_ascii=False, indent=2))
    elif args.what == 'clear':
        need_root()
        ops.clear_panel()
        print('Настройки панели удалены.')
    else:
        need_root()
        settings = {'host': ask('IP или имя панели', '127.0.0.1'), 'port': number('Порт панели', 2053, 1, 65535),
                    'base_path': ask('Базовый путь (если задан)', ''), 'https': yes('HTTPS', False),
                    'token': getpass.getpass('API-токен (скрытый ввод, пусто — прежний): ').strip()}
        print(json.dumps(ops.save_panel(settings), ensure_ascii=False))


# ---------------------------------------------------------------- web, access, proxy, binaries

def web_cmd(args):
    import web
    need_root()
    if args.what == 'show':
        try:
            cfg = web.load_config()
            state = subprocess.run(['systemctl', 'is-active', 'exitpool-web.service'], capture_output=True, text=True).stdout.strip()
            print(f"http://{cfg['listen']}:{cfg['port']}/ для {', '.join(cfg['allow'])} · служба: {state}")
        except (OSError, ValueError):
            print('Веб-интерфейс не настроен: sudo exitpool web enable')
    elif args.what == 'disable':
        subprocess.run(['systemctl', 'disable', '--now', 'exitpool-web.service'])
        print('Веб-интерфейс выключен (настройки сохранены).')
    elif args.what == 'passwd':
        web.load_config()   # not configured: a clear error before asking for a password
        web.change_password(web.read_new_password(False))
        print('Пароль изменён; прежние веб-сессии завершены.')
    else:
        import setup
        cfg = None
        try:
            cfg = web.load_config()
        except (OSError, ValueError):
            pass
        if not cfg or yes('Изменить адрес, подсеть и пароль', False):
            cfg = setup.ask_web(force=True)
            if not cfg:
                return
            web.save_config(cfg)
        subprocess.run(['systemctl', 'enable', '--now', 'exitpool-web.service'], check=True)
        subprocess.run(['systemctl', 'restart', 'exitpool-web.service'])
        print(f"Веб-интерфейс: http://{cfg['listen']}:{cfg['port']}/")


def access(args):
    import access_policy
    need_root()
    try:
        if args.what == 'allow':
            access_policy.allow(args.interface, args.subnet)
        elif args.what == 'deny':
            access_policy.deny(args.interface, args.subnet)
    except (RuntimeError, ValueError) as exc:
        raise CliError(str(exc)) from None
    policy = access_policy.load()
    nets = policy['allowed_container_networks']
    print('Доступ к реле: сам сервер — всегда; пересылаемый трафик (контейнеры, другие машины) — '
          + ('только из: '+', '.join(f"{r['interface']} {r['subnet']}" for r in nets) if nets else 'запрещён'))


def proxy(args):
    import fetch
    need_root()
    if args.what == 'clear':
        fetch.clear_proxy()
        print('Прокси для скачиваний удалён.')
        return
    if args.what in ('show', 'test'):
        current = fetch.load_saved_proxy()
        print('Прокси: '+fetch.masked(current))
        if args.what == 'test' and current:
            ok, error = fetch.probe_proxy(current)
            print('github.com через прокси: '+('OK' if ok else 'ошибка — '+error))
        return
    url = args.url or ask('Прокси (http://, https://, socks5h://, socks5://, socks4a://, socks4://)')
    proxy_value = fetch.parse_proxy(url)
    if not proxy_value:
        raise CliError('Укажите адрес прокси; отмена — Ctrl+C.')
    if not proxy_value['user']:
        user = ask('Логин (пусто — без авторизации)', '')
        if user:
            proxy_value['user'] = user
            proxy_value['password'] = getpass.getpass('Пароль прокси (скрытый ввод): ')
    ok, error = fetch.probe_proxy(proxy_value)
    print('Проверка через прокси: '+('OK' if ok else 'ошибка — '+error))
    if ok or yes('Всё равно сохранить', False):
        fetch.save_proxy(proxy_value)
        print('Сохранено: '+fetch.masked(proxy_value))


def binaries_cmd(args):
    import fetch
    if args.what == 'status':
        files = fetch.manifest()['files']
        for name in paths.AWG_BINARIES:
            path = paths.BIN/name
            state = 'нет' if not path.exists() else 'релизный' if fetch.sha256(path) == files[name]['sha256'] \
                else 'своя сборка'
            print(f'{name:13} {state}')
        return
    need_root()
    current = fetch.load_saved_proxy() or fetch.env_proxy()
    try:
        if args.what == 'download':
            fetch.download_release(current, progress=print)
        elif args.what == 'from-dir':
            fetch.install_from_dir(args.dir, strict=not args.allow_local_build, progress=print)
        else:
            print('Сборка поставит пакеты компилятора (после сборки они удаляются), займёт ~1 ГБ диска временно.')
            if args.yes or yes('Собрать из исходников', True):
                fetch.build_from_source(progress=print, proxy=current)
    except fetch.FetchError as exc:
        raise CliError(str(exc)) from None


def preflight_cmd(args):
    import preflight
    result = preflight.collect(args.awg or [memory_budget.AWG_DEFAULT_MODE], self_test=args.self_test)
    print(preflight.render(result))
    return 1 if result["verdict"] == "FAIL" else 0


# ---------------------------------------------------------------- Happ subscription, backups

def subscription(args):
    need_root()
    from configuration import validate_subscription
    print('Вставьте новую ссылку happ://crypt5/… (скрытый ввод).')
    key = getpass.getpass('Подписка: ').strip()
    try:
        validate_subscription(key)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    j = jobs()
    j.discover(key)
    state = j.wait(lambda t: print('  '+t))
    if state['phase'] != 'awaiting_selection':
        finish_job(state)
        return
    catalog = state['catalog']
    counts = {n: catalog.count(n) for n in catalog}
    unique = [n for n in catalog if counts[n] == 1]
    for index, name in enumerate(unique, 1):
        print(f'  {index}. {name}')
    mapping = {}
    for name, proposal in state['proposal'].items():
        default = unique.index(proposal['profile_name'])+1 if proposal['profile_name'] in unique else 1
        choice = number(f'Профиль для {name}', default, 1, len(unique))
        country = ask(f'Страна выхода для {name} (код, пусто — любая)', proposal.get('country') or '')
        mapping[name] = {'profile_name': unique[choice-1], 'country': country.upper()}
    if not yes('Заменить подписку? Выходы переключатся по одному', False):
        j.cancel()
        return
    j.apply(mapping)
    finish_job(j.wait(lambda t: print('  '+t)), 'Готово: подписка заменена.')


def backups(args):
    if args.restore:
        need_root()
        j = jobs()
        j.restore(args.restore)
        finish_job(j.wait(lambda t: print('  '+t)), 'Восстановление завершено.')
        return
    for b in ops.list_backups():
        when = time.strftime('%d.%m %H:%M', time.localtime(b['created'])) if b.get('created') else '?'
        print(f"{b['id']}  {when}  {b['label']}  " + ', '.join(f'{k}: {v}' for k, v in (b.get('instances') or {}).items()))


def upgrade(args):
    need_root()
    source = Path(args.source) if args.source else None
    if not source or not (source/'lib'/'installer.py').exists():
        raise CliError('Укажите распакованный комплект новой версии: sudo exitpool upgrade --from ПАПКА\n'
                       'Проще — команда из gist: curl -fsSL …/exitpool-install.sh | sudo bash -s -- --upgrade')
    cmd = ['python3', str(source/'lib'/'installer.py'), 'upgrade']
    if args.binaries_dir:
        cmd += ['--binaries-dir', args.binaries_dir]
    elif args.binaries:
        cmd += ['--binaries', args.binaries]
    subprocess.run(cmd, check=True)
    if ops.installed() and yes('Перезапустить выходы по одному сейчас', True):
        failed = ops.rolling_restart(list(ops.installed()), progress=lambda t: print('  '+t))
        if failed:
            raise CliError(f'Остановлено на {failed}.')


# ---------------------------------------------------------------- menu

def menu():
    need_root()
    actions = [
        ('Статус выходов', lambda: show_status()),
        ('Добавить AWG-выход', menu_add_awg),
        ('Выход: перезапуск, журнал, настройки, замена профиля, удаление', menu_exit),
        ('Заменить подписку Happ', lambda: subscription(None)),
        ('Резервные копии', lambda: backups(argparse.Namespace(restore=None))),
        ('Исходящие для 3x-ui / связь с панелью', menu_panel),
        ('Веб-интерфейс', menu_web),
        ('Доступ контейнеров к реле', lambda: access(argparse.Namespace(what='show'))),
        ('Прокси для скачиваний', lambda: proxy(argparse.Namespace(what='set', url=None))),
        ('Проверка сервера', lambda: preflight_cmd(argparse.Namespace(awg=None, self_test=True))),
    ]
    while True:
        try:
            data = ops.status()
            working = sum(1 for i in data['instances'] if i['healthy'])
            header = (f"exitpool {paths.VERSION} · выходов {len(data['instances'])}: работают {working} · "
                      f"RAM свободно {data['host'].get('mem_available', '?')} МиБ")
        except ops.OpsError as exc:
            header = f'exitpool {paths.VERSION} · {exc}'
        print('\n'+header+'\n')
        for index, (title, _) in enumerate(actions, 1):
            print(f' {index:2}. {title}')
        print('  0. Выход')
        choice = ask('>', '')
        if choice in ('0', 'q', ''):
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(actions):
            continue
        try:
            actions[int(choice)-1][1]()
        except (CliError, ops.OpsError, ValueError) as exc:
            print('\n'+str(exc))
        except KeyboardInterrupt:
            print('\nОтменено.')


def pick_exit():
    cfg = ops.installed()
    names = list(cfg)
    if not names:
        raise CliError('Выходов нет.')
    for index, name in enumerate(names, 1):
        print(f'  {index}. {name} ({cfg[name].get("kind", "happ")}, {cfg[name]["tag"]})')
    return names[number('Выход', 1, 1, len(names))-1]


def menu_add_awg():
    name = ask('Имя выхода (латиница, например fi2)')
    path = ask('Путь к файлу .conf (пусто — вставить текст)', '')
    args = argparse.Namespace(name=name, profile=path or None, paste=not path, tag=ask('Тег в 3x-ui', 'awg-'+name if not name.startswith('awg') else name),
                              country=ask('Только страна (код, пусто — любая)', ''), label=ask('Подпись (необязательно)', ''),
                              memory=None, port=None, outbound=None, yes=False, force_budget=False)
    add_awg(args)


def menu_exit():
    name = pick_exit()
    kind = ops.installed()[name].get('kind', 'happ')
    options = ['Перезапустить', 'Переподключить', 'Замерить', 'Журнал', 'Остановить', 'Запустить']
    if kind == 'awg':
        options += ['Сменить режим памяти', 'Заменить профиль', 'Удалить']
    for index, title in enumerate(options, 1):
        print(f'  {index}. {title}')
    title = options[number('Действие', 1, 1, len(options))-1]
    simple = {'Перезапустить': 'restart', 'Переподключить': 'reconnect', 'Замерить': 'probe',
              'Остановить': 'stop', 'Запустить': 'start'}
    if title in simple:
        service_action(argparse.Namespace(action=simple[title], name=name, all=False))
    elif title == 'Журнал':
        show_log(argparse.Namespace(name=name, lines=40))
    elif title == 'Сменить режим памяти':
        mode = choose_memory(name, ops.installed()[name]['memory_mib'])
        ops.save_instance(name, {'memory_mib': mode})
        print('Сохранено, выход перезапускается.')
    elif title == 'Заменить профиль':
        path = ask('Путь к новому .conf (пусто — вставить текст)', '')
        replace_profile(argparse.Namespace(name=name, file=path or None, paste=not path))
    elif title == 'Удалить':
        remove(argparse.Namespace(name=name, yes=False))


def menu_panel():
    print('  1. Показать JSON исходящих\n  2. Добавить исходящие в 3x-ui через API\n  3. Настроить связь с панелью (выбор балансера на карточках)')
    choice = number('Действие', 1, 1, 3)
    if choice == 1:
        outbounds(argparse.Namespace(add=False))
    elif choice == 2:
        outbounds(argparse.Namespace(add=True))
    else:
        panel(argparse.Namespace(what='set'))


def menu_web():
    print('  1. Показать адрес\n  2. Включить / настроить\n  3. Выключить\n  4. Сменить пароль')
    what = {1: 'show', 2: 'enable', 3: 'disable', 4: 'passwd'}[number('Действие', 1, 1, 4)]
    web_cmd(argparse.Namespace(what=what))


# ---------------------------------------------------------------- arguments

def parser():
    p = argparse.ArgumentParser(prog='exitpool', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command')
    s = sub.add_parser('status', help='состояние выходов')
    s.add_argument('names', nargs='*')
    s.add_argument('--json', action='store_true')
    a = sub.add_parser('add', help='добавить выход: exitpool add awg ИМЯ --profile ФАЙЛ')
    a.add_argument('kind', choices=['awg'])
    a.add_argument('name')
    a.add_argument('--profile', help='файл .conf или - для stdin')
    a.add_argument('--paste', action='store_true', help='вставить текст профиля (скрыто)')
    a.add_argument('--tag')
    a.add_argument('--country')
    a.add_argument('--label')
    a.add_argument('--memory', type=int, help='режим памяти: 32, 64 (по умолчанию), 128 или своё ≥32')
    a.add_argument('--port', type=int)
    a.add_argument('--outbound', choices=['veth', 'loopback'], help='адрес для 3x-ui: veth (по умолчанию) или 127.0.0.1')
    a.add_argument('--yes', action='store_true')
    a.add_argument('--force-budget', action='store_true', help='добавить, даже если памяти по оценке не хватает')
    r = sub.add_parser('profile', help='заменить профиль AWG-выхода')
    r.add_argument('name')
    r.add_argument('--file')
    r.add_argument('--paste', action='store_true')
    d = sub.add_parser('remove', help='удалить AWG-выход')
    d.add_argument('name')
    d.add_argument('--yes', action='store_true')
    st = sub.add_parser('set', help='настройки выхода: exitpool set ИМЯ memory_mib=64 mtu=1380 …')
    st.add_argument('name')
    st.add_argument('values', nargs='+')
    for action in ('start', 'stop', 'restart', 'reconnect', 'probe'):
        x = sub.add_parser(action, help=f'{action} выхода')
        x.add_argument('name', nargs='?')
        x.add_argument('--all', action='store_true')
    lg = sub.add_parser('log', help='журнал службы выхода')
    lg.add_argument('name')
    lg.add_argument('-n', '--lines', type=int, default=80)
    o = sub.add_parser('outbounds', help='socks-исходящие для 3x-ui')
    o.add_argument('--add', action='store_true', help='добавить в панель через API')
    pn = sub.add_parser('panel', help='связь с панелью 3x-ui (необязательно)')
    pn.add_argument('what', choices=['show', 'set', 'clear'])
    w = sub.add_parser('web', help='веб-интерфейс (необязательно)')
    w.add_argument('what', choices=['show', 'enable', 'disable', 'passwd'])
    ac = sub.add_parser('access', help='доступ контейнеров к реле AWG')
    ac.add_argument('what', choices=['show', 'allow', 'deny'])
    ac.add_argument('interface', nargs='?')
    ac.add_argument('subnet', nargs='?')
    px = sub.add_parser('proxy', help='прокси для скачиваний')
    px.add_argument('what', choices=['show', 'set', 'clear', 'test'])
    px.add_argument('url', nargs='?')
    b = sub.add_parser('binaries', help='бинарники AWG')
    b.add_argument('what', choices=['status', 'download', 'from-dir', 'build'])
    b.add_argument('dir', nargs='?')
    b.add_argument('--allow-local-build', action='store_true')
    b.add_argument('--yes', action='store_true')
    pf = sub.add_parser('preflight', help='проверка сервера')
    pf.add_argument('--awg', type=int, action='append')
    pf.add_argument('--self-test', action='store_true')
    sub.add_parser('subscription', help='заменить подписку Happ')
    bk = sub.add_parser('backups', help='резервные копии')
    bk.add_argument('--restore', metavar='ID')
    up = sub.add_parser('upgrade', help='обновить из распакованного комплекта')
    up.add_argument('--from', dest='source')
    up.add_argument('--binaries', choices=['keep', 'download', 'build'])
    up.add_argument('--binaries-dir')
    sub.add_parser('setup', help='мастер: добавить Happ или AWG')
    sub.add_parser('cleanup-legacy', help='убрать файлы happ-service после переноса')
    sub.add_parser('version')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command is None:
            return menu()
        if args.command == 'status':
            return show_status(args.names, args.json)
        handlers = {'add': add_awg, 'profile': replace_profile, 'remove': remove, 'set': set_values,
                    'log': show_log, 'outbounds': outbounds, 'panel': panel, 'web': web_cmd, 'access': access,
                    'proxy': proxy, 'binaries': binaries_cmd, 'preflight': preflight_cmd,
                    'subscription': subscription, 'backups': backups, 'upgrade': upgrade}
        if args.command in ('start', 'stop', 'restart', 'reconnect', 'probe'):
            args.action = args.command
            return service_action(args)
        if args.command == 'version':
            print(f'exitpool {paths.VERSION}')
            return 0
        if args.command == 'setup':
            need_root()
            return subprocess.run(['python3', str(paths.LIB/'setup.py'), '--add']).returncode
        if args.command == 'cleanup-legacy':
            need_root()
            return subprocess.run(['python3', str(paths.LIB/'installer.py'), 'cleanup-legacy']).returncode
        if args.command == 'access' and args.what in ('allow', 'deny') and not args.interface:
            p.error('укажите интерфейс моста (и подсеть для allow)')
        return handlers[args.command](args)
    except PermissionError:
        if os.environ.get('EXITPOOL_DEBUG'):
            raise
        print('Недостаточно прав: sudo exitpool …', file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        if os.environ.get('EXITPOOL_DEBUG'):
            raise
        print('Команда завершилась с ошибкой (подробности выше).', file=sys.stderr)
        return 1
    except (CliError, ops.OpsError, FetchError, ValueError) as exc:
        if os.environ.get('EXITPOOL_DEBUG') and not isinstance(exc, (CliError, ops.OpsError, FetchError)):
            raise   # EXITPOOL_DEBUG=1: the full traceback of an unexpected ValueError
        print(exc, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nОтменено.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main() or 0)
