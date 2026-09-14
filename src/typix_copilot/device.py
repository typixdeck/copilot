"""Passive, topology-bound discovery. No serial open or hardware commands here."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import stat
import time


class DeviceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Endpoint:
    mode: str
    port: Path
    sys_device: Path
    device_number: int


class Board:
    def __init__(self, profile, sysfs=Path('/sys'), dev=Path('/dev')):
        self.profile = profile
        self.sysfs, self.dev = Path(sysfs), Path(dev)
        if not re.fullmatch(r'[0-9]+-[0-9]+\.[0-9]+', profile['usb_path']):
            raise DeviceError('board-profile')
        if profile.get('schema') != 1 or profile.get('board') != 'typixdeck-0720':
            raise DeviceError('board-profile')

    @staticmethod
    def read(path):
        return path.read_text(encoding='ascii').strip()

    def probe(self):
        try:
            topology = self.profile['usb_path']
            node = self.sysfs / 'bus/usb/devices' / topology
            physical = node.resolve(strict=True)
            if self.profile['controller'] not in physical.parts:
                raise DeviceError('board-mismatch')
            parent = node.parent / topology.rsplit('.', 1)[0]
            if [self.read(parent / k) for k in ('idVendor', 'idProduct')] != self.profile['hub']:
                raise DeviceError('board-mismatch')
            pair = [self.read(node / k) for k in ('idVendor', 'idProduct')]
            if pair == ['303a', '80c3']:
                mode = 'runtime'
                if self.read(node / 'product') != 'TypixDeck UAC+CDC':
                    raise DeviceError('board-mismatch')
            elif pair in (['303a', '1001'], ['303a', '0009']):
                mode = 'rom'
            else:
                raise DeviceError('board-mismatch')
            matches = []
            for tty in (self.sysfs / 'class/tty').glob('ttyACM*'):
                interface = (tty / 'device').resolve(strict=True)
                if physical not in interface.parents:
                    continue
                # Must be a direct USB interface on this device, not a nested hub.
                if not any(p.name.startswith(topology + ':') and p.parent == physical
                           for p in (interface, *interface.parents)):
                    continue
                port = self.dev / tty.name
                info = port.lstat()
                if not stat.S_ISCHR(info.st_mode):
                    raise DeviceError('port-invalid')
                matches.append(Endpoint(mode, port, physical, info.st_rdev))
            if len(matches) != 1:
                raise DeviceError('port-ambiguous' if matches else 'port-missing')
            return matches[0]
        except (OSError, ValueError, KeyError):
            raise DeviceError('device-missing') from None

    def wait(self, mode, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                endpoint = self.probe()
                if endpoint.mode == mode:
                    return endpoint
            except DeviceError:
                pass
            time.sleep(.25)
        raise DeviceError('reconnect-timeout' if mode == 'runtime' else 'rom-timeout')

    def same(self, endpoint):
        current = self.probe()
        if current != endpoint:
            raise DeviceError('target-changed')


def load_profile(path=Path('/etc/typix-copilot/board.json')):
    # Authorization lives in a root-owned profile, separate from untrusted catalogs.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise DeviceError('board-profile')
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise DeviceError('board-profile')
    if path.stat().st_size > 4096:
        raise DeviceError('board-profile')
    return json.loads(path.read_text())


def passive_status():
    try:
        endpoint = Board(load_profile()).probe()
        return {'connected': True, 'mode': endpoint.mode}
    except (DeviceError, OSError, ValueError):
        return {'connected': False, 'mode': 'unknown'}
