#!/usr/bin/env python3
"""Supervise a pinned Happ GUI in an isolated container; never use TUN."""
import concurrent.futures
import configparser
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from ui_profiles import ProfilesUI, ProfileError, ProfileMissing
from recovery import Outage, PROBE_SECONDS, ADAPTER_ERRORS

STATE = Path('/state')
RUN = Path('/run/happ')
DISCOVER = '--discover' in sys.argv
CFG = {} if DISCOVER else json.loads(Path('/run/config.json').read_text())
PORT = int(CFG.get('port', 11808))
COUNTRY = CFG.get('country', '').upper()
QUERY = CFG.get('profile_name', CFG.get('profile_query', ''))
UPDATE_SECONDS = CFG.get('update_hours', 6)*3600 + PORT%17*20
STATUS = {'country': COUNTRY, 'profile_name': QUERY, 'port': PORT,
          'healthy': False, 'phase': 'starting', 'started_at': time.time(),
          'failures': 0, 'happ_version': '4.3.0-318', 'tun': False}
PROCESSES = []
LATENCY_SAMPLES = 120   # ~40 min of 20-second checks


class UpstreamError(RuntimeError):
    """The selected profile is chosen but HTTPS through it does not work."""


def write_status(**values):
    STATUS.update(values)
    STATUS['updated_at'] = time.time()
    p = STATE / 'status.tmp'
    p.write_text(json.dumps(STATUS, ensure_ascii=False, indent=2)+'\n')
    p.replace(STATE/'status.json')


def log(message):
    print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), COUNTRY or QUERY[:24], message, flush=True)


def event(kind, **values):
    # Short private history for the host status page; never contains subscription data.
    line = json.dumps({'at': round(time.time(), 1), 'event': kind, **values}, ensure_ascii=False)
    path = STATE/'events.jsonl'
    try:
        if path.exists() and path.stat().st_size > 64*1024:
            kept = path.read_text().splitlines()[-300:]
            temp = STATE/'events.tmp'
            temp.write_text('\n'.join(kept+[line])+'\n')
            temp.replace(path)
        else:
            with path.open('a') as f: f.write(line+'\n')
    except OSError:
        pass


def prefs():
    c = configparser.ConfigParser(interpolation=None)
    c.optionxform = str
    c.read(STATE/'config/Happ.conf')
    return c


def configure():
    c = prefs()
    if not c.has_section('Preferences'):
        c.add_section('Preferences')
    p = c['Preferences']
    # Protected JSON subscriptions may ignore the GUI's inbound port settings.
    # Each container keeps Happ's original 10808; our separate relay exposes PORT.
    p.update({r'AdvancedSettings\tun': 'false',
              r'AdvancedSettings\systemProxy': 'true',
              r'AdvancedSettings\autoStart': 'false',
              'allowConnectionFromLAN': 'false',
              'graphicsBackend': 'Software',
              'windowWidth': '800', 'windowHeight': '536',
              'windowX': '240', 'windowY': '182', 'windowVisibility': '2',
              r'Subscriptions\subsConnectOnOpen': 'false',
              r'Subscriptions\subsUpdateOnOpen': 'false',
              r'Subscriptions\subsPingOnOpen': 'false',
              r'Subscriptions\subsAutoUpdate': 'false',
              r'Subscriptions\subsAutoUpdateInterval': str(CFG.get('update_hours', 6)),
              r'Subscriptions\subsIgnoreDuplicates': 'true'})
    with (STATE/'config/Happ.conf').open('w') as f:
        c.write(f, space_around_delimiters=False)


def db_updated():
    try:
        with sqlite3.connect('file:/state/config/Happ/subs.db?mode=ro', uri=True, timeout=1) as db:
            return db.execute('SELECT max(updated_at) FROM subscriptions').fetchone()[0]
    except (sqlite3.Error, TypeError):
        return None


