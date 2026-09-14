"""One-use first-upgrade authorization tests; no real serial or privilege."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from typix_copilot import writer
from typix_copilot.core import load_catalog


class CommissioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'; self.run.mkdir(mode=0o700)
        self.events = []
        self.fw = load_catalog()[0]
        self.journal = writer.Journal(self.root / 'state', self.fw, self.events.append)
        self.board = MagicMock()
        self.board.profile = {'schema':1,'board':'bound-fixture'}
        self.board.probe.return_value = SimpleNamespace(mode='runtime')
        self.file = self.run / 'commissioning.json'

    def tearDown(self): self.temp.cleanup()

    def permit(self, **changes):
        epoch, mono = int(time.time()), time.monotonic()
        data=dict(schema=1, active=True, purpose='authorized-first-upgrade-with-power-unverified',
                  profile=self.board.profile, firmware_id=self.fw.id, sha256=self.fw.sha256,
                  size=self.fw.size, nonce='c'*32, uid=1000, issued=epoch-1, expires=epoch+299,
                  monotonic_issued=mono-1, monotonic_expires=mono+299)
        data.update(changes)
        self.file.write_text(json.dumps(data));self.file.chmod(0o600)

    @contextmanager
    def authorized_fixture(self):
        original = Path.lstat
        def fake_root(path, *a, **k):
            info = original(path, *a, **k)
            if path in (self.file, self.run):
                values = list(info);values[4]=0
                return os.stat_result(values)
            return info
        with patch.object(writer, 'COMMISSIONING_ROOT', self.run), \
             patch.dict(os.environ, {'PKEXEC_UID':'1000'}), patch.object(Path, 'lstat', fake_root):
            yield

    def test_exact_capability_consumed_once_and_private_audit_precedes_boot(self):
        self.permit()
        with self.authorized_fixture():
            writer.commissioning_permit(self.journal,self.board)
            writer.commissioning_permit(self.journal,self.board,consume=True)
            with self.assertRaises(writer.WriteError):writer.commissioning_permit(self.journal,self.board)
        audit=json.loads((self.journal.job/'commissioning.json').read_text())
        self.assertFalse(audit['active'])
        self.assertEqual(audit['consumed_by'],self.journal.identifier)
        self.assertEqual((self.journal.job/'commissioning.json').stat().st_mode & 0o777,0o600)

    def test_missing_expired_wrong_uid_profile_image_size_nonce_rejected(self):
        with self.authorized_fixture():
            with self.assertRaises(writer.WriteError):writer.commissioning_permit(self.journal,self.board)
            for changes in [dict(active=False),dict(uid=1001),dict(profile={}),dict(sha256='0'*64),
                            dict(size=1),dict(firmware_id='official-20260821'),dict(nonce=''),
                            dict(issued=1,expires=2),dict(monotonic_issued=1,monotonic_expires=2),
                            dict(expires=int(time.time())+600),dict(purpose='skip')]:
                with self.subTest(changes=changes):
                    self.permit(**changes)
                    with self.assertRaises(writer.WriteError):writer.commissioning_permit(self.journal,self.board)

    def test_links_permissions_owner_and_consume_failure_rejected(self):
        self.permit()
        with patch.object(writer,'COMMISSIONING_ROOT',self.run), patch.dict(os.environ,{'PKEXEC_UID':'1000'}):
            if os.getuid()!=0:
                with self.assertRaises(writer.WriteError):writer.commissioning_permit(self.journal,self.board)
        with self.authorized_fixture():
            self.file.chmod(0o644)
            with self.assertRaises(writer.WriteError):writer.commissioning_permit(self.journal,self.board)
            self.permit()
            linked=self.run/'link';os.link(self.file,linked)
            with self.assertRaises(writer.WriteError):writer.commissioning_permit(self.journal,self.board)
            self.file.unlink();self.file.symlink_to(linked)
            with self.assertRaises(writer.WriteError):writer.commissioning_permit(self.journal,self.board)
            self.file.unlink();linked.unlink();self.permit()
            with patch.object(writer,'private_write',side_effect=OSError('disk failure')):
                with self.assertRaises(writer.WriteError):writer.commissioning_permit(self.journal,self.board,consume=True)

    def test_authenticated_uid_cannot_be_missing_negative_or_malformed(self):
        with self.authorized_fixture():
            for actor in ['', '-1', '+1000', ' 1000', '１０００', '4294967295']:
                self.permit(uid=-1 if actor in ('','-1') else 1000)
                with patch.dict(os.environ, {'PKEXEC_UID':actor}), self.assertRaises(writer.WriteError):
                    writer.commissioning_permit(self.journal,self.board)

    def enter_fixture(self, aw=None, audio=True, consume_error=False):
        connection=MagicMock();connection.__enter__.return_value=connection
        stage={'name':'aw','index':0}
        sent=[]
        def send(data):
            sent.append(data)
            if b'AUDIO_DUMP' in data:stage.update(name='audio',index=0)
        def read(*_):
            rows=([b'send REBOOT_TO_BOOT_MODE to enter flash mode\r\n',*(aw if isinstance(aw,list) else [aw or b'stats\r\n'])]
                  if stage['name']=='aw' else ([b'--- AUDIO_DUMP ---\r\n',b'reg[0x00]=0x00\r\n',b'--- END ---\r\n'] if audio else [b'stats\r\n']))
            index=stage['index'];stage['index']+=1
            return rows[index] if index<len(rows) else b''
        connection.write.side_effect=send;connection.read_until.side_effect=read
        transport=writer.SerialTransport.__new__(writer.SerialTransport)
        transport.board,self.board= self.board,self.board
        transport.journal=self.journal
        counts=[0]
        def tick():counts[0]+=.5;return counts[0]
        approvals=[]
        def capability(*args,consume=False):
            approvals.append(consume)
            if consume and consume_error:raise writer.WriteError('commissioning-denied')
        with patch.object(writer,'open_bound_serial',return_value=connection), \
             patch.object(writer.time,'monotonic',side_effect=tick), \
             patch.object(writer,'commissioning_permit',side_effect=capability):
            try:transport.enter();error=None
            except writer.WriteError as exc:error=exc.code
        return sent,approvals,error

    def test_legacy_requires_framed_roundtrip_then_consume_audit_then_single_boot(self):
        sent,approval,error=self.enter_fixture()
        self.assertIsNone(error)
        self.assertEqual(approval,[False,True])
        self.assertEqual(sent,[b'\nAW_DUMP\n',b'\nAUDIO_DUMP\n',b'EGGFLY_REBOOT_TO_BOOT_MODE\n'])
        record=json.loads((self.journal.job/'result.json').read_text())
        self.assertTrue(record['exception_used']);self.assertFalse(record['power_state_verified'])
        self.assertTrue(record['boot_requested'])
        self.assertFalse(record['write_started'])
        self.assertTrue(json.loads((self.journal.root/'private/maintenance.json').read_text())['exception_used'])

    def test_partial_failed_unsafe_or_binary_aw_never_uses_exception(self):
        for reply in [b'--- AW_DUMP ---\n',b'CONFIG_P0 [0x04] boot=0xFE live=READ_FAIL\n',
                      b'CONFIG_P0 [0x04] boot=0xFE live=0xF2\n',b'AW_DU',b'\x00\x01frame\n',
                      b'INPUT_P0 [0x00] boot=N/A live=READ_FAIL\n',b'OUTPUT_P0 [0x02] boot=0xFF live=0xFF\n',
                      b'INT_P1 [0x07] boot=0xFF live=0xFF\n',b'GCR [0x11] boot=0x00 live=0x00\n',
                      b'LEDMODE_P0 [0x12] boot=0xFF live=0xFF\n',b'READ_FAIL\n',b'\xff\xfe\xfd\n',
                      b'\x7f\n',b'\x0b\n',b'\x0c\n',b'>>> SCREENSHOT 1024x768 RGB565LE 1572864\n',b'SCRN\n',b'--- END ---\n',
                      [b'CONFIG_P0 [0x04] boot=0xFE live=0xF2\n',b'CONFIG_P0 [0x04] boot=0xFE live=0xFE\n']]:
            with self.subTest(reply=reply):
                sent,approvals,error=self.enter_fixture(aw=reply)
                self.assertEqual(error,'power-state-unverified');self.assertFalse(approvals)
                self.assertNotIn(b'EGGFLY_REBOOT_TO_BOOT_MODE\n',sent)

    def test_missing_audio_or_failed_consumption_never_sends_boot(self):
        for changes in [dict(audio=False),dict(consume_error=True)]:
            sent,_,error=self.enter_fixture(**changes)
            self.assertIsNotNone(error);self.assertNotIn(b'EGGFLY_REBOOT_TO_BOOT_MODE\n',sent)

    def test_timeout_fragments_are_reassembled_without_accepting_incomplete_or_bad_lines(self):
        sent,approvals,error=self.enter_fixture(aw=[b'sta',b'ts\r',b'\n'])
        self.assertIsNone(error);self.assertEqual(approvals,[False,True])
        for rows in [[b'AW_',b'DUMP\n'],[b'\xff',b'\n'],[b'x'*511,b'x\n'],[b'unfinished']]:
            with self.subTest(rows=rows):
                sent,approvals,error=self.enter_fixture(aw=rows)
                self.assertEqual(error,'power-state-unverified');self.assertFalse(approvals)
                self.assertNotIn(b'EGGFLY_REBOOT_TO_BOOT_MODE\n',sent)

    def test_exception_rom_session_cannot_be_reused_from_gui(self):
        self.board.probe.return_value=SimpleNamespace(mode='rom')
        (self.journal.root/'private/maintenance.json').write_text(json.dumps(dict(active=True,profile=self.board.profile,exception_used=True)))
        transport=writer.SerialTransport.__new__(writer.SerialTransport)
        transport.board,transport.journal=self.board,self.journal
        with self.assertRaisesRegex(writer.WriteError,'rom-recovery-required'):transport.enter()


if __name__=='__main__':unittest.main()
