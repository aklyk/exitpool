#!/usr/bin/env python3
"""Is this server suitable? Read-only inspection plus an optional isolated, self-cleaning kernel test.

Only Ubuntu 24.04 amd64 is tested; other systems get an honest "not verified" and the checks decide.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory_budget  # noqa: E402

TOOLS = {'ip': 'iproute2', 'nft': 'nftables', 'curl': 'curl', 'setpriv': 'util-linux',
         'systemctl': 'systemd', 'python3': 'python3'}
PROXYD = '/usr/lib/systemd/systemd-socket-proxyd'
NAMES = {'PASS': 'OK', 'WARN': 'внимание', 'FAIL': 'не подходит', 'INFO': 'сведения'}


def run(args, timeout=20):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {'code': r.returncode, 'stdout': r.stdout.strip(), 'stderr': r.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'code': -1, 'stdout': '', 'stderr': str(exc)}


def collect(awg_modes=(64,), happ=0, web=False, docker_missing=None, ports=(), self_test=False):
    checks = []

    def add(name, status, text, **detail):
        checks.append({'check': name, 'status': status, 'text': text, 'detail': detail})

    release = {}
    try:
        release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    except OSError:
        pass
    os_id, version = release.get('ID', '').strip('"'), release.get('VERSION_ID', '').strip('"')
    pretty = release.get('PRETTY_NAME', '?').strip('"')
    tested = os_id == 'ubuntu' and version == '24.04' and platform.machine() == 'x86_64'
    add('platform', 'PASS' if tested else 'WARN',
        f'{pretty}, {platform.machine()}, ядро {platform.release()}' +
        ('' if tested else ' — проверялась только Ubuntu 24.04 amd64; остальное может работать, решают проверки ниже'))
    if awg_modes:
        add('tun', 'PASS' if Path('/dev/net/tun').exists() else 'FAIL',
            '/dev/net/tun есть' if Path('/dev/net/tun').exists() else
            'нет /dev/net/tun — AWG-выход невозможен (часто на OpenVZ/LXC VPS); Happ при этом работает')
        missing = [f'{tool} ({pkg})' for tool, pkg in TOOLS.items() if not shutil.which(tool)]
        add('tools', 'FAIL' if missing else 'PASS',
            'не хватает: '+', '.join(missing) if missing else 'ip, nft, curl, setpriv, systemd, python3 на месте')
        add('socket_proxyd', 'PASS' if Path(PROXYD).exists() else 'WARN',
            'systemd-socket-proxyd есть (вход 127.0.0.1 для 3x-ui)' if Path(PROXYD).exists() else
            'нет systemd-socket-proxyd — вход 127.0.0.1 недоступен, используйте адрес veth')
        with tempfile.TemporaryDirectory(prefix='exitpool-preflight-') as tmp:
            unit = Path(tmp)/'exitpool-preflight-check.service'
            unit.write_text('[Service]\nExecStart=/usr/bin/true\nNetworkNamespacePath=/run/netns/exitpool-preflight\n')
            verified = run(['systemd-analyze', 'verify', str(unit)])
        add('systemd', 'PASS' if verified['code'] == 0 else 'FAIL',
            'systemd понимает NetworkNamespacePath' if verified['code'] == 0 else
            'systemd не принимает NetworkNamespacePath (нужен systemd 242+)')

    budget = memory_budget.read_memory()
    if docker_missing is None:
        docker_missing = bool(happ) and not shutil.which('docker')
    if budget:
        plan = memory_budget.plan(budget.available_mib, happ=happ, awg_modes=list(awg_modes), web=web,
                                  docker_missing=docker_missing)
        parts = ', '.join(f'{name} ~{mib}' for name, mib in plan['items']) or 'ничего'
        text = (f"доступно {budget.available_mib} МиБ; нужно ~{plan['expected']} ({parts}) + запас {plan['reserve']}")
        if plan['status'] == 'WARN':
            text += ' — помещается, но без запаса системе'
        elif plan['status'] == 'FAIL':
            text += ' — не помещается'
        caps = [memory_budget.awg_caps(mode) for mode in awg_modes]
        add('memory', plan['status'], text, plan=plan,
            hard_caps=[{'manager_awg_mib': c[0], 'relay_mib': c[1]} for c in caps])
    else:
        add('memory', 'WARN', 'доступную память определить не удалось')

    routes = run(['ip', '-j', 'route', 'show', 'table', 'all'])
    pool = ipaddress.ip_network('10.250.0.0/16')
    slots = [ipaddress.ip_network(f'10.250.{n}.0/30') for n in range(1, 251)]
    narrow, broad, taken = [], [], set()
    try:
        for route in json.loads(routes['stdout'] or '[]'):
            dst = route.get('dst', 'default')
            if dst == 'default' or route.get('type') == 'blackhole' or str(route.get('dev', '')).startswith('ep-h'):
                continue   # exitpool's own veths and blackholes
            try:
                network = ipaddress.ip_network(dst, strict=False)
            except ValueError:
                continue
            if network.version != 4 or not network.overlaps(pool):
                continue
            if network.prefixlen < 16:
                broad.append(f"{dst} ({route.get('dev') or route.get('type', '?')})")
            else:
                narrow.append(dst)
                taken.update(i for i, slot in enumerate(slots) if slot.overlaps(network))
    except ValueError:
        pass
    if len(taken) == len(slots):
        add('subnet', 'FAIL', 'вся 10.250.0.0/16 занята чужими маршрутами ('+', '.join(narrow[:3])+') — '
            'AWG-выходам негде взять адреса')
    elif narrow or broad:
        parts = []
        if narrow:
            parts.append('заняты чужими маршрутами: '+', '.join(narrow[:5])+' — exitpool возьмёт свободные /30')
        if broad:
            parts.append('маршрут '+', '.join(broad[:3])+' шире пула: адреса 10.250.N.0/30 выходов будут '
                         'обслуживаться exitpool, а не этим маршрутом')
        add('subnet', 'WARN', '; '.join(parts))
    else:
        add('subnet', 'PASS', '10.250.0.0/16 свободна')

    if ports:
        listeners = run(['ss', '-H', '-lnt'])['stdout'].splitlines()
        busy = sorted({p for p in ports for line in listeners
                       if len(line.split()) > 3 and line.split()[3].rsplit(':', 1)[-1] == str(p)})
        add('ports', 'WARN' if busy else 'PASS', 'заняты порты: '+', '.join(map(str, busy)) if busy else 'порты свободны')

    neighbors = {name: run(['systemctl', 'is-active', name])['stdout'] for name in ('docker', 'ufw', 'firewalld')}
    ufw = run(['ufw', 'status'])['stdout'] if shutil.which('ufw') else ''
    active = [k for k, v in neighbors.items() if v == 'active' and (k != 'ufw' or 'Status: active' in ufw)]
    add('neighbors', 'INFO', ('рядом работают: '+', '.join(active)+'. exitpool их не перенастраивает; '
                              'доступ контейнеров к реле по умолчанию закрыт') if active else 'Docker/UFW/firewalld не активны')

    if self_test and awg_modes:
        add(*kernel_self_test())
    verdict = 'FAIL' if any(c['status'] == 'FAIL' for c in checks) else \
        'WARN' if any(c['status'] == 'WARN' for c in checks) else 'PASS'
    return {'verdict': verdict, 'checks': checks}


def kernel_self_test():
    """Temporary netns + veth + TUN moved into the namespace; no addresses, routes or traffic."""
    if os.geteuid() != 0:
        return 'kernel', 'WARN', 'самотест ядра пропущен: нужен root'
    tag = uuid.uuid4().hex[:6]
    ns, host, peer, tun = f'ep-pre-{tag}', f'eph{tag}', f'epn{tag}', f'ept{tag}'
    undo = [['ip', 'link', 'del', host], ['ip', 'link', 'del', tun], ['ip', 'netns', 'del', ns]]
    code = 'import subprocess\n'+''.join(f'subprocess.run({c!r},capture_output=True)\n' for c in undo)
    timer = f'exitpool-preflight-clean-{tag}'
    previous = {s: signal.signal(s, lambda *a: (_ for _ in ()).throw(InterruptedError())) for s in (signal.SIGINT, signal.SIGTERM)}
    run(['systemd-run', '--quiet', f'--unit={timer}', '--on-active=10min', '/usr/bin/python3', '-c', code])
    try:
        for cmd in (['ip', 'netns', 'add', ns], ['ip', 'link', 'add', host, 'type', 'veth', 'peer', 'name', peer],
                    ['ip', 'link', 'set', peer, 'netns', ns], ['ip', 'tuntap', 'add', 'dev', tun, 'mode', 'tun'],
                    ['ip', 'link', 'set', tun, 'netns', ns], ['ip', '-n', ns, 'link', 'show', tun]):
            r = run(cmd)
            if r['code']:
                return 'kernel', 'FAIL', f'самотест ядра не прошёл на «{" ".join(cmd[:4])}»: {r["stderr"][:120]}'
        return 'kernel', 'PASS', 'самотест ядра: netns, veth и перенос TUN в namespace работают'
    except InterruptedError:
        return 'kernel', 'WARN', 'самотест прерван'
    finally:
        for cmd in undo:
            run(cmd)
        run(['systemctl', 'stop', f'{timer}.timer'])
        for s, handler in previous.items():
            signal.signal(s, handler)


def render(result):
    lines = []
    for c in result['checks']:
        lines.append(f"  [{NAMES.get(c['status'], c['status'])}] {c['text']}")
    lines.append('Итог: '+{'PASS': 'сервер подходит', 'WARN': 'подходит с оговорками',
                          'FAIL': 'не подходит для выбранного'}[result['verdict']])
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description='Проверка сервера для exitpool')
    ap.add_argument('--awg', type=int, action='append', metavar='РЕЖИМ', help='режим памяти AWG-выхода, можно несколько')
    ap.add_argument('--happ', type=int, default=0)
    ap.add_argument('--web', action='store_true')
    ap.add_argument('--ports', default='')
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    result = collect(a.awg if a.awg is not None else [64], a.happ, a.web,
                     ports=[int(p) for p in a.ports.split(',') if p.strip()], self_test=a.self_test)
    print(json.dumps(result, ensure_ascii=False, indent=2) if a.json else render(result))
    return 1 if result['verdict'] == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())
