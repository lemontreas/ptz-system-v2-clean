#!/usr/bin/env python3
"""Rebuild full-area scan whitelist from existing Redis round results.

This is a test/maintenance helper. It does not move PTZ, does not capture
packets, and only reads existing full_scan:round_N_results data.
"""

import argparse
import json
from datetime import datetime

import redis

from config_loader import get_full_scan_filter_config, get_redis_config
from ptz_control import _build_full_scan_whitelist_payload


def _to_local_iso(value):
    if not value:
        return value
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return value
        return dt.astimezone().replace(microsecond=0).isoformat()
    except Exception:
        return value


def _infer_work_ranges(round_payload):
    ranges = []
    seen = set()
    results = round_payload.get("results") or {}

    for point_data in results.values():
        if not isinstance(point_data, dict):
            continue
        if point_data.get("phase") not in ("work", "fine"):
            continue
        scan_range = point_data.get("scan_range") or {}
        pan_range = scan_range.get("pan") or []
        tilt_range = scan_range.get("tilt") or []
        try:
            pan_min, pan_max = sorted([float(pan_range[0]), float(pan_range[1])])
            tilt_min, tilt_max = sorted([float(tilt_range[0]), float(tilt_range[1])])
        except (TypeError, ValueError, IndexError):
            continue

        key = (round(pan_min, 4), round(pan_max, 4), round(tilt_min, 4), round(tilt_max, 4))
        if key in seen:
            continue
        seen.add(key)
        ranges.append({
            "pan_range": [pan_min, pan_max],
            "tilt_range": [tilt_min, tilt_max],
        })

    return ranges


def _strip_debug_fields(payload, keep_debug=False):
    if not keep_debug:
        payload.pop("rejected_mac_count", None)
        payload.pop("rejected_macs", None)
        payload.pop("candidate_mac_count", None)
    payload.pop("filtered_out_mac_count", None)

    payload["round_started_at"] = _to_local_iso(payload.get("round_started_at"))
    payload["round_finished_at"] = _to_local_iso(payload.get("round_finished_at"))
    payload["filter_config"] = _filter_config_for_output(payload.get("filter_config") or {})

    for item in payload.get("mac_whitelist") or []:
        if isinstance(item, dict):
            item.pop("filter_passed", None)
            item.pop("failed_reasons", None)
            item["first_seen_at"] = _to_local_iso(item.get("first_seen_at"))
            item["last_seen_at"] = _to_local_iso(item.get("last_seen_at"))


def _filter_config_for_output(filter_config):
    if "enabled" in filter_config:
        return filter_config
    return {
        "enabled": bool(filter_config.get("启用", True)),
        "min_work_hit_points": int(filter_config.get("工作区最少命中点数", 2)),
        "min_total_hit_points": int(filter_config.get("整轮最少命中点数", 2)),
        "min_work_vs_other_delta_db": float(filter_config.get("工作区相对其他区域最小强度差", 3.0)),
        "min_directional_vs_omni_delta_db": float(filter_config.get("定向相对全向最小强度差", 5.0)),
        "require_best_point_in_work_area": bool(filter_config.get("要求最强点在工作区", True)),
        "coord_dedupe_deg": float(filter_config.get("坐标去重角度", 0.05)),
    }


def _update_rounds(r, payload):
    round_index = payload.get("round_index")
    raw_rounds = r.get("full_scan:whitelist:rounds")
    try:
        rounds = json.loads(raw_rounds) if raw_rounds else []
        if not isinstance(rounds, list):
            rounds = []
    except Exception:
        rounds = []

    now = datetime.now().astimezone().replace(microsecond=0).isoformat()
    found = False
    for item in rounds:
        if isinstance(item, dict) and item.get("round_index") == round_index:
            item["round_id"] = payload.get("round_id")
            item["mac_count"] = payload.get("mac_count", 0)
            item["updated_at"] = now
            found = True
            break
    if not found:
        rounds.append({
            "round_index": round_index,
            "round_id": payload.get("round_id"),
            "mac_count": payload.get("mac_count", 0),
            "created_at": now,
        })

    rounds.sort(key=lambda item: item.get("round_index", 0) if isinstance(item, dict) else 0)
    r.set("full_scan:whitelist:rounds", json.dumps(rounds, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Rebuild full scan whitelist from existing Redis results.")
    parser.add_argument("--round", dest="round_index", type=int, default=None, help="Round index, default uses full_scan:latest_round")
    parser.add_argument("--print", dest="print_payload", action="store_true", help="Print rebuilt whitelist payload")
    parser.add_argument("--keep-debug", dest="keep_debug", action="store_true", help="保留 rejected_macs 等调试字段")
    args = parser.parse_args()

    redis_host, redis_port = get_redis_config()
    r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)

    round_index = args.round_index
    if round_index is None:
        latest_round = r.get("full_scan:latest_round")
        if not latest_round:
            raise SystemExit("ERROR: full_scan:latest_round 不存在，请用 --round 指定轮次")
        round_index = int(latest_round)

    results_key = f"full_scan:round_{round_index}_results"
    raw_payload = r.get(results_key)
    if not raw_payload:
        raise SystemExit(f"ERROR: 未找到轮次结果 {results_key}")

    round_payload = json.loads(raw_payload)
    work_ranges = round_payload.get("work_ranges") or _infer_work_ranges(round_payload)
    if not work_ranges:
        raise SystemExit("ERROR: 无法从轮次结果推断工作区范围")

    filter_config = get_full_scan_filter_config()
    whitelist_payload = _build_full_scan_whitelist_payload(
        round_payload=round_payload,
        work_ranges=work_ranges,
        filter_config=filter_config,
    )
    _strip_debug_fields(whitelist_payload, keep_debug=args.keep_debug)

    whitelist_key = f"full_scan:whitelist:round_{round_index}"
    encoded = json.dumps(whitelist_payload, ensure_ascii=False)
    r.set(whitelist_key, encoded)
    r.set("full_scan:whitelist:latest_success", encoded)
    r.set("full_scan:whitelist:latest_round", str(round_index))
    _update_rounds(r, whitelist_payload)

    summary = {
        "status": "done",
        "round_index": round_index,
        "source_results_key": results_key,
        "whitelist_key": whitelist_key,
        "mac_count": whitelist_payload.get("mac_count", 0),
        "work_ranges": work_ranges,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.print_payload:
        print(json.dumps(whitelist_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
