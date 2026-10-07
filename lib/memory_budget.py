"""Memory planning: one place for every estimate the wizard, CLI and web interface show.

Budgets are empirical expectations of typical use, not guarantees and not ceilings: with enough CPU the
measured peaks stay below them, but on a starved CPU (25 % of a core) the Go heap outgrows GOMEMLIMIT
for a while and PSS peaks went 30-60 % above the budget (7.10.2026); the system reserve is there to absorb that.
Hard systemd limits (MemoryMax) are separate numbers and never summed into the requirement.
"""
from dataclasses import dataclass
import math
from pathlib import Path
import re

MIB = 1024*1024
PER_INSTANCE_MIB = 256     # one Happ exit (GUI, Xvfb, core, relay)
RESERVE_MIB = 256          # system reserve when Happ is installed
AWG_RESERVE_MIB = 128      # system reserve for an AWG-only server
DOCKER_MIB = 90            # dockerd + containerd when Docker is not installed yet
WEB_MIB = 25               # optional web interface

# AWG memory modes: GOMEMLIMIT of amneziawg-go and of the Xray relay (each), MiB.
AWG_MODES = (32, 64, 128)
AWG_DEFAULT_MODE = 64
AWG_MIN_MODE = 32
# Test 3 (06.10.2026): peak PSS 58.9 / 79.7 / 125.3 MiB under 8 connections -> budgets with headroom.
# Mode test (07.10.2026, n=1-2): peaks without a CPU quota 67 / 83 / 111 MiB; with 25 % of a core
# 111 / 131 / 140 MiB (48: 142 MiB and memory.max reclaim, no OOM).
AWG_BUDGET = {32: 80, 64: 100, 128: 160}
# Texts chosen by the user on 07.10.2026 after the mode test: no speeds and no fixed multipliers/percentages
# (n=1-2 measurements did not support "2-3x CPU", "-25 %" or "+60 %" as general rules).
AWG_MODE_TEXT = {
    32: 'для экономии памяти: при нехватке процессора загрузки могут идти медленнее',
    64: 'рекомендуется: баланс памяти и нагрузки на процессор',
    128: 'больше запаса памяти для интенсивной нагрузки',
}


def awg_budget(mode):
    """Expected memory of one AWG exit (manager + amneziawg-go + relay), MiB."""
    mode = int(mode)
    if mode <= 32:
        return AWG_BUDGET[32]
    if mode <= 128:
        low, high = (32, 64) if mode <= 64 else (64, 128)
        return math.ceil(AWG_BUDGET[low]+(mode-low)*(AWG_BUDGET[high]-AWG_BUDGET[low])/(high-low))
    return math.ceil(AWG_BUDGET[128]+(mode-128)*1.25)


def awg_caps(mode):
    """Hard limits (MemoryMax, MiB): manager+amneziawg-go unit, relay unit. 32->128/64, 64->128/96, 128->192/160."""
    mode = int(mode)
    return max(128, mode+64), max(64, mode+32)


@dataclass(frozen=True)
class MemoryBudget:
    total_mib: int
    available_mib: int
    first_mib: int = 0

    def estimate(self, count):
        total = count*PER_INSTANCE_MIB
        additional = max(0, total-self.first_mib)
        return {'total':total, 'additional':additional, 'reserve':RESERVE_MIB,
                'fits':additional+RESERVE_MIB <= self.available_mib,
                'remaining':self.available_mib-additional}


def plan(available_mib, happ=0, awg_modes=(), web=False, docker_missing=False, happ_first_mib=0):
    """Budget for adding components. PASS: fits with reserve; WARN: fits without it; FAIL: does not fit."""
    items = []
    if docker_missing:
        items.append(('Docker', DOCKER_MIB))
    if happ:
        items.append((f'Happ × {happ}', max(0, happ*PER_INSTANCE_MIB-happ_first_mib)))
    for mode in awg_modes:
        items.append((f'AWG, режим {mode} МиБ', awg_budget(mode)))
    if web:
        items.append(('веб-интерфейс', WEB_MIB))
    expected = sum(v for _, v in items)
    reserve = RESERVE_MIB if happ else AWG_RESERVE_MIB
    status = 'PASS' if available_mib >= expected+reserve else 'WARN' if available_mib >= expected else 'FAIL'
    return {'items': items, 'expected': expected, 'reserve': reserve, 'available': available_mib,
            'remaining': available_mib-expected, 'status': status}


def read_memory(first_mib=0, meminfo='/proc/meminfo', cgroup='/sys/fs/cgroup'):
    try:
        values={line.split(':',1)[0]:int(line.split()[1])*1024
                for line in Path(meminfo).read_text().splitlines() if ':' in line}
        total, available = values['MemTotal'], values['MemAvailable']
    except (OSError,ValueError,KeyError):return None
    # Container/LXC VPS can expose host /proc/meminfo despite a lower cgroup limit.
    for limit_file,used_file in [('memory.max','memory.current'),
                                 ('memory/memory.limit_in_bytes','memory/memory.usage_in_bytes')]:
        try:
            limit=int((Path(cgroup)/limit_file).read_text())
            used=int((Path(cgroup)/used_file).read_text())
            total=min(total,limit); available=min(available,max(0,limit-used))
        except (OSError,ValueError):pass
    return MemoryBudget(total//MIB, max(0,available)//MIB, max(0,first_mib))


def parse_docker_mib(value):
    match=re.fullmatch(r'\s*([\d.]+)\s*(B|KiB|MiB|GiB|TiB)\s*',value.split('/',1)[0])
    if not match:return 0
    return int(float(match[1])*{'B':1/MIB,'KiB':1/1024,'MiB':1,'GiB':1024,'TiB':1024**2}[match[2]])
