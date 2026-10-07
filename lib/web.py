#!/usr/bin/env python3
"""exitpool LAN web interface: status of Happ and AmneziaWG exits, settings, subscription replacement,
adding/replacing/removing AWG exits.

Python standard library only. Listens on one configured address (a LAN IP, not the internet),
accepts clients from configured subnets, requires a password. Optional: exitpool works without it.
"""
import argparse
import base64
import getpass
import hashlib
import hmac
import http.server
import ipaddress
import json
import re
import secrets
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent/'webui'
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/app.css': ('app.css', 'text/css; charset=utf-8')}
ROUNDS = 390_000
MAX_BODY = 64*1024
COOKIE = 'exitpool_session'
LOGIN_WINDOW, LOGIN_LIMIT, GLOBAL_LIMIT = 600, 5, 30
FAILED_LOGIN_DELAY = 1
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class BadRequest(ValueError):
    pass


def hash_password(password, salt=None, rounds=ROUNDS):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, rounds)
    return f'pbkdf2_sha256${rounds}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}'


def check_password(password, stored):
    try:
        algorithm, rounds, salt, digest = stored.split('$')
        if algorithm != 'pbkdf2_sha256':
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except (ValueError, TypeError, AttributeError):
        return False


def validate_password(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 256 or any(ord(c) < 32 for c in password):
        raise ValueError('Пароль: от 8 до 256 символов, без управляющих символов.')
    return password


def config_path():
    return ops.ETC/'web.json'


def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError('web.json: ожидается объект.')
    listen = str(ipaddress.ip_address(data.get('listen', '')))
    port = data.get('port')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('web.json: порт от 1 до 65535.')
    allow = data.get('allow')
    if not isinstance(allow, list) or not 1 <= len(allow) <= 16:
        raise ValueError('web.json: allow — список подсетей.')
    allow = [str(ipaddress.ip_network(n, strict=False)) for n in allow]
    hosts = data.get('hosts', [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and re.fullmatch(r'[A-Za-z0-9.:\[\]-]{1,255}', h) for h in hosts):
        raise ValueError('web.json: hosts — список имён хоста.')
    hours = data.get('session_hours', 72)
    if type(hours) is not int or not 1 <= hours <= 24*90:
        raise ValueError('web.json: session_hours от 1 до 2160.')
    password = data.get('password', '')
    if not isinstance(password, str):
        raise ValueError('web.json: неверный пароль.')
    return {'listen': listen, 'port': port, 'allow': allow, 'hosts': hosts,
            'session_hours': hours, 'password': password}


def load_config():
    return validate_config(json.loads(config_path().read_text()))


def save_config(config):
    config_path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    ops.write_file(config_path(), json.dumps(validate_config(config), indent=2)+'\n', 0o600)


class App:
    def __init__(self, config, jobs=None, memory=None, panel=None):
        self.config = config
        self.networks = [ipaddress.ip_network(n) for n in config['allow']]
        wildcard = config['listen'] in ('0.0.0.0', '::')
        address = f"[{config['listen']}]" if ':' in config['listen'] else config['listen']
        self.hosts = None if wildcard and not config['hosts'] else \
            {h.lower() for h in [address, f"{address}:{config['port']}", *config['hosts']]}
        self.attempts = []
        self.lock = threading.Lock()
        self.jobs = jobs or ops.Jobs()
        self.memory = memory or ops.MemoryStats()
        self.panel = panel or ops.PanelMonitor()
        self.sessions = self.load_sessions()

    # Sessions survive service restarts (upgrades) but not reboots; only token hashes are stored.
    @staticmethod
    def sessions_path():
        return ops.RUNDIR/'exitpool-web-sessions.json'

    def load_sessions(self):
        data = ops.read_json(self.sessions_path(), {})
        now = time.time()
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get('csrf'), str)
                and isinstance(v.get('expires'), (int, float)) and v['expires'] > now}

    def save_sessions(self):
        try:
            self.sessions_path().parent.mkdir(parents=True, exist_ok=True)
            ops.write_file(self.sessions_path(), json.dumps(self.sessions), 0o600)
        except OSError:
            pass

    @staticmethod
    def key(token):
        return hashlib.sha256((token or '').encode()).hexdigest()

    def new_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
            self.sessions[self.key(token)] = {'expires': now+self.config['session_hours']*3600, 'csrf': csrf}
            self.save_sessions()
        return token

    def session(self, token):
        with self.lock:
            item = self.sessions.get(self.key(token))
            if item and item['expires'] > time.time():
                return item
        return None

    def drop_session(self, token):
        with self.lock:
            if self.sessions.pop(self.key(token), None):
                self.save_sessions()

    def throttled(self, address):
        now = time.time()
        with self.lock:
            self.attempts = [(t, a) for t, a in self.attempts if now-t < LOGIN_WINDOW]
            mine = sum(1 for _, a in self.attempts if a == address)
            return mine >= LOGIN_LIMIT or len(self.attempts) >= GLOBAL_LIMIT

    def failed_login(self, address):
        with self.lock:
            self.attempts.append((time.time(), address))

    def set_password(self, password):
        config = dict(self.config, password=hash_password(validate_password(password)))
        save_config(config)
        with self.lock:
            self.config = config
            self.sessions = {}
            self.save_sessions()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'exitpool-web'
    sys_version = ''
    protocol_version = 'HTTP/1.1'
    app = None

    def log_request(self, code='-', size='-'):
        sys.stderr.write(f'{self.client_address[0]} {self.command} {urlsplit(self.path).path} {code}\n')

    def log_message(self, fmt, *args):
        sys.stderr.write(f'{self.client_address[0]} {fmt % args}\n'[:300])

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    # -- responses
    def headers_common(self, content_type, length):
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)

    def reply(self, code, body, content_type, cookie=None):
        self.send_response(code)
        self.headers_common(content_type, len(body))
        if cookie is not None:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def json(self, code, value, cookie=None):
        self.reply(code, json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8', cookie)

    # -- request helpers
    def allowed(self):
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if getattr(address, 'ipv4_mapped', None):
            address = address.ipv4_mapped
        if not any(address in n for n in self.app.networks):
            return False
        host = (self.headers.get('Host') or '').lower()
        if self.app.hosts is None or host in self.app.hosts:
            return True
        # DNS rebinding cannot produce loopback names: they mean an SSH/proxy tunnel to this
        # interface on some local port. The client subnet check above still applies.
        return re.fullmatch(r'(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?', host) is not None

    def token(self):
        for part in (self.headers.get('Cookie') or '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == COOKIE:
                return value
        return None

    def read_raw(self):
        # Always consume the body, even for rejected requests: leftovers would be parsed
        # as the next request on a keep-alive connection.
        self.raw = b''
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True
            raise BadRequest('Неверный размер запроса.')
        self.raw = self.rfile.read(length)

    def body(self):
        if not (self.headers.get('Content-Type') or '').startswith('application/json'):
            raise BadRequest('Ожидается JSON.')
        try:
            value = json.loads(self.raw or b'{}')
        except ValueError:
            raise BadRequest('Неверный JSON.') from None
        if not isinstance(value, dict):
            raise BadRequest('Ожидается объект JSON.')
        return value

    def cookie(self, token, max_age):
        return f'{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}'

    # -- routing
    def dispatch(self, method):
        try:
            if method == 'POST':
                self.read_raw()
            if not self.allowed():
                return self.reply(403, b'Forbidden\n', 'text/plain; charset=utf-8')
            path = urlsplit(self.path).path
            if method == 'GET' and path in STATIC:
                name, kind = STATIC[path]
                return self.reply(200, (ASSETS/name).read_bytes(), kind)
            if path == '/api/login' and method == 'POST':
                return self.login()
            session = self.app.session(self.token())
            if path == '/api/session' and method == 'GET':
                return self.json(200, {'authenticated': bool(session), 'csrf': session['csrf'] if session else None})
            if not session:
                return self.json(401, {'error': 'Нужно войти.'})
            if method == 'POST':
                if not hmac.compare_digest(self.headers.get('X-Exitpool-CSRF') or '', session['csrf']):
                    return self.json(403, {'error': 'Сессия устарела: обновите страницу.'})
                return self.post(path, self.body())
            return self.get(path)
        except ops.OpsError as exc:
            self.json(409, {'error': str(exc)})
        except BadRequest as exc:
            self.json(400, {'error': str(exc)})
        except Exception as exc:  # noqa: BLE001 - details only to the service journal
            traceback.print_exc()
            self.json(500, {'error': 'Внутренняя ошибка ('+type(exc).__name__+'); см. journalctl -u exitpool-web.'})

    def get(self, path):
        if path == '/api/status':
            value = ops.status(self.app.memory, self.app.panel)
            value['job'] = summary(self.app.jobs.snapshot())
            return self.json(200, value)
        if path == '/api/job':
            return self.json(200, self.app.jobs.snapshot())
        if path == '/api/backups':
            return self.json(200, {'backups': ops.list_backups()})
        if path == '/api/panel':
            return self.json(200, {'settings': ops.panel_public(ops.load_panel()), 'state': self.app.panel.get()})
        match = re.fullmatch(r'/api/instances/([a-z][a-z0-9_-]{0,23})/log', path)
        if match:
            return self.json(200, {'lines': ops.journal(match[1])})
        return self.json(404, {'error': 'Не найдено.'})

    def post(self, path, body):
        match = re.fullmatch(r'/api/instances/([a-z][a-z0-9_-]{0,23})/(action|settings|profile|remove)', path)
        if match and match[2] == 'action':
            action = body.get('action')
            if action in ('refresh', 'reconnect', 'probe'):
                ops.command(match[1], action)
            elif action in ('start', 'stop', 'restart'):
                with ops.locked():
                    ops.service(match[1], action)
            else:
                raise BadRequest('Неизвестное действие.')
            return self.json(200, {'ok': True})
        if match and match[2] == 'settings':
            return self.json(200, {'ok': True, 'settings': ops.save_instance(match[1], body.get('settings'))})
        if match and match[2] == 'profile':
            # The profile is written to a private file only; it is never echoed back.
            self.app.jobs.awg_replace(match[1], body.get('profile'))
            return self.json(200, {'ok': True})
        if match:
            return self.json(200, {'ok': True, **ops.remove_instance(match[1])})
        if path == '/api/awg':
            entry = self.app.jobs.awg_add(body)
            return self.json(200, {'ok': True, 'tag': entry['tag'], 'port': entry['port'],
                                   'address': ops.relay_address(entry), 'memory_mib': entry['memory_mib']})
        if path == '/api/subscription/discover':
            self.app.jobs.discover(body.get('key'))
            return self.json(200, {'ok': True})
        if path == '/api/subscription/apply':
            self.app.jobs.apply(body.get('mapping'))
            return self.json(200, {'ok': True})
        if path == '/api/subscription/cancel':
            self.app.jobs.cancel()
            return self.json(200, {'ok': True})
        if path == '/api/backups/restore':
            self.app.jobs.restore(body.get('id'))
            return self.json(200, {'ok': True})
        if path == '/api/panel':
            settings = ops.save_panel(body.get('settings'))
            self.app.panel.refresh()
            return self.json(200, {'ok': True, 'settings': settings, 'state': self.app.panel.get()})
        if path == '/api/panel/clear':
            ops.clear_panel()
            self.app.panel.refresh()
            return self.json(200, {'ok': True})
        if path == '/api/password':
            if not check_password(str(body.get('current', '')), self.app.config['password']):
                return self.json(403, {'error': 'Текущий пароль неверен.'})
            try:
                self.app.set_password(body.get('new'))
            except ValueError as exc:
                raise BadRequest(str(exc)) from None
            return self.json(200, {'ok': True}, cookie=self.cookie('', 0))
        if path == '/api/logout':
            self.app.drop_session(self.token())
            return self.json(200, {'ok': True}, cookie=self.cookie('', 0))
        return self.json(404, {'error': 'Не найдено.'})

    def login(self):
        address = self.client_address[0]
        if self.app.throttled(address):
            return self.json(429, {'error': 'Слишком много попыток входа. Подождите 10 минут.'})
        body = self.body()
        if not check_password(str(body.get('password', ''))[:256], self.app.config['password']):
            self.app.failed_login(address)
            time.sleep(FAILED_LOGIN_DELAY)
            return self.json(403, {'error': 'Неверный пароль.'})
        token = self.app.new_session()
        return self.json(200, {'ok': True}, cookie=self.cookie(token, self.app.config['session_hours']*3600))


def summary(job):
    return {k: job.get(k) for k in ('phase', 'kind', 'error') if k in job}


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32


def make_server(app):
    handler = type('BoundHandler', (Handler,), {'app': app})
    Server.address_family = socket.AF_INET6 if ':' in app.config['listen'] else socket.AF_INET
    return Server((app.config['listen'], app.config['port']), handler)


def background(app, stop):
    while not stop.wait(30):
        app.memory.refresh()
        app.panel.refresh()


def serve():
    config = load_config()
    if not config['password']:
        raise SystemExit('Пароль веб-интерфейса не задан: sudo exitpool web passwd')
    app = App(config)
    server = make_server(app)
    stop = threading.Event()
    app.memory.refresh()
    app.panel.refresh()
    threading.Thread(target=background, args=(app, stop), daemon=True).start()
    print(f"exitpool web: http://{config['listen']}:{config['port']}/ allow {', '.join(config['allow'])}", flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


def change_password(password):
    """New password from the CLI: old sessions end even if the service keeps them on disk across restarts."""
    import subprocess
    cfg = load_config()
    cfg['password'] = hash_password(validate_password(password))
    save_config(cfg)
    state = subprocess.run(['systemctl', 'is-active', 'exitpool-web.service'], capture_output=True, text=True)
    active = state.stdout.strip() in ('active', 'activating', 'reloading')
    if active:
        subprocess.run(['systemctl', 'stop', 'exitpool-web.service'], check=True)
    App.sessions_path().unlink(missing_ok=True)
    if active:
        subprocess.run(['systemctl', 'start', 'exitpool-web.service'], check=True)
    return active


def read_new_password(from_stdin):
    if from_stdin:
        return validate_password(sys.stdin.readline().rstrip('\n'))
    while True:
        first = getpass.getpass('Новый пароль веб-интерфейса: ')
        if getpass.getpass('Повторите пароль: ') != first:
            print('Пароли не совпадают.')
            continue
        try:
            return validate_password(first)
        except ValueError as exc:
            print(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('serve')
    configure = sub.add_parser('configure', help='Создать или изменить web.json')
    configure.add_argument('--listen', required=True)
    configure.add_argument('--port', type=int, default=1338)
    configure.add_argument('--allow', action='append', required=True)
    configure.add_argument('--host', action='append', default=[])
    configure.add_argument('--password-stdin', action='store_true')
    passwd = sub.add_parser('passwd', help='Сменить пароль')
    passwd.add_argument('--stdin', action='store_true')
    sub.add_parser('show')
    args = parser.parse_args()
    try:
        if args.action == 'serve':
            return serve()
        if args.action == 'show':
            config = load_config()
            print(f"http://{config['listen']}:{config['port']}/  allow: {', '.join(config['allow'])}  "
                  f"password: {'set' if config['password'] else 'NOT SET'}")
            return 0
        if args.action == 'passwd':
            change_password(read_new_password(args.stdin))
            print('Пароль изменён; прежние веб-сессии завершены.')
            return 0
        try:
            previous = load_config()
        except (OSError, ValueError):
            previous = {}
        config = {'listen': args.listen, 'port': args.port, 'allow': args.allow, 'hosts': args.host,
                  'session_hours': previous.get('session_hours', 72), 'password': previous.get('password', '')}
        if args.password_stdin or not config['password']:
            config['password'] = hash_password(read_new_password(args.password_stdin))
        save_config(config)
        print(f"Сохранено: http://{args.listen}:{args.port}/")
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    raise SystemExit(main())
