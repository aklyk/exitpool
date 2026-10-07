#!/usr/bin/env python3
"""Local tests: fake panel, wizard steps, installer and migration in a temporary root with fake systemctl/docker.
No router, real panel, root rights or system changes."""
import contextlib
import copy
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE/'lib'))
import configuration as config  # noqa: E402
from memory_budget import MemoryBudget, read_memory, parse_docker_mib  # noqa: E402
import discovery  # noqa: E402
import panel_outbounds as panel  # noqa: E402
import paths  # noqa: E402
import ops  # noqa: E402
import fetch  # noqa: E402
import installer  # noqa: E402
import setup as wizard  # noqa: E402


def instances():
    return config.validate_instances({'de': {'country':'DE','profile_query':'Germany','port':12808},
                                      'fi': {'country':'FI','profile_query':'Finland','port':12828}})


class ConfigTests(unittest.TestCase):
    def test_subset_custom_ports_and_tags(self):
        cfg = instances(); cfg['de']['tag'] = 'friend-de'
        self.assertEqual([o['settings']['port'] for o in config.make_outbounds(cfg)], [12808, 12828])
        self.assertEqual(config.make_outbounds(cfg)[0]['tag'], 'friend-de')

    def test_path_command_and_env_injection_rejected(self):
        for name in ('../escape', 'de;touch_x', '-bad', 'de\nPORT=22', 'de/fi'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                config.validate_instances({name: instances()['de']})
        cfg=instances();cfg['de']['port']='11808\nMEMORY_LIMIT=99g'
        with self.assertRaises(ValueError):config.validate_instances(cfg)

    def test_duplicates_reserved_ports_and_accidental_secrets_rejected(self):
        for port in (10808,10809,12828):
            cfg=instances();cfg['de']['port']=port
            with self.assertRaises(ValueError):config.validate_instances(cfg)
        cfg=instances();cfg['de']['api_token']='example-token'
        with self.assertRaises(ValueError):config.validate_instances(cfg)

    def test_occupied_port_caught(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            cfg={'de':{**instances()['de'],'port':sock.getsockname()[1]}}
            with self.assertRaises(ValueError):config.check_ports(cfg)

    def test_subscription_error_does_not_echo_value(self):
        secret='not-a-link-SENSITIVE'
        with self.assertRaises(ValueError) as caught:config.validate_subscription(secret)
        self.assertNotIn(secret,str(caught.exception))


class FakePanel:
    def __init__(self):
        self.old={'outbounds':[{'tag':'direct','protocol':'freedom'},{'tag':'warp','protocol':'socks','settings':{'port':64900}}],
                  'routing':{'rules':[{'inboundTag':['home'],'balancerTag':'old'}],
                             'balancers':[{'tag':'old','selector':['warp']}]},'dns':{'servers':['localhost']}}
        self.template={'xraySetting':copy.deepcopy(self.old),'outboundTestUrl':'https://example.org/test'}
        self.updates=0;self.reads=0;self.tests=[];self.redirect=False;self.fail_test=False;self.race=False;self.mismatch=False
        parent=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                from urllib.parse import parse_qs
                if self.headers.get('Authorization')!='Bearer example-token':
                    self.send_response(401);self.end_headers();return
                if parent.redirect:
                    self.send_response(302);self.send_header('Location','/stolen-token');self.end_headers();return
                data=parse_qs(self.rfile.read(int(self.headers.get('Content-Length',0))).decode())
                if self.path=='/base/panel/api/xray/':
                    parent.reads+=1
                    if parent.race and parent.reads==2:
                        parent.template['xraySetting']['routing']['rules'].append({'outboundTag':'direct'})
                    body={'success':True,'obj':json.dumps(parent.template)}
                elif self.path=='/base/panel/api/xray/update':
                    parent.updates+=1
                    parent.template['xraySetting']=json.loads(data['xraySetting'][0])
                    parent.template['outboundTestUrl']=data['outboundTestUrl'][0]
                    if parent.mismatch:parent.template['xraySetting']['outbounds'].pop()
                    body={'success':True}
                elif self.path=='/base/panel/api/xray/testOutbound':
                    parent.tests.append(json.loads(data['outbound'][0]))
                    body={'success':True,'obj':{'success':not parent.fail_test,'delay':10,'httpStatus':204}}
                else:
                    self.send_response(404);self.end_headers();return
                raw=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/base/panel/outbound'
        self.client=panel.PanelClient(self.url,'example-token')
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.fake=FakePanel();self.addCleanup(self.fake.close)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.backup=Path(self.temp.name)/'private'
        self.additions=config.make_outbounds(instances())

    def test_custom_outbounds_preserve_routing_and_default(self):
        tags=self.fake.client.add(self.additions,self.backup)
        self.assertEqual(tags,['happ-de','happ-fi'])
        new=self.fake.template['xraySetting'];old=self.fake.old
        self.assertEqual(new['outbounds'][:len(old['outbounds'])],old['outbounds'])
        self.assertEqual(new['routing'],old['routing']);self.assertEqual(new['dns'],old['dns'])
        self.assertEqual(self.fake.template['outboundTestUrl'],'https://example.org/test')
        for path in self.backup.iterdir():self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(self.fake.client.add(self.additions,self.backup),[])
        self.assertEqual(self.fake.updates,1)

    def test_conflict_refused_without_write(self):
        self.fake.template['xraySetting']['outbounds'].append({'tag':'happ-de','protocol':'blackhole'})
        with self.assertRaises(panel.PanelError):self.fake.client.add(self.additions,self.backup)
        self.assertEqual(self.fake.updates,0)

    def test_parallel_edit_refused(self):
        self.fake.race=True
        with self.assertRaises(panel.PanelError):self.fake.client.add(self.additions,self.backup)
        self.assertEqual(self.fake.updates,0)

    def test_readback_mismatch_detected_and_both_backups_exist(self):
        self.fake.mismatch=True
        with self.assertRaises(panel.PanelError):self.fake.client.add(self.additions,self.backup)
        self.assertEqual(len(list(self.backup.glob('*.json'))),2)

    def test_failed_inner_test_does_not_pass_as_success(self):
        self.fake.fail_test=True
        with self.assertRaises(panel.PanelError):self.fake.client.test(self.additions)
        self.assertEqual(self.fake.updates,0)

    def test_redirect_does_not_forward_token(self):
        self.fake.redirect=True
        with self.assertRaises(panel.PanelError) as caught:self.fake.client.read()
        self.assertNotIn('example-token',str(caught.exception))
        self.assertEqual(self.fake.reads,0)

    def test_bad_auth_is_sanitized(self):
        client=panel.PanelClient(self.fake.url,'SENSITIVE-INVALID-TOKEN')
        with self.assertRaises(panel.PanelError) as caught:client.read()
        self.assertNotIn('SENSITIVE',str(caught.exception))


def answers(*values):
    items = iter(values)
    return mock.patch('builtins.input', side_effect=lambda _='': next(items))


class WizardTests(unittest.TestCase):
    def setUp(self):
        free = mock.patch.object(ops, 'port_free', return_value=True)
        free.start()
        self.addCleanup(free.stop)

    def test_happ_selection_with_spaces_custom_ports_and_prefix(self):
        with answers('1 3', '21808', '21818', '12', 'friend', 'нет'), contextlib.redirect_stdout(io.StringIO()):
            cfg = wizard.choose_happ(['Germany', 'Sweden', 'Finland'], None, set(), set())
        self.assertEqual(list(cfg), ['p1', 'p3'])
        self.assertEqual((cfg['p1']['port'], cfg['p3']['port']), (21808, 21818))
        self.assertEqual(cfg['p3']['tag'], 'friend-p3')
        self.assertEqual((cfg['p3']['profile_name'], cfg['p3']['country'], cfg['p3']['update_hours']), ('Finland', '', 12))

    def test_happ_ports_skip_ports_and_tags_already_taken(self):
        with answers('1', '', '6', 'awg', 'happ', 'нет'), contextlib.redirect_stdout(io.StringIO()):
            cfg = wizard.choose_happ(['Germany'], None, {11808}, {'awg-p1'})
        self.assertEqual((cfg['p1']['port'], cfg['p1']['tag']), (11818, 'happ-p1'))

    def test_happ_duplicates_and_low_memory(self):
        with answers('1', '3', '', '6', 'happ', 'нет'), contextlib.redirect_stdout(io.StringIO()):
            cfg = wizard.choose_happ(['Same', 'Same', 'Japan ⚡️'], None, set(), set())
        self.assertEqual((list(cfg), cfg['p3']['profile_name']), (['p3'], 'Japan ⚡️'))
        with self.assertRaises(wizard.SetupError):
            wizard.choose_happ(['Same', 'Same'], None, set(), set())
        out = io.StringIO()
        with answers('1 2 3', '2', '', '6', 'happ', 'нет'), contextlib.redirect_stdout(out):
            cfg = wizard.choose_happ(['A', 'B', 'C'], lambda: MemoryBudget(1024, 550, 100), set(), set())
        self.assertEqual(list(cfg), ['p2'])
        self.assertIn('не хватает', out.getvalue())

    def test_memory_menu_order_default_and_custom(self):
        import cli
        out = io.StringIO()
        with answers(''), contextlib.redirect_stdout(out):
            self.assertEqual(cli.choose_memory('fi'), 64)
        text = out.getvalue()
        self.assertLess(text.index('32 МиБ'), text.index('64 МиБ'))
        self.assertLess(text.index('64 МиБ'), text.index('128 МиБ'))
        self.assertIn('64 МиБ: рекомендуется', text)
        self.assertIn('(по умолчанию)', text.split('64 МиБ')[1].split('\n')[0])
        self.assertIn('4 — своё значение\n', text)   # no description for the custom value
        for choice, expected in (('1', 32), ('3', 128)):
            with answers(choice), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.choose_memory('fi'), expected)
        with answers('4', '16', '200'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.choose_memory('fi'), 200)
        with answers('', ''), contextlib.redirect_stdout(io.StringIO()):   # current custom value is the default
            self.assertEqual(cli.choose_memory('fi', 48), 48)

    def test_web_configured_but_stopped_is_not_called_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            ops.set_root(d)
            self.addCleanup(ops.set_root, '/')
            paths.ETC.mkdir(parents=True)
            (paths.ETC/'web.json').write_text('{}')
            facts = {'available': 2000, 'docker': 'ready', 'awg_ok': True}
            for state, label, hint in (('inactive', 'настроен, служба выключена', 'sudo exitpool web enable'),
                                       ('active', 'уже включён', 'Веб уже включён')):
                out = io.StringIO()
                with mock.patch.object(wizard, 'run', return_value=mock.Mock(stdout=state+'\n')), \
                        answers('3', ''), contextlib.redirect_stdout(out):
                    parts = wizard.choose_parts(facts, {})
                self.assertIn(label, out.getvalue())
                self.assertIn(hint, out.getvalue())
                self.assertFalse(parts['web'])   # never re-asked: the saved address and password stay

    def test_budget_lines(self):
        facts = {'available': 300, 'docker': 'ready'}
        parts = {'awg': True, 'happ': False, 'web': False}
        self.assertEqual(wizard.budget_lines(facts, parts, [64], 1)[0]['status'], 'PASS')
        self.assertEqual(wizard.budget_lines({**facts, 'available': 150}, parts, [64], 1)[0]['status'], 'WARN')
        plan, lines = wizard.budget_lines({**facts, 'available': 300, 'docker': 'missing'},
                                          {'awg': True, 'happ': True, 'web': True}, [64], 1)
        self.assertEqual(plan['status'], 'FAIL')
        self.assertTrue(any('Docker' in line for line in lines))
        self.assertIn('не хватает', lines[-1])

    def test_awg_exit_questions(self):
        with tempfile.TemporaryDirectory() as d:
            profile = Path(d)/'de.conf'
            import base64
            key = lambda: base64.b64encode(os.urandom(32)).decode()
            profile.write_text(f'[Interface]\nAddress = 10.8.1.2/32\nPrivateKey = {key()}\nJc = 4\n'
                               f'[Peer]\nPublicKey = {key()}\nAllowedIPs = 0.0.0.0/0\nEndpoint = 192.0.2.10:51820\n')
            out = io.StringIO()
            with answers('да', 'de', str(profile), '', 'de', '', 'да', 'de2', str(profile), str(Path(d)/'missing'),
                         '/dev/null'), contextlib.redirect_stdout(out):
                with self.assertRaises(StopIteration):   # the duplicate key and broken files are refused, input runs out
                    wizard.ask_awg_exits(set(), set(), {})
            text = out.getvalue()
            self.assertIn('Профиль принят: сервер 192.0.2.10:51820', text)
            self.assertIn('один ключ — один туннель', text)
            self.assertIn('Не удалось прочитать файл', text)
            self.assertNotIn(profile.read_text().split('PrivateKey = ')[1].split('\n')[0], text)
            with answers('да', 'de', str(profile), '', 'fi', '1', 'нет'), contextlib.redirect_stdout(io.StringIO()):
                exits = wizard.ask_awg_exits(set(), set(), {})
        self.assertEqual([(e['name'], e['tag'], e['country'], e['memory_mib']) for e in exits], [('de', 'awg-de', 'FI', 32)])

    def test_binaries_failure_shows_real_error_and_offers_choices(self):
        args = argparse_namespace()
        error = fetch.FetchError('amneziawg-go не скачался с github.com: curl: (28) Connection timed out after 20001 ms')
        out = io.StringIO()
        with mock.patch.object(ops, 'awg_binaries_ready', return_value=False), \
                mock.patch.object(fetch, 'download_files', side_effect=error), \
                answers('3'), contextlib.redirect_stdout(out):
            self.assertEqual(wizard.acquire_binaries(args, '/tmp/x', None), ('build', None, True, None))
        self.assertIn('Connection timed out after 20001 ms', out.getvalue())
        self.assertIn('через прокси', out.getvalue())
        proxy = fetch.parse_proxy('socks5h://127.0.0.1:1080')
        calls = []
        with mock.patch.object(ops, 'awg_binaries_ready', return_value=False), \
                mock.patch.object(fetch, 'download_files', side_effect=lambda *a, **k: calls.append(a) or (
                    (_ for _ in ()).throw(error) if len(calls) == 1 else None)), \
                mock.patch.object(wizard, 'ask_proxy', return_value=proxy), \
                answers('2'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wizard.acquire_binaries(args, '/tmp/x', None), ('dir', '/tmp/x', False, proxy))
        self.assertEqual(calls[1][1], proxy)
        with answers('0'), mock.patch.object(ops, 'awg_binaries_ready', return_value=False), \
                mock.patch.object(fetch, 'download_files', side_effect=error), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                wizard.acquire_binaries(args, '/tmp/x', None)

    def test_summary_never_shows_secrets(self):
        plan = {'fresh': True, 'binaries': ('dir', '/tmp/x', False),
                'awg': [{'name': 'de', 'endpoint': '192.0.2.10:51820', 'tag': 'awg-de', 'country': '', 'memory_mib': 64,
                         'profile': 'PrivateKey = SENSITIVE-AWG-KEY'}],
                'happ': {'p1': {'profile_name': 'DE', 'country': '', 'port': 11808, 'tag': 'happ-p1'}},
                'subscription': 'happ://crypt5/SENSITIVE-SUB',
                'web': {'listen': '192.168.1.10', 'port': 1338, 'allow': ['192.168.1.0/24'], 'password': 'pbkdf2_sha256$SENSITIVE'},
                'panel': {'mode': 3, 'url': 'http://127.0.0.1:2053/private-base', 'token': 'SENSITIVE-TOKEN'},
                'proxy': fetch.parse_proxy('http://u:SENSITIVE-PASS@h:3128'), 'save_proxy': True}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wizard.summarize(plan)
        text = out.getvalue()
        for secret in ('SENSITIVE', 'private-base'):
            self.assertNotIn(secret, text)
        self.assertIn('режим памяти 64 МиБ (ожидаемо ~100 МиБ; жёсткие лимиты 128/96 МиБ)', text)

    def test_apply_uses_private_files_and_env_for_secrets(self):
        proxy = fetch.parse_proxy('http://u:SENSITIVE-PASS@h:3128')
        plan = {'fresh': True, 'binaries': ('dir', '/tmp/bins', True), 'proxy': proxy,
                'web': {'listen': '127.0.0.1', 'port': 1338, 'allow': ['127.0.0.0/8'], 'hosts': [],
                        'session_hours': 72, 'password': 'pbkdf2_sha256$1$x$y'},
                'happ': None, 'awg': [{'name': 'de', 'profile': 'PROFILE', 'tag': 'awg-de', 'country': '', 'memory_mib': 32}]}
        observed = []

        def runner(argv, **kwargs):
            observed.append(argv)
            config_file = Path(argv[argv.index('--web-config')+1])
            self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(config_file.read_text())['listen'], '127.0.0.1')
            self.assertNotIn('SENSITIVE', ' '.join(argv))
            self.assertIn('SENSITIVE-PASS', kwargs['env']['EXITPOOL_PROXY'])
            return mock.Mock(returncode=0)
        jobs = mock.Mock()
        jobs.return_value.awg_add_sync.return_value = {'phase': 'done'}
        with mock.patch.object(wizard.subprocess, 'run', side_effect=runner), mock.patch.object(ops, 'Jobs', jobs), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wizard.apply(plan), [])
        argv = observed[0]
        self.assertEqual(argv[2:5], ['base', '--binaries', 'dir'])
        self.assertIn('--binaries-local-build', argv)
        self.assertFalse(Path(argv[argv.index('--web-config')+1]).exists())
        data = jobs.return_value.awg_add_sync.call_args.args[0]
        self.assertEqual(data, {'name': 'de', 'profile': 'PROFILE', 'tag': 'awg-de', 'country': '', 'memory_mib': 32})

    def test_url_from_browser_and_embedded_credentials(self):
        self.assertEqual(panel.normalize_url('http://localhost:1337/secret/panel/outbound'), 'http://localhost:1337/secret')
        for value in ('file:///etc/passwd', 'http://user:password@example.com', 'http://localhost:99999'):
            with self.assertRaises(ValueError):
                panel.normalize_url(value)
        self.assertEqual(wizard.panel_settings('https://p.example/base', 'T'),
                         {'host': 'p.example', 'port': 443, 'base_path': '/base', 'https': True, 'token': 'T'})


