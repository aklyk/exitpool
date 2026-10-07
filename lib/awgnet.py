#!/usr/bin/env python3
"""AmneziaWG exit without Docker (exitpool scheme A).

amneziawg-go keeps its UDP transport in the host network namespace; its TUN interface is moved into an
isolated namespace exitpool-<name>. An unprivileged Xray SOCKS relay (TCP + UDP) runs inside that namespace
on the veth address 10.250.<slot>.2. nft inside the namespace is fail-closed: only the tunnel leaves it.
The relay opens only after HTTPS works through the tunnel (readiness) and closes on repeated failures.

Status and events use the same format as the Happ manager, so the CLI and the web page treat both alike.
Commands (systemd units call them): run, cleanup, cleanup-if-dead, guard-all, purge.
Key material never reaches logs, status or events.
"""
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402
from awg_profile import parse, strip, missing_after_apply  # noqa: E402
from configuration import validate_instances, kind as kind_of  # noqa: E402
import access_policy  # noqa: E402

LATENCY_SAMPLES = 120
DEGRADED_AFTER = 20          # seconds without a working tunnel before reporting "degraded"
READINESS_PAUSE = 2
UAPI_DIR = Path('/var/run/amneziawg')
SETPRIV = '/usr/bin/setpriv'
UNITS = {'manager': 'exitpool-awg@{}.service', 'relay': 'exitpool-awg-relay@{}.service',
         'socket': 'exitpool-awg-loopback@{}.socket', 'proxy': 'exitpool-awg-loopback@{}.service'}
PROBES = (('gstatic', 'https://www.gstatic.com/generate_204', 204),
          ('cloudflare', 'https://www.cloudflare.com/cdn-cgi/trace', 200))


class SafeError(RuntimeError):
    """Messages are safe to show: they never contain keys or profile text."""


class Restart(Exception):
    """Leave the run loop so systemd recreates the whole exit (fresh namespace and handshake)."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def run(args, data=None, check=True, timeout=30):
    args = [str(a) for a in args]
    r = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    if check and r.returncode:
        # awg output could mention key material; ip/nft/systemctl messages are safe.
        quiet = any(Path(a).name == 'awg' for a in args)
        output = '' if quiet else (r.stderr or r.stdout).strip()
        detail = ': '+output.splitlines()[-1][:200] if output else ''
        raise SafeError(f'{Path(args[0]).name} {args[1] if len(args) > 1 else ""} failed{detail}')
    return r


def unit(kind, name):
    return UNITS[kind].format(name)


def public_dir(path):
    """Directory readable by the relay's dynamic user (umask 077 would hide it)."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o755)
    return path


def network_lock():
    public_dir(paths.RUN)
    handle = (paths.RUN/'network.lock').open('a')
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def tombstone(name):
    """Set by purge: a cleanup that was already queued (OnSuccess/OnFailure) must not put the blackhole back."""
    return paths.RUN/'removed'/name


