#!/usr/bin/env python3
"""Web interface and host operations against a temporary tree with fake systemctl/docker.
No real services, containers, subscriptions or network access."""
import http.client
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE/'lib'))
import ops  # noqa: E402
import web  # noqa: E402
from memory_budget import MemoryBudget  # noqa: E402
import http.server

OLD_KEY = 'happ://crypt5/OLD-EXAMPLE-KEY-NOT-REAL'
NEW_KEY = 'happ://crypt5/NEW-EXAMPLE-KEY-NOT-REAL'
PASSWORD = 'correct horse battery'

SHIM = r'''#!PYTHON
import json, os, sys, time, pathlib
root = pathlib.Path(os.environ['EXITPOOL_TEST_ROOT'])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root/'commands.jsonl').open('a') as f: f.write(json.dumps([name, *args])+'\n')
state = root/'units.json'
units = json.loads(state.read_text()) if state.exists() else {}
if name == 'systemctl':
    action = args[0]
    if action in ('enable', 'disable') and args[1:2] == ['--now']:
        action = 'start' if action == 'enable' else 'stop'
        args = [action, args[2]]
    if action == 'show':
        blocks = []
        for unit in [a for a in args if a.startswith(('exitpool-happ@', 'exitpool-awg'))]:
            active = units.get(unit, 'active')
            blocks.append(f"Id={unit}\nActiveState={active}\nSubState=running\nNRestarts=2\nUnitFileState=enabled\n"
                          f"MemoryCurrent={15*2**20}")
        print('\n\n'.join(blocks))
    elif action in ('start', 'restart', 'stop'):
        unit = args[1]; instance = unit.split('@', 1)[1][:-8]
        units[unit] = 'inactive' if action == 'stop' else 'active'
        if action != 'stop':
            var = root/'var/lib/exitpool'/instance
            subs = var/'config/Happ/subs.db'
            profile = root/'etc/exitpool/awg'/(instance+'.conf')
            broken = (subs.exists() and subs.read_bytes() == b'BROKEN') or \
                     (profile.exists() and 'BROKEN' in profile.read_text())
            cfg = json.loads((root/'etc/exitpool/instances'/(instance+'.json')).read_text())
            status = {'started_at': time.time(), 'updated_at': time.time(),
                      'phase': 'degraded' if broken else 'running', 'healthy': not broken,
                      'degraded_reason': ('upstream' if profile.exists() else 'profile_missing') if broken else None,
                      'exit_country': cfg.get('country') or 'XX'}
            if cfg.get('kind') == 'awg':
                status['kind'] = 'awg'
                status['tunnel'] = {'endpoint': '192.0.2.10:51820', 'mtu': 1280, 'handshake_age': 12, 'rx': 2048, 'tx': 1024}
            (var/'status.json').write_text(json.dumps(status))
        state.write_text(json.dumps(units))
elif name == 'journalctl':
    print('2026-09-26T00:00:00+0000 host docker[1]: 2026-09-26T00:00:00Z DE connected; local SOCKS relay ready')
elif name == 'docker':
    if args[:1] == ['stats']:
        print('exitpool-de\t44.2MiB / 384MiB\nexitpool-discovery-abc\t90MiB / 384MiB')
    elif args[:2] == ['network', 'inspect']:
        print('exitpool')
elif name == 'ip':
    print('[]')
elif name == 'nft':
    sys.exit(1 if args[:2] == ['list', 'table'] else 0)
'''.replace('PYTHON', sys.executable)
SHIM_NAMES = ('systemctl', 'journalctl', 'docker', 'ip', 'nft', 'python3')


class FakeXui:
    """Minimal 3x-ui 3.8 API: template, balancer status, observatory. Records every call."""
    def __init__(self, token='panel-token-example'):
        self.calls, self.token = [], token
        template = {'outbounds': [{'tag': 'direct'}, {'tag': 'happ-de'}, {'tag': 'happ-se'}],
                    'routing': {'balancers': [{'tag': 'doublehop', 'selector': ['happ-se', 'other']},
                                              {'tag': 'youtube', 'selector': ['happ-']},
                                              {'tag': 'unrelated', 'selector': ['warp']}]}}
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def reply(self, body):
                raw = json.dumps(body).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

            def handle_any(self, method):
                length = int(self.headers.get('Content-Length') or 0)
                form = self.rfile.read(length).decode()
                parent.calls.append((method, self.path, form))
                if self.headers.get('Authorization') != 'Bearer '+parent.token:
                    self.send_response(401); self.end_headers(); return
                if method == 'POST' and self.path == '/base/panel/api/xray/':
                    return self.reply({'success': True, 'obj': json.dumps({'xraySetting': template})})
                if method == 'POST' and self.path == '/base/panel/api/xray/balancerStatus':
                    tags = form.split('=', 1)[1].replace('%2C', ',').split(',') if form else []
                    states = {'doublehop': {'tag': 'doublehop', 'running': True, 'override': '', 'selected': ['happ-se']},
                              'youtube': {'tag': 'youtube', 'running': True, 'override': 'happ-de', 'selected': ['happ-de']}}
                    return self.reply({'success': True, 'obj': {t: states[t] for t in tags if t in states}})
                if method == 'GET' and self.path == '/base/panel/api/server/xrayObservatory':
                    return self.reply({'success': True, 'obj': [{'tag': 'happ-de', 'alive': True, 'delay': 288, 'updatedAt': 1},
                                                                {'tag': 'happ-se', 'alive': False, 'delay': 99999, 'updatedAt': 1}]})
                self.send_response(404); self.end_headers()

            def do_GET(self): self.handle_any('GET')
            def do_POST(self): self.handle_any('POST')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port

    def close(self):
        self.server.shutdown(); self.server.server_close()


