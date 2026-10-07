#!/usr/bin/env python3
"""exitpool host operations shared by the CLI, the web interface, the wizard and upgrades.

Every message raised as OpsError is safe to show to the user: it never contains the
subscription key, AWG keys, panel tokens or raw Happ logs.
"""
from collections import Counter
import contextlib
import copy
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402
from configuration import (validate_instances, validate_subscription, make_outbounds,  # noqa: E402
                           relay_address, kind as kind_of_cfg, AWG_SLOTS)
from panel_outbounds import PanelClient, PanelError  # noqa: E402
from memory_budget import (read_memory, parse_docker_mib, PER_INSTANCE_MIB, RESERVE_MIB,  # noqa: E402
                           AWG_RESERVE_MIB, awg_budget, awg_caps)
import awg_profile  # noqa: E402

STALE_SECONDS = 90
EDITABLE = ('profile_name', 'country', 'update_hours', 'health_interval', 'failure_threshold', 'memory_mib')
AWG_EDITABLE = ('label', 'country', 'mtu', 'health_interval', 'failure_threshold', 'memory_mib', 'outbound')
STATE_ITEMS = ('config', 'data', 'catalog.json', 'refresh.json')
KEEP_BACKUPS = 10
DAY = 24*3600
PORT_BASE, PORT_STEP = 11808, 10


class OpsError(RuntimeError):
    pass


def set_root(root='/'):
    """Tests run against a temporary tree; production uses the real filesystem."""
    global ROOT, ETC, VAR, OPT, RUNDIR, AWG_ETC, UNIT_DIR, BIN
    paths.set_root(root)
    ROOT, ETC, VAR, OPT, RUNDIR = paths.ROOT, paths.ETC, paths.VAR, paths.OPT, paths.RUNDIR
    AWG_ETC, UNIT_DIR, BIN = paths.AWG_ETC, paths.UNIT_DIR, paths.BIN


set_root(os.environ.get('EXITPOOL_ROOT', '/'))