class Exit:
    def __init__(self, name):
        path = paths.ETC/'instances'/f'{name}.json'
        try:
            cfg = validate_instances({name: json.loads(path.read_text())})[name]
        except (OSError, ValueError) as exc:
            raise SafeError(f'Неверная конфигурация выхода {name}: {exc}') from None
        if kind_of(cfg) != 'awg':
            raise SafeError(f'{name} — не AWG-выход.')
        self.name, self.cfg = name, cfg
        slot = cfg['slot']
        self.slot, self.port = slot, cfg['port']
        self.ns = f'exitpool-{name}'
        self.tun, self.hostif, self.guestif = f'ep-t{slot}', f'ep-h{slot}', f'ep-n{slot}'
        self.host, self.guest, self.net = f'10.250.{slot}.1', f'10.250.{slot}.2', f'10.250.{slot}.0/30'
        self.state = paths.VAR/name
        self.rundir = paths.RUN/name
        self.profile = paths.AWG_ETC/f'{name}.conf'
        self.mode = int(cfg['memory_mib'])
        self.country = (cfg.get('country') or '').upper()
        self.status = {'kind': 'awg', 'country': self.country, 'port': self.port, 'healthy': False,
                       'phase': 'starting', 'started_at': time.time(), 'failures': 0, 'tun': True,
                       'memory_mode': self.mode}
        self.child = None
        self.key_lock = None
        self.down_since = None      # an outage that started before this run (restart during an outage)
        self.down_reason = None

    # -- status and events (same format as the Happ manager)
    def write_status(self, **values):
        self.status.update(values)
        self.status['updated_at'] = time.time()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.state/'status.tmp'
        temp.write_text(json.dumps(self.status, ensure_ascii=False, indent=2)+'\n')
        temp.replace(self.state/'status.json')

    def event(self, kind, **values):
        line = json.dumps({'at': round(time.time(), 1), 'event': kind, **values}, ensure_ascii=False)
        path = self.state/'events.jsonl'
        try:
            if path.exists() and path.stat().st_size > 64*1024:
                kept = path.read_text().splitlines()[-300:]
                temp = self.state/'events.tmp'
                temp.write_text('\n'.join(kept+[line])+'\n')
                temp.replace(path)
            else:
                with path.open('a') as out:
                    out.write(line+'\n')
        except OSError:
            pass

    def log(self, message):
        print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'AWG', self.name, message, flush=True)

    # -- network
    def nft_rules(self):
        return f'''table inet exitpool {{
  chain output {{
    type filter hook output priority 0; policy drop;
    meta nfproto ipv6 counter drop comment "no-ipv6"
    oifname "lo" accept
    oifname "{self.guestif}" ip daddr {self.host} ct direction reply ct state established accept comment "relay-replies"
    oifname "{self.tun}" accept comment "tunnel"
    counter drop comment "blocked-output"
  }}
  chain input {{
    type filter hook input priority 0; policy drop;
    meta nfproto ipv6 counter drop
    iifname "lo" accept
    iifname "{self.guestif}" ip saddr {self.host} tcp dport {self.port} accept comment "relay"
    iifname "{self.guestif}" ip saddr {self.host} meta l4proto udp accept comment "relay-udp"
    iifname "{self.tun}" ct state established,related accept comment "tunnel-replies"
    counter drop comment "blocked-input"
  }}
  chain forward {{ type filter hook forward priority 0; policy drop; }}
}}
'''

    def in_ns(self, args, **kwargs):
        return run(['ip', 'netns', 'exec', self.ns, *args], **kwargs)

    def blackhole(self):
        # Keeps the relay address off the default route whenever the veth is absent.
        run(['ip', 'route', 'replace', 'blackhole', self.net, 'metric', '32760'])

    def setup_namespace(self, facts):
        with network_lock():
            tombstone(self.name).unlink(missing_ok=True)   # the exit was added again under the same name
            access_policy.apply()
            self.blackhole()
            if run(['ip', 'netns', 'list'], check=False).stdout.split().count(self.ns):
                raise SafeError('остался namespace прошлого запуска; уборка не прошла')
            public_dir(paths.RUN)
            public_dir(self.rundir)
            run(['ip', 'netns', 'add', self.ns])
            run(['ip', 'link', 'add', self.hostif, 'type', 'veth', 'peer', 'name', self.guestif])
            run(['ip', 'link', 'set', self.guestif, 'netns', self.ns])
            self.in_ns(['sysctl', '-qw', 'net.ipv6.conf.all.disable_ipv6=1', 'net.ipv6.conf.default.disable_ipv6=1'])
            self.in_ns(['nft', '-f', '-'], data=self.nft_rules())
            run(['ip', 'addr', 'add', f'{self.host}/30', 'dev', self.hostif])
            run(['ip', 'link', 'set', self.hostif, 'up'])
            self.in_ns(['ip', 'addr', 'add', f'{self.guest}/30', 'dev', self.guestif])
            self.in_ns(['ip', 'link', 'set', self.guestif, 'up'])
            self.in_ns(['ip', 'link', 'set', 'lo', 'up'])
            dns = public_dir(paths.NETNS_ETC/self.ns)
            servers = facts['dns4'] or ['1.1.1.1', '8.8.8.8']
            (dns/'resolv.conf').write_text(''.join(f'nameserver {ip}\n' for ip in servers)+'options timeout:2 attempts:1\n')

    def start_tunnel(self, text, facts):
        self.state.joinpath('logs').mkdir(mode=0o700, parents=True, exist_ok=True)
        env = dict(os.environ, LOG_LEVEL='error', GOMEMLIMIT=f'{self.mode}MiB',
                   GOMAXPROCS=str(min(2, os.cpu_count() or 1)))
        argv = [str(paths.BIN/'amneziawg-go'), '-f', self.tun]
        if Path(SETPRIV).exists():
            argv = [SETPRIV, '--bounding-set=-all,+net_admin', '--no-new-privs', *argv]
        out = (self.state/'logs'/'awg.log').open('ab', buffering=0)
        self.child = subprocess.Popen(argv, stdout=out, stderr=out, env=env)
        out.close()
        sock = UAPI_DIR/f'{self.tun}.sock'
        for _ in range(100):
            if sock.exists():
                break
            if self.child.poll() is not None:
                raise SafeError('amneziawg-go завершился до создания интерфейса')
            time.sleep(.1)
        else:
            raise SafeError('amneziawg-go не создал управляющий сокет')
        run([paths.BIN/'awg', 'setconf', self.tun, '/dev/stdin'], data=strip(text))
        missing = missing_after_apply(text, run([paths.BIN/'awg', 'showconf', self.tun]).stdout)
        if missing:
            raise SafeError('параметры профиля не применились: '+', '.join(missing))
        mtu = int(self.cfg.get('mtu') or facts['mtu'] or 1280)
        # Scheme A: MTU is fixed before the move; changing it later recreates the exit.
        run(['ip', 'link', 'set', self.tun, 'mtu', str(mtu)])
        run(['ip', 'link', 'set', self.tun, 'netns', self.ns])
        self.in_ns(['ip', '-4', 'addr', 'add', facts['address4'], 'dev', self.tun])
        self.in_ns(['ip', 'link', 'set', self.tun, 'mtu', str(mtu), 'up'])
        self.in_ns(['ip', '-4', 'route', 'replace', 'default', 'dev', self.tun])
        if missing_after_apply(text, self.in_ns([paths.BIN/'awg', 'showconf', self.tun]).stdout):
            raise SafeError('профиль изменился после переноса интерфейса')
        return mtu

    def write_relay(self, facts):
        servers = facts['dns4'] or ['1.1.1.1', '8.8.8.8']
        inbound = {'tag': 'relay', 'listen': self.guest, 'port': self.port, 'protocol': 'socks',
                   'settings': {'auth': 'noauth', 'udp': True, 'ip': self.guest}}

        def config(legacy):
            out = {'tag': 'tunnel', 'protocol': 'freedom'}
            if legacy:
                out['settings'] = {'domainStrategy': 'UseIPv4'}
            else:
                out['streamSettings'] = {'sockopt': {'domainStrategy': 'UseIPv4'}}
            return {'log': {'loglevel': 'warning', 'access': 'none'},
                    'dns': {'servers': servers, 'queryStrategy': 'UseIPv4'},
                    'inbounds': [inbound], 'outbounds': [out]}

        path = self.rundir/'relay.json'
        for legacy in (False, True):
            path.write_text(json.dumps(config(legacy)))
            path.chmod(0o644)   # read by the relay's dynamic user; contains no secrets
            if run([paths.BIN/'xray', 'run', '-test', '-c', path], check=False).returncode == 0:
                break
        else:
            raise SafeError('Xray не принял конфигурацию реле')
        memory = self.rundir/'memory.env'
        memory.write_text(f'GOMEMLIMIT={self.mode}MiB\nGOMAXPROCS={min(2, os.cpu_count() or 1)}\n')
        memory.chmod(0o644)

    def open_relay(self):
        run(['systemctl', 'start', unit('relay', self.name)])
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.guest, self.port), timeout=.3):
                    break
            except OSError:
                time.sleep(.2)
        else:
            raise SafeError('реле не начало принимать соединения')
        if self.cfg.get('outbound') == 'loopback':
            run(['systemctl', 'start', unit('socket', self.name)])

    def close_relay(self):
        for kind in ('socket', 'proxy', 'relay'):
            run(['systemctl', 'stop', unit(kind, self.name)], check=False)

    # -- checks
    def probe(self, via_relay):
        def one(target):
            tag, url, expected = target
            args = ['curl', '--silent', '--show-error', '--fail', '--noproxy', '', '--connect-timeout', '3',
                    '--max-time', '6', '--write-out', '\n%{http_code} %{time_total}']
            if via_relay:
                args += ['--socks5-hostname', f'{self.guest}:{self.port}']
                argv = args+[url]
            else:
                argv = ['ip', 'netns', 'exec', self.ns, *args, url]
            try:
                r = subprocess.run(argv, capture_output=True, timeout=9)
            except (subprocess.TimeoutExpired, OSError):
                return {'ok': False}
            if r.returncode:
                return {'ok': False, 'curl_code': r.returncode}
            try:
                body, tail = r.stdout.decode(errors='replace').rsplit('\n', 1)
                code, total = tail.split()
            except ValueError:
                return {'ok': False}
            result = {'ok': code == str(expected), 'http_code': int(code), 'ms': round(float(total)*1000)}
            if '/cdn-cgi/trace' in url:
                trace = dict(line.split('=', 1) for line in body.splitlines() if '=' in line)
                result.update(exit_country=trace.get('loc'), exit_ip=trace.get('ip'))
                result['ok'] = result['ok'] and (not self.country or trace.get('loc') == self.country)
            return result
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            google, trace = pool.map(one, PROBES)
        mismatch = bool(self.country and trace.get('exit_country') and trace['exit_country'] != self.country)
        return (google['ok'] or trace['ok']) and not mismatch, trace, google, mismatch

    def record_latency(self, trace, google):
        ms = next((r['ms'] for r in (google, trace) if r.get('ok') and r.get('ms') is not None), None)
        history = self.status.setdefault('latency_history', [])
        history.append([round(time.time()), ms])
        del history[:-LATENCY_SAMPLES]
        if ms is not None:
            self.status.update(latency_ms=ms, latency_at=time.time())

    def tunnel_info(self, facts, mtu):
        info = {'endpoint': f"{facts['endpoint_host']}:{facts['endpoint_port']}", 'mtu': mtu,
                'relay': f'{self.guest}:{self.port}', 'outbound': self.cfg.get('outbound', 'veth')}
        r = run([paths.BIN/'awg', 'show', self.tun, 'latest-handshakes'], check=False)
        fields = r.stdout.split()
        if r.returncode == 0 and len(fields) >= 2 and fields[1].isdigit() and int(fields[1]) > 0:
            info['handshake_age'] = round(time.time()-int(fields[1]))
        rx, tx, _ = self.transfer()
        info.update(rx=rx, tx=tx)
        return info

    def transfer(self):
        r = run([paths.BIN/'awg', 'show', self.tun, 'transfer'], check=False)
        rows = [line.split() for line in r.stdout.splitlines()]
        rx = sum(int(x[-2]) for x in rows if len(x) >= 3 and x[-2].isdigit())
        tx = sum(int(x[-1]) for x in rows if len(x) >= 3 and x[-1].isdigit())
        r = run(['ip', 'netns', 'exec', self.ns, 'ip', '-s', '-j', 'link', 'show', self.tun], check=False)
        try:
            plain = json.loads(r.stdout)[0]['stats64']['tx']['bytes']
        except (ValueError, KeyError, IndexError, TypeError):
            plain = 0
        return rx, tx, plain

    def take_command(self):
        path = self.state/'command.json'
        if not path.exists():
            return None
        try:
            action = json.loads(path.read_text()).get('action')
        except (OSError, ValueError, AttributeError):
            action = None
        path.unlink(missing_ok=True)
        return action if action in ('reconnect', 'probe') else None

    # -- lifecycle
    def lock_key(self, fingerprint):
        keys = paths.RUN/'keys'
        keys.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.key_lock = (keys/f'{fingerprint}.lock').open('a')
        try:
            fcntl.flock(self.key_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafeError('этот ключ уже использует другой выход: один ключ — один туннель') from None

    def wait_ready(self, facts):
        """Relay stays closed until HTTPS works through the tunnel itself."""
        since = time.monotonic()
        degraded = False
        while True:
            if self.child.poll() is not None:
                raise SafeError('amneziawg-go завершился')
            if self.take_command() == 'reconnect':
                raise Restart('manual')
            ok, trace, google, mismatch = self.probe(via_relay=False)
            self.record_latency(trace, google)
            if ok:
                return trace
            if not degraded and time.monotonic()-since >= DEGRADED_AFTER:
                reason = 'country_mismatch' if mismatch else 'upstream'
                if self.down_since is None:     # a new outage, not the continuation of a previous one
                    self.down_since, self.down_reason = time.time(), reason
                    self.event('down', reason=reason)
                self.write_status(healthy=False, phase='degraded', degraded_reason=reason,
                                  down_since=self.down_since, action='probe')
                self.log('degraded: '+reason)
                degraded = True
            else:
                self.write_status(failures=self.status.get('failures', 0)+(1 if degraded else 0))
            time.sleep(READINESS_PAUSE)

    def run(self):
        try:
            previous = json.loads((self.state/'status.json').read_text())
        except (OSError, ValueError):
            previous = {}
        for key in ('latency_history', 'latency_ms', 'latency_at', 'exit_country', 'exit_ip'):
            if key in previous:
                self.status[key] = previous[key][-LATENCY_SAMPLES:] if key == 'latency_history' else previous[key]
        if previous.get('phase') == 'degraded' and previous.get('down_since'):
            self.down_since, self.down_reason = previous['down_since'], previous.get('degraded_reason')
        # Always "connecting" at start: "degraded" means this run already waited DEGRADED_AFTER in vain.
        self.write_status(phase='connecting', healthy=False, error=None, down_since=self.down_since,
                          degraded_reason=self.down_reason)
        self.event('start')
        text = self.profile.read_text()
        facts = parse(text)
        self.status['profile_fingerprint'] = facts['fingerprint']
        self.lock_key(facts['fingerprint'])
        self.setup_namespace(facts)
        mtu = self.start_tunnel(text, facts)
        self.write_relay(facts)
        self.log(f'tunnel up (mtu {mtu}); waiting for HTTPS through the tunnel')
        trace = self.wait_ready(facts)
        self.open_relay()
        down_since = self.down_since
        self.write_status(healthy=True, phase='running', failures=0, last_success=time.time(),
                          exit_country=trace.get('exit_country') or self.status.get('exit_country'),
                          exit_ip=trace.get('exit_ip') or self.status.get('exit_ip'),
                          degraded_reason=None, down_since=None, action=None, error=None,
                          tunnel=self.tunnel_info(facts, mtu))
        if down_since:
            self.event('up', reason=self.down_reason or 'upstream', duration=round(time.time()-down_since))
        else:
            self.event('up', reason='start', duration=round(time.time()-self.status['started_at']))
        self.log(f'ready: relay {self.guest}:{self.port}')
        self.supervise(facts, mtu)

    def supervise(self, facts, mtu):
        interval = int(self.cfg['health_interval'])
        threshold = int(self.cfg['failure_threshold'])
        failures, next_probe, last_check, last_hint, tick = 0, time.monotonic()+interval, time.monotonic(), 0, 0
        previous, window = self.transfer(), []
        while True:
            time.sleep(1)
            tick += 1
            now = time.monotonic()
            requested = self.take_command()
            if requested == 'reconnect':
                raise Restart('manual')
            current = self.transfer()
            delta = [c-p for c, p in zip(current, previous)]
            previous = current
            window = [item for item in window+[(now, delta)] if item[0] >= now-3]
            summed = [sum(max(0, item[1][i]) for item in window) for i in range(3)]
            # Plaintext and tunnel bytes leave, nothing comes back: check now, never restart on this alone.
            hint = (summed[1] >= 4096 and summed[2] >= 4096 and summed[0] == 0
                    and now-last_check >= 3 and now-last_hint >= interval)
            if now >= next_probe or hint or requested == 'probe':
                if hint and now < next_probe:
                    last_hint = now
                started = time.monotonic()
                ok, trace, google, mismatch = self.probe(via_relay=True)
                self.record_latency(trace, google)
                failures = 0 if ok else failures+1
                last_check, next_probe = time.monotonic(), started+interval
                values = {'healthy': ok, 'failures': failures, 'checks': {'cloudflare': trace, 'google': google},
                          'tunnel': self.tunnel_info(facts, mtu)}
                if ok:
                    values['last_success'] = time.time()
                    if trace.get('exit_country'):
                        values.update(exit_country=trace['exit_country'], exit_ip=trace.get('exit_ip'))
                self.write_status(**values)
                if mismatch or failures >= threshold:
                    reason = 'country_mismatch' if mismatch else 'upstream'
                    self.close_relay()
                    self.write_status(healthy=False, phase='degraded', degraded_reason=reason,
                                      down_since=time.time(), action='reconnect')
                    self.event('down', reason=reason)
                    self.log('degraded: '+reason)
                    raise Restart(reason)
            if self.child.poll() is not None:
                raise SafeError('amneziawg-go завершился')
            if tick % 5 == 0 and run(['systemctl', 'is-active', '--quiet', unit('relay', self.name)],
                                     check=False).returncode:
                raise SafeError('реле остановилось')

    def stop_children(self):
        self.close_relay()
        if self.child is not None and self.child.poll() is None and self.child.pid > 1:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=3)


