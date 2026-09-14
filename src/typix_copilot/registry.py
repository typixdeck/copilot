"""Bounded, signed online firmware metadata and atomic offline snapshots.

Only the public typixdeck/copilot repository is used for metadata and binary
transfers. A saved snapshot is revalidated when read; the bundled approval
catalog stays immutable. Network transports are replaceable in offline tests.
"""
from __future__ import annotations

from dataclasses import asdict, fields
import errno
import http.client
import json
import os
from pathlib import Path
import re
import stat
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from .core import Firmware, MAX_IMAGE_BYTES, load_catalog
from .cache import ArtifactCache, CacheError, Cancelled, _check_cancel, _io_error, _open_https


REGISTRY_BASE = "https://raw.githubusercontent.com/typixdeck/copilot/main/firmware/"
REGISTRY_URL = REGISTRY_BASE + "index.json"
SIGNATURE_URL = REGISTRY_BASE + "index.json.sig"
MAX_INDEX_BYTES = 256 * 1024
MAX_FIRMWARES = 100
HTTP_TIMEOUT = 10
FETCH_DEADLINE = 30
SNAPSHOT_NAME = "firmware-index.signed"
_BASE_FIELDS = {
    "id", "version", "title", "summary", "filename", "size", "sha256",
    "source_url", "commit", "layout", "nvs_reset", "capabilities",
}
_REQUIRED = _BASE_FIELDS | {"download_url", "chip", "board", "image_kind", "flash_offset"}
_ALLOWED = {field.name for field in fields(Firmware)}


class RegistryError(CacheError):
    """The index is unavailable, invalid, or could not be saved safely."""


def _text(value, field, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit
            or (not empty and not value) or value != value.strip()
            or any(unicodedata.category(char).startswith("C") for char in value)):
        raise RegistryError(f"固件目录字段 {field} 无效。")
    return value


def _binary_url(value, filename):
    _text(value, "download_url", 512)
    relative = value[len(REGISTRY_BASE):] if value.startswith(REGISTRY_BASE) else value
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", relative)
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or relative.split("/")[-1] != filename):
        raise RegistryError("固件下载地址必须位于 Copilot 仓库的 firmware 目录。")
    return REGISTRY_BASE + relative


def _validate_source(value):
    _text(value, "source_url", 512)
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        raise RegistryError("固件源码地址无效。") from None
    parts = parsed.path.split("/")
    if (parsed.scheme != "https" or parsed.netloc != "github.com"
            or parsed.query or parsed.fragment or len(parts) < 3
            or parts[1] not in {"typixdeck", "TypixNode"}
            or any(part in {"", ".", ".."} for part in parts[1:])
            or not re.fullmatch(r"/[A-Za-z0-9._/-]+", parsed.path)):
        raise RegistryError("固件源码地址必须是受支持的 GitHub 仓库地址。")


