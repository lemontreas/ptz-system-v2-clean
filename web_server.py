from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import math
import redis
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

from history_store import (
    create_project,
    finish_project,
    get_latest_snapshot,
    get_project,
    get_snapshot_at,
    init_db,
    list_projects,
    list_snapshots,
    update_project_status,
)
from config_loader import (
    get_redis_config,
    get_ptz_config,
    get_location_scan_config,
    get_camera_config,
    get_full_scan_config,
    get_wifi_connect_config,
)
from antenna_bias_utils import build_antenna_bias
from full_scan_wifi_mode import (
    build_full_scan_single_channel_test_config,
    build_full_scan_wifi_configs,
)
from full_scan_image_context import (
    SINGLE_CAPTURE_RE,
    normalize_image_url,
    resolve_panorama_image_context,
    resolve_single_image_context,
)
import wifi_mode_utils
from ptz_range_redis import (
    business_scan_range_from_redis,
    full_area_precheck_range_from_redis,
    hardware_limits_from_redis,
)
from panorama_stitch_utils import compute_shot_grid

try:
    from hugin_panorama_runtime import (
        angles_to_pixels_pmap as hugin_angles_to_pixels_pmap,
        angles_to_pixels_visual as hugin_angles_to_pixels_visual,
        build_hugin_panorama_from_shots,
        pixels_to_angles_pmap as hugin_pixels_to_angles_pmap,
        pixels_to_angles_visual as hugin_pixels_to_angles_visual,
    )
except Exception as _hugin_import_error:
    HUGIN_IMPORT_ERROR = repr(_hugin_import_error)
    build_hugin_panorama_from_shots = None
    hugin_angles_to_pixels_pmap = None
    hugin_pixels_to_angles_pmap = None
    hugin_pixels_to_angles_visual = None
    hugin_angles_to_pixels_visual = None
else:
    HUGIN_IMPORT_ERROR = None


# ---------------- 基础配置 ----------------
# 从 config.json 加载配置，支持环境变量覆盖
_redis_host, _redis_port = get_redis_config()
REDIS_HOST = _redis_host
REDIS_PORT = _redis_port

PTZ_COMMAND_QUEUE = 'ptz:command_queue'
PTZ_STATUS_KEY = 'ptz:current_status'

CAPTURE_COMMAND_QUEUE = 'capture:command_queue'
CAPTURE_DATA_STREAM = 'capture:data_stream'
CAPTURE_RUNNING_KEY = 'capture:running'
CAPTURE_STATUS_KEY = 'capture:status'
CURRENT_PROJECT_KEY = 'history:current_project'
WIFI_CONNECT_STATUS_KEY = 'wifi_connect:status'
WIFI_CONNECT_ACTIVE_ID_KEY = 'wifi_connect:active_connect_id'