def spawn(argv, logfile, env=None):
    f = (STATE/'logs'/logfile).open('ab', buffering=0)
    p = subprocess.Popen(argv, env=env, stdout=f, stderr=f)
    f.close()
    PROCESSES.append(p)
    return p


def request(url, port, expected_status=None):
    try:
        r = subprocess.run(['curl', '--silent', '--show-error', '--fail',
                            '--noproxy', '', '--connect-timeout', '5', '--max-time', '10',
                            '--proxy', f'socks5h://127.0.0.1:{port}',
                            '--write-out', '\n%{http_code} %{time_total}', url], capture_output=True, timeout=12)
        if r.returncode:
            return {'ok': False, 'curl_code': r.returncode}
        body, tail = r.stdout.decode(errors='replace').rsplit('\n', 1)
        code, total = tail.split()
        # Full HTTPS request on a new connection through the exit ("real delay").
        result = {'ok': expected_status is None or code == str(expected_status), 'http_code': int(code),
                  'ms': round(float(total)*1000)}
        if '/cdn-cgi/trace' in url:
            trace = dict(line.split('=', 1) for line in body.splitlines() if '=' in line)
            result.update(exit_country=trace.get('loc'), exit_ip=trace.get('ip'))
            result['ok'] = result['ok'] and (not COUNTRY or trace.get('loc') == COUNTRY)
        return result
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return {'ok': False}


def health(port, require_country=False):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        trace = pool.submit(request, 'https://www.cloudflare.com/cdn-cgi/trace', port, 200)
        google = pool.submit(request, 'https://www.gstatic.com/generate_204', port, 204)
        a, b = trace.result(), google.result()
    # An explicit country mismatch must never be treated as a healthy target.
    mismatch = bool(COUNTRY and a.get('exit_country') is not None and a['exit_country'] != COUNTRY)
    ok = (a['ok'] if require_country else (a['ok'] or b['ok'])) and not mismatch
    return ok, a, b


def record_latency(a, b):
    ms = next((r['ms'] for r in (b, a) if r.get('ok') and r.get('ms') is not None), None)
    history = STATUS.setdefault('latency_history', [])
    history.append([round(time.time()), ms])
    del history[:-LATENCY_SAMPLES]
    if ms is not None:
        STATUS.update(latency_ms=ms, latency_at=time.time())


def pause(seconds):
    # Sleep, but react to a queued command at once instead of after the whole interval.
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if (STATE/'command.json').exists(): return
        time.sleep(max(0, min(1, deadline-time.monotonic())))


def start_relay():
    # This Xray allocates a dynamic UDP port for each SOCKS association.
    # Advertise the container's bridge address: host Xray can reach it directly.
    # Mapping one fixed UDP port would break UDP ASSOCIATE.
    bridge_ip = socket.gethostbyname(socket.gethostname())
    config = {'log': {'loglevel': 'warning', 'access': 'none'},
              'inbounds': [{'tag': 'local-relay', 'listen': '0.0.0.0', 'port': PORT,
                            'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': True, 'ip': bridge_ip}}],
              'outbounds': [{'tag': 'happ', 'protocol': 'socks',
                             'settings': {'servers': [{'address': '127.0.0.1', 'port': 10808}]}}]}
    path = RUN/'relay.json'
    path.write_text(json.dumps(config))
    process=spawn(['/opt/happ/bin/core/xray', 'run', '-c', str(path)], 'relay.log')
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        if process.poll() is not None:raise RuntimeError('Local SOCKS relay exited during startup')
        try:
            with socket.create_connection(('127.0.0.1',PORT),timeout=.2):return process
        except OSError:time.sleep(.1)
    raise RuntimeError('Local SOCKS relay did not start listening')


def close_relay(relay):
    if relay is None or relay.poll() is not None:
        return
    relay.terminate()
    try: relay.wait(timeout=5)
    except subprocess.TimeoutExpired: relay.kill(); relay.wait(timeout=3)


