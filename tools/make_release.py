#!/usr/bin/env python3
"""Build a release: reproducible exitpool-X.Y.Z.tar.gz, SHA256SUMS and the gist loader with the archive hash.

  python3 tools/make_release.py --out dist [--binaries DIR] [--extra FILE ...]
With --binaries the three AWG binaries are checked against release/binaries.json and copied next to the archive.
--extra adds files as they are (e.g. the GPL source archive of amneziawg-tools) to the release and SHA256SUMS.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT/'lib'))
import paths  # noqa: E402

INCLUDE = ['lib', 'happ', 'systemd', 'webui', 'release', 'setup.sh', 'install.sh', 'uninstall.sh',
           'README.md', 'INSTALL.md', 'LICENSE', 'THIRD_PARTY_LICENSES.md']
SKIP = {'__pycache__', '.DS_Store'}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def files():
    for item in INCLUDE:
        path = ROOT/item
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from sorted(p for p in path.rglob('*') if p.is_file() and not SKIP & set(p.parts)
                              and not p.name.endswith(('.pyc', '.tmp')))
        else:
            raise SystemExit(f'нет {item}')


def build_archive(out):
    prefix = f'exitpool-{paths.VERSION}'
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.PAX_FORMAT) as tar:
        for path in files():
            rel = path.relative_to(ROOT).as_posix()
            info = tarfile.TarInfo(f'{prefix}/{rel}')
            data = path.read_bytes()
            info.size, info.mtime, info.uid, info.gid, info.uname, info.gname = len(data), 0, 0, 0, 'root', 'root'
            info.mode = 0o755 if path.suffix == '.sh' or rel in ('lib/cli.py', 'lib/setup.py') else 0o644
            tar.addfile(info, io.BytesIO(data))
    target = out/f'{prefix}.tar.gz'
    with open(target, 'wb') as handle, gzip.GzipFile(fileobj=handle, mode='wb', mtime=0, filename='') as gz:
        gz.write(raw.getvalue())
    return target


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, default=ROOT/'dist')
    ap.add_argument('--binaries', type=Path)
    ap.add_argument('--extra', type=Path, action='append', default=[])
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    archive = build_archive(a.out)
    sums = {archive.name: sha256(archive)}
    if a.binaries:
        manifest = json.loads((ROOT/'release'/'binaries.json').read_text())
        for name in paths.AWG_BINARIES:
            source = a.binaries/name
            if sha256(source) != manifest['files'][name]['sha256']:
                raise SystemExit(f'{name}: SHA-256 не совпадает с release/binaries.json — обновите манифест или сборку')
            shutil.copyfile(source, a.out/name)
            sums[name] = manifest['files'][name]['sha256']
    for extra in a.extra:
        shutil.copyfile(extra, a.out/extra.name)
        sums[extra.name] = sha256(extra)
    (a.out/'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in sums.items()))
    loader = (ROOT/'bootstrap'/'exitpool-install.sh').read_text()
    loader = loader.replace('@VERSION@', paths.VERSION).replace('@SHA256@', sums[archive.name])
    (a.out/'exitpool-install.sh').write_text(loader)
    (a.out/'release.json').write_text(json.dumps({'version': paths.VERSION, 'archive': archive.name,
                                                  'sha256': sums[archive.name]}, indent=2)+'\n')
    for name, digest in sums.items():
        print(f'{digest}  {name}')
    print(f'Загрузчик для gist: {a.out/"exitpool-install.sh"}')


if __name__ == '__main__':
    main()