def _deep_merge_status(base, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge_status(base[key], value)
        else:
            base[key] = value
    return base


def _patch_ptz_status_atomic(r, patch):
    """通过 WATCH 原子 patch 聚合状态，避免 stop 被 worker 的旧快照覆盖。"""
    pipeline_factory = getattr(type(r), 'pipeline', None)
    if not callable(pipeline_factory):
        try:
            current = json.loads(r.get(PTZ_STATUS_KEY) or '{}')
        except Exception:
            current = {}
        _deep_merge_status(current, patch)
        current['ts'] = time.time()
        r.set(PTZ_STATUS_KEY, json.dumps(current, ensure_ascii=False))
        return current

    for _attempt in range(8):
        pipe = r.pipeline()
        try:
            pipe.watch(PTZ_STATUS_KEY)
            try:
                current = json.loads(pipe.get(PTZ_STATUS_KEY) or '{}')
            except Exception:
                current = {}
            _deep_merge_status(current, patch)
            current['ts'] = time.time()
            pipe.multi()
            pipe.set(PTZ_STATUS_KEY, json.dumps(current, ensure_ascii=False))
            pipe.execute()
            return current
        except redis.exceptions.WatchError:
            continue
        finally:
            pipe.reset()
    raise RuntimeError("ptz:current_status 原子更新连续冲突")


def _delete_key_if_value_matches(r, key, expected_value):
    """仅当 Redis 字符串值仍匹配时删除，避免自愈误删新任务 ID。"""
    if expected_value in (None, ''):
        return 0
    return r.eval(
        """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        end
        return 0
        """,
        1,
        key,
        str(expected_value),
    )


def _project_full_scan_stop_status(
    status,
    stop_raw,
    active_scan_id,
    current_project=None,
    now=None,
):
    """综合 stop key 与 STOPPING 项目修正视图，并兜底已消化信号的历史脏态。"""
    if not isinstance(status, dict):
        return status
    full_scan = status.get('full_scan')
    if not isinstance(full_scan, dict) or full_scan.get('terminal') is True:
        return status
    try:
        stop_data = json.loads(stop_raw) if stop_raw else {}
    except (TypeError, json.JSONDecodeError):
        stop_data = {}
    stop_scan_id = stop_data.get('scan_id') if isinstance(stop_data, dict) else None
    status_scan_id = full_scan.get('scan_id')
    matching_stop = bool(
        stop_scan_id
        and status_scan_id
        and stop_scan_id == status_scan_id
    )
    project = current_project if isinstance(current_project, dict) else {}
    project_scan_id = project.get('scan_id')
    project_stopping = bool(
        project.get('scan_type') == 'full_area'
        and project.get('status') == 'STOPPING'
        and (
            not project_scan_id
            or not status_scan_id
            or project_scan_id == status_scan_id
        )
    )
    project_running = bool(
        project.get('scan_type') == 'full_area'
        and project.get('status') in ('RUNNING', 'STOPPING')
        and (
            not project_scan_id
            or not status_scan_id
            or project_scan_id == status_scan_id
        )
    )
    if not matching_stop and not project_stopping:
        state = str(full_scan.get('state') or '').lower()
        looks_active = bool(
            full_scan.get('active') is True
            or state in ('queued', 'running', 'stopping')
        )
        if not active_scan_id and looks_active and not project_running:
            terminal_state = (
                'stopped'
                if (
                    full_scan.get('reason') == 'manual_stop'
                    or full_scan.get('stop_requested') is True
                )
                else 'done'
            )
            full_scan.update({
                'active': False,
                'state': terminal_state,
                'phase': 'stopped' if terminal_state == 'stopped' else 'completed',
                'stop_requested': False,
                'terminal': True,
            })
            return status
        return status

    full_scan['active'] = False
    stop_age = None
    now_ts = float(now if now is not None else time.time())
    if matching_stop:
        try:
            stop_age = now_ts - float(stop_data.get('ts'))
        except (TypeError, ValueError):
            pass
    if stop_age is None and project_stopping and project.get('updated_at'):
        try:
            updated = datetime.fromisoformat(
                str(project['updated_at']).replace('Z', '+00:00')
            )
            stop_age = (
                datetime.fromtimestamp(now_ts, timezone.utc) - updated
            ).total_seconds()
        except (TypeError, ValueError):
            pass
    if stop_age is None and project_stopping:
        fallback_ts = full_scan.get('stop_requested_at')
        if fallback_ts is None and full_scan.get('reason') == 'manual_stop':
            fallback_ts = status.get('ts')
        try:
            stop_age = now_ts - float(fallback_ts)
        except (TypeError, ValueError):
            pass

    stop_expired = stop_age is not None and stop_age >= 2.0
    if active_scan_id and not stop_expired:
        full_scan.update({
            'state': 'stopping',
            'stop_requested': True,
            'terminal': False,
        })
    else:
        full_scan.update({
            'state': 'stopped',
            'phase': 'stopped',
            'stop_requested': False,
            'terminal': True,
            'reason': (
                stop_data.get('reason')
                or full_scan.get('reason')
                or 'manual_stop'
            ),
        })
    return status


# ---------------- PTZ 配置（从 config.json 加载，支持环境变量覆盖） ----------------
_ptz_config = get_ptz_config()
# 默认开机时自动扫描的范围，不传参时将采用此范围
PTZ_DEFAULT_PAN_MIN = _ptz_config["默认扫描范围"]["水平最小"]
PTZ_DEFAULT_PAN_MAX = _ptz_config["默认扫描范围"]["水平最大"]
PTZ_DEFAULT_TILT_MIN = _ptz_config["默认扫描范围"]["垂直最小"]
PTZ_DEFAULT_TILT_MAX = _ptz_config["默认扫描范围"]["垂直最大"]
# 默认步长（可选环境变量覆盖）
PTZ_DEFAULT_PAN_STEP = _ptz_config["默认扫描范围"]["水平步长"]
PTZ_DEFAULT_TILT_STEP = _ptz_config["默认扫描范围"]["垂直步长"]

# ---------------- 设备限位（与 PTZ Worker 保持一致，从 config.json 加载） ----------------
PTZ_LIMIT_PAN_MIN = _ptz_config["限位"]["水平最小"]
PTZ_LIMIT_PAN_MAX = _ptz_config["限位"]["水平最大"]
PTZ_LIMIT_TILT_MIN = _ptz_config["限位"]["垂直最小"]
PTZ_LIMIT_TILT_MAX = _ptz_config["限位"]["垂直最大"]
FULL_AREA_ESTIMATED_CONFIG_COUNT = 26

# Redis 不可用时，硬件限位与「默认扫描范围」兜底（来自 config.json）
_HARDWARE_LIMITS_FALLBACK = {
    "pan_min": PTZ_LIMIT_PAN_MIN,
    "pan_max": PTZ_LIMIT_PAN_MAX,
    "tilt_min": PTZ_LIMIT_TILT_MIN,
    "tilt_max": PTZ_LIMIT_TILT_MAX,
}
_CONFIG_DEFAULT_SCAN = {
    "pan_min": PTZ_DEFAULT_PAN_MIN,
    "pan_max": PTZ_DEFAULT_PAN_MAX,
    "tilt_min": PTZ_DEFAULT_TILT_MIN,
    "tilt_max": PTZ_DEFAULT_TILT_MAX,
    "pan_step": PTZ_DEFAULT_PAN_STEP,
    "tilt_step": PTZ_DEFAULT_TILT_STEP,
}

BASE_DIR = Path(__file__).resolve().parent
CAPTURES_DIR = BASE_DIR / "data" / "captures"
PANORAMA_OVERLAP = 0.6
PANORAMA_TRUSTED_CORE_HALF_RATIO = float(
    os.getenv(
        'PANORAMA_TRUSTED_CORE_HALF_RATIO',
        os.getenv('HUGIN_TRUSTED_CORE_HALF_RATIO', '0.20'),
    )
)
PANORAMA_TRUSTED_OVERLAP = float(os.getenv('PANORAMA_TRUSTED_OVERLAP', '0.30'))
PANORAMA_PX_PER_DEG = get_camera_config()["全景像素每度"]
PANORAMA_AE_SETTLE_SEC = 2.0
CAMERA_PANORAMA_META_KEY = 'camera:last_panorama_meta'
PTZ_ARRIVE_TOL_DEG = 1.0
PTZ_POLL_INTERVAL = 0.3
PTZ_ARRIVE_TIMEOUT = 90.0
MOVING_STATES = frozenset({"MOVING", "CALIBRATING", "HOMING"})

# ---------------- 定位扫描配置（从 config.json 加载，支持环境变量覆盖） ----------------
_loc_scan_config = get_location_scan_config()
LOCATION_SCAN_EXPAND_DEG    = _loc_scan_config["扩边角度"]
LOCATION_SCAN_TRACK_RSSI_THRESHOLD = _loc_scan_config["快速校验信号阈值"]
LOCATION_SCAN_SHRINK_TOP_N = _loc_scan_config["收缩强点数量"]
LOCATION_SCAN_SHRINK_RSSI_DELTA = _loc_scan_config["收缩RSSI差值阈值"]
LOCATION_SCAN_SHRINK_OUTLIER_DEG = _loc_scan_config["收缩离群角度阈值"]
LOCATION_SCAN_SHRINK_SINGLE_PAN_HALF = _loc_scan_config["单点兜底水平半宽"]
LOCATION_SCAN_SHRINK_SINGLE_TILT_HALF = _loc_scan_config["单点兜底垂直半宽"]
LOCATION_SCAN_TARGET_GUARD_INNER_POINTS = _loc_scan_config["目标区内部保底点数"]
LOCATION_SCAN_TARGET_GUARD_MIN_SPAN_DEG = _loc_scan_config["目标区分点最小跨度"]

_FULL_SCAN_CONFIG = get_full_scan_config()
FULL_SCAN_PRECHECK_STEP = 8.0
FULL_SCAN_WORK_STEP = 4.0
FULL_SCAN_COARSE_DWELL_TIME = float(_FULL_SCAN_CONFIG.get("粗扫每信道停留时长", 0.4))
FULL_SCAN_FINE_DWELL_TIME = float(_FULL_SCAN_CONFIG.get("细扫每信道停留时长", 0.6))
FULL_SCAN_DEVIATION_DWELL_TIME = float(_FULL_SCAN_CONFIG.get("偏差区每信道停留时长", 0.8))
FULL_SCAN_COARSE_STEP = float(_FULL_SCAN_CONFIG.get("粗扫步径", 10.0))
FULL_SCAN_COARSE_CORE_MIN = int(_FULL_SCAN_CONFIG.get("粗扫核心点数下限", 12))
FULL_SCAN_COARSE_OUTER_PROBE_DEG = float(_FULL_SCAN_CONFIG.get("粗扫外扩角度", 9.0))
FULL_SCAN_FINE_CORE_MIN = int(_FULL_SCAN_CONFIG.get("细扫核心点数下限", 20))
FULL_SCAN_FINE_CORE_MAX = int(_FULL_SCAN_CONFIG.get("细扫核心点数上限", 26))
FULL_SCAN_DEVIATION_DIVISIONS = max(2, int(_FULL_SCAN_CONFIG.get("偏差区外扩等分数", 4)))
FULL_SCAN_DEVIATION_POINTS_PER_LAYER = int(_FULL_SCAN_CONFIG.get("偏差区每层点数", 5))
FULL_SCAN_DEVIATION_NARROW_SIDE_DEG = float(_FULL_SCAN_CONFIG.get("偏差区窄边阈值", 2.0))
FULL_SCAN_SMALL_MOVE_INTERLEAVE_DEG = float(_FULL_SCAN_CONFIG.get("小范围交错排序阈值", 1.0))
FULL_SCAN_MINIMAL_TEST_COARSE_OUTER_PROBE_POINTS = 2
FULL_SCAN_MINIMAL_TEST_COARSE_GRID_POINTS = 3
FULL_SCAN_MINIMAL_TEST_FINE_POINTS = 3
FULL_SCAN_MINIMAL_TEST_DEVIATION_POINTS_PER_LAYER = 2


def _build_axis_positions(axis_min, axis_max, axis_step):
    """Mirror the worker logic: step from min and force-append max when needed."""
    positions = []
    current = axis_min
    while current <= axis_max:
        positions.append(current)
        current += axis_step
    if positions[-1] != axis_max:
        positions.append(axis_max)
    return positions


def _count_scan_points(pan_range, tilt_range, pan_step, tilt_step):
    pan_positions = _build_axis_positions(pan_range[0], pan_range[1], pan_step)
    tilt_positions = _build_axis_positions(tilt_range[0], tilt_range[1], tilt_step)
    return len(pan_positions) * len(tilt_positions)


def _build_scan_path_points(pan_range, tilt_range, pan_step, tilt_step):
    pan_positions = _build_axis_positions(pan_range[0], pan_range[1], pan_step)
    tilt_positions = _build_axis_positions(tilt_range[0], tilt_range[1], tilt_step)
    points = []
    for row_idx, tilt in enumerate(tilt_positions):
        row = pan_positions if row_idx % 2 == 0 else reversed(pan_positions)
        for pan in row:
            points.append((round(float(pan), 2), round(float(tilt), 2)))
    return points


def _build_scan_edge_points(pan_range, tilt_range, pan_step, tilt_step):
    pan_min, pan_max = sorted([float(pan_range[0]), float(pan_range[1])])
    tilt_min, tilt_max = sorted([float(tilt_range[0]), float(tilt_range[1])])
    pan_positions = _build_axis_positions(pan_min, pan_max, pan_step)
    tilt_positions = _build_axis_positions(tilt_min, tilt_max, tilt_step)
    points = []

    for pan in pan_positions:
        points.append((round(float(pan), 2), round(float(tilt_min), 2)))
    for tilt in tilt_positions[1:-1]:
        points.append((round(float(pan_max), 2), round(float(tilt), 2)))
    for pan in reversed(pan_positions):
        points.append((round(float(pan), 2), round(float(tilt_max), 2)))
    for tilt in reversed(tilt_positions[1:-1]):
        points.append((round(float(pan_min), 2), round(float(tilt), 2)))

    return points


def _auto_full_area_step(axis_min, axis_max, phase="precheck"):
    """Keep API estimates aligned with ptz_control full-area path generation."""
    return FULL_SCAN_WORK_STEP if phase == "work" else FULL_SCAN_PRECHECK_STEP


def _point_in_range(pan, tilt, pan_range, tilt_range):
    eps = 1e-9
    pan_min, pan_max = sorted([float(pan_range[0]), float(pan_range[1])])
    tilt_min, tilt_max = sorted([float(tilt_range[0]), float(tilt_range[1])])
    return (
        pan_min - eps <= float(pan) <= pan_max + eps and
        tilt_min - eps <= float(tilt) <= tilt_max + eps
    )


def _point_in_work_ranges(pan, tilt, work_ranges):
    for item in work_ranges or []:
        if _point_in_range(pan, tilt, item["pan_range"], item["tilt_range"]):
            return True
    return False


def _expand_work_range_for_guard(item, precheck_pan_range, precheck_tilt_range):
    pan_min, pan_max = sorted([float(item["pan_range"][0]), float(item["pan_range"][1])])
    tilt_min, tilt_max = sorted([float(item["tilt_range"][0]), float(item["tilt_range"][1])])
    pre_pan_min, pre_pan_max = sorted([float(precheck_pan_range[0]), float(precheck_pan_range[1])])
    pre_tilt_min, pre_tilt_max = sorted([float(precheck_tilt_range[0]), float(precheck_tilt_range[1])])
    guard_pan = [
        max(pre_pan_min, pan_min),
        min(pre_pan_max, pan_max),
    ]
    guard_tilt = [
        max(pre_tilt_min, tilt_min),
        min(pre_tilt_max, tilt_max),
    ]
    if guard_pan[0] >= guard_pan[1] or guard_tilt[0] >= guard_tilt[1]:
        return None
    return {"pan_range": guard_pan, "tilt_range": guard_tilt}


def _dedupe_scan_points(points, seen):
    deduped = []
    for pan, tilt in points:
        key = (round(float(pan), 2), round(float(tilt), 2))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _choose_full_scan_grid_counts(span_h, span_v, min_points, max_points):
    min_points = max(1, int(min_points))
    max_points = max(min_points, int(max_points))
    ratio = span_h / span_v if span_v > 0 else 1.0
    target = (min_points + max_points) / 2.0
    best = None
    for rows in range(1, max_points + 1):
        for cols in range(1, max_points + 1):
            count = rows * cols
            if count < min_points or count > max_points:
                continue
            grid_ratio = cols / rows if rows else ratio
            ratio_score = abs(math.log(max(grid_ratio, 1e-6) / max(ratio, 1e-6)))
            count_score = abs(count - target) / max(target, 1.0)
            score = ratio_score * 2.0 + count_score
            if best is None or score < best[0]:
                best = (score, rows, cols)
    if best:
        return best[1], best[2]
    rows = max(1, int(math.sqrt(min_points / max(ratio, 1e-6))))
    cols = max(1, int(math.ceil(min_points / rows)))
    return rows, cols


def _choose_min_full_scan_grid_counts(span_h, span_v, min_points):
    min_points = max(1, int(min_points))
    ratio = span_h / span_v if span_v > 0 else 1.0
    best = None
    for rows in range(1, min_points + 1):
        cols = max(1, int(math.ceil(min_points / rows)))
        count = rows * cols
        grid_ratio = cols / rows if rows else ratio
        ratio_score = abs(math.log(max(grid_ratio, 1e-6) / max(ratio, 1e-6)))
        count_score = (count - min_points) / max(min_points, 1)
        score = ratio_score * 2.0 + count_score
        if best is None or score < best[0]:
            best = (score, rows, cols)
    return best[1], best[2]


def _grid_center_points_by_step_for_plan(pan_range, tilt_range, step, min_points):
    pan_min, pan_max = sorted([float(pan_range[0]), float(pan_range[1])])
    tilt_min, tilt_max = sorted([float(tilt_range[0]), float(tilt_range[1])])
    span_h = max(0.0, pan_max - pan_min)
    span_v = max(0.0, tilt_max - tilt_min)
    step = max(float(step or 1.0), 0.1)
    cols = max(1, int(math.ceil(span_h / step))) if span_h > 0 else 1
    rows = max(1, int(math.ceil(span_v / step))) if span_v > 0 else 1
    if rows * cols < max(1, int(min_points)):
        rows, cols = _choose_min_full_scan_grid_counts(span_h, span_v, min_points)
    points = []
    for row_idx in range(rows):
        tilt = tilt_min + (row_idx + 0.5) * span_v / rows if span_v > 0 else (tilt_min + tilt_max) / 2.0
        row_points = []
        for col_idx in range(cols):
            pan = pan_min + (col_idx + 0.5) * span_h / cols if span_h > 0 else (pan_min + pan_max) / 2.0
            row_points.append((round(float(pan), 2), round(float(tilt), 2)))
        if row_idx % 2:
            row_points.reverse()
        points.extend(row_points)
    return points


def _grid_center_points_for_plan(pan_range, tilt_range, min_points, max_points):
    pan_min, pan_max = sorted([float(pan_range[0]), float(pan_range[1])])
    tilt_min, tilt_max = sorted([float(tilt_range[0]), float(tilt_range[1])])
    rows, cols = _choose_full_scan_grid_counts(
        pan_max - pan_min,
        tilt_max - tilt_min,
        min_points,
        max_points,
    )
    points = []
    for row_idx in range(rows):
        tilt = tilt_min + (row_idx + 0.5) * (tilt_max - tilt_min) / rows
        row_points = []
        for col_idx in range(cols):
            pan = pan_min + (col_idx + 0.5) * (pan_max - pan_min) / cols
            row_points.append((round(float(pan), 2), round(float(tilt), 2)))
        if row_idx % 2:
            row_points.reverse()
        points.extend(row_points)
    return points


def _deviation_ring_point_count_for_plan(work_ranges):
    return len(work_ranges or []) * (FULL_SCAN_DEVIATION_DIVISIONS - 1) * max(1, FULL_SCAN_DEVIATION_POINTS_PER_LAYER)


def _pixel_grid_point_count_for_plan(pixel_range, target_count, min_points, max_points):
    x0, x1 = pixel_range["x_range"]
    y0, y1 = pixel_range["y_range"]
    width = x1 - x0
    height = y1 - y0
    if width <= 0 or height <= 0:
        return 0
    cols = max(3, round(math.sqrt(target_count * width / height)))
    rows = max(3, round(target_count / cols))
    while cols * rows > max_points:
        if cols >= rows:
            cols -= 1
        else:
            rows -= 1
    while cols * rows < min_points:
        cols += 1
    points = {
        (
            int(x0 + (col + 0.5) * width / cols),
            int(y0 + (row + 0.5) * height / rows),
        )
        for col in range(cols)
        for row in range(rows)
    }
    return len(points)


def _build_full_area_scan_plan(outer_range, target_range):
    scan_plan = []
    coarse_core_count = _pixel_grid_point_count_for_plan(
        outer_range,
        target_count=20,
        min_points=12,
        max_points=24,
    )
    deviation_layers = max(1, FULL_SCAN_DEVIATION_DIVISIONS - 1)
    deviation_points_per_layer = max(1, FULL_SCAN_DEVIATION_POINTS_PER_LAYER)
    scan_plan.append({
        "phase": "coarse",
        "area_index": None,
        "coordinate_space": "pixel",
        "x_range": outer_range["x_range"],
        "y_range": outer_range["y_range"],
        "core_point_min": 12,
        "core_point_max": 24,
        "core_point_count": coarse_core_count,
        "outer_probe_point_count": 8,
        "point_count": coarse_core_count + 8,
    })

    fine_core_count = _pixel_grid_point_count_for_plan(
        target_range,
        target_count=23,
        min_points=20,
        max_points=26,
    )
    scan_plan.append({
        "phase": "fine",
        "area_index": 0,
        "coordinate_space": "pixel",
        "x_range": target_range["x_range"],
        "y_range": target_range["y_range"],
        "core_point_min": 20,
        "core_point_max": 26,
        "core_point_count": fine_core_count,
        "boundary_point_count": 0,
        "point_count": fine_core_count,
    })

    scan_plan.append({
        "phase": "deviation_a",
        "area_index": None,
        "coordinate_space": "pixel",
        "x_range": outer_range["x_range"],
        "y_range": outer_range["y_range"],
        "target_range": target_range,
        "outer_divisions": FULL_SCAN_DEVIATION_DIVISIONS,
        "layers": deviation_layers,
        "points_per_layer": deviation_points_per_layer,
        "point_count": deviation_layers * deviation_points_per_layer,
        "config_source": "target_seen_macs",
    })

    return scan_plan, sum(item["point_count"] for item in scan_plan)


def _apply_full_scan_minimal_point_test_plan(scan_plan):
    """Return an estimated plan for the internal minimal-point full-scan test mode."""
    limited_plan = []
    for item in scan_plan or []:
        phase = item.get("phase")
        limited = dict(item)
        original_point_count = int(limited.get("point_count") or 0)
        limited["original_point_count"] = original_point_count
        limited["internal_test_minimal_points"] = True

        if phase == "coarse":
            original_core_count = int(limited.get("core_point_count") or 0)
            original_outer_probe_count = int(limited.get("outer_probe_point_count") or 0)
            outer_probe_count = min(
                FULL_SCAN_MINIMAL_TEST_COARSE_OUTER_PROBE_POINTS,
                original_outer_probe_count,
            )
            core_limit = min(FULL_SCAN_MINIMAL_TEST_COARSE_GRID_POINTS, original_core_count)
            limited["original_core_point_count"] = original_core_count
            limited["original_outer_probe_point_count"] = original_outer_probe_count
            limited["outer_probe_point_count"] = outer_probe_count
            limited["core_point_count"] = core_limit
            limited["point_count"] = outer_probe_count + core_limit
            limited["test_point_limit"] = {
                "outer_probe": FULL_SCAN_MINIMAL_TEST_COARSE_OUTER_PROBE_POINTS,
                "grid": FULL_SCAN_MINIMAL_TEST_COARSE_GRID_POINTS,
            }
        elif phase == "fine":
            original_core_count = int(limited.get("core_point_count") or original_point_count)
            core_limit = min(FULL_SCAN_MINIMAL_TEST_FINE_POINTS, original_core_count)
            limited["original_core_point_count"] = original_core_count
            limited["core_point_count"] = core_limit
            limited["point_count"] = core_limit
            limited["test_point_limit"] = {"grid": FULL_SCAN_MINIMAL_TEST_FINE_POINTS}
        elif phase == "deviation_a":
            layers = int(limited.get("layers") or max(1, FULL_SCAN_DEVIATION_DIVISIONS - 1))
            original_points_per_layer = int(limited.get("points_per_layer") or 0)
            points_per_layer = min(
                FULL_SCAN_MINIMAL_TEST_DEVIATION_POINTS_PER_LAYER,
                original_points_per_layer or FULL_SCAN_MINIMAL_TEST_DEVIATION_POINTS_PER_LAYER,
            )
            limited["original_points_per_layer"] = original_points_per_layer
            limited["points_per_layer"] = points_per_layer
            limited["point_count"] = layers * points_per_layer
            limited["test_point_limit"] = {
                "layers": layers,
                "points_per_layer": FULL_SCAN_MINIMAL_TEST_DEVIATION_POINTS_PER_LAYER,
            }
        limited_plan.append(limited)
    return limited_plan, sum(int(item.get("point_count") or 0) for item in limited_plan)


def _estimate_full_area_scan_minutes(
    scan_plan,
    dwell_times,
    config_count=FULL_AREA_ESTIMATED_CONFIG_COUNT,
):
    phase_to_dwell = {
        "precheck": dwell_times["coarse_dwell_time"],
        "coarse": dwell_times["coarse_dwell_time"],
        "guard": dwell_times["deviation_dwell_time"],
        "work": dwell_times["fine_dwell_time"],
        "fine": dwell_times["fine_dwell_time"],
        "deviation": dwell_times["deviation_dwell_time"],
        "deviation_a": dwell_times["deviation_dwell_time"],
    }
    total_seconds = 0.0
    for item in scan_plan:
        dwell = float(phase_to_dwell.get(item.get("phase"), dwell_times["coarse_dwell_time"]))
        total_seconds += int(item.get("point_count", 0)) * int(config_count) * dwell
    return max(1, int(math.ceil(total_seconds / 60.0)))


def _auto_location_step(axis_min, axis_max, phase="coarse"):
    """估算定位扫描步径（与 ptz_control._auto_location_step 保持一致，供 API 点数预估使用）。"""
    if phase in ("fine", "fine_4"):
        return 2.0
    if phase in ("mid", "mid_8"):
        return 4.0
    # coarse / coarse_16
    return 8.0


def _location_point_key(point):
    return (round(float(point[0]), 2), round(float(point[1]), 2))


def _dedupe_location_points(points):
    deduped = []
    seen = set()
    for point in points:
        key = _location_point_key(point)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _estimate_location_target_guard_points(pan_range, tilt_range, location_bound):
    pan_min = max(float(location_bound["pan_min"]), float(min(pan_range)))
    pan_max = min(float(location_bound["pan_max"]), float(max(pan_range)))
    tilt_min = max(float(location_bound["tilt_min"]), float(min(tilt_range)))
    tilt_max = min(float(location_bound["tilt_max"]), float(max(tilt_range)))
    if pan_min > pan_max or tilt_min > tilt_max:
        return []

    center = ((pan_min + pan_max) / 2.0, (tilt_min + tilt_max) / 2.0)
    required = [
        (pan_min, tilt_min),
        (pan_min, tilt_max),
        (pan_max, tilt_min),
        (pan_max, tilt_max),
        center,
    ]
    inner_points = max(0, int(LOCATION_SCAN_TARGET_GUARD_INNER_POINTS))
    min_span = max(0.0, float(LOCATION_SCAN_TARGET_GUARD_MIN_SPAN_DEG))
    pan_span = abs(pan_max - pan_min)
    tilt_span = abs(tilt_max - tilt_min)
    pan_can_split = pan_span >= min_span
    tilt_can_split = tilt_span >= min_span
    if inner_points <= 0 or (not pan_can_split and not tilt_can_split):
        return _dedupe_location_points(required)

    grid_side = max(3, int(math.ceil(math.sqrt(inner_points))) + 2)
    pan_samples = (
        [pan_min + pan_span * i / (grid_side + 1) for i in range(1, grid_side + 1)]
        if pan_can_split else [center[0]]
    )
    tilt_samples = (
        [tilt_min + tilt_span * i / (grid_side + 1) for i in range(1, grid_side + 1)]
        if tilt_can_split else [center[1]]
    )
    candidates = [(pan, tilt) for tilt in tilt_samples for pan in pan_samples]
    picked = []
    seed_points = [_location_point_key(point) for point in required]
    while candidates and len(picked) < inner_points:
        refs = seed_points + [_location_point_key(point) for point in picked]
        best_idx = 0
        best_score = -1.0
        for idx, candidate in enumerate(candidates):
            cand_key = _location_point_key(candidate)
            score = min(math.hypot(cand_key[0] - ref[0], cand_key[1] - ref[1]) for ref in refs)
            if score > best_score:
                best_idx = idx
                best_score = score
        picked.append(candidates.pop(best_idx))
    return _dedupe_location_points(required + picked)


def pixel_to_absolute_ptz(px, py, cx, cy, fx, fy, current_pan_deg, current_tilt_deg):
    """像素 → 云台绝对角度（球面模型，与 step3_mapping_verify 一致）。"""
    P = math.radians(current_pan_deg)
    T = math.radians(current_tilt_deg)

    rx = (px - cx) / fx
    ry = (cy - py) / fy
    rz = 1.0
    norm = math.sqrt(rx * rx + ry * ry + rz * rz)
    rx, ry, rz = rx / norm, ry / norm, rz / norm

    cosT, sinT = math.cos(T), math.sin(T)
    tx = rx
    ty = ry * cosT + rz * sinT
    tz = -ry * sinT + rz * cosT

    cosP, sinP = math.cos(P), math.sin(P)
    wx = tx * cosP + tz * sinP
    wy = ty
    wz = -tx * sinP + tz * cosP

    new_pan_deg = math.degrees(math.atan2(wx, wz))
    if new_pan_deg < 0:
        new_pan_deg += 360
    new_tilt_deg = math.degrees(math.atan2(wy, math.sqrt(wx * wx + wz * wz)))
    return new_pan_deg, new_tilt_deg


def absolute_ptz_to_pixel(
    pan_target_deg,
    tilt_target_deg,
    pan0_deg,
    tilt0_deg,
    cx,
    cy,
    fx,
    fy,
    img_w=1920,
    img_h=1080,
):
    """
    绝对云台角度 → 去畸变图像素坐标（pixel_to_absolute_ptz 的逆）。
    Returns (px, py, in_frame)；目标在相机后方时 (None, None, False)。
    """
    P_t, T_t = math.radians(pan_target_deg), math.radians(tilt_target_deg)
    P0, T0 = math.radians(pan0_deg), math.radians(tilt0_deg)

    wx = math.sin(P_t) * math.cos(T_t)
    wy = math.sin(T_t)
    wz = math.cos(P_t) * math.cos(T_t)

    cosP, sinP = math.cos(P0), math.sin(P0)
    tx = wx * cosP - wz * sinP
    ty = wy
    tz = wx * sinP + wz * cosP

    cosT, sinT = math.cos(T0), math.sin(T0)
    rx = tx
    ry = ty * cosT - tz * sinT
    rz = ty * sinT + tz * cosT

    if rz <= 0:
        return None, None, False

    px = round(cx + fx * (rx / rz))
    py = round(cy - fy * (ry / rz))
    in_frame = 0 <= px < img_w and 0 <= py < img_h
    return int(px), int(py), in_frame


def create_app():
    app = Flask(__name__)
    app.json.ensure_ascii = False  # 支持中文 JSON 输出
    CORS(app)

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    init_db()

    try:
        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[web_server] 创建 captures 目录失败: {e}")

    _calib_cache = {"path": None, "K": None, "dist": None, "image_size": None}

    def _resolve_calib_npz_path():
        rel = get_camera_config()["标定文件"]
        p = (BASE_DIR / rel).resolve()
        if not p.exists():
            raise FileNotFoundError(f"标定文件不存在: {p}")
        return p

    def _load_calib_from_npz():
        path = _resolve_calib_npz_path()
        ps = str(path)
        if _calib_cache["path"] == ps and _calib_cache["K"] is not None:
            return _calib_cache["K"], _calib_cache["dist"], _calib_cache["image_size"]
        d = np.load(ps)
        K = np.asarray(d["camera_matrix"], dtype=np.float64)
        dist = np.asarray(d["dist_coeffs"], dtype=np.float64).ravel()
        ims = None
        if "image_size" in getattr(d, "files", []):
            ims = d["image_size"]
        if ims is not None:
            iw, ih = int(ims[0]), int(ims[1])
        else:
            iw, ih = 1920, 1080
        _calib_cache.update({"path": ps, "K": K, "dist": dist, "image_size": (iw, ih)})
        return K, dist, (iw, ih)

    def _undistort_bgr(img_bgr, K, dist, alpha=0.0):
        h, w = img_bgr.shape[:2]
        new_K, _roi = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), alpha)
        out = cv2.undistort(img_bgr, K, dist, None, new_K)
        return out, new_K

    def _intrinsics_for_coord(K, dist, iw, ih):
        """与 step3 一致：在固定分辨率下得到去畸变后的等效 fx,fy,cx,cy。"""
        new_K, _ = cv2.getOptimalNewCameraMatrix(K, dist, (iw, ih), 0.0)
        return (
            float(new_K[0, 0]),
            float(new_K[1, 1]),
            float(new_K[0, 2]),
            float(new_K[1, 2]),
        )

    def _fetch_snapshot_bytes():
        url = get_camera_config()["截图地址"]
        req = urllib.request.Request(url, headers={"User-Agent": "ptz_capture_system/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()

    def _decode_snapshot_jpeg(buf):
        arr = np.frombuffer(buf, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("无法解码快照图像")
        return img

    def _wait_ptz_settled(target_pan, target_tilt, tol_deg=PTZ_ARRIVE_TOL_DEG, timeout=PTZ_ARRIVE_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = r.get(PTZ_STATUS_KEY)
            if not raw:
                time.sleep(PTZ_POLL_INTERVAL)
                continue
            try:
                st = json.loads(raw)
            except Exception:
                time.sleep(PTZ_POLL_INTERVAL)
                continue
            state = st.get("state", "")
            pos = st.get("position") or {}
            cp = pos.get("pan")
            ct = pos.get("tilt")
            if cp is None or ct is None:
                time.sleep(PTZ_POLL_INTERVAL)
                continue
            if state in MOVING_STATES:
                time.sleep(PTZ_POLL_INTERVAL)
                continue
            if abs(float(cp) - float(target_pan)) <= tol_deg and abs(float(ct) - float(target_tilt)) <= tol_deg:
                return True
            time.sleep(PTZ_POLL_INTERVAL)
        return False


    # ------------- 启动时清理僵死项目 -------------
    # 程序重启后 Redis 里可能残留 RUNNING/STOPPING 状态的项目
    # 这些项目的 worker 已经不在了，需要强制结束
    try:
        _startup_project_raw = r.get('history:current_project')
        if _startup_project_raw:
            _startup_project = json.loads(_startup_project_raw)
            _sp_status = _startup_project.get('status', '')
            if _sp_status in ('RUNNING', 'STOPPING'):
                _sp_id = _startup_project.get('project_id', '?')
                _sp_name = _startup_project.get('project_name', '?')
                try:
                    update_project_status(_sp_id, 'STOPPED')
                except Exception:
                    pass
                r.delete('history:current_project')
                print(f"[web_server] ⚠️ 启动时清理僵死项目: {_sp_name} ({_sp_id}) {_sp_status} → STOPPED")
    except Exception as e:
        print(f"[web_server] ⚠️ 启动时清理项目异常: {e}")

    # ------------- 工具函数：统一响应 -------------
    def ok(data=None):
        return jsonify({"code": 0, "data": data or {}, "msg": "ok"})

    def err(msg, http_status=400, app_code=-1, data=None):
        return jsonify({"code": app_code, "data": data or {}, "msg": str(msg)}), http_status

    def _safe_capture_relative_path(value):
        rel = unquote(str(value or "").strip()).replace("\\", "/")
        if (
            not rel
            or rel.startswith("/")
            or rel.strip() != rel
            or any(part in {"", ".", ".."} for part in rel.split("/"))
        ):
            raise ValueError("image_url format error")
        root = CAPTURES_DIR.resolve()
        candidate = (root / Path(*rel.split("/"))).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("image_url format error")
        return rel, candidate

    def _capture_image_path_from_url(image_url):
        if image_url in (None, ""):
            return None
        value = str(image_url).strip()
        parsed = urlparse(value)
        path = parsed.path if parsed.scheme or parsed.netloc else value
        prefix = "/api/v1/camera/images/"
        rel_path = path[len(prefix):] if path.startswith(prefix) else path
        filename, image_path = _safe_capture_relative_path(rel_path)
        if not image_path.is_file():
            raise FileNotFoundError(f"图片不存在: {filename}")
        return image_path

    def _panorama_meta_from_image_url(image_url):
        image_path = _capture_image_path_from_url(image_url)
        if image_path is None:
            raw_meta = r.get(CAMERA_PANORAMA_META_KEY)
            if not raw_meta:
                raise LookupError("无全景图元数据，请先拍摄全景图或传入 image_url")
            return json.loads(raw_meta)
        meta_path = image_path.with_name(image_path.stem + "_meta.json")
        if not meta_path.is_file():
            raise LookupError(f"全景图元数据不存在: {meta_path.name}")
        return json.loads(meta_path.read_text(encoding="utf-8"))

    def _single_capture_angles_from_image_url(image_url):
        image_path = _capture_image_path_from_url(image_url)
        if image_path is None:
            return None
        match = re.match(
            r"^snap_pan(?P<pan>[-+]?(?:\d+(?:\.\d*)?|\.\d+))_tilt(?P<tilt>[-+]?(?:\d+(?:\.\d*)?|\.\d+))_",
            image_path.name,
        )
        if not match:
            raise ValueError("单张图文件名无法解析角度")
        return float(match.group("pan")), float(match.group("tilt"))

    # --- 全面扫描图像上下文解析（委托给 full_scan_image_context 模块） ---

    def _resolve_full_scan_image_context(mode, image_url=None):
        """根据 mode 和可选 image_url 解析全面扫描图像上下文。

        返回规范化后的 dict，包含 mode、image_url、尺寸和转换参数。
        校验失败抛出 ValueError/FileNotFoundError/LookupError。
        """
        VALID_MODES = ("panorama", "single")
        if not isinstance(mode, str) or mode not in VALID_MODES:
            raise ValueError(
                f"mode 参数无效，须为 {'/'.join(VALID_MODES)}，收到: {mode!r}"
            )

        if mode == "panorama":
            return _resolve_panorama_image_context(image_url)
        else:
            return _resolve_single_image_context(image_url)

    def _resolve_panorama_image_context(image_url=None):
        """解析全景模式图像上下文。"""
        if image_url:
            image_path = _capture_image_path_from_url(image_url)
            if image_path is None:
                raise ValueError("image_url 格式错误")
            meta_path = image_path.with_name(image_path.stem + "_meta.json")
            if not meta_path.is_file():
                raise FileNotFoundError(f"全景图元数据不存在: {meta_path.name}")
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception as e:
                raise ValueError(f"全景图元数据读取失败: {e}")
            normalized_url = normalize_image_url(image_path, CAPTURES_DIR)
        else:
            raw_meta = r.get(CAMERA_PANORAMA_META_KEY)
            if not raw_meta:
                raise LookupError("无全景图元数据，请先拍摄全景图或传入 image_url")
            try:
                meta = json.loads(raw_meta)
            except Exception as e:
                raise ValueError(f"Redis 全景图元数据解析失败: {e}")
            meta_image_path = meta.get("image_path")
            if meta_image_path:
                try:
                    normalized_url = normalize_image_url(meta_image_path, CAPTURES_DIR)
                except ValueError:
                    normalized_url = None
            else:
                normalized_url = None

        return resolve_panorama_image_context(meta, normalized_url, captures_dir=CAPTURES_DIR)

    def _resolve_single_image_context(image_url=None):
        """解析单图模式图像上下文。"""
        if image_url:
            image_path = _capture_image_path_from_url(image_url)
            if image_path is None:
                raise ValueError("image_url 格式错误")
            if not SINGLE_CAPTURE_RE.match(image_path.name):
                raise ValueError(
                    f"image_url 不是合法的单图文件: {image_path.name}"
                )
            normalized_url = normalize_image_url(image_path, CAPTURES_DIR)
        else:
            candidates = []
            try:
                candidates = [
                    e for e in CAPTURES_DIR.rglob("snap_pan*_tilt*_*.jpg")
                    if e.is_file()
                ]
            except Exception:
                pass
            if not candidates:
                raise LookupError(
                    "未找到单图文件，请先拍照或传入 image_url"
                )
            candidates.sort(
                key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True
            )
            image_path = candidates[0]
            normalized_url = normalize_image_url(image_path, CAPTURES_DIR)

        # 读取实际图片尺寸（使用 numpy.fromfile + cv2.imdecode 兼容中文路径）
        if cv2 is None:
            raise RuntimeError("OpenCV (cv2) 不可用，无法读取图片尺寸")
        img_bytes = np.fromfile(str(image_path), dtype=np.uint8)
        img = cv2.imdecode(img_bytes, cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(f"无法读取图片: {image_path}")
        height, width = img.shape[:2]
        img = None  # 释放内存

        def _intrinsics_with_actual_size():
            """用实际图片尺寸获取内参，而非标定文件记录的旧尺寸。"""
            K, dist, _ = _load_calib_from_npz()
            return _intrinsics_for_coord(K, dist, width, height)

        ctx = resolve_single_image_context(
            image_path, width, height, intrinsics_fn=_intrinsics_with_actual_size
        )
        ctx["image_url"] = normalized_url
        return ctx

    def _build_default_project_name(scan_type):
        return f"{scan_type}_{time.strftime('%Y%m%d_%H%M%S', time.localtime())}"

    def _serialize_project_for_redis(project):
        if not project:
            return None
        payload = {
            "project_id": project["project_id"],
            "project_name": project["project_name"],
            "scan_type": project["scan_type"],
            "status": project["status"],
            "started_at": project["started_at"],
            "ended_at": project["ended_at"],
            "updated_at": project.get("updated_at"),
        }
        scan_params = project.get("scan_params")
        if isinstance(scan_params, dict) and scan_params.get("scan_id"):
            payload["scan_id"] = scan_params["scan_id"]
        return payload

    def _read_current_project():
        raw = r.get(CURRENT_PROJECT_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _write_current_project(project):
        payload = _serialize_project_for_redis(project)
        if payload is None:
            r.delete(CURRENT_PROJECT_KEY)
            return
        r.set(CURRENT_PROJECT_KEY, json.dumps(payload, ensure_ascii=False))

    def _ensure_no_active_project():
        current = _read_current_project()
        if current and current.get("status") in ("RUNNING", "STOPPING"):
            # STOPPING 超过 120 秒视为僵死，强制结束
            if current.get("status") == "STOPPING":
                updated_at = current.get("updated_at", "")
                if updated_at:
                    try:
                        from datetime import datetime, timezone
                        # 解析 updated_at（格式如 "2026-04-01T08:48:21Z"）
                        updated_time = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        elapsed = (now - updated_time).total_seconds()
                        if elapsed > 120:
                            # 超时，强制结束项目
                            try:
                                update_project_status(current["project_id"], "STOPPED")
                            except Exception:
                                pass
                            _write_current_project(None)
                            return None
                    except Exception:
                        pass
            return current
        return None

    def _start_project_or_raise(scan_type, project_name, scan_params):
        active = _ensure_no_active_project()
        if active:
            raise ValueError(
                f"已有运行中的项目: {active.get('project_name')} ({active.get('scan_type')})，"
                "请先停止当前项目"
            )
        project = create_project(
            project_name=project_name or _build_default_project_name(scan_type),
            scan_type=scan_type,
            scan_params=scan_params,
        )
        _write_current_project(project)
        return project

    def _mark_project_stopping(expected_scan_type):
        current = _read_current_project()
        if not current:
            raise ValueError("当前没有运行中的项目")
        if current.get("scan_type") != expected_scan_type:
            raise ValueError(
                f"当前运行项目模式为 {current.get('scan_type')}，不能用 {expected_scan_type} 停止"
            )
        project = update_project_status(current["project_id"], "STOPPING")
        _write_current_project(project)
        return project

    def _wifi_connect_active():
        """检查是否有 wifi_connect 任务正在运行（非终态），返回 (active, status_dict)。"""
        try:
            raw = r.get(WIFI_CONNECT_STATUS_KEY)
            if not raw:
                return False, None
            status = json.loads(raw)
            if status.get('active') and not status.get('terminal'):
                return True, status
        except Exception:
            pass
        return False, None

    def _preempt_all_tasks(r_obj, reason='wifi_connect_preempt'):
        """WiFi 连接触发的全量 stop 操作：停止全面扫描、定位扫描、独立抓包。"""
        _now = time.time()

        # ── 停止全面扫描 ─────────────────────────────────────────────────────
        _fs_scan_id = r_obj.get('full_scan:active_scan_id')
        if _fs_scan_id:
            r_obj.set('multi_scan:stop_full_area_scan', '1', ex=120)
            r_obj.set('full_scan:stop', json.dumps({
                'scan_id': _fs_scan_id, 'reason': reason, 'ts': _now
            }), ex=120)
            _patch_ptz_status_atomic(r_obj, {
                'full_scan': {
                    'active': False, 'state': 'stopping',
                    'scan_id': _fs_scan_id, 'stop_requested': True,
                    'stop_requested_at': _now, 'terminal': False,
                },
            })
            r_obj.lpush(PTZ_COMMAND_QUEUE, json.dumps({
                'action': 'stop_full_area_scan', 'scan_id': _fs_scan_id
            }))

        # ── 停止定位扫描 ─────────────────────────────────────────────────────
        _ls_scan_id = r_obj.get('location_scan:active_scan_id')
        if _ls_scan_id:
            r_obj.set('location_scan:stop', json.dumps({
                'scan_id': _ls_scan_id, 'reason': reason, 'ts': _now
            }), ex=120)
            r_obj.set('capture:stop', '1', ex=120)
            r_obj.set('location_scan:status', json.dumps({
                'phase': 'idle', 'status': 'stopping',
                'stop_requested': True, 'reason': reason, 'ts': _now
            }))
            _patch_ptz_status_atomic(r_obj, {
                'location_scan': {
                    'active': True, 'state': 'stopping',
                    'scan_id': _ls_scan_id, 'stop_requested': True,
                    'stop_requested_at': _now, 'terminal': False,
                },
            })
            r_obj.lpush(PTZ_COMMAND_QUEUE, json.dumps({
                'action': 'stop_location_scan', 'scan_id': _ls_scan_id
            }))
            r_obj.lpush(CAPTURE_COMMAND_QUEUE, json.dumps({
                'action': 'stop_capture', 'reason': reason
            }))

        # ── 停止独立抓包 ─────────────────────────────────────────────────────
        _cap_running = r_obj.get(CAPTURE_RUNNING_KEY)
        if _cap_running == '1':
            r_obj.set('capture:stop', '1', ex=120)
            r_obj.lpush(CAPTURE_COMMAND_QUEUE, json.dumps({
                'action': 'stop_capture', 'reason': reason
            }))

    def _reject_if_wifi_connect_running():
        """如果 wifi_connect 正在运行，返回 409 错误响应；否则返回 None。"""
        active, _ = _wifi_connect_active()
        if active:
            return err("WiFi 连接任务正在运行，请等待完成或取消", http_status=409,
                       app_code=2001, data={'reason': 'wifi_connect_running'})
        return None

    @app.get('/api/v1/history/projects')
    def api_list_history_projects():
        try:
            limit = int(request.args.get('limit', 100))
        except ValueError:
            return err('limit 参数必须是整数', http_status=400, app_code=2001)

        return ok({"projects": list_projects(limit=limit)})

    @app.get('/api/v1/history/projects/current')
    def api_get_current_project():
        current = _read_current_project()
        if not current:
            return ok({"has_project": False})
        project = get_project(current["project_id"])
        return ok({"has_project": project is not None, "project": project})

    @app.get('/api/v1/history/projects/<project_id>')
    def api_get_history_project(project_id):
        project = get_project(project_id)
        if not project:
            return err('项目不存在', http_status=404, app_code=2004)
        latest_snapshot = get_latest_snapshot(project_id)
        return ok({"project": project, "latest_snapshot": latest_snapshot})

    @app.get('/api/v1/history/projects/<project_id>/snapshots')
    def api_list_history_snapshots(project_id):
        project = get_project(project_id)
        if not project:
            return err('项目不存在', http_status=404, app_code=2004)

        try:
            limit = int(request.args.get('limit', 200))
        except ValueError:
            return err('limit 参数必须是整数', http_status=400, app_code=2001)

        return ok({
            "project": project,
            "snapshots": list_snapshots(project_id, limit=limit),
        })

    @app.get('/api/v1/history/projects/<project_id>/snapshot')
    def api_get_history_snapshot(project_id):
        project = get_project(project_id)
        if not project:
            return err('项目不存在', http_status=404, app_code=2004)

        timestamp = request.args.get('timestamp')
        if timestamp:
            snapshot = get_snapshot_at(project_id, timestamp)
        else:
            snapshot = get_latest_snapshot(project_id)

        if not snapshot:
            return ok({
                "project": project,
                "has_snapshot": False,
                "timestamp": timestamp,
            })

        return ok({
            "project": project,
            "has_snapshot": True,
            "snapshot": snapshot,
            "timestamp": timestamp,
        })

    # ------------- PTZ APIs -------------
    @app.post('/api/v1/ptz/move')
    def api_ptz_move():
        _reject = _reject_if_wifi_connect_running()
        if _reject:
            return _reject
        try:
            payload = request.get_json(force=True)
        except Exception:
            return err('非法 JSON 请求体', http_status=400, app_code=1000)

        try:
            pan = float(payload.get('pan'))
            tilt = float(payload.get('tilt'))
            # 新增：水平角度步长
            pan_step_size = payload.get('pan_step_size', 20.0)
            # 新增：垂直角度步长
            tilt_step_size = payload.get('tilt_step_size', 10.0)
        except Exception:
            return err('pan/tilt 参数缺失或类型错误', http_status=400, app_code=1001)

        # 优先从 Redis 读取 Worker 公布的限位（确保同源），失败则回退环境变量
        try:
            limits_raw = r.get('ptz:limits')
            if limits_raw:
                limits = json.loads(limits_raw)
                PAN_MIN = float(limits.get('pan', {}).get('min', PTZ_LIMIT_PAN_MIN))
                PAN_MAX = float(limits.get('pan', {}).get('max', PTZ_LIMIT_PAN_MAX))
                TILT_MIN = float(limits.get('tilt', {}).get('min', PTZ_LIMIT_TILT_MIN))
                TILT_MAX = float(limits.get('tilt', {}).get('max', PTZ_LIMIT_TILT_MAX))
            else:
                PAN_MIN, PAN_MAX = PTZ_LIMIT_PAN_MIN, PTZ_LIMIT_PAN_MAX
                TILT_MIN, TILT_MAX = PTZ_LIMIT_TILT_MIN, PTZ_LIMIT_TILT_MAX
        except Exception:
            PAN_MIN, PAN_MAX = PTZ_LIMIT_PAN_MIN, PTZ_LIMIT_PAN_MAX
            TILT_MIN, TILT_MAX = PTZ_LIMIT_TILT_MIN, PTZ_LIMIT_TILT_MAX
        if not (PAN_MIN <= pan <= PAN_MAX):
            return err('pan 超出范围', http_status=400, app_code=1002, data={"allowed": [PAN_MIN, PAN_MAX], "got": pan})
        if not (TILT_MIN <= tilt <= TILT_MAX):
            return err('tilt 超出范围', http_status=400, app_code=1003, data={"allowed": [TILT_MIN, TILT_MAX], "got": tilt})

        cmd = {"action": "move_absolute", "pan": pan, "tilt": tilt,"pan_step_size": pan_step_size, "tilt_step_size": tilt_step_size}
        
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        return ok({"queued": True})

    @app.post('/api/v1/ptz/auto_scan')
    def api_ptz_auto_scan():
        try:
            payload = request.get_json(force=True)
        except Exception:
            return err('非法 JSON 请求体', http_status=400, app_code=1000)

        pan_range = payload.get('pan_range')
        tilt_range = payload.get('tilt_range')
        br = business_scan_range_from_redis(r, _HARDWARE_LIMITS_FALLBACK, _CONFIG_DEFAULT_SCAN)
        if 'pan_step' not in payload:
            pan_step = br['pan_step']
        else:
            pan_step = payload.get('pan_step', PTZ_DEFAULT_PAN_STEP)
        if 'tilt_step' not in payload:
            tilt_step = br['tilt_step']
        else:
            tilt_step = payload.get('tilt_step', PTZ_DEFAULT_TILT_STEP)
        # 新增：每一步的停止时间（秒）
        step_delay = payload.get('step_delay', 2.0)
        # 新增：水平角度步长
        pan_step_size = payload.get('pan_step_size', 20.0)
        # 新增：垂直角度步长
        tilt_step_size = payload.get('tilt_step_size', 10.0)

        if pan_range is None:
            pan_range = [br['pan_min'], br['pan_max']]
        if tilt_range is None:
            tilt_range = [br['tilt_min'], br['tilt_max']]
        try:
            pan_min, pan_max = float(pan_range[0]), float(pan_range[1])
            tilt_min, tilt_max = float(tilt_range[0]), float(tilt_range[1])
            pan_step = float(pan_step)
            tilt_step = float(tilt_step)
            step_delay = float(step_delay)
            pan_step_size = float(pan_step_size)
            tilt_step_size = float(tilt_step_size)
        except (TypeError, ValueError, IndexError):
            return err('pan_range/tilt_range/pan_step/tilt_step/step_delay/pan_step_size/tilt_step_size 参数格式错误', http_status=400, app_code=1001)

        # 验证新增参数范围
        if step_delay < 0.1 or step_delay > 60.0:
            return err('step_delay 超出范围 (0.1-60.0秒)', http_status=400, app_code=1004, data={"allowed": [0.1, 60.0], "got": step_delay})
        if pan_step_size < 1.0 or pan_step_size > 90.0:
            return err('pan_step_size 超出范围 (1.0-90.0度)', http_status=400, app_code=1005, data={"allowed": [1.0, 90.0], "got": pan_step_size})
        if tilt_step_size < 1.0 or tilt_step_size > 45.0:
            return err('tilt_step_size 超出范围 (1.0-45.0度)', http_status=400, app_code=1006, data={"allowed": [1.0, 45.0], "got": tilt_step_size})

        # 优先从 Redis 读取 Worker 公布的限位（确保同源），失败则回退环境变量
        try:
            limits_raw = r.get('ptz:limits')
            if limits_raw:
                limits = json.loads(limits_raw)
                PAN_MIN = float(limits.get('pan', {}).get('min', PTZ_LIMIT_PAN_MIN))
                PAN_MAX = float(limits.get('pan', {}).get('max', PTZ_LIMIT_PAN_MAX))
                TILT_MIN = float(limits.get('tilt', {}).get('min', PTZ_LIMIT_TILT_MIN))
                TILT_MAX = float(limits.get('tilt', {}).get('max', PTZ_LIMIT_TILT_MAX))
            else:
                PAN_MIN, PAN_MAX = PTZ_LIMIT_PAN_MIN, PTZ_LIMIT_PAN_MAX
                TILT_MIN, TILT_MAX = PTZ_LIMIT_TILT_MIN, PTZ_LIMIT_TILT_MAX
        except Exception:
            PAN_MIN, PAN_MAX = PTZ_LIMIT_PAN_MIN, PTZ_LIMIT_PAN_MAX
            TILT_MIN, TILT_MAX = PTZ_LIMIT_TILT_MIN, PTZ_LIMIT_TILT_MAX
        if not (PAN_MIN <= pan_min <= pan_max <= PAN_MAX):
            return err('pan_range 超出允许范围', http_status=400, app_code=1002, data={"allowed": [PAN_MIN, PAN_MAX], "got": [pan_min, pan_max]})
        if not (TILT_MIN <= tilt_min <= tilt_max <= TILT_MAX):
            return err('tilt_range 超出允许范围', http_status=400, app_code=1003, data={"allowed": [TILT_MIN, TILT_MAX], "got": [tilt_min, tilt_max]})

        cmd = {
            "action": "start_auto_scan", 
            "pan_range": [pan_min, pan_max], 
            "tilt_range": [tilt_min, tilt_max], 
            "pan_step": pan_step, 
            "tilt_step": tilt_step,
            "step_delay": step_delay,
            "pan_step_size": pan_step_size,
            "tilt_step_size": tilt_step_size
        }
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        return ok({"queued": True})

    @app.post('/api/v1/ptz/stop_scan')
    def api_ptz_stop_scan():
        cmd = {"action": "stop_auto_scan"}
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        return ok({"queued": True})

    @app.post('/api/v1/ptz/stop')
    def api_ptz_stop():
        """停止云台所有动作，包括移动和扫描"""
        cmd = {"action": "stop"}
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        return ok({"queued": True})

    # ==================== 智能扫描API ====================
    
# 简化智能扫描API
    @app.post('/api/v1/ptz/intelligent_scan/start')
    def api_start_intelligent_scan():
        """
        启动智能自适应扫描API - 只需要传递扫描范围
        """
        try:
            payload = request.get_json(force=True)
        except Exception:
            return err('非法 JSON 请求体', http_status=400, app_code=1000)
        
        # 只获取必需参数
        pan_range = payload.get('pan_range')
        tilt_range = payload.get('tilt_range')
         # 🔥 新增：全天候模式参数
        retry_on_no_signal = payload.get('retry_on_no_signal', False)
        
        # 参数验证
        if not pan_range or len(pan_range) != 2:
            return err('请提供有效的水平扫描范围 [最小角度, 最大角度]', http_status=400, app_code=1001)
        
        if not tilt_range or len(tilt_range) != 2:
            return err('请提供有效的垂直扫描范围 [最小角度, 最大角度]', http_status=400, app_code=1002)
        
        try:
            pan_min, pan_max = float(pan_range[0]), float(pan_range[1])
            tilt_min, tilt_max = float(tilt_range[0]), float(tilt_range[1])
        except (ValueError, TypeError):
            return err('扫描范围参数必须是数字', http_status=400, app_code=1003)
        
        if pan_min >= pan_max or tilt_min >= tilt_max:
            return err('扫描范围格式错误，最小值应小于最大值', http_status=400, app_code=1004)
        
        # 构建简化的命令
        cmd = {
            "action": "start_intelligent_scan",
            "pan_range": [pan_min, pan_max],
            "tilt_range": [tilt_min, tilt_max],
            "retry_on_no_signal": retry_on_no_signal  # 🔥 添加全天候模式参数
        }
        
        # 发送命令到PTZ队列
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        
        return ok({
            "queued": True,
            "message": "智能自适应扫描已启动",
            "scan_config": {
                "pan_range": [pan_min, pan_max],
                "tilt_range": [tilt_min, tilt_max],
                "scan_area": f"{pan_max-pan_min:.1f}° × {tilt_max-tilt_min:.1f}°",
                "retry_on_no_signal": retry_on_no_signal
            },
            "tips": "系统将自动计算最优步长和扫描策略，从粗到细逐步精确定位最强信号位置"
        })
        
    @app.post('/api/v1/ptz/intelligent_scan/stop')
    def api_stop_intelligent_scan():
        """停止智能自适应扫描API"""
        try:
            cmd = {"action": "stop_intelligent_scan"}
            r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
            
            return ok({
                "queued": True,
                "message": "智能扫描停止命令已发送"
            })
            
        except Exception as e:
            return err(f'停止智能扫描失败: {str(e)}', http_status=500, app_code=-1)

    @app.get('/api/v1/ptz/intelligent_scan/current_round')
    def api_get_current_round():
        """
        获取当前智能扫描轮次信息API
        
        返回当前轮次的详细信息，包括轮次号、范围、步长等
        前端可以根据这些信息知道从哪个ap:轮次_x_步长_步长格式的Redis键读取数据
        """
        try:
            # 检查智能扫描是否激活
            is_active = r.get("intelligent_scan:active") == '1'
            
            if not is_active:
                return ok({
                    "has_current_round": False,
                    "message": "当前没有进行中的智能扫描"
                })
            
            # 从Redis读取当前状态信息
            status_raw = r.get("intelligent_scan:status")
            
            if not status_raw:
                return ok({
                    "has_current_round": True,  # 激活但无详细状态
                    "message": "智能扫描已激活，正在等待状态信息",
                    "round_info": {
                        "current_round": 1,
                        "range": {},
                        "status": "initializing"
                    }
                })
            
            # 解析状态信息
            status_info = json.loads(status_raw)
            
            return ok({
                "has_current_round": True,
                "round_info": {
                    "current_round": status_info.get("current_round", 1),
                    "range": status_info.get("range", {}),
                    "started_at": status_info.get("started_at"),
                    "last_updated": status_info.get("last_updated"),
                    "status": "running"
                },
                "usage_hint": "智能扫描使用全局网格存储数据，键名格式: p:{grid_id}_1.0_1.0"
            })
            
        except Exception as e:
            return err(f'获取轮次信息失败: {str(e)}', http_status=500, app_code=2004)

    @app.get('/api/v1/ptz/intelligent_scan/status')
    def api_get_intelligent_scan_status():
        """获取智能扫描状态API"""
        try:
            # 检查智能扫描激活状态
            is_active = r.get("intelligent_scan:active") == '1'
            
            if not is_active:
                return ok({
                    "active": False,
                    "message": "智能扫描未激活"
                })
            
            # 获取智能扫描详细状态
            status_raw = r.get("intelligent_scan:status")
            ptz_status_raw = r.get(PTZ_STATUS_KEY)
            
            # 解析状态信息
            status_info = {}
            if status_raw:
                status_info = json.loads(status_raw)
            
            ptz_status = {}
            if ptz_status_raw:
                ptz_status = json.loads(ptz_status_raw)
            
            return ok({
                "active": True,
                "current_round": status_info.get("current_round", 1),
                "range": status_info.get("range", {}),
                "started_at": status_info.get("started_at"),
                "last_updated": status_info.get("last_updated"),
                "ptz_state": ptz_status.get('state', 'unknown'),
                "position": ptz_status.get('position', {}),
                "message": "智能扫描正在运行中"
            })
            
        except Exception as e:
            return err(f'获取智能扫描状态失败: {str(e)}', http_status=500, app_code=-1)

    @app.get('/api/v1/ptz/status')
    def api_ptz_status():
        s = r.get(PTZ_STATUS_KEY)
        try:
            status = json.loads(s) if s else {}
        except Exception:
            return ok({"raw": s})
        try:
            _full_scan_was_terminal = bool(
                isinstance(status.get('full_scan'), dict)
                and status['full_scan'].get('terminal') is True
            )
            _full_scan_stop_raw = r.get('full_scan:stop')
            _full_scan_active_id = r.get('full_scan:active_scan_id')
            _current_project = _read_current_project()
            status = _project_full_scan_stop_status(
                status,
                _full_scan_stop_raw,
                _full_scan_active_id,
                current_project=_current_project,
            )
            _projected_full_scan = status.get('full_scan')
            if (
                not _full_scan_was_terminal
                and isinstance(_projected_full_scan, dict)
                and _projected_full_scan.get('terminal') is True
            ):
                # worker 已结束但权威状态漏写时，查询接口执行一次幂等自愈，
                # 同时释放遗留的 STOPPING 项目门禁。
                _patch_ptz_status_atomic(r, {'full_scan': _projected_full_scan})
                _delete_key_if_value_matches(
                    r,
                    'full_scan:active_scan_id',
                    _projected_full_scan.get('scan_id'),
                )
                _stale_project = _current_project
                if (
                    _stale_project
                    and _stale_project.get('scan_type') == 'full_area'
                    and _stale_project.get('status') == 'STOPPING'
                ):
                    try:
                        update_project_status(_stale_project['project_id'], 'STOPPED')
                    finally:
                        _write_current_project(None)
        except Exception:
            pass
        try:
            capture_running_raw = r.get(CAPTURE_RUNNING_KEY)
            capture_running = capture_running_raw == '1'
            capture_active = capture_running
            capture_phase = 'capturing' if capture_running else 'idle'
            capture_detail_raw = r.get(CAPTURE_STATUS_KEY)
            capture_detail = {}
            if capture_detail_raw:
                try:
                    capture_detail = json.loads(capture_detail_raw)
                except Exception:
                    capture_detail = {}
            cab = status.get('capture_at_best')
            # 合并 capture:status 的实时状态到 capture_at_best（capture_worker 不再写 ptz:current_status）
            if isinstance(cab, dict) and cab.get('active') and isinstance(capture_detail, dict):
                if 'active' in capture_detail:
                    cab['active'] = capture_detail['active']
                if 'phase' in capture_detail:
                    cab['phase'] = capture_detail['phase']
                if 'updated_at' in capture_detail:
                    cab['updated_at'] = capture_detail['updated_at']
                if 'stopped_at' in capture_detail:
                    cab['stopped_at'] = capture_detail['stopped_at']
            if capture_running and isinstance(cab, dict) and cab.get('active'):
                capture_active = True
                capture_phase = str(cab.get('phase') or capture_phase)
                cab['active'] = True
                cab.setdefault('phase', 'capturing')
            capture_payload = dict(capture_detail) if isinstance(capture_detail, dict) else {}
            capture_payload.update({
                'active': capture_active,
                'running': capture_running,
                'phase': capture_phase,
            })
            status['capture'] = capture_payload
            status['capture_running'] = capture_running
        except Exception:
            pass
        # 合并 wifi_connect:status（不写入 ptz:current_status，仅在此处拼装）
        try:
            wc_raw = r.get('wifi_connect:status')
            if wc_raw:
                wc_status = json.loads(wc_raw)
                # 补充 elapsed_seconds
                if wc_status.get('active') and wc_status.get('started_at'):
                    wc_status['elapsed_seconds'] = round(time.time() - wc_status['started_at'], 1)
                status['wifi_connect'] = wc_status
        except Exception:
            pass
        return ok(status)

    # ------------- Capture APIs -------------
    @app.post('/api/v1/capture/start')
    def api_capture_start():
        _reject = _reject_if_wifi_connect_running()
        if _reject:
            return _reject
        try:
            payload = request.get_json(force=True)
        except Exception:
            return err('非法 JSON 请求体')

        # 必填参数
        channel = payload.get('channel')
        bandwidth = payload.get('bandwidth')
        target_mac = payload.get('target_mac')
        if channel is None or bandwidth is None or not target_mac:
            return err('channel/bandwidth/target_mac 为必填', http_status=400)

        # 构造 tcpdump 过滤：只过滤 TA（wlan ta）
        target_mac_l = ''.join(str(target_mac).split()).lower()
        if not target_mac_l:
            return err('target_mac 参数不能为空', http_status=400)
        filter_expr = f"wlan ta {target_mac_l}"

        cmd = {
            "action": "start_capture",
            "channel": channel,
            "bandwidth": bandwidth,
            "target_mac": target_mac_l,
        }
        r.lpush(CAPTURE_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        return ok({"queued": True, "filter": filter_expr})

    @app.post('/api/v1/capture/stop')
    def api_capture_stop():
        cmd = {"action": "stop_capture"}
        r.set('capture:stop', '1', ex=120)
        r.lpush(CAPTURE_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        # 同步等待：阻塞直到 capture:running 变为 '0' 或超时
        timeout_s = float(request.args.get('timeout', '8.0'))
        start_ts = time.time()
        running = None
        while time.time() - start_ts < timeout_s:
            try:
                running = r.get('capture:running')
            except Exception:
                running = None
            if running == '0':
                return ok({"stopped": True})
            time.sleep(0.2)
        return err('停止抓包超时', http_status=504, app_code=2001, data={"running": running})

    @app.post('/api/v1/capture/save_pcap')
    def api_capture_save_pcap():
        _reject = _reject_if_wifi_connect_running()
        if _reject:
            return _reject
        try:
            payload = request.get_json(force=True)
        except Exception:
            return err('非法 JSON 请求体')

        # 必填参数
        channel = payload.get('channel')
        bandwidth = payload.get('bandwidth')
        target_mac = payload.get('target_mac')
        pcap_filename = payload.get('pcap_filename')
        
        if channel is None or bandwidth is None or not target_mac or not pcap_filename:
            return err('channel/bandwidth/target_mac/pcap_filename 为必填', http_status=400)

        # 验证文件名格式
        if not pcap_filename.endswith('.pcap'):
            pcap_filename += '.pcap'
        
        # 构造 tcpdump 过滤：只过滤 TA（wlan ta）
        target_mac_l = ''.join(str(target_mac).split()).lower()
        if not target_mac_l:
            return err('target_mac 参数不能为空', http_status=400)
        filter_expr = f"wlan ta {target_mac_l}"

        cmd = {
            "action": "save_pcap",
            "channel": channel,
            "bandwidth": bandwidth,
            "target_mac": target_mac_l,
            "pcap_filename": pcap_filename,
        }
        r.lpush(CAPTURE_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        return ok({"queued": True, "filter": filter_expr, "pcap_file": f"/mnt/data/{pcap_filename}"})

    @app.post('/api/v1/capture/clear_data')
    def api_capture_clear_data():
        """清理抓包数据流和网格数据"""
        try:
            clear_type = request.get_json().get('type', 'all') if request.is_json else 'all'
            
            cleared_items = []
            
            # 清理 Redis Stream 数据
            stream_result = r.delete(CAPTURE_DATA_STREAM)
            if stream_result:
                cleared_items.append("抓包数据流")
            
            if clear_type in ['all', 'global']:
                # 🔥 新增：清理全局网格数据（p:系列）
                p_keys = r.keys("p:*")
                if p_keys:
                    r.delete(*p_keys)
                    cleared_items.append(f"{len(p_keys)}个全局网格")
            
            if clear_type in ['all', 'intelligent']:
                # 清理智能扫描网格（ap:系列）
                ap_keys = r.keys("ap:*")
                if ap_keys:
                    r.delete(*ap_keys)
                    cleared_items.append(f"{len(ap_keys)}个智能扫描网格")
            
            # 🔥 移除RSSI数据列表清理（已改为直接入库策略，不需要分流列表）
            # 旧的分流策略已淘汰：["manual:rssi_data_list", "auto:rssi_data_list", "capture:rssi_data_list"]
            
            if cleared_items:
                message = f"已清理: {', '.join(cleared_items)}"
                return ok({"cleared": True, "message": message, "type": clear_type})
            else:
                return ok({"cleared": False, "message": "没有找到需要清理的数据"})
                
        except Exception as e:
            return err(f'清理数据失败: {str(e)}', http_status=500, app_code=2002)

    @app.post('/api/v1/ptz/check_config_update')
    def api_check_config_update():
        """手动触发Redis配置检查更新API"""
        try:
            # 发送配置检查命令
            command = {"action": "check_config_update"}
            r.lpush(PTZ_COMMAND_QUEUE, json.dumps(command))
            
            # 等待响应
            response_tuple = r.brpop('ptz:response', timeout=10)
            if response_tuple:
                response = json.loads(response_tuple[1])
                if response.get('action') == 'check_config_update':
                    if response.get('success'):
                        return ok({
                            "message": response.get('message', '配置检查完成'),
                            "updated": 'Redis配置已更新' in response.get('message', ''),
                            "response": response
                        })
                    else:
                        return err(f"配置检查失败: {response.get('error', '未知错误')}", 
                                 http_status=500, app_code=3001)
                else:
                    return err("收到意外响应", http_status=500, app_code=3002)
            else:
                return err("配置检查超时，PTZ服务可能繁忙", http_status=408, app_code=3003)
                
        except Exception as e:
            return err(f'配置检查请求失败: {str(e)}', http_status=500, app_code=3000)

    @app.post('/api/v1/ptz/migrate_grid_data')
    def api_migrate_grid_data():
        """🔄 网格数据迁移API - 将旧步径数据迁移到新步径"""
        try:
            if not request.is_json:
                return err('请求必须是JSON格式', http_status=400, app_code=1001)
            
            data = request.get_json()
            
            # 参数验证
            required_params = ['pan_range', 'tilt_range', 'new_pan_step', 'new_tilt_step']
            for param in required_params:
                if param not in data:
                    return err(f'缺少必需参数: {param}', http_status=400, app_code=1002)
            
            pan_range = data['pan_range']
            tilt_range = data['tilt_range']
            new_pan_step = float(data['new_pan_step'])
            new_tilt_step = float(data['new_tilt_step'])
            
            # 参数范围验证
            if not (isinstance(pan_range, list) and len(pan_range) == 2):
                return err('pan_range必须是包含两个元素的数组', http_status=400, app_code=1003)
            
            if not (isinstance(tilt_range, list) and len(tilt_range) == 2):
                return err('tilt_range必须是包含两个元素的数组', http_status=400, app_code=1003)
            
            if new_pan_step <= 0 or new_tilt_step <= 0:
                return err('步径必须大于0', http_status=400, app_code=1003)
            
            if new_pan_step < 0.2 or new_tilt_step < 0.2:
                return err('步径不能小于0.2度（硬件限制）', http_status=400, app_code=1003)
            
            # 发送迁移命令到PTZ控制进程
            migrate_cmd = {
                "action": "migrate_grid_data",
                "pan_range": pan_range,
                "tilt_range": tilt_range,
                "new_pan_step": new_pan_step,
                "new_tilt_step": new_tilt_step,
                "timestamp": time.time()
            }
            
            r.lpush(PTZ_COMMAND_QUEUE, json.dumps(migrate_cmd, ensure_ascii=False))
            
            return ok({
                "queued": True,
                "message": f"网格迁移命令已发送：{new_pan_step}°×{new_tilt_step}°",
                "migration_params": {
                    "pan_range": pan_range,
                    "tilt_range": tilt_range,
                    "new_pan_step": new_pan_step,
                    "new_tilt_step": new_tilt_step
                }
            })
            
        except ValueError as e:
            return err(f'参数格式错误: {str(e)}', http_status=400, app_code=1003)
        except Exception as e:
            return err(f'网格迁移失败: {str(e)}', http_status=500, app_code=2003)

    # ==================== 多点扫描与全面扫描 API ====================
    
    @app.post('/api/v1/ptz/initial_scan/start')
    def api_start_initial_scan():
        """初始MAC扫描 - 在固定点位发现所有可用的MAC地址"""
        try:
            payload = request.get_json(force=True) if request.is_json else {}
        except Exception:
            return err('非法 JSON 请求体', http_status=400, app_code=1000)
        
        # 可选参数
        dwell_time = payload.get('dwell_time', 7)  # 默认7秒，确保足够采集信标
        configs = payload.get('configs')  # 如果不提供，使用默认69个配置
        
        try:
            dwell_time = float(dwell_time)
            if dwell_time < 1 or dwell_time > 10:
                return err('dwell_time 必须在 1-10 秒之间', http_status=400, app_code=1001)
        except (ValueError, TypeError):
            return err('dwell_time 必须是数字', http_status=400, app_code=1002)
        
        # 构建命令
        # 防重入：如果初始扫描正在进行中，拒绝重复发起
        if r.get('multi_scan:initial_scanning') == '1':
            return err('初始扫描正在进行中，请等待完成', http_status=409, app_code=2010)
        
        cmd = {
            "action": "start_initial_scan",
            "dwell_time": dwell_time
        }
        if configs:
            cmd['configs'] = configs
        
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        
        return ok({
            "queued": True,
            "message": "初始MAC扫描已启动",
            "config": {
                "dwell_time": dwell_time,
                "configs_count": len(configs) if configs else 69
            }
        })
    
    @app.post('/api/v1/ptz/initial_scan/stop')
    def api_stop_initial_scan():
        """停止初始MAC扫描"""
        # ① 直接写 Redis 标志位 —— 让阻塞在 step8 轮询中的 ptz_worker 立刻感知
        #    （ptz_worker 阻塞期间无法读命令队列，仅靠命令队列无法及时停止）
        r.set('multi_scan:stop_initial_scan', '1', ex=120)
        r.delete('multi_scan:initial_scanning')  # 清除防重入锁
        # ② 同时塞入命令队列 —— 让 ptz_worker 下次空闲时也能做状态收尾
        cmd = {"action": "stop_initial_scan"}
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        return ok({"queued": True, "message": "停止命令已发送"})
    
    @app.get('/api/v1/ptz/initial_scan/result')
    def api_get_initial_scan_result():
        """获取初始扫描结果"""
        try:
            target_macs = r.get('multi_scan:target_macs')
            initial_position = r.get('multi_scan:initial_position')
            
            if not target_macs:
                return ok({
                    "has_result": False,
                    "message": "未找到扫描结果，请先执行初始扫描"
                })
            
            return ok({
                "has_result": True,
                "target_macs": json.loads(target_macs),
                "initial_position": json.loads(initial_position) if initial_position else None,
                "mac_count": len(json.loads(target_macs))
            })
        except Exception as e:
            return err(f'获取扫描结果失败: {str(e)}', http_status=500, app_code=-1)
    
    @app.post('/api/v1/ptz/multi_point_scan/start')
    def api_start_multi_point_scan():
        """
        多点扫描启动接口（持续重复模式）

        Body 参数：
            mode        : "auto"（默认）| "manual"
                          auto   — MAC 来自初始扫描结果，按 MULTI_SCAN_MAC_REFRESH_ROUNDS 定期刷新
                          manual — 由调用方直接指定 MAC 列表，不触发初始扫描
            manual_macs : list，仅 manual 模式有效
                          格式：[{"mac": "xx:xx:xx:xx:xx:xx", "channel": 6, "bandwidth": "HT20"}, ...]
                          channel/bandwidth 可选；未填时系统自动探测
            pan_range   : [min, max]，默认 [-180, 180]
            tilt_range  : [min, max]，默认 [-30, 90]
            pan_step    : 水平步进（度），默认 10.0
            tilt_step   : 垂直步进（度），默认 10.0
            dwell_time  : 每个点位停留采集时长（秒），不填则使用后端 MULTI_SCAN_DWELL_TIME 常量
            extend_time : 无信号时延长采集时长（秒），不填则使用后端 MULTI_SCAN_EXTEND_TIME 常量
        """
        try:
            payload = request.get_json(force=True)
        except Exception:
            return err('非法 JSON 请求体', http_status=400, app_code=1000)

        # ── 扫描模式 ──────────────────────────────────────────────────────────
        mode = payload.get('mode', 'auto')
        project_name = payload.get('project_name')
        if mode not in ('auto', 'manual'):
            return err('mode 参数无效，仅支持 "auto" 或 "manual"',
                       http_status=400, app_code=1004)

        manual_macs = payload.get('manual_macs', [])
        if mode == 'manual' and not manual_macs:
            return err('manual 模式必须提供 manual_macs 列表',
                       http_status=400, app_code=1005)

        # ── 扫描范围参数 ──────────────────────────────────────────────────────
        pan_range  = payload.get('pan_range',  [-180, 180])
        tilt_range = payload.get('tilt_range', [-30, 90])
        pan_step   = payload.get('pan_step',   10.0)
        tilt_step  = payload.get('tilt_step',  10.0)
        # dwell_time/extend_time 不填时传 None，让 ptz_control 使用自身常量
        dwell_time  = payload.get('dwell_time',  None)
        extend_time = payload.get('extend_time', None)

        try:
            pan_min, pan_max   = float(pan_range[0]),  float(pan_range[1])
            tilt_min, tilt_max = float(tilt_range[0]), float(tilt_range[1])
            pan_step  = float(pan_step)
            tilt_step = float(tilt_step)
            if dwell_time  is not None: dwell_time  = float(dwell_time)
            if extend_time is not None: extend_time = float(extend_time)
        except (TypeError, ValueError, IndexError):
            return err('参数格式错误', http_status=400, app_code=1001)

        if pan_min >= pan_max or tilt_min >= tilt_max:
            return err('范围格式错误，最小值应小于最大值', http_status=400, app_code=1002)

        # auto 模式前置条件：必须有初始扫描结果
        if mode == 'auto' and not r.get('multi_scan:target_macs'):
            return err('auto 模式请先执行初始MAC扫描（start_initial_scan）',
                       http_status=400, app_code=1003)

        # ── 构建命令 ──────────────────────────────────────────────────────────
        cmd = {
            "action":      "start_multi_point_scan",
            "mode":        mode,
            "manual_macs": manual_macs,
            "pan_range":   [pan_min, pan_max],
            "tilt_range":  [tilt_min, tilt_max],
            "pan_step":    pan_step,
            "tilt_step":   tilt_step,
        }
        # 仅在调用方明确传值时才覆盖后端常量
        if dwell_time  is not None: cmd["dwell_time"]  = dwell_time
        if extend_time is not None: cmd["extend_time"] = extend_time

        project_scan_params = {
            "mode": mode,
            "manual_macs": manual_macs,
            "pan_range": [pan_min, pan_max],
            "tilt_range": [tilt_min, tilt_max],
            "pan_step": pan_step,
            "tilt_step": tilt_step,
            "dwell_time": dwell_time,
            "extend_time": extend_time,
        }

        try:
            project = _start_project_or_raise(
                scan_type="multi_point",
                project_name=project_name,
                scan_params=project_scan_params,
            )
        except ValueError as e:
            return err(str(e), http_status=409, app_code=2002)

        try:
            r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        except Exception as e:
            finish_project(project["project_id"], status="FAILED")
            _write_current_project(None)
            return err(f'启动多点扫描失败: {str(e)}', http_status=500, app_code=-1)

        point_count = _count_scan_points(
            [pan_min, pan_max],
            [tilt_min, tilt_max],
            pan_step,
            tilt_step,
        )

        return ok({
            "queued":  True,
            "message": f"多点扫描已启动（{mode} 模式，持续重复）",
            "config": {
                "project_id":       project["project_id"],
                "project_name":     project["project_name"],
                "mode":             mode,
                "manual_macs_count": len(manual_macs) if mode == 'manual' else None,
                "pan_range":        [pan_min, pan_max],
                "tilt_range":       [tilt_min, tilt_max],
                "pan_step":         pan_step,
                "tilt_step":        tilt_step,
                "estimated_points": point_count,
            }
        })
    
    @app.post('/api/v1/ptz/multi_point_scan/stop')
    def api_stop_multi_point_scan():
        """停止多点扫描"""
        try:
            project = _mark_project_stopping("multi_point")
        except ValueError as e:
            return err(str(e), http_status=409, app_code=2003)

        # ① 直接写 Redis 标志位 —— 让正在阻塞执行多点扫描的 ptz_worker 立刻感知到
        #    （ptz_worker 阻塞期间无法读命令队列，仅靠命令队列无法及时停止）
        r.set('multi_scan:stop_multi_point_scan', '1', ex=120)
        # ② 同时塞入命令队列 —— 让 ptz_worker 下次空闲时也能感知并做状态更新
        cmd = {"action": "stop_multi_point_scan"}
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        return ok({
            "queued": True,
            "message": "停止命令已发送",
            "project_id": project["project_id"],
        })

    
    @app.get('/api/v1/ptz/multi_point_scan/result')
    def api_get_multi_point_scan_result():
        """获取多点扫描结果"""
        try:
            # 获取所有点位数据
            point_keys = r.keys('multi_scan:point_*')
            
            if not point_keys:
                return ok({
                    "has_result": False,
                    "message": "未找到扫描结果，请先执行多点扫描"
                })
            
            results = {}
            for key in point_keys:
                point_data = r.get(key)
                if point_data:
                    results[key] = json.loads(point_data)
            
            return ok({
                "has_result": True,
                "points_count": len(results),
                "results": results
            })
        except Exception as e:
            return err(f'获取扫描结果失败: {str(e)}', http_status=500, app_code=-1)
    
    @app.post('/api/v1/ptz/full_area_scan/start')
    def api_start_full_area_scan():
        """
        全面扫描启动接口（时间窗口/间隔模式）

        Body / Query 参数：
            mode          : 必填，图像上下文模式，"panorama" / "single"
            image_url     : 可选，显式指定图片；不传时按 mode 自动选择最新图片
            target_ranges : 必填，当前只允许一个像素目标区
            wifi_mode     : 可选，"2.4" / "5"；缺失、null 或空字符串表示双频
            停留时长由 config.json 的「全面扫描」配置决定
            scan_time_limit : 可选，扫描窗口时长，单位分钟
            time_interval   : 可选，窗口/轮次之间的等待间隔，单位秒

        说明：
            - 前端不再控制步径
            - 外层范围只读取 Redis gimbal:default_config 的 work_x_range/work_y_range
            - 旧 pan/tilt/work_ranges 角度字段不参与路径或估算
            - 未传 scan_time_limit/time_interval 时，只完整执行一轮全面扫描
            - mode 为 "panorama" 时使用最新全景图；为 "single" 时使用最新单图
            - 显式 image_url 优先于自动选择最新图片
        """
        payload = request.get_json(silent=True)
        if payload is None:
            if request.get_data(cache=False):
                return err('非法 JSON 请求体', http_status=400, app_code=1000)
            payload = {}
        if not isinstance(payload, dict):
            return err('非法 JSON 请求体', http_status=400, app_code=1000)

        def _optional_param(name):
            if name in payload:
                return payload.get(name)
            return request.args.get(name)

        def _optional_bool_param(name):
            raw_value = _optional_param(name)
            if raw_value in (None, ""):
                return False
            if isinstance(raw_value, bool):
                return raw_value
            value = str(raw_value).strip().lower()
            if value in ("1", "true", "yes", "on", "y"):
                return True
            if value in ("0", "false", "no", "off", "n"):
                return False
            raise ValueError(f"{name} 参数须为布尔值")

        project_name = payload.get('project_name')
        try:
            full_scan_wifi_mode, full_scan_configs = build_full_scan_wifi_configs(
                _optional_param('wifi_mode')
            )
        except ValueError as e:
            return err(str(e), http_status=400, app_code=1001)
        try:
            internal_minimal_point_test = _optional_bool_param("test_minimal_points")
        except ValueError as e:
            return err(str(e), http_status=400, app_code=1001)
        if internal_minimal_point_test:
            if not _FULL_SCAN_CONFIG.get("允许最小点位内部测试", False):
                return err("最小点位内部测试未启用", http_status=403, app_code=2003)
        internal_test_channel = _optional_param("test_channel")
        internal_single_channel_test = internal_test_channel not in (None, "")
        if internal_single_channel_test:
            if not _FULL_SCAN_CONFIG.get("允许单信道内部测试", False):
                return err("单信道内部测试未启用", http_status=403, app_code=2003)
            try:
                full_scan_wifi_mode, full_scan_configs = (
                    build_full_scan_single_channel_test_config(internal_test_channel)
                )
                internal_test_channel = full_scan_configs[0]["channel"]
            except ValueError as e:
                return err(str(e), http_status=400, app_code=1001)

        # --- 图像上下文解析 ---
        scan_mode = _optional_param('mode')
        if scan_mode is None:
            return err("mode 为必填参数，须为 panorama / single", http_status=400, app_code=1001)
        scan_image_url = _optional_param('image_url')
        try:
            image_context = _resolve_full_scan_image_context(scan_mode, scan_image_url)
        except ValueError as e:
            return err(str(e), http_status=400, app_code=1001)
        except FileNotFoundError as e:
            return err(str(e), http_status=404, app_code=2004)
        except LookupError as e:
            return err(str(e), http_status=404, app_code=2001)
        except RuntimeError as e:
            return err(str(e), http_status=500, app_code=5004)

        # --- 像素范围解析与校验 ---
        img_ctx_width = image_context.get("width")
        img_ctx_height = image_context.get("height")
        if not img_ctx_width or not img_ctx_height:
            return err("image_context 缺少有效 width/height", http_status=500, app_code=5004)

        # 外层像素范围：从 Redis gimbal:default_config 读取
        try:
            _raw_wx = r.hget("gimbal:default_config", "work_x_range")
            _raw_wy = r.hget("gimbal:default_config", "work_y_range")
        except Exception:
            _raw_wx, _raw_wy = None, None

        if _raw_wx is None or _raw_wy is None:
            return err(
                "Redis gimbal:default_config 缺少 work_x_range / work_y_range，请先设置外层像素范围",
                http_status=400, app_code=1002,
            )

        try:
            wx_arr = json.loads(_raw_wx) if isinstance(_raw_wx, str) else _raw_wx
            wy_arr = json.loads(_raw_wy) if isinstance(_raw_wy, str) else _raw_wy
            work_x_range = [float(wx_arr[0]), float(wx_arr[1])]
            work_y_range = [float(wy_arr[0]), float(wy_arr[1])]
        except (TypeError, ValueError, IndexError):
            return err("work_x_range / work_y_range 格式错误，须为 [min, max]", http_status=400, app_code=1001)

        # 归一化：下界 floor，上界 ceil
        work_x_range = [math.floor(work_x_range[0]), math.ceil(work_x_range[1])]
        work_y_range = [math.floor(work_y_range[0]), math.ceil(work_y_range[1])]

        if work_x_range[0] >= work_x_range[1] or work_y_range[0] >= work_y_range[1]:
            return err("work_x_range / work_y_range 归一化后起点须小于终点", http_status=400, app_code=1002)

        if work_x_range[0] < 0 or work_y_range[0] < 0:
            return err("work_x_range / work_y_range 不能为负数", http_status=400, app_code=1002)

        if work_x_range[1] > img_ctx_width or work_y_range[1] > img_ctx_height:
            return err(
                f"work_x_range / work_y_range 超出图像尺寸 {img_ctx_width}x{img_ctx_height}",
                http_status=400, app_code=1002,
            )

        # 目标区：从请求 body 解析
        target_ranges_raw = payload.get("target_ranges")
        if not isinstance(target_ranges_raw, list) or len(target_ranges_raw) == 0:
            return err("target_ranges 为必填参数，须为非空数组", http_status=400, app_code=1001)

        if len(target_ranges_raw) != 1:
            return err(
                f"当前仅支持单目标区，target_ranges 长度须为 1，收到 {len(target_ranges_raw)}",
                http_status=400, app_code=1002,
            )

        try:
            tr_item = target_ranges_raw[0]
            x_range_raw = tr_item["x_range"]
            y_range_raw = tr_item["y_range"]
            target_x = [float(x_range_raw[0]), float(x_range_raw[1])]
            target_y = [float(y_range_raw[0]), float(y_range_raw[1])]
        except (TypeError, ValueError, IndexError, KeyError):
            return err(
                "target_ranges[0] 格式错误，须包含 x_range: [min, max] 和 y_range: [min, max]",
                http_status=400, app_code=1001,
            )

        # 归一化
        target_x = [math.floor(target_x[0]), math.ceil(target_x[1])]
        target_y = [math.floor(target_y[0]), math.ceil(target_y[1])]

        if target_x[0] >= target_x[1] or target_y[0] >= target_y[1]:
            return err("target_ranges[0] 归一化后起点须小于终点", http_status=400, app_code=1002)

        # 必须完全位于外层范围内
        if (target_x[0] < work_x_range[0] or target_x[1] > work_x_range[1]
                or target_y[0] < work_y_range[0] or target_y[1] > work_y_range[1]):
            return err(
                f"target_ranges[0] [{target_x[0]},{target_x[1]}]x[{target_y[0]},{target_y[1]}] "
                f"必须完全位于外层范围 [{work_x_range[0]},{work_x_range[1]}]x[{work_y_range[0]},{work_y_range[1]}] 内",
                http_status=400, app_code=1002,
            )

        normalized_target_ranges = [{"x_range": target_x, "y_range": target_y}]

        _reject = _reject_if_wifi_connect_running()
        if _reject:
            return _reject

        scan_time_limit_raw = _optional_param('scan_time_limit')
        time_interval_raw = _optional_param('time_interval')

        scan_time_limit = None
        if scan_time_limit_raw not in (None, ''):
            try:
                scan_time_limit = float(scan_time_limit_raw)
            except (TypeError, ValueError):
                return err('scan_time_limit 参数格式错误', http_status=400, app_code=1001)
            if scan_time_limit <= 0:
                return err('scan_time_limit 必须大于 0（单位：分钟）', http_status=400, app_code=1002)

        time_interval = None
        if time_interval_raw not in (None, ''):
            try:
                time_interval = float(time_interval_raw)
            except (TypeError, ValueError):
                return err('time_interval 参数格式错误', http_status=400, app_code=1001)
            if time_interval < 0:
                return err('time_interval 不能小于 0（单位：秒）', http_status=400, app_code=1002)

        outer_pixel_range = {
            "x_range": work_x_range,
            "y_range": work_y_range,
        }
        target_pixel_range = normalized_target_ranges[0]
        # 仅按像素逻辑点估算；PMAP 空洞或执行姿态去重属于 worker 运行时跳过。
        scan_plan, point_count = _build_full_area_scan_plan(
            outer_pixel_range,
            target_pixel_range,
        )
        if internal_minimal_point_test:
            scan_plan, point_count = _apply_full_scan_minimal_point_test_plan(scan_plan)
        dwell_source = "config"
        dwell_times = {
            "coarse_dwell_time": FULL_SCAN_COARSE_DWELL_TIME,
            "fine_dwell_time": FULL_SCAN_FINE_DWELL_TIME,
            "deviation_dwell_time": FULL_SCAN_DEVIATION_DWELL_TIME,
        }
        estimated_time_minutes = _estimate_full_area_scan_minutes(
            scan_plan,
            dwell_times,
            config_count=len(full_scan_configs),
        )

        # 构建命令
        _fs_scan_id = f"full_{int(time.time() * 1000)}"

        # 天线偏差补偿：优先使用本次请求的目标距离，兼容旧 Redis 默认配置。
        _fs_ab_bias = None
        _fs_target_distance = payload.get("target_distance_m", payload.get("target_distance"))
        try:
            _fs_td_raw = _fs_target_distance
            if _fs_td_raw is None:
                _fs_td_raw = r.hget("gimbal:default_config", "target_distance")
            if _fs_td_raw is not None:
                _fs_ab_bias = build_antenna_bias(_fs_td_raw)
                if not _fs_ab_bias.get("enabled"):
                    _fs_ab_bias = None
        except Exception:
            _fs_ab_bias = None

        cmd = {
            "action": "start_full_area_scan",
            "scan_time_limit": scan_time_limit,
            "time_interval": time_interval,
            "scan_id": _fs_scan_id,
            "wifi_mode": full_scan_wifi_mode,
            "configs": full_scan_configs,
            "mode": image_context["mode"],
            "image_context": image_context,
            "work_x_range": work_x_range,
            "work_y_range": work_y_range,
            "target_ranges": normalized_target_ranges,
        }
        if internal_single_channel_test:
            cmd["internal_test_channel"] = internal_test_channel
        if internal_minimal_point_test:
            cmd["test_minimal_points"] = True
        if _fs_ab_bias is not None:
            cmd["antenna_bias"] = _fs_ab_bias
        if _fs_target_distance is not None:
            cmd["target_distance_m"] = _fs_target_distance

        project_scan_params = {
            "scan_id": _fs_scan_id,
            "scan_plan": scan_plan,
            "dwell_time_source": dwell_source,
            "dwell_times": dwell_times,
            "scan_time_limit": scan_time_limit,
            "time_interval": time_interval,
            "scan_time_limit_unit": "minutes",
            "time_interval_unit": "seconds",
            "estimated_points": point_count,
            "estimated_time_minutes": estimated_time_minutes,
            "work_x_range": work_x_range,
            "work_y_range": work_y_range,
            "target_ranges": normalized_target_ranges,
        }
        internal_test_config = {}
        if internal_single_channel_test:
            internal_test_config.update({
                "single_channel": True,
                "channel": internal_test_channel,
                "bandwidth": "HT20",
            })
        if internal_minimal_point_test:
            internal_test_config["minimal_points"] = True
            internal_test_config["point_limits"] = {
                "coarse": {
                    "outer_probe": FULL_SCAN_MINIMAL_TEST_COARSE_OUTER_PROBE_POINTS,
                    "grid": FULL_SCAN_MINIMAL_TEST_COARSE_GRID_POINTS,
                },
                "fine": {"grid": FULL_SCAN_MINIMAL_TEST_FINE_POINTS},
                "deviation_a": {"points_per_layer": FULL_SCAN_MINIMAL_TEST_DEVIATION_POINTS_PER_LAYER},
            }
        if internal_test_config:
            project_scan_params["internal_test"] = internal_test_config
        if _fs_ab_bias is not None:
            project_scan_params["antenna_bias"] = _fs_ab_bias
        if _fs_target_distance is not None:
            project_scan_params["target_distance_m"] = _fs_target_distance

        try:
            project = _start_project_or_raise(
                scan_type="full_area",
                project_name=project_name,
                scan_params=project_scan_params,
            )
        except ValueError as e:
            return err(str(e), http_status=409, app_code=2002)

        try:
            # 新任务拥有 stop key 的换代权；清旧信号、发布 active_scan_id 和
            # 入队必须原子完成，避免并发 stop 落入中间窗口。
            with r.pipeline(transaction=True) as pipe:
                pipe.delete(
                    'full_scan:stop',
                    'multi_scan:stop_full_area_scan',
                    'capture:stop',
                )
                pipe.setex('full_scan:active_scan_id', 86400, _fs_scan_id)
                pipe.lpush(
                    PTZ_COMMAND_QUEUE,
                    json.dumps(cmd, ensure_ascii=False),
                )
                pipe.execute()
        except Exception as e:
            finish_project(project["project_id"], status="FAILED")
            _write_current_project(None)
            return err(f'启动全面扫描失败: {str(e)}', http_status=500, app_code=-1)

        if scan_time_limit is not None:
            warning = f"全面扫描本次扫描窗口将持续 {scan_time_limit:g} 分钟"
        else:
            warning = f"全面扫描预计需要 {estimated_time_minutes} 分钟，请耐心等待"
        if time_interval is not None:
            warning += f"，窗口结束后间隔 {time_interval:g} 秒再继续"
        internal_test_notes = []
        if internal_single_channel_test:
            internal_test_notes.append(f"仅扫描信道 {internal_test_channel} HT20")
        if internal_minimal_point_test:
            internal_test_notes.append("最小点位：粗扫2个外扩点+3个内部点，细扫3点，偏差区每层2点")
        if internal_test_notes:
            warning = (
                "内部测试模式：" + "；".join(internal_test_notes) +
                "；结果不能作为正式完整扫描结论"
            )

        return ok({
            "queued": True,
            "message": "全面扫描已启动",
            "config": {
                "project_id": project["project_id"],
                "project_name": project["project_name"],
                "scan_plan": scan_plan,
                "dwell_time_source": dwell_source,
                "dwell_times": dwell_times,
                "scan_time_limit": scan_time_limit,
                "time_interval": time_interval,
                "scan_time_limit_unit": "minutes",
                "time_interval_unit": "seconds",
                "estimated_points": point_count,
                "estimated_time_minutes": estimated_time_minutes,
                "work_x_range": work_x_range,
                "work_y_range": work_y_range,
                "target_ranges": normalized_target_ranges,
                **({"internal_test": internal_test_config} if internal_test_config else {}),
            },
            **({"antenna_bias": _fs_ab_bias} if _fs_ab_bias is not None else {}),
            "warning": warning
        })
    
    @app.post('/api/v1/ptz/full_area_scan/refine_existing')
    def api_refine_existing_full_scan_whitelist():
        """复用已有轮次白名单，仅执行 Todo 26 位置复核。"""
        if not _FULL_SCAN_CONFIG.get("启用白名单位置复核", False):
            return err(
                "全面扫描.启用白名单位置复核 未开启",
                http_status=403,
                app_code=2003,
            )
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return err("非法 JSON 请求体", http_status=400, app_code=1000)
        try:
            round_index = payload.get("round_index")
            if round_index in (None, ""):
                round_index = r.get("full_scan:whitelist:latest_round")
            round_index = int(round_index)
        except (TypeError, ValueError):
            return err("round_index 必须为整数", http_status=400, app_code=1001)

        raw_round = r.get(f"full_scan:round_{round_index}_results")
        raw_whitelist = r.get(f"full_scan:whitelist:round_{round_index}")
        if not raw_round or not raw_whitelist:
            return err(
                f"轮次 {round_index} 缺少原始结果或白名单",
                http_status=404,
                app_code=2001,
            )
        try:
            whitelist_payload = json.loads(raw_whitelist)
        except Exception:
            return err("轮次白名单 JSON 损坏", http_status=500, app_code=5004)
        available_macs = {
            str(item.get("mac") or "").lower()
            for item in whitelist_payload.get("mac_whitelist") or []
            if isinstance(item, dict)
        }
        requested_macs = payload.get("macs")
        if requested_macs is None and payload.get("mac"):
            requested_macs = [payload.get("mac")]
        if requested_macs is None:
            target_macs = sorted(available_macs)
        elif not isinstance(requested_macs, list):
            return err("macs 必须为数组", http_status=400, app_code=1001)
        else:
            target_macs = sorted({
                str(mac).lower()
                for mac in requested_macs
                if str(mac).strip()
            })
        missing_macs = [
            mac for mac in target_macs if mac not in available_macs
        ]
        if not target_macs:
            return err("没有可复核的白名单 MAC", http_status=400, app_code=1002)
        if missing_macs:
            return err(
                f"指定 MAC 不在轮次白名单中: {missing_macs}",
                http_status=400,
                app_code=1002,
            )

        try:
            status_raw = r.get(PTZ_STATUS_KEY)
            current_status = json.loads(status_raw) if status_raw else {}
            current_full_scan = current_status.get("full_scan") or {}
            if (
                current_full_scan.get("active")
                and not current_full_scan.get("terminal")
            ):
                return err(
                    "已有全面扫描或复核任务正在运行",
                    http_status=409,
                    app_code=2002,
                )
        except Exception:
            pass
        _reject = _reject_if_wifi_connect_running()
        if _reject:
            return _reject

        try:
            round_payload = json.loads(raw_round)
        except Exception:
            return err("轮次原始结果 JSON 损坏", http_status=500, app_code=5004)
        image_context = round_payload.get("image_context")
        scan_mode = round_payload.get("mode")
        if not isinstance(image_context, dict):
            scan_mode = payload.get("mode")
            if scan_mode not in ("panorama", "single"):
                return err(
                    "旧轮次未保存 image_context，请传 mode=panorama/single",
                    http_status=400,
                    app_code=1001,
                )
            try:
                image_context = _resolve_full_scan_image_context(
                    scan_mode,
                    payload.get("image_url"),
                )
            except ValueError as e:
                return err(str(e), http_status=400, app_code=1001)
            except (FileNotFoundError, LookupError) as e:
                return err(str(e), http_status=404, app_code=2001)
            except RuntimeError as e:
                return err(str(e), http_status=500, app_code=5004)

        scan_id = f"refine_existing_{round_index}_{int(time.time() * 1000)}"
        try:
            project = _start_project_or_raise(
                scan_type="full_area",
                project_name=payload.get("project_name")
                or f"refine_round_{round_index}",
                scan_params={
                    "scan_id": scan_id,
                    "refinement_only": True,
                    "source_round": round_index,
                    "target_macs": target_macs,
                    "mode": scan_mode,
                },
            )
        except ValueError as e:
            return err(str(e), http_status=409, app_code=2002)
        command = {
            "action": "start_full_scan_whitelist_refinement",
            "scan_id": scan_id,
            "round_index": round_index,
            "mode": scan_mode,
            "image_context": image_context,
            "target_macs": target_macs,
        }
        try:
            with r.pipeline(transaction=True) as pipe:
                pipe.delete(
                    'full_scan:stop',
                    'multi_scan:stop_full_area_scan',
                    'capture:stop',
                )
                pipe.setex("full_scan:active_scan_id", 86400, scan_id)
                pipe.lpush(
                    PTZ_COMMAND_QUEUE,
                    json.dumps(command, ensure_ascii=False),
                )
                pipe.execute()
        except Exception as e:
            finish_project(project["project_id"], status="FAILED")
            _write_current_project(None)
            return err(f"启动已有白名单复核失败: {e}", http_status=500)
        return ok({
            "queued": True,
            "message": "已有白名单位置复核已启动",
            "scan_id": scan_id,
            "source_round": round_index,
            "target_macs": target_macs,
            "project_id": project["project_id"],
        })

    @app.post('/api/v1/ptz/full_area_scan/stop')
    def api_stop_full_area_scan():
        """停止全面扫描"""
        try:
            project = _mark_project_stopping("full_area")
        except ValueError as e:
            return err(str(e), http_status=409, app_code=2003)

        r.set('multi_scan:stop_full_area_scan', '1', ex=120)
        _fs_stop_scan_id = None
        try:
            _fs_stop_scan_id = r.get('full_scan:active_scan_id')
        except Exception:
            pass
        _stop_requested_at = time.time()
        r.set('full_scan:stop', json.dumps({
            'scan_id': _fs_stop_scan_id, 'reason': 'manual_stop', 'ts': _stop_requested_at,
        }), ex=120)
        # 立即更新状态为 stopping，前端知道"停止请求已收到"
        try:
            _patch_ptz_status_atomic(r, {
                'full_scan': {
                    'active': False,
                    'state': 'stopping',
                    'scan_id': _fs_stop_scan_id,
                    'stop_requested': True,
                    'stop_requested_at': _stop_requested_at,
                    'terminal': False,
                },
            })
        except Exception as exc:
            app.logger.exception("全面扫描 stopping 状态原子写入失败: %s", exc)
        _stop_fs_scan_id = None
        try:
            _stop_fs_scan_id = r.get('full_scan:active_scan_id')
        except Exception:
            pass
        cmd = {"action": "stop_full_area_scan", "scan_id": _stop_fs_scan_id}
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        return ok({
            "queued": True,
            "message": "停止命令已发送",
            "project_id": project["project_id"],
        })
    
    @app.get('/api/v1/ptz/full_area_scan/result')
    def api_get_full_area_scan_result():
        """获取全面扫描结果"""
        try:
            round_index_raw = request.args.get('round_index')
            if round_index_raw not in (None, ''):
                try:
                    round_index = int(round_index_raw)
                except (TypeError, ValueError):
                    return err('round_index 参数格式错误', http_status=400, app_code=1001)
                results_key = f'full_scan:round_{round_index}_results'
                results = r.get(results_key)
            else:
                round_index = None
                results_key = 'full_scan:results'
                results = r.get(results_key)
            latest_round = r.get('full_scan:latest_round')
            
            if not results:
                return ok({
                    "has_result": False,
                    "requested_round": round_index,
                    "source_results_key": results_key,
                    "message": "未找到扫描结果，请先执行全面扫描"
                })
            
            results_data = json.loads(results)
            if isinstance(results_data, dict) and "results" in results_data:
                round_meta = results_data
                point_results = results_data.get("results") or {}
            else:
                round_meta = {}
                point_results = results_data if isinstance(results_data, dict) else {}

            # 统计信息
            total_macs = 0
            for point_data in point_results.values():
                if isinstance(point_data, dict):
                    total_macs += point_data.get('mac_count', 0)
            
            return ok({
                "has_result": True,
                "source_results_key": results_key,
                "latest_round": round_meta.get("latest_round") or (int(latest_round) if latest_round else None),
                "round_index": round_meta.get("round_index"),
                "round_id": round_meta.get("round_id"),
                "round_status": round_meta.get("round_status"),
                "stop_reason": round_meta.get("stop_reason"),
                "expected_points": round_meta.get("expected_points"),
                "completed_points": round_meta.get("completed_points", len(point_results)),
                "round_started_at": round_meta.get("round_started_at"),
                "round_finished_at": round_meta.get("round_finished_at"),
                "scan_time_limit": round_meta.get("scan_time_limit"),
                "scan_time_limit_unit": round_meta.get("scan_time_limit_unit"),
                "time_interval": round_meta.get("time_interval"),
                "time_interval_unit": round_meta.get("time_interval_unit"),
                "window_index": round_meta.get("window_index"),
                "window_started_at": round_meta.get("window_started_at"),
                "window_deadline_at": round_meta.get("window_deadline_at"),
                "remaining_seconds": round_meta.get("remaining_seconds"),
                "whitelist_key": round_meta.get("whitelist_key"),
                "whitelist_count": round_meta.get("whitelist_count"),
                "whitelist_skipped_reason": round_meta.get("whitelist_skipped_reason"),
                "whitelist_error": round_meta.get("whitelist_error"),
                "points_count": len(point_results),
                "total_mac_detections": total_macs,
                "results": point_results
            })
        except Exception as e:
            return err(f'获取扫描结果失败: {str(e)}', http_status=500, app_code=-1)


    @app.get('/api/v1/ptz/full_area_scan/whitelist')
    def api_get_full_area_scan_whitelist():
        """获取全面扫描轮次白名单"""
        try:
            round_index_raw = request.args.get('round_index')
            rounds_raw = r.get('full_scan:whitelist:rounds')
            try:
                rounds = json.loads(rounds_raw) if rounds_raw else []
            except Exception:
                rounds = []

            if round_index_raw not in (None, ''):
                try:
                    round_index = int(round_index_raw)
                except (TypeError, ValueError):
                    return err('round_index 参数格式错误', http_status=400, app_code=1001)
                whitelist_key = f'full_scan:whitelist:round_{round_index}'
            else:
                round_index = None
                whitelist_key = 'full_scan:whitelist:latest_success'

            whitelist_raw = r.get(whitelist_key)
            if not whitelist_raw:
                return ok({
                    "has_result": False,
                    "requested_round": round_index,
                    "source_whitelist_key": whitelist_key,
                    "rounds": rounds,
                    "message": "未找到全面扫描白名单；只有 SUCCESS 轮次会生成白名单"
                })

            whitelist_data = json.loads(whitelist_raw)
            return ok({
                "has_result": True,
                "source_whitelist_key": whitelist_key,
                "rounds": rounds,
                **whitelist_data,
            })
        except Exception as e:
            return err(f'获取全面扫描白名单失败: {str(e)}', http_status=500, app_code=-1)


    # ==================== T3 定位扫描 API ====================

    @app.post('/api/v1/ptz/location_scan/start')
    def api_start_location_scan():
        _reject = _reject_if_wifi_connect_running()
        if _reject:
            return _reject
        """
        定位扫描启动接口（后端自动规划步径，多 MAC 共享点位扫描）

        Body 参数：
            target_macs   : list，必填，目标 MAC 列表
                            如 ["aa:bb:cc:dd:ee:ff", "bb:cc:dd:ee:ff:00"]
            pan_ranges    : list of [min, max]，必填，可含多个范围
                            如 [[10, 60], [80, 120]]
            tilt_ranges   : list of [min, max]，必填，与 pan_ranges 一一对应
            dwell_time    : float，每点停留时长（秒），默认 1.0
            target_configs: list，可选，按 MAC 指定信道/带宽
                            如 [{"mac": "aa:bb:cc:dd:ee:ff", "channel": 6, "bandwidth": "HT20"}]
            channel       : int，可选；不填则自动探测信道和带宽
            bandwidth     : str，可选；指定 channel 时必填；不能与 target_configs 同时使用
            expand_deg    : float，可选；扫描范围四周扩边角度，
                            默认从 config.json 读取（默认 10.0°）
            probe_dwell_time : float，可选；信道探测每配置停留时长，默认 1s
            probe_rounds_max : int，可选；信道探测最大轮数，默认 2
            capture_time_limit : float，可选；定位完成后每个 MAC 抓包时长（秒）
            pcap_split_size_mb : float，可选；pcap 单文件分包大小，默认 100MB
            min_free_memory_mb : float，可选；抓包资源保护内存阈值
            min_free_disk_mb   : float，可选；抓包资源保护磁盘阈值
            time_interval : float，可选；轮次之间等待间隔（秒），不传则只执行一轮
            track_time_limit : float，可选；连续追踪总时长上限（分钟）
            track_rssi_threshold : float，可选；Fast Verify 允许 RSSI 下降阈值（dB），
                                   默认从 config.json 读取（默认 5.0）
            project_name  : str，可选，项目名称
        """
        try:
            payload = request.get_json(force=True)
        except Exception:
            return err('非法 JSON 请求体', http_status=400, app_code=1000)

        # ── 必填参数 ─────────────────────────────────────────────────────────
        target_macs = payload.get('target_macs', [])
        pan_ranges  = payload.get('pan_ranges',  [])
        tilt_ranges = payload.get('tilt_ranges', [])

        if not target_macs:
            return err('target_macs 不能为空', http_status=400, app_code=1001)
        if not pan_ranges or not tilt_ranges:
            return err('pan_ranges/tilt_ranges 不能为空', http_status=400, app_code=1002)
        if len(pan_ranges) != len(tilt_ranges):
            return err('pan_ranges 与 tilt_ranges 长度必须相同', http_status=400, app_code=1003)

        # ── 可选参数 ─────────────────────────────────────────────────────────
        dwell_time      = float(payload.get('dwell_time', 1.0))
        target_configs  = payload.get('target_configs', [])
        channel         = payload.get('channel', None)
        bandwidth       = payload.get('bandwidth', None)
        expand_deg      = float(payload.get('expand_deg',       LOCATION_SCAN_EXPAND_DEG))
        probe_dwell_time = float(payload.get('probe_dwell_time', 1.0))
        probe_rounds_max = int(payload.get('probe_rounds_max', 2))
        capture_time_limit = payload.get('capture_time_limit', None)
        pcap_split_size_mb = float(payload.get('pcap_split_size_mb', 100.0))
        min_free_memory_mb = payload.get('min_free_memory_mb', None)
        min_free_disk_mb = payload.get('min_free_disk_mb', None)
        time_interval_raw = payload.get('time_interval', None)
        track_time_limit_raw = payload.get('track_time_limit', None)
        track_rssi_threshold_raw = payload.get('track_rssi_threshold', LOCATION_SCAN_TRACK_RSSI_THRESHOLD)
        project_name    = payload.get('project_name')

        # ── 参数校验 ─────────────────────────────────────────────────────────
        try:
            pan_ranges_f  = [[float(pr[0]), float(pr[1])] for pr in pan_ranges]
            tilt_ranges_f = [[float(tr[0]), float(tr[1])] for tr in tilt_ranges]
        except (TypeError, ValueError, IndexError):
            return err('pan_ranges/tilt_ranges 格式错误', http_status=400, app_code=1004)

        for i, (pr, tr) in enumerate(zip(pan_ranges_f, tilt_ranges_f)):
            if pr[0] >= pr[1]:
                return err(f'pan_ranges[{i}] 最小值须小于最大值', http_status=400, app_code=1005)
            if tr[0] >= tr[1]:
                return err(f'tilt_ranges[{i}] 最小值须小于最大值', http_status=400, app_code=1005)

        time_interval = None
        if time_interval_raw not in (None, ''):
            try:
                time_interval = float(time_interval_raw)
            except (TypeError, ValueError):
                return err('time_interval 参数格式错误', http_status=400, app_code=1006)
            if time_interval < 0:
                return err('time_interval 不能小于 0（单位：秒）', http_status=400, app_code=1007)

        track_time_limit = None
        if track_time_limit_raw not in (None, ''):
            try:
                track_time_limit = float(track_time_limit_raw)
            except (TypeError, ValueError):
                return err('track_time_limit 参数格式错误', http_status=400, app_code=1008)
            if track_time_limit <= 0:
                return err('track_time_limit 必须大于 0（单位：分钟）', http_status=400, app_code=1009)

        try:
            track_rssi_threshold = float(track_rssi_threshold_raw)
        except (TypeError, ValueError):
            return err('track_rssi_threshold 参数格式错误', http_status=400, app_code=1010)
        if track_rssi_threshold < 0:
            return err('track_rssi_threshold 不能小于 0（单位：dB）', http_status=400, app_code=1011)

        target_macs_l = [m.lower().strip() for m in target_macs if m]
        if not target_macs_l:
            return err('target_macs 不能为空', http_status=400, app_code=1001)

        if target_configs in (None, ''):
            target_configs = []

        fixed_channel_enabled = channel not in (None, '')
        if fixed_channel_enabled and target_configs:
            return err('channel/bandwidth 不能与 target_configs 同时使用', http_status=400, app_code=1014)

        if fixed_channel_enabled:
            if bandwidth in (None, ''):
                return err('指定 channel 时必须同时传 bandwidth', http_status=400, app_code=1012)
            try:
                channel = int(channel)
            except (TypeError, ValueError):
                return err('channel 参数格式错误', http_status=400, app_code=1013)
            bandwidth = str(bandwidth).strip()
            if not bandwidth:
                return err('指定 channel 时必须同时传 bandwidth', http_status=400, app_code=1012)
        else:
            channel = None
            bandwidth = None

        target_config_map = {}
        if target_configs:
            if not isinstance(target_configs, list):
                return err('target_configs 必须是数组', http_status=400, app_code=1015)
            target_mac_set = set(target_macs_l)
            for idx, cfg in enumerate(target_configs):
                if not isinstance(cfg, dict):
                    return err(f'target_configs[{idx}] 格式错误', http_status=400, app_code=1015)
                mac = str(cfg.get('mac', '')).lower().strip()
                if not mac:
                    return err(f'target_configs[{idx}].mac 不能为空', http_status=400, app_code=1016)
                if mac not in target_mac_set:
                    return err(
                        f'target_configs[{idx}].mac 不在 target_macs 中: {mac}',
                        http_status=400,
                        app_code=1016,
                    )
                if mac in target_config_map:
                    return err(f'target_configs 中 MAC 重复: {mac}', http_status=400, app_code=1017)
                raw_channel = cfg.get('channel')
                raw_bandwidth = cfg.get('bandwidth')
                if raw_channel in (None, '') or raw_bandwidth in (None, ''):
                    return err(
                        f'target_configs[{idx}] 必须同时包含 channel 和 bandwidth',
                        http_status=400,
                        app_code=1018,
                    )
                try:
                    cfg_channel = int(raw_channel)
                except (TypeError, ValueError):
                    return err(
                        f'target_configs[{idx}].channel 参数格式错误',
                        http_status=400,
                        app_code=1019,
                    )
                cfg_bandwidth = str(raw_bandwidth).strip()
                if not cfg_bandwidth:
                    return err(
                        f'target_configs[{idx}] 必须同时包含 channel 和 bandwidth',
                        http_status=400,
                        app_code=1018,
                    )
                target_config_map[mac] = {
                    'channel': cfg_channel,
                    'bandwidth': cfg_bandwidth,
                }

        # ── 防重入 ───────────────────────────────────────────────────────────
        if r.get('location_scan:active_scan_id'):
            return err('定位扫描正在进行中，请先停止', http_status=409, app_code=2001)
        loc_status_raw = r.get('location_scan:status')
        if loc_status_raw:
            loc_status = json.loads(loc_status_raw)
            if loc_status.get('status') in ('running', 'scanning') or (
                loc_status.get('status') is None and
                loc_status.get('phase') in (
                    'queued',
                    'detecting_channels',
                    'channel_probe',
                    'fast_verify',
                    'scanning',
                    'coarse',
                    'mid',
                    'fine',
                    'locating',
                    'capturing',
                    'waiting',
                )
            ):
                return err('定位扫描正在进行中，请先停止', http_status=409, app_code=2001)

        # ── 构建命令 ─────────────────────────────────────────────────────────
        now = time.time()
        _ls_scan_id = f"location_{int(now * 1000)}"

        # 天线偏差补偿：从 Redis gimbal:default_config 读取 target_distance
        _ls_ab_bias = None
        try:
            _ls_td_raw = r.hget("gimbal:default_config", "target_distance")
            if _ls_td_raw is not None:
                _ls_ab_bias = build_antenna_bias(_ls_td_raw)
                if not _ls_ab_bias.get("enabled"):
                    _ls_ab_bias = None
        except Exception:
            _ls_ab_bias = None

        cmd = {
            'action':           'start_location_scan',
            'target_macs':      target_macs_l,
            'pan_ranges':       pan_ranges_f,
            'tilt_ranges':      tilt_ranges_f,
            'dwell_time':       dwell_time,
            'expand_deg':       expand_deg,
            'probe_dwell_time': probe_dwell_time,
            'probe_rounds_max': probe_rounds_max,
            'capture_time_limit': float(capture_time_limit) if capture_time_limit not in (None, '') else None,
            'pcap_split_size_mb': pcap_split_size_mb,
            'min_free_memory_mb': float(min_free_memory_mb) if min_free_memory_mb not in (None, '') else None,
            'min_free_disk_mb': float(min_free_disk_mb) if min_free_disk_mb not in (None, '') else None,
            'time_interval':     time_interval,
            'track_time_limit':  track_time_limit,
            'track_rssi_threshold': track_rssi_threshold,
            'project_name':      project_name,
            'scan_id':           _ls_scan_id,
        }
        if channel is not None:
            cmd['channel']   = channel
            cmd['bandwidth'] = bandwidth
        if target_config_map:
            cmd['target_configs'] = target_config_map
        if _ls_ab_bias is not None:
            cmd['antenna_bias'] = _ls_ab_bias

        app.logger.info(
            "[T3] 定位扫描启动信道配置: fixed=%s channel=%s bandwidth=%s "
            "target_configs_count=%s target_configs_macs=%s auto_probe_macs=%s",
            channel is not None,
            channel,
            bandwidth,
            len(target_config_map),
            sorted(target_config_map.keys()),
            sorted(set(target_macs_l) - set(target_config_map.keys()))
            if channel is None else [],
        )

        r.delete('location_scan:result')
        r.set('location_scan:status', json.dumps({
            'phase': 'queued',
            'status': 'running',
            'ts': now,
            'target_macs': target_macs_l,
            'mac_count': len(target_macs_l),
            'range_count': len(pan_ranges_f),
        }, ensure_ascii=False))
        # 写入 Redis active_scan_id，供 capture_worker 校验
        r.setex('location_scan:active_scan_id', 86400, _ls_scan_id)
        # 新统一状态：写入 ptz:current_status.location_scan
        try:
            ptz_raw = r.get(PTZ_STATUS_KEY)
            ptz_status = json.loads(ptz_raw) if ptz_raw else {}
            ptz_status['ts'] = now
            ptz_status['location_scan'] = {
                'active': True,
                'phase': 'queued',
                'state': 'queued',
                'scan_id': _ls_scan_id,
                'stop_requested': False,
                'terminal': False,
            }
            r.set(PTZ_STATUS_KEY, json.dumps(ptz_status, ensure_ascii=False))
        except Exception:
            pass
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))

        # 估算点数：定位扩边裁剪到 Redis gimbal:default_config 的 work_* 业务运行范围。
        location_bound = full_area_precheck_range_from_redis(
            r,
            _HARDWARE_LIMITS_FALLBACK,
            _CONFIG_DEFAULT_SCAN,
        )
        total_round1_points = 0
        round1_plan = []
        target_guard_plan = []
        target_guard_total_points = 0
        target_guard_covered_by_round1 = 0
        target_guard_remaining_after_round1 = 0
        for pr, tr in zip(pan_ranges_f, tilt_ranges_f):
            exp_pr = [max(location_bound['pan_min'], pr[0] - expand_deg),
                      min(location_bound['pan_max'], pr[1] + expand_deg)]
            exp_tr = [max(location_bound['tilt_min'], tr[0] - expand_deg),
                      min(location_bound['tilt_max'], tr[1] + expand_deg)]
            if exp_pr[0] > exp_pr[1] or exp_tr[0] > exp_tr[1]:
                round1_plan.append({
                    'pan_range': exp_pr,
                    'tilt_range': exp_tr,
                    'pan_step': None,
                    'tilt_step': None,
                    'point_count': 0,
                    'skipped_reason': 'outside_location_bound',
                })
                target_guard_plan.append({
                    'target_pan_range': pr,
                    'target_tilt_range': tr,
                    'point_count': 0,
                    'covered_by_round1_estimate': 0,
                    'remaining_after_round1_estimate': 0,
                    'skipped_reason': 'outside_location_bound',
                })
                continue
            pan_step = _auto_location_step(exp_pr[0], exp_pr[1], phase="coarse_16")
            tilt_step = _auto_location_step(exp_tr[0], exp_tr[1], phase="coarse_16")
            round1_points = set(_build_scan_path_points(exp_pr, exp_tr, pan_step, tilt_step))
            point_count = _count_scan_points(exp_pr, exp_tr, pan_step, tilt_step)
            total_round1_points += point_count
            round1_plan.append({
                'pan_range': exp_pr,
                'tilt_range': exp_tr,
                'pan_step': pan_step,
                'tilt_step': tilt_step,
                'point_count': point_count,
            })
            guard_points = _estimate_location_target_guard_points(pr, tr, location_bound)
            guard_keys = {_location_point_key(point) for point in guard_points}
            covered_keys = guard_keys.intersection(round1_points)
            remaining_count = len(guard_keys) - len(covered_keys)
            target_guard_total_points += len(guard_keys)
            target_guard_covered_by_round1 += len(covered_keys)
            target_guard_remaining_after_round1 += remaining_count
            target_guard_plan.append({
                'target_pan_range': pr,
                'target_tilt_range': tr,
                'point_count': len(guard_keys),
                'required_points': 5,
                'inner_points': LOCATION_SCAN_TARGET_GUARD_INNER_POINTS,
                'min_span_deg': LOCATION_SCAN_TARGET_GUARD_MIN_SPAN_DEG,
                'covered_by_round1_estimate': len(covered_keys),
                'remaining_after_round1_estimate': remaining_count,
            })

        target_guard_mid_estimate = int(math.ceil(target_guard_remaining_after_round1 / 2.0))
        target_guard_fine_estimate = target_guard_remaining_after_round1 - target_guard_mid_estimate

        return ok({
            'queued': True,
            'message': f'定位扫描已启动，{len(target_macs_l)} 个 MAC，{len(pan_ranges_f)} 个范围',
            'config': {
                'target_macs':       target_macs_l,
                'mac_count':         len(target_macs_l),
                'range_count':       len(pan_ranges_f),
                'pan_ranges':        pan_ranges_f,
                'tilt_ranges':       tilt_ranges_f,
                'location_bound': {
                    'pan_range': [location_bound['pan_min'], location_bound['pan_max']],
                    'tilt_range': [location_bound['tilt_min'], location_bound['tilt_max']],
                    'source': location_bound.get('source'),
                    'fallback_source': location_bound.get('fallback_source'),
                },
                'dwell_time':        dwell_time,
                'expand_deg':        expand_deg,
                'probe_dwell_time':  probe_dwell_time,
                'probe_rounds_max':  probe_rounds_max,
                'channel':           channel,
                'bandwidth':         bandwidth,
                'target_configs':    [
                    {'mac': mac, **cfg}
                    for mac, cfg in target_config_map.items()
                ],
                'capture_time_limit': float(capture_time_limit) if capture_time_limit not in (None, '') else None,
                'pcap_split_size_mb': pcap_split_size_mb,
                'time_interval':      time_interval,
                'track_time_limit':   track_time_limit,
                'track_rssi_threshold': track_rssi_threshold,
                'shrink_top_n': LOCATION_SCAN_SHRINK_TOP_N,
                'shrink_rssi_delta': LOCATION_SCAN_SHRINK_RSSI_DELTA,
                'shrink_outlier_deg': LOCATION_SCAN_SHRINK_OUTLIER_DEG,
                'shrink_single_pan_half': LOCATION_SCAN_SHRINK_SINGLE_PAN_HALF,
                'shrink_single_tilt_half': LOCATION_SCAN_SHRINK_SINGLE_TILT_HALF,
                'target_guard_inner_points': LOCATION_SCAN_TARGET_GUARD_INNER_POINTS,
                'target_guard_min_span_deg': LOCATION_SCAN_TARGET_GUARD_MIN_SPAN_DEG,
                'estimated_round1_points': total_round1_points,
                'estimated_target_guard_points': target_guard_total_points,
                'estimated_target_guard_covered_by_round1': target_guard_covered_by_round1,
                'estimated_target_guard_remaining_after_round1': target_guard_remaining_after_round1,
                'scan_plan': {
                    'channel_probe': {
                        'description': '信道探测：每个目标区域中心 + 四角探测，所有 MAC 同时探测',
                        'point_count_per_round': len(pan_ranges_f) * 5,
                        'probe_rounds_max': probe_rounds_max,
                        'probe_dwell_time': probe_dwell_time,
                    },
                    'scanning': {
                        'description': '扫描：8° 第一轮，4° 第二轮，2° 第三轮；中扫/细扫保留 RSSI 收缩候选框，并追加目标区保底覆盖点',
                        'round1_plan': round1_plan,
                        'round2_step': 4.0,
                        'round3_step': 2.0,
                        'target_guard_plan': target_guard_plan,
                        'target_guard_total_points': target_guard_total_points,
                        'target_guard_covered_by_round1_estimate': target_guard_covered_by_round1,
                        'target_guard_mid_extra_points_estimate': target_guard_mid_estimate,
                        'target_guard_fine_extra_points_estimate': target_guard_fine_estimate,
                        'target_guard_note': '每个目标区域保底覆盖四角+中心，并在普通范围内追加均匀内部点；保底点不占 RSSI 候选框点数上限，已由前序阶段实际扫到的点不会重复补扫。',
                    },
                    'locating': {
                        'description': '最终定位：确认每个 MAC 的最优位置和 RSSI',
                        'mac_count': len(target_macs_l),
                    },
                },
                **({'antenna_bias': _ls_ab_bias} if _ls_ab_bias is not None else {}),
            },
        })

    @app.post('/api/v1/ptz/location_scan/stop')
    def api_stop_location_scan():
        """停止定位扫描"""
        now = time.time()
        # ① 直接写 Redis 标志位（带 scan_id，worker 只在匹配时才停）
        _current_scan_id = None
        try:
            _current_scan_id = r.get('location_scan:active_scan_id')
        except Exception:
            pass
        r.set('location_scan:stop', json.dumps({
            'scan_id': _current_scan_id,
            'ts': now,
        }), ex=120)
        r.set('capture:stop', '1', ex=120)
        # ② 立即更新状态为 stopping（不是 stopped）。
        #    前端立刻知道"停止请求已收到"，但 worker 还在收尾。
        #    active 保持 true，等 worker 真退出后由 worker 写 active=false。
        r.set('location_scan:status', json.dumps({
            'phase': 'idle',
            'status': 'stopping',
            'stop_requested': True,
            'reason': 'manual_stop',
            'ts': now,
        }, ensure_ascii=False))
        try:
            ptz_raw = r.get(PTZ_STATUS_KEY)
            ptz_status = json.loads(ptz_raw) if ptz_raw else {}
            ptz_status['ts'] = now
            ptz_status['state'] = 'IDLE'
            # patch 而非替换：保留原有的 scan_id、phase、current_point 等字段
            ls = ptz_status.get('location_scan', {})
            ls.update({
                'active': True,
                'state': 'stopping',
                'stop_requested': True,
                'stop_requested_at': now,
                'terminal': False,
            })
            ptz_status['location_scan'] = ls
            r.set(PTZ_STATUS_KEY, json.dumps(ptz_status, ensure_ascii=False))
        except Exception:
            pass
        # ③ 同时塞命令队列，兼容空闲状态下的 stop 分支处理（带 scan_id 防误伤）
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps({
            'action': 'stop_location_scan',
            'scan_id': _current_scan_id,
        }))
        r.lpush(CAPTURE_COMMAND_QUEUE, json.dumps({
            'action': 'stop_capture',
            'reason': 'location_scan_stop',
        }, ensure_ascii=False))
        return ok({'queued': True, 'message': '停止命令已发送'})

    @app.get('/api/v1/ptz/location_scan/status')
    def api_get_location_scan_status():
        """查询定位扫描当前进度"""
        try:
            raw = r.get('location_scan:status')
            ptz_raw = r.get(PTZ_STATUS_KEY)
            ptz_status = json.loads(ptz_raw) if ptz_raw else {}
            if not raw:
                return ok({
                    'phase': 'idle',
                    'ptz_state': ptz_status.get('state', 'UNKNOWN'),
                    'ptz_position': ptz_status.get('position'),
                })
            status = json.loads(raw)
            return ok({
                **status,
                'ptz_state': ptz_status.get('state', 'UNKNOWN'),
                'ptz_position': ptz_status.get('position'),
                'location_scan_detail': ptz_status.get('location_scan'),
            })
        except Exception as e:
            return err(f'获取状态失败: {str(e)}', http_status=500, app_code=-1)

    @app.get('/api/v1/ptz/location_scan/result')
    def api_get_location_scan_result():
        """获取定位扫描最终结果"""
        try:
            raw = r.get('location_scan:result')
            if not raw:
                return ok({
                    'has_result': False,
                    'message': '未找到定位结果，请先执行定位扫描',
                })
            data = json.loads(raw)
            results = data.get('results', {})
            found   = [m for m, v in results.items()
                       if isinstance(v, dict) and v.get('status') == 'found']
            return ok({
                'has_result':  True,
                'ts':          data.get('ts'),
                'mac_count':   len(results),
                'found_count': len(found),
                'results':     results,
            })
        except Exception as e:
            return err(f'获取结果失败: {str(e)}', http_status=500, app_code=-1)

    # ==================== S8 客户端扫描 API ====================

    @app.post('/api/v1/ptz/client_scan/start')
    def api_start_client_scan():
        """
        S8 客户端扫描启动接口

        Body 参数：
            pan_range   : [min, max]，用户指定的水平扫描范围（必填）
            tilt_range  : [min, max]，用户指定的垂直扫描范围（必填）
            pan_step    : 水平步进（度），默认 10.0
            tilt_step   : 垂直步进（度），默认 10.0
            dwell_time  : 每个信道配置的驻留时长（秒），不填则使用后端 CLIENT_SCAN_DEFAULT_DWELL_TIME 常量
            channel     : 扫描信道（可选）；不填则自动全频段跳扫（INITIAL_SCAN_CONFIGS）
            bandwidth   : 带宽（可选）；仅在指定 channel 时有效，默认 "HT20"

        说明：
            - 范围会在内部自动向外扩展 CLIENT_SCAN_GUARD_STEPS 步（防边界误判）
            - 单次任务，扫完自动结束，不循环
            - 结果不写历史（无 project/snapshot 概念）
        """
        try:
            payload = request.get_json(force=True)
        except Exception:
            return err('非法 JSON 请求体', http_status=400, app_code=1000)

        pan_range  = payload.get('pan_range')
        tilt_range = payload.get('tilt_range')

        if not pan_range or len(pan_range) != 2:
            return err('请提供有效的水平扫描范围 [最小角度, 最大角度]', http_status=400, app_code=1001)
        if not tilt_range or len(tilt_range) != 2:
            return err('请提供有效的垂直扫描范围 [最小角度, 最大角度]', http_status=400, app_code=1002)

        try:
            pan_min,  pan_max  = float(pan_range[0]),  float(pan_range[1])
            tilt_min, tilt_max = float(tilt_range[0]), float(tilt_range[1])
            pan_step  = float(payload.get('pan_step',  10.0))
            tilt_step = float(payload.get('tilt_step', 10.0))
            # channel/bandwidth 可选：不传 → 后端自动全频段跳扫；传了 → 固定单信道
            raw_channel   = payload.get('channel',   None)
            raw_bandwidth = payload.get('bandwidth', None)
            channel   = int(raw_channel)   if raw_channel   is not None else None
            bandwidth = str(raw_bandwidth) if raw_bandwidth is not None else None
        except (TypeError, ValueError, IndexError):
            return err('参数格式错误', http_status=400, app_code=1003)

        dwell_time = payload.get('dwell_time', None)
        if dwell_time is not None:
            try:
                dwell_time = float(dwell_time)
                if not (1.0 <= dwell_time <= 30.0):
                    return err('dwell_time 超出范围 (1.0–30.0秒)', http_status=400, app_code=1004,
                               data={"allowed": [1.0, 30.0], "got": dwell_time})
            except (TypeError, ValueError):
                return err('dwell_time 必须是数字', http_status=400, app_code=1005)

        if pan_min > pan_max or tilt_min > tilt_max:
            return err('范围格式错误，最小值不能大于最大值', http_status=400, app_code=1006)

        # 防重入：如果客户端扫描正在运行，拒绝重复发起
        status_raw = r.get('client_scan:status')
        if status_raw:
            try:
                status_obj = json.loads(status_raw)
                if status_obj.get('state') in ('running', 'stopping'):
                    return err('客户端扫描正在进行中，请先停止',
                               http_status=409, app_code=2001,
                               data={'current_status': status_obj})
            except Exception:
                pass

        cmd = {
            'action':    'start_client_scan',
            'pan_range':  [pan_min, pan_max],
            'tilt_range': [tilt_min, tilt_max],
            'pan_step':   pan_step,
            'tilt_step':  tilt_step,
        }
        if channel is not None:
            cmd['channel'] = channel
        if bandwidth is not None:
            cmd['bandwidth'] = bandwidth
        if dwell_time is not None:
            cmd['dwell_time'] = dwell_time

        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))

        return ok({
            'queued': True,
            'message': '客户端扫描已启动',
            'config': {
                'pan_range':  [pan_min, pan_max],
                'tilt_range': [tilt_min, tilt_max],
                'pan_step':   pan_step,
                'tilt_step':  tilt_step,
                'channel':    channel,
                'bandwidth':  bandwidth,
                'dwell_time': dwell_time,
            },
            'note': '扫描范围将自动向外扩展 guard_steps 步以防边界误判；扫完自动结束，无需手动停止',
        })

    @app.post('/api/v1/ptz/client_scan/stop')
    def api_stop_client_scan():
        """S8 停止客户端扫描"""
        # ① 直接写 Redis 标志位（阻塞中的 ptz_worker 会在下个点位前检测到）
        r.set('client_scan:stop', '1', ex=120)
        # ② 同时下发命令队列（让 ptz_worker 空闲时也能更新状态）
        cmd = {'action': 'stop_client_scan'}
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
        return ok({'queued': True, 'message': '停止命令已发送'})

    @app.get('/api/v1/ptz/client_scan/status')
    def api_get_client_scan_status():
        """S8 获取客户端扫描当前状态与进度"""
        try:
            status_raw = r.get('client_scan:status')
            ptz_raw    = r.get(PTZ_STATUS_KEY)

            if not status_raw:
                return ok({
                    'has_task': False,
                    'message': '当前没有客户端扫描任务',
                })

            status_obj = json.loads(status_raw)
            ptz_obj    = json.loads(ptz_raw) if ptz_raw else {}

            # 进度信息已合并进 ptz:current_status 的 client_scan 字段
            cs_field   = ptz_obj.get('client_scan', {})
            progress_obj = {
                'current_point':    cs_field.get('current_point'),
                'total_points':     cs_field.get('total_points'),
                'completed_points': cs_field.get('completed_points'),
                'in_target':        cs_field.get('in_target'),
                'pan':              ptz_obj.get('position', {}).get('pan'),
                'tilt':             ptz_obj.get('position', {}).get('tilt'),
            } if cs_field.get('active') else None

            return ok({
                'has_task':  True,
                'status':    status_obj,
                'progress':  progress_obj,
                'ptz_state': ptz_obj.get('state', 'unknown'),
                'position':  ptz_obj.get('position', {}),
            })
        except Exception as e:
            return err(f'获取客户端扫描状态失败: {str(e)}', http_status=500, app_code=-1)

    @app.get('/api/v1/ptz/client_scan/result')
    def api_get_client_scan_result():
        """
        S8 获取客户端扫描结果

        响应字段说明：
            client_count    : 发现的客户端总数
            clients         : 按 best_rssi 降序排列的客户端字典
                {
                  "<mac>": {
                    best_rssi    : 最强 RSSI（dBm）
                    pan          : 最强信号对应的 Pan 角度
                    tilt         : 最强信号对应的 Tilt 角度
                    in_target    : 最强点是否在用户选区内
                    confidence   : "high" | "medium" | "low"
                    ap_bssid     : 关联的 AP MAC（若有）
                    ap_ssid      : 关联的 AP SSID（若有）
                    probe_ssids  : Probe Request 中携带的 SSID 列表
                    status       : "associated" | "probing"
                    sample_count : 定向天线采样帧数
                    omni_rssi_avg: 全向天线 RSSI 均值（若有）
                  }
                }
            target_range    : 用户原始选区 {pan:[],tilt:[]}
            guard_steps     : 实际使用的扩展步数
            scanned_points  : 实际扫描点位数
            total_points    : 总点位数（含扩展 guard zone）
            finished_at     : 完成时间戳
        """
        try:
            result_raw = r.get('client_scan:result')
            if not result_raw:
                return ok({
                    'has_result': False,
                    'message': '未找到客户端扫描结果，请先执行客户端扫描',
                })

            result = json.loads(result_raw)

            return ok({
                'has_result':     True,
                'client_count':   result.get('client_count', 0),
                'scanned_points': result.get('scanned_points', 0),
                'total_points':   result.get('total_points', 0),
                'finished_at':    result.get('finished_at'),
                'target_range':   result.get('target_range', {}),
                'guard_steps':    result.get('guard_steps', 1),
                'channel':        result.get('channel'),
                'bandwidth':      result.get('bandwidth'),
                'clients':        result.get('clients', {}),
                # point_details 体积较大，按需返回（通过 ?include_details=1 参数控制）
                'point_details':  result.get('point_details', {}) if request.args.get('include_details') == '1' else None,
            })
        except Exception as e:
            return err(f'获取客户端扫描结果失败: {str(e)}', http_status=500, app_code=-1)

    # ─── 指定点位抓包 ──────────────────────────────────────────────────────────

    @app.post('/api/v1/ptz/capture_at_best')
    def api_capture_at_best():
        _reject = _reject_if_wifi_connect_running()
        if _reject:
            return _reject
        """
        指定点位抓包。

        流程：
          1. 前端直接传入 pan / tilt / mac / channel / bandwidth
          2. 后端写扫描停止标志（不等待）
          3. 将 move_to_best_capture 命令推入 ptz:command_queue
             ptz_control 收到后：移动到指定点位 → 推送抓包命令给 capture_worker
          4. 返回排队信息

        Body 参数：
            pan                : float，必填，目标 pan
            tilt               : float，必填，目标 tilt
            mac                : str，必填，目标 MAC
            channel            : int，必填，抓包信道
            bandwidth          : str，必填，抓包带宽
            capture_time_limit : float，选填，不传则持续抓包直到手动停止
            pcap_filename      : str，选填，不传则由后端自动生成
            min_free_memory_mb : float，选填，抓包资源保护内存阈值
            min_free_disk_mb   : float，选填，抓包资源保护磁盘阈值
        """
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return err('非法 JSON 请求体', http_status=400, app_code=1000)

        missing = [
            name for name in ('pan', 'tilt', 'mac', 'channel', 'bandwidth')
            if payload.get(name) in (None, '')
        ]
        if missing:
            return err(
                f"缺少必填参数: {', '.join(missing)}",
                http_status=400,
                app_code=1001,
            )

        target_mac = ''.join(str(payload.get('mac')).split()).lower()
        bandwidth = str(payload.get('bandwidth')).strip()
        if not target_mac:
            return err('mac 参数不能为空', http_status=400, app_code=1001)
        if not bandwidth:
            return err('bandwidth 参数不能为空', http_status=400, app_code=1001)

        try:
            pan = float(payload.get('pan'))
            tilt = float(payload.get('tilt'))
            channel = int(payload.get('channel'))
        except (TypeError, ValueError):
            return err('pan/tilt/channel 参数类型错误', http_status=400, app_code=1002)

        if not (PTZ_LIMIT_PAN_MIN <= pan <= PTZ_LIMIT_PAN_MAX):
            return err(
                'pan 超出范围',
                http_status=400,
                app_code=1003,
                data={"allowed": [PTZ_LIMIT_PAN_MIN, PTZ_LIMIT_PAN_MAX], "got": pan},
            )
        if not (PTZ_LIMIT_TILT_MIN <= tilt <= PTZ_LIMIT_TILT_MAX):
            return err(
                'tilt 超出范围',
                http_status=400,
                app_code=1004,
                data={"allowed": [PTZ_LIMIT_TILT_MIN, PTZ_LIMIT_TILT_MAX], "got": tilt},
            )

        capture_limit = payload.get('capture_time_limit')
        capture_limit_f = None
        if capture_limit not in (None, ''):
            try:
                capture_limit_f = float(capture_limit)
            except (TypeError, ValueError):
                return err('capture_time_limit 参数类型错误', http_status=400, app_code=1005)
            if capture_limit_f <= 0:
                return err('capture_time_limit 必须大于 0', http_status=400, app_code=1005)

        min_free_memory_mb = payload.get('min_free_memory_mb')
        min_free_memory_mb_f = None
        if min_free_memory_mb not in (None, ''):
            try:
                min_free_memory_mb_f = float(min_free_memory_mb)
            except (TypeError, ValueError):
                return err('min_free_memory_mb 参数类型错误', http_status=400, app_code=1007)
            if min_free_memory_mb_f < 0:
                return err('min_free_memory_mb 不能小于 0', http_status=400, app_code=1007)

        min_free_disk_mb = payload.get('min_free_disk_mb')
        min_free_disk_mb_f = None
        if min_free_disk_mb not in (None, ''):
            try:
                min_free_disk_mb_f = float(min_free_disk_mb)
            except (TypeError, ValueError):
                return err('min_free_disk_mb 参数类型错误', http_status=400, app_code=1008)
            if min_free_disk_mb_f < 0:
                return err('min_free_disk_mb 不能小于 0', http_status=400, app_code=1008)

        pcap_filename = payload.get('pcap_filename') or None
        if pcap_filename is None:
            mac_safe = target_mac.replace(':', '').lower()
            pcap_filename = f"capture_{mac_safe}_{int(time.time())}.pcap"
        else:
            pcap_filename = str(pcap_filename).strip()
            if not pcap_filename:
                mac_safe = target_mac.replace(':', '').lower()
                pcap_filename = f"capture_{mac_safe}_{int(time.time())}.pcap"
            if '/' in pcap_filename or '\\' in pcap_filename or pcap_filename in ('.', '..'):
                return err('pcap_filename 只能是文件名，不能包含路径', http_status=400, app_code=1006)
            if not pcap_filename.lower().endswith('.pcap'):
                pcap_filename += '.pcap'

        # 设置停止标志，不等待扫描停止（扫描会自行停止）
        # 全面扫描 stop 走 scan_id 语义
        _cab_fs_scan_id = None
        try:
            _cab_fs_scan_id = r.get('full_scan:active_scan_id')
        except Exception:
            pass
        if _cab_fs_scan_id:
            r.set('full_scan:stop', json.dumps({
                'scan_id': _cab_fs_scan_id, 'reason': 'capture_at_best', 'ts': time.time(),
            }), ex=120)
        r.set('multi_scan:stop_full_area_scan', '1', ex=120)
        # 定位扫描 stop 走 scan_id 语义
        _cab_ls_scan_id = None
        try:
            _cab_ls_scan_id = r.get('location_scan:active_scan_id')
        except Exception:
            pass
        if _cab_ls_scan_id:
            r.set('location_scan:stop', json.dumps({
                'scan_id': _cab_ls_scan_id, 'ts': time.time(),
            }), ex=120)

        # 天线偏差补偿：可选读取请求体 target_distance
        # web_server 只透传 antenna_bias，visual→RF 转换由 ptz_control.py 统一处理
        _cab_ab_bias = None
        _cab_td = payload.get('target_distance')
        if _cab_td not in (None, ''):
            try:
                _cab_ab_bias = build_antenna_bias(_cab_td)
                if not _cab_ab_bias.get("enabled"):
                    _cab_ab_bias = None
            except Exception:
                _cab_ab_bias = None

        cmd = {
            'action':        'move_to_best_capture',
            'pan':           pan,
            'tilt':          tilt,
            'mac':           target_mac,
            'channel':       channel,
            'bandwidth':     bandwidth,
            'pcap_filename': pcap_filename,
        }
        if capture_limit_f is not None:
            cmd['capture_time_limit'] = capture_limit_f
        if min_free_memory_mb_f is not None:
            cmd['min_free_memory_mb'] = min_free_memory_mb_f
        if min_free_disk_mb_f is not None:
            cmd['min_free_disk_mb'] = min_free_disk_mb_f
        if _cab_ab_bias is not None:
            cmd['antenna_bias'] = _cab_ab_bias
        r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))

        _resp = {
            'queued':        True,
            'message':       '已发送移动+抓包命令，云台正在移动到指定点位',
            'target_position': {'pan': pan, 'tilt': tilt},
            'mac':           target_mac,
            'channel':       channel,
            'bandwidth':     bandwidth,
            'capture_time_limit': capture_limit_f,
            'min_free_memory_mb': min_free_memory_mb_f,
            'min_free_disk_mb': min_free_disk_mb_f,
            'pcap_filename': pcap_filename,
            'capture_target_mac_match': 'any_addr',
        }
        if _cab_ab_bias is not None:
            _resp['antenna_bias'] = _cab_ab_bias
        return ok(_resp)

    # ─── S9: 星链设备查询接口 ──────────────────────────────────────────────────
    @app.get('/api/v1/starlink/detected')
    def api_get_starlink_detected():
        """
        查询当前会话中已识别的星链设备列表。
        数据来源：capture_worker 在扫描过程中实时写入 Redis starlink:detected_bssids。
        识别条件：F1(TPC=63) + F2(WMM字节) + F3(Vendor三联) 三条全部命中。
        """
        try:
            raw = r.get('starlink:detected_bssids')
            if not raw:
                return ok({
                    'count':   0,
                    'devices': {},
                    'message': '当前会话尚未识别到星链设备，请先执行客户端扫描',
                })
            devices = json.loads(raw)
            return ok({
                'count':   len(devices),
                'devices': devices,
            })
        except Exception as e:
            return err(f'查询星链设备失败: {str(e)}', http_status=500, app_code=-1)

    # ============= WiFi 连接接口 =============

    @app.post('/api/v1/wifi/connect')
    def api_wifi_connect():
        """发起 WiFi 连接尝试（非阻塞，立即返回）。"""
        try:
            body = request.get_json(force=True, silent=True) or {}
        except Exception:
            body = {}

        ssid = (body.get('ssid') or '').strip()
        bssid = (body.get('bssid') or '').strip() or None
        password = body.get('password', '')
        timeout = body.get('timeout', get_wifi_connect_config()['默认超时秒数'])

        # 参数校验
        if not ssid:
            return err('缺少 ssid', http_status=400, app_code=1001,
                       data={'reason': 'invalid_args'})
        if password and not (8 <= len(password) <= 63) and len(password) != 64:
            # WPA-PSK 密码 8~63 字节 ASCII 或 64 字节 HEX；空字符串表示开放网络
            return err('密码长度不符合 WPA-PSK 要求（8~63 字节 ASCII 或 64 字节 HEX）',
                       http_status=400, app_code=1001,
                       data={'reason': 'invalid_args'})
        if bssid:
            import re
            if not re.match(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$', bssid):
                return err('BSSID 格式错误', http_status=400, app_code=1001,
                           data={'reason': 'invalid_args'})
        wc_cfg = get_wifi_connect_config()
        if not (wc_cfg['最小超时秒数'] <= timeout <= wc_cfg['最大超时秒数']):
            return err(f'ttimeout 超出范围 ({wc_cfg["最小超时秒数"]}~{wc_cfg["最大超时秒数"]})',
                       http_status=400, app_code=1001,
                       data={'reason': 'invalid_args'})

        # 依赖检查
        ok_deps, missing = wifi_mode_utils.check_dependencies()
        if not ok_deps:
            return err(f'依赖工具缺失: {", ".join(missing)}', http_status=503, app_code=3001,
                       data={'reason': 'dependency_missing', 'missing': missing})

        # 防重入检查
        active, existing = _wifi_connect_active()
        if active:
            return err('已有 WiFi 连接任务正在运行', http_status=409, app_code=2001,
                       data={'reason': 'wifi_connect_already_running'})

        # 生成 connect_id
        connect_id = f'wificonn_{int(time.time() * 1000)}'

        # [P0] 入队前写 active_connect_id
        r.set(WIFI_CONNECT_ACTIVE_ID_KEY, connect_id)

        # 写初始状态
        r.set(WIFI_CONNECT_STATUS_KEY, json.dumps({
            'state': 'queued', 'active': True, 'terminal': False,
            'connect_id': connect_id, 'ssid': ssid, 'bssid': bssid,
            'timeout': timeout, 'started_at': time.time(),
        }))

        # ── 等价 stop 操作 ─────────────────────────────────────────────────
        _preempt_all_tasks(r, reason='wifi_connect_preempt')

        # best_position + 天线偏差补偿：分两步透传
        # 1. best_position 合法就透传（无论有无 target_distance）
        # 2. target_distance 有效且 bias enabled 才透传 antenna_bias
        _wc_best_pos = body.get('best_position')
        _wc_td = body.get('target_distance')
        _wc_ab_bias = None
        _wc_valid_best = False
        if _wc_best_pos and isinstance(_wc_best_pos, dict):
            try:
                _wc_bp_pan = float(_wc_best_pos.get("pan"))
                _wc_bp_tilt = float(_wc_best_pos.get("tilt"))
                import math as _m
                if _m.isfinite(_wc_bp_pan) and _m.isfinite(_wc_bp_tilt):
                    _wc_valid_best = True
            except (TypeError, ValueError):
                pass
        if _wc_valid_best and _wc_td not in (None, ''):
            try:
                _wc_ab_bias = build_antenna_bias(_wc_td)
                if not _wc_ab_bias.get("enabled"):
                    _wc_ab_bias = None
            except Exception:
                _wc_ab_bias = None

        # 入队命令
        _wc_cmd = {
            'action': 'wifi_connect',
            'connect_id': connect_id,
            'ssid': ssid,
            'bssid': bssid,
            'password': password,
            'timeout': timeout,
        }
        if _wc_valid_best:
            _wc_cmd['best_position'] = _wc_best_pos
        if _wc_ab_bias is not None:
            _wc_cmd['antenna_bias'] = _wc_ab_bias
        r.lpush(CAPTURE_COMMAND_QUEUE, json.dumps(_wc_cmd))

        _wc_resp = {
            'queued': True,
            'connect_id': connect_id,
            'message': '连接任务已排队，已发出停止当前任务信号',
        }
        if _wc_ab_bias is not None:
            _wc_resp['antenna_bias'] = _wc_ab_bias
        return ok(_wc_resp)

    @app.post('/api/v1/wifi/connect/stop')
    def api_wifi_connect_stop():
        """取消正在进行的 WiFi 连接。"""
        connect_id = r.get(WIFI_CONNECT_ACTIVE_ID_KEY)
        if not connect_id:
            # 尝试从 status 中兜底读取
            try:
                raw = r.get(WIFI_CONNECT_STATUS_KEY)
                if raw:
                    status = json.loads(raw)
                    if not status.get('terminal'):
                        connect_id = status.get('connect_id')
            except Exception:
                pass

        if not connect_id:
            return err('当前没有可取消的 WiFi 连接任务', http_status=404, app_code=2002,
                       data={'reason': 'wifi_connect_not_running'})

        # 写 stop key（JSON 格式，含 connect_id）
        r.set('wifi_connect:stop', json.dumps({
            'connect_id': connect_id,
            'reason': 'manual_stop',
            'ts': time.time(),
        }), ex=60)

        # patch 状态为 stopping
        try:
            raw = r.get(WIFI_CONNECT_STATUS_KEY)
            if raw:
                status = json.loads(raw)
                if status.get('connect_id') == connect_id:
                    status['state'] = 'stopping'
                    status['stop_requested'] = True
                    status['stop_requested_at'] = time.time()
                    r.set(WIFI_CONNECT_STATUS_KEY, json.dumps(status))
        except Exception:
            pass

        return ok({
            'stopping': True,
            'message': '已请求取消，请继续轮询 /api/v1/ptz/status',
        })

    @app.get('/api/v1/wifi/connect/status')
    def api_wifi_connect_status():
        """查询 WiFi 连接状态。"""
        try:
            raw = r.get(WIFI_CONNECT_STATUS_KEY)
            if not raw:
                return ok({'state': 'idle', 'active': False, 'terminal': True})
            status = json.loads(raw)
            if status.get('active') and status.get('started_at'):
                status['elapsed_seconds'] = round(time.time() - status['started_at'], 1)
            return ok(status)
        except Exception as e:
            return err(f'查询状态失败: {str(e)}', http_status=500, app_code=-1)

    @app.get('/api/v1/camera/images/<path:filename>')
    def api_serve_capture_image(filename):
        try:
            rel_path, image_path = _safe_capture_relative_path(filename)
        except ValueError:
            return err('非法文件名', http_status=400, app_code=1001)
        if not image_path.is_file():
            return err('文件不存在', http_status=404, app_code=2004)
        return send_from_directory(str(CAPTURES_DIR), rel_path)

    @app.post('/api/v1/camera/capture')
    def api_camera_capture():
        if cv2 is None:
            return err('服务端未安装 opencv-python', http_status=503, app_code=5001)
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return err('非法 JSON 请求体', http_status=400, app_code=1000)
        panorama = bool(payload.get('panorama') is True)
        try:
            K, dist, _ims = _load_calib_from_npz()
        except FileNotFoundError as e:
            return err(str(e), http_status=500, app_code=5002)
        except Exception as e:
            return err(f'加载标定失败: {e}', http_status=500, app_code=5003)

        if not panorama:
            raw_status = r.get(PTZ_STATUS_KEY)
            if not raw_status:
                return err('无法读取云台状态', http_status=503, app_code=5004)
            try:
                st = json.loads(raw_status)
                pos = st.get('position') or {}
                cap_pan = float(pos.get('pan', 0))
                cap_tilt = float(pos.get('tilt', 0))
            except Exception:
                return err('云台状态格式错误', http_status=500, app_code=5005)
            try:
                buf = _fetch_snapshot_bytes()
                img = _decode_snapshot_jpeg(buf)
                img = cv2.flip(img, -1)
                img_ud, new_K = _undistort_bgr(img, K, dist, alpha=0.0)
            except Exception as e:
                return err(f'拍照失败: {e}', http_status=500, app_code=5006)
            ts = time.strftime('%Y%m%d_%H%M%S', time.localtime())
            fname = f"snap_pan{cap_pan:.2f}_tilt{cap_tilt:.2f}_{ts}.jpg"
            out_path = CAPTURES_DIR / fname
            cv2.imwrite(str(out_path), img_ud)
            rel = f"/api/v1/camera/images/{fname}"
            return ok({
                'mode':         'single',
                'image_url':    rel,
                'capture_pan':  cap_pan,
                'capture_tilt': cap_tilt,
            })

        # 全景：范围与 grid / capture 一致，来自 Redis gimbal:default_config（已在库内与硬件限位求交）
        br = business_scan_range_from_redis(r, _HARDWARE_LIMITS_FALLBACK, _CONFIG_DEFAULT_SCAN)
        pan_min, pan_max = br['pan_min'], br['pan_max']
        tilt_min, tilt_max = br['tilt_min'], br['tilt_max']

        try:
            buf0 = _fetch_snapshot_bytes()
            ref_img = _decode_snapshot_jpeg(buf0)
            ref_img = cv2.flip(ref_img, -1)
            ref_ud, ref_newK = _undistort_bgr(ref_img, K, dist, alpha=0.0)
            fh, fw = ref_ud.shape[:2]
            fx = float(ref_newK[0, 0])
            fy = float(ref_newK[1, 1])
            fov_h = math.degrees(2 * math.atan(fw / (2.0 * fx)))
            fov_v = math.degrees(2 * math.atan(fh / (2.0 * fy)))
            trusted_half_ratio = max(0.01, min(0.5, PANORAMA_TRUSTED_CORE_HALF_RATIO))
            trusted_overlap = max(0.0, min(0.9, PANORAMA_TRUSTED_OVERLAP))
            try:
                stitch_crop_w_ratio = max(0.05, min(1.0, float(os.getenv('HUGIN_SOURCE_CROP_WIDTH_RATIO', '0.55'))))
            except Exception:
                stitch_crop_w_ratio = 0.55
            try:
                stitch_crop_h_ratio = max(0.05, min(1.0, float(os.getenv('HUGIN_SOURCE_CROP_HEIGHT_RATIO', '1.0'))))
            except Exception:
                stitch_crop_h_ratio = 1.0
            grid_fov_h = math.degrees(2 * math.atan((fw * stitch_crop_w_ratio) / (2.0 * fx)))
            grid_fov_v = math.degrees(2 * math.atan((fh * stitch_crop_h_ratio) / (2.0 * fy)))
            if pan_max - pan_min <= fov_h:
                grid_fov_h = fov_h
            if tilt_max - tilt_min <= fov_v:
                grid_fov_v = fov_v
        except Exception as e:
            return err(f'全景初始化失败: {e}', http_status=500, app_code=5007)

        shots, _sp, _st = compute_shot_grid(
            pan_min, pan_max, tilt_min, tilt_max, grid_fov_h, grid_fov_v, trusted_overlap
        )
        shots_meta = []
        total_shots = len(shots)
        app.logger.info(
            f"📷 全景拍照开始: {total_shots} 个拍摄点, 范围 pan=[{pan_min},{pan_max}] tilt=[{tilt_min},{tilt_max}], "
            f"完整FOV=({fov_h:.1f},{fov_v:.1f}) 可信FOV=({grid_fov_h:.1f},{grid_fov_v:.1f}) "
            f"trusted_core_half_ratio={trusted_half_ratio:.2f} trusted_overlap={trusted_overlap:.2f} "
            f"stitch_crop_ratio=({stitch_crop_w_ratio:.2f},{stitch_crop_h_ratio:.2f})"
        )
        for i, (pan, tilt) in enumerate(shots, 1):
            app.logger.info(f"📷 [{i}/{total_shots}] 移动到 pan={pan:.1f}° tilt={tilt:.1f}°")
            cmd = {
                'action':           'move_absolute',
                'pan':              round(float(pan), 2),
                'tilt':             round(float(tilt), 2),
                'pan_step_size':    20.0,
                'tilt_step_size':   10.0,
            }
            r.lpush(PTZ_COMMAND_QUEUE, json.dumps(cmd, ensure_ascii=False))
            if not _wait_ptz_settled(pan, tilt):
                return err(f'云台到位超时: pan={pan} tilt={tilt}', http_status=504, app_code=5008)
            time.sleep(PANORAMA_AE_SETTLE_SEC)
            try:
                buf = _fetch_snapshot_bytes()
                im = _decode_snapshot_jpeg(buf)
                im = cv2.flip(im, -1)
                im_ud, _ = _undistort_bgr(im, K, dist, alpha=0.0)
            except Exception as e:
                return err(f'全景采集中拍照失败: {e}', http_status=500, app_code=5009)
            shots_meta.append({'pan': float(pan), 'tilt': float(tilt), 'img': im_ud})
            app.logger.info(f"📷 [{i}/{total_shots}] 拍照完成")
        app.logger.info(f"📷 全景采集完成，开始拼接...")

        if not shots_meta:
            return err('拍摄点为空，请检查扫描范围与标定', http_status=400, app_code=1010)

        if build_hugin_panorama_from_shots is None:
            return err(
                f'Hugin 全景运行模块不可用，请检查 hugin_panorama_runtime.py: {HUGIN_IMPORT_ERROR}',
                http_status=500,
                app_code=5010,
            )
        try:
            hugin_result = build_hugin_panorama_from_shots(
                shots_meta=shots_meta,
                new_camera_matrix=ref_newK,
                captures_dir=CAPTURES_DIR,
                pan_range=(pan_min, pan_max),
                tilt_range=(tilt_min, tilt_max),
                range_source=br.get('source'),
            )
        except Exception as e:
            app.logger.exception("Hugin panorama stitching failed")
            return err(f'Hugin 全景拼接失败: {e}', http_status=500, app_code=5011)

        jpg_name = hugin_result['image_name']
        meta = hugin_result['metadata']
        timings = meta.get('timings') or {}
        if timings:
            timing_text = ", ".join(
                f"{name}={float(seconds):.2f}s"
                for name, seconds in timings.items()
                if isinstance(seconds, (int, float))
            )
            app.logger.info(f"📷 Hugin 全景阶段耗时: {timing_text}")
        correction_summary = meta.get('correction_summary') or {}
        if correction_summary:
            app.logger.info(
                "📷 Hugin correction统计: "
                f"candidates={correction_summary.get('total_candidates')} "
                f"sampled={correction_summary.get('sampled_candidates')} "
                f"accepted={correction_summary.get('accepted_points')} "
                f"primary={correction_summary.get('primary_accepted_points')}/"
                f"{correction_summary.get('primary_attempted_points')} "
                f"fallback={correction_summary.get('fallback_accepted_points')}/"
                f"{correction_summary.get('fallback_attempted_points')} "
                f"cross_source={correction_summary.get('fallback_cross_source_selected_points')} "
                f"rejected_large_shift={correction_summary.get('rejected_large_shift_points')} "
                f"elapsed={correction_summary.get('elapsed_seconds')}s"
            )
        r.set(CAMERA_PANORAMA_META_KEY, json.dumps(meta, ensure_ascii=False))
        return ok({
            'mode':      'panorama',
            'image_url': f'/api/v1/camera/images/{jpg_name}',
            'metadata':  meta,
        })

    @app.post('/api/v1/camera/coordinate_convert')
    def api_camera_coordinate_convert():
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            return err('非法 JSON 请求体', http_status=400, app_code=1000)
        has_px = 'pixels' in payload
        has_ang = 'angles' in payload
        if has_px == has_ang:
            return err('须且仅能指定 pixels 或 angles 之一', http_status=400, app_code=1001)

        mode = payload.get('mode', 'single')
        image_url = payload.get('image_url')

        # ── 全景图模式：线性映射 ──────────────────────────────────────────
        if mode == 'panorama':
            try:
                pano_meta = _panorama_meta_from_image_url(image_url)
            except ValueError as e:
                return err(str(e), http_status=400, app_code=1002)
            except FileNotFoundError as e:
                return err(str(e), http_status=404, app_code=2004)
            except LookupError as e:
                return err(str(e), http_status=404, app_code=2001)
            except Exception:
                return err('全景图元数据格式错误', http_status=500, app_code=5004)
            try:
                pan_min, pan_max = [float(v) for v in pano_meta['pan_range']]
                tilt_min, tilt_max = [float(v) for v in pano_meta['tilt_range']]
                canvas_w, canvas_h = [int(v) for v in pano_meta['canvas_size']]
            except Exception:
                return err('全景图元数据格式错误', http_status=500, app_code=5004)

            if pano_meta.get('backend') == 'hugin' and pano_meta.get('pmap_path'):
                if hugin_pixels_to_angles_pmap is None or hugin_angles_to_pixels_pmap is None:
                    return err('Hugin PMAP 坐标转换模块不可用', http_status=500, app_code=5012)
                pmap_path = Path(str(pano_meta.get('pmap_path'))).expanduser()
                if not pmap_path.exists():
                    return err(f'Hugin PMAP 文件不存在: {pmap_path}', http_status=404, app_code=2002)
                session_json = pano_meta.get('session_json')
                if not session_json:
                    return err('Hugin PMAP 缺少 session_json，无法换算云台角度', http_status=500, app_code=5015)
                session_path = Path(str(session_json)).expanduser()
                if not session_path.exists():
                    return err(f'Hugin session.json 不存在: {session_path}', http_status=404, app_code=2002)
                try:
                    trusted_core_half_ratio = float(payload.get('trusted_core_half_ratio', 0.20))
                except Exception:
                    return err('trusted_core_half_ratio 参数格式错误', http_status=400, app_code=1001)
                if has_px:
                    pixels = payload.get('pixels') or []
                    points = []
                    for item in pixels:
                        try:
                            points.append((float(item[0]), float(item[1])))
                        except Exception:
                            return err('pixels 格式须为 [[px,py],...]', http_status=400, app_code=1001)
                    try:
                        converted = hugin_pixels_to_angles_pmap(
                            pmap_path,
                            session_path,
                            points,
                            trusted_core_half_ratio=trusted_core_half_ratio,
                        )
                    except Exception as e:
                        app.logger.exception("Hugin PMAP pixel->angle conversion failed")
                        return err(f'Hugin PMAP 全景像素转角度失败: {e}', http_status=500, app_code=5016)
                    out_angles = []
                    for item in converted:
                        if not item.get('has_source'):
                            out_angles.append({
                                'pan': None,
                                'tilt': None,
                                'method': item.get('method'),
                                'reliable': False,
                                'in_frame': bool(item.get('in_frame', False)),
                                'has_source': False,
                            })
                            continue
                        out_angles.append({
                            'pan': round(float(item.get('pan')), 4),
                            'tilt': round(float(item.get('tilt')), 4),
                            'method': item.get('method'),
                            'reliable': bool(item.get('reliable', False)),
                            'match_score': item.get('match_score'),
                            'base_angle': item.get('base_angle'),
                            'visual_shift_px': item.get('visual_shift_px'),
                            'source': item.get('source'),
                            'trusted_core': bool(item.get('trusted_core', False)),
                            'trusted_core_half_ratio': item.get('trusted_core_half_ratio'),
                            'center_norm': item.get('center_norm'),
                            'coverage_count': item.get('coverage_count'),
                            'pmap': item.get('pmap'),
                        })
                    return ok({'angles': out_angles, 'mapping': 'pmap'})

                angles = payload.get('angles') or []
                angle_values = []
                for item in angles:
                    try:
                        angle_values.append((float(item['pan']), float(item['tilt'])))
                    except Exception:
                        return err('angles 格式须为 [{"pan":..,"tilt":..},...]', http_status=400, app_code=1001)
                try:
                    converted = hugin_angles_to_pixels_pmap(
                        pmap_path,
                        session_path,
                        angle_values,
                        trusted_core_half_ratio=trusted_core_half_ratio,
                    )
                except Exception as e:
                    app.logger.exception("Hugin PMAP angle->pixel conversion failed")
                    return err(f'Hugin PMAP 全景角度转像素失败: {e}', http_status=500, app_code=5017)
                out_pixels = []
                for item in converted:
                    if not item.get('in_frame'):
                        out_pixels.append({'px': None, 'py': None, 'in_frame': False, 'reliable': False})
                        continue
                    px = int(round(float(item.get('px'))))
                    py = int(round(float(item.get('py'))))
                    out_pixels.append({
                        'px': px,
                        'py': py,
                        'in_frame': 0 <= px < canvas_w and 0 <= py < canvas_h,
                        'method': item.get('method'),
                        'reliable': bool(item.get('reliable', False)),
                        'match_score': item.get('match_score'),
                        'source': item.get('source'),
                        'geometric': item.get('geometric'),
                        'trusted_core': bool(item.get('trusted_core', False)),
                        'trusted_core_half_ratio': item.get('trusted_core_half_ratio'),
                        'center_norm': item.get('center_norm'),
                        'coverage_count': item.get('coverage_count'),
                        'pmap': item.get('pmap'),
                    })
                return ok({'pixels': out_pixels, 'mapping': 'pmap'})

            if pano_meta.get('backend') == 'hugin' and pano_meta.get('map_path'):
                if hugin_pixels_to_angles_visual is None or hugin_angles_to_pixels_visual is None:
                    return err('Hugin 坐标转换模块不可用', http_status=500, app_code=5012)
                map_path = Path(str(pano_meta.get('map_path'))).expanduser()
                if not map_path.exists():
                    return err(f'Hugin 全景映射文件不存在: {map_path}', http_status=404, app_code=2002)
                try:
                    visual_radius = int(payload.get('visual_radius', 96))
                    visual_patch = int(payload.get('visual_patch', 41))
                    trusted_core_half_ratio = float(payload.get('trusted_core_half_ratio', 0.20))
                    use_visual_match = bool(payload.get('use_visual_match', False))
                except Exception:
                    return err('visual_radius/visual_patch/trusted_core_half_ratio/use_visual_match 参数格式错误', http_status=400, app_code=1001)

                if has_px:
                    pixels = payload.get('pixels') or []
                    points = []
                    for item in pixels:
                        try:
                            points.append((float(item[0]), float(item[1])))
                        except Exception:
                            return err('pixels 格式须为 [[px,py],...]', http_status=400, app_code=1001)
                    try:
                        converted = hugin_pixels_to_angles_visual(
                            map_path,
                            points,
                            radius=visual_radius,
                            patch=visual_patch,
                            trusted_core_half_ratio=trusted_core_half_ratio,
                            use_visual_match=use_visual_match,
                        )
                    except Exception as e:
                        app.logger.exception("Hugin pixel->angle conversion failed")
                        return err(f'Hugin 全景像素转角度失败: {e}', http_status=500, app_code=5013)
                    out_angles = []
                    for item in converted:
                        out_angles.append({
                            'pan': round(float(item.get('pan')), 4),
                            'tilt': round(float(item.get('tilt')), 4),
                            'method': item.get('method'),
                            'reliable': bool(item.get('reliable', False)),
                            'match_score': item.get('match_score'),
                            'base_angle': item.get('base_angle'),
                            'visual_shift_px': item.get('visual_shift_px'),
                            'source': item.get('source'),
                            'trusted_core': bool(item.get('trusted_core', False)),
                            'trusted_core_half_ratio': item.get('trusted_core_half_ratio'),
                            'center_norm': item.get('center_norm'),
                            'coverage_count': item.get('coverage_count'),
                            'correction': item.get('correction'),
                        })
                    return ok({'angles': out_angles})

                angles = payload.get('angles') or []
                angle_values = []
                for item in angles:
                    try:
                        angle_values.append((float(item['pan']), float(item['tilt'])))
                    except Exception:
                        return err('angles 格式须为 [{"pan":..,"tilt":..},...]', http_status=400, app_code=1001)
                try:
                    converted = hugin_angles_to_pixels_visual(
                        map_path,
                        angle_values,
                        radius=visual_radius,
                        patch=visual_patch,
                        trusted_core_half_ratio=trusted_core_half_ratio,
                        use_visual_match=use_visual_match,
                    )
                except Exception as e:
                    app.logger.exception("Hugin angle->pixel conversion failed")
                    return err(f'Hugin 全景角度转像素失败: {e}', http_status=500, app_code=5014)
                out_pixels = []
                for item in converted:
                    if not item.get('in_frame'):
                        out_pixels.append({'px': None, 'py': None, 'in_frame': False, 'reliable': False})
                        continue
                    px = int(round(float(item.get('px'))))
                    py = int(round(float(item.get('py'))))
                    out_pixels.append({
                        'px': px,
                        'py': py,
                        'in_frame': 0 <= px < canvas_w and 0 <= py < canvas_h,
                        'method': item.get('method'),
                        'reliable': bool(item.get('reliable', False)),
                        'match_score': item.get('match_score'),
                        'source': item.get('source'),
                        'geometric': item.get('geometric'),
                        'trusted_core': bool(item.get('trusted_core', False)),
                        'trusted_core_half_ratio': item.get('trusted_core_half_ratio'),
                        'center_norm': item.get('center_norm'),
                        'correction': item.get('correction'),
                    })
                return ok({'pixels': out_pixels})

            if has_px:
                pixels = payload.get('pixels') or []
                out_angles = []
                for item in pixels:
                    try:
                        px, py = int(item[0]), int(item[1])
                    except Exception:
                        return err('pixels 格式须为 [[px,py],...]', http_status=400, app_code=1001)
                    if canvas_w <= 1 or canvas_h <= 1:
                        return err('全景图尺寸无效', http_status=500, app_code=5005)
                    p_out = pan_min + (px / (canvas_w - 1)) * (pan_max - pan_min)
                    t_out = tilt_max - (py / (canvas_h - 1)) * (tilt_max - tilt_min)
                    out_angles.append({'pan': round(p_out, 4), 'tilt': round(t_out, 4)})
                return ok({'angles': out_angles})

            angles = payload.get('angles') or []
            out_pixels = []
            for item in angles:
                try:
                    tp = float(item['pan'])
                    tt = float(item['tilt'])
                except Exception:
                    return err('angles 格式须为 [{"pan":..,"tilt":..},...]', http_status=400, app_code=1001)
                px = round((tp - pan_min) / (pan_max - pan_min) * (canvas_w - 1))
                py = round((tilt_max - tt) / (tilt_max - tilt_min) * (canvas_h - 1))
                in_frame = 0 <= px < canvas_w and 0 <= py < canvas_h
                out_pixels.append({'px': int(px), 'py': int(py), 'in_frame': in_frame})
            return ok({'pixels': out_pixels})

        # ── 单张图模式：球面投影 ──────────────────────────────────────────
        try:
            cap_pan = float(payload['capture_pan'])
            cap_tilt = float(payload['capture_tilt'])
        except (KeyError, TypeError, ValueError):
            try:
                parsed_angles = _single_capture_angles_from_image_url(image_url)
            except ValueError as e:
                return err(str(e), http_status=400, app_code=1003)
            except FileNotFoundError as e:
                return err(str(e), http_status=404, app_code=2004)
            if parsed_angles is None:
                return err('capture_pan/capture_tilt 缺失或无效', http_status=400, app_code=1001)
            cap_pan, cap_tilt = parsed_angles

        if cv2 is None:
            return err('服务端未安装 opencv-python', http_status=503, app_code=5001)
        try:
            K, dist, (iw, ih) = _load_calib_from_npz()
            fx, fy, cx, cy = _intrinsics_for_coord(K, dist, iw, ih)
        except FileNotFoundError as e:
            return err(str(e), http_status=500, app_code=5002)
        except Exception as e:
            return err(f'加载标定失败: {e}', http_status=500, app_code=5003)

        if has_px:
            pixels = payload.get('pixels') or []
            out_angles = []
            for item in pixels:
                try:
                    px, py = int(round(float(item[0]))), int(round(float(item[1])))
                except Exception:
                    return err('pixels 格式须为 [[px,py],...]', http_status=400, app_code=1001)
                p_out, t_out = pixel_to_absolute_ptz(
                    px, py, cx, cy, fx, fy, cap_pan, cap_tilt
                )
                out_angles.append({'pan': round(p_out, 1), 'tilt': round(t_out, 1)})
            return ok({'angles': out_angles})

        angles = payload.get('angles') or []
        out_pixels = []
        for item in angles:
            try:
                tp = round(float(item['pan']), 1)
                tt = round(float(item['tilt']), 1)
            except Exception:
                return err('angles 格式须为 [{"pan":..,"tilt":..},...]', http_status=400, app_code=1001)
            px, py, inf = absolute_ptz_to_pixel(
                tp, tt, cap_pan, cap_tilt, cx, cy, fx, fy, img_w=iw, img_h=ih
            )
            if px is None:
                out_pixels.append({'px': None, 'py': None, 'in_frame': False})
            else:
                out_pixels.append({'px': int(px), 'py': int(py), 'in_frame': inf})
        return ok({'pixels': out_pixels})

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=True)