def argparse_namespace(**values):
    import argparse
    defaults = {'binaries_dir': None, 'build_binaries': False, 'plan': False, 'force_budget': False}
    return argparse.Namespace(**{**defaults, **values})


SHIM = r"""#!PYTHON
import json, os, sys, time, pathlib
root = pathlib.Path(os.environ['EXITPOOL_TEST_ROOT'])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root/'commands.jsonl').open('a') as f: f.write(json.dumps([name, *args])+'\n')
if name == 'systemctl':
    path = root/'units.json'
    units = json.loads(path.read_text()) if path.exists() else {}
    if args[:2] in (['enable', '--now'], ['disable', '--now']) or args[:1] in (['stop'], ['start'], ['restart']):
        unit = args[-1]
        units[unit] = 'active' if args[0] in ('enable', 'start', 'restart') else 'inactive'
        path.write_text(json.dumps(units))
    if args[:2] == ['enable', '--now'] and '@' in args[2]:
        unit = args[2]; instance = unit.split('@', 1)[1].rsplit('.', 1)[0]
        broken = (root/'broken').exists() and (root/'broken').read_text().strip() == instance
        if unit.startswith('exitpool-'):
            cfg = json.loads((root/'etc/exitpool/instances'/(instance+'.json')).read_text())
            var = root/'var/lib/exitpool'/instance
        else:   # happ-service 1.x units after a rollback
            cfg, broken, var = {}, False, root/'var/lib/happ-service'/instance
        state = {'started_at': time.time(), 'updated_at': time.time(), 'phase': 'degraded' if broken else 'running',
                 'healthy': not broken}
        if cfg.get('kind') == 'awg':
            state['kind'] = 'awg'
        var.mkdir(parents=True, exist_ok=True)
        (var/'status.json').write_text(json.dumps(state))
    elif args[:1] == ['show']:
        print('\n\n'.join(f"Id={u}\nActiveState={units.get(u, 'inactive')}" for u in args if '@' in u))
    elif args[:1] == ['is-active']:
        print(units.get(args[1], 'inactive'))
elif name == 'docker':
    if args[:2] == ['network', 'inspect']:
        print('exitpool')
elif name == 'ip':
    print('[]')
elif name == 'nft':
    sys.exit(1 if args[:2] == ['list', 'table'] else 0)
""".replace('PYTHON', sys.executable)


