#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云台「业务扫描范围」与硬件限位 —— 一律以 Redis 为准。

- 硬件限位：PTZ Worker 写入的 ptz:limits（与串口设备一致）
- 业务范围：前端与其它模块约定的 gimbal:default_config（hash），与 capture_worker / grid_utils 同源

config.json 仅作 Redis 不可用时的兜底，不作为线上实时唯一来源。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

GIMBAL_DEFAULT_CONFIG_KEY = "gimbal:default_config"


def _parse_range_field(raw: Any, fallback: Tuple[float, float]) -> Tuple[float, float]:
    if raw is None:
        return float(fallback[0]), float(fallback[1])
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        a, b = float(raw[0]), float(raw[1])
    elif isinstance(raw, str):
        arr = json.loads(raw)
        a, b = float(arr[0]), float(arr[1])
    else:
        return float(fallback[0]), float(fallback[1])
    lo, hi = (a, b) if a <= b else (b, a)
    return lo, hi


def hardware_limits_from_redis(r, fallback: Dict[str, float]) -> Dict[str, float]:
    """
    读取 PTZ Worker 发布的设备限位；无则使用 fallback（通常为 config.json 加载的常量）。

    Returns:
        dict: pan_min, pan_max, tilt_min, tilt_max
    """
    try:
        raw = r.get("ptz:limits")
        if raw:
            data = json.loads(raw)
            pan = data.get("pan") or {}
            tilt = data.get("tilt") or {}
            return {
                "pan_min": float(pan.get("min", fallback["pan_min"])),
                "pan_max": float(pan.get("max", fallback["pan_max"])),
                "tilt_min": float(tilt.get("min", fallback["tilt_min"])),
                "tilt_max": float(tilt.get("max", fallback["tilt_max"])),
            }
    except Exception:
        pass
    return dict(fallback)


def business_scan_range_from_redis(
    r,
    hardware_fallback: Dict[str, float],
    config_default_scan: Dict[str, float],
) -> Dict[str, Any]:
    """
    业务扫描矩形 + 步长：优先 Redis hash gimbal:default_config，并与硬件限位求交集。

    config_default_scan 键：pan_min, pan_max, tilt_min, tilt_max, pan_step, tilt_step

    Returns:
        pan_min, pan_max, tilt_min, tilt_max, pan_step, tilt_step,
        source: 'redis_gimbal' | 'config_default'
    """
    hw = hardware_limits_from_redis(r, hardware_fallback)

    def _clamp_range(lo: float, hi: float, axis: str) -> Tuple[float, float]:
        if axis == "pan":
            a = max(hw["pan_min"], min(lo, hi))
            b = min(hw["pan_max"], max(lo, hi))
        else:
            a = max(hw["tilt_min"], min(lo, hi))
            b = min(hw["tilt_max"], max(lo, hi))
        if a > b:
            if axis == "pan":
                return hw["pan_min"], hw["pan_max"]
            return hw["tilt_min"], hw["tilt_max"]
        return a, b

    try:
        h = r.hgetall(GIMBAL_DEFAULT_CONFIG_KEY)
    except Exception:
        h = {}

    if not h:
        pmn, pmx = _clamp_range(
            config_default_scan["pan_min"], config_default_scan["pan_max"], "pan"
        )
        tmn, tmx = _clamp_range(
            config_default_scan["tilt_min"], config_default_scan["tilt_max"], "tilt"
        )
        return {
            "pan_min": pmn,
            "pan_max": pmx,
            "tilt_min": tmn,
            "tilt_max": tmx,
            "pan_step": float(config_default_scan.get("pan_step", 20.0)),
            "tilt_step": float(config_default_scan.get("tilt_step", 5.0)),
            "source": "config_default",
        }

    pr = _parse_range_field(
        h.get("pan_range"),
        (config_default_scan["pan_min"], config_default_scan["pan_max"]),
    )
    tr = _parse_range_field(
        h.get("tilt_range"),
        (config_default_scan["tilt_min"], config_default_scan["tilt_max"]),
    )

    pmn, pmx = _clamp_range(pr[0], pr[1], "pan")
    tmn, tmx = _clamp_range(tr[0], tr[1], "tilt")

    try:
        ps = float(h.get("pan_step", config_default_scan.get("pan_step", 20.0)))
    except (TypeError, ValueError):
        ps = float(config_default_scan.get("pan_step", 20.0))
    try:
        ts = float(h.get("tilt_step", config_default_scan.get("tilt_step", 5.0)))
    except (TypeError, ValueError):
        ts = float(config_default_scan.get("tilt_step", 5.0))

    return {
        "pan_min": pmn,
        "pan_max": pmx,
        "tilt_min": tmn,
        "tilt_max": tmx,
        "pan_step": ps,
        "tilt_step": ts,
        "source": "redis_gimbal",
    }


def full_area_precheck_range_from_redis(
    r,
    hardware_fallback: Dict[str, float],
    config_default_scan: Dict[str, float],
) -> Dict[str, Any]:
    """
    全面扫描初筛范围：优先使用 Redis hash gimbal:default_config 中的
    work_pan_range/work_tilt_range；缺失或解析失败时回退到 pan_range/tilt_range。

    注意：这里的 work_* 是前端写入的“初筛范围”，不是目标细扫范围。
    返回值已与硬件限位求交集。
    """
    base = business_scan_range_from_redis(r, hardware_fallback, config_default_scan)
    hw = hardware_limits_from_redis(r, hardware_fallback)

    def _clamp_range(lo: float, hi: float, axis: str) -> Tuple[float, float]:
        if axis == "pan":
            a = max(hw["pan_min"], min(lo, hi))
            b = min(hw["pan_max"], max(lo, hi))
            if a > b:
                return base["pan_min"], base["pan_max"]
        else:
            a = max(hw["tilt_min"], min(lo, hi))
            b = min(hw["tilt_max"], max(lo, hi))
            if a > b:
                return base["tilt_min"], base["tilt_max"]
        return a, b

    try:
        h = r.hgetall(GIMBAL_DEFAULT_CONFIG_KEY)
        if h and h.get("work_pan_range") is not None and h.get("work_tilt_range") is not None:
            pr = _parse_range_field(
                h.get("work_pan_range"),
                (base["pan_min"], base["pan_max"]),
            )
            tr = _parse_range_field(
                h.get("work_tilt_range"),
                (base["tilt_min"], base["tilt_max"]),
            )
            pmn, pmx = _clamp_range(pr[0], pr[1], "pan")
            tmn, tmx = _clamp_range(tr[0], tr[1], "tilt")
            return {
                "pan_min": pmn,
                "pan_max": pmx,
                "tilt_min": tmn,
                "tilt_max": tmx,
                "source": "redis_gimbal_work",
                "fallback_source": base.get("source"),
            }
    except Exception:
        pass

    return {
        "pan_min": base["pan_min"],
        "pan_max": base["pan_max"],
        "tilt_min": base["tilt_min"],
        "tilt_max": base["tilt_max"],
        "source": f"{base.get('source', 'unknown')}_pan_tilt",
    }
