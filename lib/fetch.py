#!/usr/bin/env python3
"""Downloads for exitpool: release binaries checked by SHA-256, optional proxy.

Proxy: http://, https:// (TLS to the proxy), socks5h://, socks5://, socks4a://, socks4://, with or without a
login. The password never appears in process arguments or output: curl gets it from a temporary 0600 config.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths  # noqa: E402

SCHEMES = ('http', 'https', 'socks5h', 'socks5', 'socks4a', 'socks4')
MANIFEST = Path(__file__).resolve().parent.parent/'release'/'binaries.json'


class FetchError(RuntimeError):
    pass


def parse_proxy(value, user=None, password=None):
    """Normalise a proxy URL; returns dict(url without credentials, user, password) or None."""
    if not value:
        return None
    value = value.strip()
    if '://' not in value:
        raise FetchError('Прокси: укажите тип, например socks5h://127.0.0.1:1080 или http://host:3128.')
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise FetchError('Прокси: неверный адрес или порт (нужно число от 1 до 65535).') from None
    if parsed.scheme not in SCHEMES or not parsed.hostname or not port:
        raise FetchError('Прокси: тип http, https, socks5h, socks5, socks4a или socks4, адрес и порт обязательны.')
    url_user = urllib.parse.unquote(parsed.username) if parsed.username else None
    url_pass = urllib.parse.unquote(parsed.password) if parsed.password else None
    host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    return {'url': f'{parsed.scheme}://{host}:{parsed.port}', 'user': user or url_user,
            'password': password if password is not None else url_pass}


def masked(proxy):
    if not proxy:
        return 'без прокси'
    scheme, rest = proxy['url'].split('://', 1)
    return f'{scheme}://{proxy["user"]}:***@{rest}' if proxy.get('user') else proxy['url']


def proxy_path():
    return paths.ETC/'proxy.json'


def load_saved_proxy():
    try:
        data = json.loads(proxy_path().read_text())
        return parse_proxy(data['url'], data.get('user'), data.get('password'))
    except (OSError, ValueError, KeyError, FetchError):
        return None


def save_proxy(proxy):
    path = proxy_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(proxy, out)


def clear_proxy():
    proxy_path().unlink(missing_ok=True)


def full_url(proxy):
    """URL with percent-encoded credentials, for the environment of a child process (never for arguments)."""
    auth = ''
    if proxy.get('user'):
        auth = urllib.parse.quote(proxy['user'], safe='')+':'+urllib.parse.quote(proxy.get('password') or '', safe='')+'@'
    scheme, rest = proxy['url'].split('://', 1)
    return f'{scheme}://{auth}{rest}'


def env_proxy():
    # EXITPOOL_PROXY: handed over by the gist loader and the wizard; then the usual variables.
    for key in ('EXITPOOL_PROXY', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
        if os.environ.get(key):
            try:
                return parse_proxy(os.environ[key])
            except FetchError:
                return None
    return None


def _quote(value):
    return '"'+value.replace('\\', '\\\\').replace('"', '\\"')+'"'


def curl(url, output, proxy=None, timeout=300):
    """Download with curl; credentials only in a private config file. Returns (ok, short error)."""
    with tempfile.TemporaryDirectory(prefix='exitpool-curl-') as tmp:
        config = Path(tmp)/'curl.conf'
        lines = ['silent', 'show-error', 'fail', 'location', 'retry = 2', 'connect-timeout = 20',
                 f'max-time = {timeout}', f'url = {_quote(url)}', f'output = {_quote(str(output))}']
        if proxy:
            lines.append(f'proxy = {_quote(proxy["url"])}')
            if proxy.get('user'):
                lines.append(f'proxy-user = {_quote(proxy["user"]+":"+(proxy.get("password") or ""))}')
        else:
            lines.append('noproxy = "*"')
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write('\n'.join(lines)+'\n')
        r = subprocess.run(['curl', '-K', str(config)], capture_output=True, text=True, timeout=timeout+30)
    if r.returncode:
        message = (r.stderr.strip().splitlines() or [f'curl код {r.returncode}'])[-1]
        if proxy and proxy.get('password'):
            message = message.replace(proxy['password'], '***')
        return False, message[:300]
    return True, ''


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def manifest():
    return json.loads(MANIFEST.read_text())


def install_binary(source, name, expected=None):
    if expected and sha256(source) != expected:
        raise FetchError(f'{name}: SHA-256 не совпадает с релизом — файл отвергнут.')
    paths.BIN.mkdir(mode=0o755, parents=True, exist_ok=True)
    target = paths.BIN/name
    temp = paths.BIN/f'.{name}.new'
    shutil.copyfile(source, temp)
    temp.chmod(0o755)
    temp.replace(target)


def install_from_dir(directory, strict=True, progress=print):
    """Binaries from a local directory (release download, CI artifacts or the fallback build)."""
    files = manifest()['files']
    for name in paths.AWG_BINARIES:
        source = Path(directory)/name
        if not source.is_file():
            raise FetchError(f'В {directory} нет файла {name}.')
        actual = sha256(source)
        if actual != files[name]['sha256']:
            if strict:
                raise FetchError(f'{name}: SHA-256 не совпадает с релизом — файл отвергнут.')
            progress(f'{name}: собран локально, хеш {actual[:12]} отличается от релизного — это нормально для своей сборки.')
        install_binary(source, name)
    progress('Бинарники AWG установлены: '+', '.join(paths.AWG_BINARIES))


def check_dir(directory):
    """name -> 'release' (hash matches), 'local' (own build) or 'missing'."""
    files = manifest()['files']
    result = {}
    for name in paths.AWG_BINARIES:
        source = Path(directory)/name
        result[name] = 'missing' if not source.is_file() else \
            'release' if sha256(source) == files[name]['sha256'] else 'local'
    return result


def download_files(directory, proxy=None, progress=print, base_url=None):
    """Download and verify release binaries into directory; nothing is installed."""
    data = manifest()
    base = base_url or data['base_url']
    for name, info in data['files'].items():
        progress(f'Скачиваю {name} ({masked(proxy)})…')
        ok, error = curl(base+name, Path(directory)/name, proxy)
        if not ok:
            raise FetchError(f'{name} не скачался с {urllib.parse.urlsplit(base).hostname}: {error}')
        if sha256(Path(directory)/name) != info['sha256']:
            raise FetchError(f'{name}: SHA-256 не совпадает с релизом — файл отвергнут.')


def download_release(proxy=None, progress=print, base_url=None):
    with tempfile.TemporaryDirectory(prefix='exitpool-bin-') as tmp:
        download_files(tmp, proxy, progress, base_url)
        install_from_dir(tmp, strict=True, progress=progress)


def build_to(directory, proxy=None, progress=print, keep_build_tools=False):
    """Fallback: compile from pinned commits on this server (needs ~1 GB disk temporarily)."""
    script = Path(__file__).resolve().parent.parent/'release'/'build-binaries.sh'
    env = dict(os.environ, EXITPOOL_KEEP_BUILD_TOOLS='1' if keep_build_tools else '0')
    if proxy:
        # apt/git/go downloads inside the build; the password is part of this environment only.
        full = full_url(proxy)
        env.update(HTTPS_PROXY=full, HTTP_PROXY=full, ALL_PROXY=full, https_proxy=full, http_proxy=full)
    progress('Собираю бинарники AWG из закреплённых исходников (несколько минут)…')
    r = subprocess.run(['bash', str(script), str(directory)], env=env)
    if r.returncode:
        raise FetchError('Сборка из исходников не удалась (вывод выше).')


def build_from_source(progress=print, proxy=None, keep_build_tools=False):
    with tempfile.TemporaryDirectory(prefix='exitpool-build-') as tmp:
        build_to(tmp, proxy, progress, keep_build_tools)
        install_from_dir(tmp, strict=False, progress=progress)


def probe_proxy(proxy):
    """Short check through the proxy, with the real reason on failure."""
    with tempfile.TemporaryDirectory(prefix='exitpool-probe-') as tmp:
        return curl('https://github.com/', Path(tmp)/'probe', proxy, timeout=20)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='exitpool binaries')
    ap.add_argument('action', choices=['download', 'from-dir', 'build', 'verify'])
    ap.add_argument('--dir')
    ap.add_argument('--proxy')
    ap.add_argument('--base-url')
    ap.add_argument('--allow-local-build', action='store_true')
    args = ap.parse_args()
    try:
        proxy = parse_proxy(args.proxy) if args.proxy else load_saved_proxy() or env_proxy()
        if args.action == 'download':
            download_release(proxy, base_url=args.base_url)
        elif args.action == 'from-dir':
            install_from_dir(args.dir, strict=not args.allow_local_build)
        elif args.action == 'build':
            build_from_source(proxy=proxy)
        else:
            files = manifest()['files']
            for name in paths.AWG_BINARIES:
                path = paths.BIN/name
                print(name, 'OK' if path.exists() and sha256(path) == files[name]['sha256'] else
                      ('локальная сборка' if path.exists() else 'нет'))
    except FetchError as exc:
        ap.exit(1, str(exc)+'\n')