class InstallerTests(unittest.TestCase):
    """installer.py against a temporary root: base install, purge, migration from happ-service with rollback."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = root = Path(self.temp.name)
        shim = root/'shim'
        shim.mkdir()
        for name in ('systemctl', 'docker', 'ip', 'nft', 'python3', 'journalctl'):
            (shim/name).write_text(SHIM)
            (shim/name).chmod(0o755)
        env = mock.patch.dict(os.environ, {'PATH': f'{shim}:{os.environ["PATH"]}', 'EXITPOOL_TEST_ROOT': str(root)})
        env.start()
        self.addCleanup(env.stop)
        paths.set_root(root)
        ops.set_root(root)
        self.addCleanup(ops.set_root, '/')
        self.bins = root/'bins'
        self.bins.mkdir()
        for name in paths.AWG_BINARIES:
            (self.bins/name).write_bytes(b'local build of '+name.encode())
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def commands(self):
        path = self.root/'commands.jsonl'
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def test_base_install_then_purge(self):
        installer.base(None, 'dir', self.bins, None, local_build=True)
        self.assertTrue((paths.LIB/'cli.py').exists())
        self.assertEqual((paths.LIB/'cli.py').stat().st_mode & 0o777, 0o755)
        self.assertEqual(json.loads((paths.OPT/'instances.json').read_text()), {})
        self.assertEqual((paths.BIN/'awg').read_bytes(), b'local build of awg')
        self.assertTrue((paths.UNIT_DIR/'exitpool-awg@.service').exists())
        wrapper = installer.wrapper_path()
        self.assertIn('/opt/exitpool/lib/cli.py', wrapper.read_text())
        self.assertTrue((paths.OPT/'uninstall.sh').exists())
        self.assertEqual(paths.ETC.stat().st_mode & 0o777, 0o700)
        commands = self.commands()
        self.assertIn(['systemctl', 'daemon-reload'], commands)
        self.assertIn(['systemctl', 'enable', 'exitpool-awg-guard.service'], commands)
        with self.assertRaises(installer.InstallError):
            installer.base(None, 'skip')
        with self.assertRaises(fetch.FetchError):   # a release install refuses unknown binaries
            installer.binaries('dir', self.bins)
        installer.purge()
        self.assertFalse(paths.OPT.exists() or wrapper.exists() or (paths.UNIT_DIR/'exitpool-awg@.service').exists())
        self.assertTrue(paths.ETC.exists())
        installer.purge(everything=True)
        self.assertFalse(paths.ETC.exists() or paths.VAR.exists())

    def legacy(self, broken=None):
        etc, opt, var = paths.LEGACY_ETC, paths.LEGACY_OPT, paths.LEGACY_VAR
        for path in (etc/'instances', etc/'awg', opt, var/'de', var/'fi'):
            path.mkdir(parents=True)
        old = {'de': {'country': 'DE', 'profile_name': 'Germany', 'port': 21808, 'tag': 'happ-de',
                      'update_hours': 6, 'health_interval': 20, 'failure_threshold': 3, 'memory_mib': 384},
               'fi': {'kind': 'awg', 'country': 'FI', 'port': 21838, 'tag': 'awg-fi', 'label': 'FI.conf',
                      'health_interval': 20, 'failure_threshold': 3, 'memory_mib': 96}}
        (opt/'instances.json').write_text(json.dumps(old))
        for name, values in old.items():
            (etc/'instances'/f'{name}.json').write_text(json.dumps(values))
        (etc/'subscription.secret').write_text('happ://crypt5/EXAMPLE-NOT-REAL\n')
        (etc/'awg'/'fi.conf').write_text('[Interface]\nPrivateKey = EXAMPLE\n')
        (var/'de'/'events.jsonl').write_text('{"at": 1, "event": "start"}\n')
        installer.wrapper_path('happ_status.sh').parent.mkdir(parents=True, exist_ok=True)
        installer.wrapper_path('happ_status.sh').write_text('#!/bin/sh\nold\n')
        if broken:
            (self.root/'broken').write_text(broken)
        (paths.BIN).mkdir(parents=True)
        for name in paths.AWG_BINARIES:
            (paths.BIN/name).write_bytes(b'x')

    def test_backups_created_in_same_second_do_not_collide(self):
        with mock.patch.object(installer.time, 'strftime', return_value='20261007-000000'):
            first = installer.backup_dir('migration')
            (first/'preserved').write_text('original')
            second = installer.backup_dir('migration')
        self.assertNotEqual(first, second)
        self.assertEqual((first/'preserved').read_text(), 'original')
        self.assertEqual(second.stat().st_mode & 0o777, 0o700)

    def test_migration_switches_exits_one_by_one(self):
        self.legacy()
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            installer.upgrade(None, 'keep')
        cfg = json.loads((paths.OPT/'instances.json').read_text())
        self.assertEqual((cfg['fi']['memory_mib'], cfg['fi']['slot'], cfg['fi']['outbound'], cfg['fi']['health_interval']),
                         (64, 1, 'loopback', 10))
        self.assertEqual(cfg['de']['memory_mib'], 384)
        self.assertEqual((paths.ETC/'subscription.secret').stat().st_mode & 0o777, 0o600)
        self.assertEqual((paths.AWG_ETC/'fi.conf').stat().st_mode & 0o777, 0o600)
        self.assertTrue((paths.VAR/'de'/'events.jsonl').exists())
        self.assertIn('MemoryMax=128M', (paths.UNIT_DIR/'exitpool-awg@fi.service.d/exitpool.conf').read_text())
        switches = [c[1:] for c in self.commands() if c[0] == 'systemctl' and c[1] in ('enable', 'disable') and '--now' in c]
        self.assertEqual(switches, [['disable', '--now', 'happ@de.service'], ['enable', '--now', 'exitpool-happ@de.service'],
                                    ['disable', '--now', 'happ-awg@fi.service'], ['enable', '--now', 'exitpool-awg@fi.service']])
        self.assertIn('exitpool status', installer.wrapper_path('happ_status.sh').read_text())
        self.assertTrue(paths.LEGACY_ETC.exists())   # old files stay until cleanup-legacy
        self.assertEqual(json.loads((paths.OPT/'3xui-outbounds.json').read_text())[1]['settings'],
                         {'address': '127.0.0.1', 'port': 21838})

    def test_migration_rolls_back_the_exit_that_fails(self):
        self.legacy(broken='fi')
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            with self.assertRaises(installer.InstallError) as caught:
                installer.upgrade(None, 'keep')
        self.assertIn('Уже перенесены: de', str(caught.exception))
        tail = [c for c in self.commands() if c[0] in ('systemctl', 'python3')][-3:]
        self.assertEqual(tail[0][1:], ['disable', '--now', 'exitpool-awg@fi.service'])
        self.assertEqual((Path(tail[1][1]).name, tail[1][2:]), ('awgnet.py', ['cleanup', 'fi']))
        self.assertEqual(tail[2][1:], ['enable', '--now', 'happ-awg@fi.service'])

    def test_cleanup_refuses_after_failed_migration(self):
        self.legacy(broken='fi')
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            with self.assertRaises(installer.InstallError):
                installer.upgrade(None, 'keep')
        before = self.commands()
        with self.assertRaises(installer.InstallError):
            installer.cleanup_legacy()
        self.assertTrue(paths.LEGACY_ETC.exists())
        self.assertEqual(self.commands(), before)

    def test_cleanup_accepts_completed_migration(self):
        self.legacy()
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            installer.upgrade(None, 'keep')
        self.assertEqual(json.loads((paths.VAR/'migration-state.json').read_text())['phase'], 'complete')
        installer.cleanup_legacy()
        self.assertFalse(paths.LEGACY_ETC.exists())
        self.assertTrue((paths.OPT/'instances.json').exists())

    def test_retry_resumes_the_interrupted_migration(self):
        self.legacy(broken='fi')
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            with self.assertRaises(installer.InstallError) as caught:
                installer.upgrade(None, 'keep')
        self.assertIn('прежний выход снова работает', str(caught.exception))
        self.assertIn('продолжит перенос', str(caught.exception))
        slot = json.loads((paths.OPT/'instances.json').read_text())['fi']['slot']
        (self.root/'broken').unlink()
        before = len(self.commands())
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            installer.upgrade(None, 'keep')
        added = [c[1:] for c in self.commands()[before:] if c[0] == 'systemctl' and '--now' in c]
        # de already works under exitpool: untouched; fi is switched again, never both units at once.
        self.assertEqual(added, [['disable', '--now', 'happ-awg@fi.service'], ['enable', '--now', 'exitpool-awg@fi.service']])
        self.assertEqual(json.loads((paths.OPT/'instances.json').read_text())['fi']['slot'], slot)
        self.assertEqual(installer.migration_state()['phase'], 'complete')
        installer.cleanup_legacy()
        self.assertFalse(paths.LEGACY_ETC.exists())

    def test_side_by_side_without_a_migration_record_is_refused(self):
        self.legacy()
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            installer.upgrade(None, 'keep')
        (paths.VAR/'migration-state.json').unlink()
        installer.quiet(['systemctl', 'enable', '--now', 'happ-awg@fi.service'])   # someone started the old exit
        before = len(self.commands())
        with self.assertRaisesRegex(installer.InstallError, 'один ключ дважды'):
            installer.upgrade(None, 'keep')
        with self.assertRaises(installer.InstallError):
            installer.cleanup_legacy()
        added = self.commands()[before:]
        self.assertFalse(any(c[0] == 'systemctl' and c[1] in ('enable', 'disable', 'restart', 'stop') for c in added))

    def test_cleanup_refuses_while_an_old_unit_runs(self):
        self.legacy()
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            installer.upgrade(None, 'keep')
        installer.quiet(['systemctl', 'enable', '--now', 'happ@de.service'])
        with self.assertRaisesRegex(installer.InstallError, 'ещё работают'):
            installer.cleanup_legacy()
        self.assertTrue(paths.LEGACY_ETC.exists())

    def test_status_helpers_in_home_directories_are_rewritten(self):
        self.legacy()
        old = '#!/usr/bin/env bash\nexec python3 /opt/happ-service/status.py "$@"\n'
        for path in (self.root/'root'/'happ_status.sh', self.root/'home'/'ann'/'happ_status.sh'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(old)
        other = self.root/'home'/'bob'/'happ_status.sh'
        other.parent.mkdir(parents=True)
        other.write_text('#!/bin/sh\necho mine\n')
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            installer.upgrade(None, 'keep')
        for path in (self.root/'root'/'happ_status.sh', self.root/'home'/'ann'/'happ_status.sh'):
            self.assertIn('exitpool status', path.read_text())
        self.assertEqual(other.read_text(), '#!/bin/sh\necho mine\n')

    def test_purge_keeps_settings_and_upgrade_restores_them(self):
        self.legacy()
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            installer.upgrade(None, 'keep')
        installer.cleanup_legacy()
        cfg = json.loads((paths.OPT/'instances.json').read_text())
        installer.purge()
        self.assertTrue(installer.kept_config())
        with self.assertRaisesRegex(installer.InstallError, 'Восстановить'):
            installer.base(None, 'skip')
        before = len(self.commands())
        with mock.patch('image_build.prepare_image', return_value='retagged'):
            installer.upgrade(None, 'dir', self.bins, None, local_build=True)
        self.assertEqual(json.loads((paths.OPT/'instances.json').read_text()), cfg)
        self.assertTrue((paths.UNIT_DIR/'exitpool-awg@fi.service.d/exitpool.conf').exists())
        started = [c[1:] for c in self.commands()[before:] if c[0] == 'systemctl' and c[1:3] == ['enable', '--now']]
        self.assertIn(['enable', '--now', 'exitpool-awg@fi.service'], started)
        self.assertIn(['enable', '--now', 'exitpool-happ@de.service'], started)
        self.assertFalse(installer.kept_config())

    def test_migration_needs_binaries_for_awg(self):
        self.legacy()
        for name in paths.AWG_BINARIES:
            (paths.BIN/name).unlink()
        with self.assertRaises(installer.InstallError):
            installer.upgrade(None, 'keep')
        self.assertFalse(any(c[1:3] == ['disable', '--now'] for c in self.commands()))

    def test_release_archive_is_reproducible_and_loader_has_its_hash(self):
        import subprocess
        out1, out2 = self.root/'d1', self.root/'d2'
        for out in (out1, out2):
            subprocess.run([sys.executable, str(SOURCE/'tools'/'make_release.py'), '--out', str(out)],
                           check=True, capture_output=True)
        name = f'exitpool-{paths.VERSION}.tar.gz'
        self.assertEqual((out1/name).read_bytes(), (out2/name).read_bytes())
        digest = fetch.sha256(out1/name)
        loader = (out1/'exitpool-install.sh').read_text()
        self.assertIn(f'SHA256={digest}', loader)
        self.assertIn(f'VERSION={paths.VERSION}', loader)
        import tarfile
        with tarfile.open(out1/name) as tar:
            names = tar.getnames()
        self.assertTrue(all(n.startswith(f'exitpool-{paths.VERSION}/') for n in names))
        self.assertIn(f'exitpool-{paths.VERSION}/lib/awgnet.py', names)
        self.assertFalse(any('/tests/' in n or '__pycache__' in n for n in names))


class MemoryTests(unittest.TestCase):
    def test_existing_discovery_credit_reserve_and_no_swap_credit(self):
        budget=MemoryBudget(2048,700,150)
        self.assertTrue(budget.estimate(2)['fits'])
        self.assertFalse(budget.estimate(3)['fits'])
        self.assertEqual(budget.estimate(2)['additional'],362)
        self.assertEqual(parse_docker_mib('141.2MiB / 384MiB'),141)
        self.assertEqual(parse_docker_mib('1.5GiB / 2GiB'),1536)
        self.assertEqual(parse_docker_mib('bad'),0)

    def test_lxc_limit_and_swap_exclusion(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'meminfo').write_text('MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\nSwapFree: 9999999 kB\n')
            (p/'memory.max').write_text(str(1024*1024*1024))
            (p/'memory.current').write_text(str(700*1024*1024))
            b=read_memory(meminfo=p/'meminfo',cgroup=p)
            self.assertEqual((b.total_mib,b.available_mib),(1024,324))
            self.assertFalse(b.estimate(1)['fits'])


class DiscoveryTests(unittest.TestCase):
    def test_failed_discovery_cleans_container_and_secret(self):
        observed=[]
        def runner(argv,**kw):
            if argv[:2]==['docker','run']:
                for arg in argv:
                    if 'dst=/run/secrets/subscription' in arg:
                        secret=Path(arg.split('src=',1)[1].split(',',1)[0]);observed.append(secret)
                        self.assertEqual(secret.stat().st_mode&0o777,0o600)
                        self.assertNotIn('SENSITIVE',' '.join(argv))
                raise discovery.subprocess.CalledProcessError(1,argv)
            return discovery.subprocess.CompletedProcess(argv,0,'','')
        with mock.patch.object(discovery.subprocess,'run',side_effect=runner) as run:
            with self.assertRaises(discovery.subprocess.CalledProcessError):
                with discovery.Discovery(SOURCE/'happ','happ://crypt5/SENSITIVE'):pass
        self.assertTrue(observed)
        self.assertFalse(observed[0].exists())
        self.assertTrue(any(c.args[0][:2]==['docker','stop'] for c in run.call_args_list))

    def test_catalog_validation_does_not_trim_exact_titles(self):
        self.assertEqual(config.validate_catalog([' A ⚡️ ','B']),[' A ⚡️ ','B'])
        for value in ([],['bad\x1b[31m'],[None],['a']*201):
            with self.assertRaises(ValueError):config.validate_catalog(value)
        cfg=config.validate_instances({'p1':{'profile_name':' A ⚡️ ','port':12808}})
        self.assertEqual(cfg['p1']['profile_name'],' A ⚡️ ')
        self.assertEqual(cfg['p1']['country'],'')


if __name__=='__main__':unittest.main()
