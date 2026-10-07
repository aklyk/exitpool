#!/usr/bin/env python3
"""Privileged installation steps of exitpool (called by install.sh, the wizard and `exitpool upgrade`).

  base      code, units, guard, optional web interface and AWG binaries; no exits yet
  happ      add Happ exits (Docker image, subscription, imported state)
  upgrade   new code/units/binaries for an installed exitpool, or migration from happ-service 1.x
  uninstall stop and disable services (configuration, profiles and keys are kept)
  purge     remove code, units and network objects; --all also removes keys, profiles and settings
Nothing here prints secrets. Exits are (re)started one at a time.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

SOURCE = Path(__file__).resolve().parent.parent
UNITS = ('exitpool-happ@.service', 'exitpool-awg@.service', 'exitpool-awg-relay@.service',
         'exitpool-awg-cleanup@.service', 'exitpool-awg-loopback@.socket', 'exitpool-awg-loopback@.service',
         'exitpool-awg-guard.service', 'exitpool-web.service')
LEGACY_UNITS = {'happ': 'happ@{}.service', 'awg': 'happ-awg@{}.service'}


class InstallError(RuntimeError):
    pass


def say(text):
    print(text, flush=True)


def run(args, check=True, timeout=600, **kwargs):
    r = subprocess.run([str(a) for a in args], text=True, timeout=timeout, **kwargs)
    if check and r.returncode:
        raise InstallError(f'Команда завершилась с ошибкой: {" ".join(str(a) for a in args[:3])}')
    return r


def quiet(args, timeout=120):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)


def write_private(path, text, mode=0o600):
    path = Path(path)
    temp = path.with_name('.'+path.name+'.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as out:
        out.write(text)
    os.chmod(temp, mode)
    temp.replace(path)


# ---------------------------------------------------------------- files

def ensure_dirs():
    for path, mode in ((paths.ETC, 0o700), (paths.ETC/'instances', 0o700), (paths.AWG_ETC, 0o700),
                       (paths.VAR, 0o700), (paths.OPT, 0o755), (paths.LIB, 0o755), (paths.BIN, 0o755)):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)


def copy_tree(src, dst, suffixes=None):
    dst.mkdir(mode=0o755, parents=True, exist_ok=True)
    dst.chmod(0o755)
    for item in sorted(Path(src).iterdir()):
        if item.name.startswith('.') and item.name != '.dockerignore' or item.name == '__pycache__':
            continue
        if item.is_dir():
            copy_tree(item, dst/item.name, suffixes)
        elif suffixes is None or item.suffix in suffixes or item.name in ('Dockerfile', '.dockerignore'):
            shutil.copyfile(item, dst/item.name)
            (dst/item.name).chmod(0o755 if item.suffix in ('.sh',) else 0o644)


def install_code():
    if SOURCE.resolve() == paths.OPT.resolve():
        raise InstallError('Установка из уже установленной копии невозможна: нужен распакованный архив новой версии.')
    ensure_dirs()
    for old in paths.LIB.glob('*.py'):
        old.unlink()
    copy_tree(SOURCE/'lib', paths.LIB, {'.py'})
    for name in ('awgnet.py', 'cli.py', 'installer.py', 'preflight.py', 'fetch.py', 'web.py', 'setup.py'):
        if (paths.LIB/name).exists():
            (paths.LIB/name).chmod(0o755)
    shutil.rmtree(paths.OPT/'webui', ignore_errors=True)
    copy_tree(SOURCE/'webui', paths.OPT/'webui')
    shutil.rmtree(paths.OPT/'happ', ignore_errors=True)
    copy_tree(SOURCE/'happ', paths.OPT/'happ')
    copy_tree(SOURCE/'release', paths.OPT/'release')
    (paths.OPT/'VERSION').write_text(paths.VERSION+'\n')
    shutil.copyfile(SOURCE/'uninstall.sh', paths.OPT/'uninstall.sh')
    (paths.OPT/'uninstall.sh').chmod(0o755)
    paths.UNIT_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in UNITS:
        shutil.copyfile(SOURCE/'systemd'/name, paths.UNIT_DIR/name)
        (paths.UNIT_DIR/name).chmod(0o644)
    wrapper = wrapper_path()
    wrapper.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    wrapper.write_text('#!/bin/sh\nexec /usr/bin/python3 /opt/exitpool/lib/cli.py "$@"\n')
    wrapper.chmod(0o755)
    run(['systemctl', 'daemon-reload'])


def wrapper_path(name='exitpool'):
    return paths.ROOT/'usr/local/bin'/name


def ops():
    import ops as module
    module.set_root(paths.ROOT)
    return module


def binaries(mode, directory=None, proxy=None, local_build=False):
    import fetch
    if mode == 'skip':
        return
    if mode == 'dir':
        fetch.install_from_dir(directory, strict=not local_build, progress=say)
    elif mode == 'download':
        fetch.download_release(proxy, progress=say)
    elif mode == 'build':
        fetch.build_from_source(progress=say, proxy=proxy)
    elif mode == 'keep':
        if not all((paths.BIN/n).exists() for n in paths.AWG_BINARIES):
            raise InstallError('Бинарники AWG не установлены.')
    else:
        raise InstallError('Неизвестный способ получения бинарников.')


def install_web(config_file):
    if not config_file:
        if (paths.ETC/'web.json').exists():
            quiet(['systemctl', 'try-restart', 'exitpool-web.service'])
        return
    import web
    cfg = web.validate_config(json.loads(Path(config_file).read_text()))
    if not cfg['password'].startswith('pbkdf2_sha256$'):
        raise InstallError('В настройках веба нужен хеш пароля.')
    write_private(paths.ETC/'web.json', json.dumps(cfg, indent=2)+'\n')
    run(['systemctl', 'enable', '--now', 'exitpool-web.service'])
    run(['systemctl', 'restart', 'exitpool-web.service'])


def guard():
    run(['systemctl', 'enable', 'exitpool-awg-guard.service'])
    run(['systemctl', 'restart', 'exitpool-awg-guard.service'])


# ---------------------------------------------------------------- base install

def base(web_config=None, binaries_mode='skip', binaries_dir=None, proxy=None, local_build=False):
    if (paths.OPT/'instances.json').exists():
        raise InstallError('exitpool уже установлен: используйте --upgrade.')
    if paths.LEGACY_ETC.exists():
        raise InstallError('Найдена установка happ-service: используйте --upgrade (она перенесётся в exitpool).')
    if any((paths.ETC/'instances').glob('*.json')):
        raise InstallError('В /etc/exitpool остались настройки прежней установки. Восстановить их: мастер с --upgrade '
                           '(или sudo bash install.sh reinstall); удалить: sudo bash uninstall.sh --purge --all.')
    install_code()
    o = ops()
    o.write_instances({})
    binaries(binaries_mode, binaries_dir, proxy, local_build)
    guard()
    install_web(web_config)
    say('exitpool установлен. Команда управления: sudo exitpool')


# ---------------------------------------------------------------- Happ exits

def happ(subscription_file, instances_file, seed_state=None):
    o = ops()
    from configuration import validate_instances, validate_subscription
    import image_build
    if not shutil.which('docker'):
        raise InstallError('Для Happ нужен Docker 28+.')
    new = validate_instances(json.loads(Path(instances_file).read_text()))
    cfg = o.installed()
    clash = set(new) & set(cfg)
    if clash:
        raise InstallError('Такие выходы уже есть: '+', '.join(sorted(clash)))
    key = validate_subscription(Path(subscription_file).read_text())
    say('Готовлю образ Happ (если его ещё нет, сборка займёт несколько минут)…')
    image_build.prepare_image(paths.OPT/'happ')
    write_private(paths.ETC/'subscription.secret', key+'\n')
    merged = dict(cfg)
    merged.update(new)
    o.write_instances(merged)
    for name in new:
        state = paths.VAR/name
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if seed_state:
            for item in ('config', 'data', 'catalog.json'):
                src = Path(seed_state)/item
                if src.is_dir():
                    shutil.copytree(src, state/item, dirs_exist_ok=True)
                elif src.exists():
                    shutil.copy2(src, state/item)
    o.ensure_happ_network()
    for name in new:
        run(['systemctl', 'enable', '--now', f'exitpool-happ@{name}.service'])
        time.sleep(4)
    say('Happ-выходы запущены: '+', '.join(new))


# ---------------------------------------------------------------- upgrade and migration

def backup_dir(label):
    stamp = time.strftime('%Y%m%d-%H%M%S')
    parent = paths.VAR/'backups'
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    index = 0
    while True:
        suffix = f'-{index}' if index else ''
        target = parent/f'{stamp}-{label}{suffix}'
        try:
            target.mkdir(mode=0o700)
            return target
        except FileExistsError:
            index += 1


def migration_state():
    try:
        state = json.loads((paths.VAR/'migration-state.json').read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def legacy_active_units():
    """happ-service units that are still running (each holds a key or a port of some exit)."""
    try:
        old = json.loads((paths.LEGACY_OPT/'instances.json').read_text())
    except (OSError, ValueError):
        old = {}
    units = [LEGACY_UNITS[c.get('kind', 'happ')].format(n) for n, c in old.items()]+['happ-web.service']
    return [u for u in units if quiet(['systemctl', 'is-active', u]).stdout.strip() in ('active', 'activating', 'reloading')]


def pending_migration():
    """'resume' when a migration stopped part-way, None otherwise. Refuses side-by-side states it cannot reason about.

    A failed migration leaves the new manifest next to restored happ-service exits; an ordinary upgrade there would
    restart exitpool units whose keys the old exits still use.
    """
    if not paths.LEGACY_ETC.exists() or not (paths.OPT/'instances.json').exists():
        return None
    phase = migration_state().get('phase')
    if phase == 'started':
        return 'resume'
    active = legacy_active_units()
    if active:
        raise InstallError('Рядом с exitpool работают службы happ-service ('+', '.join(active)+'), а незавершённого '
                           'переноса не записано. Обновление остановлено, чтобы не запустить один ключ дважды; '
                           'остановите старые службы или удалите exitpool и повторите перенос.')
    return None


def kept_config():
    """Settings left by `uninstall.sh --purge`: instances under /etc, but no program in /opt."""
    return not (paths.OPT/'instances.json').exists() and any((paths.ETC/'instances').glob('*.json'))


def upgrade(web_config=None, binaries_mode='keep', binaries_dir=None, proxy=None, local_build=False):
    if pending_migration() == 'resume':
        return migrate(web_config, binaries_mode, binaries_dir, proxy, local_build, resume=True)
    if paths.LEGACY_ETC.exists() and not (paths.OPT/'instances.json').exists():
        return migrate(web_config, binaries_mode, binaries_dir, proxy, local_build)
    if kept_config():
        return reinstall(web_config, binaries_mode, binaries_dir, proxy, local_build)
    if not (paths.OPT/'instances.json').exists():
        raise InstallError('exitpool не установлен.')
    o = ops()
    cfg = o.installed()
    target = backup_dir('upgrade')
    shutil.copytree(paths.ETC/'instances', target/'instances')
    if paths.AWG_ETC.exists():
        shutil.copytree(paths.AWG_ETC, target/'awg')
    shutil.copytree(paths.OPT, target/'opt', ignore=shutil.ignore_patterns('bin', '__pycache__'))
    (target/'meta.json').write_text(json.dumps({'id': target.name, 'created': time.time(), 'label': 'upgrade',
                                                'fingerprint': None, 'instances': {n: n for n in cfg}}))
    install_code()
    o = ops()
    o.write_instances(cfg)
    binaries(binaries_mode, binaries_dir, proxy, local_build)
    guard()
    if any(c.get('kind', 'happ') == 'happ' for c in cfg.values()):
        import image_build
        image_build.prepare_image(paths.OPT/'happ')
    install_web(web_config)
    say(f'Файлы новой версии установлены. Копия прежней: {target}')
    say('Выходы перезапускаются по одному командой: sudo exitpool restart --all')


def migrate(web_config=None, binaries_mode='keep', binaries_dir=None, proxy=None, local_build=False, resume=False):
    """happ-service 1.x -> exitpool: copy configuration, then switch exits one at a time with rollback.

    resume: an earlier run stopped part-way; exits already working under exitpool are left alone,
    the settings written then (slots in particular) are kept.
    """
    if resume:
        say('Продолжаю прерванный перенос happ-service: уже перенесённые выходы не трогаю.')
    else:
        say('Найдена установка happ-service: переношу её в exitpool. Старые файлы остаются до явной уборки.')
    legacy_manifest = paths.LEGACY_OPT/'instances.json'
    if not legacy_manifest.exists():
        raise InstallError('У happ-service нет /opt/happ-service/instances.json — перенос вручную.')
    old = json.loads(legacy_manifest.read_text())
    ensure_dirs()
    write_private(paths.VAR/'migration-state.json', json.dumps({'phase': 'started', 'instances': sorted(old)}))
    target = backup_dir('migration')
    shutil.copytree(paths.LEGACY_ETC, target/'happ-service-etc')
    for item in ('subscription.secret', 'panel.json', 'web.json'):
        if (paths.LEGACY_ETC/item).exists() and not (resume and (paths.ETC/item).exists()):
            write_private(paths.ETC/item, (paths.LEGACY_ETC/item).read_text())
    if (paths.LEGACY_ETC/'awg').is_dir():
        for profile in (paths.LEGACY_ETC/'awg').glob('*.conf'):
            if not (resume and (paths.AWG_ETC/profile.name).exists()):
                write_private(paths.AWG_ETC/profile.name, profile.read_text())
    import ops as current
    current.set_root(paths.ROOT)
    kept = {}
    if resume:
        try:
            kept = current.installed()
        except current.OpsError:
            kept = {}
    taken = current.routes_10_250() | {c['slot'] for c in kept.values() if c.get('kind') == 'awg'}
    new, slot = {}, 1
    for name, values in old.items():
        if name in kept:
            new[name] = kept[name]
            continue
        values = dict(values)
        if values.get('kind') == 'awg':
            while slot in taken:
                slot += 1
            # Docker memory limit -> memory mode; keep 127.0.0.1:PORT so 3x-ui needs no change.
            values.update(memory_mib=64, slot=slot, outbound='loopback',
                          health_interval=min(int(values.get('health_interval', 10)), 10))
            slot += 1
        new[name] = values
    if any(v.get('kind') == 'awg' for v in new.values()) and binaries_mode == 'keep' \
            and not all((paths.BIN/n).exists() for n in paths.AWG_BINARIES):
        raise InstallError('Для переноса AWG-выходов нужны бинарники: --binaries download|dir|build.')
    install_code()
    o = ops()
    o.write_instances(new)
    binaries(binaries_mode, binaries_dir, proxy, local_build)
    guard()
    for name, values in new.items():
        src = paths.LEGACY_VAR/name
        if src.is_dir() and not (paths.VAR/name).exists():
            shutil.copytree(src, paths.VAR/name, symlinks=True)
        (paths.VAR/name).mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(v.get('kind', 'happ') == 'happ' for v in new.values()):
        import image_build
        say('Образ Happ: ' + image_build.prepare_image(paths.OPT/'happ'))
        o.ensure_happ_network()
    switched = []
    for name, values in new.items():
        kind = values.get('kind', 'happ')
        old_unit, new_unit = LEGACY_UNITS[kind].format(name), o.unit(name)
        if resume and already_switched(o, name, old_unit):
            say(f'{name}: уже работает под exitpool.')
            switched.append(name)
            continue
        say(f'{name}: переключаю {old_unit} → {new_unit}…')
        quiet(['systemctl', 'stop', new_unit])   # a leftover of the interrupted run must start afresh
        started = time.time()
        quiet(['systemctl', 'disable', '--now', old_unit])
        wait_port_free(values['port'])
        quiet(['systemctl', 'enable', '--now', new_unit])
        result = o.wait_running(name, started, timeout=240 if kind == 'happ' else 120)
        if result != 'running':
            say(f'{name}: новая служба не заработала ({result}); возвращаю {old_unit}.')
            quiet(['systemctl', 'disable', '--now', new_unit])
            if kind == 'awg':
                quiet(['python3', str(paths.LIB/'awgnet.py'), 'cleanup', name])
            back = time.time()
            quiet(['systemctl', 'enable', '--now', old_unit])
            state = ('прежний выход снова работает' if wait_legacy(name, back) else
                     f'прежний выход запущен, но ещё не прошёл проверку (sudo systemctl status {old_unit})')
            raise InstallError(f'Перенос остановлен на {name}: {state}. Уже перенесены: {", ".join(switched) or "нет"}; '
                               f'остальные работают по-старому. Повторный запуск с --upgrade продолжит перенос. '
                               f'Копия: {target}')
        switched.append(name)
        say(f'{name}: работает.')
    if (paths.LEGACY_ETC/'web.json').exists():
        quiet(['systemctl', 'disable', '--now', 'happ-web.service'])
    install_web(web_config)
    if (paths.ETC/'web.json').exists() and not web_config:
        run(['systemctl', 'enable', '--now', 'exitpool-web.service'])
    for helper in status_helpers():
        helper.write_text('#!/bin/sh\nexec /usr/local/bin/exitpool status "$@"\n')   # owner and mode are kept
    write_private(paths.VAR/'migration-state.json', json.dumps({'phase': 'complete', 'instances': sorted(new)}))
    say('Перенос завершён: все выходы работают под exitpool. Теги и порты для 3x-ui прежние.')
    say('Старые файлы happ-service сохранены; убрать их: sudo exitpool cleanup-legacy')


def already_switched(o, name, old_unit):
    """Resume: the exitpool unit runs and is healthy, the happ-service unit is down."""
    if quiet(['systemctl', 'is-active', old_unit]).stdout.strip() in ('active', 'activating', 'reloading'):
        return False
    if o.units([name])[name].get('ActiveState') != 'active':
        return False
    state = o.read_json(paths.VAR/name/'status.json', {}) or {}
    return state.get('phase') == 'running' and bool(state.get('healthy'))


def wait_legacy(name, since, timeout=90):
    """After a rollback: wait until the happ-service exit reports a working relay again."""
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            state = json.loads((paths.LEGACY_VAR/name/'status.json').read_text())
        except (OSError, ValueError):
            state = {}
        if state.get('started_at', 0) >= since-1 and state.get('phase') == 'running' and state.get('healthy'):
            return True
        time.sleep(2)
    return False


def status_helpers():
    """happ_status.sh copies of happ-service 1.x: the global one and the copy in the owner's home directory."""
    found = [wrapper_path('happ_status.sh'), paths.ROOT/'root'/'happ_status.sh',
             *sorted((paths.ROOT/'home').glob('*/happ_status.sh'))]
    result = []
    for path in found:
        try:
            if path.is_file() and not path.is_symlink() and ('happ-service' in path.read_text(errors='replace')
                                                             or path == wrapper_path('happ_status.sh')):
                result.append(path)
        except OSError:
            continue
    return result


