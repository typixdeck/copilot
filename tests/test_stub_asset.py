"""Pin the reviewed upstream maintenance payload; never open a serial port."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('copilot_build', ROOT / 'tools/build-deb.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class StubAssetTests(unittest.TestCase):
    def test_reviewed_asset_is_accepted(self):
        self.assertEqual(len(build.checked_s3_stub()), 16780)

    def test_changed_asset_is_refused_before_packaging(self):
        data = bytearray(build.checked_s3_stub())
        data[100] ^= 1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'vendor').mkdir()
            (root / 'vendor/esp32s3-v1.2.2.json').write_bytes(data)
            with patch.object(build, 'ROOT', root), self.assertRaisesRegex(SystemExit, 'hash/size mismatch'):
                build.checked_s3_stub()
