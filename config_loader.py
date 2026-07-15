#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置加载器
从 config.json 加载配置，支持环境变量覆盖
优先级：环境变量 > config.json > 代码默认值
"""

import json
import os
import sys

# 配置文件路径
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

# 全局配置缓存
_config = None


def load_config():
    """
    加载配置文件
    
    Returns:
        dict: 配置字典
    """
    global _config
    
    if _config is not None:
        return _config
    
    # 默认配置
    default_config = {
        "redis": {
            "host": "localhost",
            "port": 6379
        },
        "网卡": {
            "定向网卡": "wlx200db0c22e38",
            "全向网卡": "wlx200db0c048a0",
            "定向网卡候选": [
                "wlx200db0c22e38",
                "wlx200db0c059a5"
            ],
            "全向网卡候选": [
                "wlx200db0c048a0",
                "wlx200db0c05a98"
            ]
        },
        "云台": {
            "串口": "/dev/ttyUSB0",
            "波特率": 9600,
            "设备地址": 1,
            "限位": {
                "水平最小": 0.0,
                "水平最大": 347.0,
                "垂直最小": -85.0,
                "垂直最大": 20.0
            },
            "默认扫描范围": {
                "水平最小": 0.0,
                "水平最大": 100.0,
                "垂直最小": 0.0,
                "垂直最大": 20.0,
                "水平步长": 20.0,
                "垂直步长": 5.0
            }
        },
        "摄像头": {
            "截图地址": "http://127.0.0.1:8080/?action=snapshot",
            "视频流地址": "http://127.0.0.1:8080/?action=stream",
            "标定文件": "calib/imx334_85fov/calibration.npz",
        },
        "定位扫描": {
            "扩边角度": 9.0,
            "快速校验信号阈值": 5.0,
            "收缩强点数量": 5,
            "收缩RSSI差值阈值": 4.0,
            "收缩离群角度阈值": 20.0,
            "单点兜底水平半宽": 8.0,
            "单点兜底垂直半宽": 6.0,
            "目标区内部保底点数": 5,
            "目标区分点最小跨度": 1.0,
        },
        "全面扫描": {
            "每点停留时长": 3,
            "保存扫描采样截图": False,
            "保存测试运行摘要": True,
            "测试运行摘要目录": "/tmp/logs",
            "保存测试计时明细": False,
            "粗扫步径": 10.0,
            "粗扫核心点数下限": 12,
            "粗扫外扩角度": 9.0,
            "粗扫交错等分数": 3,
            "细扫核心点数下限": 20,
            "细扫核心点数上限": 26,
            "细扫步径下限": 2.0,
            "细扫步径上限": 9.0,
            "细扫最少次数": 2,
            "细扫最多次数": 5,
            "小范围交错排序阈值": 1.0,
            "偏差区外扩等分数": 4,
            "偏差区每层点数": 5,
            "偏差区窄边阈值": 2.0,
            "偏差区正常模式面积比阈值": 0.7,
            "偏差区大范围模式面积比阈值": 0.3,
            "温启动白名单MAC阈值": 10,
            "温启动剩余信道扫描间隔点数": 2,
            "大范围峰值边缘保护比例": 0.15,
            "大范围RSSI标准差阈值": 3.0,
        },
        "全面扫描筛选": {
            "启用": True,
            "真实目标区最少命中点数": 2,
            "白名单缓冲区最少命中点数": 3,
            "整轮最少命中点数": 4,
            "白名单判定最小缓冲角": 1.0,
            "缓冲区相对外部最小强度差": 2.0,
            "分散强外部点检查": True,
            "定向相对全向最小强度差": 5.0,
            "要求最强点在工作区": True,
            "坐标去重角度": 0.05,
        },
        "天线偏差补偿": {
            "启用": True,
            "模式": "lateral_offset",
            "标定距离米": 220,
            "RF最强点像素": [4687, 635],
            "实际目标像素": [4710, 584],
            "最小距离米": 1,
            "最大补偿角度": 5.0,
        }
    }
    
    # 尝试加载配置文件
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                file_config = json.load(f)
            # 合并配置（文件配置覆盖默认配置）
            _config = _merge_config(default_config, file_config)
            print(f"[配置] 已从 {CONFIG_FILE} 加载配置")
        else:
            _config = default_config
            print(f"[配置] 配置文件不存在，使用默认配置: {CONFIG_FILE}")
    except Exception as e:
        print(f"[配置] 加载配置文件失败: {e}，使用默认配置")
        _config = default_config
    
    return _config


def _merge_config(default, override):
    """
    深度合并配置
    
    Args:
        default: 默认配置
        override: 覆盖配置
    
    Returns:
        dict: 合并后的配置
    """
    result = default.copy()
    
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_config(result[key], value)
        else:
            result[key] = value
    
    return result


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(',') if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _interface_exists(interface_name):
    if not interface_name:
        return False
    return os.path.exists(os.path.join('/sys/class/net', interface_name))


def _select_interface(configured, candidates, fallback="", optional=False):
    """
    选择网卡：
    - configured 为固定网卡名时直接使用，兼容旧配置。
    - configured 为空或 auto 时，从 candidates 里选择当前系统存在的第一个。
    - optional=True 且候选都不存在时返回空字符串，用于禁用全向网卡。
    """
    configured = str(configured or "").strip()
    candidate_list = _as_list(candidates)

    if configured and configured.lower() not in ("auto", "自动"):
        return configured

    for name in candidate_list:
        if _interface_exists(name):
            return name

    if optional:
        return ""
    return candidate_list[0] if candidate_list else fallback


def get_redis_config():
    """
    获取 Redis 配置，支持环境变量覆盖
    
    Returns:
        tuple: (host, port)
    """
    config = load_config()
    redis_config = config.get("redis", {})
    
    host = os.getenv('REDIS_HOST', redis_config.get("host", "localhost"))
    port = int(os.getenv('REDIS_PORT', redis_config.get("port", 6379)))
    
    return host, port


def get_capture_config():
    """
    获取抓包网卡配置，支持环境变量覆盖
    
    Returns:
        tuple: (directional_interface, omni_interface)
    """
    config = load_config()
    network_config = config.get("网卡", {})

    env_directional = os.getenv('CAPTURE_INTERFACE')
    env_omni = os.getenv('CAPTURE_OMNI_INTERFACE')

    directional = env_directional or _select_interface(
        network_config.get("定向网卡", "wlx200db0c22e38"),
        network_config.get("定向网卡候选", []),
        fallback="wlx200db0c22e38",
        optional=False,
    )
    omni = env_omni if env_omni is not None else _select_interface(
        network_config.get("全向网卡", "wlx200db0c048a0"),
        network_config.get("全向网卡候选", []),
        fallback="",
        optional=True,
    )
    
    return directional, omni


def get_ptz_config():
    """
    获取云台配置，支持环境变量覆盖
    
    Returns:
        dict: 云台配置
    """
    config = load_config()
    ptz_config = config.get("云台", {})
    
    return {
        "串口": os.getenv('PTZ_SERIAL_PORT', ptz_config.get("串口", "/dev/ttyUSB0")),
        "波特率": int(os.getenv('PTZ_BAUDRATE', ptz_config.get("波特率", 9600))),
        "设备地址": int(os.getenv('PTZ_DEVICE_ADDRESS', ptz_config.get("设备地址", 1))),
        "限位": {
            "水平最小": float(os.getenv('PTZ_LIMIT_PAN_MIN', ptz_config.get("限位", {}).get("水平最小", 0.0))),
            "水平最大": float(os.getenv('PTZ_LIMIT_PAN_MAX', ptz_config.get("限位", {}).get("水平最大", 347.0))),
            "垂直最小": float(os.getenv('PTZ_LIMIT_TILT_MIN', ptz_config.get("限位", {}).get("垂直最小", -85.0))),
            "垂直最大": float(os.getenv('PTZ_LIMIT_TILT_MAX', ptz_config.get("限位", {}).get("垂直最大", 20.0)))
        },
        "默认扫描范围": {
            "水平最小": float(os.getenv('PTZ_DEFAULT_PAN_MIN', ptz_config.get("默认扫描范围", {}).get("水平最小", 0.0))),
            "水平最大": float(os.getenv('PTZ_DEFAULT_PAN_MAX', ptz_config.get("默认扫描范围", {}).get("水平最大", 100.0))),
            "垂直最小": float(os.getenv('PTZ_DEFAULT_TILT_MIN', ptz_config.get("默认扫描范围", {}).get("垂直最小", 0.0))),
            "垂直最大": float(os.getenv('PTZ_DEFAULT_TILT_MAX', ptz_config.get("默认扫描范围", {}).get("垂直最大", 20.0))),
            "水平步长": float(os.getenv('PTZ_DEFAULT_PAN_STEP', ptz_config.get("默认扫描范围", {}).get("水平步长", 20.0))),
            "垂直步长": float(os.getenv('PTZ_DEFAULT_TILT_STEP', ptz_config.get("默认扫描范围", {}).get("垂直步长", 5.0)))
        }
    }


def get_location_scan_config():
    """
    获取定位扫描配置，支持环境变量覆盖

    Returns:
        dict: 定位扫描配置
            - 扩边角度: 每个扫描范围四周各扩展的角度（度），默认 9.0
            - 快速校验信号阈值: Fast Verify 信号衰减阈值（dB），默认 5.0
            - 收缩强点数量: 每轮收缩最多取前 N 个强点，默认 5
            - 收缩RSSI差值阈值: 强点必须不低于 best_rssi - 阈值，默认 4.0dB
            - 收缩离群角度阈值: 强点相对最强点最大角距离，默认 20.0°
            - 单点兜底水平半宽/垂直半宽: 过滤后只剩单点时的最小候选框半宽
            - 目标区内部保底点数: 每个目标区域除四角+中心外至少补扫的内部点数
            - 目标区分点最小跨度: 目标区域单轴超过该角度才沿该轴继续分点
    """
    config = load_config()
    loc_config = config.get("定位扫描", {})

    return {
        "扩边角度": float(os.getenv('LOCATION_SCAN_EXPAND_DEG',
                                    loc_config.get("扩边角度", 9.0))),
        "快速校验信号阈值": float(os.getenv('LOCATION_SCAN_TRACK_RSSI_THRESHOLD',
                                          loc_config.get("快速校验信号阈值", 5.0))),
        "收缩强点数量": int(os.getenv('LOCATION_SCAN_SHRINK_TOP_N',
                                    loc_config.get("收缩强点数量", 5))),
        "收缩RSSI差值阈值": float(os.getenv('LOCATION_SCAN_SHRINK_RSSI_DELTA',
                                          loc_config.get("收缩RSSI差值阈值", 4.0))),
        "收缩离群角度阈值": float(os.getenv('LOCATION_SCAN_SHRINK_OUTLIER_DEG',
                                          loc_config.get("收缩离群角度阈值", 20.0))),
        "单点兜底水平半宽": float(os.getenv('LOCATION_SCAN_SHRINK_SINGLE_PAN_HALF',
                                         loc_config.get("单点兜底水平半宽", 8.0))),
        "单点兜底垂直半宽": float(os.getenv('LOCATION_SCAN_SHRINK_SINGLE_TILT_HALF',
                                         loc_config.get("单点兜底垂直半宽", 6.0))),
        "目标区内部保底点数": int(os.getenv('LOCATION_SCAN_TARGET_GUARD_INNER_POINTS',
                                      loc_config.get("目标区内部保底点数", 5))),
        "目标区分点最小跨度": float(os.getenv('LOCATION_SCAN_TARGET_GUARD_MIN_SPAN_DEG',
                                      loc_config.get("目标区分点最小跨度", 1.0))),
    }


def get_full_scan_filter_config():
    """
    获取全面扫描筛选配置，支持环境变量覆盖

    Returns:
        dict: 全面扫描筛选配置
    """
    config = load_config()
    filter_config = config.get("全面扫描筛选", {})

    def _env_bool(name, default):
        def _to_bool(value):
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")

        raw = os.getenv(name)
        if raw in (None, ""):
            return _to_bool(default)
        return _to_bool(raw)

    return {
        "启用": _env_bool("FULL_SCAN_FILTER_ENABLED", filter_config.get("启用", True)),
        "真实目标区最少命中点数": int(os.getenv(
            "FULL_SCAN_FILTER_MIN_REAL_TARGET_HIT_POINTS",
            filter_config.get(
                "真实目标区最少命中点数",
                filter_config.get("目标区最少命中点数", filter_config.get("工作区最少命中点数", 2)),
            ),
        )),
        "白名单缓冲区最少命中点数": int(os.getenv(
            "FULL_SCAN_FILTER_MIN_BUFFER_HIT_POINTS",
            filter_config.get("白名单缓冲区最少命中点数", 3),
        )),
        "整轮最少命中点数": int(os.getenv(
            "FULL_SCAN_FILTER_MIN_TOTAL_HIT_POINTS",
            filter_config.get("整轮最少命中点数", 4),
        )),
        "白名单判定最小缓冲角": float(os.getenv(
            "FULL_SCAN_FILTER_MIN_DECISION_BUFFER_DEG",
            filter_config.get("白名单判定最小缓冲角", 1.0),
        )),
        "缓冲区相对外部最小强度差": float(os.getenv(
            "FULL_SCAN_FILTER_MIN_BUFFER_VS_OUTSIDE_RSSI_DELTA",
            filter_config.get(
                "缓冲区相对外部最小强度差",
                filter_config.get("工作区相对其他区域最小强度差", 2.0),
            ),
        )),
        "分散强外部点检查": _env_bool(
            "FULL_SCAN_FILTER_DISPERSED_EXTERNAL_CHECK",
            filter_config.get("分散强外部点检查", True),
        ),
        "定向相对全向最小强度差": float(os.getenv(
            "FULL_SCAN_FILTER_MIN_DIRECTIONAL_VS_OMNI_DELTA",
            filter_config.get("定向相对全向最小强度差", 5.0),
        )),
        "要求最强点在工作区": _env_bool(
            "FULL_SCAN_FILTER_REQUIRE_BEST_POINT_IN_WORK_AREA",
            filter_config.get("要求最强点在工作区", True),
        ),
        "坐标去重角度": float(os.getenv(
            "FULL_SCAN_FILTER_COORD_DEDUPE_DEG",
            filter_config.get("坐标去重角度", 0.05),
        )),
        "输出淘汰明细": _env_bool(
            "FULL_SCAN_FILTER_OUTPUT_REJECTED",
            filter_config.get("输出淘汰明细", True),
        ),
    }


def get_full_scan_config():
    """
    获取全面扫描参数配置，支持环境变量覆盖。

    三个阶段 dwell_time 各自独立（文档规范）：
        - 粗扫：0.25s ~ 0.5s / 信道（快速覆盖）
        - 细扫：0.5s ~ 1s / 信道
        - 偏差区 A/B：0.5s ~ 1s / 信道

    Returns:
        dict:
            - 粗扫每信道停留时长: 粗扫实际使用值，默认 0.3
            - 粗扫每信道停留时长下限: 跨轮次动态调整下限，默认 0.25
            - 粗扫每信道停留时长上限: 跨轮次动态调整上限，默认 0.5
            - 细扫每信道停留时长: 细扫实际使用值，默认 0.5
            - 细扫每信道停留时长下限: 默认 0.5
            - 细扫每信道停留时长上限: 默认 1.0
            - 偏差区每信道停留时长: 偏差区实际使用值，默认 0.5
            - 偏差区每信道停留时长下限: 默认 0.5
            - 偏差区每信道停留时长上限: 默认 1.0
            - 粗扫步径: 粗扫核心格心步径（度），默认 10.0
            - 粗扫外扩角度: 粗扫额外外部探测点外扩角度（度），默认 9.0
            - 粗扫交错等分数: 粗扫 9 宫格等分数，默认 3
            - 细扫步径下限: 细扫最小步径（度），默认 2.0
            - 细扫步径上限: 细扫最大步径（度），默认 9.0
            - 细扫最少次数: 细扫最少重复次数，默认 2
            - 细扫最多次数: 细扫最多重复次数，默认 5
            - 偏差区每层点数: 每个偏差区外扩层点数，默认 5
            - 偏差区窄边阈值: 小于等于该角度的外扩边进入贴边轮换采样，默认 2.0
            - 偏差区正常模式面积比阈值: dev_area_ratio >= 此值走正常模式，默认 0.7
            - 偏差区大范围模式面积比阈值: dev_area_ratio < 此值走大范围模式，默认 0.3
            - 温启动白名单MAC阈值: 白名单 MAC 数量达到此值后细扫进入温启动，默认 10
            - 温启动剩余信道扫描间隔点数: 温启动下每 N 个细扫点补扫剩余信道，默认 2
            - 大范围峰值边缘保护比例: 峰值距工作区边界至少为短边比例，默认 0.15
            - 大范围RSSI标准差阈值: 工作区内 RSSI 标准差阈值，默认 3.0
    """
    config = load_config()
    fs_config = config.get("全面扫描", {})

    def _env_bool(name, default):
        value = os.getenv(name)
        if value is None:
            return bool(default)
        return str(value).strip().lower() in ("1", "true", "yes", "on", "y")

    return {
        # 正式扫描采样截图默认关闭，避免逐点 HTTP 截图增加扫描耗时。
        "保存扫描采样截图": _env_bool(
            "FULL_SCAN_SAMPLING_CAPTURE_ENABLED",
            fs_config.get("保存扫描采样截图", False),
        ),
        # ── 测试运行摘要 ───────────────────────────────────────────────
        "保存测试运行摘要": _env_bool(
            "FULL_SCAN_TEST_SUMMARY_ENABLED",
            fs_config.get("保存测试运行摘要", True),
        ),
        "测试运行摘要目录": os.getenv(
            "FULL_SCAN_TEST_SUMMARY_DIR",
            fs_config.get("测试运行摘要目录", "/tmp/logs"),
        ),
        "保存测试计时明细": _env_bool(
            "FULL_SCAN_TIMING_TRACE_ENABLED",
            fs_config.get("保存测试计时明细", False),
        ),
        "允许单信道内部测试": _env_bool(
            "FULL_SCAN_ALLOW_SINGLE_CHANNEL_TEST",
            fs_config.get("允许单信道内部测试", False),
        ),
        "允许最小点位内部测试": _env_bool(
            "FULL_SCAN_ALLOW_MINIMAL_POINT_TEST",
            fs_config.get("允许最小点位内部测试", False),
        ),
        "配置优先持续采集": _env_bool(
            "FULL_SCAN_CONFIG_PRIORITY_COLLECT",
            fs_config.get("配置优先持续采集", False),
        ),
        "启用白名单位置复核": _env_bool(
            "FULL_SCAN_WHITELIST_REFINEMENT_ENABLED",
            fs_config.get("启用白名单位置复核", True),
        ),
        # ── 粗扫停留时长 ───────────────────────────────────────────────
        "粗扫每信道停留时长": float(os.getenv(
            "FULL_SCAN_COARSE_DWELL_TIME",
            fs_config.get("粗扫每信道停留时长", 0.3),
        )),
        "粗扫每信道停留时长下限": float(os.getenv(
            "FULL_SCAN_COARSE_DWELL_MIN",
            fs_config.get("粗扫每信道停留时长下限", 0.25),
        )),
        "粗扫每信道停留时长上限": float(os.getenv(
            "FULL_SCAN_COARSE_DWELL_MAX",
            fs_config.get("粗扫每信道停留时长上限", 0.5),
        )),
        # ── 细扫停留时长 ───────────────────────────────────────────────
        "细扫每信道停留时长": float(os.getenv(
            "FULL_SCAN_FINE_DWELL_TIME",
            fs_config.get("细扫每信道停留时长", 0.5),
        )),
        "细扫每信道停留时长下限": float(os.getenv(
            "FULL_SCAN_FINE_DWELL_MIN",
            fs_config.get("细扫每信道停留时长下限", 0.5),
        )),
        "细扫每信道停留时长上限": float(os.getenv(
            "FULL_SCAN_FINE_DWELL_MAX",
            fs_config.get("细扫每信道停留时长上限", 1.0),
        )),
        # ── 偏差区停留时长 ─────────────────────────────────────────────
        "偏差区每信道停留时长": float(os.getenv(
            "FULL_SCAN_DEVIATION_DWELL_TIME",
            fs_config.get("偏差区每信道停留时长", 0.5),
        )),
        "偏差区每信道停留时长下限": float(os.getenv(
            "FULL_SCAN_DEVIATION_DWELL_MIN",
            fs_config.get("偏差区每信道停留时长下限", 0.5),
        )),
        "偏差区每信道停留时长上限": float(os.getenv(
            "FULL_SCAN_DEVIATION_DWELL_MAX",
            fs_config.get("偏差区每信道停留时长上限", 1.0),
        )),
        # ── 步径与次数 ─────────────────────────────────────────────────
        "粗扫步径": float(os.getenv(
            "FULL_SCAN_COARSE_STEP",
            fs_config.get("粗扫步径", 10.0),
        )),
        "粗扫核心点数下限": int(os.getenv(
            "FULL_SCAN_COARSE_CORE_MIN",
            fs_config.get("粗扫核心点数下限", 12),
        )),
        "粗扫外扩角度": float(os.getenv(
            "FULL_SCAN_COARSE_OUTER_PROBE_DEG",
            fs_config.get("粗扫外扩角度", 9.0),
        )),
        "粗扫交错等分数": int(os.getenv(
            "FULL_SCAN_COARSE_SUBDIVISIONS",
            fs_config.get("粗扫交错等分数", 3),
        )),
        "细扫核心点数下限": int(os.getenv(
            "FULL_SCAN_FINE_CORE_MIN",
            fs_config.get("细扫核心点数下限", 20),
        )),
        "细扫核心点数上限": int(os.getenv(
            "FULL_SCAN_FINE_CORE_MAX",
            fs_config.get("细扫核心点数上限", 26),
        )),
        "细扫步径下限": float(os.getenv(
            "FULL_SCAN_FINE_STEP_MIN",
            fs_config.get("细扫步径下限", 2.0),
        )),
        "细扫步径上限": float(os.getenv(
            "FULL_SCAN_FINE_STEP_MAX",
            fs_config.get("细扫步径上限", 9.0),
        )),
        "细扫最少次数": int(os.getenv(
            "FULL_SCAN_FINE_COUNT_MIN",
            fs_config.get("细扫最少次数", 2),
        )),
        "细扫最多次数": int(os.getenv(
            "FULL_SCAN_FINE_COUNT_MAX",
            fs_config.get("细扫最多次数", 5),
        )),
        "小范围交错排序阈值": float(os.getenv(
            "FULL_SCAN_SMALL_MOVE_INTERLEAVE_DEG",
            fs_config.get("小范围交错排序阈值", 1.0),
        )),
        "偏差区外扩等分数": int(os.getenv(
            "FULL_SCAN_DEVIATION_DIVISIONS",
            fs_config.get("偏差区外扩等分数", 4),
        )),
        "偏差区每层点数": int(os.getenv(
            "FULL_SCAN_DEVIATION_POINTS_PER_LAYER",
            fs_config.get("偏差区每层点数", 5),
        )),
        "偏差区窄边阈值": float(os.getenv(
            "FULL_SCAN_DEVIATION_NARROW_SIDE_DEG",
            fs_config.get("偏差区窄边阈值", 2.0),
        )),
        # ── 优化策略阈值 ───────────────────────────────────────────────
        "偏差区正常模式面积比阈值": float(os.getenv(
            "FULL_SCAN_DEV_RATIO_NORMAL_MIN",
            fs_config.get("偏差区正常模式面积比阈值", 0.7),
        )),
        "偏差区大范围模式面积比阈值": float(os.getenv(
            "FULL_SCAN_DEV_RATIO_LARGE_MAX",
            fs_config.get("偏差区大范围模式面积比阈值", 0.3),
        )),
        "温启动白名单MAC阈值": int(os.getenv(
            "FULL_SCAN_WARM_START_THRESHOLD",
            fs_config.get("温启动白名单MAC阈值", 10),
        )),
        "温启动剩余信道扫描间隔点数": int(os.getenv(
            "FULL_SCAN_WARM_REMAINING_INTERVAL",
            fs_config.get("温启动剩余信道扫描间隔点数", 2),
        )),
        "大范围峰值边缘保护比例": float(os.getenv(
            "FULL_SCAN_LARGE_EDGE_RATIO",
            fs_config.get("大范围峰值边缘保护比例", 0.15),
        )),
        "大范围RSSI标准差阈值": float(os.getenv(
            "FULL_SCAN_LARGE_RSSI_STD_MIN",
            fs_config.get("大范围RSSI标准差阈值", 3.0),
        )),
    }


def get_camera_config():
    """
    获取摄像头配置，支持环境变量覆盖
    
    Returns:
        dict: 摄像头配置
    """
    config = load_config()
    camera_config = config.get("摄像头", {})
    
    default_calib = "calib/imx334_85fov/calibration.npz"
    calib_rel = os.getenv("CAMERA_CALIB_FILE", camera_config.get("标定文件", default_calib))
    return {
        "截图地址": os.getenv('CAMERA_SNAPSHOT_URL', camera_config.get("截图地址", "http://127.0.0.1:8080/?action=snapshot")),
        "视频流地址": os.getenv('CAMERA_STREAM_URL', camera_config.get("视频流地址", "http://127.0.0.1:8080/?action=stream")),
        "标定文件": calib_rel,
        "全景像素每度": float(os.getenv('CAMERA_PANORAMA_PX_PER_DEG', camera_config.get("全景像素每度", 36))),
    }


def get_wifi_connect_config():
    """
    获取 WiFi 连接配置，支持环境变量覆盖

    Returns:
        dict: WiFi 连接配置
    """
    config = load_config()
    wc_config = config.get("WiFi连接", {})
    return {
        "默认超时秒数": int(os.getenv('WIFI_CONNECT_DEFAULT_TIMEOUT', wc_config.get("默认超时秒数", 120))),
        "最大超时秒数": int(os.getenv('WIFI_CONNECT_MAX_TIMEOUT', wc_config.get("最大超时秒数", 300))),
        "最小超时秒数": int(os.getenv('WIFI_CONNECT_MIN_TIMEOUT', wc_config.get("最小超时秒数", 10))),
        "轮询间隔秒数": float(os.getenv('WIFI_CONNECT_POLL_INTERVAL', wc_config.get("轮询间隔秒数", 0.5))),
        "停止其他任务超时秒数": int(os.getenv('WIFI_CONNECT_STOP_OTHERS_TIMEOUT', wc_config.get("停止其他任务超时秒数", 30))),
        "临时配置文件目录": os.getenv('WIFI_CONNECT_TMP_DIR', wc_config.get("临时配置文件目录", "/tmp")),
        "DHCP客户端": os.getenv('WIFI_CONNECT_DHCP_CLIENT', wc_config.get("DHCP客户端", "auto")),
        "DHCP超时秒数": int(os.getenv('WIFI_CONNECT_DHCP_TIMEOUT', wc_config.get("DHCP超时秒数", 20))),
        "恢复监听信道": int(os.getenv('WIFI_CONNECT_RESTORE_CHANNEL', wc_config.get("恢复监听信道", 36))),
        "恢复监听带宽": os.getenv('WIFI_CONNECT_RESTORE_BW', wc_config.get("恢复监听带宽", "HT20")),
        "日志明文密码": wc_config.get("日志明文密码", False),
    }


def get_antenna_bias_config():
    """
    获取天线偏差补偿配置，支持环境变量覆盖

    Returns:
        dict: 天线偏差补偿配置
            - 启用: 是否启用偏差补偿
            - 模式: 补偿模式，第一版支持 lateral_offset
            - 标定距离米: 标定样本对应距离，只用于反推模型
            - RF最强点像素: [px, py] 未修正前定位出的 RF 最强点
            - 实际目标像素: [px, py] 前端全景图上真实目标点
            - 最小距离米: 防止距离过小导致角度发散
            - 最大补偿角度: 防御性上限（度）
    """
    config = load_config()
    ab_config = config.get("天线偏差补偿", {})

    def _env_bool(name, default):
        def _to_bool(value):
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        raw = os.getenv(name)
        if raw in (None, ""):
            return _to_bool(default)
        return _to_bool(raw)

    rf_px_raw = ab_config.get("RF最强点像素", [4687, 635])
    actual_px_raw = ab_config.get("实际目标像素", [4710, 584])

    # 环境变量可覆盖像素点（逗号分隔）
    env_rf = os.getenv("ANTENNA_BIAS_RF_PIXEL")
    if env_rf:
        try:
            rf_px_raw = [int(x.strip()) for x in env_rf.split(",")]
        except (ValueError, TypeError):
            pass
    env_actual = os.getenv("ANTENNA_BIAS_ACTUAL_PIXEL")
    if env_actual:
        try:
            actual_px_raw = [int(x.strip()) for x in env_actual.split(",")]
        except (ValueError, TypeError):
            pass

    return {
        "启用": _env_bool("ANTENNA_BIAS_ENABLED", ab_config.get("启用", True)),
        "模式": os.getenv("ANTENNA_BIAS_MODE", ab_config.get("模式", "lateral_offset")),
        "标定距离米": float(os.getenv("ANTENNA_BIAS_CALIBRATION_DISTANCE",
                                      ab_config.get("标定距离米", 220))),
        "RF最强点像素": rf_px_raw,
        "实际目标像素": actual_px_raw,
        "最小距离米": float(os.getenv("ANTENNA_BIAS_MIN_DISTANCE",
                                     ab_config.get("最小距离米", 1))),
        "最大补偿角度": float(os.getenv("ANTENNA_BIAS_MAX_BIAS_DEG",
                                      ab_config.get("最大补偿角度", 5.0))),
    }


# 测试代码
if __name__ == "__main__":
    print("=== 配置加载测试 ===")
    config = load_config()
    print(f"完整配置: {json.dumps(config, indent=2, ensure_ascii=False)}")
    print(f"\nRedis 配置: {get_redis_config()}")
    print(f"网卡配置: {get_capture_config()}")
    print(f"云台配置: {get_ptz_config()}")
    print(f"摄像头配置: {get_camera_config()}")
    print(f"定位扫描配置: {get_location_scan_config()}")
    print(f"全面扫描筛选配置: {get_full_scan_filter_config()}")
    print(f"全面扫描参数配置: {get_full_scan_config()}")
    print(f"天线偏差补偿配置: {get_antenna_bias_config()}")
