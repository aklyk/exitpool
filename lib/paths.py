"""Filesystem layout of exitpool. Tests point it at a temporary tree with set_root()."""
import os
from pathlib import Path

VERSION = '2.0.0'
NAME = 'exitpool'


def set_root(root='/'):
    global ROOT, ETC, VAR, OPT, LIB, BIN, RUNDIR, RUN, AWG_ETC, UNIT_DIR, NETNS_ETC, LEGACY_ETC, LEGACY_VAR, LEGACY_OPT
    ROOT = Path(root)
    ETC = ROOT/'etc/exitpool'
    VAR = ROOT/'var/lib/exitpool'
    OPT = ROOT/'opt/exitpool'
    LIB = OPT/'lib'
    BIN = OPT/'bin'
    RUNDIR = ROOT/'run'
    RUN = RUNDIR/'exitpool'
    AWG_ETC = ETC/'awg'
    UNIT_DIR = ROOT/'etc/systemd/system'
    NETNS_ETC = ROOT/'etc/netns'
    # Legacy layout of happ-service 1.x (migrated by the upgrade).
    LEGACY_ETC = ROOT/'etc/happ-service'
    LEGACY_VAR = ROOT/'var/lib/happ-service'
    LEGACY_OPT = ROOT/'opt/happ-service'


set_root(os.environ.get('EXITPOOL_ROOT', '/'))

HAPP_NETWORK = 'exitpool-happ'
AWG_BINARIES = ('amneziawg-go', 'awg', 'xray')
