"""AmneziaWG profile (.conf) checks shared by the host service and the exit container.

Errors never contain key material: only parameter names and short explanations.
The profile itself is applied unchanged (minus wg-quick-only lines) by `awg setconf`.
"""
import base64
import hashlib
import ipaddress
import re

MAX_BYTES = 16*1024
# Lines that only wg-quick understands; awg setconf must not receive them.
QUICK_KEYS = {'address', 'dns', 'mtu', 'table', 'preup', 'postup', 'predown', 'postdown', 'saveconfig'}
SCRIPT_KEYS = {'preup', 'postup', 'predown', 'postdown'}
INTERFACE_KEYS = {'privatekey', 'listenport', 'fwmark', 'jc', 'jmin', 'jmax', 's1', 's2', 's3', 's4',
                  'h1', 'h2', 'h3', 'h4', 'i1', 'i2', 'i3', 'i4', 'i5', 'headerprotectionkey',
                  'contentpaddingaddition', 'rekeyaftertime', 'rekeytimeout', 'rejectaftertime',
                  'keepalivetimeout', 'maxhandshakeattempts', 'randomtrailers', 'disablecookies'} | QUICK_KEYS
PEER_KEYS = {'endpoint', 'publickey', 'allowedips', 'persistentkeepalive', 'presharedkey', 'advancedsecurity'}
KEY_FIELDS = ('privatekey', 'publickey', 'presharedkey', 'headerprotectionkey')
HOST = re.compile(r'(?=.{1,253}\Z)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z')


class ProfileError(ValueError):
    pass


def _lines(text):
    if not isinstance(text, str):
        raise ProfileError('Нужен текст профиля AmneziaWG (.conf).')
    if len(text.encode()) > MAX_BYTES:
        raise ProfileError('Профиль слишком большой (больше 16 КБ).')
    text = text.lstrip('\ufeff')
    if any(ord(c) < 32 and c not in '\r\n\t' for c in text):
        raise ProfileError('В профиле есть управляющие символы.')
    return text.replace('\r\n', '\n').replace('\r', '\n').split('\n')


def _split(line):
    """(key_lower, value) for 'Key = value' lines; comments after # are ignored for parsing."""
    stripped = line.split('#', 1)[0].strip()
    if not stripped or '=' not in stripped:
        return None, None
    key, value = stripped.split('=', 1)
    return key.strip().lower(), value.strip()