def rotate_logs():
    for p in (STATE/'logs').glob('*.log'):
        if p.stat().st_size > 2*1024*1024:
            # Children open logs with O_APPEND; truncation does not leave holes.
            with p.open('r+b') as f:
                f.seek(-128*1024, os.SEEK_END)
                tail = f.read()
                f.seek(0); f.write(tail); f.truncate()


def stop(signum=None, frame=None):
    write_status(healthy=False, phase='stopping')
    event('stop')
    for p in reversed(PROCESSES):
        if p.poll() is None:
            p.terminate()
    for p in reversed(PROCESSES):
        try: p.wait(timeout=2)
        except subprocess.TimeoutExpired: p.kill()
    raise SystemExit(0)


def socks_running():
    try:
        with socket.create_connection(('127.0.0.1',10808), timeout=1): return True
    except OSError: return False


def disconnect(ui):
    if not socks_running(): return
    ui.click(ui.x+617, ui.y+178)
    for _ in range(30):
        if not socks_running(): return
        time.sleep(.2)
    raise RuntimeError('Happ could not disconnect safely')


def connect_selected(ui, attempts=5):
    title = ui.select(QUERY, exact='profile_name' in CFG)
    if not socks_running(): ui.click(ui.x+617, ui.y+178)
    time.sleep(2)
    for attempt in range(attempts):
        ok, a, b = health(10808, require_country=bool(COUNTRY))
        if ok: return title, a
        if attempt+1 < attempts: time.sleep(3)
    raise UpstreamError('Selected profile did not pass verified HTTPS startup check')


def write_catalog(names):
    p=STATE/'catalog.tmp'
    p.write_text(json.dumps(names,ensure_ascii=False)+'\n')
    p.replace(STATE/'catalog.json')


def last_refresh():
    try: return json.loads((STATE/'refresh.json').read_text())['attempt_at']
    except (OSError, ValueError, KeyError):
        now = time.time()
        (STATE/'refresh.json').write_text(json.dumps({'attempt_at':now}))
        return now


def fetch_subscription(ui):
    (STATE/'refresh.json').write_text(json.dumps({'attempt_at':time.time()}))
    before=db_updated()
    ui.refresh()
    deadline=time.monotonic()+60
    while time.monotonic()<deadline and db_updated()==before:
        write_status()  # keep the status fresh for the host while Happ downloads
        time.sleep(1)
    refreshed=db_updated()!=before
    time.sleep(1) # allow Qt to finish replacing the subscription's list delegates
    write_catalog(ProfilesUI().catalog())
    STATUS['refresh_ok'] = refreshed
    event('refresh', ok=refreshed)
    return refreshed


def take_command():
    command = STATE/'command.json'
    if not command.exists(): return None
    try:
        action = json.loads(command.read_text()).get('action')
    except (OSError, ValueError, AttributeError):
        action = None
        log('could not process control command')
    # Always consume the file: a leftover would make pause() return immediately forever.
    try: command.unlink()
    except OSError: pass
    return action if action in ('refresh', 'reconnect', 'probe') else None


def mark_running(title, trace, relay):
    write_status(healthy=True, phase='running', selected_profile=title, failures=0,
                 exit_country=trace.get('exit_country'), exit_ip=trace.get('exit_ip'),
                 last_success=time.time(), subscription_updated=db_updated(),
                 degraded_reason=None, down_since=None, action=None, error=None)
    return relay