def wait_port_free(port, timeout=20):
    import ops as o
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if o.port_free(port):
            return
        time.sleep(.5)


def reinstall(web_config=None, binaries_mode='keep', binaries_dir=None, proxy=None, local_build=False):
    """After `uninstall.sh --purge`: program and services again, from the kept settings, profiles and keys."""
    if not kept_config():
        raise InstallError('Сохранённых настроек exitpool нет.')
    if paths.LEGACY_ETC.exists():
        raise InstallError('Рядом есть happ-service: сначала перенос (--upgrade) или удаление одной из установок.')
    o = ops()
    cfg = o.installed()
    awg = any(c.get('kind') == 'awg' for c in cfg.values())
    if awg and binaries_mode in ('keep', 'skip') and not all((paths.BIN/n).exists() for n in paths.AWG_BINARIES):
        raise InstallError('Для AWG-выходов нужны бинарники: --binaries download|dir|build.')
    say('Восстанавливаю exitpool из сохранённых настроек: '+(', '.join(cfg) or 'выходов нет')+'.')
    install_code()
    o = ops()
    o.write_instances(cfg)
    binaries(binaries_mode, binaries_dir, proxy, local_build)
    guard()
    if any(c.get('kind', 'happ') == 'happ' for c in cfg.values()):
        import image_build
        say('Образ Happ: '+image_build.prepare_image(paths.OPT/'happ'))
        o.ensure_happ_network()
    install_web(web_config)
    if (paths.ETC/'web.json').exists() and not web_config:
        run(['systemctl', 'enable', '--now', 'exitpool-web.service'])
    failed = []
    for name in cfg:
        (paths.VAR/name).mkdir(mode=0o700, parents=True, exist_ok=True)
        started = time.time()
        quiet(['systemctl', 'enable', '--now', o.unit(name)])
        result = o.wait_running(name, started, timeout=240 if cfg[name].get('kind', 'happ') == 'happ' else 120)
        say(f'{name}: '+('работает.' if result == 'running' else f'пока не работает ({result}).'))
        if result != 'running':
            failed.append(name)
    say('Готово.'+(' Требуют внимания: '+', '.join(failed) if failed else ''))