def validate_registry_firmware(firmware: Firmware) -> str:
    """Validate all registry metadata again before any artifact cache I/O.

    Constructing a Firmware in Python does not make arbitrary network URLs valid.
    Returning a URL grants permission to download only, never to flash hardware.
    """
    if not isinstance(firmware, Firmware):
        raise RegistryError("固件目录条目类型无效。")
    limits = {"id": 80, "version": 48, "title": 120, "summary": 640,
              "filename": 160, "layout": 160, "publisher": 80}
    for name, limit in limits.items():
        _text(getattr(firmware, name), name, limit, empty=name == "publisher")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", firmware.id):
        raise RegistryError("固件 ID 无效。")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.bin", firmware.filename)
            or type(firmware.size) is not int or not 0 < firmware.size <= MAX_IMAGE_BYTES
            or not isinstance(firmware.sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", firmware.sha256)
            or not isinstance(firmware.commit, str)
            or firmware.commit and not re.fullmatch(r"[0-9a-f]{40}", firmware.commit)):
        raise RegistryError("固件文件名、大小、摘要或源码提交无效。")
    if (firmware.chip != "esp32s3" or firmware.board != "typixdeck"
            or firmware.image_kind != "merged-image"
            or type(firmware.flash_offset) is not int or firmware.flash_offset != 0
            or type(firmware.nvs_reset) is not bool
            or type(firmware.hardware_verified) is not bool):
        raise RegistryError("目录仅支持 TypixDeck ESP32-S3 的零地址合并镜像。")
    if not isinstance(firmware.capabilities, tuple) or len(firmware.capabilities) > 16:
        raise RegistryError("固件能力列表无效。")
    for capability in firmware.capabilities:
        _text(capability, "capabilities", 80)
    _validate_source(firmware.source_url)
    url = _binary_url(firmware.download_url, firmware.filename)
    for pinned in load_catalog():
        if firmware.id == pinned.id and any(
                getattr(firmware, name) != getattr(pinned, name) for name in _BASE_FIELDS):
            raise RegistryError("在线目录不能覆盖内置固件的固定信息。")
        if (firmware.filename == pinned.filename and
                (firmware.sha256 != pinned.sha256 or firmware.size != pinned.size)):
            raise RegistryError("在线固件与同名内置文件的大小或摘要冲突。")
    return url


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError("固件目录包含重复 JSON 字段。")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("Non-finite JSON values are unsupported")


def parse_catalog(data: bytes) -> list[Firmware]:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_INDEX_BYTES:
        raise RegistryError("固件目录为空或超过 256 KiB。")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise RegistryError("固件目录不是有效的 UTF-8 JSON。") from None
    if (not isinstance(document, dict) or set(document) != {"schema", "firmwares"}
            or type(document["schema"]) is not int or document["schema"] != 1
            or not isinstance(document["firmwares"], list)
            or len(document["firmwares"]) > MAX_FIRMWARES):
        raise RegistryError("固件目录版本、字段或条目数量不受支持。")
    catalog = []
    seen = set()
    for row in document["firmwares"]:
        if (not isinstance(row, dict) or not _REQUIRED.issubset(row)
                or set(row) - _ALLOWED or not isinstance(row["capabilities"], list)):
            raise RegistryError("固件目录条目字段不完整或不受支持。")
        firmware = Firmware(**{**row, "capabilities": tuple(row["capabilities"])})
        url = validate_registry_firmware(firmware)
        firmware = Firmware(**{**asdict(firmware), "download_url": url})
        if firmware.id in seen:
            raise RegistryError("固件目录包含重复 ID。")
        seen.add(firmware.id)
        catalog.append(firmware)
    return catalog


def merge_catalog(remote: list[Firmware], bundled=None) -> list[Firmware]:
    """Keep exact bundled objects for pinned rows and append validated new rows."""
    bundled = list(load_catalog() if bundled is None else bundled)
    by_id = {firmware.id: firmware for firmware in bundled}
    seen = set()
    for firmware in remote:
        validate_registry_firmware(firmware)
        if firmware.id in seen:
            raise RegistryError("固件目录包含重复 ID。")
        seen.add(firmware.id)
        if firmware.id not in by_id:
            bundled.append(firmware)
            by_id[firmware.id] = firmware
    return bundled