def recover(ui, relay, reason, reconnect_now, processes):
    """Stay degraded (relay closed) until the exact profile passes HTTPS again."""
    close_relay(relay)
    outage = Outage(reason, time.time(), last_refresh())
    write_status(healthy=False, phase='degraded', degraded_reason=reason,
                 down_since=outage.since, action=None)
    event('down', reason=reason)
    log('degraded: '+reason)
    action = 'reconnect' if reconnect_now else None
    adapter_errors = 0
    while True:
        if any(p.poll() is not None for p in processes):
            raise RuntimeError('Supervised process exited')
        requested = take_command()
        action = requested or action or outage.next_action(time.time())
        write_status(action=action, degraded_reason=outage.reason)
        try:
            if action == 'restart':
                raise RuntimeError('Upstream unavailable for 30 minutes; clean restart')
            if action == 'refresh':
                disconnect(ui)
                fetch_subscription(ui)
                outage.refreshed(time.time())
                title, trace = connect_selected(ui, attempts=2)
            elif action == 'reconnect':
                outage.reconnected(time.time())
                disconnect(ui)
                title, trace = connect_selected(ui, attempts=1)
            elif action == 'probe':
                ok, trace, google = health(10808, require_country=bool(COUNTRY))
                record_latency(trace, google)
                if not ok: raise UpstreamError('probe failed')
                title = STATUS.get('selected_profile') or QUERY
            else:
                raise UpstreamError('waiting for subscription refresh')
            break
        except ProfileMissing:
            adapter_errors = 0
            outage.reason = 'profile_missing'
        except UpstreamError:
            adapter_errors = 0
            if action in ('refresh', 'reconnect') and outage.reason == 'profile_missing':
                outage.reason = 'upstream'   # the profile exists again, only HTTPS is missing
        except (ProfileError, subprocess.SubprocessError) as exc:
            adapter_errors += 1
            log('adapter error while degraded: '+(str(exc) if isinstance(exc, ProfileError) else type(exc).__name__))
            if adapter_errors >= ADAPTER_ERRORS:
                raise RuntimeError('Happ interface is not responding; clean restart')
        action = None
        write_status(action=None, degraded_reason=outage.reason, failures=STATUS.get('failures', 0)+1)
        rotate_logs()
        pause(PROBE_SECONDS)
    relay = start_relay()
    duration = round(time.time()-outage.since)
    event('up', reason=outage.reason, duration=duration)
    log(f'recovered after {duration} s; local SOCKS relay ready')
    return mark_running(title, trace, relay)


def refresh_selected(ui, relay, processes):
    # Close the published relay before Happ can replace/remove a selected server.
    close_relay(relay)
    write_status(healthy=False, phase='refreshing')
    disconnect(ui)
    fetch_subscription(ui)
    # Cache remains useful after a fetch error, but the exact profile must exist.
    try:
        title, trace = connect_selected(ui)
    except ProfileMissing:
        return recover(ui, None, 'profile_missing', False, processes)
    except UpstreamError:
        return recover(ui, None, 'upstream', False, processes)
    return mark_running(title, trace, start_relay())


