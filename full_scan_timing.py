"""全面扫描 PTZ 侧结构化计时诊断。"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime


_SAFE_ID_RE = re.compile(r"[^0-9A-Za-z_-]+")
_ALLOWED_EXTRA_FIELDS = {
    "phase",
    "point_id",
    "point_type",
    "config_index",
    "config_count",
    "channel",
    "bandwidth",
    "success",
    "reason",
    "relay_used",
    "move_reason",
    "target_pan",
    "target_tilt",
    "attempted",
    "image_success",
    "event_count",
    "mac_count",
    "round_result_count",
    "serialized_bytes",
    "step_index",
    "is_first_point",
    "is_last_point",
    "sampling_complete",
}


def safe_round_id(round_id):
    """返回安全且长度受限的文件名片段。"""
    value = _SAFE_ID_RE.sub("_", str(round_id or "unknown")).strip("_")
    return (value or "unknown")[:160]


def _percentile(values, percentile):
    """标准库 nearest-rank 百分位；空集合返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    rank = max(1, int(math.ceil((float(percentile) / 100.0) * len(ordered))))
    return ordered[min(rank - 1, len(ordered) - 1)]


def _rounded(value):
    return round(float(value), 3)


class FullScanTimingTrace:
    """每轮一个文件的 JSONL 计时写入器。"""

    def __init__(
        self,
        *,
        enabled=False,
        directory="/tmp/logs",
        scan_id=None,
        round_id=None,
        strategy=None,
        logger=None,
    ):
        self.enabled = bool(enabled)
        self.scan_id = scan_id
        self.round_id = round_id
        self.strategy = strategy
        self.logger = logger
        self.path = None
        self._fp = None
        self._closed = False
        self._durations = {}
        self._writer_event_count = 0
        self._writer_total_ms = 0.0
        self._writer_max_ms = 0.0
        if not self.enabled:
            return

        self.path = os.path.join(
            str(directory or "/tmp/logs"),
            f"full_scan_{safe_round_id(round_id)}_timing.jsonl",
        )
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # 独占创建保证不会有两个写入器共享同一轮文件。
            self._fp = open(self.path, "x", encoding="utf-8")
        except Exception as exc:
            self._warning(f"全面扫描计时文件打开失败: {exc}")
            self._fp = None

    def _warning(self, message):
        try:
            if self.logger is not None:
                self.logger.warning(message)
        except Exception:
            pass

    def start(self):
        """返回计时起点；关闭时仅做一次轻量布尔判断。"""
        if not self.enabled:
            return None
        return time.monotonic_ns()

    def finish(self, operation, started_ns, **fields):
        if started_ns is None:
            return None
        duration_ms = (time.monotonic_ns() - started_ns) / 1_000_000.0
        self.record(operation, duration_ms, **fields)
        return duration_ms

    def record(self, operation, duration_ms, **fields):
        """追加一条字段受限的 JSON 记录并立即 flush。"""
        if not self.enabled:
            return False
        try:
            duration = max(0.0, float(duration_ms))
        except (TypeError, ValueError):
            duration = 0.0
        operation = str(operation or "unknown")
        self._durations.setdefault(operation, []).append(duration)

        event = {
            "ts": datetime.now().astimezone().isoformat(),
            "ts_unix_ns": time.time_ns(),
            "scan_id": self.scan_id,
            "round_id": self.round_id,
            "strategy": fields.pop("strategy", self.strategy),
            "operation": operation,
            "duration_ms": _rounded(duration),
        }
        for key, value in fields.items():
            if key in _ALLOWED_EXTRA_FIELDS and value is not None:
                event[key] = value

        if self._fp is None or self._closed:
            return False
        write_started_ns = time.monotonic_ns()
        try:
            self._fp.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            self._fp.write("\n")
            self._fp.flush()
        except Exception as exc:
            self._warning(f"全面扫描计时文件写入失败: {exc}")
            return False
        finally:
            write_ms = (time.monotonic_ns() - write_started_ns) / 1_000_000.0
            self._writer_event_count += 1
            self._writer_total_ms += write_ms
            self._writer_max_ms = max(self._writer_max_ms, write_ms)
        return True

    def close(self):
        """尽力关闭文件；可重复调用且绝不向业务层抛异常。"""
        if self._closed:
            return
        self._closed = True
        fp = self._fp
        self._fp = None
        if fp is None:
            return
        try:
            fp.flush()
        except Exception as exc:
            self._warning(f"全面扫描计时文件 flush 失败: {exc}")
        try:
            fp.close()
        except Exception as exc:
            self._warning(f"全面扫描计时文件关闭失败: {exc}")

    def summary(self):
        operations = {}
        for operation, values in sorted(self._durations.items()):
            count = len(values)
            total = sum(values)
            operations[operation] = {
                "count": count,
                "total_ms": _rounded(total),
                "avg_ms": _rounded(total / count) if count else 0.0,
                "p50_ms": _rounded(_percentile(values, 50)),
                "p95_ms": _rounded(_percentile(values, 95)),
                "max_ms": _rounded(max(values)) if values else 0.0,
            }
        return {
            "enabled": True,
            "path": self.path,
            "writer": {
                "event_count": self._writer_event_count,
                "total_write_ms": _rounded(self._writer_total_ms),
                "max_write_ms": _rounded(self._writer_max_ms),
            },
            "operations": operations,
        }
