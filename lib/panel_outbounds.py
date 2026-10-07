#!/usr/bin/env python3
"""Add selected Happ outbounds to 3x-ui without changing its routing."""
import argparse
import copy
import datetime
import getpass
import http.client
import ipaddress
import json
import os
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request

from configuration import load_instances, make_outbounds


class PanelError(RuntimeError):
    """kind 'scheme' means the other protocol (http <-> https) is likely right; connect() retries it once."""
    def __init__(self, message, kind=None):
        super().__init__(message)
        self.kind = kind


DROPPED = 'Панель оборвала соединение — похоже, она работает по HTTPS: адрес должен начинаться с https://'


def is_loopback(host):
    if host in ('localhost', 'localhost.localdomain'):
        return True
    try:
        return ipaddress.ip_address(host.strip('[]')).is_loopback
    except ValueError:
        return False


def normalize_url(value):
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port  # validate malformed/out-of-range ports
    except ValueError:
        raise ValueError('Неверный адрес панели.') from None
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Адрес панели должен начинаться с http:// или https://, без логина/пароля в URL.')
    if parsed.query or parsed.fragment:
        raise ValueError('Укажите адрес панели без параметров после ? и #.')
    path = parsed.path.rstrip('/')
    for suffix in ('/panel/outbound', '/panel/inbounds', '/panel/settings', '/panel'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, '', '')).rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the bearer token to a redirect destination.
        return None