def cleanup_legacy():
    """After a successful migration: old units, files and the old Docker network (if unused)."""
    if not paths.LEGACY_ETC.exists():
        say('Старой установки happ-service нет.')
        return
    o = ops()
    if not (paths.OPT/'instances.json').exists() or set(o.installed()) != set(json.loads((paths.LEGACY_OPT/'instances.json').read_text())):
        raise InstallError('Перенос не завершён полностью — старые файлы не удаляю.')
    try:
        completed = json.loads((paths.VAR/'migration-state.json').read_text())
    except (OSError, ValueError):
        completed = {}
    if completed.get('phase') != 'complete' or completed.get('instances') != sorted(o.installed()):
        raise InstallError('Перенос не завершён полностью — старые файлы не удаляю.')
    old = json.loads((paths.LEGACY_OPT/'instances.json').read_text())
    old_units = [LEGACY_UNITS[c.get('kind', 'happ')].format(n) for n, c in old.items()]+['happ-web.service']
    if any(quiet(['systemctl', 'is-active', unit]).stdout.strip() in ('active', 'activating', 'reloading')
           for unit in old_units):
        raise InstallError('Старые службы happ-service ещё работают — уборка запрещена, завершите перенос.')
    for pattern in ('happ@*.service', 'happ-awg@*.service', 'happ-web.service'):
        for unit in quiet(['systemctl', 'list-units', '--all', '--plain', '--no-legend', pattern]).stdout.split('\n'):
            name = unit.split(' ')[0] if unit.strip() else ''
            if name:
                quiet(['systemctl', 'disable', '--now', name])
    for unit in ('happ@.service', 'happ-awg@.service', 'happ-web.service'):
        (paths.UNIT_DIR/unit).unlink(missing_ok=True)
    run(['systemctl', 'daemon-reload'])
    target = backup_dir('legacy-files')
    for path in (paths.LEGACY_ETC, paths.LEGACY_VAR/'backups'):
        if path.exists():
            shutil.copytree(path, target/path.name, symlinks=True)
    for path in (paths.LEGACY_ETC, paths.LEGACY_VAR, paths.LEGACY_OPT):
        shutil.rmtree(path, ignore_errors=True)
    for network in ('happ-private',):
        if quiet(['docker', 'network', 'inspect', '-f', '{{len .Containers}}', network]).stdout.strip() == '0':
            quiet(['docker', 'network', 'rm', network])
    say(f'Старые файлы happ-service удалены (копия ключей и настроек: {target}).')


