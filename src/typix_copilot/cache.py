"""Verified artifacts from the bundled catalog or validated public registry.

This module downloads bytes, never controls hardware. A cache hit verifies the
file again; it is not evidence of board compatibility or a successful write.
Callbacks run on the calling thread. Tests may replace ``cache._opener`` with a
callable taking a urllib Request and a keyword-only ``timeout`` argument.
"""
from __future__ import annotations

from contextlib import contextmanager, suppress
import errno
import hashlib
import http.client
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import threading
import time
from typing import Callable
import urllib.error
import urllib.request

from .core import Firmware, MAX_IMAGE_BYTES, inspect_local, load_catalog


CHUNK_BYTES = 64 * 1024
HTTP_TIMEOUT = 10
DOWNLOAD_DEADLINE = 120
FREE_SPACE_RESERVE = 1024 * 1024


class CacheError(Exception):
    """An artifact could not be safely obtained or verified."""


class Cancelled(CacheError):
    """The caller cancelled before the verified file was committed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise CacheError("固件来源发生重定向，已停止下载。")


def _open_https(request, *, timeout):
    # No environment proxy, cookies, authorization, or certificate override.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    return opener.open(request, timeout=timeout)


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled("已取消下载，未更改已验证缓存。")


def _io_error(exc):
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return CacheError("存储空间不足，请释放空间后重试。")
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return CacheError("下载超时，请检查网络后重试。")
    return CacheError("无法读取或保存固件，请检查网络、文件权限和存储空间。")


def _known(firmware: Firmware) -> str:
    if not isinstance(firmware, Firmware) or firmware not in load_catalog():
        # Import lazily: the registry reuses our hardened user-cache directory
        # operations, and validates every field before returning a fixed-host URL.
        from .registry import validate_registry_firmware
        return validate_registry_firmware(firmware)
    if (not re.fullmatch(r"[0-9a-f]{64}", firmware.sha256)
            or not re.fullmatch(r"[0-9a-f]{40}", firmware.commit)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.bin", firmware.filename)
            or not 0 < firmware.size <= MAX_IMAGE_BYTES):
        raise CacheError("固定固件元数据无效。")
    repository = "TypixNode/TypixDeck-esp32s3-firmware"
    expected = f"https://github.com/{repository}/blob/{firmware.commit}/release/{firmware.filename}"
    if firmware.source_url != expected:
        raise CacheError("固件来源不符合固定官方地址。")
    return f"https://raw.githubusercontent.com/{repository}/{firmware.commit}/release/{firmware.filename}"


class ArtifactCache:
    def __init__(self, root: Path):
        path = Path(root).expanduser()
        if ".." in path.parts:
            raise CacheError("缓存目录不能包含上级路径。")
        self.root = path.absolute()
        if self.root == Path(self.root.anchor):
            raise CacheError("不能将文件系统根目录作为缓存。")
        self._opener = _open_https

    @contextmanager
    def _directory(self, create=False):
        """Walk by descriptors so directory symlinks cannot redirect writes."""
        descriptor = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            missing = False
            try:
                for name in self.root.parts[1:]:
                    try:
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=descriptor)
                    except FileNotFoundError:
                        if not create:
                            missing = True
                            break
                        try:
                            os.mkdir(name, mode=0o700, dir_fd=descriptor)
                        except FileExistsError:
                            pass
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                if not missing:
                    info = os.fstat(descriptor)
                    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                        raise CacheError("缓存目录必须属于当前用户，且不可由其他用户写入。")
            except OSError as exc:
                raise _io_error(exc) from None
            yield None if missing else descriptor
        finally:
            os.close(descriptor)

    def _same_directory(self, descriptor):
        with self._directory() as current:
            if current is None or self._identity(os.fstat(current)) != self._identity(os.fstat(descriptor)):
                raise CacheError("缓存目录已变化，请重试。")

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _stable(info):
        return info.st_size, info.st_mtime_ns, info.st_ctime_ns

    @staticmethod
    def _entry(descriptor, name):
        try:
            entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise CacheError("缓存条目不是独立普通文件，已拒绝使用。")
        return entry

    def _valid(self, descriptor, name, firmware):
        try:
            before = self._entry(descriptor, name)
            if before is None or before.st_size != firmware.size:
                return False
            self._same_directory(descriptor)
            result = inspect_local(self.root / name)
            after = self._entry(descriptor, name)
            self._same_directory(descriptor)
            return (after is not None
                    and self._identity(before) == self._identity(after)
                    and self._stable(before) == self._stable(after)
                    and result["size"] == firmware.size
                    and result["sha256"] == firmware.sha256
                    and result["kind"] == "merged-image")
        except (ValueError, OSError):
            return False

    def cached(self, firmware: Firmware) -> Path | None:
        _known(firmware)
        name = firmware.sha256 + ".bin"
        with self._directory() as descriptor:
            if descriptor is None:
                return None
            try:
                valid = self._valid(descriptor, name, firmware)
            except CacheError:
                # Unsafe/corrupt entries are never reported as cached. ensure()
                # rejects unsafe targets explicitly rather than following them.
                return None
            return self.root / name if valid else None

    @staticmethod
    def _space(descriptor, size):
        info = os.fstatvfs(descriptor)
        if info.f_bavail * info.f_frsize < size + FREE_SPACE_RESERVE:
            raise CacheError("存储空间不足，请释放空间后重试。")

    @contextmanager
    def _stage(self, descriptor):
        name = ".partial-" + secrets.token_hex(16) + ".bin"
        handle = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=descriptor)
        try:
            with os.fdopen(handle, "wb") as output:
                yield name, output
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=descriptor)

    def _commit(self, descriptor, stage_name, output, firmware, cancel=None):
        _check_cancel(cancel)
        output.flush()
        os.fsync(output.fileno())
        if not self._valid(descriptor, stage_name, firmware):
            raise CacheError("固件大小、SHA-256 或镜像结构校验失败，未更新缓存。")
        staged = self._entry(descriptor, stage_name)
        if staged is None or self._identity(os.fstat(output.fileno())) != self._identity(staged):
            raise CacheError("临时文件已变化，请重试。")
        _check_cancel(cancel)
        name = firmware.sha256 + ".bin"
        self._entry(descriptor, name)  # Reject symlinks/FIFOs; never follow them.
        self._same_directory(descriptor)
        if not self._valid(descriptor, name, firmware):
            _check_cancel(cancel)
            os.replace(stage_name, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            try:
                os.fsync(descriptor)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        self._same_directory(descriptor)
        return self.root / name

    def ensure(self, firmware: Firmware, cancel: threading.Event | None = None,
               progress: Callable[[int, int], None] | None = None) -> Path:
        url = _known(firmware)
        _check_cancel(cancel)
        existing = self.cached(firmware)
        if existing is not None:
            _check_cancel(cancel)
            if progress:
                progress(firmware.size, firmware.size)
            return existing
        try:
            with self._directory(create=True) as descriptor:
                self._entry(descriptor, firmware.sha256 + ".bin")
                self._space(descriptor, firmware.size)
                _check_cancel(cancel)
                if progress:
                    progress(0, firmware.size)
                _check_cancel(cancel)
                request = urllib.request.Request(url, headers={
                    "Accept": "application/octet-stream", "Accept-Encoding": "identity",
                })
                deadline = time.monotonic() + DOWNLOAD_DEADLINE
                with self._opener(request, timeout=HTTP_TIMEOUT) as response:
                    if response.getcode() != 200 or response.geturl() != url:
                        raise CacheError("固件来源响应异常，已停止下载。")
                    length = response.headers.get("Content-Length")
                    encoding = response.headers.get("Content-Encoding", "identity")
                    if (encoding.lower() != "identity" or length is not None and (
                            not re.fullmatch(r"[0-9]+", length) or int(length) != firmware.size)):
                        raise CacheError("下载响应大小或编码与固件清单不一致。")
                    with self._stage(descriptor) as (name, output):
                        received = 0
                        digest = hashlib.sha256()
                        # HTTPResponse.read(n) can internally await repeated
                        # packets. read1 gives each socket read a cancellation
                        # and total-deadline checkpoint, including slow senders.
                        read_chunk = getattr(response, "read1", response.read)
                        while True:
                            _check_cancel(cancel)
                            if time.monotonic() >= deadline:
                                raise CacheError("下载超时，请检查网络后重试。")
                            chunk = read_chunk(min(CHUNK_BYTES, firmware.size - received + 1))
                            _check_cancel(cancel)
                            if time.monotonic() >= deadline:
                                raise CacheError("下载超时，请检查网络后重试。")
                            if not chunk:
                                break
                            received += len(chunk)
                            if received > firmware.size:
                                raise CacheError("下载超过清单文件大小，已停止。")
                            output.write(chunk)
                            digest.update(chunk)
                            if progress:
                                progress(received, firmware.size)
                        if received != firmware.size or digest.hexdigest() != firmware.sha256:
                            raise CacheError("下载不完整或 SHA-256 不匹配，未更新缓存。")
                        return self._commit(descriptor, name, output, firmware, cancel)
        except CacheError:
            raise
        except urllib.error.HTTPError as exc:
            exc.close()
            _check_cancel(cancel)
            raise CacheError(f"固件来源暂不可用（HTTP {exc.code}），请稍后重试。") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            _check_cancel(cancel)
            raise _io_error(exc) from None

    def _copy_known(self, source, opened, firmware, output=None):
        total = 0
        digest = hashlib.sha256()
        while True:
            chunk = source.read(min(CHUNK_BYTES, firmware.size - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > firmware.size:
                raise CacheError("本地文件在读取期间变大，已停止导入。")
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        if (total != firmware.size or digest.hexdigest() != firmware.sha256
                or self._stable(opened) != self._stable(os.fstat(source.fileno()))):
            raise CacheError("本地文件与目录中的 SHA-256 不一致，未更新缓存。")

    def import_known(self, path: Path, firmware: Firmware) -> Path:
        _known(firmware)
        path = Path(path)
        if path.suffix.lower() != ".bin":
            raise CacheError("请选择目录中固件的 .bin 文件。")
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_size != firmware.size:
                raise CacheError("本地文件类型或大小与目录中的固件不一致。")
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
                opened = os.fstat(source.fileno())
                if (not stat.S_ISREG(opened.st_mode) or self._identity(before) != self._identity(opened)
                        or opened.st_size != firmware.size):
                    raise CacheError("本地文件已变化，请重新选择。")
                if self.cached(firmware) is not None:
                    # Still check the selected import, even if this firmware is
                    # cached. A wrong local file must never appear accepted.
                    self._copy_known(source, opened, firmware)
                    existing = self.cached(firmware)
                    if existing is not None:
                        return existing
                    source.seek(0)
                with self._directory(create=True) as descriptor:
                    self._entry(descriptor, firmware.sha256 + ".bin")
                    self._space(descriptor, firmware.size)
                    with self._stage(descriptor) as (name, output):
                        self._copy_known(source, opened, firmware, output)
                        return self._commit(descriptor, name, output, firmware)
        except CacheError:
            raise
        except OSError as exc:
            raise _io_error(exc) from None
