#!/usr/bin/env python3
"""Install a verified, user-owned Copilot preview and Desktop shortcut (no root)."""
from pathlib import Path
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile

source = Path(__file__).resolve().parents[1]
manifest = json.loads((source / 'deployment.json').read_text())
version = manifest['version']
if version != '0.1.0-preview.5' or os.getuid() == 0:
    raise SystemExit('Expected preview.5, run as the desktop user')
for name, expected in manifest['files'].items():
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise SystemExit('Unsafe release path')
    path = source / relative
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit('Release hash mismatch: ' + name)
base = Path.home() / '.local/share/typixdeck/copilot-preview'
base.mkdir(parents=True, exist_ok=True)
if shutil.disk_usage(base).free < 16 * 1024 * 1024:
    raise SystemExit('At least 16 MiB free storage required')
target = base / version
if target.exists():
    if not (target / 'deployment.json').is_file() or (target / 'deployment.json').read_bytes() != (source / 'deployment.json').read_bytes():
        raise SystemExit('Existing version differs; keep it and increment the preview version')
    for name, digest in manifest['files'].items():
        if hashlib.sha256((target / name).read_bytes()).hexdigest() != digest:
            raise SystemExit('Installed bytes differ: ' + name)
else:
    staging = Path(tempfile.mkdtemp(prefix='.install-', dir=base))
    try:
        for name in manifest['files']:
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, destination)
        shutil.copy2(source / 'deployment.json', staging / 'deployment.json')
        (staging / 'start-preview.sh').chmod(0o755)
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging)
        raise

def write_managed(path, content, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and marker not in path.read_text()):
        raise SystemExit('Preserving existing unmanaged file: ' + str(path))
    if path.exists() and path.read_text() != content:
        backup = base / 'backups'
        backup.mkdir(exist_ok=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        shutil.copy2(path, backup / (path.name + '.' + digest))
    temporary = path.with_name(path.name + '.copilot-tmp')
    temporary.write_text(content)
    temporary.chmod(0o755)
    temporary.replace(path)

wrapper = Path.home() / '.local/bin/typix-copilot-preview'
write_managed(wrapper, '#!/bin/sh\n# TypixDeck managed Copilot preview\nexport COPILOT_PYTHON=/usr/bin/python3\nexec ' + shlex.quote(str(target / 'start-preview.sh')) + ' --fullscreen "$@"\n', 'TypixDeck managed Copilot preview')
desktop = Path(subprocess.check_output(['xdg-user-dir', 'DESKTOP'], text=True).strip())
if not desktop.is_absolute() or desktop == Path.home():
    raise SystemExit('Desktop directory unavailable')
entry = desktop / 'typix-copilot-preview.desktop'
write_managed(entry, f'''[Desktop Entry]
Type=Application
Name=Copilot 预览
Comment=TypixDeck 板载协处理器固件商店 · 预览模式
Exec={wrapper}
Icon={target}/icon.svg
Terminal=false
Categories=Utility;
StartupNotify=false
X-TypixDeck-Preview=true
X-TypixDeck-FullscreenAppId=ai.typixdeck.copilot.preview;
''', 'X-TypixDeck-Preview=true')
print(json.dumps({'installed':str(target), 'version':version, 'files':len(manifest['files']), 'shortcut':str(entry), 'root':False, 'firmware_written':False}))