class PanelClient:
    def __init__(self, url, token):
        self.url = normalize_url(url)
        self.token = token.strip()
        if not self.token or any(c.isspace() for c in self.token):
            raise ValueError('API-токен пустой или содержит пробелы.')
        self.host = urllib.parse.urlsplit(self.url).hostname or ''
        self.scheme = urllib.parse.urlsplit(self.url).scheme
        # A panel on this server is reached over loopback, while its certificate names the public domain:
        # there is nothing to verify on 127.0.0.1. Remote panels keep full certificate checks.
        context = ssl._create_unverified_context() if is_loopback(self.host) else ssl.create_default_context()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  urllib.request.HTTPSHandler(context=context))

    def api(self, path, form, method='POST', timeout=90):
        data = urllib.parse.urlencode(form).encode() if method == 'POST' else None
        req = urllib.request.Request(self.url+path, data=data, method=method,
                                     headers={'Authorization': 'Bearer '+self.token})
        try:
            with self.opener.open(req, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            code, location = exc.code, exc.headers.get('Location', '') if exc.headers else ''
            exc.close()
            if self.scheme == 'http' and 300 <= code < 400 and location.startswith('https:'):
                raise PanelError('Панель работает по HTTPS: адрес должен начинаться с https://', 'scheme') from None
            if self.scheme == 'http' and code == 400:
                raise PanelError('Панель вернула HTTP 400 — так HTTPS-порт отвечает на запрос по http://. '
                                 'Если панель по HTTPS, адрес должен начинаться с https://', 'scheme') from None
            hint = (' 403 часто значит, что в панели задан домен (Panel Domain): подключайтесь по нему.'
                    if code == 403 else '')
            raise PanelError(f'Панель вернула HTTP {code}; проверьте URL, базовый путь и API-токен.{hint}') from None
        except http.client.HTTPException:
            # e.g. "UnknownProtocol: HTTP/0.0": an HTTPS port answered a plain-HTTP request.
            if self.scheme == 'http':
                raise PanelError('Панель ответила не по HTTP — похоже, она работает по HTTPS: '
                                 'адрес должен начинаться с https://', 'scheme') from None
            raise PanelError('Панель ответила не по протоколу HTTP; проверьте адрес и порт.') from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise PanelError('Сертификат панели не прошёл проверку ('+(reason.verify_message or 'ошибка')+'). '
                                 'Для панели на этом же сервере укажите https://127.0.0.1:ПОРТ/…') from None
            if isinstance(reason, ssl.SSLError) and self.scheme == 'https':
                detail = getattr(reason, 'reason', None) or str(reason)
                raise PanelError(f'TLS-соединение с панелью не установилось ({detail}); '
                                 'если панель работает без HTTPS, адрес должен начинаться с http://', 'scheme') from None
            if isinstance(reason, (ConnectionResetError, BrokenPipeError)) and self.scheme == 'http':
                raise PanelError(DROPPED, 'scheme') from None
            raise PanelError(f'Не удалось подключиться к панели: {reason}.') from None
        except (ConnectionResetError, BrokenPipeError) as exc:
            if self.scheme == 'http':   # a TLS port drops a plain-HTTP request
                raise PanelError(DROPPED, 'scheme') from None
            raise PanelError(f'Панель оборвала соединение: {type(exc).__name__}.') from None
        except (TimeoutError, OSError) as exc:
            raise PanelError(f'Не удалось получить ответ панели: {type(exc).__name__}.') from None
        except ValueError:
            raise PanelError('Панель вернула не JSON; проверьте URL и базовый путь.') from None
        if not isinstance(result, dict) or not result.get('success'):
            # Server messages may contain request data; do not echo them with secrets.
            raise PanelError('API панели отклонил запрос. Проверьте токен и совместимость с 3x-ui 3.8.x.')
        return result

    def read(self):
        try:
            value = self.api('/panel/api/xray/', {})['obj']
            result = json.loads(value) if isinstance(value, str) else value
            self.setting(result)
            return result
        except (KeyError, TypeError, ValueError):
            raise PanelError('Неожиданный формат шаблона панели; изменения не выполнялись.') from None

    @staticmethod
    def setting(template):
        value = template['xraySetting']
        result = json.loads(value) if isinstance(value, str) else value
        if not isinstance(result, dict) or not isinstance(result.get('outbounds'), list):
            raise ValueError('Invalid xray template')
        return result

    def balancers(self, timeout=15):
        """Balancer tag -> selector prefixes, from the saved template (read only)."""
        value = self.api('/panel/api/xray/', {}, timeout=timeout)['obj']
        try:
            template = json.loads(value) if isinstance(value, str) else value
            routing = self.setting(template).get('routing') or {}
            return {b['tag']: [s for s in b.get('selector') or [] if isinstance(s, str)]
                    for b in routing.get('balancers') or [] if isinstance(b, dict) and isinstance(b.get('tag'), str)}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PanelError('Неожиданный формат шаблона панели.') from None

    def balancer_status(self, tags, timeout=10):
        """Current choice of running balancers (3x-ui 3.8+); read only."""
        obj = self.api('/panel/api/xray/balancerStatus', {'tags': ','.join(tags)}, timeout=timeout).get('obj')
        return obj if isinstance(obj, dict) else {}

    def observatory(self, timeout=10):
        """Xray observatory: alive/delay per outbound as the balancers see them."""
        obj = self.api('/panel/api/server/xrayObservatory', {}, method='GET', timeout=timeout).get('obj')
        return {o['tag']: o for o in obj or [] if isinstance(o, dict) and isinstance(o.get('tag'), str)}

    def check_conflicts(self, additions):
        prepare_template(self.setting(self.read()), additions)

    def test(self, additions):
        results = []
        for outbound in additions:
            result = self.api('/panel/api/xray/testOutbound', {'outbound': json.dumps(outbound), 'mode': 'real'})
            obj = result.get('obj', {})
            if not isinstance(obj, dict) or not obj.get('success'):
                raise PanelError('Проверка исходящего не прошла: '+outbound['tag'])
            results.append({'tag': outbound['tag'], 'success': True, 'delay': obj.get('delay'),
                            'http_status': obj.get('httpStatus')})
        return results

    def add(self, additions, backup_dir):
        before = self.read()
        old = self.setting(before)
        new = prepare_template(old, additions)
        if new == old:
            return []
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        write_private(backup_dir/('panel-'+stamp+'.before.json'), before)
        if self.read() != before:
            raise PanelError('Настройки панели изменились параллельно. Повторите добавление; перезапись отменена.')
        self.api('/panel/api/xray/update', {'xraySetting': json.dumps(new, ensure_ascii=False),
                                          'outboundTestUrl': before.get('outboundTestUrl', '')})
        after = self.read()
        write_private(backup_dir/('panel-'+stamp+'.after.json'), after)
        if self.setting(after) != new:
            raise PanelError('Проверка сохранённого шаблона не совпала. Остановитесь и проверьте резервную копию.')
        tags = {o.get('tag') for o in old['outbounds']}
        return [o['tag'] for o in additions if o['tag'] not in tags]


def connect(url, token, check=None):
    """PanelClient whose address is proven: check(client) (default: read the template) must succeed.

    A wrong protocol (http vs https) is fixed automatically once. Returns (client, note); note is a text
    for the user when the address was corrected, otherwise None.
    """
    check = check or (lambda c: c.read())
    client = PanelClient(url, token)
    try:
        check(client)
        return client, None
    except PanelError as exc:
        if exc.kind != 'scheme':
            raise
        other = ('https' if client.scheme == 'http' else 'http')+client.url[len(client.scheme):]
        second = PanelClient(other, token)
        try:
            check(second)
        except PanelError:
            raise exc from None
        return second, f'Панель отвечает по {second.scheme.upper()}: использую {second.scheme}://.'


def prepare_template(old, additions):
    new = copy.deepcopy(old)
    existing = {o.get('tag'): o for o in old['outbounds']}
    for outbound in additions:
        tag = outbound['tag']
        if tag in existing:
            if existing[tag] != outbound:
                raise PanelError('В панели уже есть другой исходящий с tag '+tag+'. Измените префикс.')
        else:
            new['outbounds'].append(copy.deepcopy(outbound))
    return new


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write('\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', required=True)
    p.add_argument('--token-file', type=Path)
    p.add_argument('--backup-dir', type=Path, required=True)
    p.add_argument('--instances', type=Path, default=Path(__file__).with_name('instances.json'))
    p.add_argument('--test-only', action='store_true')
    args = p.parse_args()
    os.umask(0o077)
    try:
        token = args.token_file.read_text().strip() if args.token_file else getpass.getpass('API-токен 3x-ui: ')
        client = PanelClient(args.url, token)
        additions = make_outbounds(load_instances(args.instances))
        if args.test_only:
            print(json.dumps(client.test(additions), ensure_ascii=False, indent=2))
        else:
            tags = client.add(additions, args.backup_dir)
            print('Добавлены: '+', '.join(tags) if tags else 'Исходящие уже настроены; изменений нет.')
            print('Маршруты, балансеры и другие исходящие сохранены.')
    except (ValueError, OSError, PanelError) as exc:
        p.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    main()