def main():
    os.umask(0o077)
    for p in [STATE, RUN, *(STATE/x for x in ('config', 'data', 'cache', 'logs'))]:
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
    (RUN/'dbus.address').write_text(os.environ['DBUS_SESSION_BUS_ADDRESS'])
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    previous = {}
    try: previous = json.loads((STATE/'status.json').read_text())
    except (OSError, ValueError): pass
    # Keep the latency history across container restarts.
    if isinstance(previous.get('latency_history'), list):
        STATUS['latency_history'] = previous['latency_history'][-LATENCY_SAMPLES:]
        STATUS.update({k: previous[k] for k in ('latency_ms', 'latency_at') if k in previous})
    write_status()
    configure()
    subprocess.run(['xauth', '-f', os.environ['XAUTHORITY'], 'add', ':97',
                    'MIT-MAGIC-COOKIE-1', secrets.token_hex(16)], check=True, capture_output=True)
    xvfb = spawn(['Xvfb', ':97', '-screen', '0', '1280x900x24', '-nolisten', 'tcp',
                  '-auth', os.environ['XAUTHORITY'], '-fbdir', str(RUN)], 'xvfb.log')
    time.sleep(1)
    env = os.environ.copy(); env['LD_LIBRARY_PATH'] = '/opt/happ/lib'
    argv = ['/opt/happ/bin/Happ', '--software']
    if not db_updated():
        key = Path('/run/secrets/subscription').read_text().strip()
        if not key.startswith(('happ://', 'https://')):
            raise RuntimeError('Unsupported subscription key')
        argv += ['--test-crypt5', key]
    app = spawn(argv, 'happ.private.log', env)
    log('waiting for subscription and GUI')
    ui = None
    deadline = time.monotonic()+90
    while time.monotonic() < deadline:
        if app.poll() is not None or xvfb.poll() is not None:
            raise RuntimeError('Happ or Xvfb exited during startup')
        if db_updated():
            try:
                ui = ProfilesUI()
                break
            except (RuntimeError, subprocess.SubprocessError):
                pass
        time.sleep(2)
    if ui is None:
        raise RuntimeError('Subscription/GUI startup timeout')
    time.sleep(3)
    if DISCOVER:
        catalog = ui.catalog()
        if not catalog: raise RuntimeError('Subscription contains no selectable profiles')
        write_catalog(catalog)
        write_status(phase='awaiting_selection', profile_count=len(catalog))
        while app.poll() is None and xvfb.poll() is None: time.sleep(1)
        raise RuntimeError('Discovery process exited unexpectedly')
    write_status(phase='connecting')
    event('start')
    processes = (app, xvfb)
    next_refresh = last_refresh() + UPDATE_SECONDS
    if not (STATE/'catalog.json').exists():
        try: write_catalog(ui.catalog())
        except (ProfileError, subprocess.SubprocessError): pass
    p = prefs()['Preferences']
    if p.get(r'AdvancedSettings\tun', '').lower() != 'false':
        raise RuntimeError('TUN must be disabled')
    try:
        title, a = connect_selected(ui)
        relay = mark_running(title, a, start_relay())
        event('up', reason='start', duration=round(time.time()-STATUS['started_at']))
        log('connected; local SOCKS relay ready')
    except ProfileMissing:
        relay = recover(ui, None, 'profile_missing', False, processes)
    except UpstreamError:
        relay = recover(ui, None, 'upstream', False, processes)
    failures = 0
    while True:
        if any(p.poll() is not None for p in (*processes, relay)):
            raise RuntimeError('Supervised process exited')
        requested = take_command()
        if requested == 'reconnect':
            relay = recover(ui, relay, 'manual', True, processes)
            failures = 0
            continue
        if requested == 'refresh' or time.time() >= next_refresh:
            next_refresh = time.time() + UPDATE_SECONDS
            relay = refresh_selected(ui, relay, processes)
            failures = 0
            continue
        ok, a, b = health(PORT)
        record_latency(a, b)
        if COUNTRY and a.get('exit_country') and a['exit_country'] != COUNTRY:
            relay = recover(ui, relay, 'country_mismatch', True, processes)
            failures = 0
            continue
        failures = 0 if ok else failures+1
        values = {'healthy': ok, 'failures': failures, 'subscription_updated': db_updated(),
                  'checks': {'cloudflare': a, 'google': b}}
        if ok:
            values['last_success'] = time.time()
            if a.get('exit_country'):
                values.update(exit_country=a['exit_country'], exit_ip=a.get('exit_ip'))
        write_status(**values)
        if failures >= int(CFG.get('failure_threshold', 3)):
            relay = recover(ui, relay, 'upstream', True, processes)
            failures = 0
            continue
        rotate_logs()
        # After a failed check confirm quickly instead of waiting a full interval.
        pause(int(CFG.get('health_interval', 20)) if ok else 5)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never include raw Happ logs, URLs, credentials, or subscription keys.
        kind = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        log(kind)
        event('failed', error=kind)
        write_status(healthy=False, phase='failed', error=kind)
        sys.exit(1)
