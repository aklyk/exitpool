"""Shared validation for the wizard, installer, status tool and panel helper."""
import copy
import json
from pathlib import Path
import re
import socket

DEFAULTS = {'update_hours': 6, 'health_interval': 20, 'failure_threshold': 3, 'memory_mib': 384}
# AmneziaWG exits: no Docker, no GUI, no subscription; the profile lives in /etc/exitpool/awg/<name>.conf.
# memory_mib is the memory mode: GOMEMLIMIT of amneziawg-go and of the Xray relay (each), default 64.
AWG_DEFAULTS = {'health_interval': 10, 'failure_threshold': 3, 'memory_mib': 64, 'outbound': 'veth'}
AWG_SLOTS = range(1, 251)       # 10.250.<slot>.0/30 per exit
ID_PATTERN = re.compile(r'[a-z][a-z0-9_-]{0,23}\Z')
TAG_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
MAX_HAPP, MAX_TOTAL = 8, 16
HAPP_FIELDS = set(DEFAULTS) | {'kind', 'country', 'profile_query', 'profile_name', 'port', 'tag'}
AWG_FIELDS = set(AWG_DEFAULTS) | {'kind', 'country', 'port', 'tag', 'mtu', 'label', 'slot'}


def kind(cfg):
    return cfg.get('kind', 'happ') if isinstance(cfg, dict) else 'happ'


def _common(name, cfg, ports, tags, low_memory, high_memory, low_interval=10):
    if not isinstance(cfg['country'], str) or (cfg['country'] and not re.fullmatch(r'[A-Z]{2}', cfg['country'])):
        raise ValueError('Необязательное ограничение страны: пусто или код DE, SE, FI и т. п.')
    for field, low, high in [('port', 1025, 65535), ('health_interval', low_interval, 60), ('failure_threshold', 2, 10),
                             ('memory_mib', low_memory, high_memory)]:
        value = cfg.get(field)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{name}: {field} должен быть целым числом от {low} до {high}.')
    if cfg['port'] in (10808, 10809) or cfg['port'] in ports:
        raise ValueError('Порты должны различаться; 10808 и 10809 заняты внутри Happ.')
    if not isinstance(cfg['tag'], str) or not TAG_PATTERN.fullmatch(cfg['tag']) or cfg['tag'] in tags:
        raise ValueError('Tags исходящих должны быть уникальными: латиница, цифры, _, - или точка.')


def validate_instances(data):
    """Happ exits (default kind) and AmneziaWG exits; an AWG-only installation may start empty."""
    if not isinstance(data, dict) or len(data) > MAX_TOTAL:
        raise ValueError(f'Нужно не больше {MAX_TOTAL} выходов.')
    if sum(1 for c in data.values() if kind(c) == 'happ') > MAX_HAPP:
        raise ValueError(f'Нужно от 1 до {MAX_HAPP} экземпляров Happ.')
    result, ports, tags, slots = {}, set(), set(), set()
    for name, original in data.items():
        if not isinstance(name, str) or not ID_PATTERN.fullmatch(name):
            raise ValueError('Метка экземпляра: маленькие латинские буквы, цифры, дефис или _.')
        if not isinstance(original, dict):
            raise ValueError('Неверный формат параметров экземпляра.')
        if kind(original) not in ('happ', 'awg'):
            raise ValueError('Вид выхода: happ или awg.')
        if kind(original) == 'awg':
            if set(original) - AWG_FIELDS:
                raise ValueError('В параметрах экземпляра есть неизвестные поля.')
            cfg = {**AWG_DEFAULTS, **copy.deepcopy(original)}
            cfg.setdefault('country', '')
            cfg.setdefault('tag', 'awg-' + name)
            _common(name, cfg, ports, tags, 32, 1024, low_interval=5)
            if type(cfg.get('slot')) is not int or cfg['slot'] not in AWG_SLOTS or cfg['slot'] in slots:
                raise ValueError(f'{name}: slot — уникальное целое от 1 до 250 (подсеть 10.250.<slot>.0/30).')
            if cfg['outbound'] not in ('veth', 'loopback'):
                raise ValueError(f'{name}: outbound — veth или loopback.')
            slots.add(cfg['slot'])
            if 'mtu' in cfg and (type(cfg['mtu']) is not int or not 1280 <= cfg['mtu'] <= 1500):
                raise ValueError(f'{name}: mtu должен быть целым числом от 1280 до 1500.')
            label = cfg.get('label', '')
            if (not isinstance(label, str) or len(label) > 80
                    or any(ord(c) < 32 or ord(c) == 127 for c in label)):
                raise ValueError(f'{name}: подпись выхода до 80 символов без управляющих символов.')
            cfg['kind'] = 'awg'
        else:
            if set(original) - HAPP_FIELDS:
                raise ValueError('В параметрах экземпляра есть неизвестные поля.')
            cfg = {**DEFAULTS, **copy.deepcopy(original)}
            cfg.setdefault('country', '')
            field = 'profile_name' if 'profile_name' in cfg else 'profile_query'
            query = cfg.get(field)
            if (not isinstance(query, str) or not query.strip() or len(query) > 240
                    or any(ord(c) < 32 or ord(c) == 127 for c in query)):
                raise ValueError('Нужно название профиля без управляющих символов.')
            if field == 'profile_name' and 'profile_query' in cfg:
                raise ValueError('Используйте точное имя или прежний запрос поиска, не оба сразу.')
            value = cfg.get('update_hours')
            if type(value) is not int or not 1 <= value <= 168:
                raise ValueError(f'{name}: update_hours должен быть целым числом от 1 до 168.')
            cfg.setdefault('tag', 'happ-' + name)
            _common(name, cfg, ports, tags, 256, 2048)
            cfg.pop('kind', None)   # Happ entries keep the original on-disk format
        ports.add(cfg['port']); tags.add(cfg['tag']); result[name] = cfg
    return result


