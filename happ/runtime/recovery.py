"""Outage policy: keep Happ running, probe often, escalate gradually.

Restarting the container never fixes a provider-side outage and hides the recovery
for a whole Happ/Xvfb startup. While degraded the published relay is closed, the
selected profile is probed every few seconds, so recovery is noticed within ~5-15 s
after the upstream returns. Periodic reconnects cover a stuck core; subscription
refreshes cover moved or re-keyed servers; a long outage ends in one clean restart.
"""
PROBE_SECONDS = 5          # pause between probes (a probe itself takes up to ~10 s)
RECONNECT_SECONDS = 120    # disconnect/reselect/connect while still down
REFRESH_AFTER = 180        # first subscription refresh during an outage
REFRESH_MIN_GAP = 300      # never refresh more often than every five minutes
REFRESH_EVERY = 900        # later refreshes during the same outage
RESTART_AFTER = 1800       # clean container restart after 30 minutes down
ADAPTER_ERRORS = 3         # consecutive GUI-adapter errors before a clean restart


class Outage:
    def __init__(self, reason, now, last_refresh):
        self.reason = reason
        self.since = now
        self.last_refresh = last_refresh
        self.last_reconnect = now
        self.refreshes = 0

    def next_action(self, now):
        if now-self.since >= RESTART_AFTER:
            return 'restart'
        gap = now-self.last_refresh
        if self.reason == 'profile_missing':
            # Nothing to probe: only a refreshed subscription can bring the profile back.
            return 'refresh' if gap >= REFRESH_MIN_GAP else 'wait'
        needed = REFRESH_EVERY if self.refreshes else REFRESH_MIN_GAP
        if now-self.since >= REFRESH_AFTER and gap >= needed:
            return 'refresh'
        if now-self.last_reconnect >= RECONNECT_SECONDS:
            return 'reconnect'
        return 'probe'

    def reconnected(self, now):
        self.last_reconnect = now

    def refreshed(self, now):
        self.last_refresh = now
        self.last_reconnect = now
        self.refreshes += 1
