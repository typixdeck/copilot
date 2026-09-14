"""Maintenance ordering/failure tests: fake transport only, never open serial."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock, call
from types import SimpleNamespace
import sys
import termios

from typix_copilot.core import load_catalog
from typix_copilot.device import Board, DeviceError
from typix_copilot.writer import (Journal, WriteError, approved_firmware, execute,
                                exception_diagnostics, receive_image, safe_power_config, SerialTransport)


class Transport:
    def __init__(self, data, fail=None):
        self.data, self.fail, self.calls = data, fail, []

    def call(self, stage):
        self.calls.append(stage)
        if stage == self.fail:
            raise WriteError('test-failure')

    def enter(self): self.call('enter'); return 'bound-port'
    def connect(self, endpoint): self.call('connect'); return 8192
    def read(self, size, phase):
        self.call(phase)
        return b'X' * size if phase == 'backup' or self.fail == 'mismatch' else self.data
    def write(self, data): self.call('write'); assert data == self.data
    def restart(self): self.call('restart')
    def close(self): self.calls.append('close')


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'state'
        self.data = b'real-test-byte-sequence'
        self.fw = replace(load_catalog()[0], size=len(self.data), sha256=hashlib.sha256(self.data).hexdigest())
        self.events = []
        self.journal = Journal(self.root, self.fw, self.events.append)

    def tearDown(self): self.tmp.cleanup()

    def run_transaction(self, fail=None):
        transport = Transport(self.data, fail)
        result = execute(self.fw, self.data, self.journal, transport)
        return result, transport.calls

    def test_success_requires_backup_write_readback_restart_in_order(self):
        result, calls = self.run_transaction()
        self.assertEqual(result, 0)
        self.assertEqual(calls, ['enter', 'connect', 'backup', 'write', 'verify', 'restart', 'close'])
        final = self.events[-1]
        for flag in ['backup_complete', 'write_started', 'verified', 'reconnected']:
            self.assertTrue(final[flag])
        self.assertFalse(final['runtime_version_confirmed'])
        self.assertEqual(final['status'], 'succeeded')
        backup = self.journal.job / 'flash-backup.bin'
        self.assertEqual(backup.read_bytes(), b'X' * 8192)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        public = json.loads((self.root / 'status.json').read_text())
        self.assertNotIn('sha256', public['records'][0])
        self.assertNotIn('backup_path', public['records'][0])
        self.assertEqual((self.root / 'status.json').stat().st_mode & 0o777, 0o644)

    def test_no_write_on_prewrite_failures(self):
        for phase in ['enter', 'connect', 'backup']:
            with self.subTest(phase=phase):
                result, calls = self.run_transaction(phase)
                self.assertEqual(result, 1)
                self.assertNotIn('write', calls)
                self.assertNotIn('restart', calls)

    def test_read_failure_preserves_safe_diagnostics_and_existing_failure_behavior(self):
        secret = '/dev/SECRET_DEVICE serial=SECRET_ID bytes=SECRET_UART'
        transport = SerialTransport.__new__(SerialTransport)
        transport.enter = MagicMock(return_value='bound-port')
        transport.connect = MagicMock(return_value=3 * 65536)
        transport.esp = MagicMock()
        transport.esp.read_flash.side_effect = [b'X' * 65536, OSError(secret)]
        transport.journal = self.journal
        transport.write = MagicMock()
        transport.restart = MagicMock()
        transport.close = MagicMock()
        self.assertEqual(execute(self.fw, self.data, self.journal, transport), 1)
        final = self.events[-1]
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['code'], 'transport-failed')
        self.assertEqual(final['failed_phase'], 'backup')
        self.assertEqual(final['error_type'], 'builtins.OSError')
        self.assertEqual(final['attempted_offset'], 65536)
        self.assertEqual(final['last_checked_bytes'], 65536)
        self.assertEqual([frame['function'] for frame in final['error_frames']], ['execute', 'read'])
        self.assertFalse(final['write_started'])
        self.assertFalse(final['backup_complete'])
        self.assertEqual(transport.esp.method_calls,
                         [call.read_flash(0, 65536), call.read_flash(65536, 65536)])
        transport.write.assert_not_called()
        transport.restart.assert_not_called()
        transport.close.assert_called_once_with()
        private = (self.journal.job / 'result.json').read_text()
        public = (self.root / 'status.json').read_text()
        for encoded in (private, public, json.dumps(self.events)):
            self.assertNotIn('SECRET', encoded)
        self.assertEqual(json.loads(private), final)
        self.assertEqual(json.loads(public)['records'][0], final)

    def test_unknown_exception_type_does_not_format_or_leak_its_message(self):
        def forbidden_string(self):
            raise AssertionError('exception formatting is forbidden')
        unknown = type('SECRET_NAME', (Exception,),
                       {'__module__': 'SECRET_MODULE', '__str__': forbidden_string})
        transport = Transport(self.data)
        transport.enter = MagicMock(side_effect=unknown('SECRET_MESSAGE'))
        self.assertEqual(execute(self.fw, self.data, self.journal, transport), 1)
        final = self.events[-1]
        self.assertEqual(final['error_type'], 'unknown')
        self.assertEqual(final['code'], 'transport-failed')
        self.assertEqual(final['failed_phase'], 'enter')
        self.assertEqual(transport.calls, ['close'])
        self.assertNotIn('attempted_offset', final)
        self.assertNotIn('last_checked_bytes', final)
        self.assertNotIn('SECRET', (self.root / 'status.json').read_text())

    def test_project_error_code_and_type_remain_separate(self):
        result, _ = self.run_transaction('backup')
        self.assertEqual(result, 1)
        self.assertEqual(self.events[-1]['code'], 'test-failure')
        self.assertEqual(self.events[-1]['error_type'], 'typix_copilot.writer.WriteError')
        self.assertNotIn('attempted_offset', self.events[-1])

    def test_read_counters_are_not_reported_for_another_failed_phase(self):
        transport = Transport(self.data, 'write')
        transport.stage = 'backup'
        transport.attempted_offset, transport.last_checked_bytes = 65536, 8192
        self.assertEqual(execute(self.fw, self.data, self.journal, transport), 1)
        final = self.events[-1]
        self.assertEqual(final['failed_phase'], 'write')
        self.assertNotIn('attempted_offset', final)
        self.assertNotIn('last_checked_bytes', final)

    def test_backup_disk_failure_prevents_write(self):
        from typix_copilot import writer
        original = writer.private_write
        def fail_backup(path, *args, **kwargs):
            if path.name == 'flash-backup.bin': raise OSError('no space')
            return original(path, *args, **kwargs)
        with patch.object(writer, 'private_write', fail_backup):
            result, calls = self.run_transaction()
        self.assertEqual(result, 1)
        self.assertNotIn('write', calls)
        self.assertFalse(self.events[-1]['backup_complete'])

    def test_corrupt_readback_never_restarts_or_succeeds(self):
        result, calls = self.run_transaction('mismatch')
        self.assertEqual(result, 1)
        self.assertNotIn('restart', calls)
        self.assertEqual(self.events[-1]['code'], 'verify-mismatch')
        self.assertFalse(self.events[-1]['verified'])
        self.assertTrue(self.events[-1]['backup_complete'])

    def test_write_failure_retains_backup_and_no_restart(self):
        result, calls = self.run_transaction('write')
        self.assertEqual(result, 1)
        self.assertNotIn('verify', calls)
        self.assertNotIn('restart', calls)
        self.assertTrue(self.events[-1]['write_started'])
        self.assertTrue((self.journal.job / 'flash-backup.bin').exists())

    def test_verified_without_reconnect_is_not_success(self):
        result, _ = self.run_transaction('restart')
        self.assertEqual(result, 1)
        self.assertTrue(self.events[-1]['verified'])
        self.assertFalse(self.events[-1]['reconnected'])
        self.assertEqual(self.events[-1]['status'], 'failed')

    def test_gui_pipe_loss_does_not_abort_maintenance(self):
        def broken(_): raise BrokenPipeError()
        self.journal.sink = broken
        self.assertEqual(self.run_transaction()[0], 0)
        self.assertEqual(json.loads((self.root / 'status.json').read_text())['records'][0]['status'], 'succeeded')

    def test_low_storage_prevents_backup_and_write(self):
        with patch('typix_copilot.writer.require_space', side_effect=WriteError('low-storage')):
            result, calls = self.run_transaction()
        self.assertEqual(result, 1)
        self.assertNotIn('backup', calls)
        self.assertNotIn('write', calls)

    def test_progress_is_bounded_for_full_flash_read(self):
        for count in range(4097):
            self.journal.emit('backup', .18 + .25 * count / 4096)
        self.assertLess(len(self.events), 30)

    def test_public_status_mode_survives_privileged_umask(self):
        previous = os.umask(0o077)
        try:
            self.run_transaction()
        finally:
            os.umask(previous)
        self.assertEqual((self.root / 'status.json').stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.journal.job / 'flash-backup.bin').stat().st_mode & 0o777, 0o600)

    def test_progress_disk_failure_after_start_does_not_interrupt_write(self):
        from typix_copilot import writer
        original = writer.private_write
        def fail_after_write(path, *args, **kwargs):
            if self.journal.hardware_started: raise OSError('disk filled concurrently')
            return original(path, *args, **kwargs)
        with patch.object(writer, 'private_write', fail_after_write):
            result, calls = self.run_transaction()
        self.assertEqual(result, 0)
        self.assertIn('verify', calls)
        self.assertIn('restart', calls)
        self.assertTrue(self.events[-1]['audit_degraded'])

    def test_allowlist_rejects_historical_unknown_and_shell_requests(self):
        self.assertEqual(approved_firmware('official-20260910').id, 'official-20260910')
        for identifier in ['official-20260821', '../../etc/passwd', ';reboot', '--port=/dev/ttyACM1']:
            with self.assertRaises(WriteError): approved_firmware(identifier)

    def test_image_rejected_before_transport_for_bad_size_or_hash(self):
        for content in [b'wrong', self.data + b'extra']:
            source = Path(self.tmp.name) / 'input'
            source.write_bytes(content)
            with source.open('rb') as stream, self.assertRaises(WriteError):
                receive_image(stream, self.fw, self.journal.job / 'image.bin')

    def test_power_config_requires_live_high_impedance_bits(self):
        self.assertTrue(safe_power_config(b'CONFIG_P0  [0x04]  boot=0xFF  live=0xFE  <\xe2\x80\x94 DIFF'))
        for line in [b'CONFIG_P0 [0x04] boot=0xFE live=0xF2', b'CONFIG_P0 [0x04] boot=0xFE live=READ_FAIL',
                     b'CONFIG_P1 [0x05] boot=0xFE live=0xFE', b'key=CONFIG_P0 [0x04] boot=0xFE live=0xFE']:
            self.assertFalse(safe_power_config(line))

    def test_runtime_asserts_cdc_dtr_before_diagnostic_and_fixed_boot_request(self):
        board = MagicMock()
        board.profile = {'board': 'fixture'}
        board.probe.return_value = SimpleNamespace(mode='runtime')
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.read_until.return_value = b'CONFIG_P0 [0x04] boot=0xFE live=0xFE\r\n'
        observed = []
        connection.write.side_effect = lambda data: observed.append((data, connection.dtr))
        transport = SerialTransport.__new__(SerialTransport)
        transport.board, transport.journal = board, self.journal
        with patch('typix_copilot.writer.open_bound_serial', return_value=connection):
            transport.enter()
        self.assertEqual(observed, [(b'\nAW_DUMP\n', True), (b'EGGFLY_REBOOT_TO_BOOT_MODE\n', True)])

    def test_restart_clears_only_force_download_before_watchdog(self):
        transport = SerialTransport.__new__(SerialTransport)
        transport.esp = MagicMock()
        transport.esp.RTC_CNTL_OPTION1_REG = 0x60008000
        transport.esp.RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK = 1
        transport.esp.read_reg.return_value = 0
        transport.connection = None
        transport.board = MagicMock()
        transport.journal = self.journal
        transport.restart()
        transport.esp.write_reg.assert_called_once_with(0x60008000, 0, 1)
        transport.esp.watchdog_reset.assert_called_once_with()
        transport.esp.hard_reset.assert_not_called()
        transport.board.wait.assert_called_once_with('runtime', 35)
        transport.esp.watchdog_reset.reset_mock()
        transport.esp.read_reg.return_value = 1
        with self.assertRaises(WriteError): transport.restart()
        transport.esp.watchdog_reset.assert_not_called()

    def test_rom_recovery_uses_separate_ownership_not_last_display_record(self):
        transport = SerialTransport.__new__(SerialTransport)
        transport.board = SimpleNamespace(profile={'board':'bound'}, probe=lambda: SimpleNamespace(mode='rom'))
        transport.journal = self.journal
        self.journal.previous = [{'boot_requested': False, 'status': 'failed'}]
        ownership = self.root / 'private/maintenance.json'
        ownership.write_text(json.dumps({'active':True,'profile':{'board':'bound'}}))
        self.assertEqual(transport.enter().mode, 'rom')
        ownership.write_text(json.dumps({'active':True,'profile':{'board':'different'}}))
        with self.assertRaises(WriteError): transport.enter()

    def test_connect_checks_security_before_stub_and_disables_unbound_retry(self):
        esp = MagicMock()
        esp.get_security_info.return_value = dict(chip_id=9, flash_crypt_cnt=0,
            parsed_flags=dict(SECURE_BOOT_EN=False, SECURE_DOWNLOAD_ENABLE=False))
        esp.get_secure_boot_enabled.return_value = False
        esp.get_flash_encryption_enabled.return_value = False
        esp.IS_STUB = True
        cmds = SimpleNamespace(run_stub=MagicMock(return_value=esp), attach_flash=MagicMock(),
            detect_flash_size=MagicMock(return_value='8MB'), _set_flash_parameters=MagicMock())
        target = SimpleNamespace(ESP32S3ROM=MagicMock(return_value=esp))
        transport = SerialTransport.__new__(SerialTransport)
        transport.board = MagicMock()
        with patch.dict(sys.modules, {'esptool.cmds':cmds, 'esptool.targets.esp32s3':target}), \
             patch('typix_copilot.writer.open_bound_serial', return_value=MagicMock()):
            self.assertEqual(transport.connect('bound'), 8 * 1024 * 1024)
            self.assertEqual(esp.WRITE_FLASH_ATTEMPTS, 1)
            cmds._set_flash_parameters.assert_called_once_with(esp, '8MB')
            esp.connect.assert_called_once_with(mode='no-reset', attempts=3, detecting=True, warnings=False)
            cmds.run_stub.reset_mock()
            esp.get_security_info.return_value['flash_crypt_cnt'] = 1
            with self.assertRaises(WriteError): transport.connect('bound')
            cmds.run_stub.assert_not_called()


class ChunkedReadTests(unittest.TestCase):
    def setUp(self):
        self.chunk = 64 * 1024
        self.transport = SerialTransport.__new__(SerialTransport)
        self.transport.esp = MagicMock()
        self.transport.journal = MagicMock()
        self.transport.board = MagicMock()

    def test_exact_offsets_tail_and_progress_only_after_checked_chunks_return(self):
        data = b'A' * self.chunk + b'B' * self.chunk + b'tail'
        expected_reads = [call(0, self.chunk), call(self.chunk, self.chunk), call(2 * self.chunk, 4)]
        for phase, low, width in [('backup', .18, .25), ('verify', .77, .16)]:
            with self.subTest(phase=phase):
                self.transport.esp.reset_mock()
                self.transport.journal.reset_mock()
                timeline = []
                completed = 0
                inside_read = False

                def read_flash(offset, amount):
                    nonlocal completed, inside_read
                    self.assertEqual(self.transport.attempted_offset, offset)
                    self.assertEqual(self.transport.last_checked_bytes, offset)
                    inside_read = True
                    timeline.append(('read', offset, amount))
                    try:
                        # Fake the loader completing its data/digest exchange.
                        return data[offset:offset + amount]
                    finally:
                        completed = offset + amount
                        timeline.append(('checked', completed))
                        inside_read = False

                def emit(observed_phase, progress):
                    self.assertFalse(inside_read)
                    self.assertEqual(observed_phase, phase)
                    self.assertAlmostEqual(progress, low + width * completed / len(data))
                    self.assertEqual(self.transport.last_checked_bytes, completed)
                    timeline.append(('journal', completed))

                self.transport.esp.read_flash.side_effect = read_flash
                self.transport.journal.emit.side_effect = emit
                result = self.transport.read(len(data), phase)
                self.assertEqual(result, data)
                self.assertIsInstance(result, bytes)
                self.assertEqual(self.transport.stage, phase)
                self.assertEqual(self.transport.attempted_offset, 2 * self.chunk)
                self.assertEqual(self.transport.last_checked_bytes, len(data))
                self.assertEqual(self.transport.esp.read_flash.call_args_list, expected_reads)
                self.assertEqual(timeline, [
                    ('read', 0, self.chunk), ('checked', self.chunk), ('journal', self.chunk),
                    ('read', self.chunk, self.chunk), ('checked', 2 * self.chunk), ('journal', 2 * self.chunk),
                    ('read', 2 * self.chunk, 4), ('checked', len(data)), ('journal', len(data)),
                ])

    def test_exact_multiple_has_no_extra_or_repeated_read(self):
        self.transport.esp.read_flash.side_effect = lambda offset, amount: b'X' * amount
        result = self.transport.read(2 * self.chunk, 'backup')
        self.assertEqual(len(result), 2 * self.chunk)
        self.assertEqual(self.transport.esp.method_calls,
                         [call.read_flash(0, self.chunk), call.read_flash(self.chunk, self.chunk)])
        self.assertEqual(self.transport.journal.emit.call_count, 2)
        self.transport.board.assert_not_called()

    def test_incomplete_or_oversize_segment_stops_before_progress_without_retry(self):
        size = 3 * self.chunk + 17
        for failed_segment in (0, 1, 3):
            for length_delta in (-1, 1):
                with self.subTest(segment=failed_segment, delta=length_delta):
                    self.transport.esp.reset_mock()
                    self.transport.journal.reset_mock()

                    def read_flash(offset, amount):
                        return b'X' * (amount + length_delta if offset // self.chunk == failed_segment else amount)

                    self.transport.esp.read_flash.side_effect = read_flash
                    with self.assertRaises(WriteError) as error:
                        self.transport.read(size, 'verify')
                    self.assertEqual(error.exception.code, 'readback-length')
                    self.assertEqual(self.transport.esp.method_calls, [
                        call.read_flash(index * self.chunk, min(self.chunk, size - index * self.chunk))
                        for index in range(failed_segment + 1)
                    ])
                    self.assertEqual(self.transport.journal.emit.call_count, failed_segment)
                    self.assertEqual(self.transport.attempted_offset, failed_segment * self.chunk)
                    self.assertEqual(self.transport.last_checked_bytes, failed_segment * self.chunk)
                    self.assertEqual(self.transport.board.method_calls, [])

    def test_segment_digest_or_transport_exception_propagates_without_retry_or_reset(self):
        for failure in (RuntimeError('fixture digest mismatch'), OSError('fixture transport interrupted')):
            with self.subTest(error=type(failure).__name__):
                self.transport.esp.reset_mock()
                self.transport.journal.reset_mock()
                self.transport.esp.read_flash.side_effect = [b'X' * self.chunk, failure]
                with self.assertRaises(type(failure)) as error:
                    self.transport.read(3 * self.chunk, 'verify')
                self.assertIs(error.exception, failure)
                self.assertEqual(self.transport.esp.method_calls,
                                 [call.read_flash(0, self.chunk), call.read_flash(self.chunk, self.chunk)])
                self.assertEqual(self.transport.journal.emit.call_count, 1)
                self.assertEqual(self.transport.attempted_offset, self.chunk)
                self.assertEqual(self.transport.last_checked_bytes, self.chunk)
                self.assertEqual(self.transport.board.method_calls, [])

    def test_new_read_resets_counters_before_first_attempt_and_on_empty_read(self):
        self.transport.esp.read_flash.side_effect = lambda offset, amount: b'X' * amount
        self.transport.read(2 * self.chunk, 'backup')
        self.assertEqual(self.transport.last_checked_bytes, 2 * self.chunk)
        self.transport.esp.reset_mock()
        self.transport.journal.reset_mock()

        def fail_first(offset, amount):
            self.assertEqual(self.transport.attempted_offset, 0)
            self.assertEqual(self.transport.last_checked_bytes, 0)
            raise OSError('first read failed')
        self.transport.esp.read_flash.side_effect = fail_first
        with self.assertRaises(OSError):
            self.transport.read(2 * self.chunk, 'verify')
        self.assertEqual(self.transport.attempted_offset, 0)
        self.assertEqual(self.transport.last_checked_bytes, 0)
        self.transport.esp.read_flash.assert_called_once_with(0, self.chunk)
        self.transport.journal.emit.assert_not_called()
        self.transport.attempted_offset, self.transport.last_checked_bytes = 123, 456
        self.assertEqual(self.transport.read(0, 'backup'), b'')
        self.assertEqual(self.transport.attempted_offset, 0)
        self.assertEqual(self.transport.last_checked_bytes, 0)
        self.transport.esp.read_flash.assert_called_once_with(0, self.chunk)


class ExceptionDiagnosticsTests(unittest.TestCase):
    def test_only_fixed_error_types_are_reported(self):
        for error in [OSError('secret'), termios.error(5, 'secret'), WriteError('secret'), DeviceError('secret')]:
            with self.subTest(kind=type(error).__name__):
                self.assertEqual(exception_diagnostics(error), {
                    'error_type': type(error).__module__ + '.' + type(error).__name__, 'error_frames': []})
        for module, name in [('serial.serialutil', 'SerialException'), ('esptool.util', 'FatalError')]:
            allowed = type(name, (Exception,), {'__module__': module})
            self.assertEqual(exception_diagnostics(allowed('secret'))['error_type'], module + '.' + name)
        for module, name in [('esptool.util', 'SECRET_NAME'), ('esptool.SECRET_MODULE', 'FatalError'),
                             ('builtins', 'SECRET_NAME'), (None, 'SECRET_NAME')]:
            unknown = type(name, (RuntimeError,), {'__module__': module})
            self.assertEqual(exception_diagnostics(unknown('secret'))['error_type'], 'unknown')

    def test_only_last_eight_allowed_module_frames_without_sensitive_details(self):
        namespace = {'__name__': 'esptool.loader'}
        source = ('def descend(depth):\n'
                  '    private_value = "SECRET_LOCAL"\n'
                  '    if depth: return descend(depth - 1)\n'
                  '    raise OSError("SECRET_MESSAGE")\n')
        exec(compile(source, '/SECRET_PATH/serial-device.py', 'exec'), namespace)
        try:
            namespace['descend'](12)
        except OSError as error:
            diagnostics = exception_diagnostics(error)
        self.assertEqual(diagnostics['error_type'], 'builtins.OSError')
        self.assertEqual(diagnostics['error_frames'], [
            *[{'module': 'esptool.loader', 'function': 'descend', 'line': 3} for _ in range(7)],
            {'module': 'esptool.loader', 'function': 'descend', 'line': 4},
        ])
        self.assertNotIn('SECRET', json.dumps(diagnostics))

    def test_module_prefixes_and_exception_chain_are_not_included(self):
        namespace = {'__name__': 'esptool.SECRET_MODULE'}
        exec(compile('def run():\n    raise ValueError("SECRET_MESSAGE")\n', '/SECRET_PATH', 'exec'), namespace)
        try:
            namespace['run']()
        except ValueError as error:
            error.__cause__ = OSError('SECRET_CAUSE')
            diagnostics = exception_diagnostics(error)
        self.assertEqual(diagnostics, {'error_type': 'builtins.ValueError', 'error_frames': []})


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sys = self.root / 'sys'
        self.usb = self.sys / 'bus/usb/devices'
        self.dev = self.root / 'dev'
        self.usb.mkdir(parents=True)
        self.dev.mkdir()
        self.profile = dict(schema=1, board='typixdeck-0720', controller='fe9c0000.xhci', usb_path='1-1.2', hub=['1a40', '0201'])
        parent = self.usb / '1-1'; parent.mkdir()
        (parent / 'idVendor').write_text('1a40'); (parent / 'idProduct').write_text('0201')
        self.physical = self.sys / 'devices/platform/fe9c0000.xhci/usb1/1-1/1-1.2'
        self.physical.mkdir(parents=True)
        (self.usb / '1-1.2').symlink_to(self.physical)
        for key, value in [('idVendor','303a'),('idProduct','80c3'),('product','TypixDeck UAC+CDC')]:
            (self.physical / key).write_text(value)
        interface = self.physical / '1-1.2:1.3'; interface.mkdir()
        tty = self.sys / 'class/tty/ttyACM7'; tty.mkdir(parents=True)
        (tty / 'device').symlink_to(interface)
        (self.dev / 'ttyACM7').write_text('test fixture')

    def tearDown(self): self.tmp.cleanup()

    def test_bound_topology_finds_renumbered_port(self):
        with patch('typix_copilot.device.stat.S_ISCHR', return_value=True):
            result = Board(self.profile, self.sys, self.dev).probe()
        self.assertEqual(result.port.name, 'ttyACM7')
        self.assertEqual(result.mode, 'runtime')

    def test_foreign_hub_or_runtime_product_refused(self):
        for file, bad in [(self.usb / '1-1/idProduct', 'ffff'), (self.physical / 'product', 'Other S3')]:
            old = file.read_text(); file.write_text(bad)
            with self.assertRaises(DeviceError): Board(self.profile, self.sys, self.dev).probe()
            file.write_text(old)

    def test_no_first_acm_fallback_or_regular_file_serial(self):
        with self.assertRaises(DeviceError): Board(self.profile, self.sys, self.dev).probe()
        (self.usb / '1-1.2').unlink()
        with self.assertRaises(DeviceError): Board(self.profile, self.sys, self.dev).probe()


if __name__ == '__main__': unittest.main()
