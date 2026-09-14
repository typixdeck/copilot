"""Bounded local diagnostics: fixed fields, no raw serial data or exception text."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat

MAX_LOG_BYTES = 128 * 1024
MAX_LOG_EVENTS = 192
MAX_ELAPSED_MS = 7 * 86400 * 1000
PHASE_NAMES = {'prepare': '镜像准备与校验', 'authorize': '系统授权', 'enter': '进入维护模式',
               'connect': '连接与芯片检查', 'backup': '备份原固件', 'write': '写入固件',
               'verify': '独立回读校验', 'restart': '重启与重新连接', 'complete': '完成', 'failed': '失败'}
ERROR_CATEGORIES = {'stream-stopped': '串口数据流中断', 'packet-stopped': '数据包接收中断',
                    'slip-framing': '串口协议帧头异常', 'slip-escape': '串口协议转义异常',
                    'corrupt-frame': '读取数据包长度异常', 'digest-frame': '未收到完整校验摘要',
                    'digest-mismatch': '读取内容摘要不一致', 'timeout': '通信超时',
                    'disconnected': '串口设备断开'}
MODULES = frozenset({'builtins', 'termios', 'serial', 'serial.serialutil', 'serial.serialposix',
    'esptool', 'esptool.loader', 'esptool.cmds', 'esptool.util', 'esptool.logger', 'esptool.reset',
    'esptool.bin_image', 'esptool.targets', 'esptool.targets.esp32', 'esptool.targets.esp32s3',
    'typix_copilot.writer', 'typix_copilot.device', 'typix_copilot.core'})
NUMBERS = {'started_at': 2**40, 'elapsed_ms': MAX_ELAPSED_MS,
           'attempted_offset': 16*1024*1024, 'last_checked_bytes': 16*1024*1024,
           'read_bytes': 16*1024*1024, 'read_total_bytes': 16*1024*1024,
           'backup_bytes': 16*1024*1024, 'verified_bytes': 16*1024*1024,
           'flash_capacity': 16*1024*1024, 'error_errno': 4096}
NUMBERS.update(chunk_received_bytes=65536, chunk_requested_bytes=65536,
               last_packet_elapsed_ms=MAX_ELAPSED_MS)


class DiagnosticError(ValueError):
    pass


def _type_name(value):
    if not isinstance(value, str):
        return None
    module, _, name = value.rpartition('.')
    if module in MODULES and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', name):
        return value
    return 'unknown' if value == 'unknown' else None


def sanitize_diagnostics(raw):
    result = {}
    for key, limit in NUMBERS.items():
        if type(raw.get(key)) is int and 0 <= raw[key] <= limit:
            result[key] = raw[key]
    for key in ('error_type', 'cleanup_error_type'):
        name = _type_name(raw.get(key))
        if name:
            result[key] = name
    if isinstance(raw.get('error_category'), str) and raw['error_category'] in ERROR_CATEGORIES:
        result['error_category'] = raw['error_category']
    for key in ('boot_requested', 'awaiting_digest'):
        if type(raw.get(key)) is bool:
            result[key] = raw[key]
    frames = raw.get('error_frames')
    if isinstance(frames, list):
        cleaned = []
        for frame in frames[:8]:
            if (isinstance(frame, dict) and isinstance(frame.get('module'), str) and frame['module'] in MODULES
                    and isinstance(frame.get('function'), str)
                    and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}|<(?:module|lambda|listcomp|dictcomp|setcomp|genexpr)>', frame['function'])
                    and type(frame.get('line')) is int and 0 < frame['line'] <= 1000000):
                cleaned.append({key: frame[key] for key in ('module', 'function', 'line')})
        result['error_frames'] = cleaned
    return result


def log_event(record):
    from .live import sanitize_event
    cleaned = sanitize_event(record)
    keys = ('phase', 'status', 'progress', 'elapsed_ms', 'code', 'failed_phase', 'backup_complete',
            'write_started', 'verified', 'reconnected', 'power_state_verified', 'boot_requested', 'audit_degraded',
            *NUMBERS, 'awaiting_digest', 'error_type', 'cleanup_error_type', 'error_category', 'error_frames')
    return {key: cleaned[key] for key in keys if key in cleaned}


def _identity(record):
    return {key: record[key] for key in ('job_id', 'firmware_id', 'version', 'image_sha256', 'image_size') if key in record}


def validate_log(raw, record):
    from .live import LiveError
    if (not isinstance(raw, dict) or raw.get('schema') != 1 or type(raw.get('schema')) is not int
            or type(raw.get('truncated')) is not bool
            or not isinstance(raw.get('events'), list) or len(raw['events']) > MAX_LOG_EVENTS
            or raw.get('identity') != _identity(record)):
        raise DiagnosticError('日志与此写入记录不匹配。')
    events = []
    previous = -1
    for item in raw['events']:
        if (not isinstance(item, dict) or type(item.get('elapsed_ms')) is not int
                or not 0 <= item['elapsed_ms'] <= MAX_ELAPSED_MS or item['elapsed_ms'] < previous):
            raise DiagnosticError('日志阶段时间无效。')
        try:
            cleaned = log_event({**item, **_identity(record)})
        except (LiveError, ValueError, TypeError):
            raise DiagnosticError('日志阶段内容无效。') from None
        previous = item['elapsed_ms']
        events.append(cleaned)
    return {'schema': 1, 'identity': _identity(record), 'events': events, 'truncated': raw['truncated']}


def read_job_log(record, root=Path('/var/lib/typix-copilot/logs'), trusted_uid=0):
    identifier = record.get('job_id')
    if identifier is None:
        return None
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise DiagnosticError('任务编号无效。')
    root = Path(root).absolute()
    descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Descriptor traversal prevents a symlink or swapped parent from redirecting reads.
        for name in root.parts[1:]:
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                return None
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != trusted_uid or info.st_mode & 0o022:
            raise DiagnosticError('日志目录权限无效。')
        try:
            handle = os.open(identifier + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        except FileNotFoundError:
            return None
        with os.fdopen(handle, 'rb') as source:
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != trusted_uid
                    or before.st_mode & 0o022 or not 0 < before.st_size <= MAX_LOG_BYTES):
                raise DiagnosticError('日志文件权限或大小无效。')
            data = source.read(MAX_LOG_BYTES + 1)
            after = os.fstat(source.fileno())
            if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != before.st_size):
                raise DiagnosticError('日志读取期间已变化，请刷新。')
        return validate_log(json.loads(data), record)
    except DiagnosticError:
        raise
    except (OSError, ValueError, RecursionError, TypeError):
        raise DiagnosticError('日志无法安全读取，请刷新后重试。') from None
    finally:
        os.close(descriptor)


def _seconds(value):
    return f'{value / 1000:.2f} 秒'


def format_record_log(record, details=None):
    from .live import sanitize_event, event_message, LiveError
    try:
        record = sanitize_event(record)
    except (LiveError, TypeError, ValueError):
        return '写入记录无效，无法显示详细日志。'
    rows = [f"固件：{record['firmware_id']} · {record['version']}"]
    if 'job_id' in record:
        rows.append('任务：' + record['job_id'])
    timestamp = record.get('started_at', record.get('timestamp'))
    if timestamp is not None:
        try:
            rows.append('记录时间：' + datetime.fromtimestamp(timestamp).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'))
        except (OSError, ValueError, OverflowError):
            rows.append('记录时间：不可用')
    rows.append('结果：' + event_message(record))
    if 'elapsed_ms' in record:
        rows.append('已用时间：' + _seconds(record['elapsed_ms']))
    if 'image_sha256' in record:
        rows.append('镜像 SHA-256：' + record['image_sha256'])
    if 'image_size' in record:
        rows.append(f"镜像大小：{record['image_size']:,} 字节")
    rows.append('备份：' + ('完整' if record['backup_complete'] else '未完成'))
    rows.append('实际写入：' + ('已开始' if record['write_started'] else '未开始'))
    rows.append('回读校验：' + ('通过' if record['verified'] else '未确认'))
    rows.append('重新连接：' + ('已确认' if record['reconnected'] else '未确认'))
    if record.get('failed_phase') in PHASE_NAMES:
        rows.append('失败阶段：' + PHASE_NAMES[record['failed_phase']])
    if 'attempted_offset' in record:
        rows.append(f"失败/最后读取位置：0x{record['attempted_offset']:08X}")
    if 'last_checked_bytes' in record:
        rows.append(f"已校验读取：{record['last_checked_bytes']:,} 字节")
    if 'chunk_received_bytes' in record:
        rows.append(f"当前块已接收：{record['chunk_received_bytes']:,}/{record.get('chunk_requested_bytes', 0):,} 字节")
    if record.get('awaiting_digest') and record.get('error_category') not in {'digest-mismatch', 'digest-frame'}:
        rows.append('中断位置：等待当前块的校验摘要')
    if 'last_packet_elapsed_ms' in record:
        rows.append('距最后数据包：' + _seconds(record['last_packet_elapsed_ms']))
    if 'error_category' in record:
        rows.append('通信原因：' + ERROR_CATEGORIES[record['error_category']])
    if 'error_type' in record:
        rows.append('异常类型：' + record['error_type'])
    if 'error_errno' in record:
        rows.append(f"系统错误编号：{record['error_errno']}")
    for frame in record.get('error_frames', []):
        rows.append(f"  {frame['module']}.{frame['function']} : {frame['line']}")
    rows.append('')
    if details is None:
        rows.append('此记录未保存完整阶段日志，仅显示当时可用信息。')
    else:
        try:
            details = validate_log(details, record)
        except DiagnosticError:
            rows.append('详细日志校验失败，未显示内容。')
            return '\n'.join(rows)
        events = details['events']
        terminal_keys = ('phase', 'status', 'backup_complete', 'write_started', 'verified', 'reconnected')
        if (record['status'] != 'running' and (not events or any(
                events[-1].get(key) != record.get(key) for key in terminal_keys))):
            rows.append('阶段日志未保存到最终状态，以下仅为已保存片段。')
        rows.append('阶段日志（从系统写入组件开始计时）')
        for event in events:
            extra = ''
            if 'read_bytes' in event and 'read_total_bytes' in event:
                extra = f" · 已读取 {event['read_bytes']:,}/{event['read_total_bytes']:,} 字节"
            if event['status'] == 'failed':
                extra += ' · ' + event_message({**record, **event})
            rows.append(f"+{event['elapsed_ms']/1000:7.2f}s  {PHASE_NAMES[event['phase']]}  {event['progress']:.0%}{extra}")
        if details['truncated']:
            rows.append('阶段事件达到保存上限，部分较早进度已省略；首条与最终状态保留。')
    return '\n'.join(rows)
