"""Privileged maintenance transaction. Signed artifacts, local backup, actual readback.

No GUI, downloader, shell, user paths, arbitrary serial commands, GPIO or I2C.
The CLI entry point enforces root-owned installed code/profile/catalog and isolation.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import select
import signal
import stat
import sys
import time
import termios
import uuid

from .authority import bundled_catalog, receive_authorization
from .core import inspect_local
from .device import Board, DeviceError, load_profile
from .diagnostics import MAX_LOG_EVENTS, MAX_LOG_BYTES, MAX_ELAPSED_MS, log_event

STATE_ROOT = Path('/var/lib/typix-copilot')
VENDOR_ROOT = Path('/usr/lib/typix-copilot/vendor')
COMMISSIONING_ROOT = Path('/run/typix-copilot')


class WriteError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_ERROR_TYPES = frozenset({
    'builtins.Exception', 'builtins.RuntimeError', 'builtins.ValueError',
    'builtins.TypeError', 'builtins.OSError', 'builtins.TimeoutError',
    'builtins.ConnectionError', 'builtins.BrokenPipeError',
    'builtins.ConnectionResetError', 'builtins.ConnectionAbortedError',
    'builtins.PermissionError', 'builtins.FileNotFoundError',
    'builtins.IsADirectoryError', 'builtins.NotADirectoryError',
    'builtins.BlockingIOError', 'builtins.EOFError', 'builtins.MemoryError',
    'builtins.AssertionError', 'builtins.IndexError', 'builtins.KeyError',
    'builtins.AttributeError', 'builtins.BufferError', 'builtins.UnicodeError',
    'builtins.UnicodeDecodeError', 'builtins.UnicodeEncodeError',
    'builtins.OverflowError', 'builtins.ZeroDivisionError',
    'builtins.StopIteration', 'builtins.RecursionError', 'builtins.NotImplementedError',
    'termios.error', 'serial.serialutil.SerialException',
    'serial.serialutil.SerialTimeoutException', 'serial.serialutil.PortNotOpenError',
    'esptool.util.FatalError', 'esptool.util.NotImplementedInROMError',
    'esptool.util.NotSupportedError', 'esptool.util.NANDProgramFailed',
    'esptool.util.NANDEraseFailed', 'esptool.util.UnsupportedCommandError',
    'typix_copilot.writer.WriteError', 'typix_copilot.device.DeviceError',
})
_ERROR_MODULES = frozenset({
    'builtins', 'termios', 'serial', 'serial.serialutil', 'serial.serialposix',
    'esptool', 'esptool.loader', 'esptool.cmds', 'esptool.util',
    'esptool.logger', 'esptool.reset', 'esptool.bin_image',
    'esptool.targets', 'esptool.targets.esp32', 'esptool.targets.esp32s3',
    'typix_copilot.writer', 'typix_copilot.device', 'typix_copilot.core',
})


def exception_diagnostics(exc):
    """Local structural diagnostics, without messages, paths, source or locals."""
    kind = type(exc)
    module, name = kind.__module__, kind.__name__
    name = module + '.' + name if type(module) is str and type(name) is str else 'unknown'
    frames = []
    traceback = exc.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get('__name__')
        if type(module) is str and module in _ERROR_MODULES:
            function = frame.f_code.co_name
            if not (function.isascii() and len(function) <= 80 and
                    (function.isidentifier() or function in {
                        '<module>', '<lambda>', '<listcomp>', '<dictcomp>', '<setcomp>', '<genexpr>'})):
                function = 'unknown'
            frames.append({'module': module, 'function': function, 'line': traceback.tb_lineno})
            frames = frames[-8:]
        traceback = traceback.tb_next
    result = {'error_type': name if name in _ERROR_TYPES else 'unknown', 'error_frames': frames}
    # Classify only known tool exceptions; never serialize raw messages, bytes,
    # paths or arbitrary user-defined __str__ implementations.
    if name in {'esptool.util.FatalError', 'serial.serialutil.SerialException',
                'serial.serialutil.SerialTimeoutException'}:
        args = exc.args
        message = args[0].lower() if args and type(args[0]) is str and len(args[0]) <= 4096 else ''
        for marker, category in [('serial data stream stopped', 'stream-stopped'),
            ('no serial data received', 'stream-stopped'), ('packet content transfer stopped', 'packet-stopped'),
            ('invalid head', 'slip-framing'), ('invalid slip escape', 'slip-escape'),
            ('corrupt data', 'corrupt-frame'), ('expected digest', 'digest-frame'),
            ('digest mismatch', 'digest-mismatch'), ('timed out', 'timeout'), ('timeout', 'timeout')]:
            if marker in message:
                result['error_category'] = category
                break
    if isinstance(exc, OSError) and type(exc.errno) is int and 0 <= exc.errno <= 4096:
        result['error_errno'] = exc.errno
    return result


def private_write(path, data, mode=0o600):
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        # Root runs with umask077. Public status intentionally needs mode0644;
        # private backup/image files remain0600 independently of caller umask.
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_directory(path, mode):
    path.mkdir(mode=mode, parents=False, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise WriteError('unsafe-state')
    os.chmod(path, mode)


def require_space(path, needed):
    stats = os.statvfs(path)
    if stats.f_bavail * stats.f_frsize < needed + 32 * 1024 * 1024:
        raise WriteError('low-storage')


def approved_firmware(identifier):
    """Resolve a bundled signed entry for local administrative tooling.

    Desktop requests use receive_authorization instead, preserving the exact
    signed snapshot selected by the user, including future catalog entries.
    """
    try:
        return next(fw for fw in bundled_catalog() if fw.id == identifier)
    except (ValueError, StopIteration):
        raise WriteError('firmware-not-approved') from None


def image_capacity(firmware, checked):
    """Require a complete S3 layout and return its actual Flash footprint."""
    if (firmware.chip != 'esp32s3' or firmware.board != 'typixdeck'
            or firmware.image_kind != 'merged-image' or firmware.flash_offset != 0
            or checked.get('kind') != 'merged-image' or checked.get('chip_id') != 9
            or checked.get('sha256') != firmware.sha256 or checked.get('size') != firmware.size
            or not checked.get('partitions')):
        raise WriteError('image-mismatch')
    required = max(firmware.size, *(row['offset'] + row['size'] for row in checked['partitions']))
    if checked.get('declared_flash_bytes') is not None:
        required = max(required, checked['declared_flash_bytes'])
    return required


def receive_image(source, firmware, path):
    """Bounded untrusted stdin, before serial open. Never accept a user path."""
    data = bytearray()
    deadline = time.monotonic() + 30
    while len(data) <= firmware.size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WriteError('input-timeout')
        if not select.select([source], [], [], remaining)[0]:
            raise WriteError('input-timeout')
        block = os.read(source.fileno(), min(65536, firmware.size + 1 - len(data)))
        if not block:
            break
        data.extend(block)
    if len(data) != firmware.size or hashlib.sha256(data).hexdigest() != firmware.sha256:
        raise WriteError('image-mismatch')
    private_write(path, data)
    try:
        image_capacity(firmware, inspect_local(path))
    except ValueError:
        raise WriteError('image-mismatch') from None
    return bytes(data)


class Journal:
    def __init__(self, root, firmware, sink):
        self.root, self.sink = root, sink
        self.firmware = firmware
        self.identifier = uuid.uuid4().hex
        self.job = root / 'private' / self.identifier
        ensure_directory(root, 0o755)
        ensure_directory(root / 'private', 0o700)
        ensure_directory(root / 'logs', 0o755)
        ensure_directory(self.job, 0o700)
        self.previous = []
        try:
            self.previous = json.loads((root / 'status.json').read_text())['records'][:19]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.record = dict(job_id=self.identifier, firmware_id=firmware.id, version=firmware.version,
                           image_sha256=firmware.sha256, image_size=firmware.size,
                           status='running', phase='prepare', progress=0.0,
                           backup_complete=False, write_started=False, verified=False,
                           reconnected=False, runtime_version_confirmed=False,
                           boot_requested=False, timestamp=int(time.time()),
                           started_at=int(time.time()), elapsed_ms=0)
        self.started = time.monotonic()
        self.timeline = []
        self.log_truncated = False
        self.last_log_key = None
        self.last_save = 0
        self.last_phase = None
        self.last_sent = None
        self.hardware_started = False

    def emit(self, phase, progress, *, durable=False, **fields):
        if phase != self.record['phase']:
            for key in ('attempted_offset', 'last_checked_bytes', 'read_bytes', 'read_total_bytes',
                        'chunk_received_bytes', 'chunk_requested_bytes', 'last_packet_elapsed_ms'):
                if key not in fields:
                    self.record.pop(key, None)
        self.record.update(phase=phase, progress=max(0., min(1., progress)), **fields)
        now = time.monotonic()
        self.record['elapsed_ms'] = max(0, min(MAX_ELAPSED_MS, int((now - self.started) * 1000)))
        self.record['timestamp'] = int(time.time())
        event = log_event(self.record)
        log_key = (phase, int(self.record['progress'] * 100), self.record['status'],
                   self.record['backup_complete'], self.record['write_started'], self.record['verified'],
                   self.record['reconnected'], self.record.get('error_type'), self.record.get('cleanup_error_type'))
        if log_key != self.last_log_key:
            if len(self.timeline) >= MAX_LOG_EVENTS:
                del self.timeline[1]
                self.log_truncated = True
            self.timeline.append(event)
            self.last_log_key = log_key
        if durable or phase != self.last_phase or now - self.last_save >= 2 or self.record['status'] != 'running':
            encoded = json.dumps(self.record, sort_keys=True).encode()
            try:
                private_write(self.job / 'result.json', encoded)
                public = json.dumps({'schema': 1, 'records': [self.record, *self.previous]}).encode()
                private_write(self.root / 'status.json', public, 0o644)
                detail = {'schema': 1, 'identity': {key: self.record[key] for key in
                          ('job_id', 'firmware_id', 'version', 'image_sha256', 'image_size')},
                          'events': self.timeline, 'truncated': self.log_truncated}
                log_bytes = json.dumps(detail, sort_keys=True).encode()
                while len(log_bytes) > MAX_LOG_BYTES and len(self.timeline) > 2:
                    del self.timeline[1]
                    self.log_truncated = detail['truncated'] = True
                    log_bytes = json.dumps(detail, sort_keys=True).encode()
                private_write(self.root / 'logs' / (self.identifier + '.json'), log_bytes, 0o644)
                if self.record['status'] != 'running':
                    self._prune_logs()
                self.last_save, self.last_phase = now, phase
            except OSError:
                if not self.hardware_started:
                    raise
                # Failure of progress logging after the durable start checkpoint
                # must not interrupt flash writes or readback/reset recovery.
                self.record['audit_degraded'] = True
                # A separate detail-file failure may leave space for the compact
                # status. Best effort once per file; never retry the serial transaction.
                for target, payload, mode in (
                    (self.job / 'result.json', self.record, 0o600),
                    (self.root / 'status.json', {'schema': 1, 'records': [self.record, *self.previous]}, 0o644),
                ):
                    try:
                        private_write(target, json.dumps(payload, sort_keys=True).encode(), mode)
                    except OSError:
                        pass
        key = (phase, int(self.record['progress'] * 100), self.record['status'])
        if key != self.last_sent:
            self.last_sent = key
            try:
                self.sink(dict(self.record))
            except (BrokenPipeError, OSError):
                # A lost GUI must not interrupt a write. Status is durable on the device.
                pass

    def _prune_logs(self):
        import re
        retained = {row.get('job_id') for row in [self.record, *self.previous]}
        for path in (self.root / 'logs').iterdir():
            if re.fullmatch(r'[0-9a-f]{32}\.json', path.name) and path.stem not in retained:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1:
                    path.unlink()


def port_in_use(endpoint, proc=Path('/proc')):
    for process in proc.iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            for fd in (process / 'fd').iterdir():
                try:
                    info = fd.stat()
                    if stat.S_ISCHR(info.st_mode) and info.st_rdev == endpoint.device_number:
                        return True
                except OSError:
                    pass
        except OSError:
            pass
    return False


def open_bound_serial(board, endpoint):
    import serial
    board.same(endpoint)
    if port_in_use(endpoint):
        raise WriteError('port-busy')
    connection = serial.Serial(port=None, baudrate=115200, timeout=.2, write_timeout=3, exclusive=True)
    connection.dtr = False
    connection.rts = False
    connection.port = str(endpoint.port)
    try:
        connection.open()  # pyserial uses O_NOCTTY | O_NONBLOCK and configures raw/no echo.
        if os.fstat(connection.fileno()).st_rdev != endpoint.device_number:
            raise WriteError('target-changed')
        fcntl.ioctl(connection.fileno(), termios.TIOCEXCL)
        if port_in_use(endpoint):
            raise WriteError('port-busy')
        board.same(endpoint)
        return connection
    except Exception:
        connection.close()
        raise


def safe_power_config(line):
    import re
    # Keep only the explicit maintenance field; all other UART data is discarded.
    match = re.fullmatch(rb'\s*CONFIG_P0\s+\[0x04\]\s+boot=0x[0-9A-Fa-f]{2}\s+live=0x([0-9A-Fa-f]{2})(?:\s+.*)?', line)
    return match is not None and int(match[1], 16) & 0x0C == 0x0C


def maintenance_line(raw):
    """Classify bounded CDC text without retaining keyboard/audio diagnostics."""
    ambiguous = bool(raw) and (not raw.endswith(b'\n') or len(raw) >= 512 or
                              any(c == 127 or (c < 32 and c not in (9, 10, 13)) for c in raw))
    try:
        raw.decode('utf-8', errors='strict')
    except UnicodeError:
        ambiguous = True
    line = raw.strip()
    aw = any(marker in line for marker in (
        b'AW_', b'INPUT_P', b'OUTPUT_P', b'CONFIG_P', b'INT_P', b'GCR',
        b'LEDMODE_P', b'READ_FAIL', b'HP_DET(P1_7)', b'DAC_3V3_EN(P1_0)'))
    ambiguous |= b'READ_FAIL' in line or b'SCREENSHOT' in line or b'SCRN' in line
    return line, aw, ambiguous


def commissioning_permit(journal, board, consume=False):
    """An administrator's short-lived, single-use approval for a first upgrade.

    Never installed by the package or created by the GUI. Silence is not evidence
    of safe power: this records the user's explicitly authorized experiment.
    """
    path = COMMISSIONING_ROOT / 'commissioning.json'
    try:
        parent = COMMISSIONING_ROOT.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o077:
            raise WriteError('commissioning-denied')
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > 2048):
            raise WriteError('commissioning-denied')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise WriteError('commissioning-denied')
            permit = json.loads(stream.read(2049))
        now = int(time.time())
        monotonic = time.monotonic()
        actor = os.environ.get('PKEXEC_UID', '')
        if not actor or not actor.isascii() or not actor.isdecimal() or len(actor) > 10:
            raise WriteError('commissioning-denied')
        actor_uid = int(actor)
        if not 0 <= actor_uid < 2**32 - 1:
            raise WriteError('commissioning-denied')
        firmware = journal.firmware
        if (permit.get('schema') != 1 or permit.get('active') is not True
                or permit.get('purpose') != 'authorized-first-upgrade-with-power-unverified'
                or permit.get('profile') != board.profile
                or permit.get('firmware_id') != firmware.id or permit.get('sha256') != firmware.sha256
                or permit.get('size') != firmware.size
                or not isinstance(permit.get('nonce'), str) or len(permit['nonce']) != 32
                or any(c not in '0123456789abcdef' for c in permit['nonce'])
                or type(permit.get('uid')) is not int or permit['uid'] != actor_uid
                or type(permit.get('issued')) is not int or type(permit.get('expires')) is not int
                or not permit['issued'] <= now <= permit['expires']
                or not 0 < permit['expires'] - permit['issued'] <= 300
                or type(permit.get('monotonic_issued')) not in (int, float)
                or type(permit.get('monotonic_expires')) not in (int, float)
                or not permit['monotonic_issued'] <= monotonic <= permit['monotonic_expires']
                or not 0 < permit['monotonic_expires'] - permit['monotonic_issued'] <= 300):
            raise WriteError('commissioning-denied')
        if consume:
            permit.update(active=False, consumed_by=journal.identifier, consumed_at=now)
            private_write(path, json.dumps(permit).encode())
            private_write(journal.job / 'commissioning.json', json.dumps(permit).encode())
    except (OSError, ValueError, KeyError, TypeError):
        raise WriteError('commissioning-denied') from None


class SerialTransport:
    def __init__(self, board, journal):
        self.board, self.journal = board, journal
        self.esp = None
        self.connection = None
        self.stage = 'connect'
        self._prepare_tool()

    def _prepare_tool(self):
        # esptool must already be imported under the fixed root vendor path.
        import esptool
        from esptool.logger import TemplateLogger, log
        if esptool.__version__ != '5.3.1':
            raise WriteError('tool-version')
        target = self

        class FixedLogger(TemplateLogger):
            def print(self, *a, **k): pass
            def note(self, *a, **k): pass
            def warning(self, *a, **k): pass
            def error(self, *a, **k): pass
            def stage(self, *a, **k): pass
            def set_verbosity(self, *a, **k): pass
            def progress_bar(self, cur_iter, total_iters, **kwargs):
                if target.stage == 'write' and total_iters:
                    target.journal.emit('write', .45 + .30 * cur_iter / total_iters)

        log.set_logger(FixedLogger())

    def enter(self):
        endpoint = self.board.probe()
        if endpoint.mode == 'rom':
            try:
                ownership = json.loads((self.journal.root / 'private/maintenance.json').read_text())
            except (OSError, ValueError):
                ownership = {}
            if ownership.get('active') is not True or ownership.get('profile') != self.board.profile:
                raise WriteError('rom-not-owned')
            if ownership.get('exception_used'):
                raise WriteError('rom-recovery-required')
            self.journal.emit('enter', .12, boot_requested=True)
            return endpoint
        with open_bound_serial(self.board, endpoint) as connection:
            # TinyUSB sends AW_DUMP only when its CDC host has asserted DTR.
            # This is the audited native USB runtime, not a UART GPIO reset bridge.
            connection.dtr = True
            connection.reset_input_buffer()
            connection.write(b'\nAW_DUMP\n')
            connection.flush()
            deadline = time.monotonic() + 5
            safe = False
            aw_observed = False
            boot_advertised = False
            ambiguous = False
            total = 0
            pending = bytearray()
            while time.monotonic() < deadline and total < 32768:
                fragment = connection.read_until(b'\n', 512 - len(pending))
                total += len(fragment)
                if total >= 32768:
                    raise WriteError('power-state-unverified')
                pending.extend(fragment)
                if len(pending) >= 512:
                    raise WriteError('power-state-unverified')
                if not pending.endswith(b'\n'):
                    continue
                raw = bytes(pending)
                pending.clear()
                line, aw, malformed = maintenance_line(raw)
                ambiguous |= malformed or line == b'--- END ---'
                aw_observed |= aw
                boot_advertised |= b'REBOOT_TO_BOOT_MODE' in line
                if b'CONFIG_P0' in line and not safe_power_config(line):
                    raise WriteError('power-state-unverified')
                if not ambiguous and safe_power_config(line):
                    safe = True
                    break
            if not safe:
                if aw_observed or ambiguous or pending or total >= 32768:
                    raise WriteError('power-state-unverified')
                # No AW response is ambiguous; only an explicit one-time admin
                # approval may accept it. Never apply this to a bad AW response.
                commissioning_permit(self.journal, self.board)
                connection.write(b'\nAUDIO_DUMP\n')
                connection.flush()
                audio_reply = False
                audio_end = False
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline and total < 32768:
                    fragment = connection.read_until(b'\n', 512 - len(pending))
                    total += len(fragment)
                    if total >= 32768:
                        raise WriteError('power-state-unverified')
                    pending.extend(fragment)
                    if len(pending) >= 512:
                        raise WriteError('power-state-unverified')
                    if not pending.endswith(b'\n'):
                        continue
                    raw = bytes(pending)
                    pending.clear()
                    line, aw, malformed = maintenance_line(raw)
                    ambiguous |= malformed
                    aw_observed |= aw
                    boot_advertised |= b'REBOOT_TO_BOOT_MODE' in line
                    if line == b'--- AUDIO_DUMP ---':
                        ambiguous |= audio_reply
                        audio_reply = True
                    if line == b'--- END ---':
                        ambiguous |= not audio_reply or audio_end
                        audio_end = True
                if aw_observed or ambiguous or pending or total >= 32768 or not audio_end or not boot_advertised:
                    raise WriteError('power-state-unverified')
                self.board.same(endpoint)
                commissioning_permit(self.journal, self.board, consume=True)
                self.journal.emit('enter', .11, durable=True, power_state_verified=False, exception_used=True)
            else:
                self.journal.emit('enter', .11, power_state_verified=True, exception_used=False)
            self.board.same(endpoint)
            private_write(self.journal.root / 'private/maintenance.json', json.dumps({
                'active': True, 'profile': self.board.profile,
                'exception_used': self.journal.record.get('exception_used', False),
                'origin_job': self.journal.identifier}).encode())
            self.journal.emit('enter', .12, durable=True, boot_requested=True)
            connection.write(b'EGGFLY_REBOOT_TO_BOOT_MODE\n')
            connection.flush()
        return self.board.wait('rom', 20)

    def connect(self, endpoint):
        from esptool.targets.esp32s3 import ESP32S3ROM
        from esptool.cmds import run_stub, attach_flash, detect_flash_size
        self.connection = open_bound_serial(self.board, endpoint)
        esp = ESP32S3ROM(self.connection, baud=115200, trace_enabled=False)
        self.esp = esp
        # No generic detection/reset fallback. Verify this ROM's own chip ID first.
        esp.connect(mode='no-reset', attempts=3, detecting=True, warnings=False)
        security = esp.get_security_info(cache=False)
        flags = security['parsed_flags']
        if security['chip_id'] != 9:
            raise WriteError('chip-mismatch')
        if (flags['SECURE_BOOT_EN'] or flags['SECURE_DOWNLOAD_ENABLE'] or
                security['flash_crypt_cnt'] != 0):
            raise WriteError('security-enabled')
        if esp.get_secure_boot_enabled() or esp.get_flash_encryption_enabled():
            raise WriteError('security-enabled')
        esp.secure_download_mode = False
        esp._post_connect()
        esp = run_stub(esp)
        self.esp = esp
        if not esp.IS_STUB:
            raise WriteError('stub-unavailable')
        # esptool's automatic write retry reopens a tty and applies a generic
        # reset. Our transaction must instead fail without ever rebinding it.
        esp.WRITE_FLASH_ATTEMPTS = 1
        attach_flash(esp)
        capacity = detect_flash_size(esp)
        if capacity not in {'8MB', '16MB'}:
            raise WriteError('flash-size')
        from esptool.cmds import _set_flash_parameters
        _set_flash_parameters(esp, capacity)
        esp.change_baud(921600)
        self.board.same(endpoint)
        return int(capacity[:-2]) * 1024 * 1024

    def read(self, size, phase):
        self.stage = phase
        self.attempted_offset = 0
        self.last_checked_bytes = 0
        low, width = (.18, .25) if phase == 'backup' else (.77, .16)
        data = bytearray()
        for offset in range(0, size, 64 * 1024):
            amount = min(64 * 1024, size - offset)
            # Wait for the whole checked read, including its final digest,
            # before journal I/O. The packet callback below updates memory only.
            self.attempted_offset = offset
            self.chunk_received_bytes = 0
            self.chunk_requested_bytes = amount
            self.last_packet_time = time.monotonic()
            def packet_progress(received, length, read_offset):
                # Called after packet ACK: memory assignments only. Logging,
                # hashing and callbacks to the GUI happen outside this stream.
                if (type(received) is int and 0 <= received <= amount
                        and length == amount and read_offset == offset):
                    self.chunk_received_bytes = received
                    self.last_packet_time = time.monotonic()
            block = self.esp.read_flash(offset, amount, progress_fn=packet_progress)
            if len(block) != amount:
                raise WriteError('readback-length')
            self.last_checked_bytes = offset + amount
            data.extend(block)
            self.journal.emit(phase, low + width * len(data) / size, read_bytes=len(data),
                              read_total_bytes=size, attempted_offset=offset, last_checked_bytes=len(data))
        if len(data) != size:
            raise WriteError('readback-length')
        return bytes(data)

    def write(self, data):
        from esptool.cmds import write_flash
        self.stage = 'write'
        write_flash(self.esp, [(0, data)], flash_freq='keep', flash_mode='keep',
                    flash_size='keep', erase_all=False, force=False, encrypt=False,
                    compress=True, no_progress=False)

    def restart(self):
        # The runtime command explicitly set FORCE_DOWNLOAD_BOOT. Clear only
        # that documented RTC bit; never use a generic DTR/RTS reset fallback.
        mask = self.esp.RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK
        self.esp.write_reg(self.esp.RTC_CNTL_OPTION1_REG, 0, mask)
        if self.esp.read_reg(self.esp.RTC_CNTL_OPTION1_REG) & mask:
            raise WriteError('restart-mode')
        self.esp.watchdog_reset()
        self.close()
        self.board.wait('runtime', 35)
        private_write(self.journal.root / 'private/maintenance.json', b'{"active": false}')

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None


def execute(firmware, data, journal, transport):
    """The same state machine is tested with a fake transport, never fake in production."""
    try:
        if journal.firmware != firmware:
            raise WriteError('image-mismatch')
        # Recheck the root-staged image before any serial operation. In
        # particular a short merged file may declare partitions beyond its end.
        try:
            required = image_capacity(firmware, inspect_local(journal.job / 'image.bin'))
        except ValueError:
            raise WriteError('image-mismatch') from None
        if len(data) != firmware.size or hashlib.sha256(data).hexdigest() != firmware.sha256:
            raise WriteError('image-mismatch')
        journal.emit('enter', .11)
        endpoint = transport.enter()
        journal.emit('connect', .15)
        capacity = transport.connect(endpoint)
        if type(capacity) is not int or capacity < required:
            raise WriteError('flash-size')
        require_space(journal.job, capacity + len(data))
        journal.emit('backup', .18, flash_capacity=capacity, read_bytes=0, read_total_bytes=capacity)
        backup = transport.read(capacity, 'backup')
        if len(backup) != capacity:
            raise WriteError('backup-incomplete')
        backup_path = journal.job / 'flash-backup.bin'
        private_write(backup_path, backup)
        digest = hashlib.sha256(backup).hexdigest()
        if hashlib.sha256(backup_path.read_bytes()).hexdigest() != digest:
            raise WriteError('backup-incomplete')
        private_write(journal.job / 'backup.json', json.dumps({'bytes': capacity, 'sha256': digest}).encode())
        del backup
        journal.emit('backup', .44, backup_complete=True, backup_bytes=capacity)
        # Durable before erase/write, including GUI disappearance/power-loss cases.
        journal.emit('write', .45, write_started=True)
        journal.hardware_started = True
        transport.write(data)
        journal.emit('verify', .77)
        actual = transport.read(len(data), 'verify')
        if actual != data or hashlib.sha256(actual).hexdigest() != firmware.sha256:
            raise WriteError('verify-mismatch')
        journal.emit('restart', .95, verified=True, verified_bytes=len(data), image_sha256=firmware.sha256)
        transport.restart()
        journal.emit('complete', 1., status='succeeded', reconnected=True)
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, (WriteError, DeviceError)) else 'transport-failed'
        failed_phase = journal.record['phase']
        diagnostics = exception_diagnostics(exc)
        if failed_phase in {'backup', 'verify'} and getattr(transport, 'stage', None) == failed_phase:
            for field in ('attempted_offset', 'last_checked_bytes', 'chunk_received_bytes', 'chunk_requested_bytes'):
                value = getattr(transport, field, None)
                if type(value) is int and 0 <= value <= 16 * 1024 * 1024:
                    diagnostics[field] = value
            if hasattr(transport, 'last_packet_time'):
                diagnostics['last_packet_elapsed_ms'] = max(0, min(MAX_ELAPSED_MS,
                    int((time.monotonic() - transport.last_packet_time) * 1000)))
            diagnostics['awaiting_digest'] = (getattr(transport, 'chunk_requested_bytes', 0) > 0
                and getattr(transport, 'chunk_received_bytes', -1) == transport.chunk_requested_bytes
                and diagnostics.get('error_category') not in {'digest-mismatch', 'digest-frame'})
        journal.emit('failed', journal.record['progress'], status='failed', code=code,
                     failed_phase=failed_phase, **diagnostics)
        return 1
    finally:
        try:
            transport.close()
        except Exception as exc:
            # A failed close must not replace durable verification/failure or
            # make the caller report an already completed transaction as lost.
            try:
                journal.emit(journal.record['phase'], journal.record['progress'], durable=True,
                             audit_degraded=True, cleanup_error_type=exception_diagnostics(exc)['error_type'])
            except Exception:
                pass


def main():
    output = sys.stdout
    output_fd = output.fileno()
    os.set_blocking(output_fd, False)
    def sink(event):
        # Each event is below PIPE_BUF: nonblocking atomic writes prevent a hung
        # desktop reader from stalling the serial protocol or a critical write.
        data = (json.dumps(event, sort_keys=True) + '\n').encode()
        try:
            os.write(output_fd, data)
        except (BlockingIOError, BrokenPipeError):
            pass
    journal = None
    lock = None
    firmware = None
    identifier = ''
    try:
        if os.geteuid() != 0 or not sys.flags.isolated:
            raise WriteError('authorization-required')
        if len(sys.argv) != 2:
            raise WriteError('invalid-request')
        identifier = sys.argv[1]
        try:
            firmware = receive_authorization(sys.stdin.buffer, identifier)
        except ValueError:
            raise WriteError('firmware-not-approved') from None
        # Drop influence from desktop/user tool configuration before importing esptool.
        for key in list(os.environ):
            if key.startswith(('ESPTOOL', 'PYTHON')) or key in {'XDG_CONFIG_HOME', 'XDG_CONFIG_DIRS'}:
                del os.environ[key]
        os.environ['HOME'] = '/root'
        os.environ['ESPTOOL_CFGFILE'] = '/etc/typix-copilot/esptool.cfg'
        os.chdir('/')
        os.umask(0o077)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            os.setsid()
        except PermissionError:
            pass
        descriptor = os.open('/run/lock/typix-copilot-write.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        lock = os.fdopen(descriptor, 'a+b')
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
            raise WriteError('unsafe-lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WriteError('busy') from None
        board = Board(load_profile())
        board.probe()
        journal = Journal(STATE_ROOT, firmware, sink)
        journal.emit('prepare', .10)
        require_space(journal.job, 64 * 1024 * 1024)
        data = receive_image(sys.stdin.buffer, firmware, journal.job / 'image.bin')
        sys.path.insert(0, str(VENDOR_ROOT))
        # Suppress raw tool diagnostics, including unique identifiers and UART bytes.
        with open(os.devnull, 'w') as discard, contextlib.redirect_stdout(discard), contextlib.redirect_stderr(discard):
            transport = SerialTransport(board, journal)
            return execute(firmware, data, journal, transport)
    except Exception as exc:
        code = exc.code if isinstance(exc, (WriteError, DeviceError)) else 'preflight-failed'
        if journal:
            journal.emit('failed', journal.record['progress'], status='failed', code=code)
        elif firmware is not None:
            sink(dict(status='failed', phase='failed', progress=0., code=code,
                      firmware_id=firmware.id, version=firmware.version,
                      image_sha256=firmware.sha256, image_size=firmware.size,
                      backup_complete=False, write_started=False, verified=False,
                      reconnected=False, runtime_version_confirmed=False))
        else:
            # No image identity is trusted yet; do not invent a version/hash or
            # accidentally attribute a rejection to a default official image.
            safe_identifier = identifier if (isinstance(identifier, str) and len(identifier) <= 96
                and identifier.isascii() and all(c.isalnum() or c in '._-' for c in identifier)) else ''
            sink(dict(status='failed', phase='failed', progress=0.,
                      code='firmware-not-approved', firmware_id=safe_identifier,
                      authorization_rejected=True))
        return 1
    finally:
        if lock:
            try:
                lock.close()
            except OSError:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