def uninstall():
    o = ops()
    for name in o.installed():
        quiet(['systemctl', 'disable', '--now', o.unit(name)])
    quiet(['systemctl', 'disable', '--now', 'exitpool-web.service'])
    say('Выходы и веб-интерфейс остановлены и отключены. Профили, ключ подписки и настройки сохранены.')
    say('Исходящие в 3x-ui не менялись: уберите их из балансеров, затем удалите в панели.')


def purge(everything=False):
    """Code, units, drop-ins, network objects and the guard table. Keys and settings stay unless everything."""
    import access_policy
    o = ops()
    try:
        cfg = o.installed()
    except o.OpsError:
        cfg = {}
    for name in cfg:
        quiet(['systemctl', 'disable', '--now', o.unit(name)])
    for name, values in cfg.items():
        if values.get('kind') == 'awg':
            quiet(['python3', str(paths.LIB/'awgnet.py'), 'purge', name])
            o.remove_awg_units(name)
    for unit in ('exitpool-web.service', 'exitpool-awg-guard.service'):
        quiet(['systemctl', 'disable', '--now', unit])
    access_policy.remove_table()
    for name in UNITS:
        (paths.UNIT_DIR/name).unlink(missing_ok=True)
    run(['systemctl', 'daemon-reload'])
    shutil.rmtree(paths.OPT, ignore_errors=True)
    wrapper_path().unlink(missing_ok=True)
    if everything:
        shutil.rmtree(paths.ETC, ignore_errors=True)
        shutil.rmtree(paths.VAR, ignore_errors=True)
        say('exitpool удалён полностью, включая ключи, профили и настройки.')
    else:
        say('exitpool удалён. Ключи, профили и настройки сохранены в /etc/exitpool и /var/lib/exitpool.')
    if any(values.get('kind', 'happ') == 'happ' for values in cfg.values()):
        say(f'Образ и сеть Docker для Happ оставлены: docker image rm exitpool-happ:4.3.0-318; '
            f'docker network rm {paths.HAPP_NETWORK}')
    say('Исходящие в 3x-ui не менялись: уберите их из балансеров и удалите в панели.')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=['base', 'happ', 'upgrade', 'reinstall', 'uninstall', 'purge', 'cleanup-legacy'])
    ap.add_argument('--all', action='store_true', help='purge: удалить и ключи, профили, настройки')
    ap.add_argument('--web-config')
    ap.add_argument('--binaries', choices=['skip', 'keep', 'download', 'dir', 'build'], default=None)
    ap.add_argument('--binaries-dir')
    ap.add_argument('--binaries-local-build', action='store_true', help='файлы из --binaries-dir — своя сборка')
    ap.add_argument('--proxy')
    ap.add_argument('--subscription')
    ap.add_argument('--instances')
    ap.add_argument('--seed-state')
    a = ap.parse_args()
    if os.geteuid() != 0:
        ap.exit(1, 'Запустите через sudo.\n')
    os.umask(0o077)
    import fetch
    try:
        proxy = fetch.parse_proxy(a.proxy) if a.proxy else fetch.load_saved_proxy() or fetch.env_proxy()
        mode = a.binaries or ('dir' if a.binaries_dir else None)
        if a.action == 'base':
            base(a.web_config, mode or 'skip', a.binaries_dir, proxy, a.binaries_local_build)
        elif a.action == 'happ':
            happ(a.subscription, a.instances, a.seed_state)
        elif a.action == 'upgrade':
            upgrade(a.web_config, mode or 'keep', a.binaries_dir, proxy, a.binaries_local_build)
        elif a.action == 'reinstall':
            reinstall(a.web_config, mode or 'keep', a.binaries_dir, proxy, a.binaries_local_build)
        elif a.action == 'cleanup-legacy':
            cleanup_legacy()
        elif a.action == 'purge':
            purge(a.all)
        else:
            uninstall()
    except (InstallError, fetch.FetchError, ValueError, OSError) as exc:
        ap.exit(1, f'\n{exc}\n')


if __name__ == '__main__':
    main()