def run(args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def instance_kind(name):
    return kind_of_cfg(read_json(ETC/'instances'/f'{name}.json', {}) or {})


def unit(name):
    return f'exitpool-awg@{name}.service' if instance_kind(name) == 'awg' else f'exitpool-happ@{name}.service'


def write_file(path, text, mode):
    path = Path(path)
    temp = path.with_name('.'+path.name+'.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as out:
        out.write(text)
    os.chmod(temp, mode)
    temp.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def locked():
    """One configuration-changing operation at a time (web threads, CLI, upgrade)."""
    handle = acquire_lock()
    try:
        yield
    finally:
        handle.close()


def acquire_lock():
    RUNDIR.mkdir(parents=True, exist_ok=True)
    handle = open(RUNDIR/'exitpool.lock', 'w')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise OpsError('Уже выполняется другая операция. Дождитесь её завершения.') from None
    return handle


# ---------------------------------------------------------------- configuration

def installed():
    manifest = OPT/'instances.json'
    try:
        if manifest.exists():
            return validate_instances(json.loads(manifest.read_text()))
        files = sorted((ETC/'instances').glob('*.json'))
        if not files:
            raise OpsError('exitpool не установлен.')
        return validate_instances({p.stem: json.loads(p.read_text()) for p in files})
    except ValueError as exc:
        raise OpsError('Конфигурация экземпляров повреждена: '+str(exc)) from None


def write_instances(cfg):
    cfg = validate_instances(cfg)
    (ETC/'instances').mkdir(mode=0o700, parents=True, exist_ok=True)
    changed_units = False
    for name, values in cfg.items():
        base = ETC/'instances'/name
        write_file(base.with_suffix('.json'), json.dumps(values, ensure_ascii=False, indent=2)+'\n', 0o600)
        if kind_of_cfg(values) == 'awg':
            changed_units |= write_awg_units(name, values)
        else:
            write_file(base.with_suffix('.env'), f"PORT={values['port']}\nMEMORY_LIMIT={values['memory_mib']}m\n"
                                                 f"MEMORY_SWAP={values['memory_mib']+128}m\n", 0o600)
    OPT.mkdir(parents=True, exist_ok=True)
    write_file(OPT/'instances.json', json.dumps(cfg, ensure_ascii=False, indent=2)+'\n', 0o644)
    write_file(OPT/'3xui-outbounds.json', json.dumps(make_outbounds(cfg), indent=2)+'\n', 0o644)
    if changed_units:
        run(['systemctl', 'daemon-reload'], timeout=60)
    return cfg


def awg_dropins(name):
    return {UNIT_DIR/f'exitpool-awg@{name}.service.d'/'exitpool.conf',
            UNIT_DIR/f'exitpool-awg-relay@{name}.service.d'/'exitpool.conf',
            UNIT_DIR/f'exitpool-awg-loopback@{name}.socket.d'/'exitpool.conf'}


def write_awg_units(name, values):
    """Per-exit systemd drop-ins: hard memory limits of the mode and the optional loopback entry."""
    manager_cap, relay_cap = awg_caps(values['memory_mib'])
    files = {
        UNIT_DIR/f'exitpool-awg@{name}.service.d'/'exitpool.conf':
            f'[Service]\nMemoryMax={manager_cap}M\n',
        UNIT_DIR/f'exitpool-awg-relay@{name}.service.d'/'exitpool.conf':
            f'[Service]\nMemoryMax={relay_cap}M\n',
        UNIT_DIR/f'exitpool-awg-loopback@{name}.socket.d'/'exitpool.conf':
            f"[Socket]\nListenStream=\nListenStream=127.0.0.1:{values['port']}\n",
    }
    changed = False
    for path, text in files.items():
        old = path.read_text() if path.exists() else None
        if old != text:
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            path.parent.chmod(0o755)
            write_file(path, text, 0o644)
            changed = True
    write_file(ETC/'instances'/f'{name}.loopback.env', f"TARGET=10.250.{values['slot']}.2:{values['port']}\n", 0o644)
    return changed


def remove_awg_units(name):
    for path in awg_dropins(name):
        path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            path.parent.rmdir()
    (ETC/'instances'/f'{name}.loopback.env').unlink(missing_ok=True)


def catalog(name):
    names = read_json(VAR/name/'catalog.json', [])
    if not isinstance(names, list):
        return []
    return [n for n in names if isinstance(n, str)][:200]


def save_instance(name, changes):
    with locked():
        cfg = installed()
        if name not in cfg:
            raise OpsError('Такого выхода нет.')
        awg = kind_of_cfg(cfg[name]) == 'awg'
        if not isinstance(changes, dict) or set(changes)-set(AWG_EDITABLE if awg else EDITABLE):
            raise OpsError('Тег и порт менять нельзя: на них ссылаются балансеры панели.')
        new = dict(cfg[name])
        for key, value in changes.items():
            if key == 'country':
                value = (value or '').strip().upper()
            if key == 'mtu' and value in (None, '', 0):
                new.pop('mtu', None)   # back to the profile's MTU or 1280
                continue
            if key == 'label' and isinstance(value, str):
                value = value.strip()
            if key == 'profile_name':
                if not isinstance(value, str):
                    raise OpsError('Неверное название профиля.')
                if value == new.get('profile_query') and 'profile_name' not in new:
                    continue   # legacy search query left unchanged
                known = catalog(name)
                if known and Counter(known).get(value) != 1:
                    raise OpsError('Такого однозначного профиля нет в последнем списке подписки.')
                new.pop('profile_query', None)
            new[key] = value
        cfg[name] = new
        try:
            cfg = validate_instances(cfg)
        except ValueError as exc:
            raise OpsError(str(exc)) from None
        write_instances(cfg)
        if units([name])[name].get('ActiveState') in ('active', 'activating'):
            service(name, 'restart')
        return cfg[name]


# ---------------------------------------------------------------- services

def units(names):
    if not names:
        return {}
    r = run(['systemctl', 'show', '--no-pager', '-p', 'Id,ActiveState,SubState,NRestarts,UnitFileState',
             *map(unit, names)], timeout=15)
    found = {}
    for block in r.stdout.strip().split('\n\n'):
        values = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if values.get('Id'):
            found[values['Id']] = values
    return {name: found.get(unit(name), {}) for name in names}


def service(name, action):
    if action not in ('start', 'stop', 'restart'):
        raise OpsError('Неизвестное действие.')
    if name not in installed():
        raise OpsError('Такого выхода нет.')
    if action in ('stop', 'restart') and units([name])[name].get('ActiveState') == 'active':
        # The container is torn down by its init before the manager can log this itself.
        host_event(name, 'stop', by=action)
    r = run(['systemctl', action, unit(name)], timeout=90)
    if r.returncode:
        raise OpsError(f'systemctl {action} {unit(name)} завершился с ошибкой.')


def host_event(name, kind, **values):
    path = VAR/name/'events.jsonl'
    try:
        with path.open('a') as out:   # O_APPEND: safe next to the manager's own appends
            out.write(json.dumps({'at': round(time.time(), 1), 'event': kind, **values})+'\n')
        os.chmod(path, 0o600)
    except OSError:
        pass


def command(name, action):
    """Queue refresh/reconnect/probe for the running manager through its state directory."""
    if action not in ('refresh', 'reconnect', 'probe'):
        raise OpsError('Неизвестная команда.')
    cfg = installed()
    if name not in cfg:
        raise OpsError('Такого выхода нет.')
    if action == 'refresh' and kind_of_cfg(cfg[name]) == 'awg':
        raise OpsError('У AWG-выхода нет подписки: профиль меняется кнопкой «Заменить профиль».')
    state = units([name])[name]
    if state.get('ActiveState') != 'active':
        raise OpsError('Выход остановлен: сначала запустите его.')
    write_file(VAR/name/'command.json', json.dumps({'action': action, 'at': time.time()}), 0o600)


def journal(name, lines=80):
    if name not in installed():
        raise OpsError('Такого выхода нет.')
    r = run(['journalctl', '-u', unit(name), '-n', str(lines), '-o', 'short-iso', '--no-pager'], timeout=20)
    # Only manager messages reach the journal; Happ's own logs stay in private files.
    return [line[:400] for line in r.stdout.splitlines()]


def wait_running(name, since, timeout=180):
    deadline = time.monotonic()+timeout
    first_restarts = None
    while time.monotonic() < deadline:
        state = read_json(VAR/name/'status.json', {}) or {}
        fresh = state.get('started_at', 0) >= since-1
        if fresh:
            if state.get('phase') == 'running' and state.get('healthy'):
                return 'running'
            if state.get('phase') == 'degraded' and state.get('degraded_reason') == 'profile_missing':
                return 'profile_missing'
            # An AWG exit reports degraded/failed only after its own startup retries.
            if state.get('kind') == 'awg' and state.get('phase') in ('degraded', 'failed'):
                return state['phase']
        # A unit that fails before writing any status (broken ExecStartPre, missing binary) is reported quickly
        # instead of after the whole timeout.
        info = units([name]).get(name, {})
        if info.get('ActiveState') == 'failed':
            return 'failed'
        try:
            restarts = int(info.get('NRestarts') or 0)
        except ValueError:
            restarts = 0
        if first_restarts is None:
            first_restarts = restarts
        elif restarts-first_restarts >= 3 and not fresh:
            return 'failed'
        time.sleep(2)
    return 'timeout'


def rolling_restart(names, progress=print, timeout=180):
    """Restart one exit at a time so balancers always keep the others."""
    for name in names:
        started = time.time()
        progress(f'{name}: перезапуск…')
        service(name, 'restart')
        result = wait_running(name, started, timeout)
        if result != 'running':
            progress(f'{name}: не перешёл в рабочее состояние ({result}); остальные не перезапускались.')
            return name
        progress(f'{name}: работает.')
    return None


# ---------------------------------------------------------------- status

def read_events(name):
    events = []
    try:
        lines = (VAR/name/'events.jsonl').read_text().splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get('at'), (int, float)) and isinstance(item.get('event'), str):
            events.append(item)
    return events


def down_intervals(events, now, since, up_now):
    intervals, start = [], None
    for index, item in enumerate(events):
        kind = item['event']
        if kind in ('down', 'failed', 'stop', 'start'):
            # The very first start is installation, not an outage; a failed first start still logs 'down'.
            if start is None and not (index == 0 and kind == 'start'):
                start = item['at']
        elif kind == 'up' and start is not None:
            intervals.append([start, item['at']])
            start = None
    if start is not None and not up_now:
        intervals.append([start, now])
    return [[max(a, since), min(b, now)] for a, b in intervals if b > since and b > a]


def latency_view(state, now, window=1800):
    samples = [s for s in state.get('latency_history') or []
               if isinstance(s, list) and len(s) == 2 and isinstance(s[0], (int, float)) and now-s[0] <= window]
    values = [ms for _, ms in samples if isinstance(ms, (int, float))]
    return {'last': state.get('latency_ms'), 'at': state.get('latency_at'),
            'median': round(statistics.median(values)) if values else None,
            'min': min(values) if values else None, 'max': max(values) if values else None,
            'samples': len(samples), 'failed': len(samples)-len(values)}


def instance_view(name, cfg, unit_info, now, memory_mib=None):
    state = read_json(VAR/name/'status.json', {}) or {}
    active = unit_info.get('ActiveState', 'unknown')
    stale = now-state.get('updated_at', 0) > STALE_SECONDS
    phase = state.get('phase', 'unknown')
    if active in ('inactive', 'failed', 'deactivating'):
        phase = 'stopped'
    elif active == 'activating':
        phase = 'starting'
    elif stale:
        phase = 'stale'
    healthy = active == 'active' and not stale and bool(state.get('healthy'))
    events = read_events(name)
    intervals = down_intervals(events, now, now-DAY, healthy)
    covered = min(DAY, now-events[0]['at']) if events else 0
    downtime = sum(b-a for a, b in intervals)
    checks = state.get('checks') or {}
    awg = kind_of_cfg(cfg) == 'awg'
    tunnel = state.get('tunnel') if awg and isinstance(state.get('tunnel'), dict) else {}
    return {
        'name': name, 'kind': 'awg' if awg else 'happ', 'tag': cfg['tag'], 'port': cfg['port'],
        'profile': (cfg.get('label') or 'AmneziaWG') if awg else cfg.get('profile_name', cfg.get('profile_query', '')),
        'exact': awg or 'profile_name' in cfg, 'country': cfg.get('country', ''),
        'settings': {k: cfg[k] for k in (AWG_EDITABLE if awg else EDITABLE) if k in cfg},
        'tunnel': {k: tunnel.get(k) for k in ('endpoint', 'mtu', 'handshake_age', 'rx', 'tx', 'relay') if k in tunnel},
        'address': f"{relay_address(cfg)}:{cfg['port']}",
        'memory_mode': cfg['memory_mib'] if awg else None,
        'budget_mib': awg_budget(cfg['memory_mib']) if awg else PER_INSTANCE_MIB,
        'phase': phase, 'healthy': healthy, 'stale': stale,
        'error': state.get('error'), 'degraded_reason': state.get('degraded_reason'),
        'action': state.get('action'), 'down_since': state.get('down_since'),
        'failures': state.get('failures'), 'selected_profile': state.get('selected_profile'),
        'exit_country': state.get('exit_country'), 'exit_ip': state.get('exit_ip'),
        'started_at': state.get('started_at'), 'last_success': state.get('last_success'),
        'updated_at': state.get('updated_at'), 'subscription_updated': state.get('subscription_updated'),
        'refresh_ok': state.get('refresh_ok'),
        'checks': {k: {'ok': bool(v.get('ok')), 'http_code': v.get('http_code'), 'ms': v.get('ms')}
                   for k, v in checks.items() if isinstance(v, dict)},
        'latency': latency_view(state, now),
        'unit': {'active': active, 'sub': unit_info.get('SubState'), 'enabled': unit_info.get('UnitFileState'),
                 'restarts': int(unit_info.get('NRestarts') or 0)},
        'memory_mib': memory_mib,
        'catalog': catalog(name),
        'day': {'intervals': intervals, 'downtime': round(downtime), 'outages': len(intervals),
                'covered': round(covered),
                'availability': round(100*(1-downtime/covered), 2) if covered >= 600 else None},
        'events': events[-12:],
    }


def subscription_info():
    try:
        key = (ETC/'subscription.secret').read_text().strip()
    except OSError:
        return {'present': False}
    kind = 'happ://crypt5/…' if key.startswith('happ://crypt5/') else 'другой формат'
    return {'present': True, 'kind': kind, 'fingerprint': hashlib.sha256(key.encode()).hexdigest()[:12]}


def host_view():
    info = {}
    try:
        values = {line.split(':', 1)[0]: int(line.split()[1])//1024
                  for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line}
        info.update(mem_total=values['MemTotal'], mem_available=values['MemAvailable'],
                    swap_used=values.get('SwapTotal', 0)-values.get('SwapFree', 0))
    except (OSError, ValueError, KeyError):
        pass
    try:
        usage = shutil.disk_usage(ROOT)
        info.update(disk_total=usage.total//2**20, disk_free=usage.free//2**20)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        info['load'] = [round(x, 2) for x in os.getloadavg()]
    return info


class MemoryStats:
    """docker stats (Happ) and systemd MemoryCurrent (AWG units) take time; refreshed in the background."""
    def __init__(self):
        self.values, self.lock = {}, threading.Lock()

    def refresh(self):
        values = {}
        try:
            cfg = installed()
        except OpsError:
            cfg = {}
        if any(kind_of_cfg(c) == 'happ' for c in cfg.values()) and shutil.which('docker'):
            try:
                r = run(['docker', 'stats', '--no-stream', '--format', '{{.Name}}\t{{.MemUsage}}'], timeout=30)
                for line in r.stdout.splitlines():
                    container, _, usage = line.partition('\t')
                    if container.startswith('exitpool-') and not container.startswith('exitpool-discovery-'):
                        values[container[len('exitpool-'):]] = parse_docker_mib(usage)
            except (OSError, subprocess.TimeoutExpired):
                pass
        awg = [n for n, c in cfg.items() if kind_of_cfg(c) == 'awg']
        if awg:
            names = [f'exitpool-awg{part}@{n}.service' for n in awg for part in ('', '-relay')]
            try:
                r = run(['systemctl', 'show', '-p', 'Id,MemoryCurrent', *names], timeout=15)
                for block in r.stdout.strip().split('\n\n'):
                    item = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
                    unit_id, current = item.get('Id', ''), item.get('MemoryCurrent', '')
                    if '@' in unit_id and current.isdigit():
                        name = unit_id.split('@', 1)[1].rsplit('.', 1)[0]
                        values[name] = values.get(name, 0)+int(current)//2**20
            except (OSError, subprocess.TimeoutExpired):
                pass
        with self.lock:
            self.values = values

    def get(self):
        with self.lock:
            return dict(self.values)


def status(memory=None, panel=None):
    now = time.time()
    cfg = installed()
    info = units(list(cfg))
    stats = memory.get() if memory else {}
    instances = [instance_view(n, c, info.get(n, {}), now, stats.get(n)) for n, c in cfg.items()]
    summary = panel.get() if panel else {'enabled': False}
    for item in instances:
        item['panel'] = (summary.get('instances') or {}).get(item['name'])
    summary.pop('instances', None)
    return {'now': now, 'instances': instances, 'host': host_view(),
            'subscription': subscription_info(), 'panel': summary,
            'features': {'awg': tun_available() and awg_binaries_ready(),
                         'awg_missing': awg_missing_reason(),
                         'happ': any(kind_of_cfg(c) == 'happ' for c in cfg.values())}}


# ---------------------------------------------------------------- 3x-ui panel (optional)

PANEL_HOST = re.compile(r'[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\]|[0-9A-Fa-f:.]{2,45}')
PANEL_PATH = re.compile(r'(/[A-Za-z0-9._~-]{1,64}){0,8}')


def panel_path():
    return ETC/'panel.json'


def validate_panel(data, previous=None):
    if not isinstance(data, dict):
        raise OpsError('Неверные настройки панели.')
    host = str(data.get('host', '')).strip()
    if not PANEL_HOST.fullmatch(host):
        raise OpsError('Адрес панели: IP или имя хоста.')
    port = data.get('port')
    if isinstance(port, str) and port.isdigit():
        port = int(port)
    if type(port) is not int or not 1 <= port <= 65535:
        raise OpsError('Порт панели: число от 1 до 65535.')
    base = '/'+str(data.get('base_path', '')).strip().strip('/') if str(data.get('base_path', '')).strip('/ ') else ''
    if not PANEL_PATH.fullmatch(base):
        raise OpsError('Базовый путь: латиница, цифры, / . _ ~ -')
    scheme = 'https' if data.get('https') else 'http'
    token = data.get('token') or (previous or {}).get('token', '')
    if not isinstance(token, str):
        raise OpsError('Неверный API-токен.')
    token = token.strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()
    if not token or len(token) > 512 or any(c.isspace() for c in token):
        raise OpsError('Нужен API-токен панели без пробелов.')
    return {'enabled': bool(data.get('enabled', True)), 'scheme': scheme, 'host': host, 'port': port,
            'base_path': base, 'token': token}


def load_panel():
    data = read_json(panel_path())
    if not isinstance(data, dict):
        return None
    try:
        return validate_panel({**data, 'https': data.get('scheme') == 'https'})
    except OpsError:
        return None


def panel_url(cfg):
    return f"{cfg['scheme']}://{cfg['host']}:{cfg['port']}{cfg['base_path']}"


def panel_public(cfg):
    """Settings for the browser: never the token itself."""
    if not cfg:
        return {'configured': False}
    return {'configured': True, 'enabled': cfg['enabled'], 'https': cfg['scheme'] == 'https', 'host': cfg['host'],
            'port': cfg['port'], 'base_path': cfg['base_path'],
            'token_fingerprint': hashlib.sha256(cfg['token'].encode()).hexdigest()[:8]}


def save_panel(data):
    cfg = validate_panel(data, load_panel())
    if cfg['enabled']:
        try:
            PanelClient(panel_url(cfg), cfg['token']).balancers()   # prove access before saving
        except (PanelError, ValueError) as exc:
            raise OpsError('Панель не ответила: '+str(exc)) from None
    write_file(panel_path(), json.dumps(cfg, indent=2)+'\n', 0o600)
    PanelMonitor.generation += 1   # the web page re-reads the balancer list right away
    return panel_public(cfg)


def clear_panel():
    panel_path().unlink(missing_ok=True)


def selected_by(tag, selectors):
    # Xray balancer selectors are tag prefixes.
    return any(tag.startswith(s) for s in selectors if s)


class PanelMonitor:
    """Which balancer uses each exit, and the observatory delay, read from the panel API."""
    TEMPLATE_SECONDS = 120

    generation = 0   # bumped when the panel settings are saved in this process

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {'enabled': False}
        self.balancers, self.template_at, self.template_key = {}, 0, None

    def get(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def refresh(self):
        cfg, now = load_panel(), time.time()
        if not cfg or not cfg['enabled']:
            with self.lock:
                self.data = {'enabled': False, 'configured': bool(cfg)}
            return
        try:
            client = PanelClient(panel_url(cfg), cfg['token'])
            key = (panel_url(cfg), cfg['token'], PanelMonitor.generation)
            if key != self.template_key or now-self.template_at > self.TEMPLATE_SECONDS:
                self.balancers, self.template_at, self.template_key = client.balancers(), now, key
            exits = installed()
            relevant = {b: sel for b, sel in self.balancers.items()
                        if any(selected_by(c['tag'], sel) for c in exits.values())}
            states = client.balancer_status(sorted(relevant)) if relevant else {}
            observed = client.observatory()
            result = {}
            for name, values in exits.items():
                tag = values['tag']
                balancers = []
                for balancer, selectors in relevant.items():
                    if selected_by(tag, selectors):
                        state = states.get(balancer) if isinstance(states.get(balancer), dict) else {}
                        balancers.append({'tag': balancer, 'selected': tag in (state.get('selected') or []),
                                          'override': state.get('override') or '', 'running': state.get('running')})
                seen = observed.get(tag)
                result[name] = {'balancers': balancers,
                                'observatory': {k: seen.get(k) for k in ('alive', 'delay', 'updatedAt')} if seen else None}
            data = {'enabled': True, 'configured': True, 'ok': True, 'checked_at': now, 'instances': result}
        except (PanelError, OpsError, ValueError, OSError, TypeError, KeyError) as exc:
            message = str(exc) if isinstance(exc, (PanelError, OpsError, ValueError)) else 'Панель не ответила.'
            data = {'enabled': True, 'configured': True, 'ok': False, 'error': message, 'checked_at': now}
        with self.lock:
            self.data = data


# ---------------------------------------------------------------- backups

def backup(label, names):
    root = VAR/'backups'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    target = root/f'{stamp}-{label}'
    suffix = 1
    while target.exists():
        suffix += 1
        target = root/f'{stamp}-{label}-{suffix}'
    target.mkdir(mode=0o700)
    if (ETC/'subscription.secret').exists():
        shutil.copy2(ETC/'subscription.secret', target/'subscription.secret')
    shutil.copytree(ETC/'instances', target/'instances')
    if AWG_ETC.is_dir():
        shutil.copytree(AWG_ETC, target/'awg')   # AWG profiles: private like the subscription key
    cfg = installed()
    for name in names:
        if kind_of_cfg(cfg.get(name)) == 'awg':
            continue   # an AWG exit's whole state is its profile and settings
        for item in STATE_ITEMS:
            source = VAR/name/item
            destination = target/'state'/name/item
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            elif source.exists():
                shutil.copy2(source, destination)
    meta = {'id': target.name, 'created': time.time(), 'label': label,
            'fingerprint': subscription_info().get('fingerprint'),
            'instances': {n: ('AWG '+(cfg[n].get('label') or n)) if kind_of_cfg(cfg[n]) == 'awg'
                          else cfg[n].get('profile_name', cfg[n].get('profile_query')) for n in names if n in cfg}}
    write_file(target/'meta.json', json.dumps(meta, ensure_ascii=False, indent=2)+'\n', 0o600)
    for old in sorted(p for p in root.iterdir() if (p/'meta.json').exists())[:-KEEP_BACKUPS]:
        shutil.rmtree(old)
    return target.name


def list_backups():
    root = VAR/'backups'
    result = []
    if root.exists():
        for path in sorted(root.iterdir(), reverse=True):
            meta = read_json(path/'meta.json')
            if isinstance(meta, dict):
                result.append({k: meta.get(k) for k in ('id', 'created', 'label', 'fingerprint', 'instances')})
    return result


def backup_path(backup_id):
    if not isinstance(backup_id, str) or not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-z-]+(-[0-9]+)?', backup_id):
        raise OpsError('Неверный идентификатор резервной копии.')
    path = VAR/'backups'/backup_id
    if not (path/'meta.json').exists():
        raise OpsError('Резервная копия не найдена.')
    return path


def replace_state(name, source):
    """Swap Happ's imported subscription state; keep logs and event history."""
    base = VAR/name
    for item in ('config', 'data', 'cache', 'catalog.json', 'refresh.json', 'status.json', 'command.json'):
        path = base/item
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    for item in STATE_ITEMS:
        src = Path(source)/item
        if src.is_dir():
            shutil.copytree(src, base/item, symlinks=True)
        elif src.is_file():
            shutil.copy2(src, base/item)
    for path in [base, *base.rglob('*')]:
        if not path.is_symlink():
            os.chmod(path, path.stat().st_mode & 0o700)


def restore_instance(backup_dir, name):
    """Put one exit back to the backed-up key, settings and imported state."""
    service(name, 'stop')
    saved = json.loads((backup_dir/'instances'/f'{name}.json').read_text())
    if kind_of_cfg(saved) == 'awg':
        profile = backup_dir/'awg'/f'{name}.conf'
        if profile.exists():
            AWG_ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_file(AWG_ETC/f'{name}.conf', profile.read_text(), 0o600)
    elif (backup_dir/'subscription.secret').exists():
        write_file(ETC/'subscription.secret', (backup_dir/'subscription.secret').read_text(), 0o600)
    cfg = installed()
    cfg[name] = saved
    write_instances(cfg)
    if kind_of_cfg(saved) != 'awg' and (backup_dir/'state'/name).is_dir():   # upgrade backups hold settings only
        replace_state(name, backup_dir/'state'/name)
    started = time.time()
    service(name, 'start')
    return wait_running(name, started)


# ---------------------------------------------------------------- subscription replacement

def profile_words(value):
    return [w.casefold() for w in re.findall(r'[^\W\d_]{3,}', value or '')]


def propose(cfg, names):
    """Preselect profiles by exact name, then by the same first word (e.g. country)."""
    counts = Counter(names)
    unique = [n for n in names if counts[n] == 1]
    result = {}
    for name, values in happ_only(cfg).items():
        current = values.get('profile_name', values.get('profile_query', ''))
        choice = current if current in unique else ''
        words = profile_words(current)
        if not choice and words:
            same = [n for n in unique if profile_words(n)[:1] == words[:1]]
            if len(same) == 1:
                choice = same[0]
        result[name] = {'profile_name': choice, 'country': values.get('country', '')}
    return result


def happ_only(cfg):
    return {n: c for n, c in cfg.items() if kind_of_cfg(c) == 'happ'}


# ---------------------------------------------------------------- AmneziaWG exits (no Docker)

def tun_available():
    return os.path.exists('/dev/net/tun')


def awg_binaries_ready():
    return all((BIN/name).is_file() and os.access(BIN/name, os.X_OK) for name in paths.AWG_BINARIES)


def awg_missing_reason():
    if not tun_available():
        return 'На сервере нет /dev/net/tun: AWG-выход здесь невозможен (так бывает на OpenVZ/LXC VPS).'
    if not awg_binaries_ready():
        return 'Бинарники AWG не установлены: sudo exitpool binaries install'
    return None


def check_awg_profile(text, exclude=None):
    """Validate a profile; refuse a key that another exit already uses (one key, one tunnel)."""
    try:
        facts = awg_profile.parse(text)
    except awg_profile.ProfileError as exc:
        raise OpsError(str(exc)) from None
    for name, values in installed().items():
        if name == exclude or kind_of_cfg(values) != 'awg':
            continue
        try:
            other = awg_profile.parse((AWG_ETC/f'{name}.conf').read_text())
        except (OSError, awg_profile.ProfileError):
            continue
        if other['fingerprint'] == facts['fingerprint']:
            raise OpsError(f'Этот профиль (тот же ключ) уже у выхода {name}: один ключ — один туннель.')
    return facts


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Like Docker's and systemd's listeners: TIME_WAIT leftovers do not count, only a live listener does.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def next_port(cfg):
    used = {c['port'] for c in cfg.values()}
    for port in range(PORT_BASE, 65000, PORT_STEP):
        if port not in used and port_free(port):
            return port
    raise OpsError('Не нашёл свободный локальный порт.')


def routes_10_250():
    """Slots whose /30 is already routed by someone else (an exitpool blackhole does not count)."""
    taken = set()
    try:
        own_slots = {c['slot'] for c in installed().values() if kind_of_cfg(c) == 'awg'}
    except OpsError:
        own_slots = set()   # also used before the first installation during migration
    r = run(['ip', '-j', 'route', 'show', 'table', 'all'], timeout=10)
    try:
        routes = json.loads(r.stdout or '[]')
    except ValueError:
        routes = []
    candidates = {slot: ipaddress.IPv4Network(f'10.250.{slot}.0/30') for slot in AWG_SLOTS}
    for route in routes:
        try:
            network = ipaddress.IPv4Network(route.get('dst', ''), strict=False)
        except ValueError:
            continue
        if network.prefixlen < 16:
            continue   # default or a broad aggregate (10.0.0.0/8 of a VPN): our /30 is more specific and wins
        if route.get('type') == 'blackhole' and route.get('metric') == 32760 and any(
                network == candidates[slot] for slot in own_slots):
            continue
        taken.update(slot for slot, candidate in candidates.items() if candidate.overlaps(network))
    return taken


def next_slot(cfg):
    used = {c['slot'] for c in cfg.values() if kind_of_cfg(c) == 'awg'} | routes_10_250()
    for slot in AWG_SLOTS:
        if slot not in used:
            return slot
    raise OpsError('Нет свободной подсети 10.250.N.0/30 для нового выхода.')


def ensure_happ_network():
    r = run(['docker', 'network', 'inspect', paths.HAPP_NETWORK, '--format', '{{index .Labels "app"}}'], timeout=20)
    if r.returncode == 0:
        if r.stdout.strip() != 'exitpool':
            raise OpsError(f'Сеть Docker {paths.HAPP_NETWORK} создана не exitpool; выход не запускался.')
        return
    if run(['docker', 'network', 'create', '--label', 'app=exitpool', paths.HAPP_NETWORK], timeout=30).returncode:
        raise OpsError(f'Не удалось создать сеть Docker {paths.HAPP_NETWORK}.')


def awg_ready_check(progress=None):
    reason = awg_missing_reason()
    if reason:
        raise OpsError(reason)


def awg_memory_check(modes, progress=None):
    """Budget of the exits being added (MemAvailable already counts running ones)."""
    from memory_budget import plan
    budget = read_memory()
    if not budget:
        return None
    result = plan(budget.available_mib, awg_modes=modes)
    if result['status'] == 'FAIL':
        raise OpsError(f"Мало памяти: выходу нужно около {result['expected']} МиБ, доступно {budget.available_mib} МиБ.")
    if result['status'] == 'WARN' and progress:
        progress(f"Памяти впритык: выходу ~{result['expected']} МиБ, доступно {budget.available_mib} МиБ "
                 f"(запас системе {result['reserve']} МиБ не остаётся).")
    return result


def remove_instance(name):
    """AWG exits only: stop, back up, remove network objects and files. The panel outbound stays."""
    with locked():
        cfg = installed()
        if name not in cfg:
            raise OpsError('Такого выхода нет.')
        if kind_of_cfg(cfg[name]) != 'awg':
            raise OpsError('Удаляются только AWG-выходы.')
        backup_id = backup('awg-remove', [name])
        service_unit = unit(name)
        if units([name])[name].get('ActiveState') in ('active', 'activating'):
            host_event(name, 'stop', by='remove')
        if run(['systemctl', 'disable', '--now', service_unit], timeout=90).returncode:
            raise OpsError(f'systemctl disable {service_unit} завершился с ошибкой; файлы не удалялись.')
        # Network objects first (needs the instance file), blackhole last.
        run(['python3', str(paths.LIB/'awgnet.py'), 'purge', name], timeout=60)
        removed = cfg.pop(name)
        remove_awg_units(name)
        for path in (ETC/'instances'/f'{name}.json', ETC/'instances'/f'{name}.env', AWG_ETC/f'{name}.conf'):
            path.unlink(missing_ok=True)
        write_instances(cfg)
        run(['systemctl', 'daemon-reload'], timeout=60)
        shutil.rmtree(VAR/name, ignore_errors=True)
        return {'tag': removed['tag'], 'backup_id': backup_id}


def default_discovery(key):
    from discovery import Discovery
    workdir = VAR/'tmp'
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Discovery(OPT/'happ', key, workdir=workdir)


class Jobs:
    """At most one long operation: subscription replacement or backup restore."""
    EXPIRE_SECONDS = 900

    def __init__(self, discovery_factory=default_discovery, awg_check=awg_ready_check):
        self.factory = discovery_factory
        self.awg_check = awg_check
        self.lock = threading.Lock()
        self.state = {'phase': 'idle'}
        self.discovery = None
        self.handle = None
        self.timer = None

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def _update(self, message=None, **values):
        with self.lock:
            self.state.update(values)
            if message:
                self.state.setdefault('log', []).append([round(time.time()), message])

    def _begin(self, kind, phase):
        with self.lock:
            if self.state['phase'] in ('discovering', 'awaiting_selection', 'applying'):
                raise OpsError('Уже выполняется другая операция с подпиской.')
            self.handle = acquire_lock()
            self.state = {'phase': phase, 'kind': kind, 'started_at': time.time(), 'log': []}

    def _finish(self, phase, message=None, **values):
        self._update(message, phase=phase, finished_at=time.time(), **values)
        if self.discovery:
            with contextlib.suppress(Exception):
                self.discovery.__exit__(None, None, None)
            self.discovery = None
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if self.handle:
            self.handle.close()
            self.handle = None

    def _fail(self, exc):
        if isinstance(exc, (OpsError, ValueError)):
            text = str(exc)
        elif isinstance(exc, subprocess.CalledProcessError):
            text = 'Команда Docker/systemd завершилась с ошибкой; подробности в journalctl -u exitpool-web.'
        else:
            text = 'Внутренняя ошибка ('+type(exc).__name__+'); подробности в journalctl -u exitpool-web.'
        self._finish('failed', 'Ошибка: '+text, error=text)

    # -- replacement
    def discover(self, key):
        try:
            key = validate_subscription(key if isinstance(key, str) else '')
        except ValueError as exc:
            raise OpsError(str(exc)) from None
        if not happ_only(installed()):
            raise OpsError('Happ-выходов нет: подписка используется только ими. Добавьте Happ: sudo exitpool setup.')
        budget = read_memory()
        if budget and budget.available_mib < PER_INSTANCE_MIB+RESERVE_MIB:
            raise OpsError(f'Мало свободной RAM ({budget.available_mib} МиБ) для временного Happ.')
        self._begin('replace', 'discovering')
        self._update('Запускаю временный Happ и читаю профили новой подписки (1–3 минуты)…')
        threading.Thread(target=self._discover, args=(key,), daemon=True).start()

    def _discover(self, key):
        try:
            job = self.factory(key)
            job.__enter__()
            self.discovery = job
            names = list(job.catalog)
            self.timer = threading.Timer(self.EXPIRE_SECONDS, self._expire)
            self.timer.daemon = True
            self.timer.start()
            self._update(f'Профилей в подписке: {len(names)}. Выберите профиль для каждого выхода.',
                         phase='awaiting_selection', catalog=names, proposal=propose(installed(), names),
                         expires_at=time.time()+self.EXPIRE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - reported to the user without details
            self._fail(exc)

    def _expire(self):
        with self.lock:
            waiting = self.state['phase'] == 'awaiting_selection'
        if waiting:
            self._finish('cancelled', 'Выбор не сделан за 15 минут; временный Happ остановлен.')

    def cancel(self):
        with self.lock:
            phase = self.state['phase']
        if phase == 'discovering':
            raise OpsError('Дождитесь окончания чтения подписки, затем отмените.')
        if phase != 'awaiting_selection':
            raise OpsError('Отменять нечего.')
        self._finish('cancelled', 'Замена отменена; работающие выходы не менялись.')

    def apply(self, mapping):
        with self.lock:
            if self.state['phase'] != 'awaiting_selection':
                raise OpsError('Сначала прочитайте новую подписку.')
            names = self.state['catalog']
        cfg = installed()
        if not isinstance(mapping, dict) or set(mapping) != set(happ_only(cfg)):
            raise OpsError('Нужно выбрать профиль для каждого выхода.')
        counts = Counter(names)
        new = copy.deepcopy(cfg)
        for name, choice in mapping.items():
            if not isinstance(choice, dict):
                raise OpsError('Неверный выбор профиля.')
            profile = choice.get('profile_name')
            if not isinstance(profile, str) or counts.get(profile) != 1:
                raise OpsError(f'{name}: выберите однозначный профиль из новой подписки.')
            country = choice.get('country') or ''
            new[name].pop('profile_query', None)
            new[name].update(profile_name=profile, country=country.strip().upper() if isinstance(country, str) else country)
        try:
            new = validate_instances(new)
        except ValueError as exc:
            raise OpsError(str(exc)) from None
        with self.lock:
            if self.state['phase'] != 'awaiting_selection':
                raise OpsError('Сначала прочитайте новую подписку.')
            self.state['phase'] = 'applying'
        if self.timer:
            self.timer.cancel()
        threading.Thread(target=self._apply, args=(new,), daemon=True).start()

    def _apply(self, new):
        try:
            job = self.discovery
            job.stop()
            backup_id = backup('subscription', list(happ_only(new)))
            backup_dir = VAR/'backups'/backup_id
            self._update('Резервная копия прежней подписки: '+backup_id, backup_id=backup_id)
            write_file(ETC/'subscription.secret', job.subscription+'\n', 0o600)
            warnings = []
            for index, (name, values) in enumerate(happ_only(new).items()):
                self._update(f'{name}: переключаю на «{values["profile_name"]}»…')
                service(name, 'stop')
                replace_state(name, job.state)
                cfg = installed()
                cfg[name] = values
                write_instances(cfg)
                started = time.time()
                service(name, 'start')
                result = wait_running(name, started)
                if result == 'running':
                    exit_country = (read_json(VAR/name/'status.json', {}) or {}).get('exit_country') or '?'
                    self._update(f'{name}: работает, выход {exit_country}.')
                elif index == 0:
                    # The first exit proves the new key; keep the others untouched on failure.
                    self._update(f'{name}: не заработал ({result}); возвращаю прежнюю подписку…')
                    back = restore_instance(backup_dir, name)
                    self._update(f'{name}: прежнее состояние восстановлено ({back}).')
                    raise OpsError('Новая подписка или выбранный профиль не прошли проверку на первом выходе. '
                                   'Прежняя подписка возвращена, остальные выходы не трогались.')
                else:
                    warnings.append(name)
                    self._update(f'{name}: пока не прошёл проверку ({result}); служба продолжит попытки. '
                                 'Можно выбрать другой профиль в настройках выхода.')
            self._finish('done', 'Готово: подписка заменена.' + (' Требуют внимания: '+', '.join(warnings) if warnings else ''),
                         warnings=warnings)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    # -- restore
    def restore(self, backup_id):
        path = backup_path(backup_id)
        cfg = installed()
        saved = {p.stem for p in (path/'instances').glob('*.json')}
        if saved != set(cfg):
            raise OpsError('Набор выходов изменился после этой копии; восстановление вручную.')
        self._begin('restore', 'applying')
        threading.Thread(target=self._restore, args=(path,), daemon=True).start()

    def _restore(self, path):
        try:
            names = list(installed())
            current = backup('before-restore', names)
            self._update('Текущее состояние сохранено: '+current, backup_id=current)
            failed = []
            for name in names:
                self._update(f'{name}: восстанавливаю…')
                result = restore_instance(path, name)
                self._update(f'{name}: {"работает" if result == "running" else "пока не прошёл проверку ("+result+")"}.')
                if result != 'running':
                    failed.append(name)
            self._finish('done', 'Восстановление завершено.' + (' Требуют внимания: '+', '.join(failed) if failed else ''),
                         warnings=failed)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    # -- AmneziaWG exits
    def awg_add(self, data, force_budget=False):
        """New AWG exit: validate everything first, then start it in the background."""
        if not isinstance(data, dict):
            raise OpsError('Неверный запрос.')
        self.awg_check()
        name, text = data.get('name'), data.get('profile')
        cfg = installed()
        if name in cfg:
            raise OpsError('Выход с таким именем уже есть.')
        facts = check_awg_profile(text)
        port = data.get('port')
        if port is None:
            port = next_port(cfg)
        elif type(port) is not int or port in {c['port'] for c in cfg.values()} or not port_free(port):
            # An explicit port is a promise to the panel (existing outbound): never substitute another.
            raise OpsError(f'Порт {port} занят; выход не создавался.')
        entry = {'kind': 'awg', 'port': port, 'slot': next_slot(cfg),
                 'country': str(data.get('country') or '').strip().upper()}
        mode = data.get('memory_mib')
        if mode not in (None, ''):
            if isinstance(mode, str) and mode.strip().isdigit():
                mode = int(mode)
            entry['memory_mib'] = mode
        for key in ('tag', 'label', 'outbound'):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value.strip()
        new = dict(cfg)
        new[name] = entry
        try:
            entry = validate_instances(new)[name]
        except ValueError as exc:
            raise OpsError(str(exc)) from None
        if not force_budget:
            awg_memory_check([entry['memory_mib']])
        self._begin('awg-add', 'applying')
        self._update(f'Профиль принят: сервер {facts["endpoint_host"]}:{facts["endpoint_port"]}, '
                     f'отпечаток ключа {facts["fingerprint"]}; режим памяти {entry["memory_mib"]} МиБ '
                     f'(ожидаемо ~{awg_budget(entry["memory_mib"])} МиБ).')
        threading.Thread(target=self._awg_add, args=(name, entry, text), daemon=True).start()
        return entry

    def _awg_add(self, name, entry, text):
        try:
            AWG_ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_file(AWG_ETC/f'{name}.conf', text, 0o600)
            cfg = installed()
            cfg[name] = entry
            write_instances(cfg)
            (VAR/name).mkdir(mode=0o700, parents=True, exist_ok=True)
            self._update(f'{name}: запускаю выход, реле {relay_address(entry)}:{entry["port"]} → {entry["tag"]}…')
            started = time.time()
            if run(['systemctl', 'enable', '--now', unit(name)], timeout=90).returncode:
                raise OpsError('Не удалось запустить службу выхода; см. journalctl -u '+unit(name))
            self._awg_done(name, wait_running(name, started, timeout=120), 'Выход добавлен')
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def awg_add_sync(self, data, progress=print, timeout=180, force_budget=False):
        """For the CLI and the wizard: same steps, waits and prints the job log."""
        self.awg_add(data, force_budget=force_budget)
        return self.wait(progress, timeout)

    def wait(self, progress=print, timeout=600):
        shown = 0
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            state = self.snapshot()
            log = state.get('log') or []
            for _, text in log[shown:]:
                progress(text)
            shown = len(log)
            if state['phase'] not in ('applying', 'discovering'):
                return state
            time.sleep(1)
        return self.snapshot()

    def awg_replace(self, name, text):
        cfg = installed()
        if name not in cfg or kind_of_cfg(cfg[name]) != 'awg':
            raise OpsError('Это не AWG-выход.')
        check_awg_profile(text, exclude=name)
        self._begin('awg-replace', 'applying')
        threading.Thread(target=self._awg_replace, args=(name, text), daemon=True).start()

    def _awg_replace(self, name, text):
        try:
            backup_id = backup('awg-profile', [name])
            self._update('Резервная копия прежнего профиля: '+backup_id, backup_id=backup_id)
            old = (AWG_ETC/f'{name}.conf').read_text()
            write_file(AWG_ETC/f'{name}.conf', text, 0o600)
            self._update(f'{name}: перезапускаю с новым профилем…')
            started = time.time()
            service(name, 'restart')
            result = wait_running(name, started, timeout=120)
            if result != 'running':
                self._update(f'{name}: новый профиль не прошёл проверку ({result}); возвращаю прежний…')
                write_file(AWG_ETC/f'{name}.conf', old, 0o600)
                started = time.time()
                service(name, 'restart')
                back = wait_running(name, started, timeout=120)
                raise OpsError(f'Новый профиль не заработал; прежний возвращён ({"работает" if back == "running" else back}).')
            self._awg_done(name, result, 'Профиль заменён')
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def _awg_done(self, name, result, prefix):
        state = read_json(VAR/name/'status.json', {}) or {}
        cfg = installed().get(name, {})
        where = f'socks {relay_address(cfg)}:{cfg.get("port")}, тег {cfg.get("tag")}' if cfg else ''
        if result == 'running':
            self._finish('done', f'{prefix}: {name} работает, выход {state.get("exit_country") or "?"} '
                         f'{state.get("exit_ip") or ""}. Для 3x-ui: {where}.', instance=name)
        else:
            reason = state.get('error') or state.get('degraded_reason') or result
            self._finish('done', f'{prefix}, но пока не работает ({reason}). Служба продолжает попытки; '
                         'журнал — на карточке выхода.', warnings=[name], instance=name)
