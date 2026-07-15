import redis
import json
import time
import signal
import os
import threading
import shutil
from datetime import datetime, timezone
import subprocess
# [核心修改] 导入 Scapy 和 PcapWriter
from scapy.all import sniff, RadioTap, Dot11
from scapy.utils import PcapWriter

# [新增] 导入日志系统
import logging

# 版本标识：用于确认代码是否同步到服务器
_CODE_VERSION = "2026-06-11-11:45-wifi-connect-debug"
import logging.handlers

import traceback
import grid_utils
from starlink_detector import StarLinkDetector, _MAX_ANALYZE_PER_BSSID
from config_loader import get_redis_config, get_capture_config, get_wifi_connect_config
import wifi_mode_utils

# S9: 星链设备识别器（模块级单例，跨扫描轮次累积识别结果）
_starlink_detector = StarLinkDetector()

# 设备类型识别：全局 AP/Client/SSID 缓存（程序生命周期内累积，重启后自然清空）
_global_ap_macs = set()
_global_ap_beacon_macs = set()
_global_client_macs = set()
_global_ap_ssids = {}
# AP 宣告信道缓存：{bssid(小写): 信道号(int)}，来自 Beacon DS Parameter Set IE（ID=3）
_global_ap_channels = {}
_global_ap_channel_sources = {}
# AP 工作带宽缓存：{bssid(小写): 带宽字符串}，来自 Beacon HT/VHT Operation IE
# 可能的值：'HT20' / 'HT40+' / 'HT40-' / 'VHT80'；解析失败时无此 key
_global_ap_bandwidths = {}
_full_scan_relationships_by_scan = {}
_full_scan_inferred_ap_configs_by_scan = {}
CLIENT_IDENTITY_MGMT_SUBTYPES = {0, 2, 4}


def _decode_ssid(raw_bytes):
    """解码 Beacon SSID；明确隐藏的 SSID 使用前端保留值。"""
    if raw_bytes is None:
        return '_wildcard_'
    if isinstance(raw_bytes, str):
        if not raw_bytes or all(char == '\x00' for char in raw_bytes):
            return '_wildcard_'
        return raw_bytes
    if not raw_bytes or all(byte == 0 for byte in raw_bytes):
        return '_wildcard_'
    for encoding in ('utf-8', 'gbk'):
        try:
            return raw_bytes.decode(encoding)
        except Exception:
            pass
    return None


