#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天线偏差补偿工具模块

提供 RF 坐标与前端视觉坐标之间的偏差计算和转换函数。
第一版实现 distance_scaled_pixel 模式，后续可扩展 distance_lut / hybrid。

坐标系约定：
  visual = rf + bias   （RF → 前端展示）
  rf     = visual - bias（前端传入 → 实际移动）
"""

import math
import logging

from config_loader import get_camera_config, get_antenna_bias_config

logger = logging.getLogger(__name__)


def build_antenna_bias(target_distance):
    """
    根据配置和目标距离计算天线偏差参数。

    第一版使用 distance_scaled_pixel 模型：把标定样本的像素偏差按
    target_distance / calibration_distance_m 比例缩放，再换算为角度偏差。
    旧配置中的 mode=lateral_offset 作为兼容别名，内部按同一公式处理。

    Args:
        target_distance: 目标/信源与设备的直线距离（米），必须为正数。

    Returns:
        dict: 偏差信息字典，包含：
            - enabled (bool): 是否启用补偿
            - mode (str): 实际执行模式（distance_scaled_pixel）
            - configured_mode (str): 配置文件中的原始 mode
            - target_distance (float): 目标距离
            - calibration_distance_m (float): 标定距离
            - scale (float): 距离缩放因子
            - dx_px / dy_px (float): 标定像素偏差
            - dx_px_scaled / dy_px_scaled (float): 缩放后像素偏差
            - pan_bias_deg (float): 水平偏差角度（度）
            - tilt_bias_deg (float): 垂直偏差角度（度）
            - reason (str): 未启用时的原因说明
    """
    cfg = get_antenna_bias_config()

    # 基础检查
    if not cfg.get("启用", False):
        return {"enabled": False, "mode": cfg.get("模式", ""),
                "configured_mode": cfg.get("模式", ""),
                "reason": "配置中天线偏差补偿已禁用"}

    configured_mode = cfg.get("模式", "distance_scaled_pixel")
    # 第一版：lateral_offset 作为 distance_scaled_pixel 的兼容别名
    EXECUTED_MODE = "distance_scaled_pixel"
    mode = EXECUTED_MODE

    if configured_mode not in (EXECUTED_MODE, "lateral_offset"):
        return {"enabled": False, "mode": configured_mode,
                "configured_mode": configured_mode,
                "reason": f"未实现的补偿模式: {configured_mode}"}

    # 校验 target_distance（Redis 读出来可能是 str/bytes，统一转 float）
    min_dist = cfg.get("最小距离米", 1.0)
    if target_distance is None:
        return {"enabled": False, "mode": mode, "configured_mode": configured_mode,
                "reason": "target_distance 缺失"}
    try:
        target_distance = float(target_distance)
    except (ValueError, TypeError):
        return {"enabled": False, "mode": mode, "configured_mode": configured_mode,
                "reason": f"target_distance 无法转为数值: {target_distance!r}"}
    if not math.isfinite(target_distance):
        return {"enabled": False, "mode": mode, "configured_mode": configured_mode,
                "reason": f"target_distance 非有限数值: {target_distance!r}"}
    if target_distance < min_dist:
        return {"enabled": False, "mode": mode, "configured_mode": configured_mode,
                "reason": f"target_distance({target_distance}) < 最小距离({min_dist})"}

    # 标定参数
    calib_dist = cfg.get("标定距离米", 220.0)
    rf_pixel = cfg.get("RF最强点像素", [4687, 635])
    actual_pixel = cfg.get("实际目标像素", [4710, 584])
    max_bias_deg = cfg.get("最大补偿角度", 5.0)

    # 校验像素点必须是长度为 2 的数值列表
    def _validate_pixel(name, val):
        if not isinstance(val, (list, tuple)) or len(val) != 2:
            return None
        try:
            return [float(val[0]), float(val[1])]
        except (ValueError, TypeError):
            return None

    rf_pixel = _validate_pixel("RF最强点像素", rf_pixel)
    actual_pixel = _validate_pixel("实际目标像素", actual_pixel)
    if rf_pixel is None or actual_pixel is None:
        return {"enabled": False, "mode": mode, "configured_mode": configured_mode,
                "reason": f"像素配置无效: RF最强点像素={cfg.get('RF最强点像素')}, "
                          f"实际目标像素={cfg.get('实际目标像素')}"}

    # 像素每度（从摄像头配置读取）
    px_per_deg = get_camera_config().get("全景像素每度", 36.0)
    if px_per_deg <= 0:
        return {"enabled": False, "mode": mode, "configured_mode": configured_mode,
                "reason": f"全景像素每度无效: {px_per_deg}"}

    dx_px = actual_pixel[0] - rf_pixel[0]   # +23
    dy_px = actual_pixel[1] - rf_pixel[1]   # -51

    # distance_scaled_pixel 公式：按距离比例缩放像素偏差，再换算角度
    scale = target_distance / calib_dist
    dx_px_scaled = dx_px * scale
    dy_px_scaled = dy_px * scale

    pan_bias_deg = dx_px_scaled / px_per_deg
    tilt_bias_deg = -dy_px_scaled / px_per_deg  # py 越小越靠上，tilt 越大

    # 防御性 clamp
    pan_bias_deg = max(-max_bias_deg, min(max_bias_deg, pan_bias_deg))
    tilt_bias_deg = max(-max_bias_deg, min(max_bias_deg, tilt_bias_deg))

    # 精度：保留 1 位小数
    pan_bias_deg = round(pan_bias_deg, 1)
    tilt_bias_deg = round(tilt_bias_deg, 1)

    return {
        "enabled": True,
        "mode": mode,
        "configured_mode": configured_mode,
        "target_distance": target_distance,
        "calibration_distance_m": calib_dist,
        "scale": round(scale, 4),
        "dx_px": round(dx_px, 2),
        "dy_px": round(dy_px, 2),
        "dx_px_scaled": round(dx_px_scaled, 2),
        "dy_px_scaled": round(dy_px_scaled, 2),
        "pan_bias_deg": pan_bias_deg,
        "tilt_bias_deg": tilt_bias_deg,
    }


def visual_point_to_rf(point, bias):
    """
    将视觉坐标转换为 RF 移动坐标。

    Args:
        point: {"pan": float, "tilt": float} 视觉坐标
        bias: build_antenna_bias 返回的偏差字典

    Returns:
        {"pan": float, "tilt": float} RF 坐标
    """
    if not bias or not bias.get("enabled"):
        return point
    return {
        "pan": round(point["pan"] - bias["pan_bias_deg"], 1),
        "tilt": round(point["tilt"] - bias["tilt_bias_deg"], 1),
    }


def rf_point_to_visual(point, bias):
    """
    将 RF 扫描坐标转换为视觉坐标。

    Args:
        point: {"pan": float, "tilt": float} RF 坐标
        bias: build_antenna_bias 返回的偏差字典

    Returns:
        {"pan": float, "tilt": float} 视觉坐标
    """
    if not bias or not bias.get("enabled"):
        return point
    return {
        "pan": round(point["pan"] + bias["pan_bias_deg"], 1),
        "tilt": round(point["tilt"] + bias["tilt_bias_deg"], 1),
    }


def visual_range_to_rf(pan_range, tilt_range, bias):
    """
    将视觉范围转换为 RF 扫描范围。

    Args:
        pan_range: [min, max] 视觉水平范围
        tilt_range: [min, max] 视觉垂直范围
        bias: build_antenna_bias 返回的偏差字典

    Returns:
        (rf_pan_range, rf_tilt_range) 元组
    """
    if not bias or not bias.get("enabled"):
        return (pan_range, tilt_range)
    pb = bias["pan_bias_deg"]
    tb = bias["tilt_bias_deg"]
    return (
        [round(pan_range[0] - pb, 1), round(pan_range[1] - pb, 1)],
        [round(tilt_range[0] - tb, 1), round(tilt_range[1] - tb, 1)],
    )


# ── 自检 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json

    # === 220m 样本 ===
    bias = build_antenna_bias(220)
    print("=== 220m 样本自检 ===")
    print(json.dumps(bias, indent=2, ensure_ascii=False))
    assert bias.get("enabled"), f"220m 应启用: {bias}"
    assert bias["mode"] == "distance_scaled_pixel", f"mode 应为 distance_scaled_pixel: {bias['mode']}"
    assert abs(bias["pan_bias_deg"] - 0.6) < 0.2, f"pan_bias 偏差过大: {bias['pan_bias_deg']}"
    assert abs(bias["tilt_bias_deg"] - 1.4) < 0.2, f"tilt_bias 偏差过大: {bias['tilt_bias_deg']}"
    assert abs(bias["scale"] - 1.0) < 0.01, f"scale 应为 1.0: {bias['scale']}"
    print("[OK] 220m 样本通过: pan={pan_bias_deg}, tilt={tilt_bias_deg}".format(**bias))

    # === 5m 样本：不应被放大到 5° ===
    bias_5m = build_antenna_bias(5)
    print("\n=== 5m 样本自检 ===")
    print(json.dumps(bias_5m, indent=2, ensure_ascii=False))
    assert bias_5m.get("enabled"), f"5m 应启用: {bias_5m}"
    assert bias_5m["pan_bias_deg"] == 0.0, f"5m pan_bias 应为 0.0: {bias_5m['pan_bias_deg']}"
    assert bias_5m["tilt_bias_deg"] == 0.0, f"5m tilt_bias 应为 0.0: {bias_5m['tilt_bias_deg']}"
    assert abs(bias_5m["scale"] - 5 / 220) < 0.001, f"5m scale 应为 5/220: {bias_5m['scale']}"
    print("[OK] 5m 样本通过: pan={pan_bias_deg}, tilt={tilt_bias_deg}".format(**bias_5m))

    # === 方向测试：visual -> rf 减偏差，rf -> visual 加偏差 ===
    v = {"pan": 50.0, "tilt": 10.0}
    rf = visual_point_to_rf(v, bias)
    v2 = rf_point_to_visual(rf, bias)
    print(f"\n=== 方向测试 ===")
    print(f"  visual={v} -> rf={rf} -> visual={v2}")
    assert rf["pan"] < v["pan"], "visual->rf 应减 pan 偏差"
    assert rf["tilt"] < v["tilt"], "visual->rf 应减 tilt 偏差"
    assert abs(v2["pan"] - v["pan"]) < 0.1 and abs(v2["tilt"] - v["tilt"]) < 0.1
    print("[OK] 双向转换一致，方向正确")

    # === str/bytes（模拟 Redis 读出）===
    bias_str = build_antenna_bias("220")
    assert bias_str.get("enabled"), f"str 距离应启用: {bias_str}"
    assert abs(bias_str["pan_bias_deg"] - bias["pan_bias_deg"]) < 0.01
    print("[OK] str target_distance 通过")

    bias_bytes = build_antenna_bias(b"220")
    assert bias_bytes.get("enabled"), f"bytes 距离应启用: {bias_bytes}"
    print("[OK] bytes target_distance 通过")

    # === 禁用场景 ===
    for val, label in [(None, "None"), ("abc", "abc"), ("nan", "nan"),
                       ("inf", "inf"), (0, "0"), (-5, "-5")]:
        r = build_antenna_bias(val)
        assert not r.get("enabled"), f"{label} 应禁用: {r}"
        print(f"[OK] {label} -> enabled=false, reason={r.get('reason')}")

    # === 最小距离边界 ===
    bias_below_min = build_antenna_bias(0.5)
    assert not bias_below_min.get("enabled"), f"0.5m 应禁用: {bias_below_min}"
    print("[OK] 小于最小距离正确禁用")

    print("\n[OK] 全部自检完成")