class FakeDiscovery:
    def __init__(self, key, names, content=b'NEW'):
        self.subscription, self.names, self.content = key, names, content
        self.stopped = self.exited = False

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)/'state'
        (self.state/'config/Happ').mkdir(parents=True)
        (self.state/'config/Happ/subs.db').write_bytes(self.content)
        (self.state/'data').mkdir()
        (self.state/'catalog.json').write_text(json.dumps(self.names, ensure_ascii=False))
        (self.state/'logs').mkdir()
        (self.state/'logs/happ.private.log').write_text('PRIVATE')
        self.catalog = list(self.names)
        return self

    def stop(self):
        self.stopped = True

    def __exit__(self, *args):
        self.exited = True
        self.temp.cleanup()


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        shim = root/'bin'
        shim.mkdir()
        for name in SHIM_NAMES:
            (shim/name).write_text(SHIM)
            (shim/name).chmod(0o755)
        self.env = mock_env({'PATH': f'{shim}:{os.environ["PATH"]}', 'EXITPOOL_TEST_ROOT': str(root)})
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        ops.set_root(root)
        self.addCleanup(ops.set_root, '/')
        for path in (ops.ETC/'instances', ops.OPT, ops.VAR):
            path.mkdir(parents=True, mode=0o700)
        (ops.ETC/'subscription.secret').write_text(OLD_KEY+'\n')
        ops.write_instances({
            'de': {'country': 'DE', 'profile_query': 'Германия', 'port': 11808},
            'se': {'country': 'SE', 'profile_query': 'Швеция', 'port': 11818}})
        for name in ('de', 'se'):
            state = ops.VAR/name
            (state/'config/Happ').mkdir(parents=True)
            (state/'config/Happ/subs.db').write_bytes(b'OLD-'+name.encode())
            (state/'logs').mkdir()
            (state/'catalog.json').write_text(json.dumps(['Германия ⚡️', 'Швеция', 'Финляндия'], ensure_ascii=False))
            (state/'status.json').write_text(json.dumps({
                'phase': 'running', 'healthy': True, 'updated_at': time.time(), 'started_at': time.time()-600,
                'exit_country': name.upper(), 'exit_ip': '192.0.2.1', 'subscription_updated': '2026-09-25T23:02:48'}))
            now = time.time()
            events = [{'at': now-5000, 'event': 'start'}, {'at': now-4990, 'event': 'up', 'reason': 'start'},
                      {'at': now-3000, 'event': 'down', 'reason': 'upstream'}, {'at': now-2700, 'event': 'up', 'reason': 'upstream', 'duration': 300}]
            (state/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')

    def commands(self):
        path = self.root/'commands.jsonl'
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


class mock_env:
    def __init__(self, values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, *args):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class OpsTests(Base):
    def test_latency_summary(self):
        now = time.time()
        state = {'latency_ms': 120, 'latency_at': now-10,
                 'latency_history': [[now-4000, 900], [now-60, 100], [now-40, None], [now-20, 140], [now-10, 120]]}
        view = ops.latency_view(state, now)
        self.assertEqual((view['last'], view['median'], view['min'], view['max']), (120, 120, 100, 140))
        self.assertEqual((view['samples'], view['failed']), (4, 1))
        self.assertIsNone(ops.latency_view({}, now)['median'])

    def test_legacy_first_version_layout_is_read(self):
        (ops.OPT/'instances.json').unlink()
        cfg = ops.installed()
        self.assertEqual(cfg['de']['tag'], 'happ-de')
        self.assertEqual(cfg['se']['port'], 11818)

    def test_down_intervals_and_availability(self):
        view = ops.status()['instances'][0]
        self.assertEqual(view['day']['outages'], 1)
        self.assertAlmostEqual(view['day']['downtime'], 300, delta=2)
        self.assertEqual(view['phase'], 'running')
        self.assertTrue(view['healthy'])

    def test_open_outage_counts_until_now(self):
        now = time.time()
        intervals = ops.down_intervals([{'at': now-100, 'event': 'down'}], now, now-ops.DAY, up_now=False)
        self.assertEqual(len(intervals), 1)
        self.assertAlmostEqual(intervals[0][1]-intervals[0][0], 100, delta=1)

    def test_manual_stop_counts_as_downtime(self):
        ops.service('de', 'stop')
        events = ops.read_events('de')
        self.assertEqual((events[-1]['event'], events[-1]['by']), ('stop', 'stop'))
        now = time.time()
        intervals = ops.down_intervals(events, now+120, now-ops.DAY, up_now=False)
        self.assertAlmostEqual(intervals[-1][1]-intervals[-1][0], 120, delta=2)
        ops.service('de', 'stop')   # already stopped: no second event
        self.assertEqual(len(ops.read_events('de')), len(events))

    def test_stale_and_stopped_status(self):
        path = ops.VAR/'de'/'status.json'
        state = json.loads(path.read_text()); state['updated_at'] = time.time()-500
        path.write_text(json.dumps(state))
        self.assertEqual(ops.status()['instances'][0]['phase'], 'stale')
        ops.service('de', 'stop')
        self.assertEqual(ops.status()['instances'][0]['phase'], 'stopped')

    def test_settings_cannot_change_port_or_tag_and_profile_must_exist(self):
        for change in ({'port': 12000}, {'tag': 'other'}):
            with self.assertRaises(ops.OpsError):
                ops.save_instance('de', change)
        with self.assertRaises(ops.OpsError):
            ops.save_instance('de', {'profile_name': 'Нет такого'})
        saved = ops.save_instance('de', {'profile_name': 'Германия ⚡️', 'memory_mib': 512})
        self.assertEqual(saved['profile_name'], 'Германия ⚡️')
        self.assertNotIn('profile_query', saved)
        self.assertIn('MEMORY_LIMIT=512m', (ops.ETC/'instances/de.env').read_text())
        self.assertEqual(json.loads((ops.OPT/'3xui-outbounds.json').read_text())[0],
                         {'tag': 'happ-de', 'protocol': 'socks', 'settings': {'address': '127.0.0.1', 'port': 11808}})
        self.assertIn(['systemctl', 'restart', 'exitpool-happ@de.service'], self.commands())

    def test_proposal_matches_exact_then_first_word(self):
        cfg = ops.installed()
        cfg['de'] = {**cfg['de'], 'profile_name': 'Швеция'}
        cfg['de'].pop('profile_query')
        proposal = ops.propose(cfg, ['Германия', 'Швеция', 'Швеция 2', 'Швеция'])
        # The duplicated exact name is never proposed; the only unique same-word profile is.
        self.assertEqual(proposal['de']['profile_name'], 'Швеция 2')
        self.assertEqual(ops.propose(cfg, ['Швеция', 'Швеция'])['de']['profile_name'], '')
        proposal = ops.propose(ops.installed(), ['🇩🇪 Германия Premium', 'Швеция ⚡️', 'Финляндия'])
        self.assertEqual(proposal['de']['profile_name'], '🇩🇪 Германия Premium')
        self.assertEqual(proposal['se']['profile_name'], 'Швеция ⚡️')


class WebBase(Base):
    def setUp(self):
        super().setUp()
        web.FAILED_LOGIN_DELAY = 0
        config = web.validate_config({'listen': '127.0.0.1', 'port': 1338, 'allow': ['127.0.0.0/8'], 'hosts': [],
                                      'session_hours': 1, 'password': web.hash_password(PASSWORD, rounds=1000)})
        config['port'] = 0   # ephemeral port for the test server
        self.discoveries = []
        self.content = b'NEW'

        def factory(key):
            item = FakeDiscovery(key, ['Германия ⚡️', 'Швеция', 'Финляндия', 'Дубль', 'Дубль'], self.content)
            self.discoveries.append(item)
            return item
        ops.Jobs.EXPIRE_SECONDS = 60
        self.app = web.App(config, jobs=ops.Jobs(factory))
        self.server = web.make_server(self.app)
        self.port = self.server.server_port
        self.app.config['port'] = 1338   # what a saved web.json would contain
        self.app.hosts = {'127.0.0.1', f'127.0.0.1:{self.port}'}
        quiet = mock.patch.object(web.Handler, 'log_request', lambda *args, **kwargs: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cookie = self.csrf = None

    def call(self, method, path, body=None, headers=None, csrf=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': f'127.0.0.1:{self.port}'}
        if self.cookie:
            h['Cookie'] = 'exitpool_session='+self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h['Content-Type'] = 'application/json'
        if method == 'POST' and csrf and self.csrf:
            h['X-Exitpool-CSRF'] = self.csrf
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        response = conn.getresponse()
        raw = response.read()
        cookie = response.getheader('Set-Cookie')
        conn.close()
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        return response.status, value, cookie, response

    def login(self, password=PASSWORD):
        status, value, cookie, _ = self.call('POST', '/api/login', {'password': password})
        if status == 200:
            self.cookie = cookie.split(';')[0].split('=', 1)[1]
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=Strict', cookie)
            self.csrf = self.call('GET', '/api/session')[1]['csrf']
        return status

    def wait_job(self, phases=('done', 'failed', 'cancelled', 'awaiting_selection')):
        deadline = time.time()+15
        while time.time() < deadline:
            job = self.call('GET', '/api/job')[1]
            if job['phase'] in phases:
                return job
            time.sleep(.05)
        self.fail('job did not finish: '+json.dumps(job, ensure_ascii=False))

class WebTests(WebBase):
    def test_static_page_and_security_headers(self):
        status, body, _, response = self.call('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'/app.js', body)
        self.assertIn("frame-ancestors 'none'", response.getheader('Content-Security-Policy'))
        self.assertEqual(response.getheader('X-Frame-Options'), 'DENY')

    def test_subnet_and_host_are_enforced(self):
        self.app.networks = [__import__('ipaddress').ip_network('10.0.0.0/8')]
        self.assertEqual(self.call('GET', '/')[0], 403)
        self.app.networks = [__import__('ipaddress').ip_network('127.0.0.0/8')]
        self.assertEqual(self.call('GET', '/', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.call('GET', '/', headers={'Host': '127.0.0.1.evil.example:1338'})[0], 403)
        # A tunnel to the interface (any local port) keeps working; the subnet check still applies.
        for host in ('127.0.0.1:47093', 'localhost:8080', 'localhost'):
            self.assertEqual(self.call('GET', '/', headers={'Host': host})[0], 200, host)
        self.app.networks = [__import__('ipaddress').ip_network('10.0.0.0/8')]
        self.assertEqual(self.call('GET', '/', headers={'Host': '127.0.0.1:47093'})[0], 403)

    def test_login_required_rate_limited_and_csrf(self):
        self.assertEqual(self.call('GET', '/api/status')[0], 401)
        self.assertFalse(self.call('GET', '/api/session')[1]['authenticated'])
        for _ in range(web.LOGIN_LIMIT):
            self.assertEqual(self.login('wrong password'), 403)
        self.assertEqual(self.login(), 429)
        self.app.attempts = []
        self.assertEqual(self.login(), 200)
        self.assertEqual(self.call('POST', '/api/instances/de/action', {'action': 'refresh'}, csrf=False)[0], 403)
        status, _, _, _ = self.call('POST', '/api/instances/de/action', {'action': 'refresh'},
                                    headers={'Content-Type': 'text/plain'})
        self.assertEqual(status, 400)
        self.assertEqual(self.call('POST', '/api/instances/de/action', {'action': 'refresh'})[0], 200)
        self.assertEqual(json.loads((ops.VAR/'de/command.json').read_text())['action'], 'refresh')

    def test_rejected_post_body_does_not_poison_keep_alive(self):
        self.login()
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'Host': f'127.0.0.1:{self.port}', 'Cookie': 'exitpool_session='+self.cookie,
                   'Content-Type': 'application/json'}
        conn.request('POST', '/api/instances/de/action', body=b'{"action":"restart"}', headers=headers)
        first = conn.getresponse(); first.read()
        conn.request('GET', '/api/session', headers=headers)
        second = conn.getresponse()
        self.assertEqual((first.status, second.status), (403, 200))
        self.assertTrue(json.loads(second.read())['authenticated'])
        conn.close()

    def test_status_never_contains_the_key(self):
        self.login()
        status, value, _, response = self.call('GET', '/api/status')
        self.assertEqual(status, 200)
        text = json.dumps(value, ensure_ascii=False)
        self.assertNotIn('OLD-EXAMPLE', text)
        self.assertEqual(value['subscription']['kind'], 'happ://crypt5/…')
        self.assertEqual(len(value['subscription']['fingerprint']), 12)
        self.assertEqual([i['name'] for i in value['instances']], ['de', 'se'])
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')

    def test_probe_command(self):
        self.login()
        self.assertEqual(self.call('POST', '/api/instances/se/action', {'action': 'probe'})[0], 200)
        self.assertEqual(json.loads((ops.VAR/'se/command.json').read_text())['action'], 'probe')

    def test_sessions_survive_service_restart_but_not_password_change(self):
        self.login()
        restarted = web.App(self.app.config, jobs=self.app.jobs)
        self.assertIsNotNone(restarted.session(self.cookie))
        self.assertNotIn(self.cookie, (ops.RUNDIR/'exitpool-web-sessions.json').read_text())   # only hashes on disk
        self.assertEqual((ops.RUNDIR/'exitpool-web-sessions.json').stat().st_mode & 0o777, 0o600)
        restarted.set_password('another password')
        self.assertIsNone(web.App(self.app.config, jobs=self.app.jobs).session(self.cookie))

    def test_panel_settings_never_return_token_and_show_balancers(self):
        fake = FakeXui()
        self.addCleanup(fake.close)
        self.login()
        self.assertFalse(self.call('GET', '/api/panel')[1]['settings']['configured'])
        bad = {'host': '127.0.0.1', 'port': fake.port, 'base_path': 'base', 'token': 'wrong-token', 'enabled': True}
        status, value, _, _ = self.call('POST', '/api/panel', {'settings': bad})
        self.assertEqual(status, 409)
        self.assertFalse(ops.panel_path().exists())
        good = dict(bad, token='Bearer panel-token-example')
        status, value, _, _ = self.call('POST', '/api/panel', {'settings': good})
        self.assertEqual(status, 200, value)
        self.assertNotIn('panel-token-example', json.dumps(value))
        self.assertEqual(ops.panel_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(ops.panel_path().read_text())['token'], 'panel-token-example')
        self.assertEqual(json.loads(ops.panel_path().read_text())['base_path'], '/base')
        status = self.call('GET', '/api/status')[1]
        self.assertTrue(status['panel']['ok'], status['panel'])
        self.assertNotIn('panel-token-example', json.dumps(status))
        views = {i['name']: i['panel'] for i in status['instances']}
        self.assertEqual(views['se']['balancers'], [{'tag': 'doublehop', 'selected': True, 'override': '', 'running': True},
                                                    {'tag': 'youtube', 'selected': False, 'override': 'happ-de', 'running': True}])
        self.assertEqual([b['tag'] for b in views['de']['balancers']], ['youtube'])
        self.assertEqual(views['de']['observatory'], {'alive': True, 'delay': 288, 'updatedAt': 1})
        self.assertFalse(views['se']['observatory']['alive'])
        # Only read endpoints are used; the override endpoint is never called.
        self.assertFalse(any('balancerOverride' in path or 'update' in path for _, path, _ in fake.calls))
        self.assertIn(('POST', '/base/panel/api/xray/balancerStatus', 'tags=doublehop%2Cyoutube'), fake.calls)
        # Saving again with an empty token keeps the stored one.
        status, value, _, _ = self.call('POST', '/api/panel', {'settings': dict(good, token='', enabled=False)})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(ops.panel_path().read_text())['token'], 'panel-token-example')
        self.assertFalse(self.call('GET', '/api/status')[1]['panel']['enabled'])
        self.assertEqual(self.call('POST', '/api/panel/clear', {})[0], 200)
        self.assertFalse(ops.panel_path().exists())

    def test_panel_error_is_reported_not_raised(self):
        fake = FakeXui()
        self.addCleanup(fake.close)
        ops.write_file(ops.panel_path(), json.dumps({'enabled': True, 'scheme': 'http', 'host': '127.0.0.1',
                                                     'port': fake.port, 'base_path': '/base', 'token': 'stale'}), 0o600)
        self.app.panel.refresh()
        state = self.app.panel.get()
        self.assertFalse(state['ok'])
        self.assertIn('HTTP 401', state['error'])
        self.assertNotIn('stale', state['error'])

    def test_actions_and_log(self):
        self.login()
        self.assertEqual(self.call('POST', '/api/instances/de/action', {'action': 'restart'})[0], 200)
        self.assertIn(['systemctl', 'restart', 'exitpool-happ@de.service'], self.commands())
        self.assertEqual(self.call('POST', '/api/instances/de/action', {'action': 'rm -rf'})[0], 400)
        self.assertEqual(self.call('POST', '/api/instances/xx/action', {'action': 'restart'})[0], 409)
        self.assertIn('connected', self.call('GET', '/api/instances/de/log')[1]['lines'][0])

    def test_replace_subscription_rolls_out_and_backs_up(self):
        self.login()
        self.assertEqual(self.call('POST', '/api/subscription/discover', {'key': 'not a link'})[0], 409)
        self.assertEqual(self.call('POST', '/api/subscription/discover', {'key': NEW_KEY})[0], 200)
        job = self.wait_job()
        self.assertEqual(job['phase'], 'awaiting_selection')
        self.assertEqual(job['proposal']['de']['profile_name'], 'Германия ⚡️')
        self.assertEqual(job['proposal']['se']['profile_name'], 'Швеция')
        # Settings are locked while a replacement is pending.
        self.assertEqual(self.call('POST', '/api/instances/de/settings', {'settings': {'memory_mib': 512}})[0], 409)
        bad = {'de': {'profile_name': 'Дубль'}, 'se': {'profile_name': 'Швеция'}}
        self.assertEqual(self.call('POST', '/api/subscription/apply', {'mapping': bad})[0], 409)
        mapping = {'de': {'profile_name': 'Германия ⚡️', 'country': 'de'}, 'se': {'profile_name': 'Швеция', 'country': ''}}
        self.assertEqual(self.call('POST', '/api/subscription/apply', {'mapping': mapping})[0], 200)
        job = self.wait_job(('done', 'failed'))
        self.assertEqual(job['phase'], 'done', job)
        self.assertEqual((ops.ETC/'subscription.secret').read_text().strip(), NEW_KEY)
        self.assertEqual((ops.ETC/'subscription.secret').stat().st_mode & 0o777, 0o600)
        cfg = ops.installed()
        self.assertEqual(cfg['de']['profile_name'], 'Германия ⚡️')
        self.assertEqual(cfg['de']['country'], 'DE')
        self.assertEqual(cfg['se']['country'], '')
        self.assertEqual([cfg[n]['tag'] for n in cfg], ['happ-de', 'happ-se'])
        for name in ('de', 'se'):
            self.assertEqual((ops.VAR/name/'config/Happ/subs.db').read_bytes(), b'NEW')
            self.assertFalse((ops.VAR/name/'logs/happ.private.log').exists())
        self.assertTrue(self.discoveries[0].stopped and self.discoveries[0].exited)
        backup = ops.VAR/'backups'/job['backup_id']
        self.assertEqual((backup/'subscription.secret').read_text().strip(), OLD_KEY)
        self.assertEqual((backup/'state/de/config/Happ/subs.db').read_bytes(), b'OLD-de')
        order = [c[1:] for c in self.commands() if c[0] == 'systemctl' and c[1] in ('stop', 'start')]
        self.assertEqual(order, [['stop', 'exitpool-happ@de.service'], ['start', 'exitpool-happ@de.service'],
                                 ['stop', 'exitpool-happ@se.service'], ['start', 'exitpool-happ@se.service']])
        # Restoring the backup brings back the old key and imported state.
        self.assertEqual(self.call('POST', '/api/backups/restore', {'id': job['backup_id']})[0], 200)
        job = self.wait_job(('done', 'failed'))
        self.assertEqual(job['phase'], 'done', job)
        self.assertEqual((ops.ETC/'subscription.secret').read_text().strip(), OLD_KEY)
        self.assertEqual((ops.VAR/'se/config/Happ/subs.db').read_bytes(), b'OLD-se')
        self.assertEqual(ops.installed()['de']['profile_query'], 'Германия')
        self.assertEqual(len(self.call('GET', '/api/backups')[1]['backups']), 2)

    def test_failed_first_exit_rolls_back_and_keeps_others(self):
        self.login()
        self.content = b'BROKEN'
        self.call('POST', '/api/subscription/discover', {'key': NEW_KEY})
        self.wait_job()
        mapping = {'de': {'profile_name': 'Германия ⚡️'}, 'se': {'profile_name': 'Швеция'}}
        self.call('POST', '/api/subscription/apply', {'mapping': mapping})
        job = self.wait_job(('done', 'failed'))
        self.assertEqual(job['phase'], 'failed')
        self.assertIn('Прежняя подписка возвращена', job['error'])
        self.assertEqual((ops.ETC/'subscription.secret').read_text().strip(), OLD_KEY)
        self.assertEqual((ops.VAR/'de/config/Happ/subs.db').read_bytes(), b'OLD-de')
        self.assertEqual(ops.installed()['de'].get('profile_query'), 'Германия')
        self.assertFalse(any(c[1:] == ['stop', 'exitpool-happ@se.service'] for c in self.commands() if c[0] == 'systemctl'))

    def test_cancel_discovery(self):
        self.login()
        self.call('POST', '/api/subscription/discover', {'key': NEW_KEY})
        self.wait_job()
        self.assertEqual(self.call('POST', '/api/subscription/cancel', {})[0], 200)
        self.assertEqual(self.call('GET', '/api/job')[1]['phase'], 'cancelled')
        self.assertTrue(self.discoveries[0].exited)
        self.assertEqual((ops.ETC/'subscription.secret').read_text().strip(), OLD_KEY)

    def test_password_change_invalidates_sessions(self):
        self.login()
        self.assertEqual(self.call('POST', '/api/password', {'current': 'wrong', 'new': 'another password'})[0], 403)
        self.assertEqual(self.call('POST', '/api/password', {'current': PASSWORD, 'new': 'short'})[0], 400)
        self.assertEqual(self.call('POST', '/api/password', {'current': PASSWORD, 'new': 'another password'})[0], 200)
        self.assertEqual(self.call('GET', '/api/status')[0], 401)
        saved = json.loads((ops.ETC/'web.json').read_text())
        self.assertTrue(web.check_password('another password', saved['password']))
        self.assertNotIn('another password', (ops.ETC/'web.json').read_text())
        self.assertEqual((ops.ETC/'web.json').stat().st_mode & 0o777, 0o600)
        self.cookie = None
        self.assertEqual(self.login('another password'), 200)


def awg_text(marker=''):
    import base64
    k = lambda: base64.b64encode(os.urandom(32)).decode()
    return (f"[Interface]\nAddress = 100.91.8.40/32\nDNS = 100.64.0.1\nPrivateKey = {k()}\nJc = 6\nS3 = 766\n{marker}\n"
            f"[Peer]\nPublicKey = {k()}\nAllowedIPs = 0.0.0.0/0\nEndpoint = 192.0.2.10:51820\nPersistentKeepalive = 25-35\n")


class AwgWebTests(WebBase):
    def setUp(self):
        super().setUp()
        self.checks = []
        self.app.jobs.awg_check = lambda progress=None: self.checks.append(1)
        for name, value in (('tun_available', True), ('awg_binaries_ready', True),
                            ('read_memory', MemoryBudget(4096, 2048))):
            patch = mock.patch.object(ops, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.login()

    def add(self, name='fi2', text=None, **extra):
        return self.call('POST', '/api/awg', {'name': name, 'profile': text or awg_text(), **extra})

    def test_add_awg_exit_from_web(self):
        text = awg_text()
        private = text.split('PrivateKey = ')[1].split('\n')[0]
        status, value, _, _ = self.add(text=text, country='fi', label='FI.conf')
        self.assertEqual(status, 200, value)
        self.assertEqual(value['tag'], 'awg-fi2')
        self.assertTrue(value['port'] % 10 == 8 and value['port'] not in (11808, 11818))
        job = self.wait_job(('done', 'failed'))
        self.assertEqual(job['phase'], 'done', job)
        self.assertNotIn('warnings', job)
        profile = ops.AWG_ETC/'fi2.conf'
        self.assertEqual(profile.read_text(), text)
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        cfg = ops.installed()['fi2']
        self.assertEqual((cfg['kind'], cfg['country'], cfg['label'], cfg['memory_mib'], cfg['slot'], cfg['outbound']),
                         ('awg', 'FI', 'FI.conf', 64, 1, 'veth'))
        self.assertEqual(value['address'], '10.250.1.2')
        units = ops.UNIT_DIR
        self.assertEqual((units/'exitpool-awg@fi2.service.d/exitpool.conf').read_text(), '[Service]\nMemoryMax=128M\n')
        self.assertEqual((units/'exitpool-awg-relay@fi2.service.d/exitpool.conf').read_text(), '[Service]\nMemoryMax=96M\n')
        self.assertIn(f"ListenStream=127.0.0.1:{value['port']}", (units/'exitpool-awg-loopback@fi2.socket.d/exitpool.conf').read_text())
        self.assertEqual((ops.ETC/'instances/fi2.loopback.env').read_text(), f"TARGET=10.250.1.2:{value['port']}\n")
        self.assertFalse((ops.ETC/'instances/fi2.env').exists())
        commands = self.commands()
        self.assertIn(['systemctl', 'enable', '--now', 'exitpool-awg@fi2.service'], commands)
        self.assertLess(commands.index(['systemctl', 'daemon-reload']),
                        commands.index(['systemctl', 'enable', '--now', 'exitpool-awg@fi2.service']))
        self.assertEqual(self.checks, [1])
        status = self.call('GET', '/api/status')[1]
        item = next(i for i in status['instances'] if i['name'] == 'fi2')
        self.assertEqual((item['kind'], item['phase'], item['tunnel']['mtu']), ('awg', 'running', 1280))
        self.assertEqual((item['address'], item['memory_mode'], item['budget_mib']), (f"10.250.1.2:{value['port']}", 64, 100))
        self.assertTrue(status['features']['awg'])
        everything = json.dumps([status, job, self.call('GET', '/api/backups')[1]], ensure_ascii=False)
        self.assertNotIn(private, everything)
        outbounds = json.loads((ops.OPT/'3xui-outbounds.json').read_text())
        self.assertIn({'tag': 'awg-fi2', 'protocol': 'socks', 'settings': {'address': '10.250.1.2', 'port': value['port']}},
                      outbounds)

    def test_memory_modes_set_limits_and_loopback_entry(self):
        status, value, _, _ = self.add(name='fi3', memory_mib=32, outbound='loopback')
        self.assertEqual(status, 200, value)
        self.assertEqual((value['address'], value['memory_mib']), ('127.0.0.1', 32))
        self.assertEqual(self.wait_job(('done', 'failed'))['phase'], 'done')
        units = ops.UNIT_DIR
        self.assertIn('MemoryMax=128M', (units/'exitpool-awg@fi3.service.d/exitpool.conf').read_text())
        self.assertIn('MemoryMax=64M', (units/'exitpool-awg-relay@fi3.service.d/exitpool.conf').read_text())
        self.assertEqual(self.call('POST', '/api/instances/fi3/settings', {'settings': {'memory_mib': 128}})[0], 200)
        self.assertIn('MemoryMax=192M', (units/'exitpool-awg@fi3.service.d/exitpool.conf').read_text())
        self.assertIn('MemoryMax=160M', (units/'exitpool-awg-relay@fi3.service.d/exitpool.conf').read_text())
        self.assertEqual(self.call('POST', '/api/instances/fi3/settings', {'settings': {'memory_mib': 16}})[0], 409)
        self.assertEqual(self.add(name='fi4', memory_mib=8)[0], 409)
        status = self.call('GET', '/api/status')[1]
        item = next(i for i in status['instances'] if i['name'] == 'fi3')
        self.assertEqual((item['address'], item['memory_mode'], item['budget_mib']), (f"127.0.0.1:{value['port']}", 128, 160))

    def test_memory_check_refuses_exit_that_does_not_fit(self):
        with mock.patch.object(ops, 'read_memory', return_value=MemoryBudget(512, 90)):
            status, value, _, _ = self.add(name='fi5')
        self.assertEqual(status, 409)
        self.assertIn('Мало памяти', value['error'])
        self.assertNotIn('fi5', ops.installed())

    def test_duplicate_key_bad_profile_and_name_rejected(self):
        text = awg_text()
        self.add(text=text)
        self.wait_job(('done', 'failed'))
        status, value, _, _ = self.add(name='fi3', text=text)
        self.assertEqual(status, 409)
        self.assertIn('один ключ', value['error'])
        status, value, _, _ = self.add(name='fi3', text=awg_text().replace('Endpoint = 192.0.2.10:51820', 'Endpoint = nope'))
        self.assertEqual(status, 409)
        self.assertIn('Endpoint', value['error'])
        self.assertEqual(self.add(name='de')[0], 409)           # Happ exit with that name exists
        self.assertEqual(self.add(name='Bad Name')[0], 409)
        self.assertFalse((ops.AWG_ETC/'fi3.conf').exists())
        # An explicit port that is taken is refused before any job starts, never replaced.
        import socket
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            taken = busy.getsockname()[1]
            status, value, _, _ = self.add(name='fi4', port=taken)
        self.assertEqual(status, 409)
        self.assertIn(str(taken), value['error'])
        self.assertEqual(self.add(name='fi4', port=11808)[0], 409)   # used by the Happ exit de
        self.assertNotIn('fi4', ops.installed())

    def test_replace_profile_rolls_back_when_new_one_fails(self):
        first = awg_text()
        self.add(text=first)
        self.wait_job(('done', 'failed'))
        self.assertEqual(self.call('POST', '/api/instances/fi2/profile', {'profile': awg_text('# BROKEN')})[0], 200)
        job = self.wait_job(('done', 'failed'))
        self.assertEqual(job['phase'], 'failed')
        self.assertIn('прежний возвращён', job['error'])
        self.assertEqual((ops.AWG_ETC/'fi2.conf').read_text(), first)
        backup = ops.VAR/'backups'/job['backup_id']
        self.assertEqual((backup/'awg/fi2.conf').read_text(), first)
        good = awg_text()
        self.call('POST', '/api/instances/fi2/profile', {'profile': good})
        self.assertEqual(self.wait_job(('done', 'failed'))['phase'], 'done')
        self.assertEqual((ops.AWG_ETC/'fi2.conf').read_text(), good)

    def test_awg_settings_commands_and_removal(self):
        self.add()
        self.wait_job(('done', 'failed'))
        self.assertEqual(self.call('POST', '/api/instances/fi2/action', {'action': 'refresh'})[0], 409)
        self.assertEqual(self.call('POST', '/api/instances/fi2/action', {'action': 'reconnect'})[0], 200)
        status, value, _, _ = self.call('POST', '/api/instances/fi2/settings', {'settings': {'mtu': 1380, 'memory_mib': 96}})
        self.assertEqual(status, 200, value)
        self.assertEqual(ops.installed()['fi2']['mtu'], 1380)
        self.assertEqual(self.call('POST', '/api/instances/fi2/settings', {'settings': {'update_hours': 3}})[0], 409)
        self.assertEqual(self.call('POST', '/api/instances/fi2/settings', {'settings': {'mtu': None}})[0], 200)
        self.assertNotIn('mtu', ops.installed()['fi2'])
        self.assertEqual(self.call('POST', '/api/instances/de/remove', {})[0], 409)   # Happ exits stay
        status, value, _, _ = self.call('POST', '/api/instances/fi2/remove', {})
        self.assertEqual((status, value['tag']), (200, 'awg-fi2'))
        self.assertNotIn('fi2', ops.installed())
        self.assertFalse((ops.AWG_ETC/'fi2.conf').exists() or (ops.VAR/'fi2').exists())
        self.assertFalse((ops.UNIT_DIR/'exitpool-awg@fi2.service.d').exists())
        self.assertFalse((ops.ETC/'instances/fi2.loopback.env').exists())
        commands = self.commands()
        self.assertIn(['systemctl', 'disable', '--now', 'exitpool-awg@fi2.service'], commands)
        purge = next(c for c in commands if c[0] == 'python3')
        self.assertEqual((Path(purge[1]).name, purge[2:]), ('awgnet.py', ['purge', 'fi2']))
        self.assertLess(commands.index(purge), len(commands))
        self.assertTrue((ops.VAR/'backups'/value['backup_id']/'awg/fi2.conf').exists())

    def test_subscription_replacement_leaves_awg_exits_alone(self):
        self.add()
        self.wait_job(('done', 'failed'))
        before = len(self.commands())
        self.call('POST', '/api/subscription/discover', {'key': NEW_KEY})
        job = self.wait_job()
        self.assertEqual(set(job['proposal']), {'de', 'se'})
        mapping = {'de': {'profile_name': 'Германия ⚡️'}, 'se': {'profile_name': 'Швеция'}}
        self.assertEqual(self.call('POST', '/api/subscription/apply', {'mapping': mapping})[0], 200)
        self.assertEqual(self.wait_job(('done', 'failed'))['phase'], 'done')
        touched = [c for c in self.commands()[before:] if c[0] == 'systemctl' and c[1] != 'show'
                   and any('fi2' in a for a in c)]
        self.assertEqual(touched, [])
        self.assertEqual(ops.installed()['fi2']['kind'], 'awg')


class PasswordChangeTests(Base):
    def test_cli_password_change_ends_saved_sessions(self):
        web.save_config({'listen': '127.0.0.1', 'port': 1338, 'allow': ['127.0.0.0/8'], 'hosts': [],
                         'session_hours': 1, 'password': web.hash_password(PASSWORD, rounds=1000)})
        app = web.App(web.load_config())
        token = app.new_session()
        sessions = web.App.sessions_path()
        self.assertTrue(sessions.exists())
        self.assertFalse(web.change_password('new password 123'))   # the service is not running in the test
        self.assertFalse(sessions.exists())
        self.assertTrue(web.check_password('new password 123', web.load_config()['password']))
        restarted = web.App(web.load_config())
        self.assertEqual(restarted.sessions, {})
        self.assertIsNone(restarted.session(token))
        self.assertIn(['systemctl', 'is-active', 'exitpool-web.service'], self.commands())


class PasswordTests(unittest.TestCase):
    def test_hash_roundtrip_and_rejects_garbage(self):
        stored = web.hash_password('секретный пароль', rounds=1000)
        self.assertTrue(web.check_password('секретный пароль', stored))
        self.assertFalse(web.check_password('другой', stored))
        self.assertFalse(web.check_password('x', 'garbage'))
        with self.assertRaises(ValueError):
            web.validate_password('short')

    def test_config_validation(self):
        good = {'listen': '192.168.1.10', 'port': 1338, 'allow': ['192.168.1.0/24'], 'password': ''}
        self.assertEqual(web.validate_config(good)['allow'], ['192.168.1.0/24'])
        for bad in ({**good, 'listen': 'example.com'}, {**good, 'port': 0}, {**good, 'allow': []},
                    {**good, 'allow': ['not a net']}, {**good, 'hosts': ['a b']}):
            with self.assertRaises(ValueError):
                web.validate_config(bad)


if __name__ == '__main__':
    unittest.main()
