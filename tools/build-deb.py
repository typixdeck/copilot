#!/usr/bin/env python3
"""Build ARM64 Pi OS package with hash-pinned esptool source and distro dependencies."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.5-1'
TOOL_SHA = '125781f36e6a2d08c484524a45f340694675368b5eeead9d0cb21b2034a91d98'
TOOL_URL = 'https://files.pythonhosted.org/packages/source/e/esptool/esptool-5.3.1.tar.gz'


def main():
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    archive = build / 'esptool-5.3.1.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen(TOOL_URL, timeout=30) as response:
            payload = response.read(2 * 1024 * 1024)
        if hashlib.sha256(payload).hexdigest() != TOOL_SHA:
            raise SystemExit('esptool source hash mismatch')
        archive.write_bytes(payload)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != TOOL_SHA:
        raise SystemExit('esptool source hash mismatch')
    source = build / 'tool-source'
    source.mkdir(exist_ok=True)
    with tarfile.open(archive) as tar:
        tar.extractall(source, filter='data')
    stage = build / 'package'
    if stage.exists(): shutil.rmtree(stage)
    stage.mkdir()
    def put(relative, data, mode=0o644):
        path = stage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        path.chmod(mode)
    def copy_tree(src, dst):
        for path in sorted(src.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo'}:
                continue
            if path.is_symlink(): raise SystemExit('Refusing source symlink')
            target = stage / dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o644)
    put('DEBIAN/control', f'''Package: typix-copilot
Version: {VERSION}
Architecture: arm64
Maintainer: TypixDeck <dev@typixnode.com>
Section: utils
Priority: optional
Depends: python3 (>= 3.11), python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, python3-serial (>= 3.3), python3-bitstring (>= 3.1.6), python3-cryptography (>= 43), python3-reedsolo (>= 1.5.3), python3-yaml, python3-intelhex, python3-rich-click, python3-click, pkexec, ca-certificates
X-Typix-Compatible-OS: raspios-trixie
Description: TypixDeck onboard ESP32-S3 firmware store and writer
 Native GTK3 firmware browser with verified HTTPS cache, transient Polkit
 authorization, topology-bound maintenance, private full-flash backup and
 independent byte readback. Requires the reviewed TypixDeck board profile.
''')
    put('DEBIAN/conffiles', '/etc/typix-copilot/board.json\n/etc/typix-copilot/esptool.cfg\n')
    copy_tree(ROOT / 'src/typix_copilot', Path('usr/lib/python3/dist-packages/typix_copilot'))
    copy_tree(source / 'esptool-5.3.1/esptool', Path('usr/lib/typix-copilot/vendor/esptool'))
    put('usr/bin/typix-copilot', '#!/bin/sh\nexec /usr/bin/python3 -I -m typix_copilot "$@"\n', 0o755)
    put('usr/libexec/typix-copilot-write', '#!/usr/bin/python3 -I\nfrom typix_copilot.writer import main\nraise SystemExit(main())\n', 0o755)
    for src, dst in [('board.json','etc/typix-copilot/board.json'),
                     ('esptool.cfg','etc/typix-copilot/esptool.cfg'),
                     ('org.typixdeck.copilot.policy','usr/share/polkit-1/actions/org.typixdeck.copilot.policy'),
                     ('typix-copilot.desktop','usr/share/applications/typix-copilot.desktop')]:
        put(dst, (ROOT / 'packaging' / src).read_text())
    put('usr/share/icons/hicolor/scalable/apps/typix-copilot.svg', (ROOT / 'icon.svg').read_text())
    put('usr/share/doc/typix-copilot/esptool-COPYING', (source / 'esptool-5.3.1/LICENSE').read_text())
    put('usr/share/doc/typix-copilot/copyright', 'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\nUpstream-Name: typix-copilot\n\nFiles: usr/lib/typix-copilot/vendor/esptool/*\nCopyright: Espressif Systems and contributors\nLicense: GPL-2+\n See esptool-COPYING. Exact upstream source: ' + TOOL_URL + '\n SHA256: ' + TOOL_SHA + '\n')
    files = []
    for path in sorted(stage.rglob('*')):
        if path.is_dir(): path.chmod(0o755)
        elif path.is_file() and 'DEBIAN' not in path.relative_to(stage).parts:
            data = path.read_bytes()
            files.append(dict(path=str(path.relative_to(stage)), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), mode=oct(path.stat().st_mode & 0o777)))
    (build / 'payload-manifest.json').write_text(json.dumps({'package':'typix-copilot','version':VERSION,'architecture':'arm64','files':files}, indent=2) + '\n')
    put('DEBIAN/md5sums', ''.join(hashlib.md5((stage / row['path']).read_bytes()).hexdigest() + '  ' + row['path'] + '\n' for row in files))
    dist = ROOT / 'dist'; dist.mkdir(exist_ok=True)
    target = dist / f'typix-copilot_{VERSION}_arm64.deb'
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(target)], check=True)
    print(json.dumps(dict(file=target.name, bytes=target.stat().st_size, sha256=hashlib.sha256(target.read_bytes()).hexdigest())))


if __name__ == '__main__': main()
