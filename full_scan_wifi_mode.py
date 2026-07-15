"""全面扫描任务级频段配置生成。

本模块只生成纯数据，不修改共享扫描配置或运行状态。
"""

FULL_SCAN_24_GHZ_CHANNELS = tuple(range(1, 14))
FULL_SCAN_5_GHZ_CHANNELS = (
    36, 40, 44, 48, 52, 56, 60, 64, 149, 153, 157, 161, 165,
)


def normalize_full_scan_wifi_mode(value):
    """规范化全面扫描 wifi_mode；双频模式返回 None。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('wifi_mode 仅支持 "2.4"、"5" 或留空')
    if value == "":
        return None
    if value not in {"2.4", "5"}:
        raise ValueError('wifi_mode 仅支持 "2.4"、"5" 或留空')
    return value


def build_full_scan_wifi_configs(value=None):
    """返回 ``(规范化模式, 本次任务的 HT20 配置列表)``。"""
    mode = normalize_full_scan_wifi_mode(value)
    if mode == "2.4":
        channels = FULL_SCAN_24_GHZ_CHANNELS
    elif mode == "5":
        channels = FULL_SCAN_5_GHZ_CHANNELS
    else:
        channels = FULL_SCAN_24_GHZ_CHANNELS + FULL_SCAN_5_GHZ_CHANNELS
    return mode, [
        {"channel": channel, "bandwidth": "HT20"}
        for channel in channels
    ]


def build_full_scan_single_channel_test_config(value):
    """构造内部测试专用的单信道 HT20 任务配置。"""
    if isinstance(value, bool):
        raise ValueError("test_channel 必须是支持的 WiFi 信道号")
    try:
        channel = int(value)
    except (TypeError, ValueError):
        raise ValueError("test_channel 必须是支持的 WiFi 信道号")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("test_channel 必须是支持的 WiFi 信道号")
    allowed = FULL_SCAN_24_GHZ_CHANNELS + FULL_SCAN_5_GHZ_CHANNELS
    if channel not in allowed:
        raise ValueError(
            "test_channel 仅支持 1-13、36/40/44/48/52/56/60/64/"
            "149/153/157/161/165"
        )
    mode = "2.4" if channel in FULL_SCAN_24_GHZ_CHANNELS else "5"
    return mode, [{"channel": channel, "bandwidth": "HT20"}]


def allowed_channels_from_configs(task_configs):
    """从任务配置列表中提取允许的信道号集合（去重、int）。

    用于偏差区等阶段过滤：只保留信道号在任务配置中出现过的候选，
    并强制使用 HT20 带宽，避免 AP 宣告带宽（HT40/VHT80）逸出任务锁定。
    """
    channels = set()
    for cfg in task_configs or []:
        ch = cfg.get("channel")
        if ch is not None:
            try:
                channels.add(int(ch))
            except (TypeError, ValueError):
                pass
    return channels