def sections(text):
    """[(name_lower, [(key_lower, value, original_line)])]; lines before a section are rejected."""
    result = []
    for line in _lines(text):
        stripped = line.split('#', 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith('['):
            name = stripped.strip('[]').strip().lower()
            if name not in ('interface', 'peer') or not stripped.endswith(']'):
                raise ProfileError('Неизвестная секция профиля: допустимы [Interface] и [Peer].')
            result.append((name, []))
            continue
        key, value = _split(line)
        if key is None:
            raise ProfileError('Строка профиля без «параметр = значение».')
        if not result:
            raise ProfileError('Параметры до секции [Interface].')
        result[-1][1].append((key, value, line))
    return result


def _key32(value, name):
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raw = b''
    if len(raw) != 32:
        raise ProfileError(f'{name}: ожидается ключ base64 (32 байта).')
    return raw


def parse(text):
    """Validate a profile for a full-tunnel IPv4 exit; return non-secret facts."""
    found = sections(text)
    interfaces = [items for name, items in found if name == 'interface']
    peers = [items for name, items in found if name == 'peer']
    if len(interfaces) != 1:
        raise ProfileError('Нужна ровно одна секция [Interface].')
    if len(peers) != 1:
        raise ProfileError('Поддерживается профиль ровно с одним [Peer].')
    iface, peer = {}, {}
    for items, allowed, target, section in ((interfaces[0], INTERFACE_KEYS, iface, 'Interface'),
                                            (peers[0], PEER_KEYS, peer, 'Peer')):
        for key, value, _ in items:
            if key not in allowed:
                raise ProfileError(f'[{section}]: неизвестный параметр {key}.')
            if key in SCRIPT_KEYS:
                raise ProfileError('PreUp/PostUp/PreDown/PostDown не поддерживаются: команды из профиля не выполняются.')
            if key in target and key not in ('address', 'dns', 'allowedips'):
                raise ProfileError(f'[{section}]: параметр {key} указан дважды.')
            target[key] = (target[key]+', '+value) if key in target else value
    if 'privatekey' not in iface:
        raise ProfileError('[Interface]: нет PrivateKey.')
    private = _key32(iface['privatekey'], 'PrivateKey')
    if 'publickey' not in peer:
        raise ProfileError('[Peer]: нет PublicKey.')
    _key32(peer['publickey'], 'PublicKey')
    for key in ('presharedkey',):
        if key in peer:
            _key32(peer[key], 'PresharedKey')
    if 'headerprotectionkey' in iface:
        _key32(iface['headerprotectionkey'], 'HeaderProtectionKey')

    address4 = None
    for item in [a.strip() for a in iface.get('address', '').split(',') if a.strip()]:
        try:
            net = ipaddress.ip_interface(item)
        except ValueError:
            raise ProfileError('[Interface]: неверный Address.') from None
        if net.version == 4 and address4 is None:
            address4 = str(net)
    if not address4:
        raise ProfileError('[Interface]: нужен IPv4-адрес в Address (только IPv6 пока не поддерживается).')

    dns4 = []
    for item in [d.strip() for d in iface.get('dns', '').split(',') if d.strip()]:
        try:
            ip = ipaddress.ip_address(item)
        except ValueError:
            continue   # search domains are irrelevant for a proxy exit
        if ip.version == 4 and str(ip) not in dns4:
            dns4.append(str(ip))

    mtu = None
    if 'mtu' in iface:
        if not re.fullmatch(r'\d{3,4}', iface['mtu']) or not 1280 <= int(iface['mtu']) <= 1500:
            raise ProfileError('[Interface]: MTU должен быть числом от 1280 до 1500.')
        mtu = int(iface['mtu'])

    allowed = [a.strip() for a in peer.get('allowedips', '').split(',') if a.strip()]
    try:
        nets = [ipaddress.ip_network(a, strict=False) for a in allowed]
    except ValueError:
        raise ProfileError('[Peer]: неверный AllowedIPs.') from None
    if ipaddress.ip_network('0.0.0.0/0') not in nets:
        raise ProfileError('[Peer]: AllowedIPs должен включать 0.0.0.0/0 — нужен полный туннель для выхода.')

    endpoint = peer.get('endpoint', '')
    match = re.fullmatch(r'([^\s:\[\]]+):(\d{1,5})', endpoint)
    if not match or not 1 <= int(match[2]) <= 65535:
        raise ProfileError('[Peer]: Endpoint должен быть адрес:порт (IPv6-адрес сервера пока не поддерживается).')
    host = match[1]
    try:
        if ipaddress.ip_address(host).version != 4:
            raise ProfileError('[Peer]: Endpoint с IPv6 пока не поддерживается.')
    except ValueError:
        if not HOST.fullmatch(host):
            raise ProfileError('[Peer]: неверное имя сервера в Endpoint.') from None

    return {'address4': address4, 'dns4': dns4, 'mtu': mtu, 'endpoint_host': host,
            'endpoint_port': int(match[2]), 'fingerprint': hashlib.sha256(private).hexdigest()[:12],
            'awg': any(k in iface for k in ('jc', 's1', 'h1', 'headerprotectionkey', 'i1'))}


def strip(text):
    """The profile for `awg setconf`: wg-quick-only [Interface] lines removed, everything else as is."""
    out, section = [], None
    for line in _lines(text):
        stripped = line.split('#', 1)[0].strip()
        if stripped.startswith('['):
            section = stripped.strip('[]').strip().lower()
        else:
            key, _ = _split(line)
            if section == 'interface' and key in QUICK_KEYS:
                continue
        out.append(line)
    return '\n'.join(out).strip('\n')+'\n'


def _clamped(value):
    """Curve25519 private keys are clamped by WireGuard; awg shows the clamped form."""
    try:
        raw = bytearray(base64.b64decode(value, validate=True))
    except (ValueError, TypeError):
        return value
    if len(raw) != 32:
        return value
    raw[0] &= 248
    raw[31] = (raw[31] & 127) | 64
    return base64.b64encode(bytes(raw)).decode()


def settings_view(text):
    """{(section, key): value} of parameters as awg understands them (for comparisons only)."""
    view = {}
    for name, items in sections(text):
        for key, value, _ in items:
            if name == 'interface' and key in QUICK_KEYS:
                continue
            value = re.sub(r'\s*,\s*', ', ', value)
            view[(name, key)] = _clamped(value) if key == 'privatekey' else value
    return view


# Values equal to the defaults are omitted by `awg showconf`; they must not count as "not applied".
DEFAULT_EQUIVALENT = {('interface', 'listenport'): {'0'}, ('interface', 'fwmark'): {'0', 'off'},
                      ('peer', 'persistentkeepalive'): {'0', 'off'}}
DEFAULT_EQUIVALENT.update({('interface', k): {'0'} for k in ('jc', 'jmin', 'jmax', 's1', 's2', 's3', 's4')})
DEFAULT_EQUIVALENT.update({('interface', f'h{i}'): {str(i)} for i in range(1, 5)})
BOOLEAN_KEYS = {('interface', 'randomtrailers'), ('interface', 'disablecookies'), ('peer', 'advancedsecurity')}


def _normalized(key, value):
    if value is None:
        return None
    if key in BOOLEAN_KEYS:
        low = value.lower()
        return 'on' if low in ('on', 'true', 'yes', '1') else 'off' if low in ('off', 'false', 'no', '0') else value
    return value


def missing_after_apply(profile_text, showconf_text):
    """Names of profile parameters that `awg showconf` reports differently (never values)."""
    want, have = settings_view(profile_text), settings_view(showconf_text)
    # awg resolves a host name in Endpoint; only the port is comparable then.
    port = lambda v: (v or '').rsplit(':', 1)[-1]
    missing = []
    for key, value in want.items():
        shown = have.get(key)
        if key[1] == 'endpoint':
            if port(shown) != port(value):
                missing.append(key)
            continue
        value, shown = _normalized(key, value), _normalized(key, shown)
        if shown is None and (value.lower() in DEFAULT_EQUIVALENT.get(key, set())
                              or (key in BOOLEAN_KEYS and value == 'off')):
            continue
        if shown != value:
            missing.append(key)
    return sorted(f'{s}.{k}' for s, k in missing)