def cleanup(name, only_if_dead=False):
    """Remove everything one exit created, except the blackhole route. Idempotent."""
    ex = Exit(name)
    with network_lock():
        if tombstone(name).exists():
            print(json.dumps({'event': 'cleanup_skipped_removed', 'name': name}), flush=True)
            return
        if only_if_dead:
            pid = run(['systemctl', 'show', unit('manager', name), '-p', 'MainPID', '--value'],
                      check=False).stdout.strip()
            if pid.isdigit() and int(pid) > 0 and Path(f'/proc/{pid}').exists():
                print(json.dumps({'event': 'cleanup_skipped_live', 'name': name}), flush=True)
                return
        ex.blackhole()
        _remove_objects(ex)
    print(json.dumps({'event': 'cleanup_done', 'name': name}), flush=True)


def _remove_objects(ex):
    """Units, links, namespace and run files of one exit; the caller holds the network lock."""
    for kind in ('socket', 'proxy', 'relay'):
        run(['systemctl', 'stop', unit(kind, ex.name)], check=False)
    run(['ip', 'link', 'del', ex.hostif], check=False)
    run(['ip', 'link', 'del', ex.tun], check=False)
    run(['ip', 'netns', 'del', ex.ns], check=False)
    for suffix in ('sock', 'name'):
        (UAPI_DIR/f'{ex.tun}.{suffix}').unlink(missing_ok=True)
    _rmtree(paths.NETNS_ETC/ex.ns)
    _rmtree(ex.rundir)


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def guard_all():
    """At boot: blackhole routes for every AWG exit and the host forward guard, before any exit starts."""
    for path in sorted((paths.ETC/'instances').glob('*.json')):
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if kind_of(cfg) == 'awg' and isinstance(cfg.get('slot'), int):
            run(['ip', 'route', 'replace', 'blackhole', f"10.250.{cfg['slot']}.0/30", 'metric', '32760'], check=False)
    access_policy.apply()