def _packet_time_iso(pkt):
    """返回数据包时间的 UTC ISO 字符串。"""
    try:
        return datetime.fromtimestamp(float(pkt.time), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ap_operation_ies(elements):
    """解析 Beacon 操作 IE；DS 信道优先，HT Primary Channel 作为回退。"""
    ds_channel = None
    ht_primary_channel = None
    declared_bandwidth = None
    for element_id, raw_info in elements or []:
        info = bytes(raw_info or b"")
        if element_id == 3 and len(info) >= 1:
            ds_channel = int(info[0])
        elif element_id == 61 and len(info) >= 2:
            ht_primary_channel = int(info[0])
            sta_ch_width = info[1] & 0x01
            sec_ch_offset = (info[1] >> 1) & 0x03
            if sta_ch_width == 0:
                declared_bandwidth = "HT20"
            elif sec_ch_offset == 1:
                declared_bandwidth = "HT40+"
            elif sec_ch_offset == 3:
                declared_bandwidth = "HT40-"
            else:
                declared_bandwidth = "HT20"
        elif element_id == 192 and len(info) >= 1 and info[0] >= 1:
            declared_bandwidth = "VHT80"

    if ds_channel is not None:
        channel = ds_channel
        channel_source = "ds_parameter_set"
    elif ht_primary_channel is not None:
        channel = ht_primary_channel
        channel_source = "ht_operation"
    else:
        channel = None
        channel_source = None
    return {
        "channel": channel,
        "channel_source": channel_source,
        "channel_confidence": "declared" if channel is not None else "unknown",
        "declared_bandwidth": declared_bandwidth,
    }


def _full_scan_should_replace_observation(existing, candidate):
    """按置信度、最大 RSSI、样本数、任务配置顺序选择正式观测。"""
    if not isinstance(existing, dict):
        return True
    if not isinstance(candidate, dict):
        return False
    confidence_rank = {"declared": 3, "confirmed": 3, "inferred": 2, "uncertain": 1}
    existing_rank = confidence_rank.get(existing.get("channel_confidence"), 0)
    candidate_rank = confidence_rank.get(candidate.get("channel_confidence"), 0)
    is_client = existing.get("type") == "client" or candidate.get("type") == "client"
    if not is_client and candidate_rank != existing_rank:
        return candidate_rank > existing_rank

    existing_rssi = existing.get("rssi_avg")
    candidate_rssi = candidate.get("rssi_avg")
    if candidate_rssi != existing_rssi:
        if candidate_rssi is None:
            return False
        if existing_rssi is None:
            return True
        return float(candidate_rssi) > float(existing_rssi)

    existing_samples = int(existing.get("rssi_samples") or 0)
    candidate_samples = int(candidate.get("rssi_samples") or 0)
    if candidate_samples != existing_samples:
        return candidate_samples > existing_samples

    existing_order = int(existing.get("config_order_index") or 0)
    candidate_order = int(candidate.get("config_order_index") or 0)
    return candidate_order < existing_order


def _full_scan_capture_observation(entry, channel, bandwidth):
    """Build one actual receiver-configuration observation for whitelist replay."""
    return {
        "channel": int(channel),
        "bandwidth": str(bandwidth or "HT20"),
        "rssi_avg": entry.get("rssi_avg"),
        "rssi_samples": int(entry.get("rssi_samples") or 0),
        "omni_rssi_avg": entry.get("omni_rssi_avg"),
        "omni_rssi_samples": int(entry.get("omni_rssi_samples") or 0),
        "first_seen_at": entry.get("first_seen_at"),
        "last_seen_at": entry.get("last_seen_at"),
        "config_order_index": int(entry.get("config_order_index") or 0),
    }


def _merge_full_scan_capture_observations(existing, candidate):
    """Merge actual capture configurations without mixing their RSSI evidence."""
    merged = {}
    for parent in (existing, candidate):
        if not isinstance(parent, dict):
            continue
        observations = parent.get("observed_configs")
        if not isinstance(observations, list):
            capture_config = parent.get("capture_config") or {}
            capture_channel = capture_config.get("channel", parent.get("channel"))
            capture_bandwidth = capture_config.get(
                "bandwidth",
                parent.get("bandwidth", "HT20"),
            )
            observations = (
                [_full_scan_capture_observation(parent, capture_channel, capture_bandwidth)]
                if capture_channel is not None
                else []
            )
        for raw in observations:
            if not isinstance(raw, dict) or raw.get("channel") is None:
                continue
            item = dict(raw)
            key = (int(item["channel"]), str(item.get("bandwidth") or "HT20"))
            previous = merged.get(key)
            if previous is None:
                merged[key] = item
                continue
            previous["first_seen_at"] = (
                min(
                    value
                    for value in (
                        previous.get("first_seen_at"),
                        item.get("first_seen_at"),
                    )
                    if value
                )
                if previous.get("first_seen_at") or item.get("first_seen_at")
                else None
            )
            previous["last_seen_at"] = (
                max(
                    value
                    for value in (
                        previous.get("last_seen_at"),
                        item.get("last_seen_at"),
                    )
                    if value
                )
                if previous.get("last_seen_at") or item.get("last_seen_at")
                else None
            )
            previous["rssi_samples"] = (
                int(previous.get("rssi_samples") or 0)
                + int(item.get("rssi_samples") or 0)
            )
            previous_rssi = previous.get("rssi_avg")
            item_rssi = item.get("rssi_avg")
            if item_rssi is not None and (
                previous_rssi is None or float(item_rssi) > float(previous_rssi)
            ):
                previous["rssi_avg"] = item_rssi
            previous_omni = previous.get("omni_rssi_avg")
            item_omni = item.get("omni_rssi_avg")
            if item_omni is not None and (
                previous_omni is None or float(item_omni) > float(previous_omni)
            ):
                previous["omni_rssi_avg"] = item_omni
            previous["omni_rssi_samples"] = (
                int(previous.get("omni_rssi_samples") or 0)
                + int(item.get("omni_rssi_samples") or 0)
            )
            previous["config_order_index"] = min(
                int(previous.get("config_order_index") or 0),
                int(item.get("config_order_index") or 0),
            )
    return list(merged.values())


def _is_unicast_mac(mac):
    try:
        normalized = str(mac or "").lower().strip()
        if normalized in ("", "ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
            return False
        first_octet = int(normalized.split(":", 1)[0], 16)
        return (first_octet & 0x01) == 0
    except (TypeError, ValueError):
        return False


def _parse_client_ap_data_relationship(frame_type, fcfield, addr1, addr2):
    """仅基础设施模式 Data 帧可建立 Client–AP 关系。"""
    try:
        frame_type = int(frame_type)
        fc = int(fcfield)
    except (TypeError, ValueError):
        return None
    if frame_type != 2:
        return None
    to_ds = bool(fc & 0x01)
    from_ds = bool(fc & 0x02)
    if to_ds and not from_ds:
        client_mac, ap_bssid, direction = addr2, addr1, "uplink"
    elif from_ds and not to_ds:
        client_mac, ap_bssid, direction = addr1, addr2, "downlink"
    else:
        return None
    client_mac = str(client_mac or "").lower().strip()
    ap_bssid = str(ap_bssid or "").lower().strip()
    if (
        not _is_unicast_mac(client_mac)
        or not _is_unicast_mac(ap_bssid)
        or client_mac == ap_bssid
    ):
        return None
    return {
        "client_mac": client_mac,
        "ap_bssid": ap_bssid,
        "direction": direction,
    }


def _remember_full_scan_data_relationship(pkt, scan_id, channel, bandwidth):
    if not scan_id or RadioTap not in pkt or Dot11 not in pkt:
        return None
    dot11 = pkt[Dot11]
    relationship = _parse_client_ap_data_relationship(
        dot11.type,
        int(dot11.FCfield),
        dot11.addr1,
        dot11.addr2,
    )
    if not relationship:
        return None

    client_mac = relationship["client_mac"]
    ap_bssid = relationship["ap_bssid"]
    packet_ts = _packet_time_iso(pkt)
    _global_client_macs.add(client_mac)
    _global_ap_macs.add(ap_bssid)

    scan_key = str(scan_id)
    scan_relationships = _full_scan_relationships_by_scan.setdefault(scan_key, {})
    client_relationships = scan_relationships.setdefault(client_mac, {})
    observed = client_relationships.setdefault(ap_bssid, {
        "bssid": ap_bssid,
        "first_seen_at": packet_ts,
        "last_seen_at": packet_ts,
        "frame_count": 0,
        "uplink_frames": 0,
        "downlink_frames": 0,
        "capture_config": {
            "channel": int(channel),
            "bandwidth": str(bandwidth),
        },
    })
    observed["last_seen_at"] = packet_ts
    observed["frame_count"] = int(observed.get("frame_count") or 0) + 1
    direction_key = f"{relationship['direction']}_frames"
    observed[direction_key] = int(observed.get(direction_key) or 0) + 1
    observed["capture_config"] = {
        "channel": int(channel),
        "bandwidth": str(bandwidth),
    }

    while len(_full_scan_relationships_by_scan) > 8:
        oldest_key = next(iter(_full_scan_relationships_by_scan))
        if oldest_key == scan_key and len(_full_scan_relationships_by_scan) == 1:
            break
        _full_scan_relationships_by_scan.pop(oldest_key, None)
        _full_scan_inferred_ap_configs_by_scan.pop(oldest_key, None)
    _persist_full_scan_relationship_snapshot(scan_key)
    return relationship


def _remember_full_scan_inferred_ap_config(scan_id, ap_bssid, candidate):
    if not scan_id or not ap_bssid or not isinstance(candidate, dict):
        return
    scan_key = str(scan_id)
    ap_key = str(ap_bssid).lower()
    scan_configs = _full_scan_inferred_ap_configs_by_scan.setdefault(scan_key, {})
    existing = scan_configs.get(ap_key)
    if _full_scan_should_replace_observation(existing, candidate):
        scan_configs[ap_key] = {
            key: candidate.get(key)
            for key in (
                "channel",
                "bandwidth",
                "rssi_avg",
                "rssi_samples",
                "config_order_index",
                "channel_source",
                "channel_confidence",
                "declared_bandwidth",
                "observed_best_config",
            )
        }
        _persist_full_scan_relationship_snapshot(scan_key)
    while len(_full_scan_inferred_ap_configs_by_scan) > 8:
        oldest_key = next(iter(_full_scan_inferred_ap_configs_by_scan))
        if oldest_key == scan_key and len(_full_scan_inferred_ap_configs_by_scan) == 1:
            break
        _full_scan_inferred_ap_configs_by_scan.pop(oldest_key, None)


def _apply_full_scan_client_relationship(entry, mac, scan_id):
    mac_lower = str(mac or "").lower().strip()
    if not isinstance(entry, dict) or entry.get("type") != "client":
        return
    relationships = (
        _full_scan_relationships_by_scan.get(str(scan_id), {}).get(mac_lower, {})
        if scan_id else {}
    )
    if not relationships:
        entry["relationship_status"] = "uncertain"
        entry["current_observed_ap"] = None
        entry["observed_aps"] = []
        entry["channel_confidence"] = "uncertain"
        entry["channel_source"] = "observed_best_config"
        entry["observed_best_config"] = {
            "channel": entry.get("channel"),
            "bandwidth": entry.get("bandwidth"),
        }
        return

    observed_aps = []
    for ap_bssid, observed in relationships.items():
        item = dict(observed)
        item["beacon_seen"] = ap_bssid in _global_ap_beacon_macs
        if _global_ap_channels.get(ap_bssid) is not None:
            item["channel"] = _global_ap_channels[ap_bssid]
            item["channel_source"] = (
                _global_ap_channel_sources.get(ap_bssid) or "declared"
            )
        elif (
            _full_scan_inferred_ap_configs_by_scan
            .get(str(scan_id), {})
            .get(ap_bssid)
        ):
            inferred_config = (
                _full_scan_inferred_ap_configs_by_scan[str(scan_id)][ap_bssid]
            )
            item["channel"] = inferred_config.get("channel")
            item["channel_source"] = "beacon_capture_config"
            item["ap_inferred_config"] = dict(
                inferred_config.get("observed_best_config") or {}
            )
        else:
            item["channel"] = (item.get("capture_config") or {}).get("channel")
            item["channel_source"] = "data_capture_config"
        item["declared_bandwidth"] = _global_ap_bandwidths.get(ap_bssid)
        observed_aps.append(item)
    observed_aps.sort(
        key=lambda item: (str(item.get("last_seen_at") or ""), item.get("bssid") or ""),
        reverse=True,
    )
    current = observed_aps[0]
    confirmed = bool(current.get("beacon_seen"))
    entry["relationship_status"] = "confirmed" if confirmed else "inferred"
    entry["current_observed_ap"] = current
    entry["observed_aps"] = observed_aps
    entry["channel"] = current.get("channel")
    entry["bandwidth"] = "HT20"
    entry["channel_confidence"] = "confirmed" if confirmed else "inferred"
    entry["channel_source"] = (
        current.get("channel_source")
        if confirmed
        else "data_capture_config"
    )
    entry["observed_best_config"] = dict(current.get("capture_config") or {})


def _build_full_scan_relationship_snapshot(scan_id):
    snapshot = {}
    scan_relationships = _full_scan_relationships_by_scan.get(str(scan_id), {})
    for client_mac in scan_relationships:
        entry = {"type": "client", "channel": None, "bandwidth": "HT20"}
        _apply_full_scan_client_relationship(entry, client_mac, scan_id)
        snapshot[client_mac] = {
            key: entry.get(key)
            for key in (
                "relationship_status",
                "current_observed_ap",
                "observed_aps",
                "observed_best_config",
                "channel",
                "bandwidth",
                "channel_confidence",
                "channel_source",
            )
        }
    return snapshot


def _persist_full_scan_relationship_snapshot(scan_id):
    redis_client = globals().get("r")
    if redis_client is None or not scan_id:
        return
    try:
        redis_client.setex(
            f"full_scan:{scan_id}:relationships",
            86400,
            json.dumps(
                _build_full_scan_relationship_snapshot(scan_id),
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass


def _extract_beacon_info(pkt):
    """从 Beacon 帧提取 BSSID、SSID、主信道与宣告带宽。非 Beacon 返回 None。"""
    from scapy.all import Dot11Elt
    if RadioTap not in pkt or Dot11 not in pkt:
        return None
    dot11 = pkt[Dot11]
    if dot11.type != 0 or dot11.subtype != 8:
        return None
    bssid = dot11.addr3 or dot11.addr2
    if not bssid:
        return None
    bssid = bssid.lower().strip()
    if bssid in ('ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00'):
        return None

    ssid = None
    ssid_ie_seen = False
    operation_elements = []
    try:
        for elt in pkt[Dot11Elt:]:
            if not hasattr(elt, 'ID'):
                break
            if elt.ID == 0 and not ssid_ie_seen:
                ssid_ie_seen = True
                ssid = _decode_ssid(elt.info)
            elif elt.ID in (3, 61, 192):
                operation_elements.append((int(elt.ID), bytes(elt.info or b"")))
    except Exception:
        pass

    operation = _parse_ap_operation_ies(operation_elements)
    return {'bssid': bssid, 'ssid': ssid, **operation}


def _remember_ap_beacon(pkt, analyze_starlink=True):
    """
    记录 Beacon 中的 AP 身份信息。
    返回主信道来源与宣告带宽；非 Beacon 返回 None。
    """
    info = _extract_beacon_info(pkt)
    if not info:
        return None

    bssid = info['bssid']
    ssid = info['ssid']
    _global_ap_macs.add(bssid)
    _global_ap_beacon_macs.add(bssid)
    if bssid not in _global_ap_ssids or (not _global_ap_ssids[bssid] and ssid):
        _global_ap_ssids[bssid] = ssid

    # 记录 AP 宣告信道（DS Parameter Set IE）
    if info.get('channel') is not None:
        try:
            _global_ap_channels[bssid] = int(info['channel'])
            _global_ap_channel_sources[bssid] = info.get('channel_source')
        except (ValueError, TypeError):
            pass

    # 记录 AP 工作带宽（HT/VHT Operation IE），解析失败时不写入（保留旧值或留空）
    if info.get('declared_bandwidth') is not None:
        _global_ap_bandwidths[bssid] = info['declared_bandwidth']

    if analyze_starlink:
        try:
            _starlink_detector.analyze_beacon(
                pkt,
                bssid,
                ssid,
                info.get('channel') if info.get('channel') is not None else '?',
            )
        except Exception:
            pass
    return info


def _analyze_beacon_for_starlink(pkt):
    """
    S9 共享 Beacon 分析入口，供所有扫描模式复用。
    从 Beacon 帧提取 bssid/ssid/channel，记录 AP SSID 并调用识别器。
    同一 BSSID 只解析一次（识别器内部有缓存），可在 sniff 回调中高频调用。
    注意：只在定向天线的 handler 里调用，避免双线程并发写 cache。
    """
    _remember_ap_beacon(pkt, analyze_starlink=True)


def _remember_packet_identity(
    pkt,
    analyze_starlink=True,
    full_scan_id=None,
    capture_channel=None,
    capture_bandwidth="HT20",
):
    """从 802.11 帧中累积 AP/Client 身份信息。"""
    if RadioTap not in pkt or Dot11 not in pkt:
        return

    dot11 = pkt[Dot11]
    if full_scan_id and capture_channel is not None and dot11.type == 2:
        _remember_full_scan_data_relationship(
            pkt,
            full_scan_id,
            capture_channel,
            capture_bandwidth,
        )
    if dot11.type == 0 and dot11.subtype == 8:
        _remember_ap_beacon(pkt, analyze_starlink=analyze_starlink)
        return

    ta_mac = dot11.addr2
    if not ta_mac:
        return
    ta_mac = ta_mac.lower().strip()
    if ta_mac in ('ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00'):
        return

    if dot11.type == 0 and dot11.subtype in CLIENT_IDENTITY_MGMT_SUBTYPES:
        _global_client_macs.add(ta_mac)
    elif dot11.type == 2:
        fc = int(dot11.FCfield)
        if (fc & 0x01) and not (fc & 0x02):
            _global_client_macs.add(ta_mac)


def _get_ap_ssid(mac: str):
    mac = (mac or '').lower().strip()
    if mac in _global_ap_ssids:
        return _global_ap_ssids[mac]
    starlink_info = _starlink_detector._cache.get(mac, {})
    if 'ssid' in starlink_info:
        return starlink_info.get('ssid')
    return None


def _apply_device_identity(entry: dict, mac: str, starlink_bssids=None, default_type=None):
    """
    统一输出设备身份字段：
    - type: ap / client / None
    - subtype: starlink / None
    - ssid: AP SSID；明确隐藏时为 _wildcard_；非 AP 或未知时为 None
    """
    mac_lower = (mac or '').lower().strip()
    starlink_bssids = starlink_bssids if starlink_bssids is not None else _get_starlink_bssids()

    if mac_lower in starlink_bssids or mac_lower in _global_ap_macs:
        entry['type'] = 'ap'
        entry['subtype'] = 'starlink' if mac_lower in starlink_bssids else None
        entry['ssid'] = _get_ap_ssid(mac_lower)
        if _global_ap_bandwidths.get(mac_lower) is not None:
            entry['declared_bandwidth'] = _global_ap_bandwidths[mac_lower]
        if _global_ap_channels.get(mac_lower) is not None:
            entry['channel'] = _global_ap_channels[mac_lower]
            entry['channel_source'] = (
                _global_ap_channel_sources.get(mac_lower) or "declared"
            )
            entry['channel_confidence'] = "declared"
    elif mac_lower in _global_client_macs:
        entry['type'] = 'client'
        entry['subtype'] = None
        entry['ssid'] = None
    else:
        entry['type'] = default_type
        entry['subtype'] = None
        entry['ssid'] = None


_STARLINK_WHITELIST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'starlink_whitelist.json'
)


def _load_starlink_whitelist(r):
    """
    S9: 程序启动时从白名单文件预加载星链设备。
    将文件中的记录写入 detector cache 和 Redis，跨会话保持识别结果。
    """
    if not os.path.exists(_STARLINK_WHITELIST_PATH):
        return
    try:
        with open(_STARLINK_WHITELIST_PATH, 'r', encoding='utf-8') as f:
            whitelist = json.load(f)
        if not whitelist:
            return
        for bssid, info in whitelist.items():
            # 预填 detector cache，跳过实际 Beacon 解析（直接标记为已确认星链）
            _starlink_detector._cache[bssid] = {
                'is_starlink':   True,
                'analyze_count': _MAX_ANALYZE_PER_BSSID,  # 标记为已完成，不再重复分析
                'features':      info.get('features', []),
                'ssid':          info.get('ssid', ''),
                'channel':       info.get('channel', '?'),
            }
        # 同步写入 Redis（TTL=24h）
        r.set('starlink:detected_bssids', json.dumps(whitelist), ex=86400)
        logging.getLogger('capture_worker').info(
            f"[S9] 从白名单预加载 {len(whitelist)} 个星链设备"
        )
    except Exception as e:
        logging.getLogger('capture_worker').warning(f"[S9] 白名单加载失败: {e}")


def _flush_starlink_to_redis(r):
    """
    S9: 将本轮检测到的星链设备写入 Redis（TTL=24h）并更新持久化白名单文件。
    在每个扫描命令结束时调用，保证结果可被查询接口读取。
    """
    try:
        starlink_found = _starlink_detector.get_all_starlink()
        if not starlink_found:
            return
        # 写 Redis
        r.set('starlink:detected_bssids', json.dumps(starlink_found), ex=86400)
        # 更新白名单文件（合并已有记录，只增不减）
        existing = {}
        if os.path.exists(_STARLINK_WHITELIST_PATH):
            try:
                with open(_STARLINK_WHITELIST_PATH, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                pass
        merged = {**existing, **starlink_found}  # 新结果覆盖旧记录（同 BSSID 更新信息）
        with open(_STARLINK_WHITELIST_PATH, 'w', encoding='utf-8') as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _get_starlink_bssids() -> set:
    """S9: 返回当前已确认为星链的 BSSID 集合（小写），供打标使用。"""
    return {
        bssid for bssid, info in _starlink_detector._cache.items()
        if info.get('is_starlink')
    }


# from grid_utils import update_grid_data_atomic
import math
import threading
# 🔥 修改：从grid_utils导入全局变量，而不是重复定义
from grid_utils import (
    GLOBAL_PAN_RANGE, GLOBAL_TILT_RANGE,
    GLOBAL_PAN_MIN, GLOBAL_PAN_MAX,
    GLOBAL_TILT_MIN, GLOBAL_TILT_MAX
)

# 在全局变量部分添加
worker_state = {
    'last_grid_key': None,
    'packet_count': 0
}

# ============= 多点扫描模式控制（新增）=============
# 用于控制packet_processor是否处理数据
# "normal" = 正常抓包模式
# "paused" = 暂停处理（多点扫描移动期间）
capture_mode = "normal"

# 定时更新相关
update_thread = None
update_thread_running = False

# --- 日志系统配置 ---
def setup_logging():
    """设置 Capture Worker 日志，仅输出到控制台，不写文件。"""
    logger = logging.getLogger("capture_worker")
    logger.setLevel(logging.DEBUG)

    # 清除已有的处理器（避免重复）
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        '%(asctime)s,%(msecs)03d [%(levelname)s] [capture_worker] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    console_handler.setFormatter(formatter)

    logger.addHandler(console_handler)

    return logger




# 初始化日志系统
logger = setup_logging()
logger.info(f"🚀 capture_worker 启动，代码版本: {_CODE_VERSION}")

# --- Redis, 网卡, 存储配置 (保持不变) ---
# 从 config.json 加载配置，支持环境变量覆盖
_redis_host, _redis_port = get_redis_config()
REDIS_HOST = _redis_host
REDIS_PORT = _redis_port
CAPTURE_COMMAND_QUEUE = 'capture:command_queue'
#CAPTURE_DATA_STREAM = 'capture:data_stream'
CAPTURE_RSSI_LIST = 'capture:rssi_data_list'  # 新增：RSSI数据列表

CAPTURE_RUNNING_KEY = 'capture:running'
CAPTURE_STATUS_KEY = 'capture:status'
PTZ_STATUS_KEY = 'ptz:current_status'
CAPTURE_LAST_CONFIG_KEY = 'capture:last_config'
CAPTURE_MAX_RECORDS = int(os.getenv('CAPTURE_MAX_RECORDS', '100000'))  # 默认信号输入为10万条
CAPTURE_TRIM_BATCH = int(os.getenv('CAPTURE_TRIM_BATCH', '10000'))  # 每次删除1万条
CAPTURE_TRIM_THRESHOLD = int(os.getenv('CAPTURE_TRIM_THRESHOLD', '5000'))  # 超过10万条+5000条时开始删除
# 从 config.json 加载网卡配置，支持环境变量覆盖
_directional_interface, _omni_interface = get_capture_config()
CAPTURE_DEFAULT_INTERFACE = _directional_interface
# S7: 全向天线网卡配置（为空则不启用全向采集）
CAPTURE_OMNI_INTERFACE = _omni_interface
OMNI_ENABLED = bool(CAPTURE_OMNI_INTERFACE)
PCAP_STORAGE_DIR = '/mnt/data'
# 在全局变量部分添加
# --- 全局变量 ---
capture_thread = None
last_trim_time = 0  # 上次删除时间
stop_sniffing_event = None
current_monitor_interface = None
current_omni_monitor_interface = None  # S7: 全向网卡 monitor 状态跟踪
target_mac_filter = None
target_mac_match_mode = "ta"
saved_capture_config = None
current_capture_meta = {}
last_capture_status_write = 0
shutdown_requested = False
r = None
full_scan_config_session = None
full_scan_refinement_session = None
# 当前信道和带宽状态（按网卡分别缓存，避免定向/全向互相误判）
current_channel_by_interface = {}
current_bandwidth_by_interface = {}


def _iw_channel_width_params(bandwidth):
    """Normalize API/Beacon bandwidth labels to iw set channel arguments."""
    if bandwidth is None:
        return []
    bw = str(bandwidth).strip().upper().replace("_", "")
    aliases = {
        "NOHT": ["NOHT"],
        "20": ["HT20"],
        "20MHZ": ["HT20"],
        "HT20": ["HT20"],
        "40+": ["HT40+"],
        "40PLUS": ["HT40+"],
        "40MHZ+": ["HT40+"],
        "HT40+": ["HT40+"],
        "40-": ["HT40-"],
        "40MINUS": ["HT40-"],
        "40MHZ-": ["HT40-"],
        "HT40-": ["HT40-"],
        "80": ["80MHz"],
        "80M": ["80MHz"],
        "80MHZ": ["80MHz"],
        "HT80": ["80MHz"],
        "VHT80": ["80MHz"],
    }
    return aliases.get(bw, [])


def _update_capture_at_best_status(active, phase, extra=None):
    """更新指定点位抓包状态。只写 capture:status，不再写 ptz:current_status。
    /api/v1/ptz/status 会合并 capture:status，前端通过那个接口读取。"""
    try:
        now = time.time()
        payload = {
            'active': bool(active),
            'phase': str(phase),
            'updated_at': now,
            'source': 'capture_at_best',
        }
        if not active:
            payload['stopped_at'] = now
        if extra:
            payload.update(extra)
        r.set(CAPTURE_STATUS_KEY, json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"更新指定点位抓包状态失败: {e}")




# ==================== 数据分发配置 ====================
def trim_redis_list_if_needed():
    """智能删除Redis List数据，避免频繁删除"""
    global last_trim_time
    try:
        current_length = r.llen(CAPTURE_RSSI_LIST)
        current_time = time.time()
        
        # 只有当数据量超过阈值且距离上次删除超过10秒时才删除
        if (current_length > CAPTURE_MAX_RECORDS + CAPTURE_TRIM_THRESHOLD and 
            current_time - last_trim_time > 10):
            
            # 批量删除1万条最旧的数据
            deleted_count = CAPTURE_TRIM_BATCH  # 使用配置的批量删除数量
            r.ltrim(CAPTURE_RSSI_LIST, 0, current_length - CAPTURE_TRIM_BATCH - 1)
            last_trim_time = current_time
            
            logger.info(f"Redis List 超过限制，已批量删除 {deleted_count} 条，剩余 {current_length - deleted_count} 条")
            return True
        return False
    except Exception as e:
        logger.error(f"删除Redis List数据失败: {e}")
        return False
# ==================== 网格计算前置函数====================
# ==================== 智能扫描配置 ====================

# 智能扫描Redis键名
INTELLIGENT_SCAN_ACTIVE_KEY = "intelligent_scan:active"      # 扫描激活标志
INTELLIGENT_SCAN_SIGNALS_KEY = "intelligent_scan:signals"    # 信号队列       # 停止标志

def push_signal_to_intelligent_scan(pan, tilt, rssi):
    """
    【核心设计】纯粹的数据推送器，无状态，避免竞态条件
    只负责检查标志位并推送数据，不维护任何本地状态
    """
    try:
        # 1. 检查智能扫描是否激活（原子操作）
        is_active = r.get(INTELLIGENT_SCAN_ACTIVE_KEY)
        if is_active != "1":
            return  # 不活跃，直接返回
        
        # 2. 构造信号数据
        signal_data = {
            'pan': float(pan),
            'tilt': float(tilt), 
            'rssi': float(rssi),
            'timestamp': time.time()
        }
        
        # 3. 原子性推送到Redis队列（右侧推入）
        r.rpush(INTELLIGENT_SCAN_SIGNALS_KEY, json.dumps(signal_data))
        
         # 4. 智能队列管理（保留最好的信号）
        queue_length = r.llen(INTELLIGENT_SCAN_SIGNALS_KEY)
        if queue_length > 100:  # 最大队列长度
            try:
                # 读取所有信号，按质量排序，保留好的
                signals_raw = r.lrange(INTELLIGENT_SCAN_SIGNALS_KEY, 0, -1)
                signals = [json.loads(s) for s in signals_raw]
                
                # 按RSSI排序，保留最好的20个
                signals.sort(key=lambda x: x['rssi'], reverse=True)
                best_signals = signals[:20]
                
                # 清空队列，重新推入好信号
                r.delete(INTELLIGENT_SCAN_SIGNALS_KEY)
                for signal in best_signals:
                    r.rpush(INTELLIGENT_SCAN_SIGNALS_KEY, json.dumps(signal))
                    
                logger.debug(f"智能队列管理：保留了最好的20个信号，丢弃了{queue_length-50}个较差信号")
                
            except Exception as e:
                # 如果智能管理失败，回退到原来的时间管理方式
                r.ltrim(INTELLIGENT_SCAN_SIGNALS_KEY, -50, -1)
                logger.warning(f"智能队列管理失败，回退到时间管理: {e}")
        
    except Exception as e:
        logger.debug(f"推送智能扫描信号失败: {e}")

# ==================== 智能扫描配置 ====================
# capture_worker.py 添加
def update_grid_average(r, grid_key, trigger_reason="unknown"):
    """简单直接的均值更新"""
    try:
        grid_data = r.hmget(grid_key, 'count', 'rssi_sum')
        if not grid_data[0]:
            return False
            
        count = int(grid_data[0])
        rssi_sum = float(grid_data[1])
        
        if count > 0:
            avg = rssi_sum / count
            current_time = time.time()
            
            r.hset(grid_key, mapping={
                'rssi_avg': f"{avg:.2f}",
                'last_update_time': str(current_time)
            })
            
            logger.info(f"📊 更新均值: {grid_key} = {avg:.2f}dBm ({count}个包), 原因:{trigger_reason}")
            return True
        return False
    except Exception as e:
        logger.error(f"更新均值失败: {grid_key}, 错误: {e}")
        return False

def update_all_p_data_averages(r):
    """
    更新所有P数据的RSSI均值
    """
    try:
        # 获取所有P数据键
        p_keys = r.keys("p:*")
        updated_count = 0
        
        for p_key in p_keys:
            try:
                # 更新单个P数据的均值
                if update_grid_average(r, p_key, "timer"):
                    updated_count += 1
            except Exception as e:
                logger.warning(f"更新P数据 {p_key} 失败: {e}")
                continue
        
        if updated_count > 0:
            logger.info(f"定时更新完成: 更新了 {updated_count} 个P数据的均值")
            
    except Exception as e:
        logger.error(f"定时更新所有P数据失败: {e}")

def update_grid_data_and_get_key(r, pan, tilt, rssi):
    """
    【推荐版】一站式处理，并在最后返回计算出的 p_key。
    """
    try:
        # 1. 从 Redis 获取当前系统定义的源步径
        steph, stepv = grid_utils.get_current_step_from_redis(r)
        if steph is None or stepv is None:
            logger.warning("无法获取当前步径，跳过网格处理")
            return None

        # 2. 计算新数据所属的网格信息，如果不存在则创建它
        p_key, p_data = grid_utils.create_p_data_if_needed(r, pan, tilt, steph, stepv)
        if not p_key:
            return None # 创建失败，日志已在内部记录
        # 3. 执行重叠检测与清理
        grid_utils.remove_overlapping_p_data(r, pan, tilt, steph, stepv)  
        
        # 4. 【关键】使用原子操作更新该网格的统计数据
        pipe = r.pipeline()
        pipe.hincrby(p_key, "count", 1)
        pipe.hincrbyfloat(p_key, "rssi_sum", float(rssi))
        pipe.hset(p_key, "last_update_time", time.time())
        pipe.execute()

        
        
        # 5. 返回 p_key
        return p_key

    except Exception as e:
        logger.error(f"❌ 更新网格数据并获取key失败: {e}")
        return None

def timer_update_worker():
    """
    定时更新线程：每10秒更新一次所有P数据的均值
    """
    global update_thread_running
    
    logger.info("定时更新线程启动")
    
    while update_thread_running:
        try:
            # 等待10秒
            time.sleep(10)
            
            if update_thread_running and r:
                update_current_avg(r)
                if current_capture_meta.get('active'):
                    _write_capture_status(True, phase='capturing', force=True)
                
        except Exception as e:
            logger.error(f"定时更新线程出错: {e}")
    
    logger.info("定时更新线程结束")

def update_current_avg(r):
    # #   从redis获取当前状态，如果当前装饰IDLE， 根据当前角度计算当前的格子，根据当前P对应格子的值更新avg
    # # 🔥 新增：检查全局变量是否已初始化
    # if GLOBAL_PAN_MIN is None or GLOBAL_PAN_MAX is None or GLOBAL_TILT_MIN is None or GLOBAL_TILT_MAX is None:
    #     logger.info("全局变量尚未初始化，跳过定时更新")
    #     return
    # ptz_raw = r.get(PTZ_STATUS_KEY)
    # if not ptz_raw:
    #     return
    # ptz_status = json.loads(ptz_raw)
    # if ptz_status['state'] != "IDLE":
    #     return
    # pan = ptz_status['position']['pan']
    # tilt = ptz_status['position']['tilt']
    # if pan is None or tilt is None:
    #     return
    # steph, stepv = grid_utils.get_current_step_from_redis(r)
    # if steph is None or stepv is None:
    #     return

    # result = grid_utils._grid_index_from_angles(pan, tilt, steph, stepv)
    # if not result:            
    #     return None, None
    # p_index, pan_steps, tilt_steps, pan_idx, tilt_idx = result

    #     # 计算网格边界（使用现有的逻辑）
    # left_pan = GLOBAL_PAN_MIN + pan_idx * steph
    # right_pan = min(left_pan + steph, GLOBAL_PAN_MAX)
    # bottom_tilt = GLOBAL_TILT_MIN + tilt_idx * stepv
    # top_tilt = min(bottom_tilt + stepv, GLOBAL_TILT_MAX)
    # logger.info(f"+++++++当前网格key: {GLOBAL_TILT_MAX}")
    # # 生成P键名
    # p_key = f"p:{p_index}_{steph:.1f}_{stepv:.1f}_{left_pan:.1f}_{right_pan:.1f}_{bottom_tilt:.1f}_{top_tilt:.1f}"
    # update_grid_average(r, p_key, "timeouts")
    try:
        return grid_utils.update_current_position_grid_average(r)

    except Exception as e:
        logger.info(f"调用grid_utils更新函数失败: {e}")
        return False


def start_timer_update():
    """
    启动定时更新线程
    """
    global update_thread, update_thread_running
    
    if update_thread is None or not update_thread.is_alive():
        update_thread_running = True
        update_thread = threading.Thread(target=timer_update_worker, daemon=True)
        update_thread.start()
        logger.info("定时更新线程已启动")

def stop_timer_update():
    """
    停止定时更新线程
    """
    global update_thread_running
    
    update_thread_running = False
    if update_thread and update_thread.is_alive():
        update_thread.join(timeout=2)
    logger.info("定时更新线程已停止")
# ==================== 网格计算前置函数====================
# ==================== 网格计算====================
def process_grid_logic_for_packet(r, pan, tilt, rssi, current_state):
    """
    【高效最终版】处理单个数据包的网格逻辑
    """
    global worker_state
    # 🔥 新增：确保worker_state已初始化
    if 'packet_count' not in worker_state:
        worker_state['packet_count'] = 0
    if 'last_grid_key' not in worker_state:
        worker_state['last_grid_key'] = None
    
    try:
        # 1. 一次性完成所有核心操作，并获取 p_key，绝不重复计算！
        current_grid_key = update_grid_data_and_get_key(r, pan, tilt, rssi)
        
        if not current_grid_key:
            return  # 如果失败，直接退出
        # 2. 检查是否进入新区域
        if (current_grid_key != worker_state.get('last_grid_key')):
            # 进入新区域，更新上一个网格的均值
            if worker_state.get('last_grid_key'):
                update_grid_average(r, worker_state['last_grid_key'], "region_change")
            
            # 更新状态
            worker_state['last_grid_key'] = current_grid_key
            worker_state['packet_count'] = 1 # 新区域，计数器重置为1
        else:
            # 还在同一个区域，计数器增加
            worker_state['packet_count'] = worker_state.get('packet_count', 0) + 1
        # 3. 每10个包更新一次均值
            if worker_state['packet_count'] % 10 == 0 and current_state == "IDLE":
                update_grid_average(r, current_grid_key, "count_trigger")
        
        logger.debug(f"数据处理完成: pan={pan:.1f}, tilt={tilt:.1f}, rssi={rssi:.1f}")
        
    except Exception as e:
        logger.error(f"网格数据处理失败: {e}", exc_info=True)
# ==================== 网格计算====================
# Scapy 使用的 pcap writer
pcap_writer = None


class RotatingPcapWriter:
    """Small wrapper around PcapWriter that rotates files by size."""

    def __init__(self, base_path, split_size_mb=None):
        self.base_path = base_path
        self.max_bytes = int(float(split_size_mb) * 1024 * 1024) if split_size_mb else None
        self.part_index = 1
        self.writer = None
        self.current_path = None
        self.files = []
        self._open_next_available()

    def _part_path(self, part_index):
        if not self.max_bytes:
            return self.base_path
        directory = os.path.dirname(self.base_path)
        stem, ext = os.path.splitext(os.path.basename(self.base_path))
        ext = ext or ".pcap"
        if stem.startswith("part_") and stem[5:].isdigit():
            filename = f"part_{part_index:03d}{ext}"
        else:
            filename = f"{stem}_part_{part_index:03d}{ext}"
        return os.path.join(directory, filename)

    def _open_next_available(self):
        while True:
            candidate = self._part_path(self.part_index)
            if not self.max_bytes:
                break
            if not os.path.exists(candidate) or os.path.getsize(candidate) < self.max_bytes:
                break
            self.part_index += 1
        self.current_path = self._part_path(self.part_index)
        os.makedirs(os.path.dirname(self.current_path), exist_ok=True)
        self.writer = PcapWriter(self.current_path, append=True, sync=True)
        if self.current_path not in self.files:
            self.files.append(self.current_path)

    def _rotate_if_needed(self):
        if not self.max_bytes or not self.current_path:
            return
        try:
            if os.path.exists(self.current_path) and os.path.getsize(self.current_path) >= self.max_bytes:
                self.writer.close()
                self.part_index += 1
                self._open_next_available()
        except Exception:
            pass

    def write(self, packet):
        self._rotate_if_needed()
        self.writer.write(packet)

    def close(self):
        if self.writer:
            self.writer.close()
            self.writer = None


def _available_memory_mb():
    """Return available memory in MB on Linux; None when unavailable."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def _free_disk_mb(path):
    try:
        target = path if os.path.isdir(path) else os.path.dirname(path) or "."
        os.makedirs(target, exist_ok=True)
        return shutil.disk_usage(target).free / (1024.0 * 1024.0)
    except Exception:
        return None


def _resource_stop_reason(storage_path, min_free_memory_mb=None, min_free_disk_mb=None):
    if min_free_memory_mb:
        available = _available_memory_mb()
        if available is not None and available < float(min_free_memory_mb):
            return "low_memory"
    if min_free_disk_mb:
        free_disk = _free_disk_mb(storage_path)
        if free_disk is not None and free_disk < float(min_free_disk_mb):
            return "low_disk_space"
    return None


def _current_pcap_files():
    if pcap_writer is None:
        return []
    return list(getattr(pcap_writer, "files", []))


def _normalize_dot11_mac(mac):
    if not mac:
        return None
    return str(mac).lower().strip()


def _dot11_addr_set(dot11):
    addrs = set()
    for attr in ("addr1", "addr2", "addr3", "addr4"):
        mac = _normalize_dot11_mac(getattr(dot11, attr, None))
        if mac:
            addrs.add(mac)
    return addrs


def _packet_matches_target(dot11):
    if not target_mac_filter:
        return True
    if target_mac_match_mode == "any_addr":
        return target_mac_filter in _dot11_addr_set(dot11)
    return _normalize_dot11_mac(getattr(dot11, "addr2", None)) == target_mac_filter

# 数据包统计计数器
packet_stats = {
    'total_packets': 0,
    'filtered_packets': 0,
    'processed_packets': 0,
    'pcap_written_packets': 0,
    'parse_errors': 0,
    'redis_errors': 0
}


def _new_packet_stats():
    return {
        'total_packets': 0,
        'filtered_packets': 0,
        'processed_packets': 0,
        'pcap_written_packets': 0,
        'parse_errors': 0,
        'redis_errors': 0
    }


def _packet_stats_snapshot():
    return {key: int(packet_stats.get(key, 0) or 0) for key in _new_packet_stats()}


def _write_capture_status(active, phase='capturing', extra=None, force=False):
    global last_capture_status_write
    if r is None:
        return
    now = time.time()
    if not force and now - last_capture_status_write < 1.0:
        return
    try:
        payload = dict(current_capture_meta) if isinstance(current_capture_meta, dict) else {}
        payload.update({
            'active': bool(active),
            'running': bool(active),
            'phase': str(phase),
            'updated_at': now,
            'pcap_files': _current_pcap_files(),
        })
        packet_counts = _packet_stats_snapshot()
        payload['packet_counts'] = packet_counts
        payload['packet_count'] = packet_counts.get('pcap_written_packets', 0)
        payload['pcap_written_packets'] = packet_counts.get('pcap_written_packets', 0)
        started_at = payload.get('started_at')
        if started_at is not None:
            payload['elapsed_seconds'] = max(0.0, now - float(started_at))
        deadline_at = payload.get('deadline_at')
        if deadline_at is not None and active:
            payload['remaining_seconds'] = max(0.0, float(deadline_at) - now)
        elif deadline_at is not None:
            payload['remaining_seconds'] = 0.0
        if not active:
            payload.setdefault('finished_at', now)
        if extra:
            payload.update(extra)
        r.set(CAPTURE_STATUS_KEY, json.dumps(payload, ensure_ascii=False))
        last_capture_status_write = now
    except Exception as e:
        logger.debug(f"更新抓包状态失败: {e}")


def _finish_capture_status(reason='stopped', phase='idle', extra=None):
    if isinstance(current_capture_meta, dict):
        current_capture_meta['active'] = False
        current_capture_meta['running'] = False
        current_capture_meta['phase'] = phase
        current_capture_meta['finished_at'] = time.time()
        current_capture_meta['reason'] = reason
    final_extra = {'reason': reason}
    if extra:
        final_extra.update(extra)
    _write_capture_status(False, phase=phase, extra=final_extra, force=True)

def signal_handler(signum, frame):
    global shutdown_requested
    logger.info(f"收到信号 {signum}，准备关闭...")
    shutdown_requested = True
    if stop_sniffing_event:
        stop_sniffing_event.set()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def _interface_is_monitor_mode(interface_name):
    """读取网卡真实模式，避免 worker 重启后仅因内存标记丢失而重复初始化。"""
    try:
        result = subprocess.run(
            ["sudo", "iw", "dev", interface_name, "info"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return (
            result.returncode == 0
            and "type monitor" in (result.stdout or "").lower()
        )
    except Exception as exc:
        logger.debug(
            f"读取网卡监听模式失败，将走完整初始化: "
            f"interface={interface_name}, error={exc}"
        )
        return False


def setup_monitor_mode_once(interface_name, channel, bandwidth):
    """优先复用网卡真实监听状态；必要时才执行完整监听模式初始化。"""
    global current_monitor_interface

    in_monitor_mode = (
        current_monitor_interface == interface_name
        or _interface_is_monitor_mode(interface_name)
    )
    if in_monitor_mode:
        current_monitor_interface = interface_name
        logger.info("网卡已在监听模式，只调整信道和带宽", extra={
            "interface": interface_name,
            "channel": channel,
            "bandwidth": bandwidth
        })
        if adjust_channel_and_bandwidth(interface_name, channel, bandwidth):
            return True
        logger.warning(
            f"复用监听模式切换信道失败，将重新初始化: {interface_name}"
        )

    # 第一次设置监听模式 - 调用原有的setup_monitor_mode函数
    logger.info("首次设置网卡到监听模式", extra={
        "interface": interface_name,
        "channel": channel,
        "bandwidth": bandwidth
    })
    return setup_monitor_mode(interface_name, channel, bandwidth)


def adjust_channel_and_bandwidth(interface_name, channel, bandwidth):
    """只调整信道和带宽，不改变网卡模式"""
    try:
        # 设置频道和带宽 (不再 down/up 网卡，避免唤醒系统的网络守护进程)
        iw_params = _iw_channel_width_params(bandwidth)
        subprocess.run(
            ['sudo', 'iw', 'dev', interface_name, 'set', 'channel', str(channel)] + iw_params,
            check=True,
            capture_output=True,
            timeout=5,
        )

        # ⚡ 优化：iw set channel returncode=0 即代表成功，无需再跑一次 iw dev info 验证。
        # 原验证步骤每次约消耗 0.3~0.5s，定向+全向各一次 → 每信道浪费 ~0.6s，共26信道额外约 16s。
        logger.debug(f"✅ [{interface_name}] 信道切换完成: ch{channel} {bandwidth}")
        return True
    except subprocess.CalledProcessError as e:
        stderr_output = e.stderr.decode('utf-8', errors='ignore').strip() if e.stderr else ''
        logger.error(f"调整信道和带宽失败 [returncode={e.returncode}] stderr: {stderr_output}", extra={
            "interface": interface_name,
            "channel": channel,
            "bandwidth": bandwidth,
        })
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"调整信道和带宽超时: {interface_name} ch{channel} {bandwidth}")
        return False
    except Exception as e:
        logger.error(f"调整信道和带宽失败 [{type(e).__name__}]: {e}", extra={
            "interface": interface_name,
        })
        return False


def _interface_is_monitor(interface_name):
    """Return True only when iw reports the interface is currently in monitor mode."""
    try:
        result = subprocess.run(
            ['sudo', 'iw', 'dev', interface_name, 'info'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and 'type monitor' in result.stdout.lower()
    except Exception:
        return False


def _clear_interface_channel_cache(interface_name):
    current_channel_by_interface.pop(interface_name, None)
    current_bandwidth_by_interface.pop(interface_name, None)



# ==================== S7: 全向天线并行采集 ====================

def setup_omni_monitor_once(channel, bandwidth):
    """S7: 全向网卡 Monitor 模式管理，逻辑与定向网卡一致"""
    global current_omni_monitor_interface
    
    if not OMNI_ENABLED:
        return False
    
    if current_omni_monitor_interface == CAPTURE_OMNI_INTERFACE:
        # 已在 monitor 模式，只切信道
        logger.info("🔄 [全向] 网卡已在监听模式，只调整信道和带宽", extra={
            "interface": CAPTURE_OMNI_INTERFACE,
            "channel": channel,
            "bandwidth": bandwidth
        })
        return adjust_channel_and_bandwidth(CAPTURE_OMNI_INTERFACE, channel, bandwidth)
    
    # 首次设置 monitor 模式
    logger.info("📡 [全向] 首次设置网卡到监听模式", extra={
        "interface": CAPTURE_OMNI_INTERFACE,
        "channel": channel,
        "bandwidth": bandwidth
    })
    result = setup_monitor_mode(CAPTURE_OMNI_INTERFACE, channel, bandwidth)
    if result:
        current_omni_monitor_interface = CAPTURE_OMNI_INTERFACE
    return result


def adjust_omni_channel(channel, bandwidth):
    """S7: 全向网卡信道切换，失败不影响定向天线"""
    if not OMNI_ENABLED:
        return False
    try:
        return adjust_channel_with_retry(CAPTURE_OMNI_INTERFACE, channel, bandwidth, logger=logger)
    except Exception as e:
        logger.warning(f"⚠️ [全向] 信道切换失败，不影响定向采集: {e}")
        return False


def adjust_channels_parallel(channel, bandwidth, logger=None):
    """
    ⚡ 并行切换双网卡信道（定向 + 全向同时发起 iw set channel）

    串行方式：dir_time + omni_time ≈ 1s + 1s = 2s/信道
    并行方式：max(dir_time, omni_time) ≈ 1s/信道  → 节省 ~50%

    Returns:
        (dir_ok: bool, omni_ok: bool)
    """
    dir_result = [False]
    omni_result = [False]

    def _do_dir():
        dir_result[0] = adjust_channel_with_retry(
            CAPTURE_DEFAULT_INTERFACE, channel, bandwidth, logger=logger
        )

    def _do_omni():
        if not OMNI_ENABLED:
            return
        try:
            omni_result[0] = adjust_channel_with_retry(
                CAPTURE_OMNI_INTERFACE, channel, bandwidth, logger=logger
            )
        except Exception as e:
            _log = logger or logging.getLogger('capture_worker')
            _log.warning(f"⚠️ [全向] 并行信道切换失败，不影响定向采集: {e}")

    t_dir = threading.Thread(target=_do_dir, daemon=True)
    t_omni = threading.Thread(target=_do_omni, daemon=True)
    t_dir.start()
    t_omni.start()
    t_dir.join()
    t_omni.join()

    return dir_result[0], omni_result[0]


def _stop_requested(stop_check_fn):
    if stop_check_fn is None:
        return False
    try:
        return bool(stop_check_fn())
    except Exception as e:
        logger.warning(f"⚠️ 停止检查异常，按未停止处理: {e}")
        return False


def _full_scan_capture_stopped(r, cmd_scan_id=None, stop_key=None, legacy_stop_key=None):
    """统一的全面扫描 capture 级取消判断。

    检查顺序（任一命中即为已取消）：
    1. capture:stop  — 全局抓取停止
    2. legacy_stop_key (multi_scan:stop_full_area_scan) — 旧格式停止标志
    3. stop_key (full_scan:stop) — 新 JSON 格式，含 scan_id 匹配
    4. active_scan_id 与 cmd_scan_id 不一致或已消失

    Returns:
        (stopped: bool, reason: str | None)
    """
    # 1. 全局抓取停止
    if r.get('capture:stop'):
        return True, 'capture_stop'

    # 2. 旧格式停止标志
    _legacy = legacy_stop_key or 'multi_scan:stop_full_area_scan'
    if r.get(_legacy):
        return True, 'manual_stop'

    # 3. 新 JSON 格式停止（需 scan_id 匹配）
    _stop = stop_key or 'full_scan:stop'
    raw = r.get(_stop)
    if raw:
        try:
            data = json.loads(raw)
            if not cmd_scan_id or data.get('scan_id') == cmd_scan_id:
                return True, data.get('reason') or 'manual_stop'
        except Exception:
            return True, 'manual_stop'

    # 4. active_scan_id 消失或不匹配 → 扫描已结束
    if cmd_scan_id:
        try:
            active = r.get('full_scan:active_scan_id')
            if active is None:
                return True, 'scan_finished'
            if active != cmd_scan_id:
                return True, 'scan_id_mismatch'
        except Exception:
            pass

    return False, None


def _full_scan_config_session_stream_key(session_id):
    safe_session_id = "".join(
        ch if ch.isalnum() or ch in ("_", "-") else "_"
        for ch in str(session_id or "unknown")
    )
    return f"full_scan:config_session:{safe_session_id}:events"


def _full_scan_summarize_packets(
    packets_dir,
    packets_omni,
    channel,
    bandwidth,
    omni_ok,
    config_order_index=0,
    scan_id=None,
):
    """Summarize one full-scan config-session sniff slice into MAC RSSI entries."""
    first_seen_by_mac = {}
    last_seen_by_mac = {}
    rssi_by_mac = {}
    rssi_max_by_mac = {}
    beacon_rssi_by_bssid = {}
    beacon_first_seen = {}
    beacon_last_seen = {}

    for pkt in packets_dir or []:
        if pkt.haslayer(RadioTap) and pkt.haslayer(Dot11):
            _remember_packet_identity(
                pkt,
                analyze_starlink=True,
                full_scan_id=scan_id,
                capture_channel=channel,
                capture_bandwidth=bandwidth,
            )
            dot11 = pkt[Dot11]
            ta_mac = dot11.addr2
            if ta_mac and ta_mac.lower() != 'ff:ff:ff:ff:ff:ff':
                rssi = getattr(pkt[RadioTap], 'dBm_AntSignal', None)
                if rssi is not None:
                    packet_ts = _packet_time_iso(pkt)
                    first_seen_by_mac.setdefault(ta_mac, packet_ts)
                    last_seen_by_mac[ta_mac] = packet_ts
                    rssi_by_mac.setdefault(ta_mac, []).append(rssi)
                    if ta_mac not in rssi_max_by_mac or rssi > rssi_max_by_mac[ta_mac]:
                        rssi_max_by_mac[ta_mac] = rssi
                    if dot11.type == 0 and dot11.subtype == 8:
                        bssid = (dot11.addr3 or dot11.addr2 or "").lower()
                        if bssid:
                            beacon_rssi_by_bssid.setdefault(bssid, []).append(rssi)
                            beacon_first_seen.setdefault(bssid, packet_ts)
                            beacon_last_seen[bssid] = packet_ts

    omni_rssi_by_mac = {}
    if OMNI_ENABLED and omni_ok:
        for pkt in packets_omni or []:
            if pkt.haslayer(RadioTap) and pkt.haslayer(Dot11):
                ta_mac = pkt[Dot11].addr2
                if ta_mac and ta_mac.lower() != 'ff:ff:ff:ff:ff:ff':
                    rssi = getattr(pkt[RadioTap], 'dBm_AntSignal', None)
                    if rssi is not None:
                        omni_rssi_by_mac.setdefault(ta_mac, []).append(rssi)

    macs = {}
    for mac, rssi_list in rssi_by_mac.items():
        bssid_lower = mac.lower()
        is_ap_with_declared_ch = (
            bssid_lower in _global_ap_macs and
            _global_ap_channels.get(bssid_lower) is not None
        )
        is_ap_without_declared_ch = (
            bssid_lower in _global_ap_macs and not is_ap_with_declared_ch
        )
        if is_ap_with_declared_ch:
            declared_ch = _global_ap_channels[bssid_lower]
            if int(channel) != declared_ch:
                continue
            final_channel = declared_ch
            final_bandwidth = bandwidth
        else:
            final_channel = channel
            final_bandwidth = bandwidth

        if is_ap_without_declared_ch:
            beacon_samples = beacon_rssi_by_bssid.get(bssid_lower) or []
            if not beacon_samples:
                continue
            rssi_list = beacon_samples
            representative_rssi = max(beacon_samples)
            first_seen = beacon_first_seen.get(bssid_lower)
            last_seen = beacon_last_seen.get(bssid_lower)
        else:
            representative_rssi = rssi_max_by_mac.get(mac) or round(sum(rssi_list) / len(rssi_list), 2)
            first_seen = first_seen_by_mac.get(mac)
            last_seen = last_seen_by_mac.get(mac)
        mac_entry = {
            'channel': final_channel,
            'bandwidth': final_bandwidth,
            'rssi_avg': representative_rssi,
            'rssi_samples': len(rssi_list),
            'first_seen_at': first_seen,
            'last_seen_at': last_seen,
            'config_order_index': int(config_order_index or 0),
        }
        if is_ap_with_declared_ch:
            mac_entry['channel_source'] = (
                _global_ap_channel_sources.get(bssid_lower) or 'declared'
            )
            mac_entry['channel_confidence'] = 'declared'
            mac_entry['declared_bandwidth'] = _global_ap_bandwidths.get(bssid_lower)
        elif is_ap_without_declared_ch:
            mac_entry['channel_source'] = 'beacon_capture_config'
            mac_entry['channel_confidence'] = 'inferred'
            mac_entry['declared_bandwidth'] = _global_ap_bandwidths.get(bssid_lower)
            mac_entry['observed_best_config'] = {
                'channel': int(channel),
                'bandwidth': str(bandwidth),
            }
            _remember_full_scan_inferred_ap_config(
                scan_id,
                bssid_lower,
                mac_entry,
            )
        if OMNI_ENABLED and omni_ok and mac in omni_rssi_by_mac and omni_rssi_by_mac[mac]:
            omni_samples = omni_rssi_by_mac[mac]
            mac_entry['omni_rssi_avg'] = round(sum(omni_samples) / len(omni_samples), 2)
            mac_entry['omni_rssi_samples'] = len(omni_samples)
        else:
            mac_entry['omni_rssi_avg'] = None
            mac_entry['omni_rssi_samples'] = 0
        mac_entry['capture_config'] = {
            'channel': int(channel),
            'bandwidth': str(bandwidth),
        }
        mac_entry['observed_configs'] = [
            _full_scan_capture_observation(
                mac_entry,
                channel,
                bandwidth,
            )
        ]
        _apply_device_identity(mac_entry, mac, _get_starlink_bssids())
        _apply_full_scan_client_relationship(mac_entry, mac, scan_id)
        macs[mac] = mac_entry
    return macs


def _full_scan_config_session_loop(session):
    r_obj = session.get('redis')
    stream_key = session.get('stream_key')
    scan_id = session.get('scan_id')
    session_id = session.get('session_id')
    channel = session.get('channel')
    bandwidth = session.get('bandwidth')
    config_order_index = int(session.get('config_order_index') or 0)
    stop_event = session.get('stop_event')
    omni_ok = bool(session.get('omni_ok'))
    slice_seconds = max(0.2, float(session.get('slice_seconds') or 0.5))

    def _session_stopped():
        if stop_event and stop_event.is_set():
            return True
        stopped, _reason = _full_scan_capture_stopped(
            r_obj,
            scan_id,
            stop_key=session.get('stop_key'),
            legacy_stop_key=session.get('legacy_stop_key'),
        )
        return stopped

    logger.info(
        f"▶️ [full_scan_config_session] start session={session_id} "
        f"scan_id={scan_id} ch={channel} bw={bandwidth}"
    )
    try:
        while not _session_stopped():
            t_dir_result = [None]
            t_omni_result = [None]

            def sniff_dir():
                t_dir_result[0] = _sniff_collect_with_stop(
                    iface=CAPTURE_DEFAULT_INTERFACE,
                    dwell_time=slice_seconds,
                    stop_check_fn=_session_stopped,
                    poll_interval=0.2,
                )

            def sniff_omni():
                if not (OMNI_ENABLED and omni_ok):
                    return
                try:
                    t_omni_result[0] = _sniff_collect_with_stop(
                        iface=CAPTURE_OMNI_INTERFACE,
                        dwell_time=slice_seconds,
                        stop_check_fn=_session_stopped,
                        poll_interval=0.2,
                    )
                except OSError as exc:
                    logger.warning(f"⚠️ [full_scan_config_session] 全向 sniff 异常: {exc}")

            t_dir = threading.Thread(target=sniff_dir, daemon=True)
            t_omni = threading.Thread(target=sniff_omni, daemon=True) if OMNI_ENABLED and omni_ok else None
            t_dir.start()
            if t_omni:
                t_omni.start()
            t_dir.join()
            if t_omni:
                t_omni.join()

            macs = _full_scan_summarize_packets(
                t_dir_result[0] or [],
                t_omni_result[0] or [],
                channel,
                bandwidth,
                omni_ok,
                config_order_index=config_order_index,
                scan_id=scan_id,
            )
            if macs:
                try:
                    r_obj.xadd(
                        stream_key,
                        {
                            'event': 'samples',
                            'scan_id': scan_id or '',
                            'session_id': session_id or '',
                            'channel': str(channel),
                            'bandwidth': str(bandwidth),
                            'ts': str(time.time()),
                            'macs': json.dumps(macs, ensure_ascii=False),
                        },
                        maxlen=10000,
                        approximate=True,
                    )
                except Exception as exc:
                    logger.warning(f"⚠️ [full_scan_config_session] 写入 stream 失败: {exc}")
    finally:
        try:
            stopped, reason = _full_scan_capture_stopped(
                r_obj,
                scan_id,
                stop_key=session.get('stop_key'),
                legacy_stop_key=session.get('legacy_stop_key'),
            )
            r_obj.xadd(
                stream_key,
                {
                    'event': 'session_stopped',
                    'scan_id': scan_id or '',
                    'session_id': session_id or '',
                    'reason': reason if stopped else (session.get('stop_reason') or 'completed'),
                    'ts': str(time.time()),
                },
                maxlen=10000,
                approximate=True,
            )
        except Exception:
            pass
        logger.info(f"⏹️ [full_scan_config_session] stopped session={session_id}")


def _stop_full_scan_config_session(reason='stopped', timeout=3.0):
    global full_scan_config_session
    session = full_scan_config_session
    if not session:
        return False
    session['stop_reason'] = reason
    stop_event = session.get('stop_event')
    if stop_event:
        stop_event.set()
    thread = session.get('thread')
    if thread and thread.is_alive():
        thread.join(timeout=max(0.1, float(timeout or 0)))
    if thread and thread.is_alive():
        logger.warning(
            f"⚠️ [full_scan_config_session] stop timeout session={session.get('session_id')}"
        )
        return False
    try:
        if r is not None:
            active = r.get('full_scan:config_session:active')
            if active:
                data = json.loads(active)
                if data.get('session_id') == session.get('session_id'):
                    r.delete('full_scan:config_session:active')
    except Exception:
        pass
    full_scan_config_session = None
    return True


def _full_scan_refinement_packet_handler(session, packet):
    if RadioTap not in packet or Dot11 not in packet:
        return
    ta_mac = packet[Dot11].addr2
    if not ta_mac:
        return
    mac = ta_mac.lower().strip()
    if mac not in session.get("target_macs", set()):
        return
    rssi = getattr(packet[RadioTap], "dBm_AntSignal", None)
    if rssi is None:
        return
    callback_time = time.time()
    try:
        packet_time = float(packet.time)
        if not math.isfinite(packet_time):
            packet_time = callback_time
    except (TypeError, ValueError, AttributeError):
        packet_time = callback_time
    with session["lock"]:
        segment_id = session.get("segment_id")
        if not segment_id:
            return
        segment_stats = session["segment_stats"].setdefault(segment_id, {})
        stats = segment_stats.setdefault(mac, {
            "rssi_avg": float(rssi),
            "rssi_samples": 0,
            "peak_packet_time": packet_time,
            "peak_callback_time": callback_time,
        })
        stats["rssi_samples"] += 1
        if float(rssi) > float(stats["rssi_avg"]):
            stats["rssi_avg"] = float(rssi)
            stats["peak_packet_time"] = packet_time
            stats["peak_callback_time"] = callback_time


def _full_scan_refinement_loop(session):
    while not session["stop_event"].is_set():
        try:
            _sniff_with_stop_poll(
                iface=CAPTURE_DEFAULT_INTERFACE,
                prn=lambda packet: _full_scan_refinement_packet_handler(
                    session,
                    packet,
                ),
                store=False,
                total_timeout=0.25,
                stop_check_fn=session["stop_event"].is_set,
                poll_interval=0.1,
            )
        except OSError as exc:
            logger.warning(f"⚠️ [白名单复核] sniff 异常: {exc}")
            if session["stop_event"].wait(0.1):
                break


def _stop_full_scan_refinement_session(timeout=1.0):
    global full_scan_refinement_session
    session = full_scan_refinement_session
    if not session:
        return True
    session["stop_event"].set()
    thread = session.get("thread")
    if thread and thread.is_alive():
        thread.join(timeout=max(0.0, float(timeout or 0)))
    if thread and thread.is_alive():
        return False
    full_scan_refinement_session = None
    return True


def _start_full_scan_refinement_session(command):
    global full_scan_refinement_session
    scan_id = command.get("scan_id")
    notify_key = command.get("notify_key")
    active_scan_id = r.get("full_scan:active_scan_id")
    if not scan_id or active_scan_id != scan_id:
        return {
            "status": "skipped",
            "reason": "scan_id_mismatch",
            "active_scan_id": active_scan_id,
        }
    _stop_full_scan_refinement_session(timeout=0.5)
    try:
        channel = int(command.get("channel"))
    except (TypeError, ValueError):
        return {"status": "error", "reason": "invalid_channel"}
    bandwidth = str(command.get("bandwidth") or "HT20")
    if not setup_monitor_mode_once(
        CAPTURE_DEFAULT_INTERFACE,
        channel,
        bandwidth,
    ):
        return {"status": "error", "reason": "setup_monitor_failed"}
    dir_ok, _omni_ok = adjust_channels_parallel(
        channel,
        bandwidth,
        logger=logger,
    )
    if not dir_ok:
        return {"status": "error", "reason": "channel_switch_failed"}
    session = {
        "scan_id": scan_id,
        "session_id": command.get("session_id"),
        "channel": channel,
        "bandwidth": bandwidth,
        "target_macs": {
            str(mac).lower().strip()
            for mac in command.get("target_macs") or []
        },
        "segment_id": None,
        "segment_stats": {},
        "lock": threading.Lock(),
        "stop_event": threading.Event(),
    }
    thread = threading.Thread(
        target=_full_scan_refinement_loop,
        args=(session,),
        daemon=True,
    )
    session["thread"] = thread
    full_scan_refinement_session = session
    thread.start()
    return {
        "status": "started",
        "scan_id": scan_id,
        "session_id": session.get("session_id"),
        "channel": channel,
        "bandwidth": bandwidth,
        "notify_key": notify_key,
    }


def _start_full_scan_config_session(command):
    global full_scan_config_session
    cmd_scan_id = command.get('scan_id')
    session_id = command.get('session_id') or f"session_{int(time.time() * 1000)}"
    notify_key = command.get('notify_key')
    stream_key = command.get('stream_key') or _full_scan_config_session_stream_key(session_id)
    channel = command.get('channel')
    bandwidth = command.get('bandwidth') or 'HT20'
    config_order_index = int(command.get('config_order_index') or 0)
    stop_key = command.get('stop_key') or 'full_scan:stop'
    legacy_stop_key = command.get('legacy_stop_key') or 'multi_scan:stop_full_area_scan'

    def _notify(payload):
        if notify_key:
            try:
                r.lpush(notify_key, json.dumps(payload, ensure_ascii=False))
            except Exception:
                pass

    try:
        active_scan_id = r.get('full_scan:active_scan_id')
    except Exception:
        active_scan_id = None
    if cmd_scan_id and active_scan_id != cmd_scan_id:
        logger.warning(
            f"⏭️ [full_scan_config_session] start ignored: "
            f"cmd_scan_id={cmd_scan_id} active={active_scan_id}"
        )
        _notify({'status': 'skipped', 'reason': 'scan_id_mismatch', 'scan_id': cmd_scan_id})
        return

    stopped, reason = _full_scan_capture_stopped(
        r, cmd_scan_id, stop_key=stop_key, legacy_stop_key=legacy_stop_key
    )
    if stopped:
        _notify({'status': 'stopped', 'reason': reason or 'manual_stop', 'scan_id': cmd_scan_id})
        return

    if channel is None:
        _notify({'status': 'error', 'message': 'channel is required', 'scan_id': cmd_scan_id})
        return

    # 二级计时：config_session_start 子步骤
    _t_session_start = time.time()
    _t_sub_start = time.time()

    _stop_full_scan_config_session(reason='replaced', timeout=2.0)
    _t_stop_session = time.time() - _t_sub_start

    _t_sub_start = time.time()
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        _notify({'status': 'error', 'message': 'invalid channel', 'scan_id': cmd_scan_id})
        return
    _t_parse_channel = time.time() - _t_sub_start

    _t_sub_start = time.time()
    if not setup_monitor_mode_once(CAPTURE_DEFAULT_INTERFACE, channel, bandwidth):
        _notify({'status': 'error', 'message': 'monitor mode setup failed', 'scan_id': cmd_scan_id})
        return
    _t_setup_monitor = time.time() - _t_sub_start

    _t_sub_start = time.time()
    stopped, reason = _full_scan_capture_stopped(
        r, cmd_scan_id, stop_key=stop_key, legacy_stop_key=legacy_stop_key
    )
    if stopped:
        _notify({'status': 'stopped', 'reason': reason or 'manual_stop', 'scan_id': cmd_scan_id})
        return
    _t_check_stop_1 = time.time() - _t_sub_start

    _t_sub_start = time.time()
    dir_ok, omni_ok = adjust_channels_parallel(channel, bandwidth, logger=logger)
    if not dir_ok:
        _notify({'status': 'error', 'message': 'channel switch failed', 'scan_id': cmd_scan_id})
        return
    _t_adjust_channels = time.time() - _t_sub_start

    _t_session_total = time.time() - _t_session_start
    logger.info(
        f"⏱️ [config_session_start] 子计时: "
        f"stop_session={_t_stop_session*1000:.1f}ms, "
        f"parse_channel={_t_parse_channel*1000:.1f}ms, "
        f"setup_monitor={_t_setup_monitor*1000:.1f}ms, "
        f"check_stop={_t_check_stop_1*1000:.1f}ms, "
        f"adjust_channels={_t_adjust_channels*1000:.1f}ms, "
        f"total={_t_session_total*1000:.1f}ms"
    )

    try:
        r.delete(stream_key)
    except Exception:
        pass

    stop_event = threading.Event()
    session = {
        'redis': r,
        'scan_id': cmd_scan_id,
        'session_id': session_id,
        'stream_key': stream_key,
        'channel': channel,
        'bandwidth': bandwidth,
        'config_order_index': config_order_index,
        'stop_key': stop_key,
        'legacy_stop_key': legacy_stop_key,
        'stop_event': stop_event,
        'omni_ok': bool(omni_ok),
        'slice_seconds': command.get('slice_seconds') or 0.5,
        'started_at': time.time(),
    }
    thread = threading.Thread(target=_full_scan_config_session_loop, args=(session,), daemon=True)
    session['thread'] = thread
    full_scan_config_session = session
    try:
        r.set('full_scan:config_session:active', json.dumps({
            'scan_id': cmd_scan_id,
            'session_id': session_id,
            'stream_key': stream_key,
            'channel': channel,
            'bandwidth': bandwidth,
            'config_order_index': config_order_index,
            'started_at': session['started_at'],
        }, ensure_ascii=False), ex=3600)
    except Exception:
        pass
    thread.start()
    _notify({
        'status': 'started',
        'scan_id': cmd_scan_id,
        'session_id': session_id,
        'stream_key': stream_key,
        'channel': channel,
        'bandwidth': bandwidth,
        'config_order_index': config_order_index,
        'omni_ok': bool(omni_ok),
    })


def _location_scan_capture_stopped(r, cmd_scan_id=None, stop_key=None):
    """统一的定位扫描 capture 级取消判断。

    检查顺序：
    1. capture:stop
    2. stop_key (location_scan:stop) — JSON 格式含 scan_id 匹配
    3. active_scan_id 消失或不匹配

    Returns:
        (stopped: bool, reason: str | None)
    """
    if r.get('capture:stop'):
        return True, 'capture_stop'

    _stop = stop_key or 'location_scan:stop'
    raw = r.get(_stop)
    if raw:
        try:
            data = json.loads(raw)
            if not cmd_scan_id or data.get('scan_id') == cmd_scan_id:
                return True, data.get('reason') or 'manual_stop'
        except Exception:
            # 旧格式 "1" 等
            return True, 'manual_stop'

    if cmd_scan_id:
        try:
            active = r.get('location_scan:active_scan_id')
            if active is None:
                return True, 'scan_finished'
            if active != cmd_scan_id and not (isinstance(active, str) and active.startswith('stopping:')):
                return True, 'scan_id_mismatch'
        except Exception:
            pass

    return False, None


# ─── WiFi 连接辅助函数 ───────────────────────────────────────────────────────

def _check_wifi_connect_stop(r, connect_id):
    """检查 wifi_connect:stop 校验 connect_id，返回 (stopped, reason)。"""
    raw = r.get('wifi_connect:stop')
    if not raw:
        return False, None
    try:
        data = json.loads(raw)
        if data.get('connect_id') == connect_id:
            return True, data.get('reason') or 'manual_stop'
    except Exception:
        pass
    return False, None


def _write_wifi_connect_status(r, **kwargs):
    """局部 patch wifi_connect:status，读取现有状态后合并更新。"""
    try:
        raw = r.get('wifi_connect:status')
        status = json.loads(raw) if raw else {}
        status.update(kwargs)
        if 'elapsed_seconds' not in kwargs and 'started_at' in status:
            status['elapsed_seconds'] = round(time.time() - status['started_at'], 1)
        r.set('wifi_connect:status', json.dumps(status))
    except Exception as e:
        logger.error(f"[wifi_connect] 写状态失败: {e}")


def _run_wpa_connect(r, interface, ssid, bssid, password, timeout, connect_id):
    """WiFi 连接主流程，包含 stopping_others → switching → connecting → restoring_monitor 全流程。"""
    logger.info(f"[wifi_connect] >>>>>> _run_wpa_connect 函数被调用 <<<<<< connect_id={connect_id}")
    cfg = get_wifi_connect_config()
    poll_interval = cfg['轮询间隔秒数']
    stop_others_timeout = cfg['停止其他任务超时秒数']
    tmp_dir = cfg['临时配置文件目录']
    dhcp_timeout = cfg['DHCP超时秒数']
    log_password = cfg['日志明文密码']

    password_log = password if log_password else '***'
    started_at = time.time()
    connect_result = None  # 连接本身的结果，固定后不再改写
    final_reason = None
    ip_address = None
    wpa_proc = None
    conf_path = os.path.join(tmp_dir, f'wpa_connect_{connect_id}.conf')
    restore_channel = None
    restore_bandwidth = None

    try:
        # ── 检查取消 ───────────────────────────────────────────────────────
        stopped, reason = _check_wifi_connect_stop(r, connect_id)
        if stopped:
            _write_wifi_connect_status(r, state='cancelled', reason=reason or 'manual_stop',
                                       active=False, terminal=True, result='cancelled')
            return

        # ── stopping_others：等待其他任务进入终态 ─────────────────────────────
        _write_wifi_connect_status(r, state='stopping_others', active=True, terminal=False)
        logger.info(f"[wifi_connect] connect_id={connect_id} state=stopping_others ssid={ssid}")

        stop_deadline = time.time() + stop_others_timeout
        while time.time() < stop_deadline:
            stopped, reason = _check_wifi_connect_stop(r, connect_id)
            if stopped:
                _write_wifi_connect_status(r, state='cancelled', reason=reason or 'manual_stop',
                                           active=False, terminal=True, result='cancelled')
                return

            fs_active = r.get('full_scan:active_scan_id')
            ls_active = r.get('location_scan:active_scan_id')
            cap_running = r.get('capture:running')

            if not fs_active and not ls_active and (cap_running is None or cap_running == '0'):
                break

            time.sleep(poll_interval)
        else:
            # 超时
            _write_wifi_connect_status(r, state='error', reason='stop_others_timeout',
                                       active=False, terminal=True, result='error')
            r.delete('wifi_connect:active_connect_id')
            logger.error(f"[wifi_connect] connect_id={connect_id} stopping_others 超时")
            return

        # ── 记录恢复用信道/带宽 ─────────────────────────────────────────────
        restore_channel = current_channel_by_interface.get(interface) or cfg['恢复监听信道']
        restore_bandwidth = current_bandwidth_by_interface.get(interface) or cfg['恢复监听带宽']

        # ── 检查取消 ───────────────────────────────────────────────────────
        stopped, reason = _check_wifi_connect_stop(r, connect_id)
        if stopped:
            _write_wifi_connect_status(r, state='cancelled', reason=reason or 'manual_stop',
                                       active=False, terminal=True, result='cancelled')
            return

        # ── switching_to_managed ────────────────────────────────────────────
        _write_wifi_connect_status(r, state='switching_to_managed')
        logger.info(f"[wifi_connect] connect_id={connect_id} state=switching_to_managed iface={interface}")

        # 停止本地 sniff（兜底）
        try:
            stop_capture(r, interface)
        except Exception:
            pass

        if not wifi_mode_utils.switch_to_managed(interface):
            # 切换失败，尝试恢复 monitor
            wifi_mode_utils.switch_to_monitor(interface)
            try:
                setup_monitor_mode_once(interface, restore_channel, restore_bandwidth)
            except Exception:
                pass
            _write_wifi_connect_status(r, state='error', reason='switch_managed_failed',
                                       active=False, terminal=True, result='error',
                                       connect_result='error')
            r.delete('wifi_connect:active_connect_id')
            logger.error(f"[wifi_connect] connect_id={connect_id} switch_to_managed 失败")
            return

        # ── 检查取消 ───────────────────────────────────────────────────────
        stopped, reason = _check_wifi_connect_stop(r, connect_id)
        if stopped:
            # 此时已切 managed，需要先恢复 monitor
            wifi_mode_utils.switch_to_monitor(interface)
            try:
                setup_monitor_mode_once(interface, restore_channel, restore_bandwidth)
            except Exception:
                pass
            _write_wifi_connect_status(r, state='cancelled', reason=reason or 'manual_stop',
                                       active=False, terminal=True, result='cancelled')
            r.delete('wifi_connect:active_connect_id')
            return

        # ── 写 wpa_supplicant 配置 ──────────────────────────────────────────
        _write_wifi_connect_status(r, state='connecting')
        logger.info(f"[wifi_connect] connect_id={connect_id} state=connecting ssid={ssid} "
                    f"password_present={bool(password)} password_length={len(password or '')} timeout={timeout}")

        try:
            conf_content = _build_wpa_conf(ssid, bssid, password)
            with open(conf_path, 'w') as f:
                f.write(conf_content)
            os.chmod(conf_path, 0o600)
        except Exception as e:
            connect_result = 'error'
            final_reason = 'internal_error'
            logger.error(f"[wifi_connect] 写配置文件失败: {e}")
            # 直接跳到恢复
            raise

        # ── 启动 wpa_supplicant ─────────────────────────────────────────────
        try:
            logger.info(f"[wifi_connect] 准备启动 wpa_supplicant: iface={interface} conf={conf_path}")
            wpa_proc = subprocess.Popen(
                ['wpa_supplicant', '-i', interface, '-c', conf_path, '-B'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            wpa_proc.wait(timeout=5)
            if wpa_proc.returncode != 0 and wpa_proc.returncode is not None:
                stderr = wpa_proc.stderr.read().decode(errors='replace') if wpa_proc.stderr else ''
                connect_result = 'error'
                final_reason = 'wpa_supplicant_start_failed'
                logger.error(f"[wifi_connect] wpa_supplicant 启动失败: rc={wpa_proc.returncode} stderr={stderr}")
                raise RuntimeError('wpa_supplicant start failed')
            logger.info(f"[wifi_connect] wpa_supplicant 启动成功（-B 模式）")
        except subprocess.TimeoutExpired:
            # -B 模式下 wait 可能超时，但进程已在后台运行，正常
            logger.info(f"[wifi_connect] wpa_supplicant wait 超时（-B 模式正常），检查进程是否存活...")
            # 检查进程是否真的在运行
            rc_check, out_check, _ = wifi_mode_utils._run_cmd(['pgrep', '-f', f'wpa_supplicant.*{interface}'])
            if rc_check == 0:
                logger.info(f"[wifi_connect] wpa_supplicant 进程存活: pid={out_check.strip()}")
            else:
                logger.warning(f"[wifi_connect] wpa_supplicant 进程未找到！")
        except Exception as e:
            if connect_result is None:
                connect_result = 'error'
                final_reason = 'wpa_supplicant_start_failed'
            logger.error(f"[wifi_connect] wpa_supplicant 启动异常: {e}")
            raise

        # 启动后立即检查 wpa_cli 是否能连接
        rc_wpa_test, out_wpa_test, err_wpa_test = wifi_mode_utils._run_cmd(['wpa_cli', '-i', interface, 'status'])
        logger.info(f"[wifi_connect] wpa_cli 初始状态检查: rc={rc_wpa_test} out={out_wpa_test[:200] if out_wpa_test else ''} err={err_wpa_test[:200] if err_wpa_test else ''}")

        # ── 轮询连接结果 ───────────────────────────────────────────────────
        deadline = time.time() + timeout

        while time.time() < deadline:
            # 检查取消
            stopped, reason = _check_wifi_connect_stop(r, connect_id)
            if stopped and connect_result != 'success':
                connect_result = 'cancelled'
                final_reason = reason or 'manual_stop'
                break

            # 读取 wpa_cli status（主路径 + 错误原因判断）
            try:
                rc, out, err_out = wifi_mode_utils._run_cmd(['wpa_cli', '-i', interface, 'status'])
                logger.debug(f"[wifi_connect] wpa_cli status: rc={rc} out={out[:300] if out else ''} err={err_out[:200] if err_out else ''}")
                if rc == 0:
                    wpa_state = _parse_wpa_state(out)
                    status_dict = _parse_wpa_status_dict(out)

                    # 每次轮询都打印当前 wpa_state，方便排查
                    logger.info(f"[wifi_connect] connect_id={connect_id} wpa_state={wpa_state} "
                                f"elapsed={round(time.time()-started_at,1)}s")

                    if wpa_state == 'COMPLETED':
                        # WPA 关联成功，即为连接成功（不启动 DHCP，避免修改路由）
                        connect_result = 'success'
                        final_reason = None
                        logger.info(f"[wifi_connect] connect_id={connect_id} WPA 关联成功（COMPLETED）")
                        # 更新状态
                        _write_wifi_connect_status(r, connect_result='success', state='connected')
                        # 保持 1 秒确认，然后退出
                        time.sleep(1)
                        break

                    elif wpa_state == '4WAY_HANDSHAKE':
                        # 正在四次握手，记录首次进入时间；长时间停留说明密码错误
                        if 'handshake_start' not in locals():
                            handshake_start = time.time()
                        elif time.time() - handshake_start > 10:
                            connect_result = 'failed'
                            final_reason = 'wrong_password'
                            logger.warning(f"[wifi_connect] connect_id={connect_id} 4WAY_HANDSHAKE 超时，疑似密码错误")
                            break

                    elif wpa_state in ('INACTIVE', 'DISCONNECTED'):
                        # INACTIVE/DISCONNECTED 持续超过 5 秒，可能是网络找不到或认证被拒
                        if 'inactive_start' not in locals():
                            inactive_start = time.time()
                        elif time.time() - inactive_start > 5:
                            connect_result = 'failed'
                            final_reason = 'auth_rejected'
                            logger.warning(f"[wifi_connect] connect_id={connect_id} wpa_state={wpa_state} 持续异常")
                            break

                    elif wpa_state == 'SCANNING':
                        # 持续扫描超过 timeout/2 秒，可能找不到目标网络
                        if 'scanning_start' not in locals():
                            scanning_start = time.time()
                        elif time.time() - scanning_start > timeout / 2:
                            connect_result = 'failed'
                            final_reason = 'network_not_found'
                            logger.warning(f"[wifi_connect] connect_id={connect_id} 持续 SCANNING，未找到目标网络")
                            break

                    else:
                        # ASSOCIATING 等中间状态，重置异常计时器
                        if wpa_state not in ('ASSOCIATING', 'ASSOCIATED', 'FOUR_WAY_HANDSHAKE'):
                            # 未知状态，记录但不立即判定
                            pass
                else:
                    # wpa_cli 失败，打印详细信息
                    logger.warning(f"[wifi_connect] wpa_cli status 失败: rc={rc} out={out[:300] if out else ''} err={err_out[:200] if err_out else ''}")
                    # 检查 wpa_supplicant 进程是否还活着
                    rc_pgrep, out_pgrep, _ = wifi_mode_utils._run_cmd(['pgrep', '-f', f'wpa_supplicant.*{interface}'])
                    if rc_pgrep != 0:
                        logger.error(f"[wifi_connect] wpa_supplicant 进程已退出！")
                    continue
            except Exception as _e:
                logger.warning(f"[wifi_connect] wpa_cli 轮询异常: {_e}")

            # 更新 elapsed
            _write_wifi_connect_status(r, elapsed_seconds=round(time.time() - started_at, 1))
            time.sleep(poll_interval)

        # 循环结束：检查超时
        if connect_result is None:
            connect_result = 'timeout'
            final_reason = 'connect_timeout'

        # ── 停止 wpa_supplicant ─────────────────────────────────────────────
        try:
            wifi_mode_utils.kill_wpa_supplicant(interface)
        except Exception:
            pass

        # ── 清理 dhclient 残留 ──────────────────────────────────────────────
        try:
            # 1. 杀掉 dhclient 进程
            pid_file = f'/tmp/dhclient_{interface}.pid'
            if os.path.exists(pid_file):
                with open(pid_file, 'r') as f:
                    pid = f.read().strip()
                if pid:
                    wifi_mode_utils._run_cmd(['kill', '-9', pid])
                    logger.info(f"[wifi_connect] 已清理 dhclient 进程: pid={pid}")
                os.remove(pid_file)
            # 2. 兜底：按接口名杀 dhclient
            wifi_mode_utils._run_cmd(['pkill', '-9', '-f', f'dhclient.*{interface}'])
            # 3. 删除 WiFi 接口的路由（防止影响以太网）
            wifi_mode_utils._run_cmd(['ip', 'route', 'flush', 'dev', interface])
            logger.info(f"[wifi_connect] 已清理 {interface} 的路由")
        except Exception as _e:
            logger.warning(f"[wifi_connect] 清理 dhclient 异常（忽略）: {_e}")

        # ── 删除临时配置 ───────────────────────────────────────────────────
        try:
            if os.path.exists(conf_path):
                os.remove(conf_path)
        except Exception:
            pass

        # ── 恢复 monitor 模式 ──────────────────────────────────────────────
        _write_wifi_connect_status(r, state='restoring_monitor',
                                   elapsed_seconds=round(time.time() - started_at, 1))
        logger.info(f"[wifi_connect] connect_id={connect_id} state=restoring_monitor "
                    f"connect_result={connect_result} iface={interface}")

        monitor_ok = wifi_mode_utils.switch_to_monitor(interface)
        if monitor_ok:
            try:
                setup_monitor_mode_once(interface, restore_channel, restore_bandwidth)
            except Exception as e:
                logger.warning(f"[wifi_connect] setup_monitor_mode_once 异常（忽略）: {e}")

        # ── 写终态 ─────────────────────────────────────────────────────────
        elapsed = round(time.time() - started_at, 1)
        if not monitor_ok:
            # monitor 恢复失败
            _write_wifi_connect_status(r,
                state='error', reason='monitor_restore_failed',
                result='error', connect_result=connect_result or 'error',
                ip_address=ip_address,
                active=False, terminal=True, elapsed_seconds=elapsed)
            logger.error(f"[wifi_connect] connect_id={connect_id} monitor 恢复失败，"
                         f"connect_result={connect_result}")
        else:
            # 正常终态：成功用 connected，失败用 failed/error/timeout/cancelled
            final_state = 'connected' if connect_result == 'success' else connect_result
            _write_wifi_connect_status(r,
                state=final_state, reason=final_reason,
                result=connect_result, connect_result=connect_result,
                ip_address=ip_address,
                active=False, terminal=True, elapsed_seconds=elapsed)
            logger.info(f"[wifi_connect] connect_id={connect_id} 终态 state={final_state} "
                        f"reason={final_reason} ip={ip_address} elapsed={elapsed}s")

    except Exception as e:
        # 未预期异常，尝试恢复 monitor 后写 error 终态
        logger.error(f"[wifi_connect] connect_id={connect_id} 未预期异常: {e}\n{traceback.format_exc()}")
        try:
            wifi_mode_utils.kill_wpa_supplicant(interface)
        except Exception:
            pass
        try:
            if os.path.exists(conf_path):
                os.remove(conf_path)
        except Exception:
            pass
        if restore_channel:
            try:
                wifi_mode_utils.switch_to_monitor(interface)
                setup_monitor_mode_once(interface, restore_channel, restore_bandwidth)
            except Exception:
                pass
        elapsed = round(time.time() - started_at, 1)
        _write_wifi_connect_status(r,
            state='error', reason=final_reason or 'internal_error',
            result='error', connect_result=connect_result or 'error',
            active=False, terminal=True, elapsed_seconds=elapsed)
    finally:
        r.delete('wifi_connect:active_connect_id')


def _build_wpa_conf(ssid, bssid, password):
    """构建 wpa_supplicant 配置内容，正确转义 SSID 和密码。"""
    # 转义 SSID 中的特殊字符
    escaped_ssid = ssid.replace('\\', '\\\\').replace('"', '\\"')

    if not password:
        # 开放网络
        network_block = f'    key_mgmt=NONE\n'
    else:
        # WPA/WPA2-PSK
        escaped_pw = password.replace('\\', '\\\\').replace('"', '\\"')
        network_block = f'    psk="{escaped_pw}"\n    key_mgmt=WPA-PSK\n'

    bssid_line = f'    bssid={bssid}\n' if bssid else ''

    return (
        'ctrl_interface=/var/run/wpa_supplicant\n'
        'network={\n'
        f'    ssid="{escaped_ssid}"\n'
        f'{bssid_line}'
        f'{network_block}'
        '}\n'
    )


def _parse_wpa_state(status_output):
    """从 wpa_cli status 输出中解析 wpa_state。"""
    for line in status_output.splitlines():
        if line.startswith('wpa_state='):
            return line.split('=', 1)[1].strip()
    return None


def _parse_wpa_status_dict(status_output):
    """从 wpa_cli status 输出解析为 dict（key=value 行）。"""
    result = {}
    for line in status_output.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            result[k.strip()] = v.strip()
    return result


def _run_dhcp(interface, cfg):
    """启动 DHCP 获取 IP，返回 IP 地址或 None。"""
    dhcp_client = cfg.get('DHCP客户端', 'auto')
    dhcp_timeout = cfg.get('DHCP超时秒数', 20)

    # 自动选择 DHCP 客户端
    if dhcp_client == 'auto':
        if shutil.which('dhclient'):
            dhcp_client = 'dhclient'
        elif shutil.which('udhcpc'):
            dhcp_client = 'udhcpc'
        else:
            logger.error("[wifi_connect] DHCP 客户端不可用（dhclient/udhcpc 均未找到）")
            return None

    logger.info(f"[wifi_connect] 启动 DHCP: client={dhcp_client} iface={interface} timeout={dhcp_timeout}s")

    try:
        if dhcp_client == 'dhclient':
            # -1: 只尝试一次获取租约
            # -nw: 后台运行，不阻塞
            # -pf/-lf: 指定 pid 和 lease 文件，避免影响其他接口
            # -e IF_METRIC=100: 设置低优先级路由，不覆盖以太网默认路由
            pid_file = f'/tmp/dhclient_{interface}.pid'
            lease_file = f'/tmp/dhclient_{interface}.leases'
            cmd = ['dhclient', '-1', '-nw', '-pf', pid_file, '-lf', lease_file,
                   '-e', 'IF_METRIC=100', interface]
            rc, out, err = wifi_mode_utils._run_cmd(cmd, timeout=10)
            logger.info(f"[wifi_connect] dhclient 启动: rc={rc} stderr={err[:200] if err else ''}")
            # dhclient -nw 会立即返回，需要轮询等待 IP
            # 后续的 IP 检查轮询会处理
        elif dhcp_client == 'udhcpc':
            cmd = ['udhcpc', '-i', interface, '-t', str(dhcp_timeout), '-n', '-q']
            rc, out, err = wifi_mode_utils._run_cmd(cmd, timeout=dhcp_timeout + 5)
        else:
            logger.error(f"[wifi_connect] 未知 DHCP 客户端: {dhcp_client}")
            return None

        logger.info(f"[wifi_connect] DHCP 命令结束: cmd={cmd[0]} rc={rc} stdout={out[:200] if out else ''} stderr={err[:200] if err else ''}")
    except Exception as e:
        logger.error(f"[wifi_connect] DHCP 执行异常: {e}")
        return None

    # 检查是否拿到 IP（轮询，因为 dhclient -nw 在后台完成）
    import re
    ip_address = None
    check_deadline = time.time() + dhcp_timeout  # 等待 DHCP 完成
    while time.time() < check_deadline:
        try:
            rc2, out2, _ = wifi_mode_utils._run_cmd(['ip', '-4', 'addr', 'show', interface])
            if rc2 == 0:
                match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out2)
                if match:
                    ip_address = match.group(1)
                    logger.info(f"[wifi_connect] DHCP 成功获取 IP: {ip_address}")
                    break
        except Exception as _e:
            logger.warning(f"[wifi_connect] ip addr show 异常: {_e}")
        time.sleep(1)

    # 清理 dhclient 进程（防止残留）
    try:
        pid_file = f'/tmp/dhclient_{interface}.pid'
        if os.path.exists(pid_file):
            with open(pid_file, 'r') as f:
                pid = f.read().strip()
            if pid:
                wifi_mode_utils._run_cmd(['kill', pid])
                logger.info(f"[wifi_connect] 已清理 dhclient 进程: pid={pid}")
            os.remove(pid_file)
    except Exception as _e:
        logger.warning(f"[wifi_connect] 清理 dhclient 异常（忽略）: {_e}")

    return ip_address


def _sniff_with_stop_poll(iface, prn, total_timeout, stop_check_fn=None,
                          store=False, poll_interval=1.0):
    """Run Scapy sniff in short slices so stop can be observed without packets."""
    deadline = time.time() + max(0.0, float(total_timeout or 0))

    while time.time() < deadline:
        if _stop_requested(stop_check_fn):
            return

        slice_timeout = min(float(poll_interval), max(0.0, deadline - time.time()))
        if slice_timeout <= 0:
            break

        sniff(
            iface=iface,
            prn=prn,
            store=store,
            promisc=False,
            timeout=slice_timeout,
            stop_filter=(lambda p: _stop_requested(stop_check_fn)) if stop_check_fn else None,
        )

        if _stop_requested(stop_check_fn):
            return


def _sniff_collect_with_stop(iface, dwell_time, stop_check_fn=None, poll_interval=0.1):
    """切片 sniff 收集数据包，支持轮询停止。

    与 _sniff_with_stop_poll 不同，此函数以 store=True 收集并返回所有数据包，
    适用于 full scan 等需要事后解析 RSSI 的场景。

    Args:
        iface: 网卡接口名
        dwell_time: 总抓包时长（秒）
        stop_check_fn: 停止检查函数，返回 True 则中止
        poll_interval: 轮询间隔（秒），默认 0.1

    Returns:
        list: 收集到的数据包列表，停止时返回已收集的部分
    """
    packets = []
    deadline = time.time() + max(0.0, float(dwell_time or 0))

    while time.time() < deadline:
        if _stop_requested(stop_check_fn):
            break

        remaining = max(0.0, deadline - time.time())
        slice_timeout = min(float(poll_interval), remaining)
        if slice_timeout <= 0:
            break

        try:
            chunk = sniff(
                iface=iface,
                promisc=False,
                timeout=slice_timeout,
                store=True,
                stop_filter=(lambda p: _stop_requested(stop_check_fn)) if stop_check_fn else None,
            )
            if chunk:
                packets.extend(chunk)
        except Exception as e:
            logger.warning(f"⚠️ _sniff_collect_with_stop 异常: {e}")
            break

        if _stop_requested(stop_check_fn):
            break

    return packets


def dual_sniff(interface_dir, target_macs_set, channel, bandwidth, dwell_time, stop_check_fn=None):
    """
    S7: 双卡并行 sniff 工具函数
    
    在定向天线和全向天线上同时抓包，返回两路 RSSI 数据。
    当全向网卡未启用或信道切换失败时，自动降级为仅定向模式。
    
    Args:
        interface_dir: 定向天线网卡名
        target_macs_set: 目标 MAC 集合 (set)
        channel: 信道
        bandwidth: 带宽
        dwell_time: 抓包时长（秒）
        stop_check_fn: 可选的停止检查函数，返回 True 则中止
    
    Returns:
        (directional_rssi, omni_rssi, omni_ok)
        - directional_rssi: {mac: [rssi1, rssi2, ...]}
        - omni_rssi: {mac: [rssi1, rssi2, ...]}
        - omni_ok: bool, 全向网卡本次是否正常工作
    """
    directional_rssi = {}
    omni_rssi = {}
    omni_ok = False
    
    def make_handler(target_dict, analyze_beacon=False):
        """生成 sniff 回调函数。analyze_beacon=True 时同步做 S9 Beacon 分析（仅定向天线传 True）"""
        def handler(packet):
            if RadioTap not in packet or Dot11 not in packet:
                return
            # S9: 定向天线顺带分析 Beacon 帧（全向天线不做，避免双线程并发写 cache）
            if analyze_beacon:
                _analyze_beacon_for_starlink(packet)
            ta_mac = packet[Dot11].addr2
            if not ta_mac:
                return
            ta_mac = ta_mac.lower().strip()
            if ta_mac not in target_macs_set:
                return
            rssi = getattr(packet[RadioTap], 'dBm_AntSignal', None)
            if rssi is not None:
                if ta_mac not in target_dict:
                    target_dict[ta_mac] = []
                target_dict[ta_mac].append(rssi)
        return handler
    
    # 定向天线线程（analyze_beacon=True：顺带做 S9 Beacon 分析）
    t_dir = threading.Thread(
        target=_sniff_with_stop_poll,
        kwargs={
            'iface': interface_dir,
            'prn': make_handler(directional_rssi, analyze_beacon=True),
            'store': False,
            'total_timeout': dwell_time,
            'stop_check_fn': stop_check_fn,
        }
    )
    
    # 全向天线线程（仅当启用且信道切换成功）
    t_omni = None
    if OMNI_ENABLED:
        omni_ch_ok = adjust_omni_channel(channel, bandwidth)
        if omni_ch_ok:
            omni_ok = True
            t_omni = threading.Thread(
                target=_sniff_with_stop_poll,
                kwargs={
                    'iface': CAPTURE_OMNI_INTERFACE,
                    'prn': make_handler(omni_rssi),
                    'store': False,
                    'total_timeout': dwell_time,
                    'stop_check_fn': stop_check_fn,
                }
            )
        else:
            logger.warning(f"⚠️ [全向] 信道{channel} {bandwidth} 切换失败，本次仅定向采集")
    
    # 并行启动
    t_dir.start()
    if t_omni:
        t_omni.start()
    
    # 等待完成
    t_dir.join()
    if t_omni:
        t_omni.join()
    
    return directional_rssi, omni_rssi, omni_ok


def merge_omni_to_result(result_dict, mac, omni_rssi, omni_ok):
    """
    S7: 将全向天线数据合并到单条扫描结果中
    
    Args:
        result_dict: 要追加字段的结果字典
        mac: 目标 MAC 地址
        omni_rssi: {mac: [rssi...]} 全向采集数据
        omni_ok: bool, 全向网卡本次是否正常工作
    """
    if not OMNI_ENABLED:
        result_dict["omni_rssi_avg"] = None
        result_dict["omni_sample_count"] = 0
        result_dict["omni_status"] = "disabled"
    elif not omni_ok:
        result_dict["omni_rssi_avg"] = None
        result_dict["omni_sample_count"] = 0
        result_dict["omni_status"] = "channel_switch_failed"
    elif mac in omni_rssi and omni_rssi[mac]:
        samples = omni_rssi[mac]
        result_dict["omni_rssi_avg"] = round(sum(samples) / len(samples), 2)
        result_dict["omni_sample_count"] = len(samples)
        result_dict["omni_status"] = "success"
    else:
        result_dict["omni_rssi_avg"] = None
        result_dict["omni_sample_count"] = 0
        result_dict["omni_status"] = "no_signal"


# ==================== S7 END ====================

def setup_monitor_mode(interface_name, channel, bandwidth):
    global current_monitor_interface
    logger.info(f"尝试配置网卡到监听模式", extra={
        "interface": interface_name,
        "channel": channel,
        "bandwidth": bandwidth
    })
    
    try:
        # 0. 驱逐 NetworkManager / wpa_supplicant，防止它们在设置后还原网卡模式
        for svc in ['NetworkManager', 'wpa_supplicant']:
            try:
                result = subprocess.run(['sudo', 'systemctl', 'stop', svc],
                                        capture_output=True, timeout=5)
                if result.returncode == 0:
                    logger.info(f"✅ 已停止 {svc}")
                else:
                    # 服务不存在或已停止，忽略
                    logger.debug(f"服务 {svc} 未运行或不存在，跳过")
            except Exception as svc_e:
                logger.debug(f"停止 {svc} 时异常（可忽略）: {svc_e}")

        # 0.5 强制 pkill，因为 systemctl stop 并不能保证进程立即消亡
        #     wpa_supplicant 可能在 ifconfig up 之后立刻复活并把网卡改回 managed 模式 (EBUSY)
        try:
            subprocess.run(['sudo', 'pkill', '-9', '-f', 'wpa_supplicant'],
                           capture_output=True, timeout=5)
            logger.info("✅ pkill wpa_supplicant 已执行")
        except Exception:
            pass  # 进程不存在时 pkill 返回非 0，忽略即可
        try:
            subprocess.run(['sudo', 'pkill', '-9', '-f', 'NetworkManager'],
                           capture_output=True, timeout=5)
            logger.info("✅ pkill NetworkManager 已执行")
        except Exception:
            pass

        time.sleep(1.0)  # 等待进程彻底退出，避免 ifconfig up 时被立即夺权

        # 1. 关闭网卡
        subprocess.run(['sudo', 'ifconfig', interface_name, 'down'], check=True, capture_output=True)
        logger.debug("网卡已关闭", extra={"interface": interface_name})

        
        # 2. 设置为监听模式
        subprocess.run(['sudo', 'iw', 'dev', interface_name, 'set', 'type', 'monitor'], check=True, capture_output=True)
        logger.debug("网卡类型已设置为监听模式", extra={"interface": interface_name})
        
        # 3. 启动网卡
        subprocess.run(['sudo', 'ifconfig', interface_name, 'up'], check=True, capture_output=True)
        logger.debug("网卡已启动", extra={"interface": interface_name})
        
        # 4. 等待网卡状态稳定
        logger.info("等待网卡状态稳定...", extra={"interface": interface_name})
        time.sleep(2.0)  # 等待1秒让驱动稳定
        
        # 5. 验证网卡模式
        try:
            result = subprocess.run(['sudo', 'iw', 'dev', interface_name, 'info'], 
                                  capture_output=True, text=True, check=True)
            if 'type monitor' in result.stdout.lower():
                logger.info("网卡已成功进入监听模式", extra={"interface": interface_name})
            else:
                logger.warning("网卡可能未正确进入监听模式", extra={
                    "interface": interface_name,
                    "iw_info": result.stdout
                })
        except Exception as e:
            logger.warning("无法验证网卡模式", extra={
                "interface": interface_name,
                "error": str(e)
            })
        
        # 6. 设置频道和带宽
        iw_params = _iw_channel_width_params(bandwidth)
        subprocess.run(['sudo', 'iw', 'dev', interface_name, 'set', 'channel', str(channel)] + iw_params, check=True, capture_output=True)
        logger.debug("频道和带宽设置完成", extra={
            "interface": interface_name,
            "channel": channel,
            "bandwidth": bandwidth
        })
        
        # 7. 再次等待频道设置稳定
        logger.info("等待频道设置稳定...", extra={"interface": interface_name})
        time.sleep(2.0)  # 等待0.5秒让频道稳定
        
        # 8. 验证频道设置
        try:
            result = subprocess.run(['sudo', 'iw', 'dev', interface_name, 'info'], 
                                  capture_output=True, text=True, check=True)
            if f'channel {channel}' in result.stdout:
                logger.info("✅ 监听模式设置成功，当前网卡状态:", extra={
                    "interface": interface_name, 
                    "target_channel": channel,
                    "target_bandwidth": bandwidth,
                    "iw_info": result.stdout
                })
            else:
                logger.warning("⚠️ 频道设置可能未生效", extra={
                    "interface": interface_name,
                    "expected_channel": channel,
                    "expected_bandwidth": bandwidth,
                    "iw_info": result.stdout
                })
        except Exception as e:
            logger.warning("无法验证频道设置", extra={
                "interface": interface_name,
                "error": str(e)
            })
        
        current_monitor_interface = interface_name
        
        if int(channel) <= 14: 
            freq_mhz = 2412 + (int(channel) - 1) * 5
        else: 
            freq_mhz = 5000 + int(channel) * 5
            
        logger.info("网卡配置完成，准备就绪", extra={
            "interface": current_monitor_interface,
            "frequency_mhz": freq_mhz,
            "channel": channel,
            "bandwidth": bandwidth,
            "total_setup_time": "1.5秒"
        })
        return current_monitor_interface
        
    except subprocess.CalledProcessError as e:
        logger.error("配置网卡失败", extra={
            "interface": interface_name,
            "error_type": "subprocess.CalledProcessError",
            "error_code": e.returncode,
            "stderr": e.stderr.decode() if e.stderr else "无错误输出",
            "command": e.cmd
        })
        return None
    except Exception as e:
        logger.error("配置网卡时发生未知错误", extra={
            "interface": interface_name,
            "error": str(e),
            "error_type": type(e).__name__
        })
        return None



def packet_processor(packet):
    """
    数据包处理函数
    根据capture_mode决定是否处理数据包
    """
    global target_mac_filter, target_mac_match_mode, r, pcap_writer, packet_stats, last_source_index, capture_mode
    
    # 🔥 新增：如果处于暂停模式，直接丢弃数据包
    if capture_mode == "paused":
        return
    
    # 更新总包数统计
    packet_stats['total_packets'] += 1
    
    
    # 1. 基础包类型检查
    if RadioTap not in packet or Dot11 not in packet: 
        return

    # 2. 提取关键信息
    try:
        dot11 = packet[Dot11]

        # 3. MAC地址过滤：默认按 TA；定位最终抓包可要求任意 802.11 地址字段命中目标 MAC。
        if target_mac_filter and not _packet_matches_target(dot11):
            packet_stats['filtered_packets'] += 1
            return

        ta_mac = _normalize_dot11_mac(dot11.addr2)
        target_is_ta = (not target_mac_filter) or (ta_mac == target_mac_filter)

        scan_status = r.get('capture:scan_status')
        if scan_status == 'PREPARING':
            logger.debug("扫描准备中，丢弃数据包")
            return

        # 4. 保存到 pcap 文件（如果启用）。定位抓包 any_addr 模式下，即使目标不是 TA 也保留原始帧。
        if pcap_writer:
            try:
                pcap_writer.write(packet)
                packet_stats['pcap_written_packets'] = packet_stats.get('pcap_written_packets', 0) + 1
                if packet_stats['pcap_written_packets'] % 10 == 0:
                    _write_capture_status(True, phase='capturing', force=True)
            except Exception:
                pass  # pcap写入失败不影响数据处理

        if not ta_mac:
            return

        # any_addr 只放宽 pcap 口径；RSSI/网格仍只统计 TA=目标 MAC 的帧，避免 AP 下行包污染定位。
        if target_mac_filter and target_mac_match_mode == "any_addr" and not target_is_ta:
            return

        # 5. 提取RSSI
        rssi = None
        if RadioTap in packet and hasattr(packet[RadioTap], 'dBm_AntSignal'):
            rssi = packet[RadioTap].dBm_AntSignal
        elif hasattr(packet, 'dBm_AntSignal'):
            rssi = packet.dBm_AntSignal
        
        if rssi is None:
            return  # 无RSSI信息的包直接丢弃
        
        # 6. 获取当前云台位置和状态（修复JSON结构解析）
        ptz_raw = r.get(PTZ_STATUS_KEY)
        if not ptz_raw:
            # 如果没有PTZ状态，使用默认值，但仍然处理数据包
            pan_val = 0.0
            tilt_val = 0.0
            current_state = "UNKNOWN"
        else:
            try:
                ptz_data = json.loads(ptz_raw)
                position = ptz_data.get("position", {})
                pan_val = float(position.get("pan", 0))
                tilt_val = float(position.get("tilt", 0))
                current_state = ptz_data.get("state", "UNKNOWN")
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                pan_val = 0.0
                tilt_val = 0.0
                current_state = "UNKNOWN"
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
        packet_stats['parse_errors'] += 1
        return

    # 8. 存储原始数据到列表
    record = {
        "capture_ts": datetime.fromtimestamp(float(packet.time)).strftime("%Y-%m-%d %H:%M:%S.%f"),
        "rssi": rssi, 
        "mac": ta_mac,
        "pan": pan_val, 
        "tilt": tilt_val
    }

    try:
        r.lpush(CAPTURE_RSSI_LIST, json.dumps(record))

        
        # 10. 更新统计
        packet_stats['processed_packets'] += 1
        _write_capture_status(True, phase='capturing')

         # �� 在这里添加网格处理逻辑（第390行后）
         # 在第448行之前添加
        process_grid_logic_for_packet(r, pan_val, tilt_val, rssi, current_state)
         # 🔥 智能扫描：简单推送，无状态，避免竞态条件
        push_signal_to_intelligent_scan(pan_val, tilt_val, rssi)
        
        # 记录成功处理（调试级别）
        logger.debug("数据包处理成功", extra={
            "rssi": rssi,
            "mac": ta_mac,
            "ptz_pan": pan_val,
            "ptz_tilt": tilt_val,
            "ptz_state": current_state
        })
        
    except (redis.RedisError, Exception) as e:
        # Redis操作失败时静默处理
        packet_stats['redis_errors'] += 1

def sniff_loop(interface, stop_event):
    logger.info(f"抓包线程已在接口上启动 {interface}", extra={"interface": interface})
    try:
        if target_mac_filter:
            mac_address = target_mac_filter.lower().strip()
            if target_mac_match_mode == "any_addr":
                filter_str = "wlan addr " + mac_address
            else:
                filter_str = "wlan addr2 " + mac_address
            logger.info(f"应用MAC包处理过滤器: {filter_str}", extra={
                "interface": interface,
                "target_mac": target_mac_filter,
                "target_mac_match": target_mac_match_mode,
                "packet_filter": filter_str
            })
        else:
            filter_str = ""
            logger.info("没有设置目标MAC地址，不进行过滤")
        
        while not stop_event.is_set():
            sniff(
                iface=interface,
                prn=packet_processor,
                store=False,
                promisc=False,
                timeout=1.0,
                stop_filter=lambda p: stop_event.is_set(),
            )
    except Exception as e:
        logger.error("Scapy sniff loop 失败", extra={
            "interface": interface,
            "error": str(e),
            "error_type": type(e).__name__
        })
        # 添加更详细的错误信息
        logger.error(f"详细错误信息: {traceback.format_exc()}", extra={
            "interface": interface,
            "error_details": str(e)
        })
    logger.info("抓包线程已停止", extra={"interface": interface})

def start_capture(interface_name, pcap_filename=None, pcap_split_size_mb=None, capture_meta=None):
    global capture_thread, stop_sniffing_event, pcap_writer, packet_stats, current_capture_meta
    # 验证MAC地址过滤器设置
    if not target_mac_filter:
        logger.warning("未设置目标MAC地址过滤器，将捕获所有数据包", extra={
            "interface": interface_name
        })
    else:
        logger.info("使用MAC地址过滤器", extra={
            "interface": interface_name,
            "target_mac": target_mac_filter,
            "target_mac_match": target_mac_match_mode
        })
    try:
       
        stop_sniffing_event = threading.Event()
        # 启动定时更新线程
        start_timer_update()
        
        # 重置统计计数器
        packet_stats = _new_packet_stats()
        started_at = time.time()
        current_capture_meta = dict(capture_meta or {})
        current_capture_meta.update({
            'active': True,
            'running': True,
            'phase': 'capturing',
            'started_at': started_at,
            'interface': interface_name,
            'target_mac': target_mac_filter,
            'target_mac_match': target_mac_match_mode,
            'pcap_filename': pcap_filename,
            'pcap_split_size_mb': pcap_split_size_mb,
        })
        capture_time_limit = current_capture_meta.get('capture_time_limit')
        if capture_time_limit not in (None, ''):
            current_capture_meta['capture_time_limit'] = float(capture_time_limit)
            current_capture_meta['deadline_at'] = started_at + float(capture_time_limit)
        
        if pcap_filename:
            pcap_path = os.path.join(PCAP_STORAGE_DIR, pcap_filename)
            os.makedirs(PCAP_STORAGE_DIR, exist_ok=True)
            pcap_writer = RotatingPcapWriter(pcap_path, split_size_mb=pcap_split_size_mb)
            logger.info("已启动存盘模式", extra={
                "pcap_file": pcap_path,
                "pcap_files": _current_pcap_files(),
                "split_size_mb": pcap_split_size_mb,
                "interface": interface_name
            })
            current_capture_meta['pcap_file'] = pcap_path
            current_capture_meta['pcap_files'] = _current_pcap_files()
        
        capture_thread = threading.Thread(target=sniff_loop, args=(interface_name, stop_sniffing_event))
        capture_thread.daemon = True
        capture_thread.start()
        
        # 启动统计信息输出线程
        stats_thread = threading.Thread(target=stats_reporter, args=(stop_sniffing_event,))
        stats_thread.daemon = True
        stats_thread.start()
        
        _write_capture_status(True, phase='capturing', force=True)
        logger.info("Scapy 捕获已启动", extra={"interface": interface_name})
        return True
    except Exception as e:
        logger.error("启动 Scapy 捕获失败", extra={
            "interface": interface_name,
            "error": str(e),
            "error_type": type(e).__name__
        })
        _finish_capture_status('start_capture_failed', phase='error')
        return False

def stats_reporter(stop_event):
    """定期输出统计信息的线程函数"""
    global packet_stats
    while not stop_event.wait(30):  # 每30秒输出一次
        if packet_stats['total_packets'] > 0:
            logger.info("数据包处理统计", extra={
                "total_packets": packet_stats['total_packets'],
                "filtered_packets": packet_stats['filtered_packets'],
                "processed_packets": packet_stats['processed_packets'],
                "redis_errors": packet_stats['redis_errors'],
                "filter_rate": f"{(packet_stats['filtered_packets'] / packet_stats['total_packets'] * 100):.1f}%"
            })

def stop_capture():
    global capture_thread, stop_sniffing_event, pcap_writer
    
    logger.info("开始停止抓包...")
    
    # 1. 首先设置停止标志，让Scapy优雅停止
    if stop_sniffing_event and not stop_sniffing_event.is_set():
        logger.info("设置停止标志，通知Scapy停止抓包")
        stop_sniffing_event.set()
    
    # 2. 等待抓包线程优雅结束
    if capture_thread and capture_thread.is_alive():
        logger.info("等待抓包线程优雅结束...")
        capture_thread.join(timeout=3.0)  # 等待最多3秒
        
        if capture_thread.is_alive():
            logger.warning("抓包线程未能在3秒内优雅结束，强制终止")
            # 注意：在Python中，线程无法被强制终止，只能等待
    
    # 3. 关闭pcap文件写入器
    if pcap_writer:
        try:
            pcap_writer.close()
            pcap_writer = None
            logger.info("pcap文件写入器已关闭")
        except Exception as e:
            logger.warning(f"关闭pcap文件写入器时发生错误: {e}")
    stop_timer_update()
    # 4. 清理线程引用
    capture_thread = None
    stop_sniffing_event = None
    
    logger.info("抓包停止完成")

def adjust_channel_with_retry(interface_name, channel, bandwidth, max_retries=3, logger=None):
    """
    带重试机制的信道切换函数（带缓存优化）
    
    Args:
        interface_name: 监控模式网卡名称，如 "wlan0mon"
        channel: 目标信道号，如 1, 6, 11
        bandwidth: 带宽类型，如 "HT20", "HT40+"
        max_retries: 最大重试次数
        logger: 日志对象
    
    Returns:
        bool: 切换成功返回True，失败返回False
    
    说明：
        信道切换可能因为硬件繁忙、驱动问题等失败
        使用指数退避策略重试：第1次等0.5秒，第2次等1秒，第3次等2秒
        如果目标信道和带宽与当前相同，跳过切换操作（性能优化）
    """
    global current_channel_by_interface, current_bandwidth_by_interface
    
    # 🔥 优化：如果信道和带宽相同，跳过切换
    if (
        current_channel_by_interface.get(interface_name) == channel and
        current_bandwidth_by_interface.get(interface_name) == bandwidth
    ):
        if not _interface_is_monitor(interface_name):
            _clear_interface_channel_cache(interface_name)
            if logger:
                logger.warning(f"⚠️ [{interface_name}] 信道缓存命中但网卡不在 monitor 模式，重新初始化")
        else:
            if logger:
                logger.info(f"⚡ [{interface_name}] 信道无需切换（已在 {channel} {bandwidth}），跳过")
            return True

    if not _interface_is_monitor(interface_name):
        _clear_interface_channel_cache(interface_name)
        if logger:
            logger.warning(f"⚠️ [{interface_name}] 当前不在 monitor 模式，先重新配置 monitor")
        if setup_monitor_mode(interface_name, channel, bandwidth):
            current_channel_by_interface[interface_name] = channel
            current_bandwidth_by_interface[interface_name] = bandwidth
            return True

    for attempt in range(max_retries):
        try:
            # 调用已有的adjust_channel_and_bandwidth函数
            if adjust_channel_and_bandwidth(interface_name, channel, bandwidth):
                # 更新当前网卡的状态缓存
                current_channel_by_interface[interface_name] = channel
                current_bandwidth_by_interface[interface_name] = bandwidth
                if logger:
                    logger.info(f"✅ [{interface_name}] 信道切换成功: {channel} {bandwidth}")
                return True
            else:
                if logger:
                    logger.warning(f"⚠️ 信道切换失败(尝试 {attempt+1}/{max_retries})")
                if not _interface_is_monitor(interface_name):
                    _clear_interface_channel_cache(interface_name)
                    if logger:
                        logger.warning(f"⚠️ [{interface_name}] 信道切换失败后发现不是 monitor，尝试恢复")
                    if setup_monitor_mode(interface_name, channel, bandwidth):
                        current_channel_by_interface[interface_name] = channel
                        current_bandwidth_by_interface[interface_name] = bandwidth
                        return True
        except Exception as e:
            if logger:
                logger.error(f"❌ 信道切换异常(尝试 {attempt+1}/{max_retries}): {e}")
        
        # 如果不是最后一次尝试，等待后重试
        if attempt < max_retries - 1:
            wait_time = 0.5 * (2 ** attempt)  # 指数退避: 0.5s, 1s, 2s
            if logger:
                logger.info(f"⏳ 等待 {wait_time}秒 后重试...")
            time.sleep(wait_time)
    
    # 所有重试都失败
    if logger:
        logger.error(f"❌ 信道切换最终失败: {channel} {bandwidth}")
    return False

def capture_worker_main():
    """🔥 抓包工作进程主函数 - 增强版实时同步"""
    global target_mac_filter, target_mac_match_mode, saved_capture_config, r
    logger.info("🚀 启动抓包工作进程")
    
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        logger.info("连接 Redis 成功", extra={
            "redis_host": REDIS_HOST,
            "redis_port": REDIS_PORT
        })
        r.set(CAPTURE_RUNNING_KEY, '0')
        r.set(CAPTURE_STATUS_KEY, json.dumps({
            'active': False,
            'running': False,
            'phase': 'idle',
            'updated_at': time.time(),
            'packet_counts': _new_packet_stats(),
            'pcap_files': [],
        }, ensure_ascii=False))
        
        # ⚡ 启动时自动把网卡设置到 Monitor 模式（不启动后台 sniffer）
        #    这样后续 scan_at_point 来临时，iw set channel 不会 EBUSY
        #    使用信道 36 作为初始化信道（任意合法5GHz信道即可，后续扫描会切换到目标信道）
        logger.info(f"📡 正在初始化网卡 [{CAPTURE_DEFAULT_INTERFACE}] 到 Monitor 模式...")
        if setup_monitor_mode(CAPTURE_DEFAULT_INTERFACE, channel=36, bandwidth='HT20'):
            logger.info(f"✅ 网卡 [{CAPTURE_DEFAULT_INTERFACE}] 已成功进入 Monitor 模式（信道36 HT20）")
        else:
            logger.warning(f"⚠️ 网卡 [{CAPTURE_DEFAULT_INTERFACE}] Monitor 模式初始化失败，信道切换可能不稳定")
        
        # S7: 全向天线网卡 Monitor 初始化
        if OMNI_ENABLED:
            logger.info(f"📡 [全向] 正在初始化网卡 [{CAPTURE_OMNI_INTERFACE}] 到 Monitor 模式...")
            if setup_omni_monitor_once(channel=36, bandwidth='HT20'):
                logger.info(f"✅ [全向] 网卡 [{CAPTURE_OMNI_INTERFACE}] 已成功进入 Monitor 模式（信道36 HT20）")
            else:
                logger.warning(f"⚠️ [全向] 网卡 [{CAPTURE_OMNI_INTERFACE}] Monitor 模式初始化失败，全向采集可能不可用")
        else:
            logger.info("ℹ️ 全向天线未配置（CAPTURE_OMNI_INTERFACE 为空），仅使用定向天线")
        
        # S7: 将全向天线启用状态写入 Redis，供 ptz_control 构建 render_meta 时读取
        r.set('capture:omni_enabled', '1' if OMNI_ENABLED else '0')

        # S9: 从持久化白名单预加载星链设备（跨会话保持识别结果，无需重新扫描）
        _load_starlink_whitelist(r)
        
        # 🔥 新增：启动时清空并重建全局网格
        try:
            logger.info("🧹 开始清空全局网格...")
            cleared_count = grid_utils.clear_all_global_grids(r)
            logger.info(f"✅ 已清空 {cleared_count} 个全局网格")
            # 新增：初始化capture扫描状态
            r.set('capture:scan_status', 'IDLE')
            logger.info("🔓 初始化capture状态为IDLE")
            # 🔥 新增：从Redis读取默认配置并更新全局变量
            try:
                default_config = r.hgetall('gimbal:default_config')
                if default_config:
                    pan_range = default_config.get('pan_range', '[90,270]')
                    tilt_range = default_config.get('tilt_range', '[-85,20]')
                    
                    # 确保pan_range和tilt_range是列表格式
                    if isinstance(pan_range, str):
                        pan_range = json.loads(pan_range)
                    if isinstance(tilt_range, str):
                        tilt_range = json.loads(tilt_range)
                    
                    grid_utils.set_global_grid_config(pan_range, tilt_range)
                    logger.info(f"✅ 已从Redis更新全局网格配置: Pan={pan_range}, Tilt={tilt_range}")
                else:
                    logger.warning("❌ 未找到默认配置，使用硬编码值")
            except Exception as e:
                logger.error(f"❌ 更新全局网格配置失败: {e}")

            # 🔥 新增：初始化步径配置（清除旧的，使用默认的）
            try:
                # 清除旧的当前步径配置
                r.delete('gimbal:current_config')
                logger.info("�� 已清除旧的步径配置")
                
                # 从默认配置初始化当前步径
                default_config = r.hgetall('gimbal:default_config')
                if default_config:
                    pan_step = default_config.get('pan_step', '5')
                    tilt_step = default_config.get('tilt_step', '5')
                    
                    current_config = {
                        'source_steph': float(pan_step),
                        'source_stepv': float(tilt_step),
                        'updated_by': 'capture_worker_startup',
                        'ts': time.time()
                    }
                    r.set('gimbal:current_config', json.dumps(current_config))
                    logger.info(f"✅ 已初始化步径配置: ({pan_step}, {tilt_step})")
                else:
                    logger.warning("❌ 未找到默认步径配置")
            except Exception as e:
                logger.error(f"❌ 初始化步径配置失败: {e}")
            
        except Exception as e:
            logger.error(f"❌ 全局网格初始化失败: {e}")
            # 不中断程序，继续运行
        
        
        while not shutdown_requested:
            try:
                command_data = r.brpop(CAPTURE_COMMAND_QUEUE, timeout=1)
                if not command_data: 
                    continue
                    
                command = json.loads(command_data[1])
                action = command.get("action")
                logger.info("收到命令", extra={
                    "action": action,
                    "command_data": command
                })
                
                if action == "start_config_session":
                    _start_full_scan_config_session(command)
                    continue

                if action == "stop_config_session":
                    cmd_scan_id = command.get('scan_id')
                    notify_key = command.get('notify_key')
                    session = full_scan_config_session
                    if cmd_scan_id and session and session.get('scan_id') != cmd_scan_id:
                        payload = {
                            'status': 'skipped',
                            'reason': 'scan_id_mismatch',
                            'scan_id': cmd_scan_id,
                            'active_scan_id': session.get('scan_id'),
                        }
                    else:
                        had_active_session = session is not None
                        stopped = _stop_full_scan_config_session(
                            reason=command.get('reason') or 'stopped',
                            timeout=float(command.get('timeout') or 3.0),
                        )
                        payload = {
                            'status': (
                                'stopped'
                                if stopped
                                else ('timeout' if had_active_session else 'idle')
                            ),
                            'reason': command.get('reason') or 'stopped',
                            'scan_id': cmd_scan_id,
                        }
                    if notify_key:
                        r.lpush(notify_key, json.dumps(payload, ensure_ascii=False))
                    continue

                if action == "start_refinement_session":
                    payload = _start_full_scan_refinement_session(command)
                    notify_key = command.get("notify_key")
                    if notify_key:
                        r.lpush(notify_key, json.dumps(payload, ensure_ascii=False))
                    continue

                if action == "set_refinement_segment":
                    notify_key = command.get("notify_key")
                    session = full_scan_refinement_session
                    if (
                        not session
                        or session.get("scan_id") != command.get("scan_id")
                    ):
                        payload = {
                            "status": "skipped",
                            "reason": "session_not_active",
                        }
                    else:
                        segment_id = command.get("segment_id")
                        with session["lock"]:
                            session["segment_id"] = segment_id
                            if segment_id:
                                session["segment_stats"].setdefault(
                                    segment_id,
                                    {},
                                )
                        payload = {
                            "status": "ready",
                            "segment_id": segment_id,
                        }
                    if notify_key:
                        r.lpush(notify_key, json.dumps(payload, ensure_ascii=False))
                    continue

                if action == "finish_refinement_segment":
                    notify_key = command.get("notify_key")
                    segment_id = command.get("segment_id")
                    session = full_scan_refinement_session
                    if (
                        not session
                        or session.get("scan_id") != command.get("scan_id")
                    ):
                        payload = {
                            "status": "skipped",
                            "reason": "session_not_active",
                            "segment_id": segment_id,
                        }
                    else:
                        with session["lock"]:
                            if session.get("segment_id") == segment_id:
                                session["segment_id"] = None
                            stats = dict(
                                session["segment_stats"].pop(
                                    segment_id,
                                    {},
                                )
                            )
                        payload = {
                            "status": "done",
                            "segment_id": segment_id,
                            "macs": stats,
                        }
                    if notify_key:
                        r.lpush(notify_key, json.dumps(payload, ensure_ascii=False))
                    continue

                if action == "stop_refinement_session":
                    notify_key = command.get("notify_key")
                    stopped = _stop_full_scan_refinement_session(
                        timeout=float(command.get("timeout") or 1.0),
                    )
                    payload = {
                        "status": "stopped" if stopped else "timeout",
                        "scan_id": command.get("scan_id"),
                    }
                    if notify_key:
                        r.lpush(notify_key, json.dumps(payload, ensure_ascii=False))
                    continue

                if action in ["start_capture", "save_pcap"]:
                    # 🔥 新增：重置网格扫描状态
                    try: 
                        r.delete(CAPTURE_RSSI_LIST)
                        # 2. 🔥【核心需求】清理所有旧的网格数据 (p:*)
                        # 我们直接调用 grid_utils 中已经写好的函数，实现代码复用
                        deleted_grid_count = grid_utils.clear_all_global_grids(r)
                        logger.info(f"已清理 {deleted_grid_count} 个P网格数据")  # 新增日志
                        logger.info("已清理之前的抓包数据列表")
                    except Exception as e:
                        logger.warning("清理数据失败", extra={"error": str(e)})

                        
                    interface, channel, bandwidth, target_mac = CAPTURE_DEFAULT_INTERFACE, command.get("channel"), command.get("bandwidth"), command.get("target_mac")
                    pcap_filename = command.get("pcap_filename")  # 支持 start_capture 和 save_pcap 两种模式
                    pcap_split_size_mb = command.get("pcap_split_size_mb")
                    min_free_memory_mb = command.get("min_free_memory_mb")
                    min_free_disk_mb = command.get("min_free_disk_mb")
                    notify_key = command.get("notify_key")
                    ignore_location_stop = bool(command.get("ignore_location_stop"))
                    requested_match_mode = str(command.get("target_mac_match") or "ta").strip().lower()
                    if requested_match_mode not in ("ta", "any_addr"):
                        requested_match_mode = "ta"

                    if r.get("capture:stop") or ((not ignore_location_stop) and r.get("location_scan:stop")):
                        reason = "manual_stop"
                        logger.warning(f"🛑 启动 {action} 前检测到停止标志，取消抓包")
                        if notify_key:
                            r.lpush(notify_key, json.dumps({
                                "status": "stopped",
                                "reason": reason,
                                "target_mac": str(target_mac or "").lower(),
                                "target_mac_match": requested_match_mode,
                                "pcap_files": [],
                            }))
                        r.set(CAPTURE_RUNNING_KEY, '0')
                        _update_capture_at_best_status(False, 'stopped', {'reason': reason, 'pcap_files': []})
                        continue
                    
                    if not all([channel, bandwidth, target_mac]):
                        logger.error(f"启动 {action} 失败：缺少参数", extra={
                            "action": action,
                            "channel": channel,
                            "bandwidth": bandwidth,
                            "target_mac": target_mac
                        })
                        continue
                    
                    if pcap_filename:
                        reason = _resource_stop_reason(
                            os.path.join(PCAP_STORAGE_DIR, pcap_filename),
                            min_free_memory_mb=min_free_memory_mb,
                            min_free_disk_mb=min_free_disk_mb,
                        )
                        if reason:
                            payload = {
                                "status": "stopped",
                                "reason": reason,
                                "target_mac": target_mac.lower(),
                                "target_mac_match": requested_match_mode,
                                "pcap_files": [],
                            }
                            if notify_key:
                                r.lpush(notify_key, json.dumps(payload))
                            _update_capture_at_best_status(False, 'stopped', {'reason': reason, 'pcap_files': []})
                            logger.warning(f"🛑 存盘前资源不足，取消抓包: {reason}")
                            continue

                    stop_capture()
                    if not r.get("location_scan:stop"):
                        r.delete('capture:stop')
                    target_mac_filter = ''.join(str(target_mac).split()).lower()
                    target_mac_match_mode = requested_match_mode
                    saved_capture_config = {
                        'channel': channel,
                        'bandwidth': bandwidth,
                        'target_mac': target_mac_filter,
                        'target_mac_match': target_mac_match_mode,
                    }
                    
                    logger.info(f"准备启动 {action}", extra={
                        "interface": interface,
                        "channel": channel,
                        "bandwidth": bandwidth,
                        "target_mac": target_mac_filter,
                        "target_mac_match": target_mac_match_mode,
                        "pcap_filename": pcap_filename,
                        "pcap_split_size_mb": pcap_split_size_mb,
                        "notify_key": notify_key,
                        "ignore_location_stop": ignore_location_stop,
                    })
                    
                    capture_time_limit = command.get("capture_time_limit")
                    capture_meta = {
                        'action': action,
                        'channel': channel,
                        'bandwidth': bandwidth,
                        'target_mac': target_mac_filter,
                        'target_mac_match': target_mac_match_mode,
                        'capture_time_limit': float(capture_time_limit) if capture_time_limit not in (None, '') else None,
                        'min_free_memory_mb': min_free_memory_mb,
                        'min_free_disk_mb': min_free_disk_mb,
                    }

                    if setup_monitor_mode_once(interface, channel, bandwidth):
                        time.sleep(3.0)
                        if start_capture(interface, pcap_filename=pcap_filename,
                                         pcap_split_size_mb=pcap_split_size_mb,
                                         capture_meta=capture_meta):
                            logger.info(f"模式 '{action}' 已成功启动", extra={
                                "action": action,
                                "interface": interface,
                                "channel": channel,
                                "bandwidth": bandwidth,
                                "target_mac": target_mac_filter,
                                "target_mac_match": target_mac_match_mode
                            })
                            r.set(CAPTURE_RUNNING_KEY, '1')
                            if action == "save_pcap" and capture_time_limit:
                                stop_reason = "completed"
                                deadline = time.time() + float(capture_time_limit)
                                while time.time() < deadline:
                                    if r.get("capture:stop"):
                                        stop_reason = "manual_stop"
                                        break
                                    if (not ignore_location_stop) and r.get("location_scan:stop"):
                                        stop_reason = "manual_stop"
                                        break
                                    resource_reason = _resource_stop_reason(
                                        os.path.join(PCAP_STORAGE_DIR, pcap_filename),
                                        min_free_memory_mb=min_free_memory_mb,
                                        min_free_disk_mb=min_free_disk_mb,
                                    )
                                    if resource_reason:
                                        stop_reason = resource_reason
                                        break
                                    _write_capture_status(True, phase='capturing', force=True)
                                    time.sleep(1.0)
                                pcap_files = _current_pcap_files()
                                stop_capture()
                                r.set(CAPTURE_RUNNING_KEY, '0')
                                r.delete('capture:stop')
                                _finish_capture_status(
                                    stop_reason,
                                    phase="completed" if stop_reason == "completed" else "stopped",
                                    extra={'pcap_files': pcap_files},
                                )
                                payload = {
                                    "status": "done" if stop_reason == "completed" else "stopped",
                                    "reason": stop_reason,
                                    "target_mac": target_mac_filter,
                                    "target_mac_match": target_mac_match_mode,
                                    "pcap_files": pcap_files,
                                }
                                _update_capture_at_best_status(
                                    False,
                                    "completed" if stop_reason == "completed" else "stopped",
                                    {
                                        "reason": stop_reason,
                                        "pcap_files": pcap_files,
                                    },
                                )
                                if notify_key:
                                    r.lpush(notify_key, json.dumps(payload))
                                logger.info(f"✅ 定位抓包结束: {payload}")
                        else:
                            logger.error("启动抓包失败", extra={
                                "action": action,
                                "interface": interface
                            })
                            if notify_key:
                                r.lpush(notify_key, json.dumps({
                                    "status": "error",
                                    "reason": "start_capture_failed",
                                    "target_mac": target_mac.lower(),
                                    "target_mac_match": requested_match_mode,
                                    "pcap_files": [],
                                }))
                    else: 
                        logger.error("配置网卡失败，无法启动", extra={
                            "action": action,
                            "interface": interface
                        })
                        if notify_key:
                            r.lpush(notify_key, json.dumps({
                                "status": "error",
                                "reason": "setup_monitor_failed",
                                "target_mac": target_mac.lower(),
                                "target_mac_match": requested_match_mode,
                                "pcap_files": [],
                            }))
                
                # ============= 新增：初始MAC发现命令 =============
                elif action == "discover_macs":
                    """
                    初始MAC发现命令
                    功能：在固定点位遍历所有信道/带宽配置，收集发现的TA MAC地址
                    
                    参数：
                        - configs: 配置列表，格式 [{"channel": 1, "bandwidth": "HT20"}, ...]
                        - dwell_time: 每个配置停留时间（秒），默认5秒
                    
                    工作流程：
                        1. 遍历每个信道/带宽配置
                        2. 切换到目标配置
                        3. 临时抓包dwell_time秒
                        4. 收集所有TA MAC地址
                        5. 存储到Redis: multi_scan:target_macs
                        6. 通知完成: multi_scan:initial_scan_notify
                    """
                    logger.info("📡 收到初始MAC发现命令")
                    
                    # 步骤1：获取参数
                    # dwell_time 由调用方（ptz_control）传入，默认20秒
                    # 若需调整，请修改 ptz_control.py 顶部的 INITIAL_SCAN_DWELL_TIME 常量
                    configs = command.get('configs', [])
                    dwell_time = command.get('dwell_time', 20)

                    if not configs:
                        logger.error("❌ discover_macs命令缺少configs参数")
                        r.lpush('multi_scan:initial_scan_notify', json.dumps({
                            'status': 'error',
                            'message': '缺少configs参数'
                        }))
                        continue
                    
                    logger.info(f"🔍 开始MAC发现: {len(configs)}个配置, 每个停留{dwell_time}秒")
                    
                    # 步骤2：首次设置监听模式（使用第一个配置的参数）
                    if configs:
                        first_config = configs[0]
                        first_channel = first_config.get('channel', 1)
                        first_bandwidth = first_config.get('bandwidth', 'HT20')
                        
                        logger.info(f"📡 首次设置监听模式: 信道{first_channel} {first_bandwidth}")
                        if not setup_monitor_mode_once(CAPTURE_DEFAULT_INTERFACE, first_channel, first_bandwidth):
                            logger.error("❌ 无法设置监听模式，中止MAC发现")
                            r.lpush('multi_scan:initial_scan_notify', json.dumps({
                                'status': 'error',
                                'message': '无法设置监听模式'
                            }))
                            continue
                        
                        logger.info("✅ 网卡已进入监听模式")
                    
                    # 步骤3：初始化结果存储
                    discovered_macs = {}  # {mac: {"channel": x, "bandwidth": "HT20"}}
                    
                    # 步骤4：遍历每个配置
                    for idx, config in enumerate(configs):
                        # 🛑 检查停止标志（初始扫描自身的 + 多点扫描的）
                        if r.get('multi_scan:stop_initial_scan') or r.get('multi_scan:stop_multi_point_scan'):
                            logger.warning("🛑 检测到停止标志，终止初始扫描")
                            r.lpush('multi_scan:initial_scan_notify', json.dumps({
                                'status': 'stopped',
                                'message': '用户主动停止',
                                'mac_count': len(discovered_macs)
                            }))
                            break
                        
                        channel = config.get('channel')
                        bandwidth = config.get('bandwidth')
                        
                        if not channel or not bandwidth:
                            logger.warning(f"⚠️ 配置{idx}缺少channel或bandwidth，跳过")
                            continue
                        
                        logger.info(f"🔄 [{idx+1}/{len(configs)}] 切换到 信道{channel} {bandwidth}")
                        
                        # 步骤4.1：⚡ 并行切换双网卡信道（定向+全向同时 iw set channel）
                        dir_ok, omni_ok_this_round = adjust_channels_parallel(channel, bandwidth, logger=logger)
                        if not dir_ok:
                            logger.error(f"❌ 信道切换失败: {channel} {bandwidth}，跳过此配置")
                            continue

                        # 步骤4.2：S7 双卡并行抓包（discover_macs 不过滤 MAC，收集所有）
                        temp_mac_rssi = {}  # 定向: {mac: [rssi1, rssi2, ...]}
                        omni_mac_rssi = {}  # 全向: {mac: [rssi1, rssi2, ...]}
                        
                        def temp_packet_handler_dir(packet):
                            """定向天线临时包处理器：提取TA MAC和RSSI，顺带做 S9 Beacon 分析"""
                            if RadioTap not in packet or Dot11 not in packet:
                                return
                            _remember_packet_identity(packet, analyze_starlink=True)
                            dot11 = packet[Dot11]
                            ta_mac = dot11.addr2
                            if ta_mac:
                                ta_mac = ta_mac.lower().strip()
                                rssi = getattr(packet[RadioTap], 'dBm_AntSignal', None)
                                if ta_mac not in temp_mac_rssi:
                                    temp_mac_rssi[ta_mac] = []
                                if rssi is not None:
                                    temp_mac_rssi[ta_mac].append(rssi)
                        
                        def temp_packet_handler_omni(packet):
                            """全向天线临时包处理器：提取TA MAC和RSSI"""
                            if RadioTap not in packet or Dot11 not in packet:
                                return
                            ta_mac = packet[Dot11].addr2
                            if ta_mac:
                                ta_mac = ta_mac.lower().strip()
                                rssi = getattr(packet[RadioTap], 'dBm_AntSignal', None)
                                if ta_mac not in omni_mac_rssi:
                                    omni_mac_rssi[ta_mac] = []
                                if rssi is not None:
                                    omni_mac_rssi[ta_mac].append(rssi)
                        
                        # 步骤4.3：开始临时抓包（双卡并行）
                        logger.info(f"📶 开始抓包 {dwell_time}秒...")
                        
                        def check_stop_initial_scan():
                            return (r.get('multi_scan:stop_initial_scan') is not None or
                                    r.get('multi_scan:stop_multi_point_scan') is not None)
                        
                        # S7: 全向信道已在步骤4.1中并行切换完毕，omni_ok_this_round 已赋值
                        stop_filter_fn = lambda p: check_stop_initial_scan()
                            
                        try:
                            # 定向天线线程
                            t_dir = threading.Thread(
                                target=sniff,
                                kwargs={
                                    'iface': CAPTURE_DEFAULT_INTERFACE,
                                    'prn': temp_packet_handler_dir,
                                    'store': False,
                                    'timeout': dwell_time,
                                    'stop_filter': stop_filter_fn
                                }
                            )
                            # 全向天线线程
                            t_omni = None
                            if OMNI_ENABLED and omni_ok_this_round:
                                t_omni = threading.Thread(
                                    target=sniff,
                                    kwargs={
                                        'iface': CAPTURE_OMNI_INTERFACE,
                                        'prn': temp_packet_handler_omni,
                                        'store': False,
                                        'timeout': dwell_time,
                                        'stop_filter': stop_filter_fn
                                    }
                                )
                            
                            t_dir.start()
                            if t_omni:
                                t_omni.start()
                            t_dir.join()
                            if t_omni:
                                t_omni.join()
                            
                            if check_stop_initial_scan():
                                logger.warning(f"🛑 初始扫描被中断")
                                break
                                
                            logger.info(f"✅ 发现 {len(temp_mac_rssi)} 个MAC地址")
                            
                            # 步骤4.4：记录MAC、配置和RSSI统计（含全向数据）
                            for mac, rssi_list in temp_mac_rssi.items():
                                if mac not in discovered_macs:
                                    # 计算定向RSSI统计
                                    rssi_avg = sum(rssi_list) / len(rssi_list) if rssi_list else None
                                    
                                    mac_entry = {
                                        "channel": channel,
                                        "bandwidth": bandwidth,
                                        "rssi_avg": round(rssi_avg, 1) if rssi_avg else None,
                                        "rssi_samples": len(rssi_list)
                                    }
                                    
                                    # S7: 全向RSSI数据
                                    if OMNI_ENABLED and omni_ok_this_round and mac in omni_mac_rssi and omni_mac_rssi[mac]:
                                        omni_samples = omni_mac_rssi[mac]
                                        mac_entry["omni_rssi_avg"] = round(sum(omni_samples) / len(omni_samples), 1)
                                        mac_entry["omni_rssi_samples"] = len(omni_samples)
                                    else:
                                        mac_entry["omni_rssi_avg"] = None
                                        mac_entry["omni_rssi_samples"] = 0
                                    
                                    discovered_macs[mac] = mac_entry
                                    
                                    if rssi_avg:
                                        omni_info = ""
                                        if mac_entry.get("omni_rssi_avg") is not None:
                                            omni_info = f", 全向={mac_entry['omni_rssi_avg']}dBm({mac_entry['omni_rssi_samples']}样本)"
                                        logger.info(f"🆕 新MAC: {mac} -> 信道{channel} {bandwidth}, RSSI均值={rssi_avg:.1f}dBm ({len(rssi_list)}样本){omni_info}")
                                    else:
                                        logger.info(f"🆕 新MAC: {mac} -> 信道{channel} {bandwidth}, 无RSSI数据")
                        
                        except Exception as e:
                            logger.error(f"❌ 抓包过程异常: {e}")
                    
                    # 步骤5：设备类型打标 + 保存结果到Redis
                    _flush_starlink_to_redis(r)
                    _sl_bssids = _get_starlink_bssids()
                    for _mac, _entry in discovered_macs.items():
                        _apply_device_identity(_entry, _mac, _sl_bssids)
                    logger.info(f"💾 保存发现的MAC地址: 共{len(discovered_macs)}个")
                    r.set('multi_scan:target_macs', json.dumps(discovered_macs))

                    # 步骤6：发送完成通知
                    r.lpush('multi_scan:initial_scan_notify', json.dumps({
                        'status': 'done',
                        'mac_count': len(discovered_macs)
                    }))
                    logger.info("✅ 初始MAC发现完成")
                
                # ============= 全面扫描：点位MAC发现命令 =============
                elif action == "discover_macs_for_full_scan":
                    """
                    全面扫描中的点位MAC发现命令
                    功能：在指定点位遍历所有信道/带宽配置，收集该点位的MAC地址和RSSI

                    参数：
                        - point_id: 点位ID（如 "full_point_0"）
                        - configs: 配置列表
                        - dwell_time: 每个配置停留时间（秒）
                        - scan_id: 扫描ID（用于取消校验）
                        - stop_key: 可选，ptz_control 传入的停止 key（如 'full_scan:stop'）
                        - legacy_stop_key: 可选，旧格式停止 key（如 'multi_scan:stop_full_area_scan'）

                    与discover_macs的区别：
                        - 结果通知到 full_scan:{point_id}_notify（而不是 initial_scan_notify）
                        - 不保存到 multi_scan:target_macs
                        - 直接在通知中返回发现的MAC数据
                    """
                    point_id = command.get('point_id', 'unknown')
                    cmd_scan_id = command.get('scan_id')
                    cmd_stop_key = command.get('stop_key')
                    cmd_legacy_stop_key = command.get('legacy_stop_key')

                    # 便捷闭包：统一取消判断
                    def _fs_stopped():
                        stopped, _reason = _full_scan_capture_stopped(
                            r, cmd_scan_id,
                            stop_key=cmd_stop_key,
                            legacy_stop_key=cmd_legacy_stop_key,
                        )
                        return stopped

                    def _fs_stop_reason():
                        _stopped, _reason = _full_scan_capture_stopped(
                            r, cmd_scan_id,
                            stop_key=cmd_stop_key,
                            legacy_stop_key=cmd_legacy_stop_key,
                        )
                        return _reason if _stopped else None

                    # scan_id 严格校验：命令带 scan_id 时，必须与 Redis 中活跃 scan_id 精确匹配
                    if cmd_scan_id:
                        try:
                            active_scan_id = r.get('full_scan:active_scan_id')
                            if active_scan_id != cmd_scan_id:
                                logger.warning(f"⏭️ [{point_id}] scan_id 不匹配，跳过: cmd={cmd_scan_id} active={active_scan_id}")
                                continue
                        except Exception:
                            # Redis 异常时保守跳过，避免旧命令误执行
                            logger.warning(f"⏭️ [{point_id}] 无法读取 active_scan_id，跳过")
                            continue

                    # ★ 收到命令后立刻检查取消（避免在已停止后还做初始化）
                    if _fs_stopped():
                        _stop_reason = _fs_stop_reason() or 'manual_stop'
                        logger.warning(f"🛑 [{point_id}] 收到命令时已处于停止状态，跳过 reason={_stop_reason}")
                        r.lpush(f'full_scan:{point_id}_notify', json.dumps({
                            'status': 'stopped',
                            'reason': _stop_reason,
                            'point_id': point_id,
                            'scan_id': cmd_scan_id,
                            'mac_count': 0,
                            'macs': {},
                        }))
                        continue

                    logger.info(f"🔍 [{point_id}] 收到点位MAC发现命令")

                    # 步骤1：获取参数
                    # dwell_time 由调用方（ptz_control）传入，默认20秒
                    # 若需调整，请修改 ptz_control.py 顶部的 FULL_SCAN_DWELL_TIME 常量
                    configs = command.get('configs', [])
                    dwell_time = command.get('dwell_time', 20)

                    if not configs:
                        logger.error("❌ 缺少configs参数")
                        r.lpush(f'full_scan:{point_id}_notify', json.dumps({
                            'status': 'error',
                            'message': '缺少configs参数'
                        }))
                        continue

                    logger.info(f"📋 [{point_id}] 扫描{len(configs)}个配置，每个{dwell_time}秒")

                    # ★ 步骤2前：setup 前检查取消
                    if _fs_stopped():
                        _stop_reason = _fs_stop_reason() or 'manual_stop'
                        logger.warning(f"🛑 [{point_id}] setup 前检测到停止 reason={_stop_reason}")
                        r.lpush(f'full_scan:{point_id}_notify', json.dumps({
                            'status': 'stopped',
                            'reason': _stop_reason,
                            'point_id': point_id,
                            'scan_id': cmd_scan_id,
                            'mac_count': 0,
                            'macs': {},
                        }))
                        continue

                    # 步骤2：确保网卡在监听模式
                    if not setup_monitor_mode_once(CAPTURE_DEFAULT_INTERFACE, configs[0]['channel'], configs[0]['bandwidth']):
                        logger.error(f"❌ [{point_id}] 无法将网卡设置为监听模式")
                        r.lpush(f'full_scan:{point_id}_notify', json.dumps({
                            'status': 'error',
                            'message': '网卡监听模式设置失败'
                        }))
                        continue

                    # ★ setup 后检查取消（setup 耗时长，期间可能已停止）
                    if _fs_stopped():
                        _stop_reason = _fs_stop_reason() or 'manual_stop'
                        logger.warning(f"🛑 [{point_id}] setup 后检测到停止 reason={_stop_reason}")
                        r.lpush(f'full_scan:{point_id}_notify', json.dumps({
                            'status': 'stopped',
                            'reason': _stop_reason,
                            'point_id': point_id,
                            'scan_id': cmd_scan_id,
                            'mac_count': 0,
                            'macs': {},
                        }))
                        continue

                    # 步骤3：遍历配置，发现MAC
                    discovered_macs = {}
                    first_seen_by_mac = {}
                    last_seen_by_mac = {}
                    scan_stopped = False
                    stop_reason = None

                    for idx, config in enumerate(configs):
                        # ★ 每个配置前检查取消
                        if _fs_stopped():
                            stop_reason = _fs_stop_reason() or 'manual_stop'
                            logger.warning(f"🛑 [{point_id}] 信道{config.get('channel')}前检测到停止 reason={stop_reason}")
                            scan_stopped = True
                            break

                        channel = config['channel']
                        bandwidth = config['bandwidth']

                        logger.info(f"🔄 [{point_id}] [{idx+1}/{len(configs)}] 信道{channel} {bandwidth}")

                        # ⚡ 并行切换双网卡信道
                        dir_ok_cfg, omni_ok_this_config = adjust_channels_parallel(channel, bandwidth, logger=logger)
                        if not dir_ok_cfg:
                            logger.error(f"❌ [{point_id}] 信道切换失败，跳过此配置")
                            continue

                        # ★ 信道切换后检查取消
                        if _fs_stopped():
                            stop_reason = _fs_stop_reason() or 'manual_stop'
                            logger.warning(f"🛑 [{point_id}] 信道{channel}切换后检测到停止 reason={stop_reason}")
                            scan_stopped = True
                            break

                        # 抓包（S7: 双卡并行，使用切片 sniff 支持轮询停止）
                        logger.info(f"📶 [{point_id}] 开始抓包 {dwell_time}秒...")

                        try:
                            # ★ 使用 _sniff_collect_with_stop 替代原始 sniff
                            # 定向天线抓包
                            t_dir_result = [None]
                            t_omni_result = [None]

                            def sniff_dir():
                                t_dir_result[0] = _sniff_collect_with_stop(
                                    iface=CAPTURE_DEFAULT_INTERFACE,
                                    dwell_time=dwell_time,
                                    stop_check_fn=_fs_stopped,
                                    poll_interval=0.1,
                                )

                            def sniff_omni():
                                for _attempt in range(3):
                                    try:
                                        t_omni_result[0] = _sniff_collect_with_stop(
                                            iface=CAPTURE_OMNI_INTERFACE,
                                            dwell_time=dwell_time,
                                            stop_check_fn=_fs_stopped,
                                            poll_interval=0.1,
                                        )
                                        return
                                    except OSError as e:
                                        if _attempt < 2:
                                            logger.warning(f"⚠️ [全向] sniff 启动失败({e})，0.5秒后重试...")
                                            time.sleep(0.5)
                                        else:
                                            logger.warning(f"⚠️ [全向] sniff 重试3次仍失败: {e}")

                            t_dir = threading.Thread(target=sniff_dir)
                            t_omni = None
                            if OMNI_ENABLED and omni_ok_this_config:
                                t_omni = threading.Thread(target=sniff_omni)

                            t_dir.start()
                            if t_omni:
                                t_omni.start()
                            t_dir.join()
                            if t_omni:
                                t_omni.join()

                            packets_dir = t_dir_result[0] or []
                            packets_omni = t_omni_result[0] or []

                            # ★ sniff 后检查取消
                            if _fs_stopped():
                                stop_reason = _fs_stop_reason() or 'manual_stop'
                                logger.warning(f"🛑 [{point_id}] 扫描被中断 reason={stop_reason}")
                                scan_stopped = True
                                break

                            # 解析定向天线数据包（顺带做 S9 Beacon 分析）
                            rssi_by_mac = {}
                            rssi_max_by_mac = {}   # {mac: 本信道内单包最大 RSSI}
                            beacon_rssi_by_bssid = {}
                            beacon_first_seen = {}
                            beacon_last_seen = {}
                            for pkt in packets_dir:
                                if pkt.haslayer(RadioTap) and pkt.haslayer(Dot11):
                                    _remember_packet_identity(
                                        pkt,
                                        analyze_starlink=True,
                                        full_scan_id=cmd_scan_id,
                                        capture_channel=channel,
                                        capture_bandwidth=bandwidth,
                                    )
                                    dot11 = pkt[Dot11]
                                    ta_mac = dot11.addr2
                                    if ta_mac and ta_mac.lower() != 'ff:ff:ff:ff:ff:ff':
                                        rssi = pkt[RadioTap].dBm_AntSignal if hasattr(pkt[RadioTap], 'dBm_AntSignal') else None
                                        if rssi is not None:
                                            packet_ts = _packet_time_iso(pkt)
                                            first_seen_by_mac.setdefault(ta_mac, packet_ts)
                                            last_seen_by_mac[ta_mac] = packet_ts
                                            if ta_mac not in rssi_by_mac:
                                                rssi_by_mac[ta_mac] = []
                                            rssi_by_mac[ta_mac].append(rssi)
                                            if ta_mac not in rssi_max_by_mac or rssi > rssi_max_by_mac[ta_mac]:
                                                rssi_max_by_mac[ta_mac] = rssi
                                            if dot11.type == 0 and dot11.subtype == 8:
                                                bssid = (dot11.addr3 or dot11.addr2 or "").lower()
                                                if bssid:
                                                    beacon_rssi_by_bssid.setdefault(bssid, []).append(rssi)
                                                    beacon_first_seen.setdefault(bssid, packet_ts)
                                                    beacon_last_seen[bssid] = packet_ts

                            # S7: 解析全向天线数据包
                            omni_rssi_by_mac = {}
                            if OMNI_ENABLED and omni_ok_this_config:
                                for pkt in packets_omni:
                                    if pkt.haslayer(RadioTap) and pkt.haslayer(Dot11):
                                        ta_mac = pkt[Dot11].addr2
                                        if ta_mac and ta_mac.lower() != 'ff:ff:ff:ff:ff:ff':
                                            rssi = pkt[RadioTap].dBm_AntSignal if hasattr(pkt[RadioTap], 'dBm_AntSignal') else None
                                            if rssi:
                                                if ta_mac not in omni_rssi_by_mac:
                                                    omni_rssi_by_mac[ta_mac] = []
                                                omni_rssi_by_mac[ta_mac].append(rssi)

                            # 计算RSSI并保存（含全向数据）- AP信道过滤 + 统一取max
                            for mac, rssi_list in rssi_by_mac.items():
                                bssid_lower = mac.lower()

                                # ── 判断是否为有宣告信道的 AP ──────────────────────────────
                                is_ap_with_declared_ch = (
                                    bssid_lower in _global_ap_macs and
                                    _global_ap_channels.get(bssid_lower) is not None
                                )
                                is_ap_without_declared_ch = (
                                    bssid_lower in _global_ap_macs and
                                    not is_ap_with_declared_ch
                                )

                                # ── AP 信道过滤（只处理有宣告信道的 AP）─────────────────────
                                if is_ap_with_declared_ch:
                                    declared_ch = _global_ap_channels[bssid_lower]
                                    if int(channel) != declared_ch:
                                        # 当前跳频信道号不等于 Beacon 宣告信道号 → 丢弃
                                        logger.debug(
                                            f"[{point_id}] AP {mac} 跳过非工作信道 {channel}"
                                            f"（Beacon 宣告信道={declared_ch}）"
                                        )
                                        continue

                                # ── 确定最终 channel / bandwidth ─────────────────────────
                                if is_ap_with_declared_ch:
                                    # AP：channel 使用 Beacon 宣告主信道；bandwidth 始终表示扫描带宽。
                                    final_channel = _global_ap_channels[bssid_lower]
                                    final_bandwidth = bandwidth
                                else:
                                    # Client / 未识别：使用本次跳频的 channel / bandwidth
                                    final_channel = channel
                                    final_bandwidth = bandwidth

                                # ── 计算本信道的代表 RSSI（AP 和 Client 统一取 max）─────────────────
                                # AP：只有通过信道过滤的包才到达这里，取该宣告信道内单包最大值
                                # Client / 未识别：取该跳频信道内单包最大值
                                # 兜底：rssi_max_by_mac 为空时退化为均值（理论上不会，但防御性处理）
                                if is_ap_without_declared_ch:
                                    beacon_samples = beacon_rssi_by_bssid.get(bssid_lower) or []
                                    if not beacon_samples:
                                        continue
                                    rssi_list = beacon_samples
                                    representative_rssi = max(beacon_samples)
                                    first_seen_at = beacon_first_seen.get(bssid_lower)
                                    last_seen_at = beacon_last_seen.get(bssid_lower)
                                else:
                                    representative_rssi = rssi_max_by_mac.get(mac) or round(sum(rssi_list) / len(rssi_list), 2)
                                    first_seen_at = first_seen_by_mac.get(mac)
                                    last_seen_at = last_seen_by_mac.get(mac)

                                # ── 构建 mac_entry ─────────────────────────────────────────
                                # rssi_avg 字段语义：统一存该信道单包最大值，AP 和 Client 一致（见文档说明）
                                mac_entry = {
                                    'channel': final_channel,
                                    'bandwidth': final_bandwidth,
                                    'rssi_avg': representative_rssi,
                                    'rssi_samples': len(rssi_list),
                                    'first_seen_at': first_seen_at,
                                    'last_seen_at': last_seen_at,
                                    'config_order_index': idx,
                                }
                                if is_ap_with_declared_ch:
                                    mac_entry['channel_source'] = (
                                        _global_ap_channel_sources.get(bssid_lower) or 'declared'
                                    )
                                    mac_entry['channel_confidence'] = 'declared'
                                    mac_entry['declared_bandwidth'] = (
                                        _global_ap_bandwidths.get(bssid_lower)
                                    )
                                elif is_ap_without_declared_ch:
                                    mac_entry['channel_source'] = 'beacon_capture_config'
                                    mac_entry['channel_confidence'] = 'inferred'
                                    mac_entry['declared_bandwidth'] = (
                                        _global_ap_bandwidths.get(bssid_lower)
                                    )
                                    mac_entry['observed_best_config'] = {
                                        'channel': int(channel),
                                        'bandwidth': str(bandwidth),
                                    }
                                    _remember_full_scan_inferred_ap_config(
                                        cmd_scan_id,
                                        bssid_lower,
                                        mac_entry,
                                    )

                                # S7: 全向RSSI
                                if OMNI_ENABLED and omni_ok_this_config and mac in omni_rssi_by_mac and omni_rssi_by_mac[mac]:
                                    omni_samples = omni_rssi_by_mac[mac]
                                    mac_entry['omni_rssi_avg'] = round(sum(omni_samples) / len(omni_samples), 2)
                                    mac_entry['omni_rssi_samples'] = len(omni_samples)
                                else:
                                    mac_entry['omni_rssi_avg'] = None
                                    mac_entry['omni_rssi_samples'] = 0

                                mac_entry['capture_config'] = {
                                    'channel': int(channel),
                                    'bandwidth': str(bandwidth),
                                }
                                mac_entry['observed_configs'] = [
                                    _full_scan_capture_observation(
                                        mac_entry,
                                        channel,
                                        bandwidth,
                                    )
                                ]

                                # ── 与已有记录合并 ────────────────────────────────────────
                                if mac in discovered_macs:
                                    existing = discovered_macs[mac]
                                    merged_observations = (
                                        _merge_full_scan_capture_observations(
                                            existing,
                                            mac_entry,
                                        )
                                    )
                                    # 保留最早 first_seen / 最晚 last_seen
                                    existing['first_seen_at'] = (
                                        existing.get('first_seen_at') or first_seen_by_mac.get(mac)
                                    )
                                    existing['last_seen_at'] = (
                                        last_seen_by_mac.get(mac) or existing.get('last_seen_at')
                                    )
                                    if _full_scan_should_replace_observation(existing, mac_entry):
                                        mac_entry['first_seen_at'] = (
                                            existing.get('first_seen_at') or mac_entry.get('first_seen_at')
                                        )
                                        mac_entry['observed_configs'] = merged_observations
                                        discovered_macs[mac] = mac_entry
                                        logger.info(
                                            f"[{point_id}] 🔄 更新 {mac} -> ch{final_channel} {final_bandwidth},"
                                            f" rssi_avg={representative_rssi}"
                                        )
                                    else:
                                        existing['observed_configs'] = merged_observations
                                else:
                                    discovered_macs[mac] = mac_entry
                                    omni_info = ""
                                    if mac_entry.get('omni_rssi_avg') is not None:
                                        omni_info = f", 全向={mac_entry['omni_rssi_avg']}dBm({mac_entry['omni_rssi_samples']}样本)"
                                    logger.info(
                                        f"[{point_id}] 🆕 新 MAC {mac} -> ch{final_channel} {final_bandwidth},"
                                        f" rssi_avg={representative_rssi}{omni_info}"
                                    )

                        except Exception as e:
                            logger.error(f"❌ [{point_id}] 抓包异常: {e}")

                    # 步骤4：任务级身份和 Data 关系回填，再发送通知。
                    _flush_starlink_to_redis(r)
                    _sl_bssids = _get_starlink_bssids()
                    for _mac, _entry in discovered_macs.items():
                        _apply_device_identity(_entry, _mac, _sl_bssids)
                        _apply_full_scan_client_relationship(
                            _entry,
                            _mac,
                            cmd_scan_id,
                        )
                    _relationship_snapshot = _build_full_scan_relationship_snapshot(
                        cmd_scan_id
                    )

                    # 发送通知（区分正常完成和被取消）
                    if scan_stopped:
                        # ★ 被取消时发送 stopped 通知，让 ptz_worker 立即感知
                        logger.warning(f"🛑 [{point_id}] 点位扫描被取消 reason={stop_reason}，已发现{len(discovered_macs)}个MAC")
                        _notify_payload = {
                            'status': 'stopped',
                            'reason': stop_reason or 'manual_stop',
                            'point_id': point_id,
                            'mac_count': len(discovered_macs),
                            'macs': discovered_macs,
                            'global_ap_macs': list(_global_ap_macs),
                            'global_client_macs': list(_global_client_macs),
                            'global_ap_ssids': _global_ap_ssids,
                            'starlink_bssids': list(_sl_bssids),
                            'full_scan_relationships': _relationship_snapshot,
                        }
                        if cmd_scan_id:
                            _notify_payload['scan_id'] = cmd_scan_id
                        r.lpush(f'full_scan:{point_id}_notify', json.dumps(_notify_payload))
                    else:
                        # 正常完成：设备类型打标 + 发送完成通知（带MAC数据）
                        logger.info(f"✅ [{point_id}] 点位扫描完成，发现{len(discovered_macs)}个MAC")
                        _notify_payload = {
                            'status': 'done',
                            'point_id': point_id,
                            'mac_count': len(discovered_macs),
                            'macs': discovered_macs,
                            'global_ap_macs': list(_global_ap_macs),
                            'global_client_macs': list(_global_client_macs),
                            'global_ap_ssids': _global_ap_ssids,
                            'starlink_bssids': list(_sl_bssids),
                            'full_scan_relationships': _relationship_snapshot,
                        }
                        if cmd_scan_id:
                            _notify_payload['scan_id'] = cmd_scan_id
                        r.lpush(f'full_scan:{point_id}_notify', json.dumps(_notify_payload))
                
                # ============= 定位扫描：信道探测命令 =============
                elif action == "detect_channels":
                    """
                    信道探测命令（定位扫描专用）
                    功能：在当前点位轮询所有信道，找出每个目标 MAC 信号最强的信道。
                    云台保持不动，由 ptz_control 在发送本命令前保证云台已停稳。

                    参数：
                        - target_macs : 目标 MAC 列表，如 ["aa:bb:cc:dd:ee:ff", ...]
                        - configs     : 信道配置列表（默认使用 INITIAL_SCAN_CONFIGS）
                        - total_duration : 探测总时长（秒），平均分配到各信道

                    结果通知：notify_key，默认 detect_channels:notify
                        {
                            "status": "done",
                            "results": {
                                "aa:bb:cc:dd:ee:ff": {
                                    "channel": 6, "bandwidth": "HT20",
                                    "rssi_avg": -55.0, "sample_count": 12
                                },
                                "bb:cc:dd:ee:ff:00": "not_found"
                            }
                        }
                    """
                    logger.info("🔍 [定位] 收到信道探测命令")

                    dc_target_macs = [m.lower().strip()
                                      for m in command.get('target_macs', [])]
                    dc_configs     = command.get('configs', [])
                    dc_total_dur   = float(command.get('total_duration', 60))
                    dc_notify_key  = command.get('notify_key') or 'detect_channels:notify'
                    dc_stop_key    = command.get('stop_key')
                    dc_scan_id     = command.get('scan_id')
                    logger.info(
                        f"🔖 [定位] 信道探测命令标识: "
                        f"scan_id={dc_scan_id}, notify_key={dc_notify_key}, stop_key={dc_stop_key}"
                    )

                    active_scan_id = r.get('location_scan:active_scan_id')
                    if dc_scan_id:
                        if active_scan_id != dc_scan_id:
                            logger.warning(
                                f"🧹 [定位] 跳过过期信道探测命令: "
                                f"scan_id={dc_scan_id}, active={active_scan_id}"
                            )
                            continue
                    elif active_scan_id:
                        logger.warning(
                            f"🧹 [定位] 跳过无 scan_id 的旧信道探测命令，active={active_scan_id}"
                        )
                        continue

                    if dc_stop_key and r.get(dc_stop_key) is not None:
                        logger.warning(f"🛑 [定位] 信道探测命令已取消，跳过: {dc_stop_key}")
                        continue

                    if not dc_target_macs:
                        logger.error("❌ [定位] detect_channels 缺少 target_macs")
                        r.lpush(dc_notify_key, json.dumps({
                            'status': 'error', 'message': 'target_macs 为空'
                        }))
                        continue

                    if not dc_configs:
                        logger.error("❌ [定位] detect_channels 缺少 configs")
                        r.lpush(dc_notify_key, json.dumps({
                            'status': 'error', 'message': 'configs 为空'
                        }))
                        continue

                    # 每个信道分配的停留时长（至少 1s）
                    dc_dwell = max(1.0, dc_total_dur / len(dc_configs))
                    logger.info(f"📡 [定位] 探测 {len(dc_target_macs)} 个 MAC，"
                                f"{len(dc_configs)} 个信道配置，"
                                f"每信道 {dc_dwell:.1f}s，总计 ~{dc_total_dur}s")

                    # ★ 统一取消判断（使用 _location_scan_capture_stopped）
                    def _dc_stop_check():
                        stopped, _reason = _location_scan_capture_stopped(
                            r, dc_scan_id, stop_key=dc_stop_key
                        )
                        return stopped

                    # ★ setup 前检查取消（避免在已停止后还做监听模式初始化）
                    if _dc_stop_check():
                        logger.warning("🛑 [定位] 命令到达时已处于停止状态，跳过")
                        r.lpush(dc_notify_key, json.dumps({
                            'status': 'stopped', 'reason': 'manual_stop'
                        }))
                        continue

                    # 初次设置监听模式
                    first_ch  = dc_configs[0].get('channel', 1)
                    first_bw  = dc_configs[0].get('bandwidth', 'HT20')
                    if not setup_monitor_mode_once(CAPTURE_DEFAULT_INTERFACE, first_ch, first_bw):
                        logger.error("❌ [定位] 无法设置监听模式，中止信道探测")
                        r.lpush(dc_notify_key, json.dumps({
                            'status': 'error', 'message': '无法设置监听模式'
                        }))
                        continue

                    # ★ setup 后检查取消（setup 耗时长，期间可能已停止）
                    if _dc_stop_check():
                        logger.warning("🛑 [定位] setup 后检测到停止，中止信道探测")
                        r.lpush(dc_notify_key, json.dumps({
                            'status': 'stopped', 'reason': 'manual_stop'
                        }))
                        continue

                    # 结构: {mac: {(ch,bw): [rssi, ...]}}
                    dc_rssi_map = {mac: {} for mac in dc_target_macs}
                    dc_target_set = set(dc_target_macs)

                    dc_stopped_by_user = False
                    for dc_idx, dc_cfg in enumerate(dc_configs):
                        if _dc_stop_check():
                            logger.warning("🛑 [定位] 信道探测被停止")
                            dc_stopped_by_user = True
                            break

                        dc_ch = dc_cfg.get('channel')
                        dc_bw = dc_cfg.get('bandwidth', 'HT20')

                        # 信道探测只用定向网卡，全向不参与（避免 mt7612u HT40- 固件崩溃）
                        if _dc_stop_check():
                            logger.warning("🛑 [定位] 信道切换前检测到停止")
                            dc_stopped_by_user = True
                            break
                        dc_dir_ok = adjust_channel_with_retry(CAPTURE_DEFAULT_INTERFACE, dc_ch, dc_bw, logger=logger)
                        if _dc_stop_check():
                            logger.warning("🛑 [定位] 信道切换后检测到停止")
                            dc_stopped_by_user = True
                            break
                        if not dc_dir_ok:
                            logger.warning(f"⚠️ [定位] 信道切换失败 ch{dc_ch}，跳过")
                            continue

                        # 定向天线嗅探
                        dc_temp_rssi = {}  # {mac: [rssi, ...]}

                        def _dc_dir_handler(pkt, _rssi_dict=dc_temp_rssi,
                                            _macs=dc_target_set):
                            if RadioTap not in pkt or Dot11 not in pkt:
                                return
                            _remember_packet_identity(pkt, analyze_starlink=True)
                            ta = pkt[Dot11].addr2
                            if not ta:
                                return
                            ta = ta.lower().strip()
                            if ta not in _macs:
                                return
                            rssi = getattr(pkt[RadioTap], 'dBm_AntSignal', None)
                            if rssi is not None:
                                _rssi_dict.setdefault(ta, []).append(rssi)

                        try:
                            _sniff_with_stop_poll(
                                iface=CAPTURE_DEFAULT_INTERFACE,
                                prn=_dc_dir_handler,
                                store=False,
                                total_timeout=dc_dwell,
                                stop_check_fn=_dc_stop_check,
                            )
                        except Exception as e:
                            logger.warning(f"⚠️ [定位] ch{dc_ch} 嗅探异常: {e}")

                        if _dc_stop_check():
                            logger.warning("🛑 [定位] 信道探测嗅探期间被停止")
                            dc_stopped_by_user = True
                            break

                        # 汇总本信道的 RSSI
                        for mac, samples in dc_temp_rssi.items():
                            if mac in dc_rssi_map:
                                dc_rssi_map[mac][(dc_ch, dc_bw)] = \
                                    dc_rssi_map[mac].get((dc_ch, dc_bw), []) + samples

                        found_so_far = sum(1 for mac in dc_target_macs
                                           if any(dc_rssi_map[mac].values()))
                        logger.info(f"📶 [定位] [{dc_idx+1}/{len(dc_configs)}] "
                                    f"ch{dc_ch} 完成，已找到 {found_so_far}/{len(dc_target_macs)} 个 MAC")

                    def _dc_stats(samples):
                        if not samples:
                            return None
                        return {
                            "rssi_peak": round(max(samples), 2),
                            "rssi_avg": round(sum(samples) / len(samples), 2),
                            "sample_count": len(samples),
                        }

                    def _dc_best_by_peak(ch_map):
                        best = None
                        for key, samples in ch_map.items():
                            stats = _dc_stats(samples)
                            if not stats:
                                continue
                            candidate = {
                                "key": key,
                                **stats,
                            }
                            # Client/未知设备：物理判据取单包峰值 RSSI；包数只作为次级判断。
                            score = (candidate["rssi_peak"], candidate["sample_count"])
                            if best is None or score > best["score"]:
                                best = {**candidate, "score": score}
                        return best

                    def _dc_best_for_ap(mac, ch_map):
                        declared_ch = _global_ap_channels.get(mac)
                        if declared_ch is None:
                            return None
                        declared_bw = _global_ap_bandwidths.get(mac)
                        candidates = []
                        for (ch, bw), samples in ch_map.items():
                            try:
                                if int(ch) != int(declared_ch):
                                    continue
                            except (TypeError, ValueError):
                                continue
                            stats = _dc_stats(samples)
                            if not stats:
                                continue
                            candidates.append({
                                "key": (ch, bw),
                                "declared_bw_match": declared_bw is not None and str(bw) == str(declared_bw),
                                **stats,
                            })
                        if not candidates:
                            return None
                        candidates.sort(
                            key=lambda item: (
                                item["declared_bw_match"],
                                item["rssi_peak"],
                                item["sample_count"],
                            ),
                            reverse=True,
                        )
                        best = candidates[0]
                        return {
                            **best,
                            "channel": int(declared_ch),
                            "bandwidth": declared_bw or best["key"][1],
                        }

                    # 汇总结果：
                    # - AP/BSSID 且抓到 Beacon：以 Beacon 宣告信道/带宽为准。
                    # - Client/未抓到 Beacon：完整遍历后取单包峰值 RSSI 最高的配置，包数次级。
                    dc_results = {}
                    for mac in dc_target_macs:
                        ch_map = dc_rssi_map.get(mac, {})
                        if not ch_map:
                            dc_results[mac] = "not_found"
                            logger.warning(f"⚠️ [定位] MAC {mac} 未找到")
                            continue

                        ap_choice = _dc_best_for_ap(mac, ch_map)
                        if ap_choice:
                            dc_results[mac] = {
                                "channel": ap_choice["channel"],
                                "bandwidth": ap_choice["bandwidth"],
                                "rssi_peak": ap_choice["rssi_peak"],
                                "rssi_avg": ap_choice["rssi_avg"],
                                "sample_count": ap_choice["sample_count"],
                                "source": "beacon",
                            }
                            logger.info(
                                f"✅ [定位] AP {mac} → ch{dc_results[mac]['channel']}/"
                                f"{dc_results[mac]['bandwidth']} source=beacon "
                                f"rssi_peak={dc_results[mac]['rssi_peak']} dBm "
                                f"({dc_results[mac]['sample_count']} 样本)"
                            )
                            continue

                        best = _dc_best_by_peak(ch_map)
                        if not best:
                            dc_results[mac] = "not_found"
                            logger.warning(f"⚠️ [定位] MAC {mac} 无有效 RSSI 样本")
                            continue
                        best_ch, best_bw = best["key"]
                        dc_results[mac] = {
                            "channel": best_ch,
                            "bandwidth": best_bw,
                            "rssi_peak": best["rssi_peak"],
                            "rssi_avg": best["rssi_avg"],
                            "sample_count": best["sample_count"],
                            "source": "rssi_peak",
                        }
                        logger.info(
                            f"✅ [定位] MAC {mac} → ch{best_ch}/{best_bw} "
                            f"source=rssi_peak rssi_peak={best['rssi_peak']} dBm "
                            f"rssi_avg={best['rssi_avg']} dBm ({best['sample_count']} 样本)"
                        )

                    r.lpush(dc_notify_key, json.dumps({
                        'status': 'stopped' if dc_stopped_by_user else 'done',
                        'results': dc_results,
                    }))
                    logger.info(
                        f"{'🛑 [定位] 信道探测已停止' if dc_stopped_by_user else '✅ [定位] 信道探测完成'}，结果已通知"
                    )

                # ============= 新增：点位扫描命令 =============
                elif action == "scan_at_point":
                    """
                    点位扫描命令
                    功能：在指定点位，针对特定MAC地址和配置进行RSSI采集
                    
                    参数：
                        - point_id: 点位ID（如 "point_1"）
                        - pan: 云台水平角度
                        - tilt: 云台垂直角度
                        - configs: 配置列表，格式 [{"channel": 1, "bandwidth": "HT20", "target_mac": "xx:xx:xx"}]
                        - dwell_time: 每个配置停留时间（秒），默认5秒
                        - extend_time: 无信号延长时间（秒），默认3秒
                    
                    工作流程：
                        1. 遍历每个配置（信道/带宽/目标MAC）
                        2. 切换到目标配置
                        3. 临时抓包dwell_time秒
                        4. 如果无信号，延长extend_time秒
                        5. 计算RSSI均值和样本数
                        6. 存储到Redis: multi_scan:{point_id}
                        7. 通知完成: multi_scan:{point_id}_notify
                    """
                    logger.info("📍 收到点位扫描命令")

                    # 步骤1：获取参数
                    point_id = command.get('point_id')
                    cmd_scan_id = command.get('scan_id')
                    # scan_id 严格校验：命令带 scan_id 时，必须与 Redis 中活跃 scan_id 精确匹配
                    if cmd_scan_id:
                        try:
                            active_scan_id = r.get('location_scan:active_scan_id')
                            if active_scan_id is None or active_scan_id.startswith('stopping:') or active_scan_id != cmd_scan_id:
                                logger.warning(f"⏭️ [{point_id}] scan_id 不匹配，跳过: cmd={cmd_scan_id} active={active_scan_id}")
                                continue
                        except Exception:
                            logger.warning(f"⏭️ [{point_id}] 无法读取 active_scan_id，跳过")
                            continue
                    pan = command.get('pan')
                    tilt = command.get('tilt')
                    configs = command.get('configs', [])
                    dwell_time = command.get('dwell_time', 5)
                    extend_time = command.get('extend_time', 3)
                    stop_key = command.get('stop_key') or 'multi_scan:stop_multi_point_scan'
                    
                    # 参数验证
                    if point_id is None:
                        logger.error("❌ scan_at_point命令缺少point_id参数")
                        continue
                    
                    if not configs:
                        logger.error("❌ scan_at_point命令缺少configs参数")
                        r.lpush(f'multi_scan:{point_id}_notify', json.dumps({
                            'status': 'error',
                            'message': '缺少configs参数'
                        }))
                        continue
                    
                    logger.info(f"🎯 点位扫描: point_id={point_id}, pan={pan}, tilt={tilt}, {len(configs)}个配置")
                    
                    # ⚡ 关键修复：停掉后台 Scapy sniffer（如果正在运行）
                    #    后台 sniffer 会持有网卡的 raw socket，导致 iw dev set channel 时 EBUSY (-16)
                    #    scan_at_point 内部使用的是内联 sniff()，不依赖后台线程，可以安全停止
                    stop_capture()
                    logger.info("⏹️ 已停止后台抓包线程（为信道切换让路）")
                    
                    # 步骤2：初始化点位数据结构
                    point_data = {
                        "point_id": point_id,
                        "pan": pan,
                        "tilt": tilt,
                        "scan_results": []  # 每个配置的扫描结果
                    }
                    
                    # 步骤3：遍历每个配置组（支持多MAC合并扫描）
                    stopped_by_user = False
                    def _scan_point_stopped():
                        stopped, _reason = _location_scan_capture_stopped(
                            r, cmd_scan_id, stop_key=stop_key
                        )
                        return stopped

                    for idx, config in enumerate(configs):
                        # 🛑 检查停止标志
                        if _scan_point_stopped():
                            logger.warning("🛑 检测到停止标志，终止点位扫描")
                            stopped_by_user = True
                            break
                        
                        channel = config.get('channel')
                        bandwidth = config.get('bandwidth')
                        # 🔥 支持新格式：target_macs (多个) 或 target_mac (单个)
                        target_macs = config.get('target_macs', [])
                        if not target_macs:  # 兼容旧格式
                            single_mac = config.get('target_mac', '').lower().strip()
                            if single_mac:
                                target_macs = [single_mac]
                        else:
                            target_macs = [mac.lower().strip() for mac in target_macs]
                        
                        # 参数验证
                        if not channel or not bandwidth:
                            logger.warning(f"⚠️ 配置{idx}缺少channel或bandwidth，跳过")
                            continue
                        
                        if not target_macs:
                            logger.warning(f"⚠️ 配置{idx}缺少target_mac(s)，跳过")
                            continue
                        
                        logger.info(f"🔄 [{idx+1}/{len(configs)}] 扫描: {len(target_macs)}个MAC, 信道{channel} {bandwidth}")
                        logger.info(f"   目标MAC: {', '.join(target_macs[:3])}{'...' if len(target_macs) > 3 else ''}")
                        
                        # 步骤3.1：⚡ 并行切换双网卡信道
                        if _scan_point_stopped():
                            logger.warning("🛑 信道切换前检测到停止标志")
                            stopped_by_user = True
                            break
                        dir_ok_s, _ = adjust_channels_parallel(channel, bandwidth, logger=logger)
                        if _scan_point_stopped():
                            logger.warning("🛑 信道切换后检测到停止标志")
                            stopped_by_user = True
                            break
                        if not dir_ok_s:
                            logger.error(f"❌ 信道切换失败: {channel} {bandwidth}，跳过此配置")
                            # 记录失败结果
                            for target_mac in target_macs:
                                fail_result = {
                                    "channel": channel,
                                    "bandwidth": bandwidth,
                                    "target_mac": target_mac,
                                    "rssi_avg": None,
                                    "sample_count": 0,
                                    "status": "channel_switch_failed"
                                }
                                merge_omni_to_result(fail_result, target_mac, {}, False)
                                point_data["scan_results"].append(fail_result)
                            continue
                        
                        # 步骤3.2：双卡并行抓包（S7 改造）
                        target_macs_set = set(target_macs)
                        total_dwell = dwell_time  # 固定停留时间，不随设备数叠加
                        logger.info(f"📶 开始抓包 {total_dwell}秒 (固定时长, {len(target_macs)}个MAC并行采集)...")
                        
                        def check_stop_multi_point_scan():
                            return _scan_point_stopped()
                            
                        try:
                            # S7: 使用 dual_sniff 同时在定向和全向天线上抓包
                            rssi_data, omni_rssi, omni_ok = dual_sniff(
                                interface_dir=CAPTURE_DEFAULT_INTERFACE,
                                target_macs_set=target_macs_set,
                                channel=channel,
                                bandwidth=bandwidth,
                                dwell_time=total_dwell,
                                stop_check_fn=check_stop_multi_point_scan
                            )
                            
                            if _scan_point_stopped():
                                logger.warning(f"🛑 点位扫描被中断")
                                stopped_by_user = True
                                break
                                
                            # 步骤3.3：检查哪些MAC收到数据，哪些没收到（定向天线）
                            missing_macs = [mac for mac in target_macs if mac not in rssi_data]
                            if missing_macs:
                                logger.warning(f"⚠️ {len(missing_macs)}个MAC无数据，延长 {extend_time}秒...")
                                logger.info(f"   缺失MAC: {', '.join(missing_macs[:3])}{'...' if len(missing_macs) > 3 else ''}")
                                # 延长抓包也用双卡
                                ext_dir, ext_omni, ext_omni_ok = dual_sniff(
                                    interface_dir=CAPTURE_DEFAULT_INTERFACE,
                                    target_macs_set=target_macs_set,
                                    channel=channel,
                                    bandwidth=bandwidth,
                                    dwell_time=extend_time,
                                    stop_check_fn=check_stop_multi_point_scan
                                )
                                if _scan_point_stopped():
                                    logger.warning(f"🛑 点位扫描延长抓包期间被中断")
                                    stopped_by_user = True
                                    break
                                # 合并延长数据到主数据
                                for mac, samples in ext_dir.items():
                                    rssi_data.setdefault(mac, []).extend(samples)
                                for mac, samples in ext_omni.items():
                                    omni_rssi.setdefault(mac, []).extend(samples)
                                if ext_omni_ok:
                                    omni_ok = True  # 延长阶段全向成功也算成功
                            
                            # 步骤3.4：为每个MAC计算RSSI并记录结果（含全向数据）
                            for target_mac in target_macs:
                                if target_mac in rssi_data:
                                    samples = rssi_data[target_mac]
                                    rssi_avg = round(sum(samples) / len(samples), 2)
                                    sample_count = len(samples)
                                    logger.info(f"✅ {target_mac}: RSSI={rssi_avg}dBm ({sample_count}样本)")
                                    
                                    result_entry = {
                                        "channel": channel,
                                        "bandwidth": bandwidth,
                                        "target_mac": target_mac,
                                        "rssi_avg": rssi_avg,
                                        "sample_count": sample_count,
                                        "status": "success"
                                    }
                                else:
                                    logger.warning(f"⚠️ {target_mac}: 无数据")
                                    result_entry = {
                                        "channel": channel,
                                        "bandwidth": bandwidth,
                                        "target_mac": target_mac,
                                        "rssi_avg": None,
                                        "sample_count": 0,
                                        "status": "no_signal"
                                    }
                                
                                # S7: 合并全向天线数据
                                merge_omni_to_result(result_entry, target_mac, omni_rssi, omni_ok)
                                
                                # 日志：全向数据
                                if OMNI_ENABLED and omni_ok:
                                    omni_avg = result_entry.get("omni_rssi_avg")
                                    omni_cnt = result_entry.get("omni_sample_count", 0)
                                    if omni_avg is not None:
                                        logger.info(f"   📡 [全向] {target_mac}: RSSI={omni_avg}dBm ({omni_cnt}样本)")
                                    else:
                                        logger.info(f"   📡 [全向] {target_mac}: 无信号")
                                
                                point_data["scan_results"].append(result_entry)
                        
                        except Exception as e:
                            logger.error(f"❌ 抓包过程异常: {e}")
                            # 为所有MAC记录错误
                            for target_mac in target_macs:
                                point_data["scan_results"].append({
                                    "channel": channel,
                                    "bandwidth": bandwidth,
                                    "target_mac": target_mac,
                                    "rssi_avg": None,
                                    "sample_count": 0,
                                    "status": "error",
                                    "error": str(e)
                                })
                    
                    # 步骤4：设备类型打标 + 保存点位数据到Redis
                    _flush_starlink_to_redis(r)
                    _sl_bssids = _get_starlink_bssids()
                    for _entry in point_data["scan_results"]:
                        _apply_device_identity(
                            _entry,
                            _entry.get("target_mac", ""),
                            _sl_bssids,
                            default_type='client'
                        )
                    logger.info(f"💾 保存点位数据: {point_id}")
                    r.set(f'multi_scan:{point_id}', json.dumps(point_data))

                    # 步骤5：发送完成通知（🔥 修复：不要再加 point_ 前缀）
                    r.lpush(f'multi_scan:{point_id}_notify', json.dumps({
                        'status': 'stopped' if stopped_by_user else 'done',
                        'point_id': point_id
                    }))
                    logger.info(
                        f"{'🛑 点位扫描已停止' if stopped_by_user else '✅ 点位扫描完成'}: {point_id}"
                    )
                
                # ============= S8: 客户端扫描点位命令 =============
                elif action == "scan_clients_at_point":
                    """
                    S8 客户端扫描：在指定点位逐信道跳扫，采集客户端设备（STA）发出的帧。

                    只记录 TA（发射地址 addr2）= 客户端 MAC 的帧：
                      - Probe Request (type=0, subtype=4)
                      - Association Request (type=0, subtype=0)
                      - Reassociation Request (type=0, subtype=2)
                      - Data 帧 (type=2, FCfield To DS=1 且 From DS=0)

                    参数：
                        - point_id   : 点位标识（如 "cs_point_0"）
                        - configs    : 信道配置列表 [{"channel": 6, "bandwidth": "HT20"}, ...]
                        - dwell_time : 每个信道配置的采集时长（秒）
                        - in_target  : bool，本点是否在用户选区内（用于 confidence 计算）

                    结果通过 Redis notify key 回传给 ptz_control：
                        client_scan:{point_id}_notify
                    """
                    point_id   = command.get('point_id', 'cs_point_unknown')
                    cs_configs = command.get('configs', [])
                    dwell_time = command.get('dwell_time', 3)
                    in_target  = command.get('in_target', True)

                    if not cs_configs:
                        logger.error(f"❌ [S8] [{point_id}] configs 为空，跳过本点")
                        r.lpush(f'client_scan:{point_id}_notify', json.dumps({
                            'status': 'error', 'point_id': point_id, 'message': 'configs 为空'
                        }))
                    else:
                        logger.info(f"📱 [S8] [{point_id}] 收到客户端扫描命令 "
                                    f"{len(cs_configs)} 个配置 × {dwell_time}s in_target={in_target}")

                        # 累积结果（跨所有信道）：{mac: {rssi:[], ap_bssid, ap_ssid, ap_ssid_source, probe_ssids, status}}
                        clients_dir  = {}   # 定向
                        clients_omni = {}   # 全向
                        # Beacon 收集：{bssid: ssid}，跨所有信道/线程共享，用于事后补全 ap_ssid
                        beacon_ssid_map = {}

                        # ──────────────────────────────────────────────────────
                        # 客户端帧识别逻辑（只看 TA = 客户端 MAC 的帧）
                        # subtype 含义：
                        #   0  Association Request    client→AP  含 SSID
                        #   2  Reassociation Request  client→AP  含 SSID
                        #   4  Probe Request          client→*   含 probe SSID（可为空）
                        #   10 Disassociation         client→AP
                        #   11 Authentication         client→AP
                        #   12 Deauthentication       client→AP
                        # ──────────────────────────────────────────────────────
                        CLIENT_MGMT_SUBTYPES = {0, 2, 4, 10, 11, 12}

                        def _decode_ssid(raw_bytes):
                            """解码 SSID 字节：先试 UTF-8，失败则试 GBK（中文路由器常用），最后回退 replace"""
                            try:
                                return raw_bytes.decode('utf-8').strip('\x00')
                            except UnicodeDecodeError:
                                try:
                                    return raw_bytes.decode('gbk').strip('\x00')
                                except UnicodeDecodeError:
                                    return raw_bytes.decode('utf-8', errors='replace').strip('\x00')

                        def _extract_client_info_from_packet(pkt, target_dict):
                            """
                            从一个数据包中提取客户端信息并写入 target_dict。
                            只处理客户端（STA）发出的帧，AP 发出的帧直接忽略。
                            """
                            from scapy.all import Dot11Elt
                            if RadioTap not in pkt or Dot11 not in pkt:
                                return

                            dot11 = pkt[Dot11]
                            frame_type    = dot11.type
                            frame_subtype = dot11.subtype

                            is_client_frame = False
                            ap_bssid   = None
                            ssid       = None
                            probe_ssid = None

                            if frame_type == 0:
                                if frame_subtype not in CLIENT_MGMT_SUBTYPES:
                                    return
                                is_client_frame = True
                                if frame_subtype in {0, 2}:
                                    # Association / Reassociation Request：含目标 AP BSSID 和 SSID
                                    ap_bssid = dot11.addr1
                                    try:
                                        elt = pkt[Dot11Elt]
                                        while elt:
                                            if elt.ID == 0:
                                                ssid = _decode_ssid(elt.info)
                                                break
                                            elt = elt.payload.getlayer(Dot11Elt)
                                    except Exception:
                                        pass
                                elif frame_subtype == 4:
                                    # Probe Request：仅记录 probe SSID（现代设备多为空 wildcard）
                                    try:
                                        elt = pkt[Dot11Elt]
                                        while elt:
                                            if elt.ID == 0:
                                                probe_ssid = _decode_ssid(elt.info)
                                                break
                                            elt = elt.payload.getlayer(Dot11Elt)
                                    except Exception:
                                        pass
                                elif frame_subtype in {10, 11, 12}:
                                    # Disassociation / Authentication / Deauthentication：含目标 AP BSSID
                                    ap_bssid = dot11.addr1

                            elif frame_type == 2:
                                fc      = int(dot11.FCfield)
                                to_ds   = (fc & 0x01) != 0
                                from_ds = (fc & 0x02) != 0
                                if not (to_ds and not from_ds):
                                    return
                                is_client_frame = True
                                ap_bssid = dot11.addr1

                            if not is_client_frame:
                                return

                            rssi = getattr(pkt[RadioTap], 'dBm_AntSignal', None)
                            if rssi is None:
                                return

                            client_mac = dot11.addr2
                            if not client_mac:
                                return
                            client_mac = client_mac.lower().strip()

                            if client_mac in ('ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00'):
                                return

                            if client_mac not in target_dict:
                                target_dict[client_mac] = {
                                    'rssi': [], 'ap_bssid': None,
                                    'ap_ssid': None, 'ap_ssid_source': None,
                                    'probe_ssids': [], 'status': 'probing',
                                }
                            entry = target_dict[client_mac]
                            entry['rssi'].append(rssi)

                            if ap_bssid:
                                clean_bssid = ap_bssid.lower().strip()
                                if clean_bssid not in ('ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00'):
                                    entry['ap_bssid'] = clean_bssid
                                    entry['status']   = 'associated'
                            if ssid is not None:
                                entry['ap_ssid']        = ssid
                                entry['ap_ssid_source'] = 'assoc_req'
                            if probe_ssid is not None and probe_ssid not in entry['probe_ssids']:
                                entry['probe_ssids'].append(probe_ssid)

                        def _extract_beacon_from_packet(pkt, beacon_map):
                            """从 Beacon 帧提取 bssid→ssid 映射（用于事后补全客户端的 ap_ssid）
                            同时调用 S9 星链识别器分析特征。"""
                            from scapy.all import Dot11Elt
                            if RadioTap not in pkt or Dot11 not in pkt:
                                return
                            dot11 = pkt[Dot11]
                            if dot11.type != 0 or dot11.subtype != 8:
                                return
                            bssid = dot11.addr3
                            if not bssid:
                                return
                            bssid = bssid.lower().strip()
                            if bssid in ('ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00'):
                                return
                            ssid = ''
                            try:
                                elt = pkt[Dot11Elt]
                                while elt:
                                    if elt.ID == 0:
                                        ssid = _decode_ssid(elt.info)
                                        if ssid and bssid not in beacon_map:
                                            beacon_map[bssid] = ssid
                                        break
                                    elt = elt.payload.getlayer(Dot11Elt)
                            except Exception:
                                pass
                            # S9: 星链识别（同一 BSSID 只解析一次，内部有缓存）
                            try:
                                # DS Parameter Set (ID=3) 取 AP 自声明信道
                                ap_ch = '?'
                                for _elt in pkt[Dot11Elt:]:
                                    if not hasattr(_elt, 'ID'):
                                        break
                                    if _elt.ID == 3 and len(_elt.info) >= 1:
                                        ap_ch = str(_elt.info[0])
                                        break
                                _starlink_detector.analyze_beacon(pkt, bssid, ssid, ap_ch)
                            except Exception:
                                pass

                        def make_combined_handler(client_dict, b_map):
                            """同时处理客户端帧和 Beacon 帧"""
                            def handler(pkt):
                                try:
                                    _extract_client_info_from_packet(pkt, client_dict)
                                except Exception:
                                    pass
                                try:
                                    _extract_beacon_from_packet(pkt, b_map)
                                except Exception:
                                    pass
                            return handler

                        def check_stop_cs():
                            return r.get('client_scan:stop') is not None

                        stop_filter_cs = lambda p: check_stop_cs()

                        cs_interrupted = False

                        # ── 逐信道跳扫 ──────────────────────────────────────
                        for cfg_idx, cfg in enumerate(cs_configs):
                            if check_stop_cs():
                                cs_interrupted = True
                                break

                            cfg_channel   = cfg.get('channel', 1)
                            cfg_bandwidth = cfg.get('bandwidth', 'HT20')

                            # ⚡ 并行切换双网卡信道
                            cs_dir_ok, omni_ok_cs = adjust_channels_parallel(
                                cfg_channel, cfg_bandwidth, logger=logger
                            )
                            if not cs_dir_ok:
                                logger.warning(f"⚠️ [S8] [{point_id}] [{cfg_idx+1}/{len(cs_configs)}] "
                                               f"ch{cfg_channel}/{cfg_bandwidth} 信道切换失败，跳过")
                                continue

                            try:
                                t_dir_cs = threading.Thread(
                                    target=sniff,
                                    kwargs={
                                        'iface': CAPTURE_DEFAULT_INTERFACE,
                                        'prn':   make_combined_handler(clients_dir, beacon_ssid_map),
                                        'store': False,
                                        'timeout': dwell_time,
                                        'stop_filter': stop_filter_cs,
                                    }
                                )
                                t_omni_cs = None
                                if OMNI_ENABLED and omni_ok_cs:
                                    t_omni_cs = threading.Thread(
                                        target=sniff,
                                        kwargs={
                                            'iface': CAPTURE_OMNI_INTERFACE,
                                            'prn':   make_combined_handler(clients_omni, beacon_ssid_map),
                                            'store': False,
                                            'timeout': dwell_time,
                                            'stop_filter': stop_filter_cs,
                                        }
                                    )
                                t_dir_cs.start()
                                if t_omni_cs:
                                    t_omni_cs.start()
                                t_dir_cs.join()
                                if t_omni_cs:
                                    t_omni_cs.join()
                            except Exception as e:
                                logger.error(f"❌ [S8] [{point_id}] ch{cfg_channel} 抓包异常: {e}")

                            if check_stop_cs():
                                cs_interrupted = True
                                break

                        if cs_interrupted:
                            logger.warning(f"🛑 [S8] [{point_id}] 客户端扫描被中断")
                            r.lpush(f'client_scan:{point_id}_notify', json.dumps({
                                'status': 'stopped', 'point_id': point_id,
                            }))
                        else:
                            # ── Beacon 补全 ap_ssid（Assoc Req 没拿到 SSID 时，用 Beacon 查表） ──
                            beacon_filled = 0
                            for info in clients_dir.values():
                                if info['ap_bssid'] and info['ap_ssid'] is None:
                                    ssid_from_beacon = beacon_ssid_map.get(info['ap_bssid'])
                                    if ssid_from_beacon:
                                        info['ap_ssid']        = ssid_from_beacon
                                        info['ap_ssid_source'] = 'beacon'
                                        beacon_filled += 1
                            if beacon_filled:
                                logger.info(f"📡 [S8] [{point_id}] Beacon 补全 {beacon_filled} 个 ap_ssid "
                                            f"(共收集 {len(beacon_ssid_map)} 个 Beacon)")

                            # ── 汇总本点位结果（跨所有信道累积） ──────────────
                            point_clients = {}
                            for mac, info in clients_dir.items():
                                rssi_list = info['rssi']
                                rssi_avg  = round(sum(rssi_list) / len(rssi_list), 2) if rssi_list else None
                                ap_bssid  = info['ap_bssid']
                                entry = {
                                    'type':              'client',
                                    'rssi_avg':          rssi_avg,
                                    'sample_count':      len(rssi_list),
                                    'status':            info['status'],
                                    'ap_bssid':          ap_bssid,
                                    'ap_ssid':           info['ap_ssid'],
                                    'ap_ssid_source':    info['ap_ssid_source'],
                                    'ap_is_starlink':    _starlink_detector.is_starlink(ap_bssid),  # S9
                                    'probe_ssids':       info['probe_ssids'],
                                    'omni_rssi_avg':     None,
                                    'omni_sample_count': 0,
                                }
                                if OMNI_ENABLED and mac in clients_omni:
                                    omni_list = clients_omni[mac]['rssi']
                                    if omni_list:
                                        entry['omni_rssi_avg']     = round(sum(omni_list) / len(omni_list), 2)
                                        entry['omni_sample_count'] = len(omni_list)
                                point_clients[mac] = entry

                            # S9: 将本轮识别到的星链设备写入 Redis，供 web_server 查询
                            starlink_found = _starlink_detector.get_all_starlink()
                            if starlink_found:
                                try:
                                    r.set('starlink:detected_bssids', json.dumps(starlink_found), ex=86400)
                                except Exception as _e:
                                    logger.warning(f"[S9] 写入 Redis starlink:detected_bssids 失败: {_e}")

                            starlink_count = len(starlink_found)
                            logger.info(f"✅ [S8] [{point_id}] 采集完毕: "
                                        f"发现 {len(point_clients)} 个客户端 "
                                        f"(定向 {len(clients_dir)}, 全向 match {len(clients_omni)}, "
                                        f"Beacon {len(beacon_ssid_map)} 个, "
                                        f"星链 {starlink_count} 个)")

                            r.lpush(f'client_scan:{point_id}_notify', json.dumps({
                                'status':       'done',
                                'point_id':     point_id,
                                'in_target':    in_target,
                                'clients':      point_clients,
                                'client_count': len(point_clients),
                            }))

                # ============= WiFi 连接命令 =============
                elif action == "wifi_connect":
                    connect_id = command.get('connect_id')
                    ssid = command.get('ssid', '')
                    bssid = command.get('bssid')
                    password = command.get('password', '')
                    timeout = command.get('timeout', 120)

                    if not connect_id:
                        logger.warning("[wifi_connect] 命令缺少 connect_id，忽略")
                        continue

                    # 校验 active_connect_id
                    active_id = r.get('wifi_connect:active_connect_id')
                    if active_id and active_id != connect_id:
                        logger.warning(f"[wifi_connect] connect_id 不匹配: cmd={connect_id} active={active_id}，忽略")
                        continue

                    interface = _directional_interface  # 定向网卡
                    logger.info(f"[wifi_connect] 开始处理 connect_id={connect_id} ssid={ssid} "
                                f"bssid={bssid} timeout={timeout} iface={interface}")
                    logger.info(f"[wifi_connect] ===== 即将调用 _run_wpa_connect =====")
                    _run_wpa_connect(r, interface, ssid, bssid, password, timeout, connect_id)
                    logger.info(f"[wifi_connect] ===== _run_wpa_connect 返回 =====")

                elif action == "stop_capture":

                    reason = command.get('reason', 'unknown')
                    logger.info("收到停止抓包命令", extra={"reason": reason})
                    
                    pcap_files = _current_pcap_files()
                    stop_capture()
                     # 等待一小段时间，让Scapy完全停止
                    time.sleep(0.5)
                    r.set(CAPTURE_RUNNING_KEY, '0')
                    r.delete('capture:stop')
                    _finish_capture_status(reason, phase='stopped', extra={'pcap_files': pcap_files})
                    _update_capture_at_best_status(False, 'stopped', {'reason': reason})
                    logger.info("抓包已停止")
                
                else: 
                    logger.warning("未知命令", extra={"action": action})
                    
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                logger.error("Redis 连接丢失或超时，5秒后尝试重连", extra={
                    "error": str(e),
                    "error_type": type(e).__name__
                })
                time.sleep(5)
                try:
                    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
                    logger.info("Redis 重新连接成功")
                except Exception as e: 
                    logger.error("Redis 重连失败", extra={"error": str(e)})
            except Exception as e:
                logger.error("主循环发生错误", extra={
                    "error": str(e),
                    "error_type": type(e).__name__
                })
                time.sleep(1)
                
    except Exception as e:
        logger.critical("初始化失败", extra={
            "error": str(e),
            "error_type": type(e).__name__
        })
    finally:
        logger.info("清理资源...")
        _stop_full_scan_config_session(reason='worker_shutdown', timeout=2.0)
        _stop_full_scan_refinement_session(timeout=1.0)
        stop_capture()
        try:
            if r: 
                r.set(CAPTURE_RUNNING_KEY, '0')
        except Exception: 
            pass
        logger.info("已退出")


if __name__ == "__main__":
    capture_worker_main()
