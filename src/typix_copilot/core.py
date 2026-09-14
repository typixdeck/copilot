"""Offline evidence reader and in-memory workflow simulation for Copilot.

This module has no device, subprocess or network backend.  A successful simulation
only changes Python state; image inspection does not establish board compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import struct


MAX_IMAGE_BYTES = 16 * 1024 * 1024
_BOARD_WARNING = "仅检查本地文件结构；不代表板级兼容性、发布者签名或 CM4 真机验收通过。"


@dataclass(frozen=True)
class Firmware:
    id: str
    version: str
    title: str
    summary: str
    filename: str
    size: int
    sha256: str
    source_url: str
    commit: str
    layout: str
    nvs_reset: bool
    capabilities: tuple[str, ...]
    # Registry metadata needs a signed catalog grant before hardware writing.
    download_url: str = ""
    publisher: str = ""
    hardware_verified: bool = False
    chip: str = "esp32s3"
    board: str = "typixdeck"
    image_kind: str = "merged-image"
    flash_offset: int = 0


def load_catalog() -> list[Firmware]:
    """Return the pinned, audited repository artifacts, newest file date first.

    ``commit`` identifies the repository containing the artifact, not a proven
    build-source commit. Versions are display dates from the upstream filenames.
    """
    rows = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
    return sorted(
        [Firmware(**{**row, "capabilities": tuple(row["capabilities"])}) for row in rows],
        key=lambda firmware: firmware.version,
        reverse=True,
    )


def _read_regular(path: Path) -> bytes:
    if path.suffix.lower() != ".bin":
        raise ValueError("仅支持本地 .bin 文件。")
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("请选择普通文件，不接受符号链接、目录或设备文件。")
        if before.st_size > MAX_IMAGE_BYTES:
            raise ValueError("镜像超过 16 MiB 预览上限。")
        # NOFOLLOW prevents a last-component symlink substitution; NONBLOCK also
        # prevents a substituted FIFO from hanging this bounded, read-only reader.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
                opened.st_dev, opened.st_ino
            ):
                raise ValueError("读取前文件已变化，请重新选择普通文件。")
            if opened.st_size > MAX_IMAGE_BYTES:
                raise ValueError("镜像超过 16 MiB 预览上限。")
            data = stream.read(MAX_IMAGE_BYTES + 1)
            after = os.fstat(stream.fileno())
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ) or len(data) != opened.st_size:
                raise ValueError("读取期间文件已变化，请重试。")
    except OSError:
        raise ValueError("无法只读打开所选文件，请检查文件与访问权限。") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("镜像超过 16 MiB 预览上限。")
    return data


def _image_end(data: bytes, start: int, limit: int) -> tuple[int, bool]:
    """Validate one ESP32-S3 image; return end offset and IDF app marker.

    ESP image headers are 24 bytes; segments have 8-byte headers. The XOR
    checksum is the final byte of a 16-byte-aligned image, followed by an optional
    32-byte SHA-256. Limits are relative to the whole input buffer.
    """
    if start + 24 > limit:
        raise ValueError("镜像头被截断。")
    header = data[start:start + 24]
    if header[0] != 0xE9 or not 1 <= header[1] <= 16:
        raise ValueError("ESP 镜像头或段数量无效。")
    if struct.unpack_from("<H", header, 12)[0] != 9:
        raise ValueError("仅支持结构检查 ESP32-S3 镜像（chip_id 9）。")
    if header[2] > 3 or header[23] not in (0, 1) or any(header[19:23]):
        raise ValueError("ESP 镜像头字段无效或格式暂不支持。")
    if header[3] >> 4 > 7 or header[3] & 15 not in {0, 1, 2, 15}:
        raise ValueError("ESP 镜像 Flash 容量或频率字段无效。")
    cursor = start + 24
    checksum = 0xEF
    app_marker = False
    for index in range(header[1]):
        if cursor + 8 > limit:
            raise ValueError("镜像段头被截断。")
        _, size = struct.unpack_from("<II", data, cursor)
        cursor += 8
        if size == 0 or size % 4 or size > MAX_IMAGE_BYTES:
            raise ValueError("镜像段长度无效。")
        if cursor + size > limit:
            raise ValueError("镜像段被截断或超出分区边界。")
        if index == 0:
            app_marker = data[cursor:cursor + 4] == b"\x32\x54\xcd\xab"
            if app_marker and size < 256:
                raise ValueError("ESP-IDF 应用描述被截断。")
        for byte in data[cursor:cursor + size]:
            checksum ^= byte
        cursor += size
    checksum_at = start + ((cursor - start) // 16 + 1) * 16 - 1
    end = checksum_at + 1
    if end > limit:
        raise ValueError("镜像校验尾部被截断。")
    if data[checksum_at] != checksum:
        raise ValueError("镜像段校验和不匹配。")
    if header[23]:
        if end + 32 > limit:
            raise ValueError("镜像 SHA-256 尾部被截断。")
        if hashlib.sha256(data[start:end]).digest() != data[end:end + 32]:
            raise ValueError("镜像内嵌 SHA-256 校验失败。")
        end += 32
    return end, app_marker


def _partition_table(data: bytes) -> list[dict]:
    if len(data) < 0x9000:
        raise ValueError("合并镜像分区表被截断。")
    rows = []
    table_start = 0x8000
    digest_seen = False
    terminated = False
    for cursor in range(table_start, table_start + 0xC00, 32):
        entry = data[cursor:cursor + 32]
        if entry == b"\xff" * 32:
            terminated = True
            break
        if entry[:2] == b"\xeb\xeb":
            if digest_seen or entry[2:16] != b"\xff" * 14:
                raise ValueError("分区表摘要条目无效。")
            if hashlib.md5(data[table_start:cursor]).digest() != entry[16:32]:
                raise ValueError("分区表 MD5 校验失败。")
            digest_seen = True
            continue
        if digest_seen or entry[:2] != b"\xaa\x50":
            raise ValueError("分区表条目无效。")
        _, kind, _, offset, size, label_bytes, _ = struct.unpack("<HBBII16sI", entry)
        try:
            label = label_bytes.split(b"\0", 1)[0].decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("分区名称不是支持的 ASCII 文本。") from None
        if not label or any(ord(char) < 32 or ord(char) > 126 for char in label):
            raise ValueError("分区名称无效。")
        if kind not in (0, 1) or not size or size % 0x1000 or offset < 0x9000:
            raise ValueError("分区类型、大小或起始地址无效。")
        if offset % (0x10000 if kind == 0 else 0x1000) or offset + size > MAX_IMAGE_BYTES:
            raise ValueError("分区未对齐或超出 16 MiB 检查范围。")
        if any(row["label"] == label or max(offset, row["offset"]) < min(
            offset + size, row["offset"] + row["size"]
        ) for row in rows):
            raise ValueError("分区名称重复或地址范围重叠。")
        rows.append({"offset": offset, "size": size, "label": label, "_type": kind})
    if not terminated or not rows or not any(row["_type"] == 0 for row in rows):
        raise ValueError("分区表缺少结束标记或应用分区。")
    for row in rows:
        if row["_type"] == 0:
            # Empty OTA slots are not certified by this preview reader. Require
            # every declared application to contain a structurally valid image.
            _, app_marker = _image_end(data, row["offset"], min(
                len(data), row["offset"] + row["size"]
            ))
            if not app_marker:
                raise ValueError("应用分区缺少 ESP-IDF 应用描述，暂不支持。")
    return [{key: value for key, value in row.items() if key != "_type"} for row in rows]


def inspect_local(path: Path) -> dict:
    """Read a bounded regular .bin without retaining its path or changing it."""
    path = Path(path)
    data = _read_regular(path)
    end, app_marker = _image_end(data, 0, len(data))
    warnings = [_BOARD_WARNING]
    if app_marker:
        # Never reinterpret application payload at 0x8000 as a partition table.
        kind = "app-image"
        partitions = []
        if end != len(data):
            raise ValueError("应用镜像后含未识别数据，暂不支持。")
        warnings.append("应用镜像不含已确认的完整 Flash 布局，不能推断写入地址。")
    else:
        if end > 0x8000 or data[0x8000:0x8002] != b"\xaa\x50":
            raise ValueError("无法确认完整分区表或 ESP-IDF 应用用途。")
        partitions = _partition_table(data)
        kind = "merged-image"
        warnings.append("仅识别文件内布局；数据分区可为部分填充，未知数据区的完整性仍需可信清单。")
        if any(row["label"] == "nvs" and row["offset"] < len(data) for row in partitions):
            warnings.append("合并镜像覆盖范围包含 NVS；实际完整写入可能重置设置。")
    digest = hashlib.sha256(data).hexdigest()
    for firmware in load_catalog():
        if firmware.filename == path.name and (len(data) != firmware.size or digest != firmware.sha256):
            raise ValueError("同名官方文件与固定审计的大小或 SHA-256 不一致。")
    # Control characters in a local basename must not become UI control content.
    name = "".join(char if char.isprintable() else "�" for char in path.name)
    return {"name": name, "size": len(data), "sha256": digest, "kind": kind,
            "declared_flash_bytes": (1024 * 1024) << (data[3] >> 4),
            "chip_id": 9, "partitions": partitions, "warnings": warnings}


class Simulation:
    """Deterministic, in-memory preview; no firmware bytes are downloaded/written."""

    def __init__(self, catalog: list[Firmware]):
        self._catalog = {firmware.id: firmware for firmware in catalog}
        if len(self._catalog) != len(catalog):
            raise ValueError("模拟目录包含重复固件 ID。")
        self.reset()

    @property
    def active(self) -> dict | None:
        return dict(self._active) if self._active is not None else None

    def reset(self) -> None:
        self.cached: set[str] = set()
        self.current: str | None = None
        self.history: list[dict] = []
        self._active: dict | None = None
        self._steps: list[tuple[str, str]] = []
        self._position = 0

    # Keep the spelled-out contract's Reset entry point as a harmless alias.
    Reset = reset

    def start(self, firmware_id: str, operation: str = "switch", scenario: str = "success") -> None:
        if self._active is not None and self._active["status"] == "running":
            raise ValueError("模拟任务正在运行，请先完成或取消。")
        if firmware_id not in self._catalog:
            raise ValueError("模拟目录中没有该固件。")
        if operation not in {"download", "switch", "restore"}:
            raise ValueError("不支持的模拟操作。")
        if scenario not in {"success", "verification-failure", "disconnected"}:
            raise ValueError("不支持的模拟场景。")
        steps = [("prepare", "模拟：准备本次演示事务。")]*2
        if firmware_id not in self.cached:
            steps += [("download", "模拟：下载进度演示，没有网络请求。")]*6
        if operation != "download":
            steps += [("write", "模拟：演示写入阶段，没有操作设备。")]*10
        steps += [("verify", "模拟：演示校验阶段，不代表真机验证。")]*4
        if operation != "download":
            steps += [("restart", "模拟：演示重启与重新枚举，没有操作设备。")]*3
        self._steps = steps
        self._position = 0
        self._active = {"id": firmware_id, "operation": operation, "scenario": scenario,
                        "phase": "prepare", "progress": 0.0, "status": "running",
                        "message": "模拟：已准备，等待进度推进。", "simulation": True}

    def _finish(self, status: str, message: str) -> dict:
        assert self._active is not None
        self._active.update(status=status, phase="complete" if status == "succeeded" else status,
                            message=message)
        firmware = self._catalog[self._active["id"]]
        self.history.insert(0, {"id": firmware.id, "version": firmware.version,
                               "operation": self._active["operation"], "status": status,
                               "message": message, "simulation": True})
        return dict(self._active)

    def tick(self) -> dict:
        if self._active is None:
            raise ValueError("尚未开始模拟任务。")
        if self._active["status"] != "running":
            return dict(self._active)
        phase, message = self._steps[self._position]
        self._position += 1
        self._active.update(phase=phase, message=message,
                            progress=self._position / len(self._steps))
        scenario = self._active["scenario"]
        if scenario == "verification-failure" and phase == "verify":
            return self._finish("failed", "模拟：校验失败，模拟当前版本保持不变；可修复后重试。")
        disconnected_phase = "verify" if self._active["operation"] == "download" else "write"
        if scenario == "disconnected" and phase == disconnected_phase:
            return self._finish("failed", "模拟：连接中断，模拟当前版本保持不变；可重试或演示恢复。")
        if self._position == len(self._steps):
            self.cached.add(self._active["id"])
            if self._active["operation"] != "download":
                self.current = self._active["id"]
            result = {"download": "下载演示完成，仅记录模拟缓存。",
                      "switch": "切换演示完成，仅更新模拟当前版本。",
                      "restore": "重新写回演示完成，仅更新模拟当前版本。"}
            return self._finish("succeeded", "模拟：" + result[self._active["operation"]])
        return dict(self._active)

    def cancel(self) -> dict:
        if self._active is None:
            raise ValueError("尚未开始模拟任务。")
        if self._active["status"] != "running":
            return dict(self._active)
        return self._finish("cancelled", "模拟：已停止演示，模拟当前版本保持不变；这不代表实际刷写可随时取消。")