def purge(name):
    """Full removal of an exit's network objects, including the blackhole route.

    Everything happens under one lock and ends with a tombstone; otherwise the cleanup unit queued by the
    stop could run after the route was deleted and put the blackhole of a removed exit back (seen in tests).
    """
    ex = Exit(name)
    with network_lock():
        _remove_objects(ex)
        run(['ip', 'route', 'del', 'blackhole', ex.net, 'metric', '32760'], check=False)
        public_dir(paths.RUN/'removed')
        tombstone(name).touch()
    print(json.dumps({'event': 'purge_done', 'name': name}), flush=True)


def main():
    action, name = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    os.umask(0o077)
    os.environ['PATH'] = f'{paths.BIN}:/usr/sbin:/usr/bin:/sbin:/bin'
    if action == 'cleanup':
        return cleanup(name)
    if action == 'cleanup-if-dead':
        return cleanup(name, only_if_dead=True)
    if action == 'guard-all':
        return guard_all()
    if action == 'purge':
        return purge(name)
    if action != 'run':
        raise SystemExit('usage: awgnet.py run|cleanup|cleanup-if-dead|purge NAME | guard-all')
    ex = Exit(name)

    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        ex.run()
    except KeyboardInterrupt:
        ex.stop_children()
        ex.write_status(healthy=False, phase='stopping', action=None)
        ex.event('stop')
        return 0
    except Restart as restart:
        ex.stop_children()
        if restart.reason == 'manual':
            ex.write_status(healthy=False, phase='degraded', degraded_reason='manual', down_since=time.time())
            ex.event('down', reason='manual')
        ex.log('recreating the exit: '+restart.reason)
        return 1   # systemd restarts the unit; the cleanup unit removes the old namespace first
    except Exception as exc:  # noqa: BLE001 - only safe summaries leave this process
        ex.stop_children()
        text = str(exc) if isinstance(exc, (SafeError, ValueError)) else type(exc).__name__
        ex.log(text)
        ex.event('failed', error=text[:200])
        ex.write_status(healthy=False, phase='failed', error=text[:200])
        return 1


if __name__ == '__main__':
    raise SystemExit(main() or 0)
