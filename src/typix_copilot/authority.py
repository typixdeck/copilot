"""Authenticated catalog grants shared by the ordinary client and isolated helper.

The signed bytes grant an exact firmware identity, never commands, device paths,
or hardware-check bypasses. Historical signed catalogs remain usable offline.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import os
import select
import struct
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .core import Firmware, load_catalog

MAGIC = b"TCPLT2\0\0"
MAX_CATALOG_BYTES = 256 * 1024
HEADER_BYTES = len(MAGIC) + 4 + 64
MAX_AUTHORIZATION_BYTES = HEADER_BYTES + MAX_CATALOG_BYTES
RECEIVE_TIMEOUT = 30
_proofs = OrderedDict()
_lock = threading.RLock()


def _public_key():
    key = serialization.load_pem_public_key(
        Path(__file__).with_name("firmware-public-key.pem").read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("固件目录公钥类型无效。")
    return key


def _canonical(rows):
    pinned = {fw.id: fw for fw in load_catalog()}
    return [pinned.get(fw.id, fw) for fw in rows]


def verify_catalog(raw: bytes, signature: bytes):
    if (not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CATALOG_BYTES
            or not isinstance(signature, bytes) or len(signature) != 64):
        raise ValueError("固件目录或签名大小无效。")
    try:
        _public_key().verify(signature, raw)
    except (InvalidSignature, OSError, TypeError, ValueError):
        raise ValueError("固件目录签名无效，请刷新后重试。") from None
    from .registry import parse_catalog, RegistryError
    try:
        rows = parse_catalog(raw)
    except RegistryError as exc:
        raise ValueError(str(exc)) from None
    frame = MAGIC + struct.pack("!I", len(raw)) + signature + raw
    with _lock:
        _proofs[signature] = (frame, _canonical(rows))
        _proofs.move_to_end(signature)
        while len(_proofs) > 16:
            _proofs.popitem(last=False)
    return rows


def bundled_catalog():
    directory = Path(__file__).parent
    return _canonical(verify_catalog((directory / "firmware-index.json").read_bytes(),
                                    (directory / "firmware-index.json.sig").read_bytes()))


def decode_catalog(frame: bytes, *, canonical=True):
    if (not isinstance(frame, bytes) or not HEADER_BYTES < len(frame) <= MAX_AUTHORIZATION_BYTES
            or frame[:len(MAGIC)] != MAGIC):
        raise ValueError("固件授权格式无效。")
    size = struct.unpack("!I", frame[len(MAGIC):len(MAGIC) + 4])[0]
    if not 0 < size <= MAX_CATALOG_BYTES or len(frame) != HEADER_BYTES + size:
        raise ValueError("固件授权长度无效。")
    rows = verify_catalog(frame[HEADER_BYTES:], frame[len(MAGIC) + 4:HEADER_BYTES])
    return _canonical(rows) if canonical else rows


def encode_authorization(firmware: Firmware) -> bytes:
    if not isinstance(firmware, Firmware):
        raise ValueError("固件条目类型无效。")
    def find():
        with _lock:
            for frame, rows in reversed(list(_proofs.values())):
                if firmware in rows:
                    return frame
        return None
    frame = find()
    if frame is None:
        bundled_catalog()
        frame = find()
    if frame is None:
        raise ValueError("固件缺少已验证的目录签名，请刷新目录后重试。")
    # Reverify against the installed trust key even for an in-memory proof.
    if firmware not in decode_catalog(frame):
        raise ValueError("固件信息与签名目录不一致。")
    return frame


def authorize_firmware(firmware: Firmware) -> Firmware:
    encode_authorization(firmware)
    return firmware


def receive_authorization(source, identifier: str) -> Firmware:
    deadline = time.monotonic() + RECEIVE_TIMEOUT
    def read_exact(size):
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([source], [], [], remaining)[0]:
                raise ValueError("接收固件授权超时。")
            block = os.read(source.fileno(), size - len(result))
            if not block:
                raise ValueError("固件授权数据不完整。")
            result.extend(block)
        return bytes(result)
    header = read_exact(HEADER_BYTES)
    if header[:len(MAGIC)] != MAGIC:
        raise ValueError("固件授权格式无效。")
    size = struct.unpack("!I", header[len(MAGIC):len(MAGIC) + 4])[0]
    if not 0 < size <= MAX_CATALOG_BYTES:
        raise ValueError("固件授权长度无效。")
    rows = decode_catalog(header + read_exact(size))
    for firmware in rows:
        if firmware.id == identifier:
            return firmware
    raise ValueError("所选固件不在签名目录中。")