def validate_catalog(data):
    if not isinstance(data, list) or not 1 <= len(data) <= 200:
        raise ValueError('Happ не вернул список профилей (допускается до 200).')
    for name in data:
        if (not isinstance(name, str) or not name.strip() or len(name)>240
                or any(ord(c)<32 or ord(c)==127 for c in name)):
            raise ValueError('Happ вернул некорректное название профиля.')
    return list(data)


def load_instances(path):
    return validate_instances(json.loads(Path(path).read_text()))


def relay_address(cfg):
    """Address 3x-ui uses for this exit: veth of an AWG exit, otherwise the local published port."""
    if kind(cfg) == 'awg' and cfg.get('outbound', 'veth') == 'veth':
        return f"10.250.{cfg['slot']}.2"
    return '127.0.0.1'


def make_outbounds(instances):
    return [{'tag': c['tag'], 'protocol': 'socks',
             'settings': {'address': relay_address(c), 'port': c['port']}}
            for c in validate_instances(instances).values()]


def check_ports(instances):
    for cfg in validate_instances(instances).values():
        if relay_address(cfg) != '127.0.0.1':
            continue   # a veth relay listens only inside its own namespace
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(('127.0.0.1', cfg['port']))
        except OSError:
            raise ValueError(f"Локальный TCP-порт {cfg['port']} уже занят.") from None


def validate_subscription(value):
    value = value.strip()
    if not value.startswith('happ://crypt5/') or len(value) < 24 or any(c.isspace() for c in value):
        raise ValueError('Нужна целая ссылка happ://crypt5/... одной строкой.')
    return value


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['names', 'check', 'export'])
    p.add_argument('file', type=Path)
    p.add_argument('--subscription', type=Path)
    p.add_argument('--destination', type=Path)
    args = p.parse_args()
    try:
        cfg = load_instances(args.file)
        if args.action == 'names':
            print('\n'.join(cfg))
        elif args.action == 'check':
            if args.subscription:
                validate_subscription(args.subscription.read_text())
            check_ports(cfg)
        else:
            if not args.destination:
                p.error('--destination is required')
            args.destination.mkdir(parents=True, exist_ok=True)
            (args.destination/'instances.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=2)+'\n')
            (args.destination/'3xui-outbounds.json').write_text(json.dumps(make_outbounds(cfg), indent=2)+'\n')
    except (ValueError, OSError) as exc:
        p.exit(1, str(exc)+'\n')
