#!/usr/bin/env python3
"""Host guard for AWG relays: forwarded traffic towards exit veths is dropped unless explicitly allowed.

The host itself reaches a relay through OUTPUT/INPUT and is never affected. Containers (Docker SNAT hides
their address behind the host's) and other machines reach a relay only through FORWARD — denied by default.
`allow` adds one bridge network (for a 3x-ui running in Docker). Only our own table is replaced, atomically.
"""
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

TABLE = 'exitpool_access'
IFACE = re.compile(r'[A-Za-z0-9_.:-]{1,15}\Z')


def policy_path():
    return paths.ETC/'access.json'


def load():
    try:
        data = json.loads(policy_path().read_text())
    except (OSError, ValueError):
        data = {}
    allowed = data.get('allowed_container_networks') if isinstance(data, dict) else None
    return {'allowed_container_networks': validate(allowed or [])}


def validate(rules):
    result = []
    for rule in rules:
        if not isinstance(rule, dict) or not IFACE.fullmatch(str(rule.get('interface', ''))):
            raise ValueError('Интерфейс моста: латиница, цифры, _ . : -, до 15 символов.')
        network = ipaddress.IPv4Network(str(rule.get('subnet', '')), strict=False)
        result.append({'interface': rule['interface'], 'subnet': str(network)})
    return result


def render(policy):
    lines = [f'iifname "{r["interface"]}" ip saddr {r["subnet"]} oifname "ep-h*" counter accept '
             f'comment "exitpool-container-allow"' for r in policy['allowed_container_networks']]
    # No generic "ct state established accept" here: revoking a network must cut live associations too.
    lines.append('oifname "ep-h*" counter drop comment "exitpool-deny-forward"')
    body = '\n'.join('    '+line for line in lines)
    return (f'table inet {TABLE} {{\n  chain forward {{\n'
            f'    type filter hook forward priority -5; policy accept;\n{body}\n  }}\n}}\n')


def apply(policy=None):
    policy = policy or load()
    exists = subprocess.run(['nft', 'list', 'table', 'inet', TABLE], capture_output=True).returncode == 0
    script = (f'delete table inet {TABLE}\n' if exists else '')+render(policy)
    r = subprocess.run(['nft', '-f', '-'], input=script, text=True, capture_output=True)
    if r.returncode:
        raise RuntimeError('nft: '+(r.stderr.strip().splitlines() or ['ошибка'])[-1][:200])
    return script


def save(policy):
    policy = {'allowed_container_networks': validate(policy['allowed_container_networks'])}
    path = policy_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name('.access.json.tmp')
    temp.write_text(json.dumps(policy, indent=2)+'\n')
    temp.chmod(0o600)
    temp.replace(path)
    return policy


def allow(interface, subnet):
    policy = load()
    rule = validate([{'interface': interface, 'subnet': subnet}])[0]
    if rule not in policy['allowed_container_networks']:
        policy['allowed_container_networks'].append(rule)
    apply(save(policy))
    return policy


def deny(interface, subnet=None):
    policy = load()
    policy['allowed_container_networks'] = [r for r in policy['allowed_container_networks']
                                            if not (r['interface'] == interface and (subnet is None or r['subnet'] == subnet))]
    apply(save(policy))
    return policy


def remove_table():
    subprocess.run(['nft', 'delete', 'table', 'inet', TABLE], capture_output=True)


if __name__ == '__main__':
    print(apply(), end='')
