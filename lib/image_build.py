"""Happ image: build once per exact runtime source; reuse it for every Happ exit.

AWG exits need no image (static binaries in /opt/exitpool/bin). An image built by happ-service 1.x
from the same runtime source is re-tagged instead of rebuilt (saves ~1 GB of downloads and 10 minutes).
"""
import hashlib
from pathlib import Path
import subprocess

IMAGE = 'exitpool-happ:4.3.0-318'
LEGACY_IMAGES = ('happ-headless:4.3.0-318-profiles-v4',)
LABEL = 'org.happ-service.source-sha256'   # kept from 1.x so old images can be recognised


def source_digest(source):
    source = Path(source)
    files = [source/'Dockerfile', source/'.dockerignore',
             *sorted(p for p in (source/'runtime').iterdir() if p.is_file() and p.suffix in ('.py', '.sh'))]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(source).as_posix().encode()+b'\0'+path.read_bytes()+b'\0')
    return digest.hexdigest()


def current_label(image):
    r = subprocess.run(['docker', 'image', 'inspect', '--format', '{{index .Config.Labels "'+LABEL+'"}}', image],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.strip() if r.returncode == 0 else None


def prepare_image(source, proxy_env=None):
    digest = source_digest(source)
    if current_label(IMAGE) == digest:
        return 'ready'
    for legacy in LEGACY_IMAGES:
        if current_label(legacy) == digest:
            subprocess.run(['docker', 'tag', legacy, IMAGE], check=True, timeout=30)
            return 'retagged'
    args = ['docker', 'build', '--label', LABEL+'='+digest, '-t', IMAGE]
    for key, value in (proxy_env or {}).items():   # proxy for RUN steps (apt, Happ .deb download)
        args += ['--build-arg', f'{key}={value}']
    subprocess.run(args+[str(source)], check=True)
    return 'built'


if __name__ == '__main__':
    import sys
    print(prepare_image(Path(sys.argv[1])))