class FirmwareRegistry:
    """Public registry with an atomic, validated snapshot in a user cache root."""

    def __init__(self, root: Path):
        self._storage = ArtifactCache(root)
        self._opener = _open_https

    def cached_catalog(self) -> list[Firmware]:
        from .authority import MAX_AUTHORIZATION_BYTES, decode_catalog
        try:
            with self._storage._directory() as descriptor:
                if descriptor is None:
                    return []
                before = self._storage._entry(descriptor, SNAPSHOT_NAME)
                if before is None:
                    return []
                if not 0 < before.st_size <= MAX_AUTHORIZATION_BYTES:
                    raise RegistryError("缓存目录大小无效。")
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                with os.fdopen(os.open(SNAPSHOT_NAME, flags, dir_fd=descriptor), "rb") as source:
                    opened = os.fstat(source.fileno())
                    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                            or self._storage._identity(before) != self._storage._identity(opened)):
                        raise RegistryError("固件目录缓存已变化。")
                    data = source.read(MAX_AUTHORIZATION_BYTES + 1)
                    after = os.fstat(source.fileno())
                    if (self._storage._stable(opened) != self._storage._stable(after)
                            or len(data) != opened.st_size):
                        raise RegistryError("固件目录缓存读取期间已变化。")
                self._storage._same_directory(descriptor)
                return decode_catalog(data, canonical=False)
        except RegistryError:
            raise
        except CacheError as exc:
            raise RegistryError(str(exc)) from None
        except OSError as exc:
            raise RegistryError(str(_io_error(exc))) from None
        except ValueError as exc:
            raise RegistryError(str(exc)) from None

    def _save(self, data, cancel):
        with self._storage._directory(create=True) as descriptor:
            self._storage._entry(descriptor, SNAPSHOT_NAME)
            self._storage._space(descriptor, len(data))
            with self._storage._stage(descriptor) as (name, output):
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                staged = self._storage._entry(descriptor, name)
                if staged is None or self._storage._identity(staged) != self._storage._identity(
                        os.fstat(output.fileno())):
                    raise RegistryError("目录临时文件已变化。")
                _check_cancel(cancel)
                self._storage._same_directory(descriptor)
                self._storage._entry(descriptor, SNAPSHOT_NAME)
                os.replace(name, SNAPSHOT_NAME, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                try:
                    os.fsync(descriptor)
                except OSError as exc:
                    if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                        raise
                self._storage._same_directory(descriptor)

    def fetch_catalog(self, cancel=None) -> list[Firmware]:
        from .authority import MAGIC, verify_catalog
        import struct
        _check_cancel(cancel)
        deadline = time.monotonic() + FETCH_DEADLINE
        def fetch(url, limit, accept):
            request = urllib.request.Request(url, headers={
                "Accept": accept, "Accept-Encoding": "identity",
            })
            return self._fetch_bytes(request, url, limit, deadline, cancel)
        try:
            data = fetch(REGISTRY_URL, MAX_INDEX_BYTES, "application/json")
            signature = fetch(SIGNATURE_URL, 64, "application/octet-stream")
            catalog = verify_catalog(data, signature)
            _check_cancel(cancel)
            self._save(MAGIC + struct.pack("!I", len(data)) + signature + data, cancel)
            return catalog
        except (RegistryError, Cancelled):
            raise
        except CacheError as exc:
            _check_cancel(cancel)
            raise RegistryError(str(exc)) from None
        except urllib.error.HTTPError as exc:
            exc.close()
            _check_cancel(cancel)
            raise RegistryError(f"在线固件目录暂不可用（HTTP {exc.code}）。") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            _check_cancel(cancel)
            raise RegistryError(str(_io_error(exc))) from None
        except ValueError as exc:
            raise RegistryError(str(exc)) from None

    def _fetch_bytes(self, request, url, limit, deadline, cancel):
        with self._opener(request, timeout=HTTP_TIMEOUT) as response:
            if response.getcode() != 200 or response.geturl() != url:
                raise RegistryError("在线固件目录响应异常。")
            length = response.headers.get("Content-Length")
            if (response.headers.get("Content-Encoding", "identity").lower() != "identity"
                    or length is not None and (not re.fullmatch(r"[0-9]{1,9}", length)
                                              or not 0 < int(length) <= limit)):
                raise RegistryError("在线固件目录大小或编码无效。")
            chunks = []
            received = 0
            read_chunk = getattr(response, "read1", response.read)
            while True:
                _check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise RegistryError("在线固件目录请求超时。")
                chunk = read_chunk(min(16 * 1024, limit - received + 1))
                _check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise RegistryError("在线固件目录请求超时。")
                if not chunk:
                    break
                received += len(chunk)
                if received > limit:
                    raise RegistryError("在线固件目录或签名超过大小上限。")
                chunks.append(chunk)
            if length is not None and int(length) != received:
                raise RegistryError("在线固件目录下载不完整。")
        return b"".join(chunks)
