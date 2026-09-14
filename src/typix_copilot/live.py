"""Unprivileged, fail-closed coordination of cache and the installed writer.

Only the installed helper chooses and controls hardware. This client never
terminates the helper, interprets arbitrary backend messages, or infers a
successful write from process launch or a progress percentage.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import threading

from .cache import ArtifactCache, CacheError, Cancelled
from .core import Firmware, load_catalog


HELPER = "/usr/libexec/typix-copilot-write"
PKEXEC = "/usr/bin/pkexec"
WRITABLE_FIRMWARE_ID = "official-20260910"
STATUS_PATH = Path("/var/lib/typix-copilot/status.json")
MAX_EVENT_BYTES = 8192
MAX_STREAM_BYTES = 1024 * 1024
PHASES = frozenset({"prepare", "authorize", "enter", "connect", "backup", "write", "verify", "restart", "complete", "failed"})
FLAGS = ("backup_complete", "write_started", "verified", "reconnected", "runtime_version_confirmed", "audit_degraded", "exception_used", "power_state_verified")
PHASE_TEXT = {"prepare": "下载并校验固件", "authorize": "等待系统授权", "enter": "进入维护模式",
              "connect": "连接板载协处理器", "backup": "备份当前固件", "write": "正在写入",
              "verify": "校验写入内容", "restart": "等待设备重新连接", "complete": "写入已校验，设备已重新连接"}
ERROR_TEXT = {
    "commissioning-denied": "未取得供电查询回复，需要首次升级授权",
    "rom-recovery-required": "首次升级尚未恢复运行，需要单独恢复处理",
    "cancelled": "已取消，尚未请求写入", "cache_failed": "固件下载或文件校验失败",
    "authorization_denied": "系统授权未完成，未执行写入", "helper_missing": "写入组件不可用",
    "unsupported_firmware": "此历史版本仅支持下载与检查", "busy": "已有维护任务，请勿重复写入",
    "ongoing": "维护任务尚未确认结束，请勿断电", "status_unavailable": "无法读取维护状态，已停止新写入",
    "result_unavailable": "未收到完整结果，请检查维护状态", "protocol_error": "维护结果格式异常",
    "verification_incomplete": "尚未确认写入校验与设备重新连接", "helper_failed": "写入未完成",
    "cache_changed": "缓存文件已变化，已停止写入",
    "authorization-required": "系统授权未完成", "firmware-not-approved": "此固件尚未开放写入",
    "device-missing": "未发现已绑定的板载设备", "board-profile": "板载设备配置未就绪",
    "board-mismatch": "设备与板载配置不一致", "port-busy": "设备正被其他程序使用",
    "port-invalid": "设备端口无效", "port-ambiguous": "设备端口不唯一", "port-missing": "未发现设备端口",
    "target-changed": "维护目标发生变化", "power-state-unverified": "供电保持状态未通过检查",
    "rom-timeout": "进入维护模式超时", "rom-not-owned": "当前维护模式不属于已记录事务",
    "security-enabled": "设备安全配置不允许本次写入", "chip-mismatch": "芯片型号不匹配",
    "flash-size": "Flash 容量检查失败", "backup-incomplete": "备份未完整保存，已停止写入",
    "verify-mismatch": "写入内容校验不一致", "reconnect-timeout": "设备未重新连接",
    "transport-failed": "设备通信失败", "image-mismatch": "固件文件校验不一致",
    "low-storage": "存储空间不足", "input-timeout": "接收固件超时", "readback-length": "回读数据不完整",
    "unsafe-state": "维护记录目录不安全", "unsafe-lock": "维护锁不可用",
    "tool-version": "写入工具版本不匹配", "stub-unavailable": "维护程序无法启动",
    "preflight-failed": "写入预检失败", "invalid-request": "写入请求无效",
    "restart-mode": "设备复位方式检查失败",
}


class LiveError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(ERROR_TEXT.get(code, "维护任务未完成"))


def event_message(event):
    """Only fixed local text reaches widgets; backend free text is ignored."""
    if event.get("uncertain"):
        return "维护状态待确认，请勿切换、拔线或断电"
    if event["status"] == "failed":
        base = ERROR_TEXT.get(event.get("code"), "写入未完成")
        if event.get("write_started"):
            base += "；需要恢复，请保留备份"
        return base + ("；维护记录保存不完整" if event.get("audit_degraded") else "")
    return PHASE_TEXT.get(event["phase"], "等待维护状态") + ("；维护记录保存不完整" if event.get("audit_degraded") else "")


def sanitize_event(raw, firmware=None):
    catalog = {fw.id: fw for fw in load_catalog()}
    if not isinstance(raw, dict) or raw.get("firmware_id") not in catalog:
        raise LiveError("protocol_error")
    known = catalog[raw["firmware_id"]]
    if firmware is not None and known != firmware:
        raise LiveError("protocol_error")
    progress = raw.get("progress")
    if (raw.get("phase") not in PHASES or raw.get("status") not in {"running", "succeeded", "failed"}
            or raw.get("version") != known.version or isinstance(progress, bool)
            or not isinstance(progress, (int, float)) or not math.isfinite(progress) or not 0 <= progress <= 1):
        raise LiveError("protocol_error")
    if any(name in raw and type(raw[name]) is not bool for name in FLAGS):
        raise LiveError("protocol_error")
    event = {key: raw[key] for key in ("phase", "status", "firmware_id", "version", "progress")}
    event.update({name: raw.get(name, False) for name in FLAGS})
    # Legacy firmware provides no verified running-version handshake.
    event["runtime_version_confirmed"] = False
    code = raw.get("code", "helper_failed")
    event["code"] = code if isinstance(code, str) and code in ERROR_TEXT else "helper_failed"
    if "job_id" in raw:
        if not isinstance(raw["job_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", raw["job_id"]):
            raise LiveError("protocol_error")
        event["job_id"] = raw["job_id"]
    if type(raw.get("timestamp")) is int and 0 <= raw["timestamp"] < 2**63:
        event["timestamp"] = raw["timestamp"]
    if raw.get("failed_phase") in PHASES:
        event["failed_phase"] = raw["failed_phase"]
    if event["status"] == "succeeded" and (event["phase"] != "complete" or not event["verified"] or not event["reconnected"]):
        event.update(phase="failed", status="failed", code="verification_incomplete")
    if event["status"] == "running" and event["phase"] in {"complete", "failed"}:
        raise LiveError("protocol_error")
    return event


def read_status(path=STATUS_PATH, *, trusted_uid=0):
    """Read bounded root-owned status records and a local change revision."""
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return [], None
    except OSError:
        raise LiveError("status_unavailable") from None
    try:
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != trusted_uid
                or before.st_mode & 0o022 or before.st_size > 128 * 1024):
            raise LiveError("status_unavailable")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise LiveError("status_unavailable")
            data = stream.read(128 * 1024 + 1)
            after = os.fstat(stream.fileno())
        if len(data) > 128 * 1024 or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise LiveError("status_unavailable")
        payload = json.loads(data)
        if (not isinstance(payload, dict) or payload.get("schema") != 1
                or not isinstance(payload.get("records"), list) or len(payload["records"]) > 20):
            raise LiveError("status_unavailable")
        records = [sanitize_event(row) for row in payload["records"]]
        return records, (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
    except (OSError, ValueError, LiveError):
        raise LiveError("status_unavailable") from None


class LiveController:
    def __init__(self, cache: ArtifactCache, *, popen=None, status_reader=None):
        self.cache = cache
        self._popen = popen or subprocess.Popen
        self._status_reader = status_reader or read_status
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.busy = False
        self.can_cancel = False
        self.last = None
        self.status_revision = None
        self.job_id = None
        self.previous_jobs = set()
        self._detached = []

    def records(self):
        return self._status_reader()[0]

    def pending_record(self):
        self._detached = [child for child in self._detached if child.poll() is None]
        records, revision = self._status_reader()
        for record in records:
            if self.job_id and record.get("job_id") == self.job_id:
                return record
        if (self.job_id is None and revision != self.status_revision and records
                and records[0].get("job_id") and records[0]["job_id"] not in self.previous_jobs
                and records[0]["firmware_id"] == WRITABLE_FIRMWARE_ID):
            self.job_id = records[0]["job_id"]
            return records[0]
        return None

    def _base(self, firmware, phase="prepare", status="running", **fields):
        event = {"firmware_id": firmware.id, "version": firmware.version,
                 "phase": phase, "status": status, "progress": 0.0}
        event.update({name: False for name in FLAGS})
        event.update(fields)
        return event

    def _emit(self, event, callback):
        self.last = dict(event)
        if callback is not None:
            # A UI subscriber disappearing must not interrupt maintenance.
            try:
                callback(dict(event))
            except Exception:
                pass

    def request_cancel(self, cancel):
        with self._state_lock:
            if self.can_cancel:
                cancel.set()
                return True
            return False

    def _source(self, path, firmware):
        source = None
        try:
            source = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != firmware.size:
                source.close()
                raise LiveError("cache_changed")
            data = source.read(firmware.size + 1)
            after = os.fstat(source.fileno())
            if (len(data) != firmware.size or hashlib.sha256(data).hexdigest() != firmware.sha256
                    or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)):
                source.close()
                raise LiveError("cache_changed")
            source.seek(0)
            return source
        except OSError:
            if source is not None:
                source.close()
            raise LiveError("cache_changed") from None

    def run_write(self, firmware: Firmware, cancel=None, on_event=None):
        if firmware not in load_catalog() or firmware.id != WRITABLE_FIRMWARE_ID:
            raise LiveError("unsupported_firmware")
        if not self._lock.acquire(blocking=False):
            raise LiveError("busy")
        cancel = cancel or threading.Event()
        self.busy, self.can_cancel = True, True
        child = None
        source = None
        flags = {name: False for name in FLAGS}
        self.last = self._base(firmware)
        self.job_id = None
        try:
            records, self.status_revision = self._status_reader()
            self.previous_jobs = {record.get("job_id") for record in records}
            if records and records[0]["status"] == "running":
                self.job_id = records[0].get("job_id")
                flags.update({name: records[0].get(name, False) for name in FLAGS})
                self.last = dict(records[0])
                raise LiveError("ongoing")
            self._emit(self.last, on_event)

            def progress(done, total):
                self._emit(self._base(firmware, progress=0.1 * done / total), on_event)

            path = self.cache.ensure(firmware, cancel=cancel, progress=progress)
            if cancel.is_set():
                raise Cancelled()
            source = self._source(path, firmware)
            with self._state_lock:
                if cancel.is_set():
                    raise Cancelled()
                self.can_cancel = False
            self._emit(self._base(firmware, "authorize", progress=0.1), on_event)
            try:
                child = self._popen([PKEXEC, HELPER, WRITABLE_FIRMWARE_ID], stdin=source,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    close_fds=True, start_new_session=True)
            except OSError:
                raise LiveError("helper_missing") from None
            terminal = None
            total_bytes = 0
            while True:
                line = child.stdout.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    break
                total_bytes += len(line)
                if len(line) > MAX_EVENT_BYTES or total_bytes > MAX_STREAM_BYTES or not line.endswith(b"\n"):
                    raise LiveError("protocol_error")
                try:
                    event = sanitize_event(json.loads(line), firmware)
                except (ValueError, UnicodeError):
                    raise LiveError("protocol_error") from None
                if terminal is not None:
                    raise LiveError("protocol_error")
                if self.job_id and event.get("job_id") != self.job_id:
                    raise LiveError("protocol_error")
                self.job_id = event.get("job_id", self.job_id)
                flags.update({name: flags[name] or event[name] for name in FLAGS})
                flags["runtime_version_confirmed"] = False
                event.update(flags)
                event["progress"] = max(self.last["progress"], event["progress"])
                if event["status"] in {"succeeded", "failed"}:
                    terminal = event
                else:
                    self._emit(event, on_event)
            try:
                returncode = child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise LiveError("result_unavailable") from None
            if terminal is not None and terminal["status"] == "succeeded" and returncode == 0:
                result = terminal
            elif terminal is not None and terminal["status"] == "failed":
                result = terminal
            else:
                code = "authorization_denied" if returncode in (126, 127) and not flags["write_started"] else "result_unavailable"
                result = self._base(firmware, "failed", "failed", code=code, **flags)
            self._emit(result, on_event)
            return result
        except (Cancelled, CacheError, LiveError) as exc:
            code = "cancelled" if isinstance(exc, Cancelled) else "cache_failed" if isinstance(exc, CacheError) else exc.code
            uncertain = code == "ongoing" or child is not None and child.poll() is None
            result = self._base(firmware, "failed", "failed", code=code, uncertain=uncertain,
                                progress=self.last["progress"], **flags)
            if self.job_id:
                result["job_id"] = self.job_id
            self._emit(result, on_event)
            return result
        except (OSError, ValueError):
            result = self._base(firmware, "failed", "failed", code="result_unavailable",
                                uncertain=child is not None and child.poll() is None, **flags)
            if self.job_id:
                result["job_id"] = self.job_id
            self._emit(result, on_event)
            return result
        finally:
            if source is not None:
                source.close()
            if child is not None and child.stdout is not None:
                child.stdout.close()
                if child.poll() is None:
                    self._detached.append(child)
            self.busy, self.can_cancel = False, False
            self._lock.release()
