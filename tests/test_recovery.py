"""Outage policy: fast probes, gradual escalation, recovery well within 1-3 minutes."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'happ'/'runtime'))
import recovery
from recovery import Outage


def timeline(outage, until, step=15):
    """Simulate the manager loop: every action takes `step` seconds."""
    actions, now = [], outage.since
    while now < until:
        action = outage.next_action(now)
        actions.append((round(now-outage.since), action))
        if action == 'reconnect': outage.reconnected(now)
        if action == 'refresh': outage.refreshed(now)
        if action == 'restart': break
        now += step
    return actions


class OutagePolicyTests(unittest.TestCase):
    def test_probes_first_and_reconnects_every_two_minutes(self):
        actions = timeline(Outage('upstream', 1000, last_refresh=0), 1000+170)
        kinds = [a for _, a in actions]
        self.assertEqual(kinds[0], 'probe')
        reconnects = [t for t, a in actions if a == 'reconnect']
        self.assertEqual(reconnects, [120])
        # Every loop that is not a reconnect is a cheap probe: recovery is noticed within one step.
        self.assertTrue(all(a in ('probe', 'reconnect') for a in kinds))

    def test_refresh_after_three_minutes_then_every_fifteen(self):
        actions = timeline(Outage('upstream', 10_000, last_refresh=0), 10_000+1700)
        refreshes = [t for t, a in actions if a == 'refresh']
        self.assertEqual(refreshes[0], 180)
        self.assertTrue(all(b-a >= recovery.REFRESH_EVERY for a, b in zip(refreshes, refreshes[1:])))

    def test_recent_refresh_postpones_outage_refresh(self):
        outage = Outage('upstream', 10_000, last_refresh=10_000-60)
        self.assertEqual(outage.next_action(10_000+200), 'reconnect')
        self.assertEqual(outage.next_action(10_000+240), 'refresh')

    def test_missing_profile_waits_for_refresh_gap(self):
        outage = Outage('profile_missing', 5000, last_refresh=5000-100)
        self.assertEqual(outage.next_action(5000+100), 'wait')
        self.assertEqual(outage.next_action(5000+200), 'refresh')
        outage.refreshed(5000+200)
        self.assertEqual(outage.next_action(5000+300), 'wait')
        self.assertEqual(outage.next_action(5000+500), 'refresh')

    def test_clean_restart_only_after_thirty_minutes(self):
        actions = timeline(Outage('upstream', 0, last_refresh=0), 4000)
        self.assertEqual(actions[-1], (1800, 'restart'))
        self.assertNotIn('restart', [a for _, a in actions[:-1]])


if __name__ == '__main__':
    unittest.main()
