"""
===================================================================================
云台控制+抓包入库系统 - PTZ控制模块
===================================================================================

"""

# 导入 'redis' 库，用于连接和操作 Redis 数据库，这是我们系统通信的核心。
import redis
# 导入 'json' 库，用于将 Python 字典（我们的命令和状态）转换成 JSON 字符串，以便在 Redis 中存储和传输。
import json
# 导入 'time' 库，用于实现延时（如 time.sleep()）和获取当前时间戳。
import time
from datetime import datetime
import math
import itertools
# 使用您确认无误的导入方式获取真实 PtzControl（实际串口控制完全由您的库实现）
from pelco_d_controller import PtzControl
# 仅在本地添加线程安全包装，不修改您的库文件；
# 包装器的作用：保证串口收/发在同一把锁内串行，避免"查询帧"和"移动帧"交叉。
# from ptz_lib import ThreadSafePtzControl
import os
import sys
from enum import Enum
# [新增] 导入日志系统
import logging
import logging.handlers

from grid_utils import (
    set_global_grid_config,
    clear_all_global_grids,
    GLOBAL_PAN_MIN,
    GLOBAL_PAN_MAX,
    GLOBAL_TILT_MIN,
    GLOBAL_TILT_MAX,
)
from history_store import add_snapshot, finish_project, utc_now_iso
from panorama_sampling import select_sampling_points, select_sampling_indices
from config_loader import (
    get_camera_config as _get_camera_config_fn,
    get_location_scan_config as _get_location_scan_config_fn,
    get_full_scan_filter_config as _get_full_scan_filter_config_fn,
    get_full_scan_config as _get_full_scan_config_fn,
)
from ptz_range_redis import business_scan_range_from_redis, full_area_precheck_range_from_redis
from antenna_bias_utils import visual_point_to_rf, visual_range_to_rf
from full_scan_wifi_mode import build_full_scan_wifi_configs, allowed_channels_from_configs
from full_scan_timing import FullScanTimingTrace
import urllib.request as _urllib_request
# ==================== 智能扫描配置 ====================
INTELLIGENT_SCAN_CONFIG = {
    'rssi_threshold_diff': 5,      # RSSI差值阈值（dBm）
    'max_iterations': 3,            # 最大迭代次数
    'min_range_size': 0.2,          # 最小扫描范围（度）
    'single_signal_range': 15.0,    # 单信号时的扫描范围（度）
    # 🔥 新增参数 - 简化版
    'retry_on_no_signal': False,    # 无信号时是否重复第一轮扫描（全天候模式）
    'retry_delay': 5.0,             # 重复扫描间隔（秒）
}
# ==================== 智能扫描Redis键名 ====================
INTELLIGENT_SCAN_ACTIVE_KEY = "intelligent_scan:active"      # 扫描激活标志
INTELLIGENT_SCAN_SIGNALS_KEY = "intelligent_scan:signals"    # 信号队列  
INTELLIGENT_SCAN_STATUS_KEY = "intelligent_scan:status"      # 扫描状态（仅ptz_control使用）
# ==================== 智能扫描核心功能 (最终版 - 健壮架构) ====================
def _is_scan_round_finished(r):
    """【最终版】检查当前扫描轮次是否完成"""
    try:
        ptz_status = json.loads(r.get(PTZ_STATUS_KEY) or '{}')
        # 如果云台状态不是 AUTO_SCANNING，说明这一轮扫描已跑完或被中断
        return ptz_status.get("state") != "AUTO_SCANNING"
    except Exception:
        return True


def _set_ptz_status_preserving_capture(r, status):
    try:
        current_status = json.loads(r.get(PTZ_STATUS_KEY) or '{}')
        capture_at_best = current_status.get('capture_at_best')
        if isinstance(capture_at_best, dict) and capture_at_best.get('active'):
            status.setdefault('capture_at_best', capture_at_best)
    except Exception:
        pass
    r.set(PTZ_STATUS_KEY, json.dumps(status))


# ── 统一状态管理 helper ──────────────────────────────────────────

def _deep_merge(base, patch):
    """递归合并 patch 到 base，patch 优先。base 会被原地修改。"""
    for key, val in patch.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
    return base


def _merge_ptz_status_patch(current, patch):
    """合并一次状态 patch，并维持全面扫描 stopping 的单调性。"""
    current = current if isinstance(current, dict) else {}
    patch = patch if isinstance(patch, dict) else {}
    current_full_scan = current.get('full_scan')
    patch_full_scan = patch.get('full_scan')
    same_scan = bool(
        isinstance(current_full_scan, dict)
        and isinstance(patch_full_scan, dict)
        and (
            not current_full_scan.get('scan_id')
            or not patch_full_scan.get('scan_id')
            or current_full_scan.get('scan_id') == patch_full_scan.get('scan_id')
        )
    )
    if (
        same_scan
        and current_full_scan.get('terminal') is True
        and patch_full_scan.get('terminal') is not True
    ):
        # 同一任务一旦终态，不允许迟到的进度/心跳写入复活生命周期。
        patch_full_scan.update({
            'active': False,
            'state': current_full_scan.get('state') or 'stopped',
            'terminal': True,
            'stop_requested': False,
            'reason': current_full_scan.get('reason'),
            'phase': current_full_scan.get('phase') or 'stopped',
        })
    if (
        same_scan
        and current_full_scan.get('state') == 'stopping'
        and current_full_scan.get('terminal') is False
        and patch_full_scan.get('terminal') is not True
    ):
        patch_full_scan['active'] = False
        patch_full_scan['state'] = 'stopping'
        patch_full_scan['stop_requested'] = True
    _deep_merge(current, patch)
    current['ts'] = time.time()
    return current


def patch_ptz_status(r, patch):
    """
    通过 Redis WATCH 原子合并状态，避免 Web stop 与 worker 更新互相覆盖。
    """
    pipeline_factory = getattr(type(r), 'pipeline', None)
    if not callable(pipeline_factory):
        try:
            current = json.loads(r.get(PTZ_STATUS_KEY) or '{}')
        except Exception:
            current = {}
        current = _merge_ptz_status_patch(current, patch)
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
            current = _merge_ptz_status_patch(current, patch)
            pipe.multi()
            pipe.set(PTZ_STATUS_KEY, json.dumps(current, ensure_ascii=False))
            pipe.execute()
            return current
        except redis.exceptions.WatchError:
            continue
        finally:
            pipe.reset()
    raise RuntimeError("ptz:current_status 原子更新连续冲突")


def _generate_scan_id(prefix):
    """生成 scan_id，格式：{prefix}_{timestamp_ms}"""
    return f"{prefix}_{int(time.time() * 1000)}"


def _collect_and_filter_signals(r):
    """【重试版】收集并过滤信号数据 - 防止Redis队列重建时的竞态条件"""
    for attempt in range(3):  # 重试3次
        try:
            signals_raw = r.lrange(INTELLIGENT_SCAN_SIGNALS_KEY, 0, -1)
            if not signals_raw: 
                if attempt < 2:
                    time.sleep(0.1)
                    continue
                return []
            
            # 解析JSON数据
            signals = [json.loads(s) for s in signals_raw]
            if len(signals) == 0:
                if attempt < 2:
                    time.sleep(0.1)
                    continue
                return []
            
            # 按RSSI排序，找出最佳信号
            signals.sort(key=lambda x: x['rssi'], reverse=True)
            best_rssi = signals[0]['rssi']
            threshold = best_rssi - INTELLIGENT_SCAN_CONFIG['rssi_threshold_diff']
            
            # 严格过滤：只要好信号，差信号直接丢弃
            good_signals = [s for s in signals if s['rssi'] >= threshold]
            
            # 记录日志
            if len(good_signals) == 1:
                logger.info(f"智能扫描：收集到 {len(signals)} 个信号，只有1个好信号(RSSI={good_signals[0]['rssi']:.1f})，将使用单信号模式")
            else:
                logger.info(f"智能扫描：收集到 {len(signals)} 个信号，过滤后保留 {len(good_signals)} 个好信号")
            
            return good_signals
            
        except Exception as e:
            if attempt < 2:
                time.sleep(0.1)
                continue
            logger.error(f"收集信号数据失败: {e}")
            return []
    
    return []

def _calculate_next_scan_range(good_signals, current_pan_range, current_tilt_range):
    """【修复版】根据好信号计算新的扫描范围 - 不扩展，基于当前范围限制"""
    if len(good_signals) == 0:
        return None
    
    # 使用当前扫描范围作为边界
    current_pan_min, current_pan_max = current_pan_range
    current_tilt_min, current_tilt_max = current_tilt_range
    
    if len(good_signals) == 1:
        best_signal = good_signals[0]
        center_pan = best_signal['pan']
        center_tilt = best_signal['tilt']
        half_range = INTELLIGENT_SCAN_CONFIG['single_signal_range'] / 2
        
        # 基于当前范围限制
        next_pan_min = max(current_pan_min, center_pan - half_range)
        next_pan_max = min(current_pan_max, center_pan + half_range)
        next_tilt_min = max(current_tilt_min, center_tilt - half_range)
        next_tilt_max = min(current_tilt_max, center_tilt + half_range)
        
        return [next_pan_min, next_pan_max], [next_tilt_min, next_tilt_max]
    
    # 多信号模式 - 不扩展，直接使用信号分布范围
    pans = [s['pan'] for s in good_signals]
    tilts = [s['tilt'] for s in good_signals]
    min_pan, max_pan = min(pans), max(pans)
    min_tilt, max_tilt = min(tilts), max(tilts)
    
    # 🔥 关键修改：不扩展，直接使用信号分布范围
    next_pan_min = max(current_pan_min, min_pan)  # 不减去pan_expand
    next_pan_max = min(current_pan_max, max_pan)  # 不加上pan_expand
    next_tilt_min = max(current_tilt_min, min_tilt)  # 不减去tilt_expand
    next_tilt_max = min(current_tilt_max, max_tilt)  # 不加上tilt_expand
    
    # 确保最小范围
    if (next_pan_max - next_pan_min) < INTELLIGENT_SCAN_CONFIG['min_range_size']:
        center_pan = (next_pan_min + next_pan_max) / 2
        half_range = INTELLIGENT_SCAN_CONFIG['min_range_size'] / 2
        next_pan_max = min(current_pan_max, center_pan + half_range)
        next_pan_min = max(current_pan_min, center_pan - half_range)
    
    if (next_tilt_max - next_tilt_min) < INTELLIGENT_SCAN_CONFIG['min_range_size']:
        center_tilt = (next_tilt_min + next_tilt_max) / 2
        half_range = INTELLIGENT_SCAN_CONFIG['min_range_size'] / 2
        next_tilt_max = min(current_tilt_max, center_tilt + half_range)
        next_tilt_min = max(current_tilt_min, center_tilt - half_range)
    
    return [round(next_pan_min, 1), round(next_pan_max, 1)], [round(next_tilt_min, 1), round(next_tilt_max, 1)]

    final_pan_min = round(next_pan_min, 1)
    final_pan_max = round(next_pan_max, 1)
    final_tilt_min = round(next_tilt_min, 1)
    final_tilt_max = round(next_tilt_max, 1)
    return [final_pan_min, final_pan_max], [final_tilt_min, final_tilt_max]


def _start_scan_round(r, pan_range, tilt_range, round_num, ptz_obj=None):
    """【最终版】发起新一轮的扫描"""
    # 🔥 修改：智能扫描只在第1轮开始前去限位点（在start_intelligent_scan中完成）
    # 第2/3轮不再去限位点，避免影响扫描效率和数据
    
    # 🔥 移除固定1度的逻辑，改回动态计算
    pan_step_size = max(2.0, (pan_range[1] - pan_range[0]) / 5)
    tilt_step_size = max(2.0, (tilt_range[1] - tilt_range[0]) / 2)
    
    # 新增：将智能扫描的步径更新到Redis
    try:
        current_config = {
            'source_steph': float(pan_step_size),
            'source_stepv': float(tilt_step_size),
            'updated_by': f'intelligent_scan_round_{round_num}',
            'ts': time.time()
        }
        r.set('gimbal:current_config', json.dumps(current_config))
        logger.info(f"✅ 智能扫描第{round_num}轮步径已更新: ({pan_step_size:.1f}, {tilt_step_size:.1f})")
    except Exception as e:
        logger.error(f"❌ 更新智能扫描步径到 Redis 失败: {e}")
        
    scan_cmd = {
        "action": "start_auto_scan", 
        "pan_range": pan_range, 
        "tilt_range": tilt_range,
        "pan_step_size": round(pan_step_size, 1), 
        "tilt_step_size": round(tilt_step_size, 1),
        "step_delay": 1.0,
        "loop_mode": "once",  # 🔥 智能扫描强制使用once模式，每轮只扫描一次
        "skip_calibration": True  # 🔥 智能扫描：跳过限位点（第1轮已在启动时去过，第2/3轮不需要去）
    }

    r.lpush(PTZ_COMMAND_QUEUE, json.dumps(scan_cmd))

    logger.info(f"🤖 智能扫描: 启动第 {round_num} 轮, 范围 Pan: {pan_range[0]:.1f}-{pan_range[1]:.1f}, 模式: once (跳过限位点)")

def _move_to_best_point(r, good_signals):
    """【修正版】扫描结束后，移动到信号最好的那个点"""
    if not good_signals: 
        return
    best_signal = max(good_signals, key=lambda x: x['rssi'])
    # 【修正】使用 "move_absolute" 这个 action
    try:
        current_config = r.get('gimbal:current_config')
        if current_config:
            config = json.loads(current_config)
            pan_step_size = config.get('source_steph', 5.0)
            tilt_step_size = config.get('source_stepv', 5.0)
        else:
            # 如果没有配置，使用默认值
            pan_step_size = 5.0
            tilt_step_size = 5.0
            logger.warning("未找到步径配置，使用默认值")
    except Exception as e:
        logger.error(f"获取步径配置失败: {e}")
        pan_step_size = 5.0
        tilt_step_size = 5.0
    move_cmd = {
        "action": "move_absolute", 
        "pan": best_signal['pan'], 
        "tilt": best_signal['tilt'],
        "pan_step_size": pan_step_size,
        "tilt_step_size": tilt_step_size
    }
    r.lpush(PTZ_COMMAND_QUEUE, json.dumps(move_cmd))
    logger.info(f"🎯 智能扫描结束: 移至最佳点 Pan={best_signal['pan']:.2f}, Tilt={best_signal['tilt']:.2f}")


def _goto_calibration_point(ptz, r, context="未知", stop_check_fn=None):
    """
    【硬件限位校准】移动到(0, 0)触发限位开关，消除水平累积误差
    
    作用：水平轴长时间小步径移动会产生累积偏差，
         通过触碰0°限位开关强制归零，使串口读数与实际位置重新对齐
    
    Args:
        context: 调用场景说明（如：自动扫描开始、智能扫描第2轮、bounce模式）
    
    Returns:
        bool: 校准是否成功
    """
    # 🔥 保存原始抓包状态，并暂停抓包（防止移动过程中抓到错误位置的包）
    original_capture_status = r.get('capture:scan_status')
    if original_capture_status:
        original_capture_status = original_capture_status.decode('utf-8') if isinstance(original_capture_status, bytes) else original_capture_status
    
    try:
        def _cal_stop_requested():
            if stop_check_fn is None:
                return False
            try:
                return bool(stop_check_fn())
            except Exception:
                return False

        def _restore_capture_status():
            if original_capture_status:
                r.set('capture:scan_status', original_capture_status)
                logger.info(f"🔓 [限位校准] 恢复抓包状态为: {original_capture_status} - 场景: {context}")

        if _cal_stop_requested():
            logger.warning(f"🛑 [限位校准] 启动前检测到停止标志 - 场景: {context}")
            return False

        # 暂停抓包
        r.set('capture:scan_status', 'PREPARING')
        logger.info(f"🔒 [限位校准] 暂停抓包 - 场景: {context}")

        # 🔥 写入校准状态，前端可通过 ptz:current_status 感知当前正在去限位点校准
        try:
            _cal_pan, _cal_tilt = ptz.get_position()
        except Exception:
            _cal_pan, _cal_tilt = None, None
        patch_ptz_status(r, {
            "position": {"pan": _cal_pan, "tilt": _cal_tilt},
            "state": "CALIBRATING",
            "calibration": {
                "active": True,
                "context": context,
                "target": {"pan": 0.0, "tilt": 0.0}
            }
        })
        logger.info(f"📡 [限位校准] 已写入 CALIBRATING 状态 - 场景: {context}")

        # 获取当前位置
        initial_pan, initial_tilt = _cal_pan if _cal_pan is not None else 0.0, _cal_tilt if _cal_tilt is not None else 0.0
        logger.info(f"🎯 [限位校准] 开始执行硬件限位校准 - 场景: {context}, 当前位置: Pan={initial_pan:.2f}°, Tilt={initial_tilt:.2f}°")
        
        # 移动到限位点 (0, 0)
        calibration_pan = 0.0
        calibration_tilt = 0.0
        
        # 使用分轴移动，先垂直后水平
        def _cal_redis_update(p, t):
            patch_ptz_status(r, {
                "position": {"pan": round(p, 2), "tilt": round(t, 2)},
                "state": "CALIBRATING",
                "calibration": {
                    "active": True,
                    "context": context,
                    "target": {"pan": 0.0, "tilt": 0.0}
                }
            })
        if not safe_split_move(ptz, calibration_pan, calibration_tilt,
                               order='tilt_first', settle=2.0,
                               on_move_update=_cal_redis_update,
                               stop_check_fn=stop_check_fn):
            logger.error(f"❌ [限位校准] 移动到限位点失败 - 场景: {context}")
            _restore_capture_status()
            return False
        
        # 🔥 新增：主动轮询等待移动到位（最多等待20秒）
        # 逻辑：不盲目sleep，而是每0.2秒检查一次位置，直到两个轴都到达目标
        logger.info(f"⏳ [限位校准] 等待移动到位...")
        max_wait_time = 20.0  # 最大等待时间（超时保护）
        start_wait = time.time()
        tolerance = 0.1  # 允许1度误差（考虑到硬件精度）
        arrived = False  # 是否到位标志
        
        while time.time() - start_wait < max_wait_time:
            if _cal_stop_requested():
                logger.warning(f"🛑 [限位校准] 等待到位期间检测到停止标志 - 场景: {context}")
                _restore_capture_status()
                return False

            # 每次循环都读取实时位置
            actual_pan, actual_tilt = ptz.get_position()
            
            if actual_pan is not None and actual_tilt is not None:
                # 计算当前位置与目标位置的误差
                pan_error = abs(actual_pan - calibration_pan)
                tilt_error = abs(actual_tilt - calibration_tilt)
                
                # 只有当两个轴都在误差范围内，才认为到位
                if pan_error <= tolerance and tilt_error <= tolerance:
                    elapsed = time.time() - start_wait
                    logger.info(f"✅ [限位校准] 已到位 - 耗时: {elapsed:.2f}秒, 位置: Pan={actual_pan:.2f}°, Tilt={actual_tilt:.2f}°")
                    arrived = True
                    break  # 跳出循环，继续后续流程
            
            time.sleep(0.2)  # 每0.2秒检查一次，避免过于频繁占用资源
        
        # 如果超时了还没到位，记录警告
        if not arrived:
            logger.warning(f"⚠️ [限位校准] 等待到位超时 ({max_wait_time}秒)，但继续执行")
        
        # 等待触发限位开关并稳定（重要！确保限位开关真正触发）
        # 这个sleep是必要的，因为即使位置读数正确，也需要时间让限位开关物理触发
        stable_deadline = time.time() + 2.0
        while time.time() < stable_deadline:
            if _cal_stop_requested():
                logger.warning(f"🛑 [限位校准] 稳定等待期间检测到停止标志 - 场景: {context}")
                _restore_capture_status()
                return False
            time.sleep(min(0.2, stable_deadline - time.time()))
        
        # 最终验证位置
        actual_pan, actual_tilt = ptz.get_position()
        if actual_pan is not None and actual_tilt is not None:
            pan_error = abs(actual_pan - calibration_pan)
            tilt_error = abs(actual_tilt - calibration_tilt)
            logger.info(f"✅ [限位校准] 校准完成 - 场景: {context}, 最终读数: Pan={actual_pan:.2f}°, Tilt={actual_tilt:.2f}°, 误差: Pan={pan_error:.2f}°, Tilt={tilt_error:.2f}°")
        else:
            logger.warning(f"⚠️ [限位校准] 无法读取校准后位置，但继续执行 - 场景: {context}")
        
        # 🔥 恢复原始抓包状态
        _restore_capture_status()

        # ⚠️ 注意：此处不写任何状态。
        #    调用方需要在"移回目标点到位"之后，自行写入目标状态（如 INITIAL_SCANNING / MULTI_SCANNING 等）
        #    这样可以确保 CALIBRATING 状态贯穿整个"去限位点 + 移回目标点"的过程，不出现 IDLE 闪烁。
        logger.info(f"✅ [限位校准] 校准动作完成（状态由调用方控制）- 场景: {context}")

        return True
        
    except Exception as e:
        logger.error(f"❌ [限位校准] 校准过程异常 - 场景: {context}, 错误: {e}")
        
        # 🔥 异常时也要恢复原始抓包状态
        if original_capture_status:
            r.set('capture:scan_status', original_capture_status)
            logger.warning(f"⚠️ [限位校准] 异常后恢复抓包状态为: {original_capture_status} - 场景: {context}")
        
        return False


# --- 日志系统配置 ---
def setup_logging():
    """设置日志系统（仅输出到控制台，不写文件）。"""
    logger = logging.getLogger("ptz_worker")
    logger.setLevel(logging.DEBUG)

    # 清除已有的处理器（避免重复）
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    # 只使用控制台处理器，避免 /var/log 权限问题
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        '%(asctime)s,%(msecs)03d [%(levelname)s] [ptz_worker] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger

# 初始化日志系统
logger = setup_logging()

# Redis 服务器的主机地址。'localhost' 表示 Redis 和这个程序运行在同一台机器上。
REDIS_HOST = 'localhost'
# Redis 服务器的端口号，默认就是 6379。
REDIS_PORT = 6379
# 用于接收指令的 Redis "列表(List)" 的键名。Web 服务器会把命令推送到这个列表。
PTZ_COMMAND_QUEUE = 'ptz:command_queue'
# 用于存储云台实时状态的 Redis "字符串(String)" 的键名。这个 Worker 会不断更新它。
PTZ_STATUS_KEY = 'ptz:current_status'
# 云台连接的物理串口名称。在 Linux 上通常是 '/dev/ttyUSB0'  或类似的名称。
SERIAL_PORT = '/dev/ttyUSB0'  # !!! 请根据实际情况修改 !!!
# 云台的设备地址，根据您的协议文档，这里是 1。
SERIAL_ADDRESS = 1
# 串口通信的波特率，根据您的协议文档，这里是 9600。
SERIAL_BAUDRATE = 9600

# 默认的相对移动步长 (度)。
# 前端在发送 "up", "down", "left", "right" 命令时，可以通过 "offset" 参数指定步长。
DEFAULT_MOVE_OFFSET = 5.0

def round_angle_to_2dp(angle):
    """
    将角度值四舍五入到小数点后第二位
    
    Args:
        angle (float): 原始角度值
        
    Returns:
        float: 四舍五入后的角度值，保留两位小数
    """
    if angle is None:
        return None
    try:
        return round(float(angle),1)
    except (ValueError, TypeError):
        return 0.0

def check_serial_exists(serial_port):
    return os.path.exists(serial_port)

def handle_serial_error(serial_port):
    logger.critical(f"检测到串口 {serial_port} 不存在，程序将退出")  
    # 更新 Redis 状态为错误状态
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        status = {
            "ts": time.time(), 
            "position": {"pan": None, "tilt": None}, 
            "state": "ERROR",
            "error": "串口断开，等待重启"
        }
        r.set(PTZ_STATUS_KEY, json.dumps(status))
        logger.info("已更新 Redis 状态为错误")
    except Exception as e:
        logger.warning(f"更新 Redis 状态失败: {e}")
    
    # 退出程序
    logger.info("程序即将退出，等待 systemd 重启服务")
    sys.exit(1)




# 全局设备状态对象
class PTZDeviceState:
    """云台设备状态管理类 - 内存缓存，避免频繁硬件查询"""
    
    def __init__(self):
        self._position = {"pan": 0.0, "tilt": 0.0}
        self._state = "IDLE"
        self._last_update_time = time.time()
    
    def update_position(self, pan, tilt):
        """更新位置信息"""
        if pan is not None and tilt is not None:
            self._position["pan"] = float(pan)
            self._position["tilt"] = float(tilt)
            self._last_update_time = time.time()
    
    def update_state(self, state):
        """更新设备状态"""
        if state:
            self._state = str(state)
            self._last_update_time = time.time()
    
    def get_position(self):
        """获取当前位置"""
        return self._position.copy()
    
    def get_state(self):
        """获取当前状态"""
        return self._state
    
    def get_last_update_time(self):
        """获取最后更新时间"""
        return self._last_update_time
    
    def is_stale(self, max_age_seconds=30):
        """检查状态是否过期"""
        return (time.time() - self._last_update_time) > max_age_seconds

device_state = PTZDeviceState()


# ==================== 自适应扫描配置 ====================


# --- 默认扫描配置（环境变量可覆盖，与 Web 端保持一致） ---
PTZ_DEFAULT_PAN_MIN = float(os.getenv('PTZ_DEFAULT_PAN_MIN', '0.0'))
PTZ_DEFAULT_PAN_MAX = float(os.getenv('PTZ_DEFAULT_PAN_MAX', '100.0'))
PTZ_DEFAULT_TILT_MIN = float(os.getenv('PTZ_DEFAULT_TILT_MIN', '0.0'))
PTZ_DEFAULT_TILT_MAX = float(os.getenv('PTZ_DEFAULT_TILT_MAX', '20.0'))
PTZ_DEFAULT_PAN_STEP = float(os.getenv('PTZ_DEFAULT_PAN_STEP', '20.0'))
PTZ_DEFAULT_TILT_STEP = float(os.getenv('PTZ_DEFAULT_TILT_STEP', '5.0'))

# 硬件限位默认值；ptz_process_main 启动后会用驱动读取到的真实限位覆盖。
PAN_MIN = 0.0
PAN_MAX = 347.0
TILT_MIN = -85.0
TILT_MAX = 20.0

# ====================================================================================
# 多点扫描 / 全面扫描 可配置参数
# ====================================================================================

# --- 初始扫描 / 手动模式探测：每个信道/带宽配置的监听时长（秒）---
# 值越大，探测越准确；42个配置 × 20秒 ≈ 14分钟完成一次完整探测
INITIAL_SCAN_DWELL_TIME = 20

# --- 多点扫描：每个点位的停留采集时长（秒）---
# 云台到达点位后，capture_worker 在该点位采集的持续时间
MULTI_SCAN_DWELL_TIME = 3

# --- 多点扫描：无信号时的延长采集时长（秒）---
# 若当前点位未采集到任何目标信号，额外延长的等待时间
MULTI_SCAN_EXTEND_TIME = 0.1

# --- 多点扫描：两轮之间的间隔时长（秒）---
# 完成一轮多点扫描后，等待此时间再开始下一轮
MULTI_SCAN_REPEAT_INTERVAL = 60

# --- 多点扫描（auto模式）：每隔多少轮刷新一次 MAC 列表 ---
# 每隔 N 轮重新执行一次初始扫描，追加新发现的 MAC
MULTI_SCAN_MAC_REFRESH_ROUNDS = 5

# --- 多点扫描（auto模式）：MAC 淘汰阈值 ---
# 连续 N 次刷新扫描都未出现的 MAC，从列表中移除
# 设为 0 则禁用淘汰（回退到只增不减模式）
MULTI_SCAN_MAC_MISS_THRESHOLD = 3

# --- 多点扫描（auto模式）：刷新扫描每个配置的停留时长（秒）---
# 刷新扫描不需要像初始扫描那么久，3秒足够判断 MAC 是否还在
MULTI_SCAN_REFRESH_DWELL_TIME = 3

# --- 全面扫描：各阶段每信道停留时长（秒）---
# 三个阶段的 dwell_time 各自独立，从 config.json["全面扫描"] 读取
FULL_SCAN_COARSE_DWELL_TIME   = _get_full_scan_config_fn().get("粗扫每信道停留时长",   0.4)  # 粗扫（0.25~0.6s）
FULL_SCAN_FINE_DWELL_TIME     = _get_full_scan_config_fn().get("细扫每信道停留时长",   0.6)  # 细扫（0.5~1s）
FULL_SCAN_DEVIATION_DWELL_TIME = _get_full_scan_config_fn().get("偏差区每信道停留时长", 0.8)  # 偏差区（0.5~1.2s）
# 兼容旧代码引用（取粗扫值作为默认值，实际代码应使用各阶段专属常量）
FULL_SCAN_DWELL_TIME = FULL_SCAN_COARSE_DWELL_TIME

# ====================================================================================
# S8 客户端扫描配置
# ====================================================================================
# --- 客户端扫描：防边界误判扩展步数 ---
# 扫描范围在用户选区基础上，各方向自动向外扩展 N 步。
# 扩展格子作为"guard zone"：若信号峰值在 guard zone 内而非选区内，
# 说明客户端很可能在选区外，系统会降低 confidence 级别。
# 推荐值：1。需实测调整，记录在 idea/ROADMAP.md S8 章节。
CLIENT_SCAN_GUARD_STEPS = 1

# --- 客户端扫描：每个点位的驻留采集时长（秒）---
# 云台到达点位后，在该方向采集客户端帧的持续时间。
# 值越大，采集越充分；需在「发现率 vs 总耗时」之间取得平衡。
# 推荐值：5。需实测调整，记录在 idea/ROADMAP.md S8 章节。
CLIENT_SCAN_DEFAULT_DWELL_TIME = 3


# ====================================================================================
# S1 全景拍照配置
# ====================================================================================
CAMERA_CAPTURE_TIMEOUT = 10  # HTTP 截图超时（秒）
PANORAMA_RAW_DIR = "/home/ultiwill/camera"
# 摄像头截图 URL 从 config.json 读取（前端 mjpeg-streamer 服务）
_cam_cfg = _get_camera_config_fn()
CAMERA_SNAPSHOT_URL = _cam_cfg.get("截图地址", "http://127.0.0.1:8080/?action=snapshot")
CURRENT_PROJECT_KEY = "history:current_project"

# ====================================================================================
# 初始扫描配置（基于 1.json，排除 NOHT 和 80MHz）
# ====================================================================================
# 完整的信道/带宽配置列表，用于初始点扫描发现所有可用的MAC地址
INITIAL_SCAN_CONFIGS = [
    # 2.4GHz频段 (信道 1-13)，仅保留 HT20（Beacon/Probe 均在主信道发，HT20 足够发现所有设备）
    # HT40+/HT40- 已注释，如需恢复完整扫描取消注释即可（约多耗时 2.4x）
    {"channel": 1,  "bandwidth": "HT20"},
    # {"channel": 1,  "bandwidth": "HT40+"},
    {"channel": 2,  "bandwidth": "HT20"},
    # {"channel": 2,  "bandwidth": "HT40+"},
    {"channel": 3,  "bandwidth": "HT20"},
    # {"channel": 3,  "bandwidth": "HT40+"},
    {"channel": 4,  "bandwidth": "HT20"},
    # {"channel": 4,  "bandwidth": "HT40+"},
    {"channel": 5,  "bandwidth": "HT20"},
    # {"channel": 5,  "bandwidth": "HT40+"},
    # {"channel": 5,  "bandwidth": "HT40-"},
    {"channel": 6,  "bandwidth": "HT20"},
    # {"channel": 6,  "bandwidth": "HT40+"},
    # {"channel": 6,  "bandwidth": "HT40-"},
    {"channel": 7,  "bandwidth": "HT20"},
    # {"channel": 7,  "bandwidth": "HT40+"},
    # {"channel": 7,  "bandwidth": "HT40-"},
    {"channel": 8,  "bandwidth": "HT20"},
    # {"channel": 8,  "bandwidth": "HT40+"},
    # {"channel": 8,  "bandwidth": "HT40-"},
    {"channel": 9,  "bandwidth": "HT20"},
    # {"channel": 9,  "bandwidth": "HT40+"},
    # {"channel": 9,  "bandwidth": "HT40-"},
    {"channel": 10, "bandwidth": "HT20"},
    # {"channel": 10, "bandwidth": "HT40-"},
    {"channel": 11, "bandwidth": "HT20"},
    # {"channel": 11, "bandwidth": "HT40-"},
    {"channel": 12, "bandwidth": "HT20"},
    # {"channel": 12, "bandwidth": "HT40-"},
    {"channel": 13, "bandwidth": "HT20"},
    # {"channel": 13, "bandwidth": "HT40-"},
    # 5GHz频段 (信道 36-165)，仅保留 HT20
    # HT40+/HT40- 已注释
    {"channel": 36,  "bandwidth": "HT20"},
    # {"channel": 36,  "bandwidth": "HT40+"},
    {"channel": 40,  "bandwidth": "HT20"},
    # {"channel": 40,  "bandwidth": "HT40+"},
    # {"channel": 40,  "bandwidth": "HT40-"},
    {"channel": 44,  "bandwidth": "HT20"},
    # {"channel": 44,  "bandwidth": "HT40+"},
    # {"channel": 44,  "bandwidth": "HT40-"},
    {"channel": 48,  "bandwidth": "HT20"},
    # {"channel": 48,  "bandwidth": "HT40+"},
    # {"channel": 48,  "bandwidth": "HT40-"},
    {"channel": 52,  "bandwidth": "HT20"},
    # {"channel": 52,  "bandwidth": "HT40+"},
    # {"channel": 52,  "bandwidth": "HT40-"},
    {"channel": 56,  "bandwidth": "HT20"},
    # {"channel": 56,  "bandwidth": "HT40+"},
    # {"channel": 56,  "bandwidth": "HT40-"},
    {"channel": 60,  "bandwidth": "HT20"},
    # {"channel": 60,  "bandwidth": "HT40+"},
    # {"channel": 60,  "bandwidth": "HT40-"},
    {"channel": 64,  "bandwidth": "HT20"},
    # {"channel": 64,  "bandwidth": "HT40-"},
    {"channel": 149, "bandwidth": "HT20"},
    # {"channel": 149, "bandwidth": "HT40+"},
    {"channel": 153, "bandwidth": "HT20"},
    # {"channel": 153, "bandwidth": "HT40+"},
    # {"channel": 153, "bandwidth": "HT40-"},
    {"channel": 157, "bandwidth": "HT20"},
    # {"channel": 157, "bandwidth": "HT40+"},
    # {"channel": 157, "bandwidth": "HT40-"},
    {"channel": 161, "bandwidth": "HT20"},
    # {"channel": 161, "bandwidth": "HT40+"},
    # {"channel": 161, "bandwidth": "HT40-"},
    {"channel": 165, "bandwidth": "HT20"},
    # {"channel": 165, "bandwidth": "HT40-"},
]

# 定位扫描用配置（包含 HT40，精确）
LOCATION_SCAN_CONFIGS = []
_CHANNEL_BW_MAP = {
    1: ["HT20", "HT40+"],
    2: ["HT20", "HT40+"],
    3: ["HT20", "HT40+"],
    4: ["HT20", "HT40+"],
    5: ["HT20", "HT40+", "HT40-"],
    6: ["HT20", "HT40+", "HT40-"],
    7: ["HT20", "HT40+", "HT40-"],
    8: ["HT20", "HT40+", "HT40-"],
    9: ["HT20", "HT40+", "HT40-"],
    10: ["HT20", "HT40-"],
    11: ["HT20", "HT40-"],
    12: ["HT20", "HT40-"],
    13: ["HT20", "HT40-"],
    36: ["HT20", "HT40+"],
    40: ["HT20", "HT40+", "HT40-"],
    44: ["HT20", "HT40+", "HT40-"],
    48: ["HT20", "HT40+", "HT40-"],
    52: ["HT20", "HT40+", "HT40-"],
    56: ["HT20", "HT40+", "HT40-"],
    60: ["HT20", "HT40+", "HT40-"],
    64: ["HT20", "HT40-"],
    149: ["HT20", "HT40+"],
    153: ["HT20", "HT40+", "HT40-"],
    157: ["HT20", "HT40+", "HT40-"],
    161: ["HT20", "HT40+", "HT40-"],
    165: ["HT20", "HT40-"],
}
for _ch, _bws in _CHANNEL_BW_MAP.items():
    for _bw in _bws:
        LOCATION_SCAN_CONFIGS.append({"channel": _ch, "bandwidth": _bw})

# --- 启动回零位配置（默认启用，可通过环境变量关闭或修改目标） ---
PTZ_HOME_ON_START = os.getenv('PTZ_HOME_ON_START', '1') == '1'
PTZ_HOME_PAN = float(os.getenv('PTZ_HOME_PAN', '90.0'))
PTZ_HOME_TILT = float(os.getenv('PTZ_HOME_TILT', '0.0'))

# --- 高精度控制配置 ---
PTZ_HIGH_PRECISION_TOLERANCE = float(os.getenv('PTZ_HIGH_PRECISION_TOLERANCE', '0.1'))  # 主要误差容限
PTZ_MOVE_COMPLETE_TOLERANCE = float(os.getenv('PTZ_MOVE_COMPLETE_TOLERANCE', str(max(PTZ_HIGH_PRECISION_TOLERANCE, 0.2))))  # 实际到位判定容差
PTZ_PROGRESS_DETECTION_THRESHOLD = float(os.getenv('PTZ_PROGRESS_DETECT_THRESHOLD', '0.1'))  # 进展检测阈值
PTZ_AUTO_REPAIR_THRESHOLD = float(os.getenv('PTZ_AUTO_REPAIR_THRESHOLD', str(PTZ_MOVE_COMPLETE_TOLERANCE)))  # 自动修复阈值

# --- 自动扫描循环模式（bounce/restart/once），默认 bounce（往返） ---
PTZ_AUTO_SCAN_LOOP_MODE = os.getenv('PTZ_AUTO_SCAN_LOOP_MODE', 'bounce').lower()

# --- 首次关键移动后的稳定等待（秒），用于避免设备未就绪导致的"无进展"误判 ---
PTZ_INITIAL_SETTLE_SEC = float(os.getenv('PTZ_INITIAL_SETTLE_SEC', '1.0'))
PTZ_SPLIT_SETTLE_SEC = float(os.getenv('PTZ_SPLIT_SETTLE_SEC', '2.0'))
FULL_SCAN_DIRECT_MOVE_TOLERANCE = float(
    os.getenv('FULL_SCAN_DIRECT_MOVE_TOLERANCE', '0.2')
)
FULL_SCAN_DIRECT_MOVE_RELIABLE_DELTA = float(
    os.getenv('FULL_SCAN_DIRECT_MOVE_RELIABLE_DELTA', '1.0')
)
FULL_SCAN_DIRECT_MOVE_RELAY_OFFSET = float(
    os.getenv('FULL_SCAN_DIRECT_MOVE_RELAY_OFFSET', '3.0')
)
FULL_SCAN_DIRECT_MOVE_TIMEOUT = float(
    os.getenv('FULL_SCAN_DIRECT_MOVE_TIMEOUT', '100.0')
)

# ==================== 智能扫描配置（已合并到上面） ====================
# 注意：INTELLIGENT_SCAN_CONFIG 已在文件开头定义，这里不再重复定义

# 智能扫描Redis键名
INTELLIGENT_SCAN_STATUS_KEY = 'intelligent_scan:status'      # 智能扫描状态
INTELLIGENT_SCAN_STOP_KEY = 'intelligent_scan:stop'         # 停止标志
# ===== 智能扫描状态变量 =====
first_point_settled = False  # 首次到达扫描起点后的稳定等待标记
intelligent_scan_active = False  # 智能扫描是否激活
intelligent_scan_round = 0       # 当前扫描轮次
# ===== 智能扫描状态变量 =====
def _normalize_pan(angle):
    try:
        a = float(angle) % 360.0
        if a < 0:
            a += 360.0
        return a
    except Exception:
        return angle


def _shortest_angular_diff_deg(current, target):
    """
    计算 current→target 的最短角度差（-180, 180]，用于环绕比较
    """
    try:
        cur = _normalize_pan(current)
        tar = _normalize_pan(target)
        diff = (tar - cur + 540.0) % 360.0 - 180.0
        return diff
    except Exception:
        return target - current


def _is_pan_close(current, target, tolerance):
    try:
        return abs(_shortest_angular_diff_deg(current, target)) <= tolerance
    except Exception:
        return abs((current or 0) - (target or 0)) <= tolerance


def safe_move_to_pan_tilt(ptz, pan_angle, tilt_angle):
    """
    安全移动到指定位置，处理0°边界和环绕问题（简化实现，避免过度干预底层驱动）
    """
    try:
        target_pan = _normalize_pan(pan_angle)
        current_pan, current_tilt = ptz.get_position()
        if current_pan is not None:
            cur_norm = _normalize_pan(current_pan)
            if abs(cur_norm - 0.0) < 0.5 and abs(target_pan - 0.0) >= 0.5:
                try:
                    logger.info("当前位置接近0°，先微移到1°以离开边界", extra={
                        "current_pan": current_pan,
                        "target_pan": target_pan
                    })
                    ptz.move_to_pan_tilt(pan_angle=1.0, tilt_angle=current_tilt if current_tilt is not None else tilt_angle)
                    time.sleep(0.3)
                except Exception:
                    pass
        # 若目标为0°，先移动到1°再回到0°以规避边界问题
        if abs(target_pan - 0.0) < 0.5:
            try:
                logger.info("检测到目标Pan为0°，先移动到1°再回到0°以规避边界问题", extra={
                    "target_pan": target_pan
                })
                ptz.move_to_pan_tilt(pan_angle=1.0, tilt_angle=tilt_angle)
                time.sleep(0.3)
            except Exception:
                pass
        # 执行实际移动
        logger.info("执行移动", extra={
            "target_pan": target_pan,
            "target_tilt": tilt_angle
        })
        ptz.move_to_pan_tilt(pan_angle=target_pan, tilt_angle=tilt_angle)
        return True
    except Exception as e:
        logger.error("安全移动失败", extra={
            "target_pan": pan_angle,
            "target_tilt": tilt_angle,
            "error": str(e),
            "error_type": type(e).__name__
        })
        return False


def high_precision_move(ptz, target_pan, target_tilt):
    """
    高精度移动：专门用于微调，使用更小的误差容限
    """
    try:
        target_pan = _normalize_pan(target_pan)
        current_pan, current_tilt = ptz.get_position()
        
        # 高精度移动：直接移动到目标位置
        logger.info("执行高精度微调移动", extra={
            "target_pan": target_pan,
            "target_tilt": target_tilt,
            "current_pan": current_pan,
            "current_tilt": current_tilt
        })
        
        ptz.move_to_pan_tilt(pan_angle=target_pan, tilt_angle=target_tilt)
        return True
    except Exception as e:
        logger.error("高精度移动失败", extra={
            "target_pan": target_pan,
            "target_tilt": target_tilt,
            "error": str(e),
            "error_type": type(e).__name__
        })
        return False


def safe_split_move(ptz, target_pan, target_tilt, order='pan_first', settle=None,
                    on_move_update=None, stop_check_fn=None):
    """
    分轴绝对移动：先移动一个轴，等待，再移动另一个轴，避免"两轴同时下发"被覆盖。
    order: 'pan_first' 或 'tilt_first'
    settle: 两步之间的等待秒数（默认 PTZ_SPLIT_SETTLE_SEC）
    """
    if settle is None:
        settle = PTZ_SPLIT_SETTLE_SEC
    try:
        # 二级计时：move 子步骤
        _t_move_start = time.time()
        _t_sub_start = time.time()
        _get_position_count = 0
        _get_position_total_ms = 0.0

        def _stop_ptz_for_manual_stop(context):
            try:
                ptz.stop()
            except Exception as e:
                logger.warning(f"🛑 {context} 时停止云台失败: {e}")

        def _move_stop_requested():
            if stop_check_fn is None:
                return False
            try:
                return bool(stop_check_fn())
            except Exception:
                return False

        def _sleep_with_stop(duration):
            deadline = time.time() + max(0.0, float(duration or 0))
            while time.time() < deadline:
                if _move_stop_requested():
                    logger.warning("🛑 分轴移动等待期间检测到停止标志")
                    _stop_ptz_for_manual_stop("分轴移动等待")
                    return False
                time.sleep(min(0.2, max(0.0, deadline - time.time())))
            return True

        if _move_stop_requested():
            logger.warning("🛑 分轴移动启动前检测到停止标志")
            _stop_ptz_for_manual_stop("分轴移动启动前")
            return False

        _t_sub_start = time.time()
        cur_pan, cur_tilt = ptz.get_position()
        _get_position_count += 1
        _t_get_position_init = time.time() - _t_sub_start
        _get_position_total_ms += _t_get_position_init * 1000
        # 规范化当前水平角
        if cur_pan is not None:
            cur_pan = _normalize_pan(cur_pan)
        target_pan_n = _normalize_pan(target_pan)

        # 判断哪一轴需要移动
        need_pan = True
        need_tilt = True
        try:
            if cur_pan is not None and _is_pan_close(cur_pan, target_pan_n, PTZ_HIGH_PRECISION_TOLERANCE):
                need_pan = False
        except Exception:
            pass
        try:
            if cur_tilt is not None and abs(cur_tilt - target_tilt) < PTZ_HIGH_PRECISION_TOLERANCE:
                need_tilt = False
        except Exception:
            pass

        # 内部等待函数：仅等待某一轴到位（带监控日志）
        def wait_axis(timeout_s=10.0, pan_tol=0.5, tilt_tol=0.2, wait_pan=False, wait_tilt=False):
            nonlocal _get_position_count, _get_position_total_ms
            start_ts = time.time()
            last_log_time = 0
            axis_name = "水平" if wait_pan else ("垂直" if wait_tilt else "两轴")
            
            while time.time() - start_ts < timeout_s:
                if _move_stop_requested():
                    logger.warning(f"🛑 [{axis_name}] 移动等待期间检测到停止标志")
                    _stop_ptz_for_manual_stop(f"{axis_name}移动等待")
                    return False

                _t_sub_start = time.time()
                p, t = ptz.get_position()
                _get_position_count += 1
                _t_get_pos = time.time() - _t_sub_start
                _get_position_total_ms += _t_get_pos * 1000

                if p is not None:
                    p = _normalize_pan(p)
                
                pan_ok = True if not wait_pan else (p is not None and _is_pan_close(p, target_pan_n, pan_tol))
                tilt_ok = True if not wait_tilt else (t is not None and abs(t - target_tilt) <= tilt_tol)
                
                # 🔥 添加监控日志（每秒输出一次）
                current_time = time.time()
                if current_time - last_log_time >= 1.0:
                    logger.info(f"🔄 [{axis_name}移动监控] 当前: Pan={p:.2f}°, Tilt={t:.2f}° → 目标: Pan={target_pan_n:.2f}°, Tilt={target_tilt:.2f}°, get_position耗时={_t_get_pos*1000:.1f}ms")
                    last_log_time = current_time
                # 实时回调：通知调用方当前位置（用于更新 Redis 等外部状态）
                if on_move_update is not None and p is not None and t is not None:
                    try:
                        on_move_update(p, t)
                    except Exception:
                        pass
                
                if pan_ok and tilt_ok:
                    logger.info(f"✅ [{axis_name}] 已到位: Pan={p:.2f}°, Tilt={t:.2f}°")
                    return True
                time.sleep(0.2)
            
            # 超时
            final_p, final_t = ptz.get_position()
            logger.warning(f"⚠️ [{axis_name}] 移动超时: 当前=({final_p:.2f}°, {final_t:.2f}°), 目标=({target_pan_n:.2f}°, {target_tilt:.2f}°)")
            return False
        
        # 🔥 如果仅一轴变化，使用带监控的单轴移动
        if need_pan and not need_tilt:
            logger.info(f"🔄 [单轴移动] 仅移动水平轴: {cur_pan:.2f}° → {target_pan_n:.2f}° (垂直已到位: {cur_tilt:.2f}°)")
            if not safe_move_to_pan_tilt(ptz, target_pan_n, cur_tilt if cur_tilt is not None else target_tilt):
                return False
            # 等待水平到位
            _t_sub_start = time.time()
            if not wait_axis(timeout_s=100.0, wait_pan=True):
                logger.warning("⚠️ 单轴水平移动超时")
                return False
            _t_wait_axis = time.time() - _t_sub_start
            _t_move_total = time.time() - _t_move_start
            logger.info(
                f"⏱️ [move] 子计时: "
                f"get_position_init={_t_get_position_init*1000:.1f}ms, "
                f"get_position_count={_get_position_count}, "
                f"get_position_total={_get_position_total_ms:.1f}ms, "
                f"wait_axis={_t_wait_axis*1000:.1f}ms, "
                f"total={_t_move_total*1000:.1f}ms, "
                f"target_pan={target_pan_n:.2f}, target_tilt={target_tilt:.2f}, "
                f"relay_used={False}"
            )
            return True
        
        if need_tilt and not need_pan:
            logger.info(f"🔄 [单轴移动] 仅移动垂直轴: {cur_tilt:.2f}° → {target_tilt:.2f}° (水平已到位: {cur_pan:.2f}°)")
            if not safe_move_to_pan_tilt(ptz, target_pan_n if cur_pan is None else cur_pan, target_tilt):
                return False
            # 等待垂直到位
            _t_sub_start = time.time()
            if not wait_axis(timeout_s=100.0, wait_tilt=True):
                logger.warning("⚠️ 单轴垂直移动超时")
                return False
            _t_wait_axis = time.time() - _t_sub_start
            _t_move_total = time.time() - _t_move_start
            logger.info(
                f"⏱️ [move] 子计时: "
                f"get_position_init={_t_get_position_init*1000:.1f}ms, "
                f"get_position_count={_get_position_count}, "
                f"get_position_total={_get_position_total_ms:.1f}ms, "
                f"wait_axis={_t_wait_axis*1000:.1f}ms, "
                f"total={_t_move_total*1000:.1f}ms, "
                f"target_pan={target_pan_n:.2f}, target_tilt={target_tilt:.2f}, "
                f"relay_used={False}"
            )
            return True

        # 两轴都需要变化 → 分两步
        if order == 'tilt_first':
            # 先垂直
            logger.info(f"🔄 [分轴移动-步骤1] 先移动垂直轴: {cur_tilt:.2f}° → {target_tilt:.2f}°")
            if not safe_move_to_pan_tilt(ptz, cur_pan if cur_pan is not None else target_pan_n, target_tilt):
                return False
            # 等待垂直到位，避免下一步取消前一步动作
            _t_sub_start = time.time()
            if not wait_axis(timeout_s=100.0, wait_tilt=True):
                logger.warning("⚠️ 垂直轴移动超时")
                return False
            _t_wait_axis_1 = time.time() - _t_sub_start
            if not _sleep_with_stop(settle):
                return False
            
            logger.info(f"🔄 [分轴移动-步骤2] 再移动水平轴: {cur_pan:.2f}° → {target_pan_n:.2f}°")
            if _move_stop_requested():
                logger.warning("🛑 第二轴移动前检测到停止标志")
                _stop_ptz_for_manual_stop("第二轴移动前")
                return False
            if not safe_move_to_pan_tilt(ptz, target_pan_n, target_tilt):
                return False
            # 🔥 等待第二步到位
            _t_sub_start = time.time()
            if not wait_axis(timeout_s=100.0, wait_pan=True):
                logger.warning("⚠️ 水平轴（步骤2）移动超时")
                return False
            _t_wait_axis_2 = time.time() - _t_sub_start
            _t_move_total = time.time() - _t_move_start
            logger.info(
                f"⏱️ [move] 子计时: "
                f"get_position_init={_t_get_position_init*1000:.1f}ms, "
                f"get_position_count={_get_position_count}, "
                f"get_position_total={_get_position_total_ms:.1f}ms, "
                f"wait_axis_1={_t_wait_axis_1*1000:.1f}ms, "
                f"wait_axis_2={_t_wait_axis_2*1000:.1f}ms, "
                f"total={_t_move_total*1000:.1f}ms, "
                f"target_pan={target_pan_n:.2f}, target_tilt={target_tilt:.2f}, "
                f"relay_used={False}"
            )
            return True
        else:
            # 默认先水平
            logger.info(f"🔄 [分轴移动-步骤1] 先移动水平轴: {cur_pan:.2f}° → {target_pan_n:.2f}°")
            if not safe_move_to_pan_tilt(ptz, target_pan_n, cur_tilt if cur_tilt is not None else target_tilt):
                return False
            # 等待水平方向到位，避免下一步取消前一步动作
            _t_sub_start = time.time()
            if not wait_axis(timeout_s=100.0, wait_pan=True):
                logger.warning("⚠️ 水平轴移动超时")
                return False
            _t_wait_axis_1 = time.time() - _t_sub_start
            if not _sleep_with_stop(settle):
                return False
            
            logger.info(f"🔄 [分轴移动-步骤2] 再移动垂直轴: {cur_tilt:.2f}° → {target_tilt:.2f}°")
            if _move_stop_requested():
                logger.warning("🛑 第二轴移动前检测到停止标志")
                _stop_ptz_for_manual_stop("第二轴移动前")
                return False
            if not safe_move_to_pan_tilt(ptz, target_pan_n, target_tilt):
                return False
            # 🔥 等待第二步到位
            _t_sub_start = time.time()
            if not wait_axis(timeout_s=100.0, wait_tilt=True):
                logger.warning("⚠️ 垂直轴（步骤2）移动超时")
                return False
            _t_wait_axis_2 = time.time() - _t_sub_start
            _t_move_total = time.time() - _t_move_start
            logger.info(
                f"⏱️ [move] 子计时: "
                f"get_position_init={_t_get_position_init*1000:.1f}ms, "
                f"get_position_count={_get_position_count}, "
                f"get_position_total={_get_position_total_ms:.1f}ms, "
                f"wait_axis_1={_t_wait_axis_1*1000:.1f}ms, "
                f"wait_axis_2={_t_wait_axis_2*1000:.1f}ms, "
                f"total={_t_move_total*1000:.1f}ms, "
                f"target_pan={target_pan_n:.2f}, target_tilt={target_tilt:.2f}, "
                f"relay_used={False}"
            )
            return True
    except Exception as e:
        logger.error("分轴移动失败", extra={
            "target_pan": target_pan,
            "target_tilt": target_tilt,
            "error": str(e),
            "error_type": type(e).__name__
        })
        return False


def _full_scan_pose_deltas(current_pan, current_tilt, target_pan, target_tilt):
    """返回全面扫描直接移动的 Pan 最短环绕差和 Tilt 绝对差。"""
    return (
        abs(float(_shortest_angular_diff_deg(current_pan, target_pan))),
        abs(float(target_tilt) - float(current_tilt)),
    )


def _full_scan_pose_reached(current_pan, current_tilt, target_pan, target_tilt,
                            tolerance=FULL_SCAN_DIRECT_MOVE_TOLERANCE):
    try:
        pan_delta, tilt_delta = _full_scan_pose_deltas(
            current_pan,
            current_tilt,
            target_pan,
            target_tilt,
        )
        numeric_tolerance = float(tolerance) + 1e-9
        return pan_delta <= numeric_tolerance and tilt_delta <= numeric_tolerance
    except (TypeError, ValueError):
        return False


def _full_scan_delta_needs_relay(delta):
    """(0.2°, 1°) 是当前硬件不可靠的小移动区间；边界值不包含。"""
    delta = abs(float(delta))
    return (
        FULL_SCAN_DIRECT_MOVE_TOLERANCE + 1e-9 < delta
        < FULL_SCAN_DIRECT_MOVE_RELIABLE_DELTA - 1e-9
    )


def _full_scan_direct_move_bounds(move_range):
    """把任务外层执行范围与硬件限位求交。"""
    try:
        pan_range = move_range["pan_range"]
        tilt_range = move_range["tilt_range"]
        pan_min = max(float(PAN_MIN), min(float(pan_range[0]), float(pan_range[1])))
        pan_max = min(float(PAN_MAX), max(float(pan_range[0]), float(pan_range[1])))
        tilt_min = max(float(TILT_MIN), min(float(tilt_range[0]), float(tilt_range[1])))
        tilt_max = min(float(TILT_MAX), max(float(tilt_range[0]), float(tilt_range[1])))
        if pan_min > pan_max or tilt_min > tilt_max:
            return None
        return pan_min, pan_max, tilt_min, tilt_max
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _select_full_scan_relay_pose(target_pan, target_tilt, move_range):
    """
    为全面扫描直接移动选择一次中继姿态。

    优先使用目标水平角 ±3°，水平无解时再尝试垂直角 ±3°；
    中继必须同时位于任务外层执行范围和硬件限位内。
    """
    bounds = _full_scan_direct_move_bounds(move_range)
    if bounds is None:
        return None
    pan_min, pan_max, tilt_min, tilt_max = bounds
    target_pan = float(_normalize_pan(target_pan))
    target_tilt = float(target_tilt)
    offset = float(FULL_SCAN_DIRECT_MOVE_RELAY_OFFSET)

    candidates = (
        (target_pan + offset, target_tilt, "pan_positive"),
        (target_pan - offset, target_tilt, "pan_negative"),
        (target_pan, target_tilt + offset, "tilt_positive"),
        (target_pan, target_tilt - offset, "tilt_negative"),
    )
    for relay_pan, relay_tilt, direction in candidates:
        if (
            pan_min <= relay_pan <= pan_max
            and tilt_min <= relay_tilt <= tilt_max
            and _full_scan_pose_reached(
                relay_pan,
                relay_tilt,
                target_pan,
                target_tilt,
                tolerance=FULL_SCAN_DIRECT_MOVE_RELIABLE_DELTA,
            ) is False
        ):
            return {
                "pan": round(relay_pan, 2),
                "tilt": round(relay_tilt, 2),
                "direction": direction,
            }
    return None


def _wait_full_scan_direct_pose(ptz, target_pan, target_tilt, *,
                                stop_check_fn=None,
                                timeout_s=FULL_SCAN_DIRECT_MOVE_TIMEOUT,
                                poll_interval=0.2,
                                trajectory=None):
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    last_position = (None, None)
    trajectory = list(trajectory or [])
    while True:
        if stop_check_fn is not None:
            try:
                if stop_check_fn():
                    try:
                        ptz.stop()
                    except Exception:
                        pass
                    return {
                        "success": False,
                        "reason": "stopped",
                        "position": last_position,
                        "trajectory": trajectory,
                    }
            except Exception:
                pass

        query_started_at = time.time()
        try:
            current_pan, current_tilt = ptz.get_position()
        except Exception:
            current_pan, current_tilt = None, None
        query_finished_at = time.time()
        last_position = (current_pan, current_tilt)
        if current_pan is not None and current_tilt is not None:
            trajectory.append({
                "ts": (query_started_at + query_finished_at) / 2.0,
                "pan": float(current_pan),
                "tilt": float(current_tilt),
                "query_duration_ms": round(
                    (query_finished_at - query_started_at) * 1000.0,
                    3,
                ),
            })
        if (
            current_pan is not None
            and current_tilt is not None
            and _full_scan_pose_reached(
                current_pan,
                current_tilt,
                target_pan,
                target_tilt,
            )
        ):
            return {
                "success": True,
                "reason": "reached",
                "position": last_position,
                "trajectory": trajectory,
            }
        if time.monotonic() >= deadline:
            try:
                ptz.stop()
            except Exception:
                pass
            return {
                "success": False,
                "reason": "timeout",
                "position": last_position,
                "trajectory": trajectory,
            }
        time.sleep(max(0.01, float(poll_interval)))


def _run_full_scan_direct_leg(ptz, target_pan, target_tilt, *,
                              stop_check_fn=None,
                              timeout_s=FULL_SCAN_DIRECT_MOVE_TIMEOUT,
                              poll_interval=0.2):
    """下发一次双轴绝对移动，并同时轮询两轴到位。"""
    if stop_check_fn is not None:
        try:
            if stop_check_fn():
                try:
                    ptz.stop()
                except Exception:
                    pass
                return {
                    "success": False,
                    "reason": "stopped",
                    "position": (None, None),
                    "trajectory": [],
                }
        except Exception:
            pass

    trajectory = []
    query_started_at = time.time()
    try:
        current_pan, current_tilt = ptz.get_position()
    except Exception:
        current_pan, current_tilt = None, None
    query_finished_at = time.time()
    if current_pan is not None and current_tilt is not None:
        trajectory.append({
            "ts": (query_started_at + query_finished_at) / 2.0,
            "pan": float(current_pan),
            "tilt": float(current_tilt),
            "query_duration_ms": round(
                (query_finished_at - query_started_at) * 1000.0,
                3,
            ),
        })
    if (
        current_pan is not None
        and current_tilt is not None
        and _full_scan_pose_reached(
            current_pan,
            current_tilt,
            target_pan,
            target_tilt,
        )
    ):
        return {
            "success": True,
            "reason": "already_reached",
            "position": (current_pan, current_tilt),
            "trajectory": trajectory,
        }

    try:
        command_result = ptz.move_to_pan_tilt(
            pan_angle=float(_normalize_pan(target_pan)),
            tilt_angle=float(target_tilt),
        )
    except Exception as exc:
        return {
            "success": False,
            "reason": f"command_exception:{type(exc).__name__}",
            "position": (current_pan, current_tilt),
            "trajectory": trajectory,
        }
    if command_result is False:
        return {
            "success": False,
            "reason": "command_failed",
            "position": (current_pan, current_tilt),
            "trajectory": trajectory,
        }
    return _wait_full_scan_direct_pose(
        ptz,
        target_pan,
        target_tilt,
        stop_check_fn=stop_check_fn,
        timeout_s=timeout_s,
        poll_interval=poll_interval,
        trajectory=trajectory,
    )


def _full_scan_direct_move_with_relay(ptz, target_pan, target_tilt, move_range, *,
                                      stop_check_fn=None,
                                      timeout_s=FULL_SCAN_DIRECT_MOVE_TIMEOUT,
                                      poll_interval=0.2):
    """
    全面扫描专用直接双轴移动；不可靠小步径或首次失败时最多中继一次。

    返回结构化结果，供当前点位失败处理以及后续路径证据阶段判断是否丢弃本段。
    """
    target_pan = float(_normalize_pan(target_pan))
    target_tilt = float(target_tilt)
    bounds = _full_scan_direct_move_bounds(move_range)
    if bounds is None:
        return {"success": False, "relay_used": False, "reason": "invalid_move_range"}
    pan_min, pan_max, tilt_min, tilt_max = bounds
    if not (
        pan_min <= target_pan <= pan_max
        and tilt_min <= target_tilt <= tilt_max
    ):
        return {"success": False, "relay_used": False, "reason": "target_out_of_range"}

    try:
        current_pan, current_tilt = ptz.get_position()
    except Exception:
        current_pan, current_tilt = None, None

    needs_relay = False
    if current_pan is not None and current_tilt is not None:
        if _full_scan_pose_reached(
            current_pan,
            current_tilt,
            target_pan,
            target_tilt,
        ):
            return {
                "success": True,
                "relay_used": False,
                "reason": "already_reached",
                "position": (current_pan, current_tilt),
            }
        pan_delta, tilt_delta = _full_scan_pose_deltas(
            current_pan,
            current_tilt,
            target_pan,
            target_tilt,
        )
        needs_relay = (
            _full_scan_delta_needs_relay(pan_delta)
            or _full_scan_delta_needs_relay(tilt_delta)
        )

    first_result = None
    if not needs_relay:
        first_result = _run_full_scan_direct_leg(
            ptz,
            target_pan,
            target_tilt,
            stop_check_fn=stop_check_fn,
            timeout_s=timeout_s,
            poll_interval=poll_interval,
        )
        if first_result.get("success"):
            first_result["relay_used"] = False
            return first_result
        if first_result.get("reason") == "stopped":
            first_result["relay_used"] = False
            return first_result

    relay = _select_full_scan_relay_pose(target_pan, target_tilt, move_range)
    if relay is None:
        return {
            "success": False,
            "relay_used": False,
            "reason": (
                "relay_unavailable"
                if needs_relay
                else f"{first_result.get('reason', 'direct_failed')}:relay_unavailable"
            ),
        }

    relay_result = _run_full_scan_direct_leg(
        ptz,
        relay["pan"],
        relay["tilt"],
        stop_check_fn=stop_check_fn,
        timeout_s=timeout_s,
        poll_interval=poll_interval,
    )
    if not relay_result.get("success"):
        return {
            "success": False,
            "relay_used": True,
            "relay": relay,
            "reason": f"relay_{relay_result.get('reason', 'failed')}",
        }

    final_result = _run_full_scan_direct_leg(
        ptz,
        target_pan,
        target_tilt,
        stop_check_fn=stop_check_fn,
        timeout_s=timeout_s,
        poll_interval=poll_interval,
    )
    final_result["relay_used"] = True
    final_result["relay"] = relay
    if not final_result.get("success"):
        final_result["reason"] = f"final_{final_result.get('reason', 'failed')}"
    return final_result


def _full_scan_move_point(
    ptz,
    target_pan,
    target_tilt,
    move_range,
    *,
    stage_label,
    execution_index,
    execution_total,
    point_id,
    pixel=None,
    stop_check_fn=None,
):
    """全面扫描正式点统一移动：direct 优先，失败后可靠回退 split。"""
    started = time.monotonic()
    try:
        from_pan, from_tilt = ptz.get_position()
    except Exception:
        from_pan, from_tilt = None, None
    pixel_text = (
        f" pixel=({pixel.get('x')},{pixel.get('y')})"
        if isinstance(pixel, dict) else ""
    )
    prefix = f"[{stage_label} {execution_index}/{execution_total}]"
    logger.info(
        f"📍 {prefix} 准备移动 point_id={point_id}{pixel_text} "
        f"from=({from_pan},{from_tilt}) to=({target_pan},{target_tilt}) mode=direct"
    )
    try:
        direct = _full_scan_direct_move_with_relay(
            ptz,
            target_pan,
            target_tilt,
            move_range,
            stop_check_fn=stop_check_fn,
        )
    except Exception as exc:
        direct = {
            "success": False,
            "reason": "conversion_exception",
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        }
    if direct.get("success"):
        duration_ms = round((time.monotonic() - started) * 1000, 1)
        try:
            final_pan, final_tilt = ptz.get_position()
        except Exception:
            final_pan, final_tilt = (direct.get("position") or (None, None))
        if direct.get("reason") == "already_reached":
            logger.info(
                f"⏭️ {prefix} 无需移动 duration_ms={duration_ms} "
                "reason=already_reached movement_mode=already_reached"
            )
            direct["movement_mode"] = "already_reached"
        else:
            logger.info(
                f"✅ {prefix} 移动完成 duration_ms={duration_ms} "
                f"final=({final_pan},{final_tilt}) reason={direct.get('reason', 'reached')} "
                "movement_mode=direct"
            )
            direct["movement_mode"] = "direct"
        direct["duration_ms"] = duration_ms
        direct["final_position"] = (final_pan, final_tilt)
        direct["executed"] = True
        return direct

    direct_reason = direct.get("reason", "direct_failed")
    if direct_reason == "stopped" or (stop_check_fn and stop_check_fn()):
        duration_ms = round((time.monotonic() - started) * 1000, 1)
        logger.error(
            f"❌ {prefix} 移动失败 duration_ms={duration_ms} "
            f"reason={direct_reason} movement_mode=direct"
        )
        direct.update({
            "duration_ms": duration_ms,
            "movement_mode": "direct",
            "executed": False,
        })
        return direct

    logger.warning(
        f"⚠️ {prefix} direct失败 reason={direct_reason}，进入 split_fallback"
    )
    try:
        fallback_success = safe_split_move(
            ptz,
            target_pan,
            target_tilt,
            order="pan_first",
            settle=1.0,
            stop_check_fn=stop_check_fn,
        )
        fallback_reason = "reached" if fallback_success else "split_failed"
    except Exception as exc:
        fallback_success = False
        fallback_reason = f"split_exception:{type(exc).__name__}:{exc}"
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    if fallback_success:
        try:
            final_pan, final_tilt = ptz.get_position()
        except Exception:
            final_pan, final_tilt = None, None
        logger.info(
            f"✅ {prefix} 移动完成 duration_ms={duration_ms} "
            f"final=({final_pan},{final_tilt}) reason={fallback_reason} "
            "movement_mode=split_fallback"
        )
    else:
        logger.error(
            f"❌ {prefix} 移动失败 duration_ms={duration_ms} "
            f"reason=direct:{direct_reason};fallback:{fallback_reason} "
            "movement_mode=split_fallback"
        )
    return {
        "success": bool(fallback_success),
        "relay_used": bool(direct.get("relay_used")),
        "reason": fallback_reason,
        "direct_failure_reason": direct_reason,
        "fallback_attempted": True,
        "fallback_success": bool(fallback_success),
        "movement_mode": "split_fallback",
        "duration_ms": duration_ms,
        "executed": bool(fallback_success),
    }


def _request_camera_capture(r, pan, tilt, round_id, scan_type="multi_point"):
    """
    向前端摄像头服务（mjpeg-streamer）发 HTTP GET 截图并保存到本地。
    超时/失败不阻塞扫描流程，仅记录警告。

    Args:
        r       : Redis 连接（保留参数，兼容调用处，本函数不再使用 Redis 通信）
        pan, tilt: 当前角度
        round_id : 扫描轮次 ID
        scan_type: "multi_point" | "full_area"

    Returns:
        dict|None: {"code": 0, "image_path": ...}（成功）或 None（失败）
    """
    ts = int(time.time())
    save_dir = os.path.join(PANORAMA_RAW_DIR, round_id)
    filename = f"p{pan:.1f}_t{tilt:.1f}_ts{ts}.jpg"
    filepath = os.path.join(save_dir, filename)

    try:
        os.makedirs(save_dir, exist_ok=True)
        with _urllib_request.urlopen(CAMERA_SNAPSHOT_URL,
                                     timeout=CAMERA_CAPTURE_TIMEOUT) as resp:
            img_data = resp.read()
        with open(filepath, 'wb') as f:
            f.write(img_data)
        logger.info(f"📷 截图成功: pan={pan:.1f}, tilt={tilt:.1f} → {filepath}")
        return {"code": 0, "image_path": filepath}
    except Exception as e:
        logger.warning(f"📷 截图失败（不影响扫描）: pan={pan:.1f}, tilt={tilt:.1f}, err={e}")
        return None


def _read_current_project(r):
    raw = r.get(CURRENT_PROJECT_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _clear_current_project(r):
    try:
        r.delete(CURRENT_PROJECT_KEY)
    except Exception as e:
        logger.warning(f"⚠️ 清理当前项目标记失败: {e}")


def _finalize_current_project(r, expected_scan_type=None, status="STOPPED"):
    current_project = _read_current_project(r)
    if not current_project:
        return None

    if expected_scan_type and current_project.get("scan_type") != expected_scan_type:
        return None

    project_id = current_project.get("project_id")
    if not project_id:
        _clear_current_project(r)
        return None

    try:
        project = finish_project(project_id, status=status)
        logger.info(f"🗂️ 项目已结束: {project_id} -> {status}")
    except Exception as e:
        logger.error(f"❌ 结束项目失败: {project_id}, error={e}")
        project = None
    finally:
        _clear_current_project(r)

    return project


def _point_key_sort_value(key):
    try:
        point_id = key.split(':', 1)[1]
        return int(point_id.split('_')[-1])
    except Exception:
        return 0


def _collect_multi_point_results(r):
    results = {}
    point_keys = sorted(r.keys('multi_scan:point_*'), key=_point_key_sort_value)
    for key in point_keys:
        point_data_json = r.get(key)
        if not point_data_json:
            continue
        point_id = key.split(':', 1)[1]
        results[point_id] = json.loads(point_data_json)
    return results


def _save_project_snapshot(
    r,
    scan_type,
    round_id,
    round_index,
    grid_data,
    render_meta,
    status="SUCCESS",
    panorama_path=None,
    thumbnail_path=None,
    raw_dir=None,
):
    current_project = _read_current_project(r)
    if not current_project:
        logger.warning(f"⚠️ 未找到当前项目，跳过 snapshot 落库: round={round_id}")
        return None

    if current_project.get("scan_type") != scan_type:
        logger.warning(
            f"⚠️ 当前项目模式不匹配，跳过 snapshot 落库: "
            f"project={current_project.get('scan_type')} round={scan_type}"
        )
        return None

    if not grid_data:
        logger.warning(f"⚠️ round={round_id} 无有效结果，跳过 snapshot 落库")
        return None

    archive_state = "RAW_ONLY" if raw_dir else "NO_MEDIA"
    render_meta = dict(render_meta or {})
    render_meta.setdefault("project_id", current_project.get("project_id"))
    render_meta.setdefault("project_name", current_project.get("project_name"))

    snapshot = add_snapshot(
        project_id=current_project["project_id"],
        round_id=round_id,
        round_index=round_index,
        captured_at=utc_now_iso(),
        scan_type=scan_type,
        status=status,
        archive_state=archive_state,
        panorama_path=panorama_path,
        thumbnail_path=thumbnail_path,
        raw_dir=raw_dir,
        grid_data=grid_data,
        render_meta=render_meta,
    )
    logger.info(
        f"💾 历史快照已保存: project={current_project['project_id']}, "
        f"round={round_id}, status={status}"
    )
    return snapshot


def _save_project_snapshot_nonfatal(**kwargs):
    """历史快照是旁路产物，失败不得阻断扫描生命周期收口。"""
    try:
        return _save_project_snapshot(**kwargs)
    except Exception as exc:
        logger.exception(f"❌ 项目快照保存失败（扫描继续）: {exc}")
        return None


def generate_scan_path(pan_range, tilt_range, pan_step_size, tilt_step_size):
    """
    生成之字形扫描路径点列表
    使用新的步长参数 pan_step_size 和 tilt_step_size
    """
    pan_min, pan_max = pan_range
    tilt_min, tilt_max = tilt_range


    pan_angles = []
    current_pan = pan_min
    while current_pan <= pan_max:
        pan_angles.append(current_pan)
        current_pan += pan_step_size
    if pan_angles[-1] != pan_max:
        pan_angles.append(pan_max)

    tilt_angles = []
    current_tilt = tilt_min
    while current_tilt <= tilt_max:
        tilt_angles.append(current_tilt)
        current_tilt += tilt_step_size
    if tilt_angles[-1] != tilt_max:
        tilt_angles.append(tilt_max)

    path_points = []
    for i, tilt in enumerate(tilt_angles):
        if i % 2 == 0:
            for pan in pan_angles:
                path_points.append((pan, tilt))
        else:
            for pan in reversed(pan_angles):
                path_points.append((pan, tilt))
    return path_points


def _auto_step_from_range(axis_min, axis_max, phase="precheck"):
    """全面扫描固定步径：粗筛/外扩环 8 度，工作区细扫 4 度。"""
    return 4.0 if phase == "work" else 8.0

def _point_in_scan_range(pan, tilt, pan_range, tilt_range):
    eps = 1e-9
    pan_min, pan_max = sorted([float(pan_range[0]), float(pan_range[1])])
    tilt_min, tilt_max = sorted([float(tilt_range[0]), float(tilt_range[1])])
    return (
        pan_min - eps <= float(pan) <= pan_max + eps and
        tilt_min - eps <= float(tilt) <= tilt_max + eps
    )


def _point_in_any_work_range(pan, tilt, work_ranges):
    for item in work_ranges or []:
        try:
            if _point_in_scan_range(pan, tilt, item["pan_range"], item["tilt_range"]):
                return True
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return False


def _build_scan_edge_path(pan_range, tilt_range, pan_step_size, tilt_step_size):
    pan_min, pan_max = sorted([float(pan_range[0]), float(pan_range[1])])
    tilt_min, tilt_max = sorted([float(tilt_range[0]), float(tilt_range[1])])

    pan_angles = []
    current_pan = pan_min
    while current_pan <= pan_max:
        pan_angles.append(current_pan)
        current_pan += pan_step_size
    if pan_angles[-1] != pan_max:
        pan_angles.append(pan_max)

    tilt_angles = []
    current_tilt = tilt_min
    while current_tilt <= tilt_max:
        tilt_angles.append(current_tilt)
        current_tilt += tilt_step_size
    if tilt_angles[-1] != tilt_max:
        tilt_angles.append(tilt_max)

    path_points = []
    for pan in pan_angles:
        path_points.append((pan, tilt_min))
    for tilt in tilt_angles[1:-1]:
        path_points.append((pan_max, tilt))
    for pan in reversed(pan_angles):
        path_points.append((pan, tilt_max))
    for tilt in reversed(tilt_angles[1:-1]):
        path_points.append((pan_min, tilt))
    return path_points


def _expand_work_range_for_full_scan_guard(item, precheck_range):
    pan_min, pan_max = sorted([float(item["pan_range"][0]), float(item["pan_range"][1])])
    tilt_min, tilt_max = sorted([float(item["tilt_range"][0]), float(item["tilt_range"][1])])
    pre_pan_min, pre_pan_max = sorted([
        float(precheck_range["pan_range"][0]),
        float(precheck_range["pan_range"][1]),
    ])
    pre_tilt_min, pre_tilt_max = sorted([
        float(precheck_range["tilt_range"][0]),
        float(precheck_range["tilt_range"][1]),
    ])
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
    return {
        "pan_range": guard_pan,
        "tilt_range": guard_tilt,
    }


def _calc_dev_area_ratio(work_range, precheck_range):
    """
    计算单个工作区的实际偏差区可用面积比 (dev_area_ratio)。
    返回: 0.0 ~ 1.0 的浮点数。
    """
    try:
        w_pan_min, w_pan_max = sorted([float(work_range["pan_range"][0]), float(work_range["pan_range"][1])])
        w_tilt_min, w_tilt_max = sorted([float(work_range["tilt_range"][0]), float(work_range["tilt_range"][1])])
        p_pan_min, p_pan_max = sorted([float(precheck_range["pan_range"][0]), float(precheck_range["pan_range"][1])])
        p_tilt_min, p_tilt_max = sorted([float(precheck_range["tilt_range"][0]), float(precheck_range["tilt_range"][1])])
    except (KeyError, TypeError, ValueError, IndexError):
        return 1.0  # 数据异常时默认安全比例

    w_pan_span = w_pan_max - w_pan_min
    w_tilt_span = w_tilt_max - w_tilt_min
    p_pan_span = p_pan_max - p_pan_min
    p_tilt_span = p_tilt_max - p_tilt_min
    pre_area = p_pan_span * p_tilt_span
    work_area = w_pan_span * w_tilt_span
    if pre_area <= 0:
        return 0.0

    actual_dev_area = max(0.0, pre_area - min(work_area, pre_area))
    return max(0.0, min(1.0, actual_dev_area / pre_area))


def _calc_rssi_std_dev(rssi_list):
    """计算 RSSI 列表的标准差"""
    if not rssi_list or len(rssi_list) < 2:
        return 0.0
    mean = sum(rssi_list) / len(rssi_list)
    variance = sum((x - mean) ** 2 for x in rssi_list) / (len(rssi_list) - 1)
    return math.sqrt(variance)


def _point_edge_margin_ratio_in_work_ranges(point, work_ranges):
    if not point:
        return 0.0
    try:
        pan = float(point["position"]["pan"])
        tilt = float(point["position"]["tilt"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    best_ratio = 0.0
    for item in work_ranges or []:
        try:
            pan_min, pan_max = sorted([float(item["pan_range"][0]), float(item["pan_range"][1])])
            tilt_min, tilt_max = sorted([float(item["tilt_range"][0]), float(item["tilt_range"][1])])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if not (pan_min <= pan <= pan_max and tilt_min <= tilt <= tilt_max):
            continue
        short_side = min(pan_max - pan_min, tilt_max - tilt_min)
        if short_side <= 0:
            continue
        margin = min(pan - pan_min, pan_max - pan, tilt - tilt_min, tilt_max - tilt)
        best_ratio = max(best_ratio, margin / short_side)
    return best_ratio


def _filter_and_dedupe_full_scan_path(path, seen_points, predicate=None):
    filtered = []
    for pan, tilt in path:
        key = (round(float(pan), 2), round(float(tilt), 2))
        if predicate and not predicate(key[0], key[1]):
            continue
        if key in seen_points:
            continue
        seen_points.add(key)
        filtered.append(key)
    return filtered


def _auto_location_step(axis_min, axis_max, phase="coarse"):
    """定位扫描固定步径：第一轮 8 度，第二轮 4 度，第三轮 2 度。"""
    if phase in ("fine", "fine_4"):
        return 2.0
    if phase in ("mid", "mid_8"):
        return 4.0
    return 8.0


def _dedupe_points(points, ndigits=2):
    deduped = []
    seen = set()
    for pan, tilt in points:
        key = (round(float(pan), ndigits), round(float(tilt), ndigits))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _build_location_probe_points(pan_ranges, tilt_ranges, limits=None):
    """每个区域取中心 + 四角，用于信道探测。"""
    points = []
    for pan_rng, tilt_rng in zip(pan_ranges, tilt_ranges):
        pan_min, pan_max = float(pan_rng[0]), float(pan_rng[1])
        tilt_min, tilt_max = float(tilt_rng[0]), float(tilt_rng[1])
        if limits:
            pan_min = max(float(limits["pan_min"]), pan_min)
            pan_max = min(float(limits["pan_max"]), pan_max)
            tilt_min = max(float(limits["tilt_min"]), tilt_min)
            tilt_max = min(float(limits["tilt_max"]), tilt_max)
        center_pan = (pan_min + pan_max) / 2.0
        center_tilt = (tilt_min + tilt_max) / 2.0
        points.extend([
            (center_pan, center_tilt),
            (pan_min, tilt_min),
            (pan_min, tilt_max),
            (pan_max, tilt_min),
            (pan_max, tilt_max),
        ])
    return _dedupe_points(points)


def _group_location_configs(mac_channel_map, macs):
    """按 channel/bandwidth 分组，供 scan_at_point 一次采多个 MAC。"""
    grouped = {}
    for mac in macs:
        info = mac_channel_map.get(mac)
        if not isinstance(info, dict):
            continue
        key = (int(info["channel"]), str(info["bandwidth"]))
        grouped.setdefault(key, []).append(mac)
    return [
        {"channel": ch, "bandwidth": bw, "target_macs": target_macs}
        for (ch, bw), target_macs in grouped.items()
    ]


def _location_boxes_overlap(a, b):
    return not (
        a["pan_range"][1] < b["pan_range"][0] or
        b["pan_range"][1] < a["pan_range"][0] or
        a["tilt_range"][1] < b["tilt_range"][0] or
        b["tilt_range"][1] < a["tilt_range"][0]
    )


def _merge_location_boxes(boxes):
    merged = []
    for box in boxes:
        current = {
            "macs": set(box["macs"]),
            "pan_range": list(box["pan_range"]),
            "tilt_range": list(box["tilt_range"]),
        }
        changed = True
        while changed:
            changed = False
            kept = []
            for existing in merged:
                if _location_boxes_overlap(current, existing):
                    current["macs"].update(existing["macs"])
                    current["pan_range"] = [
                        min(current["pan_range"][0], existing["pan_range"][0]),
                        max(current["pan_range"][1], existing["pan_range"][1]),
                    ]
                    current["tilt_range"] = [
                        min(current["tilt_range"][0], existing["tilt_range"][0]),
                        max(current["tilt_range"][1], existing["tilt_range"][1]),
                    ]
                    changed = True
                else:
                    kept.append(existing)
            merged = kept
        current["macs"] = sorted(current["macs"])
        merged.append(current)
    return merged


# ===========================================================================
# 全面扫描优化：新三段（粗扫/细扫/偏差区）辅助函数
# 配置项均从 config.json 内的 "全面扫描" 节读取，支持环境变量覆盖
# ===========================================================================
_FS_CONFIG               = _get_full_scan_config_fn()


def _full_scan_bool(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


FULL_SCAN_TEST_SUMMARY_ENABLED = _full_scan_bool(_FS_CONFIG.get("保存测试运行摘要"), False)
FULL_SCAN_SAMPLING_CAPTURE_ENABLED = _full_scan_bool(
    _FS_CONFIG.get("保存扫描采样截图"),
    False,
)
FULL_SCAN_TEST_SUMMARY_DIR = str(_FS_CONFIG.get("测试运行摘要目录", "/tmp/logs") or "/tmp/logs")
FULL_SCAN_TIMING_TRACE_ENABLED = _full_scan_bool(_FS_CONFIG.get("保存测试计时明细"), False)
FULL_SCAN_ALLOW_MINIMAL_POINT_TEST = _full_scan_bool(_FS_CONFIG.get("允许最小点位内部测试"), False)
FULL_SCAN_CONFIG_PRIORITY_COLLECT = _full_scan_bool(_FS_CONFIG.get("配置优先持续采集"), False)
FULL_SCAN_WHITELIST_REFINEMENT_ENABLED = _full_scan_bool(
    _FS_CONFIG.get("启用白名单位置复核"),
    False,
)
# 热路径只切信道；真实冷启动可能包含监听模式初始化，等待期间仍轮询 stop。
FULL_SCAN_REFINEMENT_SESSION_START_TIMEOUT = 10.0
FULL_SCAN_COARSE_STEP        = float(_FS_CONFIG.get("粗扫步径", 10.0))   # 粗扫格心步径
FULL_SCAN_COARSE_CORE_MIN    = int(_FS_CONFIG.get("粗扫核心点数下限", 12))
FULL_SCAN_COARSE_OUTER_PROBE_DEG = float(_FS_CONFIG.get("粗扫外扩角度", 9.0))
FULL_SCAN_FINE_STEP_MIN      = _FS_CONFIG.get("细扫步径下限",      2.0)   # 细扫步径下限
FULL_SCAN_FINE_STEP_MAX      = _FS_CONFIG.get("细扫步径上限",      9.0)   # 细扫步径上限
FULL_SCAN_FINE_CORE_MIN      = int(_FS_CONFIG.get("细扫核心点数下限", 20))
FULL_SCAN_FINE_CORE_MAX      = int(_FS_CONFIG.get("细扫核心点数上限", 26))
FULL_SCAN_FINE_COUNT_MIN     = _FS_CONFIG.get("细扫最少次数",      2)     # 细扫最少次数
FULL_SCAN_FINE_COUNT_MAX     = _FS_CONFIG.get("细扫最多次数",      2)     # 细扫最多次数
FULL_SCAN_COARSE_SUBDIVISIONS = _FS_CONFIG.get("粗扫交错等分数",    3)     # 粗扫交错采样等分数（9 宫格 = 3×3）
FULL_SCAN_SMALL_MOVE_INTERLEAVE_DEG = float(_FS_CONFIG.get("小范围交错排序阈值", 1.0))
FULL_SCAN_DEVIATION_DIVISIONS = max(2, int(_FS_CONFIG.get("偏差区外扩等分数", 4)))
FULL_SCAN_DEVIATION_POINTS_PER_LAYER = int(_FS_CONFIG.get("偏差区每层点数", 5))
FULL_SCAN_DEVIATION_NARROW_SIDE_DEG = float(_FS_CONFIG.get("偏差区窄边阈值", 2.0))
FULL_SCAN_MINIMAL_TEST_COARSE_OUTER_PROBE_POINTS = 2
FULL_SCAN_MINIMAL_TEST_COARSE_GRID_POINTS = 3
FULL_SCAN_MINIMAL_TEST_FINE_POINTS = 3
FULL_SCAN_MINIMAL_TEST_DEVIATION_POINTS_PER_LAYER = 2
FULL_SCAN_DEV_RATIO_NORMAL_MIN = float(_FS_CONFIG.get("偏差区正常模式面积比阈值", 0.7))
FULL_SCAN_DEV_RATIO_LARGE_MAX = float(_FS_CONFIG.get("偏差区大范围模式面积比阈值", 0.3))
FULL_SCAN_WARM_START_THRESHOLD = int(_FS_CONFIG.get("温启动白名单MAC阈值", 10))
FULL_SCAN_WARM_REMAINING_INTERVAL = max(1, int(_FS_CONFIG.get("温启动剩余信道扫描间隔点数", 2)))
FULL_SCAN_LARGE_EDGE_RATIO = float(_FS_CONFIG.get("大范围峰值边缘保护比例", 0.15))
FULL_SCAN_LARGE_RSSI_STD_MIN = float(_FS_CONFIG.get("大范围RSSI标准差阈值", 3.0))
FULL_SCAN_MARKER_MAC = "ff:ff:ff:ff:ff:ff"


def _calc_fine_scan_step(work_ranges):
    """
    兼容旧日志/偏移计算的任务级参考步径。
    实际细扫路径由 _build_fine_scan_path 按每个 work_range 独立计算步径。
    """
    steps = []
    for item in work_ranges or []:
        try:
            span_h = abs(float(item["pan_range"][1]) - float(item["pan_range"][0]))
            span_v = abs(float(item["tilt_range"][1]) - float(item["tilt_range"][0]))
            if span_h <= 0 or span_v <= 0:
                continue
            raw = min(span_h / 3.0, span_v / 3.0)
            steps.append(max(FULL_SCAN_FINE_STEP_MIN, min(FULL_SCAN_FINE_STEP_MAX, round(raw))))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    if not steps:
        return FULL_SCAN_FINE_STEP_MAX
    return max(FULL_SCAN_FINE_STEP_MIN, min(FULL_SCAN_FINE_STEP_MAX, round(sum(steps) / len(steps))))


def _calc_fine_scan_count(work_ranges, whitelist_count_prev=0):
    """
    动态计算细扫次数：范围 [1, 2]，根据目标区面积和上轮白名单数量调整。
    """
    area = 0.0
    for item in work_ranges or []:
        try:
            span_h = abs(float(item["pan_range"][1]) - float(item["pan_range"][0]))
            span_v = abs(float(item["tilt_range"][1]) - float(item["tilt_range"][0]))
            area += span_h * span_v
        except Exception:
            continue
    base = 2
    if area > 900:           # 目标区大（约 30°×30°以上），减少次数
        base -= 1
    if area < 100:           # 目标区小（约 10°×10°以下），增加次数
        base += 1
    if whitelist_count_prev < 2:  # 上轮白名单少，增加次数
        base += 1
    return max(FULL_SCAN_FINE_COUNT_MIN, min(FULL_SCAN_FINE_COUNT_MAX, base))


def _fine_scan_offsets(step):
    """
    生成细扫多次起始点偏移表（5 组），按 _calc_fine_scan_count 截取使用。
    """
    half    = step / 2.0
    quarter = step / 4.0
    return [
        (0.0,     0.0),
        (half,    0.0),
        (0.0,     half),
        (half,    half),
        (quarter, quarter),
    ]


def _coarse_scan_offset(full_scan_round,
                         step=FULL_SCAN_COARSE_STEP,
                         subdivisions=FULL_SCAN_COARSE_SUBDIVISIONS):
    """
    粗扫交错采样偏移：以 9 宫格模式按轮次（1-based）循环。
    第 1 轮→(0°,0°)，第 2 轮→(3°,0°)，...，第 10 轮重置为 (0°,0°)。
    """
    sub_step = step / subdivisions
    total    = subdivisions * subdivisions
    idx      = (full_scan_round - 1) % total
    pan_off  = (idx % subdivisions) * sub_step
    tilt_off = (idx // subdivisions) * sub_step
    return pan_off, tilt_off


def _normalize_full_scan_range(item):
    try:
        pan_min, pan_max = sorted([float(item["pan_range"][0]), float(item["pan_range"][1])])
        tilt_min, tilt_max = sorted([float(item["tilt_range"][0]), float(item["tilt_range"][1])])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if pan_min > pan_max or tilt_min > tilt_max:
        return None
    return pan_min, pan_max, tilt_min, tilt_max


def _choose_full_scan_grid_counts(span_h, span_v, min_points, max_points):
    """按宽高比选择核心网格行列数，使点数落在配置区间内。"""
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
    """按宽高比选择不少于 min_points 的核心格心行列数，不设置上限。"""
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


def _grid_center_points_by_step(pan_min, pan_max, tilt_min, tilt_max,
                                step=FULL_SCAN_COARSE_STEP,
                                min_points=FULL_SCAN_COARSE_CORE_MIN):
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
            row_points.append((round(pan, 2), round(tilt, 2)))
        if row_idx % 2:
            row_points.reverse()
        points.extend(row_points)
    return points


def _grid_center_points(pan_min, pan_max, tilt_min, tilt_max, min_points, max_points):
    span_h = pan_max - pan_min
    span_v = tilt_max - tilt_min
    if span_h < 0 or span_v < 0:
        return []
    rows, cols = _choose_full_scan_grid_counts(span_h, span_v, min_points, max_points)
    points = []
    for row_idx in range(rows):
        tilt = tilt_min + (row_idx + 0.5) * span_v / rows if rows > 0 else (tilt_min + tilt_max) / 2.0
        row_points = []
        for col_idx in range(cols):
            pan = pan_min + (col_idx + 0.5) * span_h / cols if cols > 0 else (pan_min + pan_max) / 2.0
            row_points.append((round(pan, 2), round(tilt, 2)))
        if row_idx % 2:
            row_points.reverse()
        points.extend(row_points)
    return points


def _boundary_8_points(pan_min, pan_max, tilt_min, tilt_max):
    mid_pan = (pan_min + pan_max) / 2.0
    mid_tilt = (tilt_min + tilt_max) / 2.0
    return [
        (round(pan_min, 2), round(tilt_min, 2)),
        (round(mid_pan, 2), round(tilt_min, 2)),
        (round(pan_max, 2), round(tilt_min, 2)),
        (round(pan_max, 2), round(mid_tilt, 2)),
        (round(pan_max, 2), round(tilt_max, 2)),
        (round(mid_pan, 2), round(tilt_max, 2)),
        (round(pan_min, 2), round(tilt_max, 2)),
        (round(pan_min, 2), round(mid_tilt, 2)),
    ]


def _clamp_full_scan_range(pan_min, pan_max, tilt_min, tilt_max):
    return (
        max(float(PAN_MIN), min(float(PAN_MAX), float(pan_min))),
        max(float(PAN_MIN), min(float(PAN_MAX), float(pan_max))),
        max(float(TILT_MIN), min(float(TILT_MAX), float(tilt_min))),
        max(float(TILT_MIN), min(float(TILT_MAX), float(tilt_max))),
    )


def _outer_probe_8_points(pan_min, pan_max, tilt_min, tilt_max,
                          expand_deg=FULL_SCAN_COARSE_OUTER_PROBE_DEG):
    expand_deg = max(0.0, float(expand_deg or 0.0))
    out_pan_min, out_pan_max, out_tilt_min, out_tilt_max = _clamp_full_scan_range(
        pan_min - expand_deg,
        pan_max + expand_deg,
        tilt_min - expand_deg,
        tilt_max + expand_deg,
    )
    return _boundary_8_points(out_pan_min, out_pan_max, out_tilt_min, out_tilt_max)


def _interleave_small_full_scan_moves(points, threshold=FULL_SCAN_SMALL_MOVE_INTERLEAVE_DEG):
    if len(points) < 3:
        return list(points)
    threshold = float(threshold)
    has_small_move = any(
        math.hypot(points[idx][0] - points[idx - 1][0], points[idx][1] - points[idx - 1][1]) < threshold
        for idx in range(1, len(points))
    )
    if not has_small_move:
        return list(points)
    return list(points[::2]) + list(points[1::2])


def _dedupe_exact_points(points, seen=None):
    seen = seen if seen is not None else set()
    result = []
    for pan, tilt in points:
        key = (round(float(pan), 2), round(float(tilt), 2))
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _build_coarse_scan_path(precheck_range, pan_offset=0.0, tilt_offset=0.0,
                              step=FULL_SCAN_COARSE_STEP):
    """
    构建粗扫路径：precheck_range 内 10° 格心稀疏覆盖 + 外扩 8 个探测点。
    pan_offset/tilt_offset 保留为兼容入参，新稳定网格不再按轮次偏移。
    """
    normalized = _normalize_full_scan_range(precheck_range)
    if not normalized:
        return []
    pan_min, pan_max, tilt_min, tilt_max = normalized
    core_points = _grid_center_points_by_step(
        pan_min, pan_max, tilt_min, tilt_max,
        step=step,
        min_points=FULL_SCAN_COARSE_CORE_MIN,
    )
    outer_probe_points = _outer_probe_8_points(
        pan_min, pan_max, tilt_min, tilt_max,
        FULL_SCAN_COARSE_OUTER_PROBE_DEG,
    )
    return _interleave_small_full_scan_moves(
        _dedupe_exact_points(core_points + outer_probe_points)
    )


def _build_fine_scan_path(work_ranges, pan_offset=0.0, tilt_offset=0.0):
    """
    构建细扫路径：每个 work_range 单次稳定网格，只保留 20-26 个核心中心点。
    pan_offset/tilt_offset 保留为兼容入参，新细扫不再执行多次 offset。
    """
    seen = set()
    all_points = []
    for item in work_ranges or []:
        normalized = _normalize_full_scan_range(item)
        if not normalized:
            continue
        pan_min, pan_max, tilt_min, tilt_max = normalized
        area_points = _grid_center_points(
            pan_min, pan_max, tilt_min, tilt_max,
            FULL_SCAN_FINE_CORE_MIN, FULL_SCAN_FINE_CORE_MAX,
        )
        all_points.extend(_dedupe_exact_points(
            _interleave_small_full_scan_moves(area_points),
            seen,
        ))
    return all_points


def _allocate_counts_by_weight(total, weights):
    total = max(0, int(total))
    positive = {key: max(0.0, float(value)) for key, value in weights.items() if float(value) > 0}
    if total <= 0 or not positive:
        return {key: 0 for key in weights}
    weight_sum = sum(positive.values())
    raw = {
        key: total * value / weight_sum
        for key, value in positive.items()
    }
    counts = {key: int(math.floor(raw.get(key, 0.0))) for key in weights}
    assigned = sum(counts.values())
    remainders = sorted(
        ((raw[key] - counts[key], key) for key in positive),
        reverse=True,
    )
    for _, key in remainders:
        if assigned >= total:
            break
        counts[key] += 1
        assigned += 1
    return counts


def _side_sample_fractions(count, layer_index):
    count = max(0, int(count))
    if count <= 0:
        return []
    if count == 1:
        return [((layer_index - 1) % 3 + 1) / 4.0]
    return [(idx + 1) / float(count + 1) for idx in range(count)]


def _edge_point_specs(side, count, pan_min, pan_max, tilt_min, tilt_max,
                      layer_index, work_range_index, narrow_side=False):
    specs = []
    for fraction in _side_sample_fractions(count, layer_index):
        if side == "left":
            pan = pan_min
            tilt = tilt_min + (tilt_max - tilt_min) * fraction
        elif side == "right":
            pan = pan_max
            tilt = tilt_min + (tilt_max - tilt_min) * fraction
        elif side == "bottom":
            pan = pan_min + (pan_max - pan_min) * fraction
            tilt = tilt_min
        elif side == "top":
            pan = pan_min + (pan_max - pan_min) * fraction
            tilt = tilt_max
        else:
            continue
        specs.append({
            "pan": round(float(pan), 2),
            "tilt": round(float(tilt), 2),
            "layer": int(layer_index),
            "side": side,
            "work_range_index": int(work_range_index),
            "narrow_side": bool(narrow_side),
        })
    return specs


def _deviation_layer_point_specs(*, layer_index, work_range_index,
                                 pan_min, pan_max, tilt_min, tilt_max,
                                 side_distances, narrow_sides):
    points_per_layer = max(1, int(FULL_SCAN_DEVIATION_POINTS_PER_LAYER))
    counts = {side: 0 for side in ("left", "right", "bottom", "top")}

    active_narrow = None
    if narrow_sides:
        active_narrow = narrow_sides[(layer_index - 1) % len(narrow_sides)]
        counts[active_narrow] = 1

    remaining = max(0, points_per_layer - sum(counts.values()))
    regular_weights = {
        side: distance
        for side, distance in side_distances.items()
        if side not in narrow_sides and distance > 0
    }
    if regular_weights:
        regular_counts = _allocate_counts_by_weight(remaining, regular_weights)
    else:
        fallback_weights = {side: 1.0 for side in counts}
        regular_counts = _allocate_counts_by_weight(remaining, fallback_weights)

    for side, value in regular_counts.items():
        counts[side] = counts.get(side, 0) + value

    specs = []
    for side in ("bottom", "right", "top", "left"):
        specs.extend(_edge_point_specs(
            side,
            counts.get(side, 0),
            pan_min,
            pan_max,
            tilt_min,
            tilt_max,
            layer_index,
            work_range_index,
            narrow_side=(side == active_narrow),
        ))
    return specs


def _build_deviation_a_point_specs(work_ranges, precheck_range):
    pre = _normalize_full_scan_range(precheck_range)
    if not pre:
        return []
    pre_pan_min, pre_pan_max, pre_tilt_min, pre_tilt_max = pre
    seen = set()
    all_specs = []
    for work_range_index, item in enumerate(work_ranges or []):
        normalized = _normalize_full_scan_range(item)
        if not normalized:
            continue
        work_pan_min, work_pan_max, work_tilt_min, work_tilt_max = normalized
        side_distances = {
            "left": max(0.0, work_pan_min - pre_pan_min),
            "right": max(0.0, pre_pan_max - work_pan_max),
            "bottom": max(0.0, work_tilt_min - pre_tilt_min),
            "top": max(0.0, pre_tilt_max - work_tilt_max),
        }
        narrow_sides = [
            side for side, distance in side_distances.items()
            if 0.0 < distance <= FULL_SCAN_DEVIATION_NARROW_SIDE_DEG
        ]
        for layer in range(1, FULL_SCAN_DEVIATION_DIVISIONS):
            fraction = layer / float(FULL_SCAN_DEVIATION_DIVISIONS)
            pan_min = pre_pan_min if "left" in narrow_sides else work_pan_min - side_distances["left"] * fraction
            pan_max = pre_pan_max if "right" in narrow_sides else work_pan_max + side_distances["right"] * fraction
            tilt_min = pre_tilt_min if "bottom" in narrow_sides else work_tilt_min - side_distances["bottom"] * fraction
            tilt_max = pre_tilt_max if "top" in narrow_sides else work_tilt_max + side_distances["top"] * fraction
            if pan_min > pan_max or tilt_min > tilt_max:
                continue
            layer_specs = _deviation_layer_point_specs(
                layer_index=layer,
                work_range_index=work_range_index,
                pan_min=pan_min,
                pan_max=pan_max,
                tilt_min=tilt_min,
                tilt_max=tilt_max,
                side_distances=side_distances,
                narrow_sides=narrow_sides,
            )
            for spec in layer_specs:
                key = (spec["pan"], spec["tilt"])
                if key in seen:
                    continue
                seen.add(key)
                all_specs.append(spec)
    return all_specs


def _build_deviation_a_points(work_ranges, precheck_range,
                                step=FULL_SCAN_COARSE_STEP):
    """
    偏差区外扩环：每个 work_range 到 precheck_range 做 4 等分，只扫中间 3 层。
    对外 phase 仍统一为 deviation_a。
    """
    specs = _build_deviation_a_point_specs(work_ranges, precheck_range)
    return [(spec["pan"], spec["tilt"]) for spec in specs]


def _full_scan_plan_point_entries(points, point_extra_builder=None):
    entries = []
    for idx, (pan, tilt) in enumerate(points or []):
        entry = {
            "idx": idx + 1,
            "pan": round(float(pan), 2),
            "tilt": round(float(tilt), 2),
        }
        if point_extra_builder:
            extra = point_extra_builder(idx, pan, tilt)
            if isinstance(extra, dict):
                entry.update(extra)
        entries.append(entry)
    return entries


def _full_scan_config_entries(configs):
    entries = []
    for config in configs or []:
        if not isinstance(config, dict):
            continue
        entry = {}
        if config.get("channel") is not None:
            entry["channel"] = config.get("channel")
        if config.get("bandwidth") is not None:
            entry["bandwidth"] = config.get("bandwidth")
        entries.append(entry)
    return entries


def _log_full_scan_stage_plan(logger_obj, *, round_index, round_id, stage_name,
                              phase, points, dwell_time, configs=None,
                              config_count=None, extra=None,
                               point_extra_builder=None,
                               log_enabled=True,
                               include_points=True,
                               include_configs=True):
    point_entries = _full_scan_plan_point_entries(points, point_extra_builder)
    per_point_config_counts = [
        item.get("config_count") for item in point_entries
        if isinstance(item.get("config_count"), int)
    ]
    if configs is not None:
        resolved_config_count = len(configs)
    elif config_count is not None:
        resolved_config_count = int(config_count)
    else:
        resolved_config_count = None

    payload = {
        "round": round_index,
        "round_id": round_id,
        "stage": stage_name,
        "phase": phase,
        "point_count": len(point_entries),
        "dwell_time_per_config_seconds": round(float(dwell_time), 3),
    }
    if include_points:
        payload["points"] = point_entries
    if resolved_config_count is not None:
        payload["config_count"] = resolved_config_count
    if include_configs and configs is not None:
        payload["configs"] = _full_scan_config_entries(configs)
    if per_point_config_counts:
        payload["estimated_capture_seconds"] = round(
            sum(per_point_config_counts) * float(dwell_time),
            2,
        )
    elif resolved_config_count is not None:
        payload["estimated_capture_seconds"] = round(
            len(point_entries) * resolved_config_count * float(dwell_time),
            2,
        )
    if isinstance(extra, dict):
        payload.update(extra)

    return payload


def _build_deviation_configs(target_mac_channel_map, task_full_scan_configs=None):
    """
    将目标MAC的信道配置去重后生成 configs 列表（用于偏差区定向扫描）。

    偏差区配置必须从 task_full_scan_configs 中取得：只保留信道号在任务配置中
    出现过的候选，并强制使用 HT20 带宽，避免 AP 宣告带宽（HT40/VHT80）逸出
    任务锁定配置。

    :param target_mac_channel_map: {mac: {"channel": int, "bandwidth": str}}
    :param task_full_scan_configs: 任务启动时锁定的 HT20 配置列表（可选）
    :return: [{"channel": int, "bandwidth": "HT20"}, ...]
    """
    allowed = allowed_channels_from_configs(task_full_scan_configs) if task_full_scan_configs else None
    seen    = set()
    configs = []
    for ch_info in target_mac_channel_map.values():
        ch = ch_info.get("channel")
        if ch is None:
            continue
        try:
            ch_int = int(ch)
        except (TypeError, ValueError):
            continue
        # 若提供了任务配置，只保留信道号在任务配置中的候选
        if allowed is not None and ch_int not in allowed:
            continue
        # 强制 HT20：偏差区配置带宽始终使用 HT20，不信任 MAC 结果中的宣告带宽
        key = (ch_int, "HT20")
        if key not in seen:
            seen.add(key)
            configs.append({"channel": ch_int, "bandwidth": "HT20"})
    return configs


def _full_scan_config_key(config):
    try:
        return (int(config.get("channel")), str(config.get("bandwidth", "HT20")))
    except (TypeError, ValueError, AttributeError):
        return None


def _dedupe_full_scan_configs(configs):
    seen = set()
    result = []
    for config in configs or []:
        key = _full_scan_config_key(config)
        if key is None or key in seen:
            continue
        seen.add(key)
        result.append({"channel": key[0], "bandwidth": key[1]})
    return result


def _load_full_scan_warm_start_configs(r, task_configs):
    """
    从上一成功白名单读取已知信道配置。
    返回 (mac_count, known_configs, remaining_configs)，异常时安全回退冷启动。
    """
    task_configs = _dedupe_full_scan_configs(task_configs)
    try:
        raw = r.get("full_scan:whitelist:latest_success")
        if not raw:
            return 0, [], list(task_configs)
        payload = json.loads(raw)
        whitelist = payload.get("mac_whitelist") or []
        known_configs = []
        task_keys = {_full_scan_config_key(item) for item in task_configs}
        for item in whitelist:
            if not isinstance(item, dict):
                continue
            ch = item.get("channel")
            if ch is None:
                continue
            candidate = {
                "channel": int(ch),
                "bandwidth": str(item.get("bandwidth", "HT20")),
            }
            if _full_scan_config_key(candidate) in task_keys:
                known_configs.append(candidate)
        known_configs = _dedupe_full_scan_configs(known_configs)
        known_keys = {_full_scan_config_key(item) for item in known_configs}
        remaining_configs = [
            item for item in task_configs
            if _full_scan_config_key(item) not in known_keys
        ]
        return len(whitelist), known_configs, remaining_configs
    except Exception:
        return 0, [], list(task_configs)


def _full_scan_fine_configs_for_point(
    warm_start_mode,
    known_configs,
    remaining_configs,
    point_idx,
    task_configs,
):
    if not warm_start_mode or not known_configs:
        return list(task_configs), "cold_full"
    if point_idx % FULL_SCAN_WARM_REMAINING_INTERVAL == 0:
        return _dedupe_full_scan_configs(list(known_configs) + list(remaining_configs)), "warm_full"
    return list(known_configs), "warm_known"


def _collect_deviation_target_macs(round_results, work_ranges, filter_config,
                                    coarse_macs_set=None):
    """
    从粗扫+细扫已有结果中提取真实目标区内出现过 MAC 的最佳信道配置。
    返回：{mac_lower: {"channel": int, "bandwidth": str}}
    """
    target_macs = {}

    for point_id, point_data in round_results.items():
        if not isinstance(point_data, dict):
            continue
        if not _is_qualified_full_scan_fixed_evidence(point_data):
            continue
        if point_data.get("phase") not in ("coarse", "fine"):
            continue
        position = point_data.get("position") or {}
        pan  = position.get("pan")
        tilt = position.get("tilt")
        if pan is None or tilt is None:
            continue
        in_work = _full_scan_point_in_work_ranges(pan, tilt, work_ranges)
        if not in_work:
            continue
        macs = point_data.get("macs") or {}
        for mac, mac_data in macs.items():
            if not isinstance(mac_data, dict):
                continue
            mac_l = str(mac).lower()
            if mac_l == FULL_SCAN_MARKER_MAC or mac_data.get("synthetic") or mac_data.get("role") == "scan_point_marker":
                continue
            rssi_avg = mac_data.get("rssi_avg")
            if rssi_avg is None:
                continue
            rssi_val = float(rssi_avg)
            ch = mac_data.get("channel")
            bw = mac_data.get("bandwidth", "HT20")
            if ch is not None:
                ex = target_macs.get(mac_l)
                if ex is None or rssi_val > ex["target_best_rssi"]:
                    target_macs[mac_l] = {
                        "channel": int(ch),
                        "bandwidth": str(bw),
                        "target_best_rssi": rssi_val,
                    }
    return target_macs


def _append_full_scan_point_marker(macs, point_started_at=None, point_finished_at=None,
                                   configs=None):
    """为真实完成扫描的点位追加普通 MAC 形状的 marker，不影响白名单。"""
    if not isinstance(macs, dict):
        macs = {}
    marker_channel = 0
    marker_bandwidth = "HT20"
    for config in configs or []:
        if not isinstance(config, dict):
            continue
        if config.get("channel") is None:
            continue
        try:
            marker_channel = int(config.get("channel"))
        except (TypeError, ValueError):
            marker_channel = 0
        marker_bandwidth = str(config.get("bandwidth", "HT20"))
        break
    macs[FULL_SCAN_MARKER_MAC] = {
        "bandwidth": marker_bandwidth,
        "channel": marker_channel,
        "first_seen_at": point_started_at,
        "last_seen_at": point_finished_at or point_started_at,
        "omni_rssi_avg": -100,
        "omni_rssi_samples": 1,
        "rssi_avg": -100,
        "rssi_samples": 1,
        "ssid": "Full-scan point marker",
        "subtype": None,
        "type": "ap",
    }
    return macs


def _full_scan_real_mac_count(macs):
    if not isinstance(macs, dict):
        return 0
    return sum(1 for mac in macs if str(mac).lower() != FULL_SCAN_MARKER_MAC)


def _apply_identity_to_round_results(round_results, notify_data):
    """用 capture_worker 返回的全局身份信息（AP/客户端/星链）回填本轮所有已知 MAC。"""
    global_ap       = set(notify_data.get('global_ap_macs', []))
    global_client   = set(notify_data.get('global_client_macs', []))
    global_ap_ssids = notify_data.get('global_ap_ssids', {}) or {}
    starlink_bssids = set(notify_data.get('starlink_bssids', []))
    relationships = notify_data.get('full_scan_relationships', {}) or {}
    for _pid, _pdata in round_results.items():
        for _mac, _mdata in _pdata.get('macs', {}).items():
            _mac_lower = _mac.lower()
            if _mac_lower in starlink_bssids or _mac_lower in global_ap:
                _mdata['type'] = 'ap'
                _mdata['subtype'] = 'starlink' if _mac_lower in starlink_bssids else _mdata.get('subtype')
                if 'ssid' not in _mdata or _mdata.get('ssid') in (None, ''):
                    _mdata['ssid'] = global_ap_ssids.get(_mac_lower)
            elif _mac_lower in global_client:
                _mdata['type'] = 'client'
                _mdata.setdefault('subtype', None)
                _mdata.setdefault('ssid', None)
            relationship = relationships.get(_mac_lower)
            if isinstance(relationship, dict):
                _mdata['type'] = 'client'
                for key, value in relationship.items():
                    _mdata[key] = value


def _apply_full_scan_relationship_snapshot(r, scan_id, round_results):
    try:
        raw = r.get(f"full_scan:{scan_id}:relationships")
        relationships = json.loads(raw) if raw else {}
    except Exception:
        relationships = {}
    if not isinstance(relationships, dict) or not relationships:
        return
    for point_data in (round_results or {}).values():
        if not isinstance(point_data, dict):
            continue
        for mac, mac_data in (point_data.get("macs") or {}).items():
            relationship = relationships.get(str(mac).lower())
            if not isinstance(mac_data, dict) or not isinstance(relationship, dict):
                continue
            mac_data["type"] = "client"
            for key, value in relationship.items():
                mac_data[key] = value


def _remaining_seconds(deadline_ts):
    if deadline_ts is None:
        return None
    return max(0, int(deadline_ts - time.time()))


def _deadline_reached(deadline_ts):
    return deadline_ts is not None and time.time() >= deadline_ts


def _full_scan_stop_reason_from_redis(r, scan_id=None):
    """检查全面扫描停止标志；读取者只忽略不匹配信号，绝不负责删除。"""
    raw = r.get('full_scan:stop')
    if raw:
        try:
            stop_data = json.loads(raw)
            stop_scan_id = stop_data.get('scan_id') if isinstance(stop_data, dict) else None
        except (json.JSONDecodeError, TypeError):
            stop_scan_id = None
        if scan_id:
            # 必须精确匹配；不匹配属于其他任务，由新任务启动者统一清理。
            if not stop_scan_id or stop_scan_id != scan_id:
                pass
            else:
                return stop_data.get('reason') or 'manual_stop'
        else:
            # 旧流程（未传 scan_id）：兼容旧格式
            return "manual_stop"
    raw = r.get('multi_scan:stop_full_area_scan')
    if not raw:
        return None
    if raw == "time_limit":
        return "time_limit"
    if scan_id:
        # 旧 key 无 scan_id，任务级读取只接受 time_limit；读取者不得删除。
        return None
    return "manual_stop"


def _parse_optional_positive_float(value, default=None):
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def _parse_optional_nonnegative_float(value, default=None):
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def local_now_iso():
    """返回设备本地时区 ISO 时间，用于人直接查看的扫描结果。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _safe_full_scan_summary_path(round_id):
    safe_round_id = "".join(
        ch if ch.isalnum() or ch in ("_", "-") else "_"
        for ch in str(round_id or "unknown")
    )
    return os.path.join(FULL_SCAN_TEST_SUMMARY_DIR, f"full_scan_{safe_round_id}_summary.json")


def _write_full_scan_test_summary(logger_obj, round_summary, timing_trace=None):
    if not FULL_SCAN_TEST_SUMMARY_ENABLED or not isinstance(round_summary, dict):
        return None
    summary_payload = dict(round_summary)
    if timing_trace is not None and timing_trace.enabled:
        summary_payload["timing_trace"] = timing_trace.summary()
    path = _safe_full_scan_summary_path(round_summary.get("round_id"))
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(summary_payload, fp, ensure_ascii=False, indent=2, sort_keys=True)
            fp.write("\n")
        logger_obj.info(f"🧾 全面扫描测试运行摘要: {path}")
        return path
    except Exception as exc:
        logger_obj.warning(f"⚠️ 全面扫描测试运行摘要保存失败: {exc}")
        return None


def _build_full_scan_round_payload(
    *,
    latest_round,
    round_id,
    round_status,
    stop_reason,
    expected_points,
    results,
    scan_time_limit,
    time_interval,
    window_index,
    window_started_at,
    window_deadline_at,
    round_started_at=None,
    round_finished_at=None,
    whitelist_key=None,
    whitelist_count=None,
    timing_trace=None,
    timing_fields=None,
    mode=None,
    image_context=None,
    work_ranges=None,
    target_ranges=None,
):
    build_started = timing_trace.start() if timing_trace is not None else None
    payload = {
        "latest_round": latest_round,
        "round_index": latest_round,
        "round_id": round_id,
        "round_status": round_status,
        "stop_reason": stop_reason,
        "expected_points": expected_points,
        "completed_points": _count_full_scan_completed_points(results),
        "scan_time_limit": scan_time_limit,
        "scan_time_limit_unit": "minutes",
        "time_interval": time_interval,
        "time_interval_unit": "seconds",
        "window_index": window_index,
        "window_started_at": window_started_at,
        "window_deadline_at": window_deadline_at,
        "remaining_seconds": _remaining_seconds(window_deadline_at),
        "round_started_at": round_started_at,
        "round_finished_at": round_finished_at,
        "whitelist_key": whitelist_key,
        "whitelist_count": whitelist_count,
        "results": results,
    }
    if mode in ("panorama", "single"):
        payload["mode"] = mode
    if isinstance(image_context, dict):
        payload["image_context"] = image_context
    if isinstance(work_ranges, list):
        payload["work_ranges"] = work_ranges
    if isinstance(target_ranges, list):
        payload["target_ranges"] = target_ranges
    if timing_trace is not None:
        fields = dict(timing_fields or {})
        fields.setdefault("round_result_count", len(results or {}))
        timing_trace.finish("result_payload_build", build_started, **fields)
    return payload


def _write_full_scan_realtime_result(r, payload, timing_trace=None, timing_fields=None):
    """保持旧调用兼容，同时可选拆分序列化与 Redis 写入计时。"""
    fields = dict(timing_fields or {})
    fields.setdefault("round_result_count", len(payload.get("results") or {}))
    serialize_started = timing_trace.start() if timing_trace is not None else None
    serialized = json.dumps(payload)
    fields["serialized_bytes"] = len(serialized.encode("utf-8"))
    if timing_trace is not None:
        timing_trace.finish("result_serialize", serialize_started, **fields)
    write_started = timing_trace.start() if timing_trace is not None else None
    r.set('full_scan:results', serialized)
    r.set('full_scan:latest_round', str(payload["latest_round"]))
    if timing_trace is not None:
        timing_trace.finish("redis_result_write", write_started, **fields)


def _is_qualified_full_scan_fixed_evidence(point_data):
    """正式统计只接受已完整采样的固定点证据。"""
    return (
        isinstance(point_data, dict)
        and point_data.get("evidence_role") == "fixed_point"
        and point_data.get("sampling_complete") is True
    )


def _full_scan_evidence_completion(interruption_reason, completed):
    """统一生成配置优先证据的完成状态和原因。"""
    if interruption_reason:
        return False, str(interruption_reason)
    if completed:
        return True, None
    return False, "config_priority_partial"


def _full_scan_notify_evidence_completion(notify_data):
    """将点位抓包通知转换为固定点证据完成语义。"""
    if not isinstance(notify_data, dict):
        return False, "capture_notify_missing"
    status = str(notify_data.get("status") or "").strip().lower()
    if status == "done":
        return True, None
    reason = (
        notify_data.get("reason")
        or notify_data.get("message")
        or (status if status else "capture_incomplete")
    )
    return False, str(reason)


def _count_full_scan_completed_points(results):
    count = 0
    for point_data in (results or {}).values():
        if _is_qualified_full_scan_fixed_evidence(point_data):
            count += 1
    return count


def _full_scan_count_snapshot(pixel_execution_plan, results=None, *,
                              executed_point_count=0):
    """全面扫描统一计数口径；旧字段由调用方映射到明确的新字段。"""
    stages = (pixel_execution_plan or {}).get("stages") or {}
    skipped = (pixel_execution_plan or {}).get("skipped") or {}
    converted = sum(len(items or []) for items in stages.values())
    skipped_count = sum(len(items or []) for items in skipped.values())
    results = results or {}
    fixed_results = [
        item for item in results.values()
        if isinstance(item, dict) and item.get("evidence_role") == "fixed_point"
    ]
    return {
        "planned_point_count": converted + skipped_count,
        "converted_point_count": converted,
        "skipped_point_count": skipped_count,
        "executed_point_count": int(executed_point_count),
        "completed_fixed_point_count": _count_full_scan_completed_points(results),
        "failed_point_count": sum(
            1 for item in fixed_results
            if item.get("movement_status") == "failed"
            or item.get("completion_reason") == "movement_failed"
        ),
        "result_record_count": len(results),
        "path_evidence_count": sum(
            1 for item in results.values()
            if isinstance(item, dict) and item.get("evidence_role") != "fixed_point"
        ),
    }


def _patch_full_scan_counts(r, counts):
    """计数状态是可观测性旁路；Redis 异常不得中断正式扫描。"""
    try:
        patch_ptz_status(r, {"full_scan": dict(counts or {})})
        return True
    except Exception as exc:
        logger.warning(f"⚠️ 全面扫描计数状态更新失败（扫描继续）: {exc}")
        return False


def _publish_full_scan_terminal_status(
    r,
    *,
    scan_id,
    round_payload,
    rounds_completed,
    final_state,
    reason=None,
    position=None,
):
    """用已持久化的最终轮次统一收口状态，避免运行期计数残留。"""
    round_payload = round_payload if isinstance(round_payload, dict) else {}
    completed = int(round_payload.get("completed_points") or 0)
    expected = int(round_payload.get("expected_points") or 0)
    converted = int(round_payload.get("converted_point_count") or 0)
    executed = int(round_payload.get("executed_point_count") or 0)
    total = max(expected, converted, completed)
    normalized_executed = max(executed, completed)
    terminal_phase = (
        "stopped"
        if final_state == "stopped"
        else ("error" if final_state == "error" else "completed")
    )
    full_scan_patch = {
        "active": False,
        "state": final_state,
        "terminal": True,
        "stop_requested": False,
        "reason": reason,
        "scan_id": scan_id,
        "rounds_completed": int(rounds_completed or 0),
        "phase": terminal_phase,
        "current_point": completed,
        "total_points": total,
        "executed_point_count": normalized_executed,
        "completed_fixed_point_count": completed,
    }
    for field in (
        "latest_round",
        "round_id",
        "planned_point_count",
        "converted_point_count",
        "skipped_point_count",
        "failed_point_count",
        "result_record_count",
        "path_evidence_count",
    ):
        if field in round_payload:
            full_scan_patch[field] = round_payload[field]
    if "latest_round" in round_payload:
        full_scan_patch["round"] = round_payload["latest_round"]
    if isinstance(round_payload.get("whitelist_refinement"), dict):
        full_scan_patch["refinement"] = round_payload["whitelist_refinement"]

    patch = {"state": "IDLE", "full_scan": full_scan_patch}
    if isinstance(position, dict):
        patch["position"] = position
    patch_ptz_status(r, patch)
    return full_scan_patch


def _full_scan_config_session_stream_key(session_id):
    safe_session_id = "".join(
        ch if ch.isalnum() or ch in ("_", "-") else "_"
        for ch in str(session_id or "unknown")
    )
    return f"full_scan:config_session:{safe_session_id}:events"


def _full_scan_should_replace_mac_observation(existing, candidate):
    confidence_rank = {"declared": 3, "confirmed": 3, "inferred": 2, "uncertain": 1}
    existing_rank = confidence_rank.get((existing or {}).get("channel_confidence"), 0)
    candidate_rank = confidence_rank.get((candidate or {}).get("channel_confidence"), 0)
    is_client = (
        (existing or {}).get("type") == "client"
        or (candidate or {}).get("type") == "client"
    )
    if not is_client and candidate_rank != existing_rank:
        return candidate_rank > existing_rank

    existing_rssi = _numeric_or_none((existing or {}).get("rssi_avg"))
    candidate_rssi = _numeric_or_none((candidate or {}).get("rssi_avg"))
    if candidate_rssi != existing_rssi:
        return (
            candidate_rssi is not None
            and (existing_rssi is None or candidate_rssi > existing_rssi)
        )

    existing_samples = int((existing or {}).get("rssi_samples") or 0)
    candidate_samples = int((candidate or {}).get("rssi_samples") or 0)
    if candidate_samples != existing_samples:
        return candidate_samples > existing_samples

    existing_order = int((existing or {}).get("config_order_index") or 0)
    candidate_order = int((candidate or {}).get("config_order_index") or 0)
    return candidate_order < existing_order


def _full_scan_actual_observations(entry):
    """Return per-configuration evidence using the receiver's actual tuning."""
    if not isinstance(entry, dict):
        return []
    observations = entry.get("observed_configs")
    if isinstance(observations, list):
        return [dict(item) for item in observations if isinstance(item, dict)]
    capture_config = entry.get("capture_config") or {}
    channel = capture_config.get("channel", entry.get("channel"))
    if channel is None:
        return []
    return [{
        "channel": channel,
        "bandwidth": capture_config.get(
            "bandwidth",
            entry.get("bandwidth", "HT20"),
        ),
        "rssi_avg": entry.get("rssi_avg"),
        "rssi_samples": entry.get("rssi_samples"),
        "omni_rssi_avg": entry.get("omni_rssi_avg"),
        "omni_rssi_samples": entry.get("omni_rssi_samples"),
        "first_seen_at": entry.get("first_seen_at"),
        "last_seen_at": entry.get("last_seen_at"),
        "config_order_index": entry.get("config_order_index", 0),
    }]


def _merge_full_scan_actual_observations(existing, candidate):
    merged = {}
    for entry in (existing, candidate):
        for raw in _full_scan_actual_observations(entry):
            channel = raw.get("channel")
            if channel is None:
                continue
            item = dict(raw)
            key = (
                int(channel),
                str(item.get("bandwidth") or "HT20"),
            )
            previous = merged.get(key)
            if previous is None:
                merged[key] = item
                continue
            previous["first_seen_at"] = _iso_min(
                previous.get("first_seen_at"),
                item.get("first_seen_at"),
            )
            previous["last_seen_at"] = _iso_max(
                previous.get("last_seen_at"),
                item.get("last_seen_at"),
            )
            previous["rssi_samples"] = (
                int(previous.get("rssi_samples") or 0)
                + int(item.get("rssi_samples") or 0)
            )
            previous_rssi = _numeric_or_none(previous.get("rssi_avg"))
            item_rssi = _numeric_or_none(item.get("rssi_avg"))
            if item_rssi is not None and (
                previous_rssi is None or item_rssi > previous_rssi
            ):
                previous["rssi_avg"] = item_rssi
            previous["omni_rssi_samples"] = (
                int(previous.get("omni_rssi_samples") or 0)
                + int(item.get("omni_rssi_samples") or 0)
            )
            previous_omni = _numeric_or_none(previous.get("omni_rssi_avg"))
            item_omni = _numeric_or_none(item.get("omni_rssi_avg"))
            if item_omni is not None and (
                previous_omni is None or item_omni > previous_omni
            ):
                previous["omni_rssi_avg"] = item_omni
            previous["config_order_index"] = min(
                int(previous.get("config_order_index") or 0),
                int(item.get("config_order_index") or 0),
            )
    return list(merged.values())


def _merge_full_scan_macs(base, incoming):
    """Merge MAC evidence, keeping stronger RSSI while preserving seen timestamps."""
    merged = dict(base or {})
    for mac, entry in (incoming or {}).items():
        if not isinstance(entry, dict):
            continue
        mac_key = str(mac).lower()
        new_entry = dict(entry)
        existing = merged.get(mac_key)
        if not isinstance(existing, dict):
            merged[mac_key] = new_entry
            continue
        merged_observations = _merge_full_scan_actual_observations(
            existing,
            new_entry,
        )
        same_config = (
            existing.get("channel") == new_entry.get("channel")
            and existing.get("bandwidth") == new_entry.get("bandwidth")
            and int(existing.get("config_order_index") or 0)
            == int(new_entry.get("config_order_index") or 0)
        )
        keep_new = _full_scan_should_replace_mac_observation(existing, new_entry)
        first_seen = _iso_min(existing.get("first_seen_at"), new_entry.get("first_seen_at"))
        last_seen = _iso_max(existing.get("last_seen_at"), new_entry.get("last_seen_at"))
        if keep_new:
            new_entry["first_seen_at"] = first_seen
            new_entry["last_seen_at"] = last_seen
            if same_config:
                new_entry["rssi_samples"] = (
                    int(existing.get("rssi_samples") or 0)
                    + int(new_entry.get("rssi_samples") or 0)
                )
            merged[mac_key] = new_entry
        else:
            existing["first_seen_at"] = first_seen
            existing["last_seen_at"] = last_seen
            if same_config:
                existing["rssi_samples"] = (
                    int(existing.get("rssi_samples") or 0)
                    + int(new_entry.get("rssi_samples") or 0)
                )
            merged[mac_key] = existing
        selected = merged[mac_key]
        selected["observed_configs"] = merged_observations
        if new_entry.get("relationship_status") is not None:
            for relationship_key in (
                "relationship_status",
                "current_observed_ap",
                "observed_aps",
                "observed_best_config",
            ):
                if relationship_key in new_entry:
                    selected[relationship_key] = new_entry[relationship_key]
            if new_entry.get("type") == "client":
                for config_key in (
                    "channel",
                    "bandwidth",
                    "channel_confidence",
                    "channel_source",
                ):
                    if config_key in new_entry:
                        selected[config_key] = new_entry[config_key]
    return merged


def _read_full_scan_config_session_events(r, stream_key, last_id="0-0", count=256):
    try:
        events = r.xrange(stream_key, min=f"({last_id}", max="+", count=count)
    except Exception:
        return last_id, []
    parsed = []
    for event_id, fields in events or []:
        last_id = event_id
        if not isinstance(fields, dict):
            continue
        event = fields.get("event")
        if event != "samples":
            parsed.append({"event": event, "fields": fields})
            continue
        try:
            macs = json.loads(fields.get("macs") or "{}")
        except Exception:
            macs = {}
        parsed.append({
            "event": "samples",
            "fields": fields,
            "macs": macs,
        })
    return last_id, parsed


def _collect_full_scan_config_session_macs(
    r,
    stream_key,
    last_id,
    timing_trace=None,
    timing_fields=None,
):
    read_started = timing_trace.start() if timing_trace is not None else None
    last_id, events = _read_full_scan_config_session_events(r, stream_key, last_id)
    macs = {}
    for event in events:
        if event.get("event") == "samples":
            macs = _merge_full_scan_macs(macs, event.get("macs") or {})
    if timing_trace is not None:
        fields = dict(timing_fields or {})
        fields.update({"event_count": len(events), "mac_count": len(macs)})
        timing_trace.finish("stream_read_merge", read_started, **fields)
    return last_id, macs


def _start_full_scan_config_capture_session(r, *, scan_id, session_id, config,
                                            config_order_index=0,
                                            stop_key='full_scan:stop',
                                            legacy_stop_key='multi_scan:stop_full_area_scan',
                                            timeout=10.0):
    stream_key = _full_scan_config_session_stream_key(session_id)
    notify_key = f"full_scan:{session_id}:session_start_notify"
    try:
        r.delete(notify_key)
    except Exception:
        pass
    r.lpush('capture:command_queue', json.dumps({
        'action': 'start_config_session',
        'scan_id': scan_id,
        'session_id': session_id,
        'stream_key': stream_key,
        'notify_key': notify_key,
        'channel': config.get('channel'),
        'bandwidth': config.get('bandwidth', 'HT20'),
        'config_order_index': int(config_order_index or 0),
        'stop_key': stop_key,
        'legacy_stop_key': legacy_stop_key,
    }))
    result = r.brpop(notify_key, timeout=max(1, int(timeout)))
    if not result:
        return {
            'status': 'error',
            'message': 'start_config_session timeout',
            'stream_key': stream_key,
        }
    try:
        payload = json.loads(result[1])
    except Exception:
        payload = {'status': 'error', 'message': 'invalid start notify'}
    payload.setdefault('stream_key', stream_key)
    return payload


def _stop_full_scan_config_capture_session(r, *, scan_id, session_id,
                                           reason='completed', timeout=5.0,
                                           max_attempts=2):
    notify_key = f"full_scan:{session_id}:session_stop_notify"
    last_payload = {'status': 'timeout', 'reason': reason}
    for attempt in range(max(1, int(max_attempts or 1))):
        try:
            r.delete(notify_key)
        except Exception:
            pass
        r.lpush('capture:command_queue', json.dumps({
            'action': 'stop_config_session',
            'scan_id': scan_id,
            'session_id': session_id,
            'notify_key': notify_key,
            'reason': reason,
            'timeout': timeout,
        }))
        wait_deadline = time.time() + max(0.0, float(timeout or 0))
        result = None
        while time.time() < wait_deadline:
            raw_result = r.rpop(notify_key)
            if raw_result is not None:
                result = (notify_key, raw_result)
                break
            time.sleep(min(0.05, max(0.0, wait_deadline - time.time())))
        if not result:
            last_payload = {
                'status': 'timeout',
                'reason': reason,
                'attempt': attempt + 1,
            }
            continue
        try:
            last_payload = json.loads(result[1])
        except Exception:
            return {'status': 'error', 'reason': 'invalid_stop_notify'}
        last_payload.setdefault('attempt', attempt + 1)
        if last_payload.get('status') in ('stopped', 'idle'):
            return last_payload
        if last_payload.get('status') != 'timeout':
            return last_payload
    return last_payload


def _full_scan_config_session_stop_confirmed(payload):
    return (
        isinstance(payload, dict)
        and payload.get("status") in ("stopped", "idle")
    )


def _full_scan_point_in_work_ranges(pan, tilt, work_ranges):
    """按坐标判断点位是否落在任意工作区域内。"""
    try:
        pan = float(pan)
        tilt = float(tilt)
    except (TypeError, ValueError):
        return False

    for item in work_ranges or []:
        try:
            pan_range = item.get("pan_range") or []
            tilt_range = item.get("tilt_range") or []
            pan_min, pan_max = sorted([float(pan_range[0]), float(pan_range[1])])
            tilt_min, tilt_max = sorted([float(tilt_range[0]), float(tilt_range[1])])
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        if pan_min <= pan <= pan_max and tilt_min <= tilt <= tilt_max:
            return True
    return False


def _full_scan_coord_key(pan, tilt, dedupe_deg):
    """把相同或非常接近的点位归到同一个坐标桶。"""
    try:
        pan = float(pan)
        tilt = float(tilt)
        dedupe_deg = float(dedupe_deg)
    except (TypeError, ValueError):
        return f"{pan},{tilt}"

    if dedupe_deg <= 0:
        return f"{round(pan, 4)},{round(tilt, 4)}"

    bucket_pan = round(round(pan / dedupe_deg) * dedupe_deg, 4)
    bucket_tilt = round(round(tilt / dedupe_deg) * dedupe_deg, 4)
    return f"{bucket_pan},{bucket_tilt}"


def _iso_min(left, right):
    if not left:
        return right
    if not right:
        return left
    return left if str(left) <= str(right) else right


def _iso_max(left, right):
    if not left:
        return right
    if not right:
        return left
    return left if str(left) >= str(right) else right


def _numeric_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _full_scan_filter_config_for_output(filter_config):
    """把内部中文筛选配置键转换为白名单输出使用的英文键。"""
    return {
        "enabled": bool(filter_config.get("启用", True)),
        "min_real_target_hit_points": int(filter_config.get("真实目标区最少命中点数", filter_config.get("目标区最少命中点数", filter_config.get("工作区最少命中点数", 2)))),
        "min_buffer_hit_points": int(filter_config.get("白名单缓冲区最少命中点数", 3)),
        "min_total_hit_points": int(filter_config.get("整轮最少命中点数", 4)),
        "min_decision_buffer_deg": float(filter_config.get("白名单判定最小缓冲角", 1.0)),
        "min_buffer_vs_outside_delta_db": float(filter_config.get("缓冲区相对外部最小强度差", 2.0)),
        "dispersed_external_check_enabled": bool(filter_config.get("分散强外部点检查", True)),
        "min_directional_vs_omni_delta_db": float(filter_config.get("定向相对全向最小强度差", 5.0)),
        "require_target_position_evidence": bool(filter_config.get("要求目标区位置证据", filter_config.get("要求最强点在工作区", True))),
        "require_best_point_in_work_area": bool(filter_config.get("要求最强点在工作区", True)),
        "coord_dedupe_deg": float(filter_config.get("坐标去重角度", 0.05)),
        "dev_ratio_normal_min": FULL_SCAN_DEV_RATIO_NORMAL_MIN,
        "dev_ratio_large_max": FULL_SCAN_DEV_RATIO_LARGE_MAX,
        "warm_start_threshold": FULL_SCAN_WARM_START_THRESHOLD,
        "warm_remaining_interval": FULL_SCAN_WARM_REMAINING_INTERVAL,
        "large_edge_ratio": FULL_SCAN_LARGE_EDGE_RATIO,
        "large_rssi_std_min": FULL_SCAN_LARGE_RSSI_STD_MIN,
    }


# ── 白名单筛选原因 code 映射 ──────────────────────────────────────

_REASON_CODE_MAP = {
    "整轮命中点数不足": "total_hits_too_low",
    "工作区命中点数不足": "work_hits_too_low",
    "目标区命中点数不足": "target_hits_too_low",
    "最强点不在工作区": "best_point_outside_work",
    "目标区位置证据不足": "target_position_evidence_low",
    "工作区最强点弱于外部(验证失败)": "work_weaker_than_outside",
    "工作区相对其他区域强度差不足": "work_vs_other_delta_too_low",
    "白名单缓冲区命中点数不足": "buffer_hits_too_low",
    "白名单缓冲区位置证据不足": "buffer_position_evidence_low",
    "白名单缓冲区相对外部强度差不足": "buffer_vs_outside_delta_too_low",
    "缓冲区外强点分散": "dispersed_external_strong_points",
    "定向相对全向强度差不足": "directional_vs_omni_delta_too_low",
    "大范围工作区RSSI分布点数不足": "large_work_rssi_insufficient",
    "真实目标区命中达标": "real_target_hits_passed",
    "白名单缓冲区命中达标": "buffer_hits_passed",
    "白名单缓冲区位置证据通过": "buffer_position_evidence_passed",
    "白名单缓冲区相对外部强度通过": "buffer_vs_outside_delta_passed",
    "缓冲区外强点未分散": "dispersed_external_check_passed",
}

# 带动态数值的原因前缀 → code
_REASON_PREFIX_MAP = {
    "大范围峰值贴边": "large_peak_near_edge",
    "大范围信号梯度不足": "large_rssi_gradient_low",
}


def _reason_to_code(reason_text):
    """将中文筛选原因转换为稳定的英文 reason_code。"""
    code = _REASON_CODE_MAP.get(reason_text)
    if code:
        return code
    for prefix, prefix_code in _REASON_PREFIX_MAP.items():
        if reason_text.startswith(prefix):
            return prefix_code
    return "unknown"


def _estimate_full_scan_weighted_position(points):
    total_weight = 0.0
    sum_pan = 0.0
    sum_tilt = 0.0
    for item in points or []:
        pos = item.get("position") or {}
        pan = _numeric_or_none(pos.get("pan"))
        tilt = _numeric_or_none(pos.get("tilt"))
        rssi = _numeric_or_none(item.get("rssi_avg"))
        if pan is None or tilt is None or rssi is None:
            continue
        weight = max(rssi + 100.0, 1.0)
        total_weight += weight
        sum_pan += pan * weight
        sum_tilt += tilt * weight
    if total_weight <= 0:
        return None
    return {
        "pan": round(sum_pan / total_weight, 2),
        "tilt": round(sum_tilt / total_weight, 2),
    }


def _full_scan_work_ranges_for_output(work_ranges):
    result = []
    for item in work_ranges or []:
        normalized = _normalize_full_scan_range(item)
        if not normalized:
            continue
        pan_min, pan_max, tilt_min, tilt_max = normalized
        result.append({
            "pan_range": [round(pan_min, 2), round(pan_max, 2)],
            "tilt_range": [round(tilt_min, 2), round(tilt_max, 2)],
        })
    return result


def _point_in_single_range(pan, tilt, item):
    normalized = _normalize_full_scan_range(item)
    if not normalized:
        return False
    pan_min, pan_max, tilt_min, tilt_max = normalized
    try:
        pan = float(pan)
        tilt = float(tilt)
    except (TypeError, ValueError):
        return False
    return pan_min <= pan <= pan_max and tilt_min <= tilt <= tilt_max


def _build_whitelist_decision_ranges(work_ranges, antenna_bias=None, min_buffer_deg=1.0):
    pan_bias = _numeric_or_none((antenna_bias or {}).get("pan_bias_deg")) or 0.0
    tilt_bias = _numeric_or_none((antenna_bias or {}).get("tilt_bias_deg")) or 0.0
    min_buffer_deg = max(0.0, float(min_buffer_deg or 0.0))
    pan_buffer = max(abs(pan_bias), min_buffer_deg)
    tilt_buffer = max(abs(tilt_bias), min_buffer_deg)
    source = "antenna_bias" if antenna_bias and antenna_bias.get("enabled") else "minimum_fallback"
    decision_ranges = []
    for idx, item in enumerate(work_ranges or []):
        normalized = _normalize_full_scan_range(item)
        if not normalized:
            continue
        pan_min, pan_max, tilt_min, tilt_max = normalized
        decision_ranges.append({
            "area_index": idx,
            "target_range": {
                "pan_range": [round(pan_min, 2), round(pan_max, 2)],
                "tilt_range": [round(tilt_min, 2), round(tilt_max, 2)],
            },
            "buffer_range": {
                "pan_range": [round(pan_min - pan_buffer, 2), round(pan_max + pan_buffer, 2)],
                "tilt_range": [round(tilt_min - tilt_buffer, 2), round(tilt_max + tilt_buffer, 2)],
            },
            "center": {
                "pan": round((pan_min + pan_max) / 2.0, 2),
                "tilt": round((tilt_min + tilt_max) / 2.0, 2),
            },
            "pan_buffer_deg": round(pan_buffer, 2),
            "tilt_buffer_deg": round(tilt_buffer, 2),
            "buffer_source": source,
        })
    return decision_ranges


def _point_quadrant_from_center(point, center):
    pos = point.get("position") or {}
    pan = _numeric_or_none(pos.get("pan"))
    tilt = _numeric_or_none(pos.get("tilt"))
    center_pan = _numeric_or_none(center.get("pan"))
    center_tilt = _numeric_or_none(center.get("tilt"))
    if pan is None or tilt is None or center_pan is None or center_tilt is None:
        return None
    horizontal = "right" if pan >= center_pan else "left"
    vertical = "top" if tilt >= center_tilt else "bottom"
    return f"{horizontal}_{vertical}"


def _has_opposite_quadrants(quadrants):
    q = set(quadrants or [])
    return (
        ("left_top" in q and "right_bottom" in q) or
        ("right_top" in q and "left_bottom" in q)
    )


def _build_full_scan_whitelist_payload(
    *,
    round_payload,
    work_ranges,
    filter_config,
    antenna_bias=None,
    round_dev_area_ratio=1.0,
    is_large_area_mode=False,
):
    """
    从完整成功轮次的原始点位结果生成按轮次保存的 MAC 白名单。

    关键点：
    - 按坐标是否落在 work_ranges 判断工作区，不按 phase 判断。
    - 同一坐标桶内重复扫到同一 MAC 只算一个命中，RSSI 取该坐标桶内最强值。
    - 全向 RSSI 缺失时跳过定向/全向差值判断。
    """
    results = round_payload.get("results") or {}
    coord_dedupe_deg = filter_config.get("坐标去重角度", 0.05)
    min_real_target_hits = int(filter_config.get(
        "真实目标区最少命中点数",
        filter_config.get("目标区最少命中点数", filter_config.get("工作区最少命中点数", 2)),
    ))
    min_buffer_hits = int(filter_config.get("白名单缓冲区最少命中点数", 3))
    min_total_hits = int(filter_config.get("整轮最少命中点数", 4))
    min_decision_buffer_deg = float(filter_config.get("白名单判定最小缓冲角", 1.0))
    min_buffer_delta = float(filter_config.get(
        "缓冲区相对外部最小强度差",
        filter_config.get("工作区相对其他区域最小强度差", 2.0),
    ))
    dispersed_external_check = bool(filter_config.get("分散强外部点检查", True))
    mode = "large" if is_large_area_mode else (
        "relaxed" if round_dev_area_ratio < FULL_SCAN_DEV_RATIO_NORMAL_MIN else "normal"
    )

    min_omni_delta = float(filter_config.get("定向相对全向最小强度差", 5.0))
    require_position_evidence = bool(filter_config.get("要求目标区位置证据", filter_config.get("要求最强点在工作区", True)))
    filter_enabled = bool(filter_config.get("启用", True))
    decision_ranges = _build_whitelist_decision_ranges(
        work_ranges,
        antenna_bias=antenna_bias,
        min_buffer_deg=min_decision_buffer_deg,
    )

    mac_config_buckets = {}
    mac_identity = {}
    mac_evidence_counts = {}

    for point_id, point_data in results.items():
        if not isinstance(point_data, dict):
            continue
        if not _is_qualified_full_scan_fixed_evidence(point_data):
            point_qualified = False
        else:
            point_qualified = True
        evidence_role = point_data.get("evidence_role")
        position = point_data.get("position") or {}
        pan = position.get("pan")
        tilt = position.get("tilt")
        macs = point_data.get("macs") or {}

        for mac, mac_data in macs.items():
            if not isinstance(mac_data, dict):
                continue
            mac_l = str(mac).lower()
            if mac_l == FULL_SCAN_MARKER_MAC or mac_data.get("synthetic") or mac_data.get("role") == "scan_point_marker":
                if mac_l != FULL_SCAN_MARKER_MAC:
                    counts = mac_evidence_counts.setdefault(mac_l, {})
                    counts["excluded_marker_or_synthetic"] = (
                        counts.get("excluded_marker_or_synthetic", 0) + 1
                    )
                continue

            identity = mac_identity.setdefault(mac_l, {})
            for key in (
                "type",
                "subtype",
                "ssid",
                "relationship_status",
                "current_observed_ap",
                "observed_aps",
                "observed_best_config",
                "channel",
                "bandwidth",
                "channel_source",
                "channel_confidence",
                "declared_bandwidth",
            ):
                value = mac_data.get(key)
                if value is not None:
                    identity[key] = value

            observations = _full_scan_actual_observations(mac_data)
            counts = mac_evidence_counts.setdefault(mac_l, {})
            observation_count = max(1, len(observations))
            if evidence_role == "path":
                counts["excluded_path"] = (
                    counts.get("excluded_path", 0) + observation_count
                )
                continue
            if not point_qualified:
                counts["excluded_partial"] = (
                    counts.get("excluded_partial", 0) + observation_count
                )
                continue
            if pan is None or tilt is None:
                counts["excluded_missing_position"] = (
                    counts.get("excluded_missing_position", 0) + observation_count
                )
                continue

            in_work_area = _full_scan_point_in_work_ranges(
                pan,
                tilt,
                work_ranges,
            )
            coord_key = _full_scan_coord_key(pan, tilt, coord_dedupe_deg)

            for observation in observations:
                rssi_avg = _numeric_or_none(observation.get("rssi_avg"))
                channel = observation.get("channel")
                if rssi_avg is None or channel is None:
                    counts["excluded_missing_config_or_rssi"] = (
                        counts.get("excluded_missing_config_or_rssi", 0) + 1
                    )
                    continue
                config_key = (
                    int(channel),
                    str(observation.get("bandwidth") or "HT20"),
                )

                first_seen_at = (
                    observation.get("first_seen_at")
                    or mac_data.get("first_seen_at")
                    or point_data.get("point_started_at")
                )
                last_seen_at = (
                    observation.get("last_seen_at")
                    or mac_data.get("last_seen_at")
                    or point_data.get("point_finished_at")
                    or first_seen_at
                )
                candidate = {
                    "coord_key": coord_key,
                    "point_id": point_id,
                    "phase": point_data.get("phase"),
                    "area_index": point_data.get("area_index"),
                    "position": {
                        "pan": round(float(pan), 2),
                        "tilt": round(float(tilt), 2),
                    },
                    "pixel_position": point_data.get("pixel_position"),
                    "in_work_area": in_work_area,
                    "rssi_avg": rssi_avg,
                    "rssi_samples": observation.get("rssi_samples"),
                    "omni_rssi_avg": _numeric_or_none(
                        observation.get("omni_rssi_avg")
                    ),
                    "omni_rssi_samples": observation.get(
                        "omni_rssi_samples",
                        0,
                    ),
                    "first_seen_at": first_seen_at,
                    "last_seen_at": last_seen_at,
                    "channel": config_key[0],
                    "bandwidth": config_key[1],
                    "config_order_index": observation.get(
                        "config_order_index",
                        0,
                    ),
                    "type": identity.get("type"),
                    "subtype": identity.get("subtype"),
                    "ssid": identity.get("ssid"),
                    "hit_count": 1,
                }

                buckets = (
                    mac_config_buckets
                    .setdefault(mac_l, {})
                    .setdefault(config_key, {})
                )
                existing = buckets.get(coord_key)
                if existing is None:
                    buckets[coord_key] = candidate
                    continue

                existing["in_work_area"] = (
                    existing.get("in_work_area") or in_work_area
                )
                existing["first_seen_at"] = _iso_min(
                    existing.get("first_seen_at"),
                    first_seen_at,
                )
                existing["last_seen_at"] = _iso_max(
                    existing.get("last_seen_at"),
                    last_seen_at,
                )
                existing["hit_count"] = existing.get("hit_count", 1) + 1

                existing_rssi = _numeric_or_none(existing.get("rssi_avg"))
                if existing_rssi is None or rssi_avg > existing_rssi:
                    candidate["first_seen_at"] = existing.get("first_seen_at")
                    candidate["last_seen_at"] = existing.get("last_seen_at")
                    candidate["hit_count"] = existing.get("hit_count", 1)
                    candidate["in_work_area"] = (
                        existing.get("in_work_area") or in_work_area
                    )
                    buckets[coord_key] = candidate

    def _config_score(item):
        config_key, buckets = item
        bucket_values = list((buckets or {}).values())
        best_rssi = max(
            (
                _numeric_or_none(point.get("rssi_avg"))
                for point in bucket_values
                if _numeric_or_none(point.get("rssi_avg")) is not None
            ),
            default=-9999.0,
        )
        sample_count = sum(
            int(point.get("rssi_samples") or 0)
            for point in bucket_values
        )
        config_order = min(
            (
                int(point.get("config_order_index") or 0)
                for point in bucket_values
            ),
            default=9999,
        )
        return best_rssi, sample_count, -config_order, -int(config_key[0])

    def _summarize_observed_configs(config_buckets, selected_key=None):
        summaries = []
        for config_key, buckets in sorted((config_buckets or {}).items()):
            bucket_values = list((buckets or {}).values())
            summaries.append({
                "channel": config_key[0],
                "bandwidth": config_key[1],
                # 沿用历史字段名；这里代表该实抓配置在固定点证据中的峰值。
                "rssi_avg": max(
                    (
                        _numeric_or_none(point.get("rssi_avg"))
                        for point in bucket_values
                        if _numeric_or_none(point.get("rssi_avg")) is not None
                    ),
                    default=None,
                ),
                "rssi_samples": sum(
                    int(point.get("rssi_samples") or 0)
                    for point in bucket_values
                ),
                "fixed_point_count": len(bucket_values),
                "first_seen_at": min(
                    (
                        point.get("first_seen_at")
                        for point in bucket_values
                        if point.get("first_seen_at")
                    ),
                    default=None,
                ),
                "last_seen_at": max(
                    (
                        point.get("last_seen_at")
                        for point in bucket_values
                        if point.get("last_seen_at")
                    ),
                    default=None,
                ),
                "selected": config_key == selected_key,
            })
        return summaries

    mac_buckets = {}
    mac_decision_config = {}
    mac_observed_config_summaries = {}
    config_selection_failures = {}
    for mac, config_buckets in mac_config_buckets.items():
        if not config_buckets:
            continue
        identity = mac_identity.get(mac) or {}
        device_type = identity.get("type")
        relationship_status = identity.get("relationship_status")
        desired_channel = identity.get("channel")
        desired_bandwidth = str(identity.get("bandwidth") or "HT20")
        desired_key = None
        try:
            if desired_channel is not None:
                desired_key = (int(desired_channel), desired_bandwidth)
        except (TypeError, ValueError):
            desired_key = None

        selected_key = None
        selection_source = None
        if device_type == "ap" and identity.get("channel_confidence") == "declared":
            if desired_key in config_buckets:
                selected_key = desired_key
                selection_source = "ap_declared_observed"
            else:
                config_selection_failures[mac] = "declared_config_not_observed"
        elif device_type == "ap":
            if desired_key in config_buckets:
                selected_key = desired_key
                selection_source = "ap_inferred_observed"
            else:
                selected_key = max(config_buckets.items(), key=_config_score)[0]
                selection_source = "beacon_capture_config"
        elif (
            device_type == "client"
            and relationship_status in ("confirmed", "inferred")
            and desired_key in config_buckets
        ):
            selected_key = desired_key
            selection_source = "relationship_config_observed"
        else:
            selected_key = max(config_buckets.items(), key=_config_score)[0]
            selection_source = (
                "observed_best_config_fallback"
                if device_type == "client"
                and relationship_status in ("confirmed", "inferred")
                else "observed_best_config"
            )

        if selected_key is None:
            mac_observed_config_summaries[mac] = (
                _summarize_observed_configs(config_buckets)
            )
            continue
        mac_buckets[mac] = config_buckets[selected_key]
        mac_observed_config_summaries[mac] = _summarize_observed_configs(
            config_buckets,
            selected_key,
        )
        counts = mac_evidence_counts.setdefault(mac, {})
        counts["included_fixed_points"] = len(config_buckets[selected_key])
        counts["excluded_wrong_config"] = sum(
            len(buckets)
            for key, buckets in config_buckets.items()
            if key != selected_key
        )
        mac_decision_config[mac] = {
            "channel": selected_key[0],
            "bandwidth": selected_key[1],
            "source": selection_source,
            "relationship_status": relationship_status,
            "available_configs": [
                {
                    "channel": key[0],
                    "bandwidth": key[1],
                    "fixed_point_count": len(buckets),
                }
                for key, buckets in sorted(config_buckets.items())
            ],
        }

    mac_whitelist = []
    rejected_macs = []

    for mac, buckets in mac_buckets.items():
        bucket_values = list(buckets.values())
        if not bucket_values:
            continue

        total_hit_points = len(bucket_values)

        best_point = max(bucket_values, key=lambda b: b.get("rssi_avg", -9999))
        estimated_position = _estimate_full_scan_weighted_position(bucket_values)

        target_evaluations = []
        for decision in decision_ranges:
            target_range = decision["target_range"]
            buffer_range = decision["buffer_range"]
            real_target_points = [
                b for b in bucket_values
                if _point_in_single_range(
                    (b.get("position") or {}).get("pan"),
                    (b.get("position") or {}).get("tilt"),
                    target_range,
                )
            ]
            buffer_points = [
                b for b in bucket_values
                if _point_in_single_range(
                    (b.get("position") or {}).get("pan"),
                    (b.get("position") or {}).get("tilt"),
                    buffer_range,
                )
            ]
            outside_buffer_points = [
                b for b in bucket_values
                if not _point_in_single_range(
                    (b.get("position") or {}).get("pan"),
                    (b.get("position") or {}).get("tilt"),
                    buffer_range,
                )
            ]
            best_buffer_point = max(buffer_points, key=lambda b: b.get("rssi_avg", -9999)) if buffer_points else None
            best_outside_buffer_point = max(outside_buffer_points, key=lambda b: b.get("rssi_avg", -9999)) if outside_buffer_points else None
            buffer_best_rssi = best_buffer_point.get("rssi_avg") if best_buffer_point else None
            outside_buffer_best_rssi = best_outside_buffer_point.get("rssi_avg") if best_outside_buffer_point else None
            buffer_vs_outside_delta = (
                round(buffer_best_rssi - outside_buffer_best_rssi, 2)
                if buffer_best_rssi is not None and outside_buffer_best_rssi is not None
                else None
            )
            estimated_position_in_buffer = (
                _point_in_single_range(
                    estimated_position.get("pan"),
                    estimated_position.get("tilt"),
                    buffer_range,
                )
                if estimated_position else False
            )
            if _point_in_single_range(
                (best_point.get("position") or {}).get("pan"),
                (best_point.get("position") or {}).get("tilt"),
                buffer_range,
            ):
                position_evidence = "best_point_in_decision_buffer"
                position_confidence = "high"
                position_evidence_failure_reason = None
            elif estimated_position_in_buffer:
                position_evidence = "weighted_centroid_in_decision_buffer"
                position_confidence = "low"
                position_evidence_failure_reason = None
            else:
                position_evidence = "outside_decision_buffer"
                position_confidence = "none"
                position_evidence_failure_reason = "best_point_and_weighted_centroid_outside_decision_buffer"

            strong_external_points = []
            if buffer_best_rssi is not None:
                for point in outside_buffer_points:
                    point_rssi = _numeric_or_none(point.get("rssi_avg"))
                    if point_rssi is None:
                        continue
                    if point_rssi >= buffer_best_rssi - min_buffer_delta:
                        quadrant = _point_quadrant_from_center(point, decision["center"])
                        strong_external_points.append({
                            "quadrant": quadrant,
                            "point": point,
                            "delta_to_buffer_best": round(buffer_best_rssi - point_rssi, 2),
                        })
            dispersed_quadrants = sorted({
                item["quadrant"] for item in strong_external_points
                if item.get("quadrant")
            })
            dispersed_external = _has_opposite_quadrants(dispersed_quadrants)
            failed_reasons_for_target = []
            if total_hit_points < min_total_hits:
                failed_reasons_for_target.append("整轮命中点数不足")
            if len(real_target_points) < min_real_target_hits:
                failed_reasons_for_target.append("目标区命中点数不足")
            if len(buffer_points) < min_buffer_hits:
                failed_reasons_for_target.append("白名单缓冲区命中点数不足")
            if require_position_evidence and position_evidence_failure_reason:
                failed_reasons_for_target.append("白名单缓冲区位置证据不足")
            if outside_buffer_best_rssi is not None and buffer_vs_outside_delta is not None and buffer_vs_outside_delta < min_buffer_delta:
                failed_reasons_for_target.append("白名单缓冲区相对外部强度差不足")
            if dispersed_external_check and dispersed_external:
                failed_reasons_for_target.append("缓冲区外强点分散")

            target_evaluations.append({
                "area_index": decision["area_index"],
                "target_range": target_range,
                "buffer_range": buffer_range,
                "pan_buffer_deg": decision["pan_buffer_deg"],
                "tilt_buffer_deg": decision["tilt_buffer_deg"],
                "buffer_source": decision["buffer_source"],
                "real_target_points": real_target_points,
                "buffer_points": buffer_points,
                "outside_buffer_points": outside_buffer_points,
                "real_target_hit_points": len(real_target_points),
                "buffer_hit_points": len(buffer_points),
                "outside_buffer_hit_points": len(outside_buffer_points),
                "buffer_best_rssi": buffer_best_rssi,
                "outside_buffer_best_rssi": outside_buffer_best_rssi,
                "buffer_vs_outside_delta": buffer_vs_outside_delta,
                "position_evidence": position_evidence,
                "position_confidence": position_confidence,
                "position_evidence_failure_reason": position_evidence_failure_reason,
                "estimated_position_in_buffer": bool(estimated_position_in_buffer),
                "strong_external_quadrants": dispersed_quadrants,
                "strong_external_points": strong_external_points,
                "dispersed_external": dispersed_external,
                "failed_reasons": failed_reasons_for_target,
            })

        if target_evaluations:
            target_eval = sorted(
                target_evaluations,
                key=lambda item: (
                    len(item["failed_reasons"]),
                    -item["buffer_hit_points"],
                    -item["real_target_hit_points"],
                    -(item["buffer_vs_outside_delta"] if item["buffer_vs_outside_delta"] is not None else -9999),
                ),
            )[0]
        else:
            target_eval = {
                "area_index": None,
                "target_range": None,
                "buffer_range": None,
                "pan_buffer_deg": 0.0,
                "tilt_buffer_deg": 0.0,
                "buffer_source": "none",
                "real_target_points": [],
                "buffer_points": [],
                "outside_buffer_points": bucket_values,
                "real_target_hit_points": 0,
                "buffer_hit_points": 0,
                "outside_buffer_hit_points": len(bucket_values),
                "buffer_best_rssi": None,
                "outside_buffer_best_rssi": best_point.get("rssi_avg"),
                "buffer_vs_outside_delta": None,
                "position_evidence": "outside_decision_buffer",
                "position_confidence": "none",
                "position_evidence_failure_reason": "no_valid_decision_range",
                "estimated_position_in_buffer": False,
                "strong_external_quadrants": [],
                "strong_external_points": [],
                "dispersed_external": False,
                "failed_reasons": ["目标区命中点数不足", "白名单缓冲区命中点数不足"],
            }

        work_points = target_eval["real_target_points"]
        other_points = target_eval["outside_buffer_points"]
        work_hit_points = target_eval["real_target_hit_points"]
        buffer_hit_points = target_eval["buffer_hit_points"]
        best_work_point = max(work_points, key=lambda b: b.get("rssi_avg", -9999)) if work_points else None
        work_best_rssi = best_work_point.get("rssi_avg") if best_work_point else None
        other_best_rssi = target_eval["outside_buffer_best_rssi"]
        work_vs_other_delta = target_eval["buffer_vs_outside_delta"]
        work_avg_rssi = (
            round(sum(b["rssi_avg"] for b in work_points) / len(work_points), 2)
            if work_points else None
        )
        other_avg_rssi = (
            round(sum(b["rssi_avg"] for b in other_points) / len(other_points), 2)
            if other_points else None
        )

        omni_deltas = []
        for item in work_points:
            omni_rssi = _numeric_or_none(item.get("omni_rssi_avg"))
            if omni_rssi is not None:
                omni_deltas.append({
                    "delta": round(item["rssi_avg"] - omni_rssi, 2),
                    "point": item,
                })
        best_omni_delta = max(omni_deltas, key=lambda item: item["delta"]) if omni_deltas else None
        directional_vs_omni_delta = best_omni_delta["delta"] if best_omni_delta else None
        estimated_position_in_work = target_eval["estimated_position_in_buffer"]
        position_evidence = target_eval["position_evidence"]
        position_confidence = target_eval["position_confidence"]
        position_evidence_failure_reason = target_eval["position_evidence_failure_reason"]

        failed_reasons = []
        if filter_enabled:
            failed_reasons.extend(target_eval.get("failed_reasons") or [])

            if directional_vs_omni_delta is not None and directional_vs_omni_delta < min_omni_delta:
                failed_reasons.append("定向相对全向强度差不足")

        accept_reasons = []
        if not failed_reasons:
            accept_reasons = [
                "真实目标区命中达标",
                "白名单缓冲区命中达标",
                "白名单缓冲区位置证据通过",
                "白名单缓冲区相对外部强度通过",
            ]
            if dispersed_external_check:
                accept_reasons.append("缓冲区外强点未分散")

        entry = {
            "mac": mac,
            "best_rssi": best_point.get("rssi_avg"),
            "best_position": best_point.get("position"),
            "best_pixel_position": best_point.get("pixel_position"),
            "best_point_id": best_point.get("point_id"),
            "best_point_in_work_area": bool(best_point.get("in_work_area")),
            "estimated_position": estimated_position,
            "estimated_position_in_work_area": bool(estimated_position_in_work),
            "position_evidence": position_evidence,
            "position_confidence": position_confidence,
            "accept_reasons": accept_reasons,
            "accept_reason_codes": [_reason_to_code(r) for r in accept_reasons],
            "first_seen_at": min((b.get("first_seen_at") for b in bucket_values if b.get("first_seen_at")), default=None),
            "last_seen_at": max((b.get("last_seen_at") for b in bucket_values if b.get("last_seen_at")), default=None),
            "work_hit_points": work_hit_points,
            "real_target_hit_points": work_hit_points,
            "buffer_hit_points": buffer_hit_points,
            "other_hit_points": len(other_points),
            "outside_buffer_hit_points": len(other_points),
            "total_hit_points": total_hit_points,
            "work_best_rssi": work_best_rssi,
            "buffer_best_rssi": target_eval.get("buffer_best_rssi"),
            "other_best_rssi": other_best_rssi,
            "outside_buffer_best_rssi": other_best_rssi,
            "work_avg_rssi": work_avg_rssi,
            "other_avg_rssi": other_avg_rssi,
            "work_vs_other_delta": work_vs_other_delta,
            "buffer_vs_outside_delta": work_vs_other_delta,
            "decision_area_index": target_eval.get("area_index"),
            "decision_buffer_range": target_eval.get("buffer_range"),
            "decision_buffer": {
                "pan_buffer_deg": target_eval.get("pan_buffer_deg"),
                "tilt_buffer_deg": target_eval.get("tilt_buffer_deg"),
                "source": target_eval.get("buffer_source"),
            },
            "strong_external_quadrants": target_eval.get("strong_external_quadrants"),
            "directional_vs_omni_delta": directional_vs_omni_delta,
            "channel": best_point.get("channel"),
            "bandwidth": best_point.get("bandwidth"),
            "channel_source": (
                (mac_decision_config.get(mac) or {}).get("source")
            ),
            "channel_confidence": (mac_identity.get(mac) or {}).get(
                "channel_confidence"
            ),
            "declared_bandwidth": (mac_identity.get(mac) or {}).get(
                "declared_bandwidth"
            ),
            "relationship_status": (mac_identity.get(mac) or {}).get(
                "relationship_status"
            ),
            "current_observed_ap": (mac_identity.get(mac) or {}).get(
                "current_observed_ap"
            ),
            "observed_aps": (mac_identity.get(mac) or {}).get("observed_aps"),
            "observed_best_config": (mac_identity.get(mac) or {}).get(
                "observed_best_config"
            ),
            "observed_configs": mac_observed_config_summaries.get(mac, []),
            "decision_config": mac_decision_config.get(mac),
            "evidence_counts": mac_evidence_counts.get(mac, {}),
            "type": best_point.get("type"),
            "subtype": best_point.get("subtype"),
            "ssid": best_point.get("ssid"),
            "filter_mode": mode,
            "round_dev_area_ratio": round(round_dev_area_ratio, 3),
        }

        if not failed_reasons:
            mac_whitelist.append(entry)
        else:
            reason_codes = [_reason_to_code(r) for r in failed_reasons]
            rejected_macs.append({
                "mac": mac,
                "reason_codes": reason_codes,
                "failed_reasons": failed_reasons,
                "metrics": {
                    "work_hit_count": work_hit_points,
                    "target_hit_count": work_hit_points,
                    "real_target_hit_count": work_hit_points,
                    "buffer_hit_count": buffer_hit_points,
                    "outside_buffer_hit_count": len(other_points),
                    "total_hit_count": total_hit_points,
                    "best_rssi": best_point.get("rssi_avg"),
                    "best_in_work": bool(best_point.get("in_work_area")),
                    "best_position": best_point.get("position"),
                    "estimated_position": estimated_position,
                    "estimated_position_in_work": bool(estimated_position_in_work),
                    "target_ranges": _full_scan_work_ranges_for_output(work_ranges),
                    "decision_area_index": target_eval.get("area_index"),
                    "decision_buffer_range": target_eval.get("buffer_range"),
                    "decision_buffer": {
                        "pan_buffer_deg": target_eval.get("pan_buffer_deg"),
                        "tilt_buffer_deg": target_eval.get("tilt_buffer_deg"),
                        "source": target_eval.get("buffer_source"),
                    },
                    "position_evidence": position_evidence,
                    "position_evidence_failure_reason": position_evidence_failure_reason,
                    "work_best_rssi": work_best_rssi,
                    "buffer_best_rssi": target_eval.get("buffer_best_rssi"),
                    "other_best_rssi": other_best_rssi,
                    "outside_buffer_best_rssi": other_best_rssi,
                    "work_vs_other_delta": work_vs_other_delta,
                    "buffer_vs_outside_delta": work_vs_other_delta,
                    "strong_external_quadrants": target_eval.get("strong_external_quadrants"),
                    "strong_external_points": [
                        {
                            "quadrant": item.get("quadrant"),
                            "delta_to_buffer_best": item.get("delta_to_buffer_best"),
                            "position": (item.get("point") or {}).get("position"),
                            "rssi_avg": (item.get("point") or {}).get("rssi_avg"),
                            "point_id": (item.get("point") or {}).get("point_id"),
                        }
                        for item in target_eval.get("strong_external_points", [])
                    ],
                    "directional_vs_omni_delta": directional_vs_omni_delta,
                    "observed_configs": mac_observed_config_summaries.get(
                        mac,
                        [],
                    ),
                    "decision_config": mac_decision_config.get(mac),
                    "evidence_counts": mac_evidence_counts.get(mac, {}),
                },
            })

    for mac, failure_reason in config_selection_failures.items():
        rejected_macs.append({
            "mac": mac,
            "reason_codes": [failure_reason],
            "failed_reasons": [failure_reason],
            "metrics": {
                "decision_config": None,
                "evidence_counts": mac_evidence_counts.get(mac, {}),
                "observed_configs": mac_observed_config_summaries.get(mac, []),
            },
        })

    mac_whitelist.sort(key=lambda item: item.get("best_rssi") if item.get("best_rssi") is not None else -9999, reverse=True)

    # ── 日志：白名单筛选结果 ──────────────────────────────────────
    for item in mac_whitelist:
        logger.info(
            f"[全扫白名单] ACCEPT mac={item['mac']} "
            f"best_rssi={item.get('best_rssi')} "
            f"work_hits={item.get('work_hit_points')} "
            f"total_hits={item.get('total_hit_points')} "
            f"ch={item.get('channel')} bw={item.get('bandwidth')}"
        )
    for item in rejected_macs:
        metrics = item.get("metrics") or {}
        logger.info(
            f"[全扫白名单] REJECT mac={item['mac']} "
            f"work_hits={metrics.get('work_hit_count')} "
            f"total_hits={metrics.get('total_hit_count')} "
            f"best_in_work={metrics.get('best_in_work')} "
            f"reasons={';'.join(item.get('failed_reasons') or [])}"
        )

    # ── 构建返回值 ────────────────────────────────────────────────
    output_rejected = bool(filter_config.get("输出淘汰明细", True))

    return {
        "round_index": round_payload.get("round_index"),
        "latest_round": round_payload.get("latest_round"),
        "round_id": round_payload.get("round_id"),
        "status": round_payload.get("round_status"),
        "stop_reason": round_payload.get("stop_reason"),
        "round_started_at": round_payload.get("round_started_at"),
        "round_finished_at": round_payload.get("round_finished_at"),
        "source_results_key": f"full_scan:round_{round_payload.get('round_index')}_results",
        "filter_enabled": filter_enabled,
        "filter_config": _full_scan_filter_config_for_output(filter_config),
        "filter_mode": mode,
        "round_dev_area_ratio": round(round_dev_area_ratio, 3),
        "work_ranges": work_ranges,
        "decision_buffers": [
            {
                "area_index": item.get("area_index"),
                "target_range": item.get("target_range"),
                "buffer_range": item.get("buffer_range"),
                "pan_buffer_deg": item.get("pan_buffer_deg"),
                "tilt_buffer_deg": item.get("tilt_buffer_deg"),
                "buffer_source": item.get("buffer_source"),
            }
            for item in decision_ranges
        ],
        "mac_whitelist": mac_whitelist,
        "mac_count": len(mac_whitelist),
        "rejected_macs": rejected_macs if output_rejected else [],
        "rejected_mac_count": len(rejected_macs),
        "candidate_mac_count": len(mac_config_buckets),
    }


def _write_full_scan_whitelist_result(r, whitelist_payload):
    round_index = whitelist_payload.get("round_index")
    if round_index is None:
        return None

    whitelist_key = f"full_scan:whitelist:round_{round_index}"
    r.set(whitelist_key, json.dumps(whitelist_payload, ensure_ascii=False))
    r.set("full_scan:whitelist:latest_success", json.dumps(whitelist_payload, ensure_ascii=False))
    r.set("full_scan:whitelist:latest_round", str(round_index))

    try:
        raw_rounds = r.get("full_scan:whitelist:rounds")
        rounds = json.loads(raw_rounds) if raw_rounds else []
        if not isinstance(rounds, list):
            rounds = []
    except Exception:
        rounds = []

    if not any(item.get("round_index") == round_index for item in rounds if isinstance(item, dict)):
        rounds.append({
            "round_index": round_index,
            "round_id": whitelist_payload.get("round_id"),
            "mac_count": whitelist_payload.get("mac_count", 0),
            "created_at": local_now_iso(),
        })
    else:
        for item in rounds:
            if isinstance(item, dict) and item.get("round_index") == round_index:
                item["mac_count"] = whitelist_payload.get("mac_count", 0)
                item["round_id"] = whitelist_payload.get("round_id")
                item["updated_at"] = local_now_iso()
    rounds.sort(key=lambda item: item.get("round_index", 0) if isinstance(item, dict) else 0)
    r.set("full_scan:whitelist:rounds", json.dumps(rounds))
    return whitelist_key


def _full_scan_refinement_command(
    r,
    scan_id,
    action,
    *,
    timeout=1.0,
    stop_check_fn=None,
    **payload,
):
    notify_key = f"full_scan:refinement:{scan_id}:{action}:{time.time_ns()}"
    command = {
        "action": action,
        "scan_id": scan_id,
        "notify_key": notify_key,
        **payload,
    }
    r.lpush("capture:command_queue", json.dumps(command, ensure_ascii=False))
    deadline = time.monotonic() + max(0.05, float(timeout))
    while time.monotonic() < deadline:
        if stop_check_fn and stop_check_fn():
            return {"status": "stopped", "reason": "manual_stop"}
        raw = r.rpop(notify_key)
        if raw is not None:
            r.delete(notify_key)
            try:
                return json.loads(raw)
            except Exception:
                return {"status": "error", "reason": "invalid_notify"}
        time.sleep(0.02)
    r.delete(notify_key)
    return {"status": "timeout", "reason": "notify_timeout"}


def _full_scan_refinement_convert_pixel(scan_mode, image_context, x, y):
    if scan_mode == "panorama":
        return _pixel_to_angle_panorama(
            x,
            y,
            image_context.get("pmap_path"),
            image_context.get("session_json"),
            return_detail=True,
        )
    return _pixel_to_angle_single(
        x,
        y,
        image_context,
        return_detail=True,
    )


def _full_scan_refinement_peak_angle(
    trajectory,
    peak_packet_time,
    start_angle,
    end_angle,
):
    """Locate one peak packet on a completed PTZ leg using timestamped poses."""
    try:
        peak_ts = float(peak_packet_time)
    except (TypeError, ValueError):
        return {"success": False, "reason": "peak_packet_time_missing"}

    samples = []
    for item in trajectory or []:
        if not isinstance(item, dict):
            continue
        try:
            ts = float(item["ts"])
            pan = float(item["pan"])
            tilt = float(item["tilt"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (ts, pan, tilt)):
            continue
        samples.append({"ts": ts, "pan": pan, "tilt": tilt})
    samples.sort(key=lambda item: item["ts"])
    if not samples:
        return {"success": False, "reason": "trajectory_missing"}

    def endpoint_result(angle, method):
        return {
            "success": True,
            "method": method,
            "pan": float(_normalize_pan(angle["pan"])),
            "tilt": float(angle["tilt"]),
            "trajectory_sample_count": len(samples),
        }

    if peak_ts <= samples[0]["ts"]:
        return endpoint_result(start_angle, "clamped_start")
    if peak_ts >= samples[-1]["ts"]:
        return endpoint_result(end_angle, "clamped_end")
    if len(samples) == 1:
        return endpoint_result(
            start_angle if peak_ts <= samples[0]["ts"] else end_angle,
            "single_sample_endpoint",
        )

    for index in range(len(samples) - 1):
        left = samples[index]
        right = samples[index + 1]
        if not left["ts"] <= peak_ts <= right["ts"]:
            continue
        duration = right["ts"] - left["ts"]
        if duration <= 0:
            ratio = 0.0
        else:
            ratio = min(1.0, max(0.0, (peak_ts - left["ts"]) / duration))
        pan_delta = _shortest_angular_diff_deg(left["pan"], right["pan"])
        return {
            "success": True,
            "method": "trajectory_interpolation",
            "pan": float(_normalize_pan(left["pan"] + pan_delta * ratio)),
            "tilt": float(
                left["tilt"] + (right["tilt"] - left["tilt"]) * ratio
            ),
            "trajectory_sample_count": len(samples),
            "left_ts": left["ts"],
            "right_ts": right["ts"],
            "ratio": ratio,
        }
    return {"success": False, "reason": "trajectory_interval_missing"}


def _full_scan_refinement_line_pixel_lookup(
    start_pixel,
    end_pixel,
    target_angle,
    convert_fn,
    *,
    coarse_step_px=64.0,
    max_coarse_segments=64,
    refinement_rounds=4,
    refinement_samples=9,
):
    """
    Find the original segment pixel whose forward-mapped angle is closest.

    The search remains on the source pixel segment and skips mapping holes.
    It performs a full sparse pass first, then refines only near the best span.
    """
    try:
        x0 = float(start_pixel["x"])
        y0 = float(start_pixel["y"])
        x1 = float(end_pixel["x"])
        y1 = float(end_pixel["y"])
        target_pan = float(target_angle["pan"])
        target_tilt = float(target_angle["tilt"])
    except (KeyError, TypeError, ValueError):
        return {"success": False, "reason": "invalid_segment_lookup_input"}
    values = (x0, y0, x1, y1, target_pan, target_tilt)
    if not all(math.isfinite(value) for value in values):
        return {"success": False, "reason": "non_finite_segment_lookup_input"}

    length_px = math.hypot(x1 - x0, y1 - y0)
    coarse_segments = max(
        1,
        min(
            int(max_coarse_segments),
            int(math.ceil(length_px / max(1.0, float(coarse_step_px)))),
        ),
    )
    refinement_samples = max(3, int(refinement_samples))
    cache = {}

    def evaluate(ratio):
        ratio = min(1.0, max(0.0, float(ratio)))
        key = round(ratio, 12)
        if key in cache:
            return cache[key]
        x = x0 + (x1 - x0) * ratio
        y = y0 + (y1 - y0) * ratio
        converted = convert_fn(x, y)
        if not isinstance(converted, dict) or not converted.get("success"):
            cache[key] = None
            return None
        try:
            mapped_pan = float(converted["pan"])
            mapped_tilt = float(converted["tilt"])
        except (KeyError, TypeError, ValueError):
            cache[key] = None
            return None
        distance = math.hypot(
            float(_shortest_angular_diff_deg(mapped_pan, target_pan)),
            mapped_tilt - target_tilt,
        )
        candidate = {
            "ratio": ratio,
            "pixel_position": {
                "x": x,
                "y": y,
            },
            "mapped_angle": {
                "pan": mapped_pan,
                "tilt": mapped_tilt,
            },
            "_distance": distance,
        }
        cache[key] = candidate
        return candidate

    coarse_ratios = [
        index / coarse_segments
        for index in range(coarse_segments + 1)
    ]
    valid = [
        candidate
        for candidate in (evaluate(ratio) for ratio in coarse_ratios)
        if candidate is not None
    ]
    if not valid:
        return {"success": False, "reason": "no_valid_segment_pixel"}
    best = min(valid, key=lambda item: item["_distance"])
    coarse_step = 1.0 / coarse_segments
    left = max(0.0, best["ratio"] - coarse_step)
    right = min(1.0, best["ratio"] + coarse_step)

    for _round in range(max(0, int(refinement_rounds))):
        if right <= left:
            break
        step = (right - left) / (refinement_samples - 1)
        local = [
            candidate
            for candidate in (
                evaluate(left + step * index)
                for index in range(refinement_samples)
            )
            if candidate is not None
        ]
        if local:
            local_best = min(local, key=lambda item: item["_distance"])
            if local_best["_distance"] < best["_distance"]:
                best = local_best
        if length_px * (right - left) <= 1.0:
            break
        next_half_width = max(step, 0.5 / max(1.0, length_px))
        left = max(0.0, best["ratio"] - next_half_width)
        right = min(1.0, best["ratio"] + next_half_width)

    return {
        "success": True,
        "pixel_position": best["pixel_position"],
        "mapped_angle": best["mapped_angle"],
        "ratio": best["ratio"],
        "coarse_segment_count": coarse_segments,
        "evaluated_pixel_count": len(cache),
    }


def _full_scan_refinement_safe_delta(value):
    value = abs(float(value))
    return value <= FULL_SCAN_DIRECT_MOVE_TOLERANCE + 1e-9 or (
        value >= FULL_SCAN_DIRECT_MOVE_RELIABLE_DELTA - 1e-9
    )


def _full_scan_refinement_route(corners, current_pose=None):
    """Find the cheapest open trail covering rectangle sides and diagonals."""
    if isinstance(current_pose, dict):
        current_pose = (
            current_pose.get("pan"),
            current_pose.get("tilt"),
        )
    elif isinstance(current_pose, (list, tuple)):
        current_pose = (
            current_pose[0] if len(current_pose) > 0 else None,
            current_pose[1] if len(current_pose) > 1 else None,
        )
    else:
        current_pose = (None, None)
    required = (
        ("A", "B"),
        ("B", "C"),
        ("C", "D"),
        ("D", "A"),
        ("A", "C"),
        ("B", "D"),
    )

    def cost(left, right):
        lp = corners[left]["angle"]
        rp = corners[right]["angle"]
        return math.hypot(
            float(_shortest_angular_diff_deg(lp["pan"], rp["pan"])),
            float(lp["tilt"]) - float(rp["tilt"]),
        )

    best = None
    for repeated in required:
        edges = list(required) + [repeated]
        for start in corners:
            def walk(vertex, remaining, path):
                nonlocal best
                if not remaining:
                    route_cost = sum(
                        cost(path[index], path[index + 1])
                        for index in range(len(path) - 1)
                    )
                    if current_pose and current_pose[0] is not None:
                        start_angle = corners[path[0]]["angle"]
                        route_cost += math.hypot(
                            float(_shortest_angular_diff_deg(
                                current_pose[0],
                                start_angle["pan"],
                            )),
                            float(current_pose[1]) - float(start_angle["tilt"]),
                        )
                    candidate = (route_cost, list(path), repeated)
                    if best is None or candidate[0] < best[0]:
                        best = candidate
                    return
                for index, edge in enumerate(remaining):
                    if vertex not in edge:
                        continue
                    next_vertex = edge[1] if edge[0] == vertex else edge[0]
                    walk(
                        next_vertex,
                        remaining[:index] + remaining[index + 1:],
                        path + [next_vertex],
                    )
            walk(start, edges, [start])
    if best is None:
        return None
    _cost, vertices, repeated = best
    return {
        "vertices": vertices,
        "repeated_edge": list(repeated),
        "segments": [
            {
                "from": vertices[index],
                "to": vertices[index + 1],
                "pixel_midpoint": {
                    "x": round(
                        (
                            corners[vertices[index]]["pixel"]["x"]
                            + corners[vertices[index + 1]]["pixel"]["x"]
                        ) / 2,
                        2,
                    ),
                    "y": round(
                        (
                            corners[vertices[index]]["pixel"]["y"]
                            + corners[vertices[index + 1]]["pixel"]["y"]
                        ) / 2,
                        2,
                    ),
                },
            }
            for index in range(len(vertices) - 1)
        ],
    }


def _build_full_scan_refinement_plan(
    whitelist_payload,
    round_results,
    scan_mode,
    image_context,
    current_pose=None,
    target_macs=None,
    target_ranges=None,
):
    target_mac_set = {
        str(mac).lower()
        for mac in (target_macs or [])
        if str(mac).strip()
    }
    def _pixel_in_target_ranges(px, py, ranges):
        if not ranges:
            return True
        for r in ranges:
            xr = r.get("x_range") or []
            yr = r.get("y_range") or []
            if len(xr) >= 2 and len(yr) >= 2:
                if min(xr) <= px <= max(xr) and min(yr) <= py <= max(yr):
                    return True
        return False

    fine_points = [
        point
        for point in (round_results or {}).values()
        if (
            _is_qualified_full_scan_fixed_evidence(point)
            and point.get("phase") == "fine"
            and isinstance(point.get("pixel_position"), dict)
            and _pixel_in_target_ranges(
                float(point["pixel_position"]["x"]),
                float(point["pixel_position"]["y"]),
                target_ranges,
            )
        )
    ]
    support_pixels = {
        (
            float(point["pixel_position"]["x"]),
            float(point["pixel_position"]["y"]),
        )
        for point in fine_points
    }
    plans = []
    skipped = []
    for item in (whitelist_payload or {}).get("mac_whitelist") or []:
        mac = str(item.get("mac") or "").lower()
        if target_mac_set and mac not in target_mac_set:
            continue
        channel = item.get("channel")
        bandwidth = str(item.get("bandwidth") or "HT20")
        strong = []
        for point in fine_points:
            mac_data = next(
                (
                    value
                    for key, value in (point.get("macs") or {}).items()
                    if str(key).lower() == mac
                ),
                None,
            )
            if not isinstance(mac_data, dict):
                continue
            matching = [
                obs
                for obs in _full_scan_actual_observations(mac_data)
                if (
                    obs.get("channel") is not None
                    and int(obs["channel"]) == int(channel)
                    and str(obs.get("bandwidth") or "HT20") == bandwidth
                    and _numeric_or_none(obs.get("rssi_avg")) is not None
                )
            ]
            if not matching:
                continue
            observation = max(
                matching,
                key=lambda obs: _numeric_or_none(obs.get("rssi_avg")),
            )
            strong.append({
                "pixel": (
                    float(point["pixel_position"]["x"]),
                    float(point["pixel_position"]["y"]),
                ),
                "rssi": _numeric_or_none(observation.get("rssi_avg")),
            })
        strong.sort(key=lambda value: value["rssi"], reverse=True)
        base_pixels = []
        for value in strong:
            if value["pixel"] not in base_pixels:
                base_pixels.append(value["pixel"])
            if len(base_pixels) >= 4:
                break
        if not base_pixels:
            skipped.append({"mac": mac, "reason": "no_fine_fixed_evidence"})
            continue

        additions = sorted(
            pixel for pixel in support_pixels if pixel not in base_pixels
        )
        candidates = [tuple()]
        candidates.extend((pixel,) for pixel in additions)
        candidates.extend(itertools.combinations(additions, 2))
        valid_rectangles = []
        closest_unsafe = None
        for extra in candidates:
            pixels = base_pixels + list(extra)
            x_min = min(pixel[0] for pixel in pixels)
            x_max = max(pixel[0] for pixel in pixels)
            y_min = min(pixel[1] for pixel in pixels)
            y_max = max(pixel[1] for pixel in pixels)
            if x_max <= x_min or y_max <= y_min:
                continue
            raw_corners = {
                "A": (x_min, y_min),
                "B": (x_max, y_min),
                "C": (x_max, y_max),
                "D": (x_min, y_max),
            }
            corners = {}
            conversion_failed = False
            conversion_failure = None
            for name, pixel in raw_corners.items():
                converted = _full_scan_refinement_convert_pixel(
                    scan_mode,
                    image_context,
                    pixel[0],
                    pixel[1],
                )
                if not converted.get("success"):
                    conversion_failed = True
                    conversion_failure = {
                        "corner": name,
                        "pixel": {"x": pixel[0], "y": pixel[1]},
                        "reason": converted.get("reason"),
                    }
                    break
                corners[name] = {
                    "pixel": {"x": pixel[0], "y": pixel[1]},
                    "angle": {
                        "pan": float(converted["pan"]),
                        "tilt": float(converted["tilt"]),
                    },
                }
            if conversion_failed:
                if closest_unsafe is None:
                    closest_unsafe = {
                        "failure_type": "pixel_to_angle_failed",
                        "detail": conversion_failure,
                    }
                continue
            unsafe_segments = []
            for left, right in (
                ("A", "B"), ("B", "C"), ("C", "D"),
                ("D", "A"), ("A", "C"), ("B", "D"),
            ):
                left_angle = corners[left]["angle"]
                right_angle = corners[right]["angle"]
                pan_delta = abs(
                    float(_shortest_angular_diff_deg(
                        left_angle["pan"],
                        right_angle["pan"],
                    ))
                )
                tilt_delta = abs(
                    float(right_angle["tilt"]) - float(left_angle["tilt"])
                )
                unsafe_axes = []
                if not _full_scan_refinement_safe_delta(pan_delta):
                    unsafe_axes.append("pan")
                if not _full_scan_refinement_safe_delta(tilt_delta):
                    unsafe_axes.append("tilt")
                if unsafe_axes:
                    unsafe_segments.append({
                        "edge": f"{left}{right}",
                        "pan_delta": round(pan_delta, 4),
                        "tilt_delta": round(tilt_delta, 4),
                        "unsafe_axes": unsafe_axes,
                    })
            if not unsafe_segments:
                valid_rectangles.append(
                    ((x_max - x_min) * (y_max - y_min), corners)
                )
            else:
                unsafe_score = (
                    sum(
                        len(item["unsafe_axes"])
                        for item in unsafe_segments
                    ),
                    len(unsafe_segments),
                    (x_max - x_min) * (y_max - y_min),
                )
                candidate_detail = {
                    "failure_type": "unreliable_axis_delta",
                    "pixel_bounds": {
                        "x": [x_min, x_max],
                        "y": [y_min, y_max],
                    },
                    "unsafe_segments": unsafe_segments,
                    "_score": unsafe_score,
                }
                if (
                    closest_unsafe is None
                    or unsafe_score < closest_unsafe.get(
                        "_score",
                        (9999, 9999, float("inf")),
                    )
                ):
                    closest_unsafe = candidate_detail
        if not valid_rectangles:
            if isinstance(closest_unsafe, dict):
                closest_unsafe.pop("_score", None)
            skipped.append({
                "mac": mac,
                "reason": "no_safe_rectangle",
                "detail": closest_unsafe,
            })
            continue
        _area, corners = min(valid_rectangles, key=lambda value: value[0])
        route = _full_scan_refinement_route(corners, current_pose=current_pose)
        if not route:
            skipped.append({"mac": mac, "reason": "route_unavailable"})
            continue
        plans.append({
            "mac": mac,
            "channel": int(channel),
            "bandwidth": bandwidth,
            "original_best_rssi": _numeric_or_none(item.get("best_rssi")),
            "corners": corners,
            "route": route,
            "whitelist_entry": item,
        })
    return plans, skipped


def _run_full_scan_whitelist_refinement(
    *,
    r,
    ptz,
    scan_id,
    whitelist_payload,
    round_results,
    scan_mode,
    image_context,
    move_range,
    stop_check_fn,
    target_macs=None,
    target_ranges=None,
):
    current_pose = ptz.get_position()
    plans, skipped = _build_full_scan_refinement_plan(
        whitelist_payload,
        round_results,
        scan_mode,
        image_context,
        current_pose=current_pose,
        target_macs=target_macs,
        target_ranges=target_ranges,
    )
    summary = {
        "status": "running",
        "planned_mac_count": len(plans),
        "completed_mac_count": 0,
        "updated_mac_count": 0,
        "skipped": skipped,
    }
    entries_by_mac = {
        str(item.get("mac") or "").lower(): item
        for item in (whitelist_payload or {}).get("mac_whitelist") or []
        if isinstance(item, dict)
    }

    def mark_entry_failure(mac, reason, detail=None, status="skipped"):
        entry = entries_by_mac.get(str(mac).lower())
        if not entry:
            return
        entry.setdefault("initial_best_position", entry.get("best_position"))
        entry.setdefault(
            "initial_best_pixel_position",
            entry.get("best_pixel_position"),
        )
        entry.setdefault("initial_best_rssi", entry.get("best_rssi"))
        entry.setdefault("best_position_source", "initial_fixed_point")
        entry["refinement"] = {
            "status": status,
            "reason": reason,
            "detail": detail,
        }

    for skipped_item in skipped:
        mark_entry_failure(
            skipped_item.get("mac"),
            skipped_item.get("reason"),
            skipped_item.get("detail"),
        )
    patch_ptz_status(r, {
        "full_scan": {
            "active": True,
            "state": "running",
            "terminal": False,
            "stop_requested": False,
            "phase": "whitelist_refinement",
            "refinement": summary,
        }
    })
    grouped = {}
    for plan in plans:
        grouped.setdefault(
            (plan["channel"], plan["bandwidth"]),
            [],
        ).append(plan)
    try:
        for (channel, bandwidth), config_plans in grouped.items():
            start = _full_scan_refinement_command(
                r,
                scan_id,
                "start_refinement_session",
                timeout=FULL_SCAN_REFINEMENT_SESSION_START_TIMEOUT,
                stop_check_fn=stop_check_fn,
                session_id=f"refine_{scan_id}_{channel}_{bandwidth}",
                channel=channel,
                bandwidth=bandwidth,
                target_macs=[plan["mac"] for plan in config_plans],
            )
            if start.get("status") != "started":
                failure_detail = {
                    "status": start.get("status"),
                    "reason": start.get("reason"),
                    "channel": channel,
                    "bandwidth": bandwidth,
                }
                if start.get("active_scan_id") is not None:
                    failure_detail["active_scan_id"] = start.get(
                        "active_scan_id"
                    )
                logger.warning(
                    "⚠️ 白名单复核抓包会话启动失败: "
                    f"scan_id={scan_id}, detail={failure_detail}"
                )
                for plan in config_plans:
                    skipped_item = {
                        "mac": plan["mac"],
                        "reason": "capture_session_start_failed",
                        "detail": failure_detail,
                    }
                    summary["skipped"].append(skipped_item)
                    mark_entry_failure(
                        plan["mac"],
                        skipped_item["reason"],
                        failure_detail,
                        status="failed",
                    )
                continue
            try:
                for plan_index, plan in enumerate(config_plans):
                    if stop_check_fn():
                        summary["status"] = "stopped"
                        return summary
                    start_name = plan["route"]["vertices"][0]
                    start_angle = plan["corners"][start_name]["angle"]
                    approach_segment = _full_scan_refinement_command(
                        r,
                        scan_id,
                        "set_refinement_segment",
                        timeout=0.5,
                        stop_check_fn=stop_check_fn,
                        segment_id=None,
                    )
                    if approach_segment.get("status") != "ready":
                        failure_detail = {
                            "step": "clear_segment_before_approach",
                            "response": approach_segment,
                        }
                        skipped_item = {
                            "mac": plan["mac"],
                            "reason": "approach_segment_clear_failed",
                            "detail": failure_detail,
                        }
                        summary["skipped"].append(skipped_item)
                        mark_entry_failure(
                            plan["mac"],
                            skipped_item["reason"],
                            failure_detail,
                            status="failed",
                        )
                        continue
                    approach = _full_scan_direct_move_with_relay(
                        ptz,
                        start_angle["pan"],
                        start_angle["tilt"],
                        move_range,
                        stop_check_fn=stop_check_fn,
                    )
                    if not approach.get("success"):
                        failure_detail = {
                            "step": "approach_move",
                            "move": approach,
                        }
                        skipped_item = {
                            "mac": plan["mac"],
                            "reason": "approach_failed",
                            "detail": failure_detail,
                        }
                        summary["skipped"].append(skipped_item)
                        mark_entry_failure(
                            plan["mac"],
                            skipped_item["reason"],
                            failure_detail,
                            status="failed",
                        )
                        continue
                    candidates = []
                    complete = True
                    incomplete_detail = None
                    for segment_index, segment in enumerate(
                        plan["route"]["segments"]
                    ):
                        segment_id = (
                            f"{plan['mac']}:{plan_index}:{segment_index}:"
                            f"{segment['from']}{segment['to']}"
                        )
                        ready = _full_scan_refinement_command(
                            r,
                            scan_id,
                            "set_refinement_segment",
                            timeout=0.5,
                            stop_check_fn=stop_check_fn,
                            segment_id=segment_id,
                        )
                        if ready.get("status") != "ready":
                            complete = False
                            incomplete_detail = {
                                "step": "segment_prepare",
                                "segment_id": segment_id,
                                "response": ready,
                            }
                            break
                        target = plan["corners"][segment["to"]]["angle"]
                        moved = _run_full_scan_direct_leg(
                            ptz,
                            target["pan"],
                            target["tilt"],
                            stop_check_fn=stop_check_fn,
                        )
                        finished = _full_scan_refinement_command(
                            r,
                            scan_id,
                            "finish_refinement_segment",
                            timeout=0.5,
                            stop_check_fn=stop_check_fn,
                            segment_id=segment_id,
                        )
                        if not moved.get("success") or finished.get("status") != "done":
                            complete = False
                            incomplete_detail = {
                                "step": (
                                    "segment_move"
                                    if not moved.get("success")
                                    else "segment_finish"
                                ),
                                "segment_id": segment_id,
                                "move": moved,
                                "finish": finished,
                            }
                            break
                        stats = (finished.get("macs") or {}).get(plan["mac"]) or {}
                        if int(stats.get("rssi_samples") or 0) >= 2:
                            from_corner = plan["corners"][segment["from"]]
                            to_corner = plan["corners"][segment["to"]]
                            trajectory = [
                                item
                                for item in (moved.get("trajectory") or [])
                                if isinstance(item, dict)
                            ]
                            trajectory_times = sorted(
                                float(item["ts"])
                                for item in trajectory
                                if item.get("ts") is not None
                            )
                            trajectory_gaps_ms = [
                                (
                                    trajectory_times[index + 1]
                                    - trajectory_times[index]
                                )
                                * 1000.0
                                for index in range(len(trajectory_times) - 1)
                            ]
                            query_durations_ms = [
                                float(item["query_duration_ms"])
                                for item in trajectory
                                if item.get("query_duration_ms") is not None
                            ]
                            peak_packet_time = _numeric_or_none(
                                stats.get("peak_packet_time")
                            )
                            peak_callback_time = _numeric_or_none(
                                stats.get("peak_callback_time")
                            )
                            peak_angle = _full_scan_refinement_peak_angle(
                                trajectory,
                                peak_packet_time,
                                from_corner["angle"],
                                to_corner["angle"],
                            )
                            candidates.append({
                                "segment_id": segment_id,
                                "from": segment["from"],
                                "to": segment["to"],
                                "start_pixel": dict(from_corner["pixel"]),
                                "end_pixel": dict(to_corner["pixel"]),
                                "start_angle": dict(from_corner["angle"]),
                                "end_angle": dict(to_corner["angle"]),
                                "rssi_avg": _numeric_or_none(stats.get("rssi_avg")),
                                "rssi_samples": int(stats.get("rssi_samples") or 0),
                                "peak_packet_time": peak_packet_time,
                                "peak_callback_time": peak_callback_time,
                                "peak_callback_delay_ms": (
                                    round(
                                        (
                                            peak_callback_time
                                            - peak_packet_time
                                        )
                                        * 1000.0,
                                        3,
                                    )
                                    if (
                                        peak_packet_time is not None
                                        and peak_callback_time is not None
                                    )
                                    else None
                                ),
                                "peak_angle": peak_angle,
                                "trajectory_sample_count": len(trajectory),
                                "trajectory_max_gap_ms": (
                                    round(max(trajectory_gaps_ms), 3)
                                    if trajectory_gaps_ms
                                    else None
                                ),
                                "trajectory_max_query_duration_ms": (
                                    round(max(query_durations_ms), 3)
                                    if query_durations_ms
                                    else None
                                ),
                            })
                    if not complete:
                        if stop_check_fn():
                            summary["status"] = "stopped"
                            return summary
                        skipped_item = {
                            "mac": plan["mac"],
                            "reason": "rectangle_incomplete",
                            "detail": incomplete_detail,
                        }
                        summary["skipped"].append(skipped_item)
                        mark_entry_failure(
                            plan["mac"],
                            skipped_item["reason"],
                            incomplete_detail,
                            status="failed",
                        )
                        _write_full_scan_whitelist_result(
                            r,
                            whitelist_payload,
                        )
                        continue
                    summary["completed_mac_count"] += 1
                    best = max(
                        (
                            candidate for candidate in candidates
                            if candidate.get("rssi_avg") is not None
                        ),
                        key=lambda candidate: candidate["rssi_avg"],
                        default=None,
                    )
                    entry = plan["whitelist_entry"]
                    entry.setdefault("initial_best_position", entry.get("best_position"))
                    entry.setdefault(
                        "initial_best_pixel_position",
                        entry.get("best_pixel_position"),
                    )
                    entry.setdefault("initial_best_rssi", entry.get("best_rssi"))
                    entry.setdefault("best_position_source", "initial_fixed_point")
                    improvement = (
                        best["rssi_avg"] - plan["original_best_rssi"]
                        if best and plan["original_best_rssi"] is not None
                        else None
                    )
                    peak_angle = best.get("peak_angle") if best else None
                    pixel_lookup = (
                        _full_scan_refinement_line_pixel_lookup(
                            best.get("start_pixel"),
                            best.get("end_pixel"),
                            peak_angle,
                            lambda x, y: _full_scan_refinement_convert_pixel(
                                scan_mode,
                                image_context,
                                x,
                                y,
                            ),
                        )
                        if peak_angle and peak_angle.get("success")
                        else None
                    )
                    updated = bool(
                        improvement is not None
                        and improvement >= 1.0
                        and pixel_lookup
                        and pixel_lookup.get("success")
                    )
                    if updated:
                        positioning_reason = None
                    elif not best:
                        positioning_reason = "no_eligible_segment"
                    elif improvement is None:
                        positioning_reason = "improvement_unavailable"
                    elif improvement < 1.0:
                        positioning_reason = "insufficient_improvement"
                    elif not peak_angle or not peak_angle.get("success"):
                        positioning_reason = (
                            (peak_angle or {}).get("reason")
                            or "peak_angle_unavailable"
                        )
                    else:
                        positioning_reason = (
                            (pixel_lookup or {}).get("reason")
                            or "segment_pixel_lookup_failed"
                        )
                    if updated:
                        entry["best_position"] = {
                            "pan": round(
                                float(pixel_lookup["mapped_angle"]["pan"]),
                                2,
                            ),
                            "tilt": round(
                                float(pixel_lookup["mapped_angle"]["tilt"]),
                                2,
                            ),
                        }
                        entry["best_pixel_position"] = dict(
                            pixel_lookup["pixel_position"]
                        )
                        entry["best_rssi"] = best["rssi_avg"]
                        entry["best_position_source"] = (
                            "whitelist_refinement_peak_trajectory"
                        )
                        summary["updated_mac_count"] += 1
                    winner = None
                    if best:
                        winner = dict(best)
                        winner["pixel_lookup"] = pixel_lookup
                        if updated:
                            winner["pixel_position"] = dict(
                                pixel_lookup["pixel_position"]
                            )
                    entry["refinement"] = {
                        "status": "updated" if updated else "kept_initial",
                        "reason": positioning_reason,
                        "corners": plan["corners"],
                        "route": plan["route"],
                        "winner": winner,
                        "improvement_db": (
                            round(improvement, 2)
                            if improvement is not None
                            else None
                        ),
                    }
                    summary["refinement"] = {
                        "current_mac": plan["mac"],
                        "current_index": summary["completed_mac_count"],
                        "total": len(plans),
                    }
                    _write_full_scan_whitelist_result(r, whitelist_payload)
                    patch_ptz_status(r, {
                        "full_scan": {
                            "active": True,
                            "state": "running",
                            "terminal": False,
                            "phase": "whitelist_refinement",
                            "refinement": summary,
                        }
                    })
            finally:
                _full_scan_refinement_command(
                    r,
                    scan_id,
                    "stop_refinement_session",
                    timeout=1.0,
                    stop_check_fn=None,
                    timeout_seconds=0.8,
                )
    finally:
        if summary["status"] == "running":
            summary["status"] = "completed"
        whitelist_payload["refinement_summary"] = summary
        _write_full_scan_whitelist_result(r, whitelist_payload)
    return summary


def _clear_full_scan_runtime_keys(r, logger=None):
    """开始新全面扫描任务前清理上一任务的 Redis 运行态结果。"""
    base_keys = [
        "full_scan:results",
        "full_scan:latest_round",
        "full_scan:whitelist:latest_success",
        "full_scan:whitelist:latest_round",
        "full_scan:whitelist:rounds",
    ]
    patterns = [
        "full_scan:round_*_results",
        "full_scan:whitelist:round_*",
    ]

    try:
        for pattern in patterns:
            for key in r.scan_iter(match=pattern):
                r.delete(key)
        r.delete(*base_keys)
        if logger:
            logger.info("🧹 已清理上一轮全面扫描 Redis 运行态结果")
    except Exception as e:
        if logger:
            logger.warning(f"⚠️ 清理全面扫描运行态结果失败: {e}")


# ==================== 智能扫描核心函数 ====================

# ==================== 多点扫描辅助函数 ====================

def wait_for_point_completion(r, point_id, timeout=1800, logger=None):
    """
    等待点位扫描完成（使用BRPOP阻塞等待）
    
    Args:
        r: Redis连接对象
        point_id: 点位ID（如 "point_1"）
        timeout: 超时时间（秒），默认1800秒（30分钟）
        logger: 日志对象
    
    Returns:
        dict: 成功返回通知数据，超时/失败返回None
    
    说明：
        使用Redis的BRPOP机制阻塞等待capture_worker完成点位扫描
        比轮询方式高效，不占用CPU资源
        当MAC数量较多时（如>100个），单个点位可能需要15-20分钟，
        因此设置较长的超时时间（30分钟）以避免误判
    """
    notify_key = f'multi_scan:{point_id}_notify'
    
    if logger:
        logger.info(f"⏳ 等待点位扫描完成: {point_id} (超时{timeout}秒)")
    
    try:
        # BRPOP阻塞等待，返回 (key, value) 或 None
        result = r.brpop(notify_key, timeout=timeout)
        
        if result:
            _, notify_json = result
            notify_data = json.loads(notify_json)
            if logger:
                logger.info(f"✅ 点位扫描完成: {point_id}")
            return notify_data
        else:
            if logger:
                logger.error(f"❌ 点位扫描超时: {point_id}")
            return None
    
    except redis.exceptions.ConnectionError as e:
        if logger:
            logger.error(f"❌ Redis连接错误: {e}")
        return None
    except Exception as e:
        if logger:
            logger.error(f"❌ 等待点位完成时发生异常: {e}")
        return None


def extract_unique_configs(target_macs_dict):
    """
    从初始扫描结果中提取唯一的信道/带宽/MAC配置
    
    Args:
        target_macs_dict: 初始扫描结果，格式：
            {
                "aa:bb:cc:dd:ee:ff": {"channel": 6, "bandwidth": "HT20"},
                "11:22:33:44:55:66": {"channel": 11, "bandwidth": "HT40+"}
            }
    
    Returns:
        list: 配置列表，格式：
            [
                {"channel": 6, "bandwidth": "HT20", "target_mac": "aa:bb:cc:dd:ee:ff"},
                {"channel": 11, "bandwidth": "HT40+", "target_mac": "11:22:33:44:55:66"}
            ]
    
    说明：
        将初始扫描的MAC字典转换为点位扫描需要的配置列表格式
    """
    configs = []
    for mac, config in target_macs_dict.items():
        configs.append({
            "channel": config.get("channel"),
            "bandwidth": config.get("bandwidth"),
            "target_mac": mac
        })
    return configs

# ==================== 多点扫描主控函数 ====================

def run_multi_point_scan_blocking(ptz, r, target_macs_dict, scan_params, logger=None, round_index=None):
    """
    多点扫描主控函数（阻塞执行）
    
    功能：
        编排整个多点扫描流程，负责云台移动和数据采集的协调
    
    Args:
        ptz: 云台控制对象
        r: Redis连接对象
        target_macs_dict: 初始扫描结果，格式：
            {
                "aa:bb:cc:dd:ee:ff": {"channel": 6, "bandwidth": "HT20"},
                ...
            }
        scan_params: 扫描参数，包含：
            - pan_range: [pan_min, pan_max]
            - tilt_range: [tilt_min, tilt_max]
            - pan_step: 水平步进（度）
            - tilt_step: 垂直步进（度）
            - dwell_time: 每个配置停留时间（秒）
            - extend_time: 无信号延长时间（秒）
    
    工作流程：
        1. 提取唯一配置（信道/带宽/MAC）
        2. 生成扫描路径（复用generate_scan_path）
        3. 遍历每个点位：
           a. 云台移动到点位
           b. 发送scan_at_point命令到capture_worker
           c. 阻塞等待点位完成
           d. 更新Redis状态
        4. 完成后恢复IDLE状态
    
    Returns:
        bool: 成功返回True，失败返回False
    """
    if logger:
        logger.info("🎯 开始多点扫描主控流程")
    
    # 步骤1：提取唯一配置
    configs = extract_unique_configs(target_macs_dict)
    if not configs:
        if logger:
            logger.error("❌ 没有可扫描的配置")
        return False

    try:
        old_point_keys = r.keys('multi_scan:point_*')
        if old_point_keys:
            r.delete(*old_point_keys)
    except Exception as e:
        if logger:
            logger.warning(f"⚠️ 清理旧多点轮次结果失败: {e}")
    
    # 🔥 优化：按信道/带宽分组，相同配置的MAC合并处理
    config_groups = {}
    for cfg in configs:
        key = (cfg['channel'], cfg['bandwidth'])
        if key not in config_groups:
            config_groups[key] = []
        config_groups[key].append(cfg['target_mac'])
    
    # 转换为新的配置格式（每组一个配置）
    grouped_configs = []
    for (channel, bandwidth), macs in config_groups.items():
        grouped_configs.append({
            'channel': channel,
            'bandwidth': bandwidth,
            'target_macs': macs  # 注意：这里是复数，包含多个MAC
        })
    
    if logger:
        logger.info(f"📋 扫描配置: {len(configs)}个MAC → 优化为{len(grouped_configs)}组配置")
        logger.info(f"💡 平均每组 {len(configs)/len(grouped_configs):.1f} 个MAC")
    
    # 步骤2：生成扫描路径
    pan_range = scan_params.get('pan_range', [GLOBAL_PAN_MIN, GLOBAL_TILT_MAX])
    tilt_range = scan_params.get('tilt_range', [GLOBAL_TILT_MIN, GLOBAL_TILT_MAX])
    pan_step = scan_params.get('pan_step', 10.0)
    tilt_step = scan_params.get('tilt_step', 10.0)
    dwell_time = scan_params.get('dwell_time', 5)
    extend_time = scan_params.get('extend_time', 3)
    
    # 🔥 复用generate_scan_path函数生成Z字形路径
    scan_path = generate_scan_path(
        pan_range, tilt_range, pan_step, tilt_step
    )
    
    if not scan_path:
        if logger:
            logger.error("❌ 生成扫描路径失败")
        return False
    
    # S1 采样点优化：计算全景拍照停留点
    sampling_points = select_sampling_points(scan_path)
    sampling_set = set(sampling_points)  # 用于 O(1) 查找
    # 生成本轮 round_id
    _round_id = f"mp_{int(time.time())}"
    if logger:
        logger.info(f"📷 [S1] 采样点优化: 原始路径 {len(scan_path)} 点 → "
                     f"全景采样 {len(sampling_points)} 点 (round={_round_id})")

    def _persist_round_snapshot(force_status=None):
        point_results = _collect_multi_point_results(r)
        snapshot_status = force_status or (
            "SUCCESS" if len(point_results) == len(scan_path) else "PARTIAL"
        )
        raw_dir = os.path.join(PANORAMA_RAW_DIR, _round_id)
        render_meta = {
            "scan_type": "multi_point",
            "scan_mode": scan_params.get("mode"),
            "pan_range": pan_range,
            "tilt_range": tilt_range,
            "pan_step": pan_step,
            "tilt_step": tilt_step,
            "dwell_time": dwell_time,
            "extend_time": extend_time,
            "expected_points": len(scan_path),
            "completed_points": len(point_results),
            "sampling_points_count": len(sampling_points),
            "grouped_config_count": len(grouped_configs),
            "target_macs": sorted(target_macs_dict.keys()),
            "omni_enabled": r.get('capture:omni_enabled') == '1',
        }
        _save_project_snapshot(
            r=r,
            scan_type="multi_point",
            round_id=_round_id,
            round_index=round_index,
            grid_data=point_results,
            render_meta=render_meta,
            status=snapshot_status,
            raw_dir=raw_dir if os.path.isdir(raw_dir) else None,
        )

    if logger:
        logger.info(f"🗺️ 扫描路径: {len(scan_path)}个点位")
        logger.info(f"📊 总任务量: {len(scan_path)}点 × {len(configs)}配置 = {len(scan_path)*len(configs)}次扫描")
    
    # 步骤3：遍历每个点位
    for point_idx, (target_pan, target_tilt) in enumerate(scan_path):
        # 🛑 检查停止标志
        if r.get('multi_scan:stop_multi_point_scan'):
            if logger:
                logger.warning("🛑 检测到停止标志，终止多点扫描")
            _persist_round_snapshot(force_status="PARTIAL")
            return False
        
        point_id = f"point_{point_idx}"
        
        if logger:
            logger.info(f"📍 [{point_idx+1}/{len(scan_path)}] 移动到点位: ({target_pan}, {target_tilt})")
        
        # 步骤3a：云台移动到点位
        try:
            # 🔥 使用safe_split_move确保精确到达
            if not safe_split_move(ptz, target_pan, target_tilt, order='pan_first', settle=1.0):
                if logger:
                    logger.error(f"❌ 移动到点位失败: ({target_pan}, {target_tilt}")
                continue
            
            # 等待云台稳定
            time.sleep(0.5)
            
            # 验证位置
            current_pan, current_tilt = ptz.get_position()
            if logger:
                logger.info(f"✅ 到达点位: ({current_pan}, {current_tilt})")

            # S1：若当前点是采样点，触发拍照
            if (target_pan, target_tilt) in sampling_set:
                _request_camera_capture(r, current_pan, current_tilt,
                                        round_id=_round_id, scan_type="multi_point")

            # 🔥 更新 Redis 中的位置状态
            try:
                current_status = json.loads(r.get(PTZ_STATUS_KEY) or '{}')
                current_status['position'] = {
                    'pan': round(current_pan, 2),
                    'tilt': round(current_tilt, 2)
                }
                current_status['ts'] = time.time()
                current_status['multi_scan']['current_point'] = point_idx + 1
                current_status['multi_scan']['total_points'] = len(scan_path)
                r.set(PTZ_STATUS_KEY, json.dumps(current_status))
            except Exception as update_e:
                if logger:
                    logger.warning(f"⚠️ 更新Redis位置失败: {update_e}")
        
        except Exception as e:
            if logger:
                logger.error(f"❌ 移动到点位失败: {e}")
            continue
        
        # 步骤3b：发送scan_at_point命令到capture_worker（使用优化后的分组配置）
        capture_command = {
            'action': 'scan_at_point',
            'point_id': point_id,
            'pan': current_pan,
            'tilt': current_tilt,
            'configs': grouped_configs,  # 使用分组后的配置
            'dwell_time': dwell_time,
            'extend_time': extend_time
        }
        r.lpush('capture:command_queue', json.dumps(capture_command))
        
        if logger:
            logger.info(f"📤 已发送scan_at_point命令: {point_id}")
        
        # 步骤3c：阻塞等待点位完成（使用BRPOP，最长30分钟）
        notify_data = wait_for_point_completion(r, point_id, timeout=1800, logger=logger)
        
        if not notify_data:
            if logger:
                logger.error(f"❌ 点位{point_id}扫描失败或超时")
            continue
        
        # 🛑 检查capture_worker是否已因stop标志提前终止（status='stopped'）
        if isinstance(notify_data, dict) and notify_data.get('status') == 'stopped':
            if logger:
                logger.warning(f"🛑 capture_worker报告点位{point_id}被用户中止，停止扫描")
            _persist_round_snapshot(force_status="PARTIAL")
            return False
        
        # 🛑 点位完成后再次检查停止标志，确保立即退出不再移动到下一个点
        if r.get('multi_scan:stop_multi_point_scan'):
            if logger:
                logger.warning(f"🛑 点位{point_id}完成后检测到停止标志，终止扫描")
            _persist_round_snapshot(force_status="PARTIAL")
            return False
        
        # 步骤3d：读取点位数据（可选，用于实时显示）
        point_data_json = r.get(f'multi_scan:{point_id}')
        if point_data_json:
            point_data = json.loads(point_data_json)
            if logger:
                success_count = sum(1 for result in point_data.get('scan_results', []) 
                                  if result.get('status') == 'success')
                logger.info(f"📊 点位{point_id}: {success_count}/{len(configs)}个配置成功")
        
        if logger:
            logger.info(f"✅ 点位{point_id}完成，继续下一个点位...")
    
    # 步骤4：完成扫描
    if logger:
        logger.info("🎉 多点扫描主控流程完成")

    _persist_round_snapshot()
    
    return True

def ptz_process_main():
    # �� 在函数开始处声明所有全局变量
    global intelligent_scan_active, intelligent_scan_round, PAN_MIN, PAN_MAX, TILT_MIN, TILT_MAX
    logger.info("启动...")

    # 初始化
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        ptz = PtzControl(device_address=SERIAL_ADDRESS, serial_port=SERIAL_PORT, baudrate=SERIAL_BAUDRATE)
        if not ptz.connect():
            raise ConnectionError("无法连接到云台硬件。")       
        logger.info("连接Redis和云台成功，网格管理器初始化完成。")        
        logger.info("进入主循环。")
        # 为ptz对象添加必要的属性
        ptz._last_move_time = time.time()
        # 串口连接成功后等待 1 秒，确保设备稳定再发首条指令
        time.sleep(1.5)

        # 首次获取并更新内存中的状态
        p0, t0 = ptz.get_position()
        device_state.update_position(p0, t0)
        device_state.update_state("IDLE")
        logger.info(f"初始化内存状态成功：Pan={device_state.get_position()['pan']}, Tilt={device_state.get_position()['tilt']}, State={device_state.get_state()}")
    except Exception as e:
        logger.error("初始化失败", extra={
            "error": str(e),
            "error_type": type(e).__name__
        })
        return

    # 设备限位
    try:
        limits = getattr(ptz, 'limits', None) or {}
        pan_limits = limits.get('pan') or {}
        tilt_limits = limits.get('tilt') or {}
        PAN_MIN = float(pan_limits.get('min', 0.0))
        PAN_MAX = float(pan_limits.get('max', 347.0))
        TILT_MIN = float(tilt_limits.get('min', -85.0))
        TILT_MAX = float(tilt_limits.get('max', 20.0))
    except Exception:
        PAN_MIN = 0.0
        PAN_MAX = 347.0
        TILT_MIN = -85.0
        TILT_MAX = 20.0

    logger.info(
        f"云台硬件限位（串口/驱动，并写入 ptz:limits）: "
        f"水平 ({PAN_MIN}° - {PAN_MAX}°), 垂直 ({TILT_MIN}° - {TILT_MAX}°)"
    )

    # 将限位写入 Redis，供 Web/其他进程读取，确保“同源”
    try:
        limits_payload = {
            "pan": {"min": PAN_MIN, "max": PAN_MAX},
            "tilt": {"min": TILT_MIN, "max": TILT_MAX}
        }
        r.set('ptz:limits', json.dumps(limits_payload, ensure_ascii=False))

        # 业务扫描范围（与 Web 全景 / capture 同源）：Redis gimbal:default_config，已与上式硬件限位求交
        try:
            br = business_scan_range_from_redis(
                r,
                {
                    "pan_min": PAN_MIN,
                    "pan_max": PAN_MAX,
                    "tilt_min": TILT_MIN,
                    "tilt_max": TILT_MAX,
                },
                {
                    "pan_min": PTZ_DEFAULT_PAN_MIN,
                    "pan_max": PTZ_DEFAULT_PAN_MAX,
                    "tilt_min": PTZ_DEFAULT_TILT_MIN,
                    "tilt_max": PTZ_DEFAULT_TILT_MAX,
                    "pan_step": PTZ_DEFAULT_PAN_STEP,
                    "tilt_step": PTZ_DEFAULT_TILT_STEP,
                },
            )
            logger.info(
                f"业务扫描范围（Redis gimbal:default_config，已与硬件限位求交）: "
                f"水平 ({br['pan_min']:.1f}° - {br['pan_max']:.1f}°), 垂直 ({br['tilt_min']:.1f}° - {br['tilt_max']:.1f}°), "
                f"步长 pan={br['pan_step']:.1f}° tilt={br['tilt_step']:.1f}°, 来源={br.get('source')}"
            )
        except Exception as _e:
            logger.warning(f"解析业务扫描范围（gimbal）用于日志时失败: {_e}")

        # 初始化状态：立即获取一次当前位置，避免 position 为 null
        p0, t0 = ptz.get_position()
        p0_rounded = round_angle_to_2dp(p0)
        t0_rounded = round_angle_to_2dp(t0)
        status = {"ts": time.time(), "position": {"pan": p0_rounded, "tilt": t0_rounded}, "state": "IDLE", "limits": limits_payload}
        device_state.update_position(p0_rounded, t0_rounded)
        device_state.update_state("IDLE")
        logger.info(f"设备状态初始化: 位置=({p0_rounded}, {t0_rounded}), 状态=IDLE")
        r.set(PTZ_STATUS_KEY, json.dumps(status))
    except Exception as e:
        logger.error("发布限位到Redis失败", extra={
            "error": str(e),
            "error_type": type(e).__name__
        })

    # 状态机变量
    state = "IDLE"
    target_position = {"pan": None, "tilt": None}
    TOLERANCE = PTZ_MOVE_COMPLETE_TOLERANCE  # 使用贴近硬件读数的到位判定容差

    auto_scan_active = False
    scan_sub_state = "IDLE"  # <--- 在这里添加
    scan_pan_range = [PTZ_DEFAULT_PAN_MIN, PTZ_DEFAULT_PAN_MAX]
    scan_tilt_range = [PTZ_DEFAULT_TILT_MIN, PTZ_DEFAULT_TILT_MAX]
    scan_pan_step = PTZ_DEFAULT_PAN_STEP
    scan_tilt_step = PTZ_DEFAULT_TILT_STEP
    # 新增：扫描步长参数
    scan_pan_step_size = 20.0
    scan_tilt_step_size = 10.0
    # 新增：每一步的停止时间
    scan_step_delay = 2.0
    scan_path_points = []
    current_scan_point_idx = 0
    scan_direction = 1  # 1 正向，-1 反向（用于 bounce）
    first_point_settled = False  # 首次到达扫描起点后的稳定等待标记
    is_once_mode = False  # 🔥 智能扫描once模式标志

    # 无进展重发参数
    NO_PROGRESS_DEG = PTZ_PROGRESS_DETECTION_THRESHOLD  # 使用配置的进展检测阈值
    RESEND_INTERVAL = 1.0
    last_progress_ts = None
    last_progress_pos = None
    homing_active = False
    # 空闲状态下的角度上报节流（秒）
    last_idle_status_ts = None
    IDLE_STATUS_INTERVAL = 1.0

    # 启动回零位
    if PTZ_HOME_ON_START:
        try:
            # 从 Redis 读取扫描范围，计算中间值作为初始点
            try:
                br = business_scan_range_from_redis(
                    r,
                    {"pan_min": PAN_MIN, "pan_max": PAN_MAX, "tilt_min": TILT_MIN, "tilt_max": TILT_MAX},
                    {"pan_min": PTZ_DEFAULT_PAN_MIN, "pan_max": PTZ_DEFAULT_PAN_MAX, "tilt_min": PTZ_DEFAULT_TILT_MIN, "tilt_max": PTZ_DEFAULT_TILT_MAX, "pan_step": PTZ_DEFAULT_PAN_STEP, "tilt_step": PTZ_DEFAULT_TILT_STEP},
                )
                home_pan = (br['pan_min'] + br['pan_max']) / 2.0
                home_tilt = (br['tilt_min'] + br['tilt_max']) / 2.0
                logger.info(f"启动回零位: 使用扫描范围中间点 Pan={home_pan:.2f}, Tilt={home_tilt:.2f}")
            except Exception as e:
                logger.warning(f"读取扫描范围失败，使用默认值: {e}")
                home_pan = PTZ_HOME_PAN % 360.0
                if home_pan < 0:
                    home_pan += 360.0
                home_pan = max(PAN_MIN, min(home_pan, PAN_MAX))
                home_tilt = max(TILT_MIN, min(PTZ_HOME_TILT, TILT_MAX))
            cur_pan, cur_tilt = ptz.get_position()
            need_move = True
            if cur_pan is not None and cur_tilt is not None:
                need_move = (abs(cur_pan - home_pan) >= TOLERANCE) or (abs(cur_tilt - home_tilt) >= TOLERANCE)
            if need_move:
                logger.info(f"启动回零位(分轴): 目标 Pan={home_pan:.2f}, Tilt={home_tilt:.2f}")
                # 分轴移动到HOME，避免两轴同发
                def _home_redis_update(p, t):
                    r.set(PTZ_STATUS_KEY, json.dumps({
                        "ts": time.time(),
                        "position": {"pan": round(p, 2), "tilt": round(t, 2)},
                        "state": "HOMING",
                        "homing": {
                            "active": True,
                            "target": {"pan": home_pan, "tilt": home_tilt}
                        }
                    }))
                if safe_split_move(ptz, home_pan, home_tilt, order='tilt_first',
                                   settle=PTZ_SPLIT_SETTLE_SEC,
                                   on_move_update=_home_redis_update):
                    ptz._last_move_time = time.time()
                    target_position = {"pan": home_pan, "tilt": home_tilt}
                    state = "MOVING"
                    homing_active = True
                    last_progress_ts = time.time()
                    last_progress_pos = None
                else:
                    logger.error("启动回零位失败：无法移动到目标位置")
                    return
            else:
                logger.info("启动回零位: 当前已在初始位置，跳过移动。")
        except Exception as e:
            logger.error("启动回零位失败", extra={
                "error": str(e),
                "error_type": type(e).__name__
            })

    # 主循环
    while True:
        timing_trace = None
        try:
            # 检查串口是否存在
            if not check_serial_exists(SERIAL_PORT):
                handle_serial_error(SERIAL_PORT)
            # 取命令
            command = None
            command_data_str = r.lpop(PTZ_COMMAND_QUEUE)
            if command_data_str:
                try:
                    command = json.loads(command_data_str)
                    logger.info(f"收到非阻塞命令: {command}")
                except json.JSONDecodeError:
                    logger.warning(f"收到无效JSON命令: {command_data_str}")
                    command = None
            elif state == "IDLE":
                command_data_tuple = r.brpop(PTZ_COMMAND_QUEUE, timeout=1)
                if command_data_tuple:
                    try:
                        command = json.loads(command_data_tuple[1])
                        logger.info(f"阻塞收到命令: {command}")
                    except json.JSONDecodeError:
                        logger.warning(f"阻塞收到无效JSON命令: {command_data_tuple[1]}")
                        command = None

            # 处理命令
            if command:
                action = command.get("action")
                new_target_pan = None
                new_target_tilt = None

                if action == 'stop':
                    ptz.stop()
                    state = "IDLE"
                    target_position = {"pan": None, "tilt": None}
                    auto_scan_active = False
                    homing_active = False
                    # 需要添加：
                    device_state.update_state("IDLE")
                    logger.info(f"设备内存状态更新: 状态={device_state.get_state()}")
                    logger.info("收到 'stop' 命令，云台停止并切换至 IDLE 状态。")
                    pan, tilt = ptz.get_position()
                    pan_rounded = round_angle_to_2dp(pan)
                    tilt_rounded = round_angle_to_2dp(tilt)
                    status = {"ts": time.time(), "position": {"pan": pan_rounded, "tilt": tilt_rounded}, "state": state}
                    status["auto_scan"] = {"active": False}
                    r.set(PTZ_STATUS_KEY, json.dumps(status))
                    continue


                if action == 'start_auto_scan':
                    pan_range = command.get('pan_range', [PTZ_DEFAULT_PAN_MIN, PTZ_DEFAULT_PAN_MAX])
                    tilt_range = command.get('tilt_range', [PTZ_DEFAULT_TILT_MIN, PTZ_DEFAULT_TILT_MAX])
                    pan_step = command.get('pan_step', scan_pan_step)
                    tilt_step = command.get('tilt_step', scan_tilt_step)
                    # 新增：每一步的停止时间（秒）
                    step_delay = command.get('step_delay', 2.0)
                    # 新增：水平角度步长
                    pan_step_size = command.get('pan_step_size', 20.0)
                    # 新增：垂直角度步长
                    tilt_step_size = command.get('tilt_step_size', 10.0)
                    # 在启动扫描时设置步径到Redis
                    try:
                        current_config = {
                            'source_steph': float(pan_step_size),
                            'source_stepv': float(tilt_step_size),
                            'updated_by': 'auto_scan',
                            'ts': time.time()
                        }
                        r.set('gimbal:current_config', json.dumps(current_config))
                        logger.info(f"✅ 系统步径已更新为自动扫描步径: ({pan_step_size}, {tilt_step_size})")
                    except Exception as e:
                        logger.error(f"❌ 更新系统步径到 Redis 失败: {e}")
                    # 🔥 智能扫描专用：检查是否为once模式
                    is_once_mode = command.get('loop_mode') == 'once'

                    scan_pan_range = pan_range
                    scan_tilt_range = tilt_range
                    scan_pan_step = pan_step
                    scan_tilt_step = tilt_step
                    # 保存新的扫描参数
                    scan_step_delay = step_delay
                    scan_pan_step_size = pan_step_size
                    scan_tilt_step_size = tilt_step_size

                    scan_path_points = generate_scan_path(pan_range, tilt_range, pan_step_size, tilt_step_size)
                    current_scan_point_idx = 0
                    scan_direction = 1
                    first_point_settled = False

                    # 注：自动栅格扫描不需要全景拍照（S1 仅用于多点扫描和全面扫描）

                    if scan_path_points:
                        auto_scan_active = True
                        homing_active = False
                        state = "AUTO_SCANNING"
                        # 🔥 保存once模式标志
                        
                        mode_info = f"模式: {'once(智能扫描)' if is_once_mode else PTZ_AUTO_SCAN_LOOP_MODE}"
                        logger.info(f"启动自动扫描，范围: Pan={pan_range}, Tilt={tilt_range}, 步长: Pan={pan_step_size}°, Tilt={tilt_step_size}°, 延迟: {step_delay}s, 总点数: {len(scan_path_points)}, {mode_info}")
                        
                        # 🔥 检查是否需要跳过限位点校准（智能扫描会跳过）
                        skip_calibration = command.get('skip_calibration', False)
                        if skip_calibration:
                            logger.info("⏭️ 跳过限位点校准（智能扫描已在第1轮开始前校准过）")
                        else:
                            # 🔥 自动扫描开始前先校准
                            if _goto_calibration_point(ptz, r, context="自动扫描开始前"):
                                time.sleep(1.0)
                            else:
                                logger.warning("⚠️ [自动扫描] 校准失败，但继续扫描")

                        pan, tilt = ptz.get_position()
                        # 同步到设备内存
                        device_state.update_state("AUTO_SCANNING")
                        logger.info(f"设备内存状态更新: 状态={device_state.get_state()}")
                        pan_rounded = round_angle_to_2dp(pan)
                        tilt_rounded = round_angle_to_2dp(tilt)
                        status = {"ts": time.time(), "position": {"pan": pan_rounded, "tilt": tilt_rounded}, "state": state}
                        scan_sub_state = "PREPARING"
                        # 在移动到起始点之前添加
                        r.set('capture:scan_status', 'PREPARING')
                        logger.info("🔒 设置capture状态为PREPARING，暂停数据包处理")
                        status.update({
                            "auto_scan": {
                                "active": True,
                                "range": {"pan": scan_pan_range, "tilt": scan_tilt_range},
                                "step_delay": step_delay,
                                "pan_step_size": pan_step_size,
                                "tilt_step_size": tilt_step_size,
                                "sub_state": scan_sub_state  # <-- 关键标志：准备中
                            }
                        })
                        # 🔥 智能扫描时添加轮次
                        # 🔥 新增：智能扫描轮次补充
                        if intelligent_scan_active:
                            status["auto_scan"]["current_round"] = intelligent_scan_round
                            status["auto_scan"]["max_rounds"] = INTELLIGENT_SCAN_CONFIG['max_iterations']  # 🔥 新增最大轮次
                        r.set(PTZ_STATUS_KEY, json.dumps(status))
                        
                        # 直接移动到扫描起始点
                        first_pan, first_tilt = scan_path_points[0]
                        logger.info(f"自动扫描：移动到扫描起始点(分轴): Pan={first_pan:.2f}°, Tilt={first_tilt:.2f}°")
                        if safe_split_move(ptz, first_pan, first_tilt, order='tilt_first', settle=PTZ_SPLIT_SETTLE_SEC):
                            time.sleep(PTZ_INITIAL_SETTLE_SEC)
                            ptz._last_move_time = time.time()
                            target_position = {"pan": _normalize_pan(first_pan), "tilt": first_tilt}                            
                            # [修改点 2] 到达后，更新Redis状态，标记为“扫描中”
                            logger.info("已到达扫描起始点，数据采集将恢复。")

                            # 新增：更新状态为SCANNING
                            scan_sub_state = "SCANNING"
                            pan, tilt = ptz.get_position() # 获取最新位置
                            pan_rounded = round_angle_to_2dp(pan)
                            tilt_rounded = round_angle_to_2dp(tilt)
                            status = {"ts": time.time(), "position": {"pan": pan_rounded, "tilt": tilt_rounded}, "state": state}
                            status.update({
                                "auto_scan": {
                                    "active": True,
                                    "range": {"pan": scan_pan_range, "tilt": scan_tilt_range},
                                    "step_delay": step_delay,
                                    "pan_step_size": pan_step_size,
                                    "tilt_step_size": tilt_step_size,
                                    "sub_state": scan_sub_state # <-- 关键标志：扫描中
                                }
                            })
                            # 🔥 智能扫描时添加轮次
                            # 🔥 新增：智能扫描轮次补充
                            if intelligent_scan_active:
                                status["auto_scan"]["current_round"] = intelligent_scan_round
                                status["auto_scan"]["max_rounds"] = INTELLIGENT_SCAN_CONFIG['max_iterations']  # 🔥 新增最大轮次
                            r.set(PTZ_STATUS_KEY, json.dumps(status))
                        else:
                            logger.error("自动扫描启动失败：无法移动到扫描起始点")
                            auto_scan_active = False
                            state = "IDLE"
                            # 如果启动失败，也需要更新状态，清除 auto_scan 信息
                            pan, tilt = ptz.get_position()
                            pan_rounded = round_angle_to_2dp(pan)
                            tilt_rounded = round_angle_to_2dp(tilt)
                            status = {"ts": time.time(), "position": {"pan": pan_rounded, "tilt": tilt_rounded}, "state": "IDLE"}
                            
                            r.set(PTZ_STATUS_KEY, json.dumps(status))
                            continue
                    else:
                        logger.warning("自动扫描路径生成失败")
                    continue

                if action == 'stop_auto_scan':
                    if auto_scan_active or state == "AUTO_SCANNING":
                        auto_scan_active = False
                        # 同步到设备内存
                        device_state.update_state("IDLE")
                        logger.info(f"设备内存状态更新: 状态={device_state.get_state()}")
                        scan_sub_state = "IDLE"  # 新增：重置子状态
                        state = "IDLE"
                        # 新增：设置capture状态为IDLE
                        r.set('capture:scan_status', 'IDLE')
                        logger.info("🔓 设置capture状态为IDLE，恢复正常数据包处理")
                        # 需要添加位置更新：
                        pan, tilt = ptz.get_position()
                        pan_rounded = round_angle_to_2dp(pan)
                        tilt_rounded = round_angle_to_2dp(tilt)
                        device_state.update_position(pan_rounded, tilt_rounded)
                        logger.info(f"设备内存状态更新: 位置=({pan_rounded}, {tilt_rounded})")
                        target_position = {"pan": None, "tilt": None}
                        scan_path_points = []
                        current_scan_point_idx = 0
                        ptz.stop()                        
                        
                        logger.info("收到停止自动扫描命令，云台停止并切换至 IDLE 状态。")
                        pan, tilt = ptz.get_position()
                        pan_rounded = round_angle_to_2dp(pan)
                        tilt_rounded = round_angle_to_2dp(tilt)
                        status = {"ts": time.time(), "position": {"pan": pan_rounded, "tilt": tilt_rounded}, "state": state}
                        status["auto_scan"] = {"active": False}
                        r.set(PTZ_STATUS_KEY, json.dumps(status))
                    else:
                        logger.warning("收到停止自动扫描命令，但当前未处于自动扫描，忽略。")
                    
                     # 🔥 新增：同时停止智能扫描
                    if intelligent_scan_active:
                        logger.info("🛑 同时停止智能扫描")
                        intelligent_scan_active = False
                        # 停止当前扫描，清理状态
                        r.lpush(PTZ_COMMAND_QUEUE, json.dumps({"action": "stop_auto_scan"}))
                        r.set(INTELLIGENT_SCAN_ACTIVE_KEY, '0')
                        r.delete(INTELLIGENT_SCAN_STATUS_KEY)
                        r.delete(INTELLIGENT_SCAN_SIGNALS_KEY)

                        logger.info("✅ 智能扫描已停止")
                    continue
                
                # ============= 新增：初始MAC扫描命令（多点扫描功能的第一步）=============
                if action == 'start_initial_scan':
                    """
                    初始MAC扫描命令
                    功能：在固定点位发现所有可用的MAC地址及其最优配置
                    
                    参数：
                        - configs: 信道/带宽配置列表（可选，默认使用INITIAL_SCAN_CONFIGS）
                        - dwell_time: 每个配置停留时间（秒，默认3秒）
                    
                    工作流程：
                        1. 更新PTZ状态为"INITIAL_SCANNING"
                        2. 转发discover_macs命令到capture_worker
                        3. 阻塞等待完成通知
                        4. 获取发现的MAC地址数据
                        5. 恢复PTZ状态为"IDLE"
                    """
                    logger.info("📡 收到初始MAC扫描命令")
                    
                    # 防重入：如果初始扫描正在进行中，拒绝重复发起
                    if r.get('multi_scan:initial_scanning') == '1':
                        logger.warning("⚠️ 初始扫描正在进行中，忽略重复命令")
                        continue
                    r.set('multi_scan:initial_scanning', '1', ex=600)  # 10分钟超时保护
                    
                    # 步骤1：获取参数（如果未提供configs，使用预定义的完整配置）
                    # dwell_time 默认使用 INITIAL_SCAN_DWELL_TIME 常量（可在文件顶部修改）
                    configs = command.get('configs', INITIAL_SCAN_CONFIGS)
                    dwell_time = command.get('dwell_time', INITIAL_SCAN_DWELL_TIME)
                    
                    if not configs:
                        logger.error("❌ start_initial_scan命令配置为空")
                        continue
                    
                    logger.info(f"🔍 开始初始扫描: {len(configs)}个配置, 每个停留{dwell_time}秒")
                    
                    # 步骤2：记录当前位置（限位校准统一在步骤4进行，此处只记录）
                    try:
                        original_pan, original_tilt = ptz.get_position()
                        logger.info(f"📍 记录当前位置: Pan={original_pan:.2f}°, Tilt={original_tilt:.2f}°")
                        pan, tilt = original_pan, original_tilt
                    except Exception as e:
                        logger.warning(f"⚠️ 获取当前位置失败: {e}")
                        pan, tilt = 0.0, 0.0
                    
                    # 步骤3：清理旧数据
                    logger.info("🧹 清理旧的扫描数据...")
                    try:
                        # 删除旧的初始扫描结果
                        r.delete('multi_scan:target_macs')
                        r.delete('multi_scan:initial_position')
                        r.delete('multi_scan:initial_scan_notify')
                        
                        # 删除旧的点位数据
                        point_keys = r.keys('multi_scan:point_*')
                        if point_keys:
                            r.delete(*point_keys)
                            logger.info(f"🗑️  删除了 {len(point_keys)} 个旧点位数据")
                        
                        logger.info("✅ 旧数据清理完成")
                    except Exception as e:
                        logger.warning(f"⚠️ 清理旧数据时出错: {e}")
                    
                    # 步骤3：记录初始点位置（使用临时变量保存）
                    initial_pan = round(pan, 2)
                    initial_tilt = round(tilt, 2)
                    initial_position = {
                        "pan": initial_pan,
                        "tilt": initial_tilt
                    }
                    logger.info(f"📍 初始点位置: 水平={initial_pan}°, 垂直={initial_tilt}°")
                    
                    # 保存初始点位置到Redis
                    r.set('multi_scan:initial_position', json.dumps(initial_position))
                    
                    # 步骤4：去限位点校准（消除累积误差）
                    logger.info("🎯 准备去限位点校准...")
                    if _goto_calibration_point(ptz, r, context="初始扫描开始前"):
                        logger.info("✅ 限位点校准完成")
                        time.sleep(1.0)  # 等待稳定
                    else:
                        logger.warning("⚠️ 限位点校准失败，继续扫描")
                    
                    # 步骤5：移动回初始点位置
                    logger.info(f"🔙 返回初始点位置: ({initial_pan}°, {initial_tilt}°)")
                    if safe_split_move(ptz, initial_pan, initial_tilt, order='tilt_first', settle=1.0):
                        time.sleep(0.5)  # 等待稳定
                        # 更新当前位置
                        pan, tilt = ptz.get_position()
                        logger.info(f"✅ 已返回初始点，当前位置: ({pan:.2f}°, {tilt:.2f}°)")
                    else:
                        logger.warning("⚠️ 返回初始点失败，使用校准后的位置继续")
                        pan, tilt = ptz.get_position()
                    
                    # 步骤6：更新PTZ状态
                    state = "INITIAL_SCANNING"
                    status = {
                        "ts": time.time(),
                        "position": {"pan": pan, "tilt": tilt},
                        "state": state,
                        "multi_scan": {
                            "active": True,
                            "phase": "initial_scanning",
                            "configs_count": len(configs),
                            "initial_position": initial_position
                        }
                    }
                    r.set(PTZ_STATUS_KEY, json.dumps(status))
                    
                    # 步骤7：转发命令到capture_worker
                    capture_command = {
                        'action': 'discover_macs',
                        'configs': configs,
                        'dwell_time': dwell_time
                    }
                    r.lpush('capture:command_queue', json.dumps(capture_command))
                    logger.info("📤 已转发discover_macs命令到capture_worker")
                    
                    # 步骤8：轮询等待完成通知（每3秒检查一次停止标志，无超时限制）
                    # 初始扫描耗时 = 配置数 × dwell_time，可能远超几百秒，不设超时
                    logger.info("⏳ 等待初始扫描完成（无超时限制，等待完成通知或停止指令）...")
                    _initial_scan_done = False
                    try:
                        while True:
                            # 检查停止标志（停止命令响应入口）
                            if r.get('multi_scan:stop_initial_scan'):
                                logger.info("🛑 检测到初始扫描停止标志，提前结束等待")
                                break
                            
                            result = r.brpop('multi_scan:initial_scan_notify', timeout=3)
                            if result:
                                _, notify_json = result
                                notify_data = json.loads(notify_json)
                                
                                if notify_data.get('status') == 'done':
                                    mac_count = notify_data.get('mac_count', 0)
                                    logger.info(f"✅ 初始扫描完成，发现{mac_count}个MAC地址")
                                    _initial_scan_done = True
                                    
                                    # 步骤9：获取发现的MAC数据
                                    target_macs_json = r.get('multi_scan:target_macs')
                                    if target_macs_json:
                                        target_macs = json.loads(target_macs_json)
                                        logger.info(f"📊 发现的MAC地址: {list(target_macs.keys())}")
                                    else:
                                        logger.warning("⚠️ 未找到multi_scan:target_macs数据")
                                elif notify_data.get('status') == 'stopped':
                                    logger.info("🛑 capture_worker 已停止初始扫描")
                                else:
                                    logger.error(f"❌ 初始扫描失败: {notify_data}")
                                break  # 收到通知，退出等待循环
                    
                    except Exception as e:
                        logger.error(f"❌ 初始扫描过程异常: {e}")
                    
                    # 步骤10：恢复PTZ状态
                    state = "IDLE"
                    status = {
                        "ts": time.time(),
                        "position": {"pan": pan, "tilt": tilt},
                        "state": state,
                        "multi_scan": {
                            "active": False,
                            "phase": "completed"
                        }
                    }
                    r.set(PTZ_STATUS_KEY, json.dumps(status))
                    # 清除防重入标志
                    r.delete('multi_scan:initial_scanning')
                    logger.info("✅ 初始扫描命令处理完成")
                
                # ============= 停止初始扫描命令 =============
                if action == 'stop_initial_scan':
                    """停止正在进行的初始扫描"""
                    logger.info("🛑 收到停止初始扫描命令")
                    
                    # 设置停止标志
                    r.set('multi_scan:stop_initial_scan', '1', ex=60)  # 60秒后自动过期
                    # 清除防重入标志
                    r.delete('multi_scan:initial_scanning')
                    logger.info("✅ 已设置初始扫描停止标志")
                    
                    # 更新PTZ状态
                    status = {
                        "ts": time.time(),
                        "position": {"pan": pan, "tilt": tilt},
                        "state": "IDLE",
                        "multi_scan": {
                            "active": False,
                            "phase": "stopped"
                        }
                    }
                    
                    r.set(PTZ_STATUS_KEY, json.dumps(status))
                    continue
                
                # ============= 多点扫描命令（支持 auto/manual 双模式 + 持续重复）=============
                if action == 'start_multi_point_scan':
                    """
                    多点扫描命令（重构版）
                    功能：在多个点位对目标MAC地址进行RSSI扫描，持续循环直到收到停止指令。

                    参数：
                        - mode: "auto"（默认）| "manual"
                            auto   — MAC来自初始扫描结果，每隔 MULTI_SCAN_MAC_REFRESH_ROUNDS 轮刷新一次
                            manual — MAC由用户直接指定，不触发初始扫描，不自动刷新
                        - manual_macs: list（仅 manual 模式）
                            格式：[{"mac": "xx:xx:xx:xx:xx:xx", "channel": 6, "bandwidth": "HT20"}, ...]
                            channel/bandwidth 为可选；若未填，系统将遍历所有配置自动探测
                        - pan_range: 水平范围 [min, max]
                        - tilt_range: 垂直范围 [min, max]
                        - pan_step: 水平步进（度）
                        - tilt_step: 垂直步进（度）
                        - dwell_time: 每个点位停留采集时长（秒），默认使用 MULTI_SCAN_DWELL_TIME
                        - extend_time: 无信号时的延长采集时长（秒），默认使用 MULTI_SCAN_EXTEND_TIME

                    重复机制：
                        - 完成一轮后等待 MULTI_SCAN_REPEAT_INTERVAL 秒，再开始下一轮
                        - 期间每秒检测一次停止标志（multi_scan:stop_multi_point_scan）
                        - auto 模式下，每隔 MULTI_SCAN_MAC_REFRESH_ROUNDS 轮重做初始扫描，
                          新发现的 MAC 追加到列表（只增不减）

                    校准：每轮开始时去限位点校准一次，轮内各点位不再重复校准。
                    """
                    logger.info("🗺️ 收到多点扫描命令（持续重复模式）")

                    # ── 解析启动参数 ──────────────────────────────────────────────
                    scan_mode = command.get('mode', 'auto')  # "auto" 或 "manual"
                    manual_macs_raw = command.get('manual_macs', [])  # 仅 manual 模式使用
                    scan_params = {
                        'pan_range':   command.get('pan_range',   [GLOBAL_PAN_MIN,  GLOBAL_PAN_MAX]),
                        'tilt_range':  command.get('tilt_range',  [GLOBAL_TILT_MIN, GLOBAL_TILT_MAX]),
                        'pan_step':    command.get('pan_step',    10.0),
                        'tilt_step':   command.get('tilt_step',   10.0),
                        # 点位停留时长：优先用请求参数，否则用顶部可配置常量
                        'dwell_time':  command.get('dwell_time',  MULTI_SCAN_DWELL_TIME),
                        # 无信号延长时长：同上
                        'extend_time': command.get('extend_time', MULTI_SCAN_EXTEND_TIME),
                    }
                    scan_params['mode'] = scan_mode
                    logger.info(f"📐 模式={scan_mode}, 扫描参数: 水平={scan_params['pan_range']}, "
                                f"垂直={scan_params['tilt_range']}, "
                                f"步进=({scan_params['pan_step']}, {scan_params['tilt_step']}), "
                                f"停留={scan_params['dwell_time']}s, 延长={scan_params['extend_time']}s")

                    # ── 清理旧数据 & 停止标志 ────────────────────────────────────
                    try:
                        point_keys = r.keys('multi_scan:point_*')
                        if point_keys:
                            r.delete(*point_keys)
                        r.delete('multi_scan:stop_multi_point_scan')
                        logger.info("✅ 旧数据和停止标志已清理")
                    except Exception as e:
                        logger.warning(f"⚠️ 清理旧数据时出错: {e}")

                    # ── manual 模式：构建初始 MAC 字典 ───────────────────────────
                    # target_macs 格式：{mac: {"channel": x, "bandwidth": "HT20", ...}}
                    if scan_mode == 'manual':
                        if not manual_macs_raw:
                            logger.error("❌ manual 模式未提供 manual_macs，中止")
                            continue
                        # 将用户输入转换为内部格式；channel/bandwidth 缺失时置 None，待探测
                        target_macs = {}
                        for item in manual_macs_raw:
                            mac = item.get('mac', '').lower().strip()
                            if not mac:
                                continue
                            target_macs[mac] = {
                                "channel":   item.get('channel',   None),
                                "bandwidth": item.get('bandwidth', None),
                                "source":    "manual"
                            }
                        logger.info(f"📋 manual 模式：用户指定 {len(target_macs)} 个 MAC")
                    else:
                        # auto 模式：从 Redis 取初始扫描结果
                        target_macs_json = r.get('multi_scan:target_macs')
                        if not target_macs_json:
                            logger.error("❌ auto 模式未找到初始扫描结果，请先执行 start_initial_scan")
                            continue
                        target_macs = json.loads(target_macs_json)
                        if not target_macs:
                            logger.error("❌ 初始扫描结果为空")
                            continue
                        logger.info(f"📊 auto 模式：初始 MAC 数量={len(target_macs)}")

                    # ══════════════════════════════════════════════════════════════
                    # 持续重复主循环
                    # ══════════════════════════════════════════════════════════════
                    multi_scan_round = 0  # 已完成的轮次计数
                    
                    # 记住初始扫描时的云台位置，后续刷新 MAC 时回到这个位置
                    # （用户是有意把云台移到这个位置做初始扫描的）
                    try:
                        _initial_scan_pan, _initial_scan_tilt = ptz.get_position()
                        logger.info(f"📌 记录初始扫描位置: Pan={_initial_scan_pan:.2f}°, Tilt={_initial_scan_tilt:.2f}°")
                    except Exception as e:
                        _initial_scan_pan, _initial_scan_tilt = None, None
                        logger.warning(f"⚠️ 无法记录初始扫描位置: {e}")

                    while True:
                        # ── 检查停止标志 ─────────────────────────────────────────
                        if r.get('multi_scan:stop_multi_point_scan'):
                            logger.info("🛑 检测到停止标志，退出多点扫描循环")
                            break

                        multi_scan_round += 1
                        logger.info(f"🔁 ===== 多点扫描 第 {multi_scan_round} 轮 开始 =====")

                        # ── 每轮开始时校准一次 ───────────────────────────────
                        if r.get('multi_scan:stop_multi_point_scan'):
                            logger.info("🛑 校准前检测到停止标志，退出多点扫描")
                            break
                        logger.info("🎯 每轮校准：前往限位点消除累积误差...")
                        if _goto_calibration_point(ptz, r, context=f"多点扫描第{multi_scan_round}轮开始"):
                            logger.info("✅ 校准完成")
                            time.sleep(1.0)
                        # 注：此处不写状态，保持 CALIBRATING，由后续第2293行在正式扫描前切换 MULTI_SCANNING

                        # ── auto 模式刷新前：回到初始扫描位置 ─────────────────
                        # 用户是有意在某个位置做初始扫描的，刷新时必须回到同一位置
                        # 这样每次刷新的天线朝向一致，淘汰判断才公平
                        need_refresh = (scan_mode == 'auto' and (
                                       (multi_scan_round == 1 and not target_macs) or
                                       (multi_scan_round > 1 and 
                                        multi_scan_round % MULTI_SCAN_MAC_REFRESH_ROUNDS == 0)))
                        
                        if need_refresh and _initial_scan_pan is not None:
                            logger.info(f"🔙 回到初始扫描位置: Pan={_initial_scan_pan:.2f}°, Tilt={_initial_scan_tilt:.2f}°")
                            if safe_split_move(ptz, _initial_scan_pan, _initial_scan_tilt, order='tilt_first', settle=1.0):
                                time.sleep(0.5)
                                pan_now, tilt_now = ptz.get_position()
                                logger.info(f"✅ 已到达初始扫描位置: Pan={pan_now:.2f}°, Tilt={tilt_now:.2f}°")
                            else:
                                logger.warning("⚠️ 移动到初始扫描位置失败，在当前位置刷新")

                        # ── auto 模式：定期刷新 MAC 列表（增删并行）────────────
                        # 第1轮：如果已有初始扫描结果则跳过刷新，直接用
                        # 后续每隔 MULTI_SCAN_MAC_REFRESH_ROUNDS 轮刷新一次
                        if scan_mode == 'auto' and (
                                (multi_scan_round == 1 and not target_macs) or
                                (multi_scan_round > 1 and multi_scan_round % MULTI_SCAN_MAC_REFRESH_ROUNDS == 0)):
                            logger.info(f"🔄 [auto] 第{multi_scan_round}轮：重新执行初始扫描刷新 MAC 列表...")
                            try:
                                # 发送初始扫描命令给 capture_worker
                                r.delete('multi_scan:initial_scan_notify')
                                capture_cmd = {
                                    'action': 'discover_macs',
                                    'configs': INITIAL_SCAN_CONFIGS,
                                    # 刷新扫描用较短的 dwell_time（不需要像初始扫描那么久）
                                    'dwell_time': MULTI_SCAN_REFRESH_DWELL_TIME
                                }
                                r.lpush('capture:command_queue', json.dumps(capture_cmd))

                                # 等待初始扫描完成（短超时轮询，每 3 秒检查一次停止标志）
                                refresh_timeout = len(INITIAL_SCAN_CONFIGS) * INITIAL_SCAN_DWELL_TIME * 2
                                refresh_start = time.time()
                                result = None
                                refresh_aborted = False
                                
                                while time.time() - refresh_start < refresh_timeout:
                                    # 每次只等 3 秒，然后检查停止标志
                                    result = r.brpop('multi_scan:initial_scan_notify', timeout=3)
                                    if result:
                                        break
                                    # 检查停止标志
                                    if r.get('multi_scan:stop_multi_point_scan'):
                                        logger.info("🛑 刷新 MAC 期间检测到停止标志，中止刷新")
                                        refresh_aborted = True
                                        break
                                
                                if refresh_aborted:
                                    break  # 跳出 while True 主循环

                                if result:
                                    _, notify_json = result
                                    notify_data = json.loads(notify_json)
                                    if notify_data.get('status') == 'done':
                                        # 读取最新扫描结果
                                        new_macs_json = r.get('multi_scan:target_macs')
                                        if new_macs_json:
                                            new_macs = json.loads(new_macs_json)
                                            added = 0
                                            
                                            # 1) 本次出现的 MAC：归零 _miss_count，新 MAC 追加
                                            for mac, info in new_macs.items():
                                                if mac not in target_macs:
                                                    info['_miss_count'] = 0
                                                    target_macs[mac] = info
                                                    added += 1
                                                else:
                                                    # 已有 MAC 本次又出现了，归零计数器
                                                    target_macs[mac]['_miss_count'] = 0
                                            
                                            # 2) 本次未出现的 MAC：_miss_count +1
                                            removed = 0
                                            macs_to_remove = []
                                            for mac in list(target_macs.keys()):
                                                if mac not in new_macs:
                                                    miss = target_macs[mac].get('_miss_count', 0) + 1
                                                    target_macs[mac]['_miss_count'] = miss
                                                    if MULTI_SCAN_MAC_MISS_THRESHOLD > 0 and miss >= MULTI_SCAN_MAC_MISS_THRESHOLD:
                                                        macs_to_remove.append(mac)
                                            
                                            # 3) 执行淘汰
                                            for mac in macs_to_remove:
                                                logger.info(f"🗑️ [auto] 淘汰 MAC: {mac}（连续 {MULTI_SCAN_MAC_MISS_THRESHOLD} 次刷新未出现）")
                                                del target_macs[mac]
                                                removed += 1
                                            
                                            logger.info(f"✅ MAC 列表刷新完成：新增 {added} 个，"
                                                        f"淘汰 {removed} 个，"
                                                        f"当前共 {len(target_macs)} 个")
                                            # 将合并后的列表写回 Redis
                                            r.set('multi_scan:target_macs',
                                                  json.dumps(target_macs))
                                        else:
                                            logger.warning("⚠️ 初始扫描完成但未找到结果数据")
                                    else:
                                        logger.warning(f"⚠️ 初始扫描未正常完成: {notify_data}")
                                else:
                                    logger.warning("⚠️ 初始扫描刷新超时，本轮使用旧 MAC 列表继续")

                            except Exception as e:
                                logger.error(f"❌ 刷新 MAC 列表异常: {e}，本轮继续使用旧列表")

                        # ── manual 模式：探测尚未确定信道的 MAC ─────────────────
                        # 只在首轮（或信道未知的 MAC 存在时）探测
                        if scan_mode == 'manual':
                            unknown_macs = [m for m, info in target_macs.items()
                                            if not info.get('channel')]
                            if unknown_macs:
                                logger.info(f"🔍 [manual] 探测 {len(unknown_macs)} 个未知信道的 MAC...")
                                try:
                                    r.delete('multi_scan:initial_scan_notify')
                                    # 复用 discover_macs 命令，完成后从结果里匹配目标 MAC
                                    capture_cmd = {
                                        'action': 'discover_macs',
                                        'configs': INITIAL_SCAN_CONFIGS,
                                        # 使用 INITIAL_SCAN_DWELL_TIME 常量（可在文件顶部修改）
                                        'dwell_time': INITIAL_SCAN_DWELL_TIME
                                    }
                                    r.lpush('capture:command_queue', json.dumps(capture_cmd))

                                    detect_timeout = len(INITIAL_SCAN_CONFIGS) * INITIAL_SCAN_DWELL_TIME * 2
                                    result = r.brpop('multi_scan:initial_scan_notify',
                                                     timeout=detect_timeout)
                                    if result:
                                        _, notify_json = result
                                        notify_data = json.loads(notify_json)
                                        if notify_data.get('status') == 'done':
                                            detected_json = r.get('multi_scan:target_macs')
                                            if detected_json:
                                                detected = json.loads(detected_json)
                                                resolved = 0
                                                for mac in unknown_macs:
                                                    if mac in detected:
                                                        target_macs[mac].update({
                                                            'channel':   detected[mac]['channel'],
                                                            'bandwidth': detected[mac]['bandwidth'],
                                                        })
                                                        resolved += 1
                                                        logger.info(f"✅ MAC {mac} 探测成功: "
                                                                    f"信道{detected[mac]['channel']} "
                                                                    f"{detected[mac]['bandwidth']}")
                                                still_unknown = len(unknown_macs) - resolved
                                                logger.info(f"📋 探测完成：{resolved} 个已解析，"
                                                            f"{still_unknown} 个本轮跳过（未捕获）")
                                    else:
                                        logger.warning("⚠️ manual 模式探测超时，未知信道 MAC 本轮跳过")
                                except Exception as e:
                                    logger.error(f"❌ manual 模式探测异常: {e}")

                        # ── 过滤出本轮可用的 MAC（已有信道信息的）────────────────
                        active_macs = {m: info for m, info in target_macs.items()
                                       if info.get('channel')}
                        if not active_macs:
                            logger.warning("⚠️ 本轮无可用 MAC（均未探测到信道），"
                                           f"等待 {MULTI_SCAN_REPEAT_INTERVAL}s 后重试...")
                        else:
                            logger.info(f"📊 本轮参与扫描的 MAC 数量: {len(active_macs)}")

                            # ── 停止标志二次确认（等待刷新 MAC 期间可能已收到停止） ──
                            if r.get('multi_scan:stop_multi_point_scan'):
                                logger.info("🛑 校准前检测到停止标志，退出多点扫描")
                                break
                            pan, tilt = ptz.get_position()

                            # ── 更新 PTZ 状态 ────────────────────────────────────
                            state = "MULTI_SCANNING"
                            r.set(PTZ_STATUS_KEY, json.dumps({
                                "ts":    time.time(),
                                "position": {"pan": pan, "tilt": tilt},
                                "state": state,
                                "multi_scan": {
                                    "active": True,
                                    "mode":   scan_mode,
                                    "round":  multi_scan_round,
                                    "phase":  "scanning",
                                    "target_macs_count": len(active_macs)
                                }
                            }))

                            # ── 执行本轮多点扫描 ─────────────────────────────────
                            logger.info(f"🚀 第{multi_scan_round}轮多点扫描执行中...")
                            try:
                                success = run_multi_point_scan_blocking(
                                    ptz=ptz,
                                    r=r,
                                    target_macs_dict=active_macs,
                                    scan_params=scan_params,
                                    logger=logger,
                                    round_index=multi_scan_round,
                                )
                                if success:
                                    logger.info(f"✅ 第{multi_scan_round}轮多点扫描完成")
                                else:
                                    logger.warning(f"⚠️ 第{multi_scan_round}轮多点扫描未完整完成")
                            except Exception as e:
                                logger.error(f"❌ 第{multi_scan_round}轮多点扫描异常: {e}")

                        # ── 再次检查停止标志 ─────────────────────────────────────
                        if r.get('multi_scan:stop_multi_point_scan'):
                            logger.info("🛑 扫描完成后检测到停止标志，退出循环")
                            break

                        # ── 轮次间隔等待：分段检测停止标志 ──────────────────────
                        # 使用 MULTI_SCAN_REPEAT_INTERVAL 常量（可在文件顶部修改）
                        logger.info(f"⏸️ 第{multi_scan_round}轮完成，等待 "
                                    f"{MULTI_SCAN_REPEAT_INTERVAL}s 后开始下一轮...")
                        for _ in range(MULTI_SCAN_REPEAT_INTERVAL):
                            time.sleep(1)
                            if r.get('multi_scan:stop_multi_point_scan'):
                                logger.info("🛑 等待期间检测到停止标志，退出循环")
                                break
                        else:
                            continue  # 正常等待结束，进入下一轮
                        break  # 等待期间被停止，退出外层 while

                    # ── 恢复 PTZ 状态 ────────────────────────────────────────────
                    state = "IDLE"
                    r.set(PTZ_STATUS_KEY, json.dumps({
                        "ts":    time.time(),
                        "position": {"pan": pan, "tilt": tilt},
                        "state": state,
                        "multi_scan": {
                            "active":         False,
                            "mode":           scan_mode,
                            "rounds_completed": multi_scan_round,
                            "phase":          "completed"
                        }
                    }))
                    logger.info(f"✅ 多点扫描结束，共完成 {multi_scan_round} 轮")
                    _finalize_current_project(r, expected_scan_type="multi_point", status="STOPPED")
                
                # ============= 停止多点扫描命令 =============
                if action == 'stop_multi_point_scan':
                    """停止正在进行的多点扫描"""
                    logger.info("🛑 收到停止多点扫描命令")
                    
                    # 设置停止标志
                    r.set('multi_scan:stop_multi_point_scan', '1', ex=60)  # 60秒后自动过期
                    logger.info("✅ 已设置多点扫描停止标志")
                    
                    # 更新PTZ状态
                    status = {
                        "ts": time.time(),
                        "position": {"pan": pan, "tilt": tilt},
                        "state": "IDLE",
                        "multi_scan": {
                            "active": False,
                            "phase": "stopped"
                        }
                    }
                    r.set(PTZ_STATUS_KEY, json.dumps(status))
                    continue
                
                # ============= 全面扫描命令（遍历所有点位，每个点位做完整扫描）=============
                if action == "start_full_scan_whitelist_refinement":
                    refinement_scan_id = str(command.get("scan_id") or "")
                    source_round = int(command.get("round_index"))
                    image_context = command.get("image_context") or {}
                    scan_mode = command.get("mode") or image_context.get("mode")
                    target_macs = command.get("target_macs") or []
                    refinement_summary = None
                    round_payload = None
                    whitelist_payload = None
                    final_state = "done"
                    final_reason = None
                    try:
                        raw_round = r.get(
                            f"full_scan:round_{source_round}_results"
                        )
                        raw_whitelist = r.get(
                            f"full_scan:whitelist:round_{source_round}"
                        )
                        if not raw_round:
                            raise ValueError(
                                f"缺少轮次原始结果 full_scan:round_{source_round}_results"
                            )
                        if not raw_whitelist:
                            raise ValueError(
                                f"缺少轮次白名单 full_scan:whitelist:round_{source_round}"
                            )
                        round_payload = json.loads(raw_round)
                        whitelist_payload = json.loads(raw_whitelist)
                        round_results = round_payload.get("results") or {}
                        if not isinstance(round_results, dict):
                            raise ValueError("轮次 results 格式错误")
                        if not (
                            isinstance(whitelist_payload, dict)
                            and whitelist_payload.get("mac_whitelist")
                        ):
                            raise ValueError("指定轮次白名单为空")
                        if scan_mode not in ("panorama", "single"):
                            raise ValueError("复核缺少有效 mode/image_context")

                        r.delete(
                            "full_scan:stop",
                            "multi_scan:stop_full_area_scan",
                        )
                        r.setex(
                            "full_scan:active_scan_id",
                            86400,
                            refinement_scan_id,
                        )
                        patch_ptz_status(r, {
                            "full_scan": {
                                "active": True,
                                "state": "running",
                                "terminal": False,
                                "stop_requested": False,
                                "scan_id": refinement_scan_id,
                                "round": source_round,
                                "phase": "whitelist_refinement",
                                "refinement_only": True,
                                "source_round": source_round,
                            }
                        })
                        _raw_range = business_scan_range_from_redis(
                            r,
                            {
                                "pan_min": PAN_MIN,
                                "pan_max": PAN_MAX,
                                "tilt_min": TILT_MIN,
                                "tilt_max": TILT_MAX,
                            },
                            {
                                "pan_min": PTZ_DEFAULT_PAN_MIN,
                                "pan_max": PTZ_DEFAULT_PAN_MAX,
                                "tilt_min": PTZ_DEFAULT_TILT_MIN,
                                "tilt_max": PTZ_DEFAULT_TILT_MAX,
                                "pan_step": PTZ_DEFAULT_PAN_STEP,
                                "tilt_step": PTZ_DEFAULT_TILT_STEP,
                            },
                        )
                        move_range = {
                            "pan_range": [_raw_range["pan_min"], _raw_range["pan_max"]],
                            "tilt_range": [_raw_range["tilt_min"], _raw_range["tilt_max"]],
                        }
                        refinement_summary = _run_full_scan_whitelist_refinement(
                            r=r,
                            ptz=ptz,
                            scan_id=refinement_scan_id,
                            whitelist_payload=whitelist_payload,
                            round_results=round_results,
                            scan_mode=scan_mode,
                            image_context=image_context,
                            move_range=move_range,
                            stop_check_fn=lambda: bool(
                                _full_scan_stop_reason_from_redis(
                                    r,
                                    refinement_scan_id,
                                )
                            ),
                            target_macs=target_macs,
                            target_ranges=round_payload.get("target_ranges"),
                        )
                        if refinement_summary.get("status") == "stopped":
                            final_state = "stopped"
                            final_reason = "manual_stop"
                    except Exception as refinement_exc:
                        final_state = "error"
                        final_reason = str(refinement_exc)
                        refinement_summary = {
                            "status": "failed",
                            "reason": final_reason,
                        }
                        logger.exception(
                            "❌ 已有白名单独立复核失败: "
                            f"{refinement_exc}"
                        )
                    finally:
                        if (
                            isinstance(round_payload, dict)
                            and isinstance(refinement_summary, dict)
                        ):
                            round_payload["whitelist_refinement"] = (
                                refinement_summary
                            )
                            r.set(
                                f"full_scan:round_{source_round}_results",
                                json.dumps(
                                    round_payload,
                                    ensure_ascii=False,
                                ),
                            )
                        if (
                            isinstance(whitelist_payload, dict)
                            and isinstance(refinement_summary, dict)
                        ):
                            whitelist_payload["refinement_summary"] = (
                                refinement_summary
                            )
                            _write_full_scan_whitelist_result(
                                r,
                                whitelist_payload,
                            )
                        try:
                            _full_scan_refinement_command(
                                r,
                                refinement_scan_id,
                                "stop_refinement_session",
                                timeout=0.8,
                                timeout_seconds=0.6,
                            )
                        except Exception:
                            pass
                        state = "IDLE"
                        pan, tilt = ptz.get_position()
                        patch_ptz_status(r, {
                            "position": {
                                "pan": round_angle_to_2dp(pan),
                                "tilt": round_angle_to_2dp(tilt),
                            },
                            "state": state,
                            "full_scan": {
                                "active": False,
                                "state": final_state,
                                "terminal": True,
                                "stop_requested": False,
                                "reason": final_reason,
                                "phase": (
                                    "stopped"
                                    if final_state == "stopped"
                                    else (
                                        "error"
                                        if final_state == "error"
                                        else "completed"
                                    )
                                ),
                                "refinement_only": True,
                                "source_round": source_round,
                                "refinement": refinement_summary,
                            },
                        })
                        if r.get("full_scan:active_scan_id") == refinement_scan_id:
                            r.delete("full_scan:active_scan_id")
                        _finalize_current_project(
                            r,
                            expected_scan_type="full_area",
                            status=(
                                "FAILED"
                                if final_state == "error"
                                else (
                                    "STOPPED"
                                    if final_state == "stopped"
                                    else "COMPLETED"
                                )
                            ),
                        )
                    continue

                if action == 'start_full_area_scan':
                    """
                    全面扫描命令（优化版：粗扫/细扫/偏差区三段）

                    阶段一：外部大范围粗扫（precheck_range，30-42核心点+8边界点，全信道）
                    阶段二：目标区细扫（work_ranges，每区20-30核心点+8边界点，单次稳定网格）
                    阶段三：偏差区外扩环扫描（4等分中间3层，每层12点，对外 phase=deviation_a）

                    参数：
                        - precheck_range: 初筛范围，形如 {"pan_range": [...], "tilt_range": [...]}
                        - work_ranges: 目标区范围列表，每项形如 {"pan_range": [...], "tilt_range": [...]}
                        - pan_range/tilt_range: 兼容旧单工作范围输入
                        - dwell_time: 每个点位每个信道/带宽配置的监听时长（秒）
                        - configs: API 启动时生成并锁定的全面扫描任务配置
                        - wifi_mode: "2.4" / "5" / null，仅用于缺少 configs 的兼容回退
                        - scan_time_limit: 可选，扫描窗口时长（分钟）
                        - time_interval: 可选，扫描窗口之间的等待间隔（秒）

                    校准：每轮开始时去限位点校准一次，轮内各点位不再重复校准。
                    """
                    logger.info("🗺️ 收到全面扫描命令（三段优化版）")

                    # ── 解析参数 ──────────────────────────────────────────────────
                    # 三个阶段 dwell_time 各自独立；若前端只传 dwell_time，则三段统一使用该值
                    _cmd_dwell = command.get('dwell_time')  # 通用覆盖（兼容旧前端）
                    try:
                        coarse_dwell_time = float(
                            command.get('coarse_dwell_time', _cmd_dwell)
                            if command.get('coarse_dwell_time', _cmd_dwell) is not None
                            else FULL_SCAN_COARSE_DWELL_TIME
                        )
                    except (TypeError, ValueError):
                        coarse_dwell_time = float(FULL_SCAN_COARSE_DWELL_TIME)
                    try:
                        fine_dwell_time = float(
                            command.get('fine_dwell_time', _cmd_dwell)
                            if command.get('fine_dwell_time', _cmd_dwell) is not None
                            else FULL_SCAN_FINE_DWELL_TIME
                        )
                    except (TypeError, ValueError):
                        fine_dwell_time = float(FULL_SCAN_FINE_DWELL_TIME)
                    try:
                        deviation_dwell_time = float(
                            command.get('deviation_dwell_time', _cmd_dwell)
                            if command.get('deviation_dwell_time', _cmd_dwell) is not None
                            else FULL_SCAN_DEVIATION_DWELL_TIME
                        )
                    except (TypeError, ValueError):
                        deviation_dwell_time = float(FULL_SCAN_DEVIATION_DWELL_TIME)
                    full_scan_dwell_times = {
                        "coarse_dwell_time": coarse_dwell_time,
                        "fine_dwell_time": fine_dwell_time,
                        "deviation_dwell_time": deviation_dwell_time,
                    }
                    dwell_time_source = "request" if _cmd_dwell is not None else "config"
                    scan_time_limit = _parse_optional_positive_float(command.get('scan_time_limit'))
                    time_interval   = _parse_optional_nonnegative_float(command.get('time_interval'))
                    scan_time_limit_seconds = scan_time_limit * 60 if scan_time_limit is not None else None
                    _full_scan_id = command.get('scan_id') or _generate_scan_id("full")
                    internal_minimal_point_test = _full_scan_bool(command.get("test_minimal_points"), False)
                    if internal_minimal_point_test and not FULL_SCAN_ALLOW_MINIMAL_POINT_TEST:
                        logger.error("❌ 最小点位内部测试未启用，拒绝执行全面扫描命令")
                        patch_ptz_status(r, {
                            "state": "IDLE",
                            "full_scan": {
                                "active": False,
                                "state": "error",
                                "terminal": True,
                                "stop_requested": False,
                                "scan_id": _full_scan_id,
                                "phase": "error",
                                "reason": "minimal_point_test_disabled",
                            },
                        })
                        try:
                            if r.get("full_scan:active_scan_id") == _full_scan_id:
                                r.delete("full_scan:active_scan_id")
                        except Exception:
                            pass
                        _finalize_current_project(
                            r,
                            expected_scan_type="full_area",
                            status="FAILED",
                        )
                        continue
                    try:
                        scan_mode = command.get("mode")
                        image_context = command.get("image_context")
                        target_ranges = command.get("target_ranges")
                        if not isinstance(target_ranges, list) or len(target_ranges) != 1:
                            raise ValueError("当前全面扫描只支持一个 target_range")
                        outer_pixel_range = {
                            "x_range": list(command["work_x_range"]),
                            "y_range": list(command["work_y_range"]),
                        }
                        target_pixel_range = {
                            "x_range": list(target_ranges[0]["x_range"]),
                            "y_range": list(target_ranges[0]["y_range"]),
                        }
                        pixel_execution_plan = _build_full_scan_pixel_execution_plan(
                            scan_mode,
                            image_context,
                            outer_pixel_range,
                            target_pixel_range,
                        )
                        full_coarse_entries_for_range = pixel_execution_plan["stages"]["coarse"]
                        full_fine_entries_for_range = pixel_execution_plan["stages"]["fine"]
                        if internal_minimal_point_test:
                            pixel_execution_plan = _apply_full_scan_minimal_test_execution_plan(
                                pixel_execution_plan
                            )
                            logger.warning(
                                "🧪 全面扫描最小点位内部测试已启用: "
                                f"original={pixel_execution_plan.get('original_stage_counts')}, "
                                f"limited={pixel_execution_plan.get('limited_stage_counts')}"
                            )
                        coarse_entries = pixel_execution_plan["stages"]["coarse"]
                        fine_entries = pixel_execution_plan["stages"]["fine"]
                        deviation_entries = pixel_execution_plan["stages"]["deviation_a"]
                        _log_full_scan_execution_plan(
                            logger,
                            pixel_execution_plan,
                            config_priority=FULL_SCAN_CONFIG_PRIORITY_COLLECT,
                        )
                        _plan_counts = _full_scan_count_snapshot(pixel_execution_plan)
                        _patch_full_scan_counts(r, {
                            **_plan_counts,
                            "total_points": _plan_counts["converted_point_count"],
                            "current_point": 0,
                        })
                        precheck_range = _full_scan_angle_range_from_entries(full_coarse_entries_for_range)
                        target_angle_range = _full_scan_angle_range_from_entries(full_fine_entries_for_range)
                        if not precheck_range or not target_angle_range:
                            raise ValueError("像素路径没有可执行的粗扫或细扫角度点")
                        work_ranges = [target_angle_range]
                        for _stage_name, _skipped_items in pixel_execution_plan["skipped"].items():
                            if _skipped_items:
                                _reason_counts = {}
                                for _item in _skipped_items:
                                    _reason = _item.get("reason", "unknown")
                                    _reason_counts[_reason] = _reason_counts.get(_reason, 0) + 1
                                logger.warning(
                                    f"⚠️ 全面扫描像素路径 {_stage_name} 跳过 "
                                    f"{len(_skipped_items)} 点: {_reason_counts}"
                                )
                    except Exception as _pixel_plan_error:
                        logger.error(f"❌ 全面扫描像素路径初始化失败: {_pixel_plan_error}")
                        patch_ptz_status(r, {
                            "state": "IDLE",
                            "full_scan": {
                                "active": False,
                                "state": "error",
                                "terminal": True,
                                "stop_requested": False,
                                "scan_id": _full_scan_id,
                                "phase": "error",
                                "reason": f"pixel_path_error: {_pixel_plan_error}",
                            },
                        })
                        try:
                            if r.get("full_scan:active_scan_id") == _full_scan_id:
                                r.delete("full_scan:active_scan_id")
                        except Exception:
                            pass
                        _finalize_current_project(
                            r,
                            expected_scan_type="full_area",
                            status="FAILED",
                        )
                        continue

                    # ── 计算任务级固定参数（整个任务内不变） ────────────────────
                    fine_step      = _calc_fine_scan_step(work_ranges)
                    task_full_scan_configs = _dedupe_full_scan_configs(command.get('configs'))
                    if not task_full_scan_configs:
                        _, task_full_scan_configs = build_full_scan_wifi_configs(
                            command.get('wifi_mode')
                        )
                    configs_count = len(task_full_scan_configs)
                    full_scan_filter_config = _get_full_scan_filter_config_fn()

                    # 天线偏差补偿：从命令读取 antenna_bias
                    _fs_ab = command.get('antenna_bias')
                    if _fs_ab and not _fs_ab.get('enabled'):
                        _fs_ab = None
                    _fs_direct_pan_range, _fs_direct_tilt_range = visual_range_to_rf(
                        precheck_range["pan_range"],
                        precheck_range["tilt_range"],
                        _fs_ab,
                    )
                    _fs_direct_move_range = {
                        "pan_range": _fs_direct_pan_range,
                        "tilt_range": _fs_direct_tilt_range,
                    }

                    # A.2 三档策略: 计算本轮全局统一面积比例
                    round_dev_area_ratio = min(
                        (_calc_dev_area_ratio(wr, precheck_range) for wr in work_ranges),
                        default=1.0
                    )
                    is_large_area_mode = (round_dev_area_ratio < FULL_SCAN_DEV_RATIO_LARGE_MAX)

                    # S1 采样点预计算（估算，实际路径在轮次内确定）
                    _est_coarse = [(item["pan"], item["tilt"]) for item in coarse_entries]
                    _est_fine = [(item["pan"], item["tilt"]) for item in fine_entries]
                    _est_dev_a = [(item["pan"], item["tilt"]) for item in deviation_entries]
                    _full_all_path  = _est_coarse + _est_fine + _est_dev_a
                    _full_sampling = (
                        select_sampling_points(_full_all_path)
                        if FULL_SCAN_SAMPLING_CAPTURE_ENABLED
                        else []
                    )
                    _full_sampling_set = set(_full_sampling)
                    logger.info(
                        "📷 全面扫描采样截图: "
                        f"{'已启用' if FULL_SCAN_SAMPLING_CAPTURE_ENABLED else '已关闭'}"
                    )

                    logger.info(
                        f"🗺️ 三段扫描参数: 细扫步径={fine_step}°, "
                        f"估算粗扫={len(_est_coarse)}点, "
                        f"估算单次细扫={len(_est_fine)}点, "
                        f"估算偏差区A={len(_est_dev_a)}点"
                    )
                    logger.info(
                        f"⏱️ 全面扫描时间配置: scan_time_limit={scan_time_limit}分钟, "
                        f"time_interval={time_interval}秒, "
                        f"dwell_source={dwell_time_source}, "
                        f"dwell_times={json.dumps(full_scan_dwell_times, ensure_ascii=False)}"
                    )
                    logger.info(
                        "🔎 全面扫描筛选配置: "
                        f"{json.dumps(full_scan_filter_config, ensure_ascii=False)}"
                    )
                    # ── 清理旧运行结果 ────────────────────────────────────────────
                    try:
                        _clear_full_scan_runtime_keys(r, logger=logger)
                    except Exception as e:
                        logger.warning(f"⚠️ 清理旧运行结果失败: {e}")
                    # stop key 的换代由 Web 启动事务负责。worker 接手后不得再删，
                    # 否则会吞掉“入队后、worker 开始前”到达的早期 stop。

                    # ══════════════════════════════════════════════════════════════
                    # 时间窗口主循环
                    # ══════════════════════════════════════════════════════════════
                    full_scan_round   = 0
                    window_index      = 0
                    final_project_status = "STOPPED"
                    stop_all_windows  = False
                    prev_whitelist_count = 0   # 上轮白名单数量（供细扫次数动态调整使用）
                    prev_known_configs = []
                    prev_remaining_configs = list(task_full_scan_configs)
                    last_round_payload = None
                    config_session_cleanup_failed = False
                    timing_trace = FullScanTimingTrace(enabled=False)
                    latched_stop_reason = None

                    # scan_id 任务级生成（由 API 层通过 command 传入），整个任务生命周期复用
                    r.setex('full_scan:active_scan_id', 86400, _full_scan_id)

                    def _full_scan_stop_reason():
                        nonlocal latched_stop_reason
                        if latched_stop_reason:
                            return latched_stop_reason
                        observed_reason = _full_scan_stop_reason_from_redis(
                            r,
                            _full_scan_id,
                        )
                        if observed_reason:
                            latched_stop_reason = observed_reason
                        return latched_stop_reason

                    def _full_scan_stop_deadline():
                        """手动停止使用 API 写入时间计算统一的两秒端到端截止时间。"""
                        try:
                            raw = r.get('full_scan:stop')
                            payload = json.loads(raw) if raw else {}
                            if (
                                isinstance(payload, dict)
                                and payload.get('scan_id') == _full_scan_id
                                and payload.get('reason') == 'manual_stop'
                            ):
                                return float(payload.get('ts')) + 2.0
                        except (TypeError, ValueError, json.JSONDecodeError):
                            pass
                        return None

                    def _fs_stopped():
                        return bool(_full_scan_stop_reason())

                    def _full_scan_sleep_with_stop(duration, context, deadline_ts=None):
                        deadline = time.time() + max(0.0, float(duration or 0))
                        while time.time() < deadline:
                            stop_reason = _full_scan_stop_reason()
                            if stop_reason:
                                logger.warning(f"🛑 [{context}] 等待期间检测到停止标志: {stop_reason}")
                                return stop_reason
                            if _deadline_reached(deadline_ts):
                                logger.warning(f"⏱️ [{context}] 等待期间扫描窗口时间已到")
                                return "time_limit"
                            time.sleep(min(0.2, max(0.0, deadline - time.time())))
                        return None

                    def _wait_full_scan_point_notify(notify_key, timeout, context):
                        wait_started_at = time.time()
                        observed_stop_reason = None
                        stop_grace_until = None
                        while time.time() - wait_started_at < timeout:
                            stop_reason = _full_scan_stop_reason()
                            if stop_reason and observed_stop_reason is None:
                                observed_stop_reason = stop_reason
                                # 手动停止不再等待当前点位结果；旧 scan_id 的迟到结果会被隔离。
                                if stop_reason == "manual_stop":
                                    return None, observed_stop_reason
                                stop_grace_until = time.time() + 2.0
                                logger.warning(
                                    f"🛑 [{context}] 等待抓包收尾通知: {stop_reason}"
                                )
                            if (
                                observed_stop_reason is None
                                and _deadline_reached(window_deadline_at)
                            ):
                                observed_stop_reason = "time_limit"
                                stop_grace_until = time.time() + 10.0
                                r.set(
                                    'multi_scan:stop_full_area_scan',
                                    'time_limit',
                                    ex=120,
                                )
                                logger.warning(f"⏱️ [{context}] 扫描窗口时间已到，等待抓包收尾通知")

                            result = r.rpop(notify_key)
                            if result is not None:
                                try:
                                    return json.loads(result), observed_stop_reason
                                except Exception:
                                    return {
                                        "status": "error",
                                        "reason": "invalid_capture_notify",
                                        "mac_count": 0,
                                        "macs": {},
                                    }, observed_stop_reason
                            if stop_grace_until and time.time() >= stop_grace_until:
                                break
                            time.sleep(0.05)
                        return None, observed_stop_reason

                    def _stop_config_session_and_record(session_id, reason, context):
                        nonlocal config_session_cleanup_failed
                        stop_started = timing_trace.start()
                        stop_deadline = _full_scan_stop_deadline()
                        stop_timeout = 1.0
                        if stop_deadline is not None:
                            stop_timeout = max(0.0, min(stop_timeout, stop_deadline - time.time()))
                        payload = _stop_full_scan_config_capture_session(
                            r,
                            scan_id=_full_scan_id,
                            session_id=session_id,
                            reason=reason,
                            timeout=stop_timeout,
                            max_attempts=1,
                        )
                        timing_trace.finish(
                            "config_session_stop",
                            stop_started,
                            strategy="config_priority",
                            phase="coarse" if "粗扫" in context else "fine",
                            success=_full_scan_config_session_stop_confirmed(payload),
                            reason=payload.get("reason") if isinstance(payload, dict) else None,
                        )
                        if not _full_scan_config_session_stop_confirmed(payload):
                            config_session_cleanup_failed = True
                            if reason == "manual_stop" and stop_deadline is not None:
                                try:
                                    r.set(
                                        'manager:restart_capture_worker',
                                        json.dumps({
                                            'scan_id': _full_scan_id,
                                            'reason': 'full_scan_stop_timeout',
                                            'ts': time.time(),
                                        }),
                                        ex=10,
                                    )
                                except Exception:
                                    pass
                            logger.error(
                                f"❌ [{context}] 配置采集会话未确认停止: {payload}"
                            )
                        return payload

                    def _new_round_summary(round_index, round_id, round_started_at):
                        if not FULL_SCAN_TEST_SUMMARY_ENABLED:
                            return None
                        return {
                            "round": round_index,
                            "round_id": round_id,
                            "round_started_at": round_started_at,
                            "outer_pixel_range": outer_pixel_range,
                            "target_ranges": [target_pixel_range],
                            "stages": [],
                        }

                    def _start_stage_summary(round_summary, plan_payload):
                        if not round_summary or not isinstance(plan_payload, dict):
                            return None
                        stage = dict(plan_payload)
                        stage["_started_ts"] = time.time()
                        stage["stage_started_at"] = local_now_iso()
                        round_summary.setdefault("stages", []).append(stage)
                        return stage

                    def _finish_stage_summary(stage_summary, status="completed", completed_count=None,
                                             skip_reason=None):
                        if not stage_summary:
                            return
                        finished_ts = time.time()
                        started_ts = stage_summary.pop("_started_ts", None)
                        stage_summary["stage_finished_at"] = local_now_iso()
                        if started_ts is not None:
                            stage_summary["duration_seconds"] = round(finished_ts - started_ts, 3)
                        stage_summary["status"] = status
                        if skip_reason:
                            stage_summary["skip_reason"] = skip_reason
                        if completed_count is not None:
                            stage_summary["completed_point_count"] = int(completed_count)
                        duration = stage_summary.get("duration_seconds")
                        completed = stage_summary.get("completed_point_count") or 0
                        planned = stage_summary.get("point_count") or 0
                        if duration is not None and completed > 0:
                            stage_summary["avg_seconds_per_completed_point"] = round(duration / completed, 3)
                        if duration is not None and planned > 0:
                            stage_summary["avg_seconds_per_planned_point"] = round(duration / planned, 3)

                    def _run_coarse_config_priority_stage(_coarse_stage_summary):
                        nonlocal completed_points, executed_point_count, stop_requested, round_stop_reason
                        if not FULL_SCAN_CONFIG_PRIORITY_COLLECT:
                            return

                        grid_items = [
                            (idx, coarse_entries[idx], coarse_path[idx])
                            for idx in range(len(coarse_entries))
                            if coarse_entries[idx].get("point_type") != "outer_probe"
                        ]
                        if not grid_items:
                            return

                        fixed_state_by_point = {}
                        fixed_completed_points = set()

                        for cfg_idx, cfg in enumerate(task_full_scan_configs):
                            if stop_requested:
                                break
                            stop_reason = _full_scan_stop_reason()
                            if stop_reason:
                                stop_requested = True
                                round_stop_reason = stop_reason
                                break
                            if _deadline_reached(window_deadline_at):
                                stop_requested = True
                                round_stop_reason = "time_limit"
                                r.set('multi_scan:stop_full_area_scan', 'time_limit', ex=120)
                                break

                            session_id = f"{_full_round_id}_coarse_cfg_{cfg_idx}"
                            session_start_started = timing_trace.start()
                            start_payload = _start_full_scan_config_capture_session(
                                r,
                                scan_id=_full_scan_id,
                                session_id=session_id,
                                config=cfg,
                                config_order_index=cfg_idx,
                            )
                            timing_trace.finish(
                                "config_session_start",
                                session_start_started,
                                strategy="config_priority",
                                phase="coarse",
                                config_index=cfg_idx,
                                config_count=len(task_full_scan_configs),
                                channel=cfg.get("channel"),
                                bandwidth=cfg.get("bandwidth", "HT20"),
                                success=start_payload.get("status") == "started",
                                reason=start_payload.get("reason") or start_payload.get("message"),
                            )
                            if start_payload.get("status") != "started":
                                logger.error(
                                    "❌ [粗扫配置优先] 启动配置会话失败: "
                                    f"cfg={cfg_idx+1}/{len(task_full_scan_configs)} payload={start_payload}"
                                )
                                _stop_config_session_and_record(
                                    session_id,
                                    "start_failed",
                                    "粗扫配置优先启动失败",
                                )
                                stop_requested = True
                                round_stop_reason = start_payload.get("reason") or start_payload.get("message") or "config_session_start_failed"
                                break

                            stream_key = start_payload.get("stream_key") or _full_scan_config_session_stream_key(session_id)
                            stream_last_id = "0-0"
                            ordered_items = grid_items if cfg_idx % 2 == 0 else list(reversed(grid_items))
                            previous_entry = None
                            previous_point = None
                            logger.info(
                                f"📡 [粗扫配置优先] 配置 {cfg_idx+1}/{len(task_full_scan_configs)} "
                                f"ch{cfg.get('channel')} {cfg.get('bandwidth', 'HT20')} "
                                f"{'正序' if cfg_idx % 2 == 0 else '反序'}遍历 {len(ordered_items)} 个格心点"
                            )

                            try:
                                for step_idx, (point_idx, pixel_entry, point_tuple) in enumerate(ordered_items):
                                    target_pan, target_tilt = point_tuple
                                    point_id = f"coarse_point_{point_idx}"
                                    timing_fields = {
                                        "strategy": "config_priority",
                                        "phase": "coarse",
                                        "point_id": point_id,
                                        "point_type": pixel_entry.get("point_type"),
                                        "config_index": cfg_idx,
                                        "config_count": len(task_full_scan_configs),
                                        "channel": cfg.get("channel"),
                                        "bandwidth": cfg.get("bandwidth", "HT20"),
                                    }
                                    point_cycle_started = timing_trace.start()

                                    stop_reason = _full_scan_stop_reason()
                                    if stop_reason:
                                        stop_requested = True
                                        round_stop_reason = stop_reason
                                        break
                                    if _deadline_reached(window_deadline_at):
                                        stop_requested = True
                                        round_stop_reason = "time_limit"
                                        r.set('multi_scan:stop_full_area_scan', 'time_limit', ex=120)
                                        break

                                    point_started_at = local_now_iso()
                                    _rf_pan, _rf_tilt = target_pan, target_tilt
                                    if _fs_ab:
                                        _rf = visual_point_to_rf({"pan": target_pan, "tilt": target_tilt}, _fs_ab)
                                        _rf_pan, _rf_tilt = _rf["pan"], _rf["tilt"]

                                    move_started = timing_trace.start()
                                    _coarse_move_result = _full_scan_move_point(
                                        ptz,
                                        _rf_pan,
                                        _rf_tilt,
                                        _fs_direct_move_range,
                                        stage_label="粗扫",
                                        execution_index=step_idx + 1,
                                        execution_total=len(ordered_items),
                                        point_id=point_id,
                                        pixel={"x": pixel_entry["px"], "y": pixel_entry["py"]},
                                        stop_check_fn=_fs_stopped,
                                    )
                                    timing_trace.finish(
                                        "move",
                                        move_started,
                                        **timing_fields,
                                        relay_used=bool(_coarse_move_result.get("relay_used")),
                                        move_reason=_coarse_move_result.get("reason"),
                                        target_pan=target_pan,
                                        target_tilt=target_tilt,
                                        success=bool(_coarse_move_result.get("success")),
                                    )
                                    if _coarse_move_result.get("relay_used"):
                                        logger.info(
                                            "↪️ [粗扫配置优先] 直接移动使用一次中继: "
                                            f"{_coarse_move_result.get('relay')}"
                                        )
                                    if not _coarse_move_result.get("success"):
                                        stop_reason = _full_scan_stop_reason()
                                        if stop_reason:
                                            stop_requested = True
                                            round_stop_reason = stop_reason
                                            break
                                        logger.error(
                                            f"❌ [粗扫配置优先] 移动失败: ({target_pan}, {target_tilt}), "
                                            f"reason={_coarse_move_result.get('reason')}"
                                        )
                                        round_results.setdefault(point_id, {
                                            "round_index": full_scan_round,
                                            "round_id": _full_round_id,
                                            "phase": "coarse",
                                            "area_index": None,
                                            "position": {"pan": round(target_pan, 2), "tilt": round(target_tilt, 2)},
                                            "pixel_position": {"x": pixel_entry["px"], "y": pixel_entry["py"]},
                                            "point_type": pixel_entry.get("point_type"),
                                            "movement_status": "failed",
                                            "movement_failure_reason": _coarse_move_result.get("reason"),
                                            "mac_count": 0,
                                            "macs": {},
                                            "evidence_role": "fixed_point",
                                            "sampling_complete": False,
                                            "completion_reason": "movement_failed",
                                            "evidence_role": "fixed_point",
                                            "sampling_complete": False,
                                            "completion_reason": "movement_failed",
                                        })
                                        previous_entry = pixel_entry
                                        previous_point = point_tuple
                                        continue

                                    executed_point_count += 1
                                    settle_started = timing_trace.start()
                                    stop_reason = _full_scan_sleep_with_stop(
                                        0.5,
                                        "粗扫配置优先移动稳定",
                                        deadline_ts=window_deadline_at,
                                    )
                                    timing_trace.finish(
                                        "settle",
                                        settle_started,
                                        **timing_fields,
                                        success=stop_reason is None,
                                        reason=stop_reason,
                                    )
                                    stream_last_id, path_macs = _collect_full_scan_config_session_macs(
                                        r,
                                        stream_key,
                                        stream_last_id,
                                        timing_trace=timing_trace,
                                        timing_fields=timing_fields,
                                    )
                                    if previous_entry is not None and path_macs:
                                        path_started_at = point_started_at
                                        path_finished_at = local_now_iso()
                                        path_id = f"coarse_path_cfg{cfg_idx}_to_{point_idx}"
                                        path_pixel = {
                                            "x": int(round((float(previous_entry["px"]) + float(pixel_entry["px"])) / 2.0)),
                                            "y": int(round((float(previous_entry["py"]) + float(pixel_entry["py"])) / 2.0)),
                                        }
                                        path_position = {
                                            "pan": round((float(previous_point[0]) + float(target_pan)) / 2.0, 2),
                                            "tilt": round((float(previous_point[1]) + float(target_tilt)) / 2.0, 2),
                                        }
                                        for _mac_info in path_macs.values():
                                            if isinstance(_mac_info, dict):
                                                _mac_info.setdefault('first_seen_at', path_started_at or point_started_at)
                                                _mac_info.setdefault('last_seen_at', path_finished_at)
                                        round_results[path_id] = {
                                            "round_index": full_scan_round,
                                            "round_id": _full_round_id,
                                            "phase": "coarse_moving",
                                            "area_index": None,
                                            "position": path_position,
                                            "pixel_position": path_pixel,
                                            "point_type": "path_midpoint",
                                            "path_from_point": previous_entry.get("point_type"),
                                            "path_to_point": pixel_entry.get("point_type"),
                                            "point_started_at": path_started_at,
                                            "point_finished_at": path_finished_at,
                                            "scan_range": {"pan": precheck_range["pan_range"], "tilt": precheck_range["tilt_range"]},
                                            "scan_config": {"channel": cfg.get("channel"), "bandwidth": cfg.get("bandwidth", "HT20")},
                                            "scan_config_index": cfg_idx,
                                            "mac_count": len(path_macs),
                                            "macs": path_macs,
                                            "evidence_role": "path",
                                            "sampling_complete": stop_reason is None,
                                            "completion_reason": stop_reason,
                                        }
                                    if stop_reason:
                                        stop_requested = True
                                        round_stop_reason = stop_reason
                                        break

                                    try:
                                        current_pan, current_tilt = ptz.get_position()
                                    except Exception:
                                        current_pan, current_tilt = target_pan, target_tilt

                                    sample_key = (target_pan, target_tilt)
                                    if sample_key in _full_sampling_set and sample_key not in captured_sampling_points:
                                        captured_sampling_points.add(sample_key)
                                        snapshot_started = timing_trace.start()
                                        snapshot_result = _request_camera_capture(
                                            r, current_pan, current_tilt,
                                            round_id=_full_round_id, scan_type="full_area"
                                        )
                                        timing_trace.finish(
                                            "camera_snapshot",
                                            snapshot_started,
                                            **timing_fields,
                                            attempted=True,
                                            image_success=bool(snapshot_result),
                                            success=bool(snapshot_result),
                                        )

                                    status_started = timing_trace.start()
                                    patch_ptz_status(r, {
                                        'position': {'pan': round(current_pan, 2), 'tilt': round(current_tilt, 2)},
                                        'full_scan': {
                                            'current_point': executed_point_count,
                                            'executed_point_count': executed_point_count,
                                            'phase': "coarse",
                                            'remaining_seconds': _remaining_seconds(window_deadline_at),
                                        },
                                    })
                                    timing_trace.finish(
                                        "status_patch",
                                        status_started,
                                        **timing_fields,
                                        success=True,
                                    )

                                    fixed_started_at = fixed_state_by_point.get(point_id, {}).get("point_started_at") or local_now_iso()
                                    dwell_started = timing_trace.start()
                                    stop_reason = _full_scan_sleep_with_stop(
                                        coarse_dwell_time,
                                        "粗扫配置优先固定采样",
                                        deadline_ts=window_deadline_at,
                                    )
                                    timing_trace.finish(
                                        "fixed_dwell",
                                        dwell_started,
                                        **timing_fields,
                                        success=stop_reason is None,
                                        reason=stop_reason,
                                    )
                                    stream_last_id, fixed_macs = _collect_full_scan_config_session_macs(
                                        r,
                                        stream_key,
                                        stream_last_id,
                                        timing_trace=timing_trace,
                                        timing_fields=timing_fields,
                                    )
                                    point_finished_at = local_now_iso()
                                    state = fixed_state_by_point.setdefault(point_id, {
                                        "point_started_at": fixed_started_at,
                                        "macs": {},
                                        "completed_config_count": 0,
                                        "configs": [],
                                    })
                                    for _mac_info in fixed_macs.values():
                                        if isinstance(_mac_info, dict):
                                            _mac_info.setdefault('first_seen_at', fixed_started_at)
                                            _mac_info.setdefault('last_seen_at', point_finished_at)
                                    state["macs"] = _merge_full_scan_macs(state.get("macs") or {}, fixed_macs)
                                    if stop_reason is None:
                                        state["completed_config_count"] = min(
                                            len(task_full_scan_configs),
                                            int(state.get("completed_config_count") or 0) + 1,
                                        )
                                    state.setdefault("configs", []).append(cfg)
                                    state["point_finished_at"] = point_finished_at

                                    for _mac in fixed_macs:
                                        coarse_macs_set.add(str(_mac).lower())

                                    sampling_complete, completion_reason = _full_scan_evidence_completion(
                                        stop_reason,
                                        state["completed_config_count"] >= len(task_full_scan_configs),
                                    )
                                    point_macs = _append_full_scan_point_marker(
                                        dict(state.get("macs") or {}),
                                        point_started_at=state.get("point_started_at"),
                                        point_finished_at=point_finished_at,
                                        configs=state.get("configs") or [cfg],
                                    )
                                    _coarse_result = {
                                        "round_index": full_scan_round,
                                        "round_id": _full_round_id,
                                        "phase": "coarse",
                                        "area_index": None,
                                        "position": {"pan": round(target_pan, 2), "tilt": round(target_tilt, 2)},
                                        "pixel_position": {"x": pixel_entry["px"], "y": pixel_entry["py"]},
                                        "point_type": pixel_entry.get("point_type"),
                                        "point_started_at": state.get("point_started_at"),
                                        "point_finished_at": point_finished_at,
                                        "scan_range": {"pan": precheck_range["pan_range"], "tilt": precheck_range["tilt_range"]},
                                        "scan_step": {"pan": FULL_SCAN_COARSE_STEP, "tilt": FULL_SCAN_COARSE_STEP},
                                        "scan_config_policy": "config_priority",
                                        "scan_config_count": len(task_full_scan_configs),
                                        "completed_config_count": state["completed_config_count"],
                                        "mac_count": len(point_macs),
                                        "macs": point_macs,
                                        "evidence_role": "fixed_point",
                                        "sampling_complete": sampling_complete,
                                        "completion_reason": completion_reason,
                                    }
                                    if _fs_ab:
                                        _coarse_result["rf_position"] = {
                                            "pan": round(current_pan, 2),
                                            "tilt": round(current_tilt, 2),
                                        }
                                        _coarse_result["antenna_bias"] = {
                                            "pan_bias_deg": _fs_ab["pan_bias_deg"],
                                            "tilt_bias_deg": _fs_ab["tilt_bias_deg"],
                                        }
                                    round_results[point_id] = _coarse_result
                                    if sampling_complete and point_id not in fixed_completed_points:
                                        fixed_completed_points.add(point_id)
                                        completed_points += 1

                                    _write_full_scan_realtime_result(
                                        r,
                                        _build_full_scan_round_payload(
                                            latest_round=full_scan_round,
                                            round_id=_full_round_id,
                                            round_status="RUNNING",
                                            stop_reason=None,
                                            expected_points=total_points,
                                            results=round_results,
                                            scan_time_limit=scan_time_limit,
                                            time_interval=time_interval,
                                            window_index=window_index,
                                            window_started_at=window_started_at,
                                            window_deadline_at=window_deadline_at,
                                            round_started_at=_round_started_at,
                                            timing_trace=timing_trace,
                                            timing_fields={
                                                "strategy": "config_priority",
                                                "phase": "coarse",
                                                "point_id": point_id,
                                                "point_type": pixel_entry.get("point_type"),
                                                "config_index": cfg_idx,
                                                "config_count": len(task_full_scan_configs),
                                                "channel": cfg.get("channel"),
                                                "bandwidth": cfg.get("bandwidth", "HT20"),
                                            },
                                        ),
                                        timing_trace=timing_trace,
                                        timing_fields={
                                            "strategy": "config_priority",
                                            "phase": "coarse",
                                            "point_id": point_id,
                                            "point_type": pixel_entry.get("point_type"),
                                            "config_index": cfg_idx,
                                            "config_count": len(task_full_scan_configs),
                                            "channel": cfg.get("channel"),
                                            "bandwidth": cfg.get("bandwidth", "HT20"),
                                        },
                                    )
                                    logger.info(
                                        f"{'✅' if stop_reason is None else '⏸️'} [粗扫配置优先] "
                                        f"cfg {cfg_idx+1}/{len(task_full_scan_configs)} 点位 {point_id} "
                                        f"{'固定采样完成' if stop_reason is None else '固定采样部分保留'}，累计配置 "
                                        f"{state['completed_config_count']}/{len(task_full_scan_configs)}"
                                    )
                                    timing_trace.finish(
                                        "point_cycle",
                                        point_cycle_started,
                                        **timing_fields,
                                        step_index=step_idx,
                                        is_first_point=step_idx == 0,
                                        is_last_point=step_idx == len(ordered_items) - 1,
                                        sampling_complete=sampling_complete,
                                        success=stop_reason is None,
                                        reason=stop_reason,
                                    )

                                    previous_entry = pixel_entry
                                    previous_point = point_tuple
                                    if stop_reason:
                                        stop_requested = True
                                        round_stop_reason = stop_reason
                                        break
                            finally:
                                _stop_config_session_and_record(
                                    session_id,
                                    round_stop_reason or (
                                        "stopped" if stop_requested else "completed"
                                    ),
                                    "粗扫配置优先",
                                )

                            if stop_requested:
                                break

                    def _run_fine_config_priority(fine_idx, fine_path, _fine_stage_summary):
                        nonlocal completed_points, executed_point_count, stop_requested, round_stop_reason
                        if not FULL_SCAN_CONFIG_PRIORITY_COLLECT:
                            return

                        items = [
                            (idx, fine_entries[idx], fine_path[idx])
                            for idx in range(min(len(fine_entries), len(fine_path)))
                        ]
                        if not items:
                            return

                        # 温启动白名单在配置优先模式下只调整顺序，不能裁掉任务配置。
                        ordered_configs = _dedupe_full_scan_configs(
                            list(prev_known_configs) + list(prev_remaining_configs)
                        )
                        ordered_keys = {
                            _full_scan_config_key(config) for config in ordered_configs
                        }
                        ordered_configs.extend(
                            config for config in task_full_scan_configs
                            if _full_scan_config_key(config) not in ordered_keys
                        )

                        fixed_state_by_point = {}
                        fixed_completed_points = set()

                        for cfg_idx, cfg in enumerate(ordered_configs):
                            if stop_requested:
                                break
                            stop_reason = _full_scan_stop_reason()
                            if stop_reason:
                                stop_requested = True
                                round_stop_reason = stop_reason
                                break
                            if _deadline_reached(window_deadline_at):
                                stop_requested = True
                                round_stop_reason = "time_limit"
                                r.set('multi_scan:stop_full_area_scan', 'time_limit', ex=120)
                                break

                            session_id = f"{_full_round_id}_fine{fine_idx}_cfg_{cfg_idx}"
                            session_start_started = timing_trace.start()
                            start_payload = _start_full_scan_config_capture_session(
                                r,
                                scan_id=_full_scan_id,
                                session_id=session_id,
                                config=cfg,
                                config_order_index=cfg_idx,
                            )
                            timing_trace.finish(
                                "config_session_start",
                                session_start_started,
                                strategy="config_priority",
                                phase="fine",
                                config_index=cfg_idx,
                                config_count=len(ordered_configs),
                                channel=cfg.get("channel"),
                                bandwidth=cfg.get("bandwidth", "HT20"),
                                success=start_payload.get("status") == "started",
                                reason=start_payload.get("reason") or start_payload.get("message"),
                            )
                            if start_payload.get("status") != "started":
                                logger.error(
                                    "❌ [细扫配置优先] 启动配置会话失败: "
                                    f"cfg={cfg_idx+1}/{len(ordered_configs)} payload={start_payload}"
                                )
                                _stop_config_session_and_record(
                                    session_id,
                                    "start_failed",
                                    "细扫配置优先启动失败",
                                )
                                stop_requested = True
                                round_stop_reason = (
                                    start_payload.get("reason")
                                    or start_payload.get("message")
                                    or "config_session_start_failed"
                                )
                                break

                            stream_key = (
                                start_payload.get("stream_key")
                                or _full_scan_config_session_stream_key(session_id)
                            )
                            stream_last_id = "0-0"
                            ordered_items = items if cfg_idx % 2 == 0 else list(reversed(items))
                            previous_entry = None
                            previous_point = None
                            logger.info(
                                f"📡 [细扫配置优先] 配置 {cfg_idx+1}/{len(ordered_configs)} "
                                f"ch{cfg.get('channel')} {cfg.get('bandwidth', 'HT20')} "
                                f"{'正序' if cfg_idx % 2 == 0 else '反序'}遍历 {len(ordered_items)} 个细扫点"
                            )

                            try:
                                for step_idx, (point_idx, pixel_entry, point_tuple) in enumerate(ordered_items):
                                    target_pan, target_tilt = point_tuple
                                    point_id = f"fine_{fine_idx}_point_{point_idx}"
                                    timing_fields = {
                                        "strategy": "config_priority",
                                        "phase": "fine",
                                        "point_id": point_id,
                                        "point_type": pixel_entry.get("point_type"),
                                        "config_index": cfg_idx,
                                        "config_count": len(ordered_configs),
                                        "channel": cfg.get("channel"),
                                        "bandwidth": cfg.get("bandwidth", "HT20"),
                                    }
                                    point_cycle_started = timing_trace.start()
                                    stop_reason = _full_scan_stop_reason()
                                    if stop_reason:
                                        stop_requested = True
                                        round_stop_reason = stop_reason
                                        break
                                    if _deadline_reached(window_deadline_at):
                                        stop_requested = True
                                        round_stop_reason = "time_limit"
                                        r.set('multi_scan:stop_full_area_scan', 'time_limit', ex=120)
                                        break

                                    point_started_at = local_now_iso()
                                    move_started = timing_trace.start()
                                    try:
                                        _rf_pan, _rf_tilt = target_pan, target_tilt
                                        if _fs_ab:
                                            _rf = visual_point_to_rf(
                                                {"pan": target_pan, "tilt": target_tilt},
                                                _fs_ab,
                                            )
                                            _rf_pan, _rf_tilt = _rf["pan"], _rf["tilt"]
                                        move_result = _full_scan_move_point(
                                            ptz,
                                            _rf_pan,
                                            _rf_tilt,
                                            _fs_direct_move_range,
                                            stage_label="细扫",
                                            execution_index=step_idx + 1,
                                            execution_total=len(ordered_items),
                                            point_id=point_id,
                                            pixel={"x": pixel_entry["px"], "y": pixel_entry["py"]},
                                            stop_check_fn=_fs_stopped,
                                        )
                                    except Exception as exc:
                                        move_result = {
                                            "success": False,
                                            "reason": f"move_exception:{exc}",
                                        }
                                    timing_trace.finish(
                                        "move",
                                        move_started,
                                        **timing_fields,
                                        relay_used=bool(move_result.get("relay_used")),
                                        move_reason=move_result.get("reason"),
                                        target_pan=target_pan,
                                        target_tilt=target_tilt,
                                        success=bool(move_result.get("success")),
                                    )

                                    if not move_result.get("success"):
                                        stream_last_id, _discarded = _collect_full_scan_config_session_macs(
                                            r,
                                            stream_key,
                                            stream_last_id,
                                            timing_trace=timing_trace,
                                            timing_fields=timing_fields,
                                        )
                                        stop_reason = _full_scan_stop_reason()
                                        if stop_reason:
                                            stop_requested = True
                                            round_stop_reason = stop_reason
                                            break
                                        logger.error(
                                            f"❌ [细扫配置优先] 移动失败: ({target_pan}, {target_tilt}), "
                                            f"reason={move_result.get('reason')}"
                                        )
                                        state = fixed_state_by_point.setdefault(point_id, {
                                            "point_started_at": point_started_at,
                                            "macs": {},
                                            "completed_config_count": 0,
                                            "configs": [],
                                        })
                                        round_results[point_id] = {
                                            "round_index": full_scan_round,
                                            "round_id": _full_round_id,
                                            "phase": "fine",
                                            "fine_index": fine_idx,
                                            "area_index": None,
                                            "position": {
                                                "pan": round(target_pan, 2),
                                                "tilt": round(target_tilt, 2),
                                            },
                                            "pixel_position": {
                                                "x": pixel_entry["px"],
                                                "y": pixel_entry["py"],
                                            },
                                            "point_type": pixel_entry.get("point_type"),
                                            "movement_status": "failed",
                                            "movement_failure_reason": move_result.get("reason"),
                                            "scan_config_policy": "config_priority",
                                            "scan_config_count": len(ordered_configs),
                                            "completed_config_count": state["completed_config_count"],
                                            "mac_count": len(state["macs"]),
                                            "macs": state["macs"],
                                            "evidence_role": "fixed_point",
                                            "sampling_complete": False,
                                            "completion_reason": "movement_failed",
                                        }
                                        previous_entry = None
                                        previous_point = None
                                        continue

                                    executed_point_count += 1
                                    settle_started = timing_trace.start()
                                    stop_reason = _full_scan_sleep_with_stop(
                                        0.5,
                                        "细扫配置优先移动稳定",
                                        deadline_ts=window_deadline_at,
                                    )
                                    timing_trace.finish(
                                        "settle",
                                        settle_started,
                                        **timing_fields,
                                        success=stop_reason is None,
                                        reason=stop_reason,
                                    )
                                    stream_last_id, path_macs = _collect_full_scan_config_session_macs(
                                        r,
                                        stream_key,
                                        stream_last_id,
                                        timing_trace=timing_trace,
                                        timing_fields=timing_fields,
                                    )
                                    if previous_entry is not None and path_macs:
                                        point_finished_at = local_now_iso()
                                        path_id = (
                                            f"fine_path_{fine_idx}_cfg{cfg_idx}_to_{point_idx}"
                                        )
                                        for mac_info in path_macs.values():
                                            if isinstance(mac_info, dict):
                                                mac_info.setdefault("first_seen_at", point_started_at)
                                                mac_info.setdefault("last_seen_at", point_finished_at)
                                        round_results[path_id] = {
                                            "round_index": full_scan_round,
                                            "round_id": _full_round_id,
                                            "phase": "fine_moving",
                                            "fine_index": fine_idx,
                                            "area_index": None,
                                            "position": {
                                                "pan": round(
                                                    (float(previous_point[0]) + float(target_pan)) / 2.0,
                                                    2,
                                                ),
                                                "tilt": round(
                                                    (float(previous_point[1]) + float(target_tilt)) / 2.0,
                                                    2,
                                                ),
                                            },
                                            "pixel_position": {
                                                "x": int(round(
                                                    (float(previous_entry["px"]) + float(pixel_entry["px"])) / 2.0
                                                )),
                                                "y": int(round(
                                                    (float(previous_entry["py"]) + float(pixel_entry["py"])) / 2.0
                                                )),
                                            },
                                            "point_type": "path_midpoint",
                                            "point_started_at": point_started_at,
                                            "point_finished_at": point_finished_at,
                                            "scan_config": {
                                                "channel": cfg.get("channel"),
                                                "bandwidth": cfg.get("bandwidth", "HT20"),
                                            },
                                            "scan_config_index": cfg_idx,
                                            "mac_count": len(path_macs),
                                            "macs": path_macs,
                                            "evidence_role": "path",
                                            "sampling_complete": stop_reason is None,
                                            "completion_reason": stop_reason,
                                        }
                                    if stop_reason:
                                        stop_requested = True
                                        round_stop_reason = stop_reason
                                        break

                                    try:
                                        current_pan, current_tilt = ptz.get_position()
                                    except Exception:
                                        current_pan, current_tilt = target_pan, target_tilt
                                    sample_key = (target_pan, target_tilt)
                                    if (
                                        sample_key in _full_sampling_set
                                        and sample_key not in captured_sampling_points
                                    ):
                                        captured_sampling_points.add(sample_key)
                                        snapshot_started = timing_trace.start()
                                        snapshot_result = _request_camera_capture(
                                            r,
                                            current_pan,
                                            current_tilt,
                                            round_id=_full_round_id,
                                            scan_type="full_area",
                                        )
                                        timing_trace.finish(
                                            "camera_snapshot",
                                            snapshot_started,
                                            **timing_fields,
                                            attempted=True,
                                            image_success=bool(snapshot_result),
                                            success=bool(snapshot_result),
                                        )
                                    status_started = timing_trace.start()
                                    patch_ptz_status(r, {
                                        "position": {
                                            "pan": round(current_pan, 2),
                                            "tilt": round(current_tilt, 2),
                                        },
                                        "full_scan": {
                                            "current_point": executed_point_count,
                                            "executed_point_count": executed_point_count,
                                            "phase": "fine",
                                            "remaining_seconds": _remaining_seconds(window_deadline_at),
                                        },
                                    })
                                    timing_trace.finish(
                                        "status_patch",
                                        status_started,
                                        **timing_fields,
                                        success=True,
                                    )

                                    fixed_started_at = (
                                        fixed_state_by_point.get(point_id, {}).get("point_started_at")
                                        or local_now_iso()
                                    )
                                    dwell_started = timing_trace.start()
                                    stop_reason = _full_scan_sleep_with_stop(
                                        fine_dwell_time,
                                        "细扫配置优先固定采样",
                                        deadline_ts=window_deadline_at,
                                    )
                                    timing_trace.finish(
                                        "fixed_dwell",
                                        dwell_started,
                                        **timing_fields,
                                        success=stop_reason is None,
                                        reason=stop_reason,
                                    )
                                    stream_last_id, fixed_macs = _collect_full_scan_config_session_macs(
                                        r,
                                        stream_key,
                                        stream_last_id,
                                        timing_trace=timing_trace,
                                        timing_fields=timing_fields,
                                    )
                                    point_finished_at = local_now_iso()
                                    state = fixed_state_by_point.setdefault(point_id, {
                                        "point_started_at": fixed_started_at,
                                        "macs": {},
                                        "completed_config_count": 0,
                                        "configs": [],
                                    })
                                    for mac_info in fixed_macs.values():
                                        if isinstance(mac_info, dict):
                                            mac_info.setdefault("first_seen_at", fixed_started_at)
                                            mac_info.setdefault("last_seen_at", point_finished_at)
                                    state["macs"] = _merge_full_scan_macs(
                                        state.get("macs") or {}, fixed_macs
                                    )
                                    if stop_reason is None:
                                        state["completed_config_count"] += 1
                                    state["configs"].append(cfg)
                                    state["point_finished_at"] = point_finished_at

                                    sampling_complete, completion_reason = _full_scan_evidence_completion(
                                        stop_reason,
                                        state["completed_config_count"] >= len(ordered_configs),
                                    )
                                    point_macs = _append_full_scan_point_marker(
                                        dict(state["macs"]),
                                        point_started_at=state["point_started_at"],
                                        point_finished_at=point_finished_at,
                                        configs=state["configs"],
                                    )
                                    fine_result = {
                                        "round_index": full_scan_round,
                                        "round_id": _full_round_id,
                                        "phase": "fine",
                                        "fine_index": fine_idx,
                                        "area_index": None,
                                        "position": {
                                            "pan": round(target_pan, 2),
                                            "tilt": round(target_tilt, 2),
                                        },
                                        "pixel_position": {
                                            "x": pixel_entry["px"],
                                            "y": pixel_entry["py"],
                                        },
                                        "point_type": pixel_entry.get("point_type"),
                                        "point_started_at": state["point_started_at"],
                                        "point_finished_at": point_finished_at,
                                        "scan_range": {
                                            "pan": work_ranges[0]["pan_range"] if work_ranges else [],
                                            "tilt": work_ranges[0]["tilt_range"] if work_ranges else [],
                                        },
                                        "scan_step": {"pan": fine_step, "tilt": fine_step},
                                        "scan_config_mode": "config_priority",
                                        "scan_config_policy": "config_priority",
                                        "scan_config_count": len(ordered_configs),
                                        "completed_config_count": state["completed_config_count"],
                                        "mac_count": len(point_macs),
                                        "macs": point_macs,
                                        "evidence_role": "fixed_point",
                                        "sampling_complete": sampling_complete,
                                        "completion_reason": completion_reason,
                                    }
                                    if _fs_ab:
                                        fine_result["rf_position"] = {
                                            "pan": round(current_pan, 2),
                                            "tilt": round(current_tilt, 2),
                                        }
                                        fine_result["antenna_bias"] = {
                                            "pan_bias_deg": _fs_ab["pan_bias_deg"],
                                            "tilt_bias_deg": _fs_ab["tilt_bias_deg"],
                                        }
                                    round_results[point_id] = fine_result
                                    if sampling_complete and point_id not in fixed_completed_points:
                                        fixed_completed_points.add(point_id)
                                        completed_points += 1

                                    _write_full_scan_realtime_result(
                                        r,
                                        _build_full_scan_round_payload(
                                            latest_round=full_scan_round,
                                            round_id=_full_round_id,
                                            round_status="RUNNING",
                                            stop_reason=None,
                                            expected_points=total_points,
                                            results=round_results,
                                            scan_time_limit=scan_time_limit,
                                            time_interval=time_interval,
                                            window_index=window_index,
                                            window_started_at=window_started_at,
                                            window_deadline_at=window_deadline_at,
                                            round_started_at=_round_started_at,
                                            timing_trace=timing_trace,
                                            timing_fields={
                                                "strategy": "config_priority",
                                                "phase": "fine",
                                                "point_id": point_id,
                                                "point_type": pixel_entry.get("point_type"),
                                                "config_index": cfg_idx,
                                                "config_count": len(ordered_configs),
                                                "channel": cfg.get("channel"),
                                                "bandwidth": cfg.get("bandwidth", "HT20"),
                                            },
                                        ),
                                        timing_trace=timing_trace,
                                        timing_fields={
                                            "strategy": "config_priority",
                                            "phase": "fine",
                                            "point_id": point_id,
                                            "point_type": pixel_entry.get("point_type"),
                                            "config_index": cfg_idx,
                                            "config_count": len(ordered_configs),
                                            "channel": cfg.get("channel"),
                                            "bandwidth": cfg.get("bandwidth", "HT20"),
                                        },
                                    )
                                    previous_entry = pixel_entry
                                    previous_point = point_tuple
                                    timing_trace.finish(
                                        "point_cycle",
                                        point_cycle_started,
                                        **timing_fields,
                                        step_index=step_idx,
                                        is_first_point=step_idx == 0,
                                        is_last_point=step_idx == len(ordered_items) - 1,
                                        sampling_complete=sampling_complete,
                                        success=stop_reason is None,
                                        reason=stop_reason,
                                    )
                                    if stop_reason:
                                        stop_requested = True
                                        round_stop_reason = stop_reason
                                        break
                            finally:
                                _stop_config_session_and_record(
                                    session_id,
                                    round_stop_reason or (
                                        "stopped" if stop_requested else "completed"
                                    ),
                                    "细扫配置优先",
                                )

                            if stop_requested:
                                break

                    while True:
                        stop_reason = _full_scan_stop_reason()
                        if stop_reason:
                            logger.info(f"🛑 检测到停止标志，退出全面扫描循环: {stop_reason}")
                            break

                        # 上一窗口 time_limit 标志在新窗口前清理
                        try:
                            if _full_scan_stop_reason() == "time_limit":
                                r.delete('multi_scan:stop_full_area_scan')
                        except Exception:
                            pass

                        window_index += 1
                        window_started_at  = time.time()
                        window_deadline_at = (
                            window_started_at + scan_time_limit_seconds
                            if scan_time_limit_seconds is not None else None
                        )
                        if scan_time_limit is not None:
                            logger.info(
                                f"⏱️ 第{window_index}个扫描窗口开始，"
                                f"持续 {scan_time_limit} 分钟，deadline={window_deadline_at:.0f}"
                            )

                        window_stop_reason = None

                        while True:  # 窗口内轮次循环
                            stop_reason = _full_scan_stop_reason()
                            if stop_reason:
                                logger.info(f"🛑 检测到停止标志，退出当前扫描窗口: {stop_reason}")
                                window_stop_reason = stop_reason
                                stop_all_windows = stop_reason == "manual_stop"
                                break
                            if _deadline_reached(window_deadline_at):
                                logger.info("⏱️ 扫描窗口时间已到，结束当前窗口")
                                window_stop_reason = "time_limit"
                                break

                            full_scan_round += 1
                            logger.info(f"🔁 ===== 全面扫描 第 {full_scan_round} 轮 开始 =====")
                            _full_round_id    = f"full_{int(time.time())}"
                            _round_started_at = local_now_iso()
                            timing_trace.close()
                            timing_trace = FullScanTimingTrace(
                                enabled=FULL_SCAN_TIMING_TRACE_ENABLED,
                                directory=FULL_SCAN_TEST_SUMMARY_DIR,
                                scan_id=_full_scan_id,
                                round_id=_full_round_id,
                                strategy=(
                                    "config_priority"
                                    if FULL_SCAN_CONFIG_PRIORITY_COLLECT
                                    else "point_priority"
                                ),
                                logger=logger,
                            )
                            round_summary = _new_round_summary(
                                full_scan_round,
                                _full_round_id,
                                _round_started_at,
                            )
                            logger.info(f"🆔 第{full_scan_round}轮 round_id={_full_round_id}")
                            round_stop_reason = None

                            # ── 计算本轮动态参数 ───────────────────────────────────
                            pan_off, tilt_off    = (0.0, 0.0)
                            fine_count           = 1
                            fine_offsets_list    = [(0.0, 0.0)]
                            coarse_path = [(item["pan"], item["tilt"]) for item in coarse_entries]
                            # 方向 B: 温启动判断。粗扫仍保持全信道，细扫优先使用白名单已知信道。
                            whitelist_mac_count, warm_known_configs, warm_remaining_configs = _load_full_scan_warm_start_configs(
                                r,
                                task_full_scan_configs,
                            )
                            if whitelist_mac_count > 0:
                                prev_whitelist_count = whitelist_mac_count
                                prev_known_configs = warm_known_configs
                                prev_remaining_configs = warm_remaining_configs
                            warm_start_mode = (
                                prev_whitelist_count >= FULL_SCAN_WARM_START_THRESHOLD
                                and bool(prev_known_configs)
                            )

                            fine_paths = [[(item["pan"], item["tilt"]) for item in fine_entries]]
                            total_points = len(coarse_path) + sum(len(p) for p in fine_paths)

                            if total_points <= 0:
                                logger.error("❌ 生成扫描路径失败，中止本轮")
                                window_stop_reason = "path_error"
                                break

                            logger.info(
                                f"📍 第{full_scan_round}轮: "
                                f"粗扫={len(coarse_path)}点, "
                                f"细扫={fine_count}次稳定网格"
                                f"[{','.join(str(len(p)) for p in fine_paths)}]点, "
                                f"主扫合计={total_points}点, "
                                f"{'温启动' if warm_start_mode else '冷启动'}"
                            )
                            if warm_start_mode:
                                logger.info(
                                    f"🔥 [温启动] 白名单MAC={prev_whitelist_count}, "
                                    f"已知信道={len(prev_known_configs)}, "
                                    f"剩余信道={len(prev_remaining_configs)}, "
                                    f"剩余信道每 {FULL_SCAN_WARM_REMAINING_INTERVAL} 个细扫点补扫一次"
                                )

                            # ── 每轮校准 ─────────────────────────────────────────────
                            logger.info(f"🎯 第{full_scan_round}轮校准：前往限位点...")
                            if _goto_calibration_point(
                                ptz,
                                r,
                                context=f"全面扫描第{full_scan_round}轮开始",
                                stop_check_fn=_fs_stopped,
                            ):
                                logger.info("✅ 校准完成")
                                stop_reason = _full_scan_sleep_with_stop(1.0, "全面扫描校准稳定")
                                if stop_reason:
                                    window_stop_reason = stop_reason
                                    stop_all_windows = True
                                    try:
                                        pan, tilt = ptz.get_position()
                                    except Exception:
                                        pass
                                    break
                                pan, tilt = ptz.get_position()
                            else:
                                stop_reason = _full_scan_stop_reason()
                                if stop_reason:
                                    logger.warning(f"🛑 全面扫描校准期间检测到停止标志: {stop_reason}")
                                    window_stop_reason = stop_reason
                                    stop_all_windows = True
                                    try:
                                        pan, tilt = ptz.get_position()
                                    except Exception:
                                        pass
                                    break
                                logger.warning("⚠️ 校准失败，继续本轮扫描")

                            # ── 初始化轮次状态 ────────────────────────────────────
                            round_results          = {}
                            completed_points       = 0
                            executed_point_count    = 0
                            stop_requested         = False
                            captured_sampling_points = set()
                            coarse_macs_set        = set()   # 粗扫发现的所有MAC（用于识别new MAC）
                            _initial_counts = _full_scan_count_snapshot(
                                pixel_execution_plan,
                                round_results,
                                executed_point_count=executed_point_count,
                            )

                            pan, tilt = ptz.get_position()
                            patch_ptz_status(r, {
                                "position": {"pan": pan, "tilt": tilt},
                                "state":    "FULL_AREA_SCANNING",
                                "full_scan": {
                                    "active":          True,
                                    "state":           "running",
                                    "scan_id":         _full_scan_id,
                                    "stop_requested":  False,
                                    "terminal":        False,
                                    "round":           full_scan_round,
                                    "round_id":        _full_round_id,
                                    "total_points":    total_points,
                                    "current_point":   0,
                                    **_initial_counts,
                                    "phase":           "coarse",
                                    "area_index":      None,
                                    "scan_time_limit": scan_time_limit,
                                    "scan_time_limit_unit": "minutes",
                                    "time_interval":   time_interval,
                                    "time_interval_unit": "seconds",
                                    "window_index":    window_index,
                                    "window_started_at":  window_started_at,
                                    "window_deadline_at": window_deadline_at,
                                    "remaining_seconds":  _remaining_seconds(window_deadline_at),
                                }
                            })
                            _write_full_scan_realtime_result(
                                r,
                                _build_full_scan_round_payload(
                                    latest_round=full_scan_round,
                                    round_id=_full_round_id,
                                    round_status="RUNNING",
                                    stop_reason=None,
                                    expected_points=total_points,
                                    results=round_results,
                                    scan_time_limit=scan_time_limit,
                                    time_interval=time_interval,
                                    window_index=window_index,
                                    window_started_at=window_started_at,
                                    window_deadline_at=window_deadline_at,
                                    round_started_at=_round_started_at,
                                    timing_trace=timing_trace,
                                    timing_fields={"phase": "coarse"},
                                ),
                                timing_trace=timing_trace,
                                timing_fields={"phase": "coarse"},
                            )
                            logger.info(f"📡 第{full_scan_round}轮状态已写入，开始遍历 {total_points} 个主扫点位")

                            # ================================================
                            # 阶段一：粗扫（整个precheck_range，动态步径，全信道）
                            # ================================================
                            coarse_total = len(coarse_path)
                            fine_total = sum(len(p) for p in fine_paths)
                            dev_a_total = len(deviation_entries)
                            _coarse_core_count = sum(
                                1 for item in coarse_entries if item.get("point_type") == "grid"
                            )
                            _coarse_outer_probe_count = sum(
                                1 for item in coarse_entries if item.get("point_type") == "outer_probe"
                            )
                            logger.info(
                                f"🔎 [阶段一 粗扫] {coarse_total} 个像素点位"
                                f"(格心核心{_coarse_core_count}+外扩探测{_coarse_outer_probe_count})，"
                                f"总点位: 粗扫{coarse_total} + 细扫{fine_total} + 偏差区{dev_a_total}"
                            )
                            _coarse_stage_plan = _log_full_scan_stage_plan(
                                logger,
                                round_index=full_scan_round,
                                round_id=_full_round_id,
                                stage_name="粗扫",
                                phase="coarse",
                                points=coarse_path,
                                dwell_time=coarse_dwell_time,
                                configs=task_full_scan_configs,
                                extra={
                                    "scan_range": {
                                        "x": outer_pixel_range["x_range"],
                                        "y": outer_pixel_range["y_range"],
                                    },
                                    "coordinate_space": "pixel",
                                    "coarse_core_min_points": 12,
                                    "coarse_core_point_count": _coarse_core_count,
                                    "coarse_outer_probe_point_count": _coarse_outer_probe_count,
                                    "config_policy": "config_priority" if FULL_SCAN_CONFIG_PRIORITY_COLLECT else "all_initial_configs",
                                    "internal_test_minimal_points": internal_minimal_point_test,
                                    **(
                                        {
                                            "original_stage_count": pixel_execution_plan.get("original_stage_counts", {}).get("coarse"),
                                            "limited_stage_count": len(coarse_entries),
                                        }
                                        if internal_minimal_point_test else {}
                                    ),
                                },
                                log_enabled=False,
                                include_points=True,
                                include_configs=True,
                            )
                            _coarse_stage_summary = _start_stage_summary(round_summary, _coarse_stage_plan)
                            if FULL_SCAN_CONFIG_PRIORITY_COLLECT:
                                _run_coarse_config_priority_stage(_coarse_stage_summary)

                            for point_idx, (target_pan, target_tilt) in enumerate(coarse_path):
                                if stop_requested:
                                    break
                                pixel_entry = coarse_entries[point_idx]
                                if (
                                    FULL_SCAN_CONFIG_PRIORITY_COLLECT
                                    and pixel_entry.get("point_type") != "outer_probe"
                                ):
                                    continue
                                stop_reason = _full_scan_stop_reason()
                                if stop_reason:
                                    logger.warning(f"🛑 [粗扫] 检测到停止标志: {stop_reason}")
                                    stop_requested = True
                                    round_stop_reason = stop_reason
                                    break
                                if _deadline_reached(window_deadline_at):
                                    logger.warning("⏱️ [粗扫] 扫描窗口时间已到")
                                    stop_requested = True
                                    round_stop_reason = "time_limit"
                                    r.set('multi_scan:stop_full_area_scan', 'time_limit', ex=120)
                                    break

                                point_id        = f"coarse_point_{point_idx}"
                                notify_point_id = f"{_full_round_id}_{point_id}"
                                timing_fields = {
                                    "strategy": "point_priority",
                                    "phase": "coarse",
                                    "point_id": point_id,
                                    "point_type": pixel_entry.get("point_type"),
                                    "config_count": len(task_full_scan_configs),
                                }
                                point_cycle_started = timing_trace.start()
                                logger.info(
                                    f"📍 [粗扫 {point_idx+1}/{coarse_total}] "
                                    f"移动到: ({target_pan}, {target_tilt})，"
                                    f"总进度: {completed_points+1}/{total_points}"
                                )

                                move_started = timing_trace.start()
                                try:
                                    # visual→RF 映射：云台移动使用 RF 坐标
                                    _rf_pan, _rf_tilt = target_pan, target_tilt
                                    if _fs_ab:
                                        _rf = visual_point_to_rf({"pan": target_pan, "tilt": target_tilt}, _fs_ab)
                                        _rf_pan, _rf_tilt = _rf["pan"], _rf["tilt"]
                                    _coarse_move_result = _full_scan_move_point(
                                        ptz,
                                        _rf_pan,
                                        _rf_tilt,
                                        _fs_direct_move_range,
                                        stage_label="粗扫",
                                        execution_index=point_idx + 1,
                                        execution_total=coarse_total,
                                        point_id=point_id,
                                        pixel={"x": pixel_entry["px"], "y": pixel_entry["py"]},
                                        stop_check_fn=_fs_stopped,
                                    )
                                    timing_trace.finish(
                                        "move",
                                        move_started,
                                        **timing_fields,
                                        relay_used=bool(_coarse_move_result.get("relay_used")),
                                        move_reason=_coarse_move_result.get("reason"),
                                        target_pan=target_pan,
                                        target_tilt=target_tilt,
                                        success=bool(_coarse_move_result.get("success")),
                                    )
                                    if not _coarse_move_result.get("success"):
                                        stop_reason = _full_scan_stop_reason()
                                        if stop_reason:
                                            logger.warning(f"🛑 [粗扫] 移动期间收到停止命令: {stop_reason}")
                                            stop_requested = True
                                            round_stop_reason = stop_reason
                                            break
                                        logger.error(
                                            f"❌ [粗扫] 移动失败: ({target_pan}, {target_tilt}), "
                                            f"reason={_coarse_move_result.get('reason')}"
                                        )
                                        round_results[point_id] = {
                                            "round_index": full_scan_round,
                                            "round_id": _full_round_id,
                                            "phase": "coarse",
                                            "area_index": None,
                                            "position": {
                                                "pan": round(target_pan, 2),
                                                "tilt": round(target_tilt, 2),
                                            },
                                            "pixel_position": {
                                                "x": pixel_entry["px"],
                                                "y": pixel_entry["py"],
                                            },
                                            "point_type": pixel_entry.get("point_type"),
                                            "movement_status": "failed",
                                            "movement_failure_reason": _coarse_move_result.get("reason"),
                                            "mac_count": 0,
                                            "macs": {},
                                        }
                                        continue
                                    executed_point_count += 1
                                    settle_started = timing_trace.start()
                                    stop_reason = _full_scan_sleep_with_stop(0.5, "粗扫移动稳定")
                                    timing_trace.finish(
                                        "settle",
                                        settle_started,
                                        **timing_fields,
                                        success=stop_reason is None,
                                        reason=stop_reason,
                                    )
                                    if stop_reason:
                                        stop_requested = True
                                        round_stop_reason = stop_reason
                                        break
                                    current_pan, current_tilt = ptz.get_position()

                                    sample_key = (target_pan, target_tilt)
                                    if sample_key in _full_sampling_set and sample_key not in captured_sampling_points:
                                        captured_sampling_points.add(sample_key)
                                        snapshot_started = timing_trace.start()
                                        snapshot_result = _request_camera_capture(
                                            r,
                                            current_pan,
                                            current_tilt,
                                            round_id=_full_round_id,
                                            scan_type="full_area",
                                        )
                                        timing_trace.finish(
                                            "camera_snapshot",
                                            snapshot_started,
                                            **timing_fields,
                                            attempted=True,
                                            image_success=bool(snapshot_result),
                                            success=bool(snapshot_result),
                                        )

                                    status_started = timing_trace.start()
                                    patch_ptz_status(r, {
                                        'position': {'pan': round(current_pan, 2), 'tilt': round(current_tilt, 2)},
                                        'full_scan': {
                                            'current_point': executed_point_count,
                                            'executed_point_count': executed_point_count,
                                            'phase': "coarse",
                                            'remaining_seconds': _remaining_seconds(window_deadline_at),
                                        },
                                    })
                                    timing_trace.finish(
                                        "status_patch",
                                        status_started,
                                        **timing_fields,
                                        success=True,
                                    )
                                except Exception as e:
                                    logger.error(f"❌ [粗扫] 移动异常: {e}")
                                    continue

                                if _deadline_reached(window_deadline_at):
                                    stop_requested = True
                                    round_stop_reason = "time_limit"
                                    r.set('multi_scan:stop_full_area_scan', 'time_limit', ex=120)
                                    break

                                # 发 MAC 发现命令（全信道）
                                point_started_at = local_now_iso()
                                enqueue_started = timing_trace.start()
                                r.lpush('capture:command_queue', json.dumps({
                                    'action':    'discover_macs_for_full_scan',
                                    'point_id':  notify_point_id,
                                    'configs':   task_full_scan_configs,
                                    'dwell_time': coarse_dwell_time,
                                    'scan_id':   _full_scan_id,
                                    'stop_key':         'full_scan:stop',
                                    'legacy_stop_key':  'multi_scan:stop_full_area_scan',
                                }))
                                timing_trace.finish(
                                    "capture_command_enqueue",
                                    enqueue_started,
                                    **timing_fields,
                                    success=True,
                                )

                                # 等待完成通知
                                per_config_timeout = max(coarse_dwell_time * 2, coarse_dwell_time + 3.0)
                                notify_timeout     = max(90.0, configs_count * per_config_timeout)
                                try:
                                    notify_started = timing_trace.start()
                                    notify_data, stop_reason = _wait_full_scan_point_notify(
                                        f'full_scan:{notify_point_id}_notify',
                                        notify_timeout,
                                        f"粗扫点位{point_id}",
                                    )
                                    timing_trace.finish(
                                        "capture_notify_wait",
                                        notify_started,
                                        **timing_fields,
                                        success=notify_data is not None,
                                        reason=stop_reason or (
                                            None if notify_data is not None else "timeout"
                                        ),
                                    )
                                    if stop_reason:
                                        stop_requested = True
                                        round_stop_reason = round_stop_reason or stop_reason

                                    if notify_data:
                                        sampling_complete, completion_reason = (
                                            _full_scan_notify_evidence_completion(notify_data)
                                        )
                                        point_finished_at = local_now_iso()
                                        mac_count  = notify_data.get('mac_count', 0)
                                        point_macs = notify_data.get('macs', {}) or {}
                                        for _mac_info in point_macs.values():
                                            if isinstance(_mac_info, dict):
                                                _mac_info.setdefault('first_seen_at', point_started_at)
                                                _mac_info.setdefault('last_seen_at',  point_finished_at)
                                        # 记录粗扫发现的MAC集合
                                        for _mac in point_macs:
                                            coarse_macs_set.add(str(_mac).lower())
                                        point_macs = _append_full_scan_point_marker(
                                            point_macs,
                                            point_started_at=point_started_at,
                                            point_finished_at=point_finished_at,
                                            configs=task_full_scan_configs,
                                        )
                                        display_mac_count = len(point_macs)
                                        if sampling_complete:
                                            completed_points += 1
                                        _coarse_result = {
                                            "round_index":      full_scan_round,
                                            "round_id":         _full_round_id,
                                            "phase":            "coarse",
                                            "area_index":       None,
                                            "position":         {"pan": round(target_pan, 2), "tilt": round(target_tilt, 2)},
                                            "pixel_position":   {"x": pixel_entry["px"], "y": pixel_entry["py"]},
                                            "point_type":       pixel_entry.get("point_type"),
                                            "point_started_at": point_started_at,
                                            "point_finished_at": point_finished_at,
                                            "scan_range":       {"pan": precheck_range["pan_range"], "tilt": precheck_range["tilt_range"]},
                                            "scan_step":        {"pan": FULL_SCAN_COARSE_STEP, "tilt": FULL_SCAN_COARSE_STEP},
                                            "mac_count":        display_mac_count,
                                            "macs":             point_macs,
                                            "evidence_role":    "fixed_point",
                                            "sampling_complete": sampling_complete,
                                            "completion_reason": completion_reason,
                                        }
                                        if _fs_ab:
                                            _coarse_result["rf_position"] = {"pan": round(current_pan, 2), "tilt": round(current_tilt, 2)}
                                            _coarse_result["antenna_bias"] = {"pan_bias_deg": _fs_ab["pan_bias_deg"], "tilt_bias_deg": _fs_ab["tilt_bias_deg"]}
                                        round_results[point_id] = _coarse_result
                                        _apply_identity_to_round_results(round_results, notify_data)
                                        _write_full_scan_realtime_result(
                                            r,
                                            _build_full_scan_round_payload(
                                                latest_round=full_scan_round,
                                                round_id=_full_round_id,
                                                round_status="RUNNING",
                                                stop_reason=None,
                                                expected_points=total_points,
                                                results=round_results,
                                                scan_time_limit=scan_time_limit,
                                                time_interval=time_interval,
                                                window_index=window_index,
                                                window_started_at=window_started_at,
                                                window_deadline_at=window_deadline_at,
                                                round_started_at=_round_started_at,
                                                timing_trace=timing_trace,
                                                timing_fields={
                                                    "strategy": "point_priority",
                                                    "phase": "coarse",
                                                    "point_id": point_id,
                                                    "point_type": pixel_entry.get("point_type"),
                                                },
                                            ),
                                            timing_trace=timing_trace,
                                            timing_fields={
                                                "strategy": "point_priority",
                                                "phase": "coarse",
                                                "point_id": point_id,
                                                "point_type": pixel_entry.get("point_type"),
                                            },
                                        )
                                        logger.info(
                                            f"{'✅' if sampling_complete else '⏸️'} [粗扫] "
                                            f"点位{point_id}{'完成' if sampling_complete else '部分保留'}，"
                                            f"发现 {mac_count} 个MAC"
                                        )
                                        timing_trace.finish(
                                            "point_cycle",
                                            point_cycle_started,
                                            **timing_fields,
                                            step_index=point_idx,
                                            is_first_point=point_idx == 0,
                                            is_last_point=point_idx == coarse_total - 1,
                                            sampling_complete=sampling_complete,
                                            success=sampling_complete,
                                            reason=completion_reason,
                                        )
                                    elif not _full_scan_stop_reason():
                                        logger.error(f"❌ [粗扫] 点位{point_id}扫描超时({notify_timeout:.0f}s)")
                                except Exception as e:
                                    logger.error(f"❌ [粗扫] 点位{point_id}异常: {e}")

                                if stop_requested:
                                    break
                            _finish_stage_summary(
                                _coarse_stage_summary,
                                "stopped" if stop_requested else "completed",
                                completed_points,
                            )

                            # ================================================
                            # 阶段二：目标区细扫（动态步径，多次，全信道）
                            # ================================================
                            if not stop_requested and not _full_scan_stop_reason():
                                logger.info(
                                    f"🔎 [阶段二 细扫] {fine_count} 次，步径={fine_step}°，"
                                    f"各次点位: {[len(p) for p in fine_paths]}"
                                )
                                for fine_idx, fine_path in enumerate(fine_paths):
                                    if stop_requested or _full_scan_stop_reason():
                                        break
                                    if _deadline_reached(window_deadline_at):
                                        round_stop_reason = "time_limit"
                                        r.set('multi_scan:stop_full_area_scan', 'time_limit', ex=120)
                                        stop_requested = True
                                        break

                                    off_pan, off_tilt = fine_offsets_list[fine_idx]
                                    fine_path_total = len(fine_path)
                                    logger.info(
                                        f"🔎 [细扫 {fine_idx+1}/{fine_count}] {fine_path_total} 个点位，"
                                        f"偏移=({off_pan:.1f}°,{off_tilt:.1f}°)"
                                    )
                                    _fine_stage_completed_before = completed_points
                                    _fine_stage_plan = _log_full_scan_stage_plan(
                                        logger,
                                        round_index=full_scan_round,
                                        round_id=_full_round_id,
                                        stage_name=f"细扫{fine_idx+1}/{fine_count}",
                                        phase="fine",
                                        points=fine_path,
                                        dwell_time=fine_dwell_time,
                                        extra={
                                            "work_ranges": work_ranges,
                                            "core_point_target": [
                                                FULL_SCAN_FINE_CORE_MIN,
                                                FULL_SCAN_FINE_CORE_MAX,
                                            ],
                                            "boundary_point_count_per_work_range": 0,
                                            "warm_start": (
                                                warm_start_mode
                                                if not FULL_SCAN_CONFIG_PRIORITY_COLLECT
                                                else False
                                            ),
                                            "config_priority_ordered_by_whitelist": (
                                                FULL_SCAN_CONFIG_PRIORITY_COLLECT
                                                and bool(prev_known_configs)
                                            ),
                                            "known_config_count": len(prev_known_configs),
                                            "remaining_config_count": len(prev_remaining_configs),
                                            "remaining_config_interval": (
                                                FULL_SCAN_WARM_REMAINING_INTERVAL
                                                if (
                                                    warm_start_mode
                                                    and not FULL_SCAN_CONFIG_PRIORITY_COLLECT
                                                )
                                                else None
                                            ),
                                            "offset": {
                                                "pan": round(float(off_pan), 2),
                                                "tilt": round(float(off_tilt), 2),
                                            },
                                            "internal_test_minimal_points": internal_minimal_point_test,
                                            **(
                                                {
                                                    "original_stage_count": pixel_execution_plan.get("original_stage_counts", {}).get("fine"),
                                                    "limited_stage_count": len(fine_entries),
                                                }
                                                if internal_minimal_point_test else {}
                                            ),
                                        },
                                        configs=(
                                            _dedupe_full_scan_configs(list(prev_known_configs) + list(prev_remaining_configs))
                                            if warm_start_mode
                                            else task_full_scan_configs
                                        ),
                                        log_enabled=False,
                                        include_points=True,
                                        include_configs=True,
                                    )
                                    _fine_stage_summary = _start_stage_summary(round_summary, _fine_stage_plan)

                                    if FULL_SCAN_CONFIG_PRIORITY_COLLECT:
                                        _run_fine_config_priority(
                                            fine_idx,
                                            fine_path,
                                            _fine_stage_summary,
                                        )
                                        _finish_stage_summary(
                                            _fine_stage_summary,
                                            "stopped" if stop_requested else "completed",
                                            completed_points - _fine_stage_completed_before,
                                        )
                                        if stop_requested:
                                            break
                                        continue

                                    for point_idx, (target_pan, target_tilt) in enumerate(fine_path):
                                        pixel_entry = fine_entries[point_idx]
                                        stop_reason = _full_scan_stop_reason()
                                        if stop_reason:
                                            logger.warning(f"🛑 [细扫] 检测到停止标志: {stop_reason}")
                                            stop_requested = True
                                            round_stop_reason = stop_reason
                                            break
                                        if _deadline_reached(window_deadline_at):
                                            logger.warning("⏱️ [细扫] 扫描窗口时间已到")
                                            stop_requested = True
                                            round_stop_reason = "time_limit"
                                            r.set('multi_scan:stop_full_area_scan', 'time_limit', ex=120)
                                            break

                                        point_id        = f"fine_{fine_idx}_point_{point_idx}"
                                        notify_point_id = f"{_full_round_id}_{point_id}"
                                        timing_fields = {
                                            "strategy": "point_priority",
                                            "phase": "fine",
                                            "point_id": point_id,
                                            "point_type": pixel_entry.get("point_type"),
                                            "config_count": len(task_full_scan_configs),
                                        }
                                        point_cycle_started = timing_trace.start()
                                        logger.info(
                                            f"📍 [细扫{fine_idx+1} {point_idx+1}/{fine_path_total}] "
                                            f"移动到: ({target_pan}, {target_tilt})，"
                                            f"总进度: {completed_points+1}/{total_points}"
                                        )

                                        move_started = timing_trace.start()
                                        try:
                                            # visual→RF 映射
                                            _rf_pan, _rf_tilt = target_pan, target_tilt
                                            if _fs_ab:
                                                _rf = visual_point_to_rf({"pan": target_pan, "tilt": target_tilt}, _fs_ab)
                                                _rf_pan, _rf_tilt = _rf["pan"], _rf["tilt"]
                                            _fine_move_result = _full_scan_move_point(
                                                ptz,
                                                _rf_pan,
                                                _rf_tilt,
                                                _fs_direct_move_range,
                                                stage_label="细扫",
                                                execution_index=point_idx + 1,
                                                execution_total=fine_path_total,
                                                point_id=point_id,
                                                pixel={"x": pixel_entry["px"], "y": pixel_entry["py"]},
                                                stop_check_fn=_fs_stopped,
                                            )
                                            timing_trace.finish(
                                                "move",
                                                move_started,
                                                **timing_fields,
                                                relay_used=bool(_fine_move_result.get("relay_used")),
                                                move_reason=_fine_move_result.get("reason"),
                                                target_pan=target_pan,
                                                target_tilt=target_tilt,
                                                success=bool(_fine_move_result.get("success")),
                                            )
                                            if _fine_move_result.get("relay_used"):
                                                logger.info(
                                                    "↪️ [细扫] 直接移动使用一次中继: "
                                                    f"{_fine_move_result.get('relay')}"
                                                )
                                            if not _fine_move_result.get("success"):
                                                stop_reason = _full_scan_stop_reason()
                                                if stop_reason:
                                                    logger.warning(f"🛑 [细扫] 移动期间收到停止命令: {stop_reason}")
                                                    stop_requested = True
                                                    round_stop_reason = stop_reason
                                                    break
                                                logger.error(
                                                    f"❌ [细扫] 移动失败: ({target_pan}, {target_tilt}), "
                                                    f"reason={_fine_move_result.get('reason')}"
                                                )
                                                round_results[point_id] = {
                                                    "round_index": full_scan_round,
                                                    "round_id": _full_round_id,
                                                    "phase": "fine",
                                                    "area_index": fine_idx,
                                                    "position": {
                                                        "pan": round(target_pan, 2),
                                                        "tilt": round(target_tilt, 2),
                                                    },
                                                    "pixel_position": {
                                                        "x": pixel_entry["px"],
                                                        "y": pixel_entry["py"],
                                                    },
                                                    "movement_status": "failed",
                                                    "movement_failure_reason": _fine_move_result.get("reason"),
                                                    "mac_count": 0,
                                                    "macs": {},
                                                    "evidence_role": "fixed_point",
                                                    "sampling_complete": False,
                                                    "completion_reason": "movement_failed",
                                                }
                                                continue
                                            executed_point_count += 1
                                            settle_started = timing_trace.start()
                                            stop_reason = _full_scan_sleep_with_stop(0.5, "细扫移动稳定")
                                            timing_trace.finish(
                                                "settle",
                                                settle_started,
                                                **timing_fields,
                                                success=stop_reason is None,
                                                reason=stop_reason,
                                            )
                                            if stop_reason:
                                                stop_requested = True
                                                round_stop_reason = stop_reason
                                                break
                                            current_pan, current_tilt = ptz.get_position()

                                            sample_key = (target_pan, target_tilt)
                                            if sample_key in _full_sampling_set and sample_key not in captured_sampling_points:
                                                captured_sampling_points.add(sample_key)
                                                snapshot_started = timing_trace.start()
                                                snapshot_result = _request_camera_capture(
                                                    r,
                                                    current_pan,
                                                    current_tilt,
                                                    round_id=_full_round_id,
                                                    scan_type="full_area",
                                                )
                                                timing_trace.finish(
                                                    "camera_snapshot",
                                                    snapshot_started,
                                                    **timing_fields,
                                                    attempted=True,
                                                    image_success=bool(snapshot_result),
                                                    success=bool(snapshot_result),
                                                )

                                            status_started = timing_trace.start()
                                            patch_ptz_status(r, {
                                                'position': {'pan': round(current_pan, 2), 'tilt': round(current_tilt, 2)},
                                                'full_scan': {
                                                    'current_point': executed_point_count,
                                                    'executed_point_count': executed_point_count,
                                                    'phase': "fine",
                                                    'remaining_seconds': _remaining_seconds(window_deadline_at),
                                                },
                                            })
                                            timing_trace.finish(
                                                "status_patch",
                                                status_started,
                                                **timing_fields,
                                                success=True,
                                            )
                                        except Exception as e:
                                            logger.error(f"❌ [细扫] 移动异常: {e}")
                                            continue

                                        if _deadline_reached(window_deadline_at):
                                            stop_requested = True
                                            round_stop_reason = "time_limit"
                                            r.set('multi_scan:stop_full_area_scan', 'time_limit', ex=120)
                                            break

                                        # 发 MAC 发现命令。冷启动全信道；温启动优先白名单信道，间隔补扫剩余信道。
                                        point_started_at = local_now_iso()
                                        fine_configs, fine_config_mode = _full_scan_fine_configs_for_point(
                                            warm_start_mode,
                                            prev_known_configs,
                                            prev_remaining_configs,
                                            point_idx,
                                            task_full_scan_configs,
                                        )
                                        timing_fields["config_count"] = len(fine_configs)
                                        enqueue_started = timing_trace.start()
                                        r.lpush('capture:command_queue', json.dumps({
                                            'action':    'discover_macs_for_full_scan',
                                            'point_id':  notify_point_id,
                                            'configs':   fine_configs,
                                            'dwell_time': fine_dwell_time,
                                            'scan_id':   _full_scan_id,
                                            'stop_key':         'full_scan:stop',
                                            'legacy_stop_key':  'multi_scan:stop_full_area_scan',
                                        }))
                                        timing_trace.finish(
                                            "capture_command_enqueue",
                                            enqueue_started,
                                            **timing_fields,
                                            success=True,
                                        )
                                        logger.info(
                                            f"📋 [细扫{fine_idx+1}] 点位{point_id} "
                                            f"configs={len(fine_configs)} mode={fine_config_mode}"
                                        )

                                        # 等待完成通知
                                        per_config_timeout = max(fine_dwell_time * 2, fine_dwell_time + 3.0)
                                        notify_timeout     = max(90.0, len(fine_configs) * per_config_timeout)
                                        try:
                                            notify_started = timing_trace.start()
                                            notify_data, stop_reason = _wait_full_scan_point_notify(
                                                f'full_scan:{notify_point_id}_notify',
                                                notify_timeout,
                                                f"细扫点位{point_id}",
                                            )
                                            timing_trace.finish(
                                                "capture_notify_wait",
                                                notify_started,
                                                **timing_fields,
                                                success=notify_data is not None,
                                                reason=stop_reason or (
                                                    None if notify_data is not None else "timeout"
                                                ),
                                            )
                                            if stop_reason:
                                                stop_requested = True
                                                round_stop_reason = round_stop_reason or stop_reason

                                            if notify_data:
                                                sampling_complete, completion_reason = (
                                                    _full_scan_notify_evidence_completion(notify_data)
                                                )
                                                point_finished_at = local_now_iso()
                                                mac_count  = notify_data.get('mac_count', 0)
                                                point_macs = notify_data.get('macs', {}) or {}
                                                for _mac_info in point_macs.values():
                                                    if isinstance(_mac_info, dict):
                                                        _mac_info.setdefault('first_seen_at', point_started_at)
                                                        _mac_info.setdefault('last_seen_at',  point_finished_at)
                                                point_macs = _append_full_scan_point_marker(
                                                    point_macs,
                                                    point_started_at=point_started_at,
                                                    point_finished_at=point_finished_at,
                                                    configs=fine_configs,
                                                )
                                                display_mac_count = len(point_macs)
                                                if sampling_complete:
                                                    completed_points += 1
                                                _fine_result = {
                                                    "round_index":      full_scan_round,
                                                    "round_id":         _full_round_id,
                                                    "phase":            "fine",
                                                    "fine_index":       fine_idx,
                                                    "area_index":       None,
                                                    "position":         {"pan": round(target_pan, 2), "tilt": round(target_tilt, 2)},
                                                    "pixel_position":   {"x": pixel_entry["px"], "y": pixel_entry["py"]},
                                                    "point_type":       pixel_entry.get("point_type"),
                                                    "point_started_at": point_started_at,
                                                    "point_finished_at": point_finished_at,
                                                    "scan_range":       {
                                                        "pan":  work_ranges[0]["pan_range"]  if work_ranges else [],
                                                        "tilt": work_ranges[0]["tilt_range"] if work_ranges else [],
                                                    },
                                                    "scan_step":   {"pan": fine_step, "tilt": fine_step},
                                                    "scan_config_mode": fine_config_mode,
                                                    "scan_config_count": len(fine_configs),
                                                    "mac_count":   display_mac_count,
                                                    "macs":        point_macs,
                                                    "evidence_role":    "fixed_point",
                                                    "sampling_complete": sampling_complete,
                                                    "completion_reason": completion_reason,
                                                }
                                                if _fs_ab:
                                                    _fine_result["rf_position"] = {"pan": round(current_pan, 2), "tilt": round(current_tilt, 2)}
                                                    _fine_result["antenna_bias"] = {"pan_bias_deg": _fs_ab["pan_bias_deg"], "tilt_bias_deg": _fs_ab["tilt_bias_deg"]}
                                                round_results[point_id] = _fine_result
                                                _apply_identity_to_round_results(round_results, notify_data)
                                                _write_full_scan_realtime_result(
                                                    r,
                                                    _build_full_scan_round_payload(
                                                        latest_round=full_scan_round,
                                                        round_id=_full_round_id,
                                                        round_status="RUNNING",
                                                        stop_reason=None,
                                                        expected_points=total_points,
                                                        results=round_results,
                                                        scan_time_limit=scan_time_limit,
                                                        time_interval=time_interval,
                                                        window_index=window_index,
                                                        window_started_at=window_started_at,
                                                        window_deadline_at=window_deadline_at,
                                                        round_started_at=_round_started_at,
                                                        timing_trace=timing_trace,
                                                        timing_fields={
                                                            "strategy": "point_priority",
                                                            "phase": "fine",
                                                            "point_id": point_id,
                                                            "point_type": pixel_entry.get("point_type"),
                                                        },
                                                    ),
                                                    timing_trace=timing_trace,
                                                    timing_fields={
                                                        "strategy": "point_priority",
                                                        "phase": "fine",
                                                        "point_id": point_id,
                                                        "point_type": pixel_entry.get("point_type"),
                                                    },
                                                )
                                                logger.info(
                                                    f"{'✅' if sampling_complete else '⏸️'} "
                                                    f"[细扫{fine_idx+1}] 点位{point_id}"
                                                    f"{'完成' if sampling_complete else '部分保留'}，"
                                                    f"发现 {mac_count} 个MAC"
                                                )
                                                timing_trace.finish(
                                                    "point_cycle",
                                                    point_cycle_started,
                                                    **timing_fields,
                                                    step_index=point_idx,
                                                    is_first_point=point_idx == 0,
                                                    is_last_point=point_idx == fine_path_total - 1,
                                                    sampling_complete=sampling_complete,
                                                    success=sampling_complete,
                                                    reason=completion_reason,
                                                )
                                            elif not _full_scan_stop_reason():
                                                logger.error(f"❌ [细扫{fine_idx+1}] 点位{point_id}扫描超时({notify_timeout:.0f}s)")
                                        except Exception as e:
                                            logger.error(f"❌ [细扫{fine_idx+1}] 点位{point_id}异常: {e}")

                                        if stop_requested:
                                            break
                                    _finish_stage_summary(
                                        _fine_stage_summary,
                                        "stopped" if stop_requested else "completed",
                                        completed_points - _fine_stage_completed_before,
                                    )
                                    if stop_requested:
                                        break

                            # ================================================
                            # 阶段三：偏差区外扩环扫描（对外 phase 统一 deviation_a）
                            # ================================================
                            if not stop_requested and not _full_scan_stop_reason():
                                deviation_macs = _collect_deviation_target_macs(
                                    round_results, work_ranges, full_scan_filter_config,
                                    coarse_macs_set=coarse_macs_set,
                                )
                                dev_a_configs = _build_deviation_configs(deviation_macs, task_full_scan_configs)
                                dev_a_specs = deviation_entries
                                dev_a_points = [
                                    (item["pan"], item["tilt"]) for item in deviation_entries
                                ]
                                if not dev_a_configs:
                                    skip_reason = "偏差区跳过：无目标区出现 MAC 配置"
                                    logger.info(skip_reason)
                                    _dev_stage_plan = _log_full_scan_stage_plan(
                                        logger,
                                        round_index=full_scan_round,
                                        round_id=_full_round_id,
                                        stage_name="偏差区外扩环",
                                        phase="deviation_a",
                                        points=dev_a_points,
                                        dwell_time=deviation_dwell_time,
                                        configs=[],
                                        extra={
                                            "scan_range": {
                                                "x": outer_pixel_range["x_range"],
                                                "y": outer_pixel_range["y_range"],
                                            },
                                            "target_ranges": [target_pixel_range],
                                            "coordinate_space": "pixel",
                                            "division_count": FULL_SCAN_DEVIATION_DIVISIONS,
                                            "scanned_layers": list(range(1, FULL_SCAN_DEVIATION_DIVISIONS)),
                                            "points_per_layer": max(1, int(FULL_SCAN_DEVIATION_POINTS_PER_LAYER)),
                                            "config_source": "target_seen_macs",
                                            "target_mac_count": 0,
                                            "internal_test_minimal_points": internal_minimal_point_test,
                                            **(
                                                {
                                                    "original_stage_count": pixel_execution_plan.get("original_stage_counts", {}).get("deviation_a"),
                                                    "limited_stage_count": len(deviation_entries),
                                                }
                                                if internal_minimal_point_test else {}
                                            ),
                                        },
                                        point_extra_builder=lambda idx, pan, tilt: {
                                            key: value for key, value in (dev_a_specs[idx] if idx < len(dev_a_specs) else {}).items()
                                            if key not in ("pan", "tilt")
                                        },
                                        log_enabled=False,
                                        include_points=True,
                                        include_configs=True,
                                    )
                                    _dev_stage_summary = _start_stage_summary(round_summary, _dev_stage_plan)
                                    _finish_stage_summary(
                                        _dev_stage_summary,
                                        "skipped",
                                        0,
                                        skip_reason=skip_reason,
                                    )
                                elif not dev_a_points:
                                    skip_reason = "偏差区跳过：无可扫点位"
                                    logger.info(skip_reason)
                                    _dev_stage_plan = _log_full_scan_stage_plan(
                                        logger,
                                        round_index=full_scan_round,
                                        round_id=_full_round_id,
                                        stage_name="偏差区外扩环",
                                        phase="deviation_a",
                                        points=[],
                                        dwell_time=deviation_dwell_time,
                                        configs=dev_a_configs,
                                        extra={
                                            "scan_range": {
                                                "x": outer_pixel_range["x_range"],
                                                "y": outer_pixel_range["y_range"],
                                            },
                                            "target_ranges": [target_pixel_range],
                                            "coordinate_space": "pixel",
                                            "division_count": FULL_SCAN_DEVIATION_DIVISIONS,
                                            "scanned_layers": list(range(1, FULL_SCAN_DEVIATION_DIVISIONS)),
                                            "points_per_layer": max(1, int(FULL_SCAN_DEVIATION_POINTS_PER_LAYER)),
                                            "config_source": "target_seen_macs",
                                            "target_mac_count": len(deviation_macs),
                                            "internal_test_minimal_points": internal_minimal_point_test,
                                            **(
                                                {
                                                    "original_stage_count": pixel_execution_plan.get("original_stage_counts", {}).get("deviation_a"),
                                                    "limited_stage_count": len(deviation_entries),
                                                }
                                                if internal_minimal_point_test else {}
                                            ),
                                        },
                                        log_enabled=False,
                                        include_points=True,
                                        include_configs=True,
                                    )
                                    _dev_stage_summary = _start_stage_summary(round_summary, _dev_stage_plan)
                                    _finish_stage_summary(
                                        _dev_stage_summary,
                                        "skipped",
                                        0,
                                        skip_reason=skip_reason,
                                    )
                                else:
                                    total_points += len(dev_a_points)
                                    patch_ptz_status(r, {"full_scan": {"total_points": total_points}})
                                    logger.info(
                                        f"🔎 [偏差区] {len(dev_a_points)} 个外扩环点位，"
                                        f"{len(dev_a_configs)} 个目标区真实 MAC 去重信道配置"
                                    )
                                    _dev_stage_completed_before = completed_points
                                    _dev_stage_plan = _log_full_scan_stage_plan(
                                        logger,
                                        round_index=full_scan_round,
                                        round_id=_full_round_id,
                                        stage_name="偏差区外扩环",
                                        phase="deviation_a",
                                        points=dev_a_points,
                                        dwell_time=deviation_dwell_time,
                                        configs=dev_a_configs,
                                        extra={
                                            "scan_range": {
                                                "x": outer_pixel_range["x_range"],
                                                "y": outer_pixel_range["y_range"],
                                            },
                                            "target_ranges": [target_pixel_range],
                                            "coordinate_space": "pixel",
                                            "division_count": FULL_SCAN_DEVIATION_DIVISIONS,
                                            "scanned_layers": list(range(1, FULL_SCAN_DEVIATION_DIVISIONS)),
                                            "points_per_layer": max(1, int(FULL_SCAN_DEVIATION_POINTS_PER_LAYER)),
                                            "config_source": "target_seen_macs",
                                            "target_mac_count": len(deviation_macs),
                                            "internal_test_minimal_points": internal_minimal_point_test,
                                            **(
                                                {
                                                    "original_stage_count": pixel_execution_plan.get("original_stage_counts", {}).get("deviation_a"),
                                                    "limited_stage_count": len(deviation_entries),
                                                }
                                                if internal_minimal_point_test else {}
                                            ),
                                        },
                                        point_extra_builder=lambda idx, pan, tilt: {
                                            key: value for key, value in (dev_a_specs[idx] if idx < len(dev_a_specs) else {}).items()
                                            if key not in ("pan", "tilt")
                                        },
                                        log_enabled=False,
                                        include_points=True,
                                        include_configs=True,
                                    )
                                    _dev_stage_summary = _start_stage_summary(round_summary, _dev_stage_plan)

                                    for point_idx, (target_pan, target_tilt) in enumerate(dev_a_points):
                                        pixel_entry = deviation_entries[point_idx]
                                        stop_reason = _full_scan_stop_reason()
                                        if stop_reason:
                                            logger.warning(f"🛑 [偏差区] 检测到停止标志: {stop_reason}")
                                            stop_requested = True
                                            round_stop_reason = stop_reason
                                            break
                                        if _deadline_reached(window_deadline_at):
                                            stop_requested = True
                                            round_stop_reason = "time_limit"
                                            r.set('multi_scan:stop_full_area_scan', 'time_limit', ex=120)
                                            break

                                        point_id        = f"dev_a_point_{point_idx}"
                                        notify_point_id = f"{_full_round_id}_{point_id}"
                                        timing_fields = {
                                            "strategy": "point_priority",
                                            "phase": "deviation_a",
                                            "point_id": point_id,
                                            "point_type": pixel_entry.get("point_type"),
                                            "config_count": len(dev_a_configs),
                                        }
                                        point_cycle_started = timing_trace.start()
                                        logger.info(
                                            f"📍 [偏差区 {point_idx+1}/{len(dev_a_points)}] "
                                            f"移动到: ({target_pan}, {target_tilt})，"
                                            f"总进度: {completed_points+1}/{total_points}"
                                        )

                                        move_started = timing_trace.start()
                                        try:
                                            _rf_pan, _rf_tilt = target_pan, target_tilt
                                            if _fs_ab:
                                                _rf = visual_point_to_rf({"pan": target_pan, "tilt": target_tilt}, _fs_ab)
                                                _rf_pan, _rf_tilt = _rf["pan"], _rf["tilt"]
                                            _dev_move_result = _full_scan_move_point(
                                                ptz,
                                                _rf_pan,
                                                _rf_tilt,
                                                _fs_direct_move_range,
                                                stage_label="偏差区",
                                                execution_index=point_idx + 1,
                                                execution_total=len(dev_a_points),
                                                point_id=point_id,
                                                pixel={"x": pixel_entry["px"], "y": pixel_entry["py"]},
                                                stop_check_fn=_fs_stopped,
                                            )
                                            move_success = _dev_move_result.get("success")
                                            timing_trace.finish(
                                                "move",
                                                move_started,
                                                **timing_fields,
                                                relay_used=bool(_dev_move_result.get("relay_used")),
                                                move_reason=_dev_move_result.get("reason"),
                                                target_pan=target_pan,
                                                target_tilt=target_tilt,
                                                success=bool(move_success),
                                            )
                                            if not move_success:
                                                stop_reason = _full_scan_stop_reason()
                                                if stop_reason:
                                                    logger.warning(f"🛑 [偏差区] 移动期间收到停止命令: {stop_reason}")
                                                    stop_requested = True
                                                    round_stop_reason = stop_reason
                                                    break
                                                logger.error(f"❌ [偏差区] 移动失败: ({target_pan}, {target_tilt})")
                                                round_results[point_id] = {
                                                    "round_index": full_scan_round,
                                                    "round_id": _full_round_id,
                                                    "phase": "deviation_a",
                                                    "position": {
                                                        "pan": round(target_pan, 2),
                                                        "tilt": round(target_tilt, 2),
                                                    },
                                                    "pixel_position": {
                                                        "x": pixel_entry["px"],
                                                        "y": pixel_entry["py"],
                                                    },
                                                    "point_type": pixel_entry.get("point_type"),
                                                    "movement_status": "failed",
                                                    "movement_failure_reason": _dev_move_result.get("reason"),
                                                    "mac_count": 0,
                                                    "macs": {},
                                                    "evidence_role": "fixed_point",
                                                    "sampling_complete": False,
                                                    "completion_reason": "movement_failed",
                                                }
                                                continue
                                            executed_point_count += 1
                                            settle_started = timing_trace.start()
                                            stop_reason = _full_scan_sleep_with_stop(0.5, "偏差区移动稳定")
                                            timing_trace.finish(
                                                "settle",
                                                settle_started,
                                                **timing_fields,
                                                success=stop_reason is None,
                                                reason=stop_reason,
                                            )
                                            if stop_reason:
                                                stop_requested = True
                                                round_stop_reason = stop_reason
                                                break
                                            current_pan, current_tilt = ptz.get_position()
                                            status_started = timing_trace.start()
                                            patch_ptz_status(r, {
                                                'position': {'pan': round(current_pan, 2), 'tilt': round(current_tilt, 2)},
                                                'full_scan': {
                                                    'current_point': executed_point_count,
                                                    'executed_point_count': executed_point_count,
                                                    'phase': "deviation_a",
                                                    'remaining_seconds': _remaining_seconds(window_deadline_at),
                                                },
                                            })
                                            timing_trace.finish(
                                                "status_patch",
                                                status_started,
                                                **timing_fields,
                                                success=True,
                                            )
                                        except Exception as e:
                                            logger.error(f"❌ [偏差区] 移动异常: {e}")
                                            continue

                                        point_started_at = local_now_iso()
                                        enqueue_started = timing_trace.start()
                                        r.lpush('capture:command_queue', json.dumps({
                                            'action':    'discover_macs_for_full_scan',
                                            'point_id':  notify_point_id,
                                            'configs':   dev_a_configs,
                                            'dwell_time': deviation_dwell_time,
                                            'scan_id':   _full_scan_id,
                                            'stop_key':         'full_scan:stop',
                                            'legacy_stop_key':  'multi_scan:stop_full_area_scan',
                                        }))
                                        timing_trace.finish(
                                            "capture_command_enqueue",
                                            enqueue_started,
                                            **timing_fields,
                                            success=True,
                                        )

                                        dev_notify_timeout = max(30.0, len(dev_a_configs) * (deviation_dwell_time + 3.0))
                                        try:
                                            notify_started = timing_trace.start()
                                            notify_data, stop_reason = _wait_full_scan_point_notify(
                                                f'full_scan:{notify_point_id}_notify',
                                                dev_notify_timeout,
                                                f"偏差区点位{point_id}",
                                            )
                                            timing_trace.finish(
                                                "capture_notify_wait",
                                                notify_started,
                                                **timing_fields,
                                                success=notify_data is not None,
                                                reason=stop_reason or (
                                                    None if notify_data is not None else "timeout"
                                                ),
                                            )
                                            if stop_reason:
                                                stop_requested = True
                                                round_stop_reason = round_stop_reason or stop_reason

                                            if notify_data:
                                                sampling_complete, completion_reason = (
                                                    _full_scan_notify_evidence_completion(notify_data)
                                                )
                                                point_finished_at = local_now_iso()
                                                mac_count  = notify_data.get('mac_count', 0)
                                                point_macs = notify_data.get('macs', {}) or {}
                                                for _mac_info in point_macs.values():
                                                    if isinstance(_mac_info, dict):
                                                        _mac_info.setdefault('first_seen_at', point_started_at)
                                                        _mac_info.setdefault('last_seen_at',  point_finished_at)
                                                point_macs = _append_full_scan_point_marker(
                                                    point_macs,
                                                    point_started_at=point_started_at,
                                                    point_finished_at=point_finished_at,
                                                    configs=dev_a_configs,
                                                )
                                                display_mac_count = len(point_macs)
                                                if sampling_complete:
                                                    completed_points += 1
                                                _dev_a_result = {
                                                    "round_index":      full_scan_round,
                                                    "round_id":         _full_round_id,
                                                    "phase":            "deviation_a",
                                                    "area_index":       None,
                                                    "position":         {"pan": round(target_pan, 2), "tilt": round(target_tilt, 2)},
                                                    "pixel_position":   {"x": pixel_entry["px"], "y": pixel_entry["py"]},
                                                    "layer":            pixel_entry.get("layer"),
                                                    "side":             pixel_entry.get("side"),
                                                    "narrow_side":      bool(pixel_entry.get("narrow_side")),
                                                    "point_started_at": point_started_at,
                                                    "point_finished_at": point_finished_at,
                                                    "scan_range":       {"pan": precheck_range["pan_range"], "tilt": precheck_range["tilt_range"]},
                                                    "scan_step":        {"pan": FULL_SCAN_COARSE_STEP, "tilt": FULL_SCAN_COARSE_STEP},
                                                    "scan_config_count": len(dev_a_configs),
                                                    "mac_count":        display_mac_count,
                                                    "macs":             point_macs,
                                                    "evidence_role":    "fixed_point",
                                                    "sampling_complete": sampling_complete,
                                                    "completion_reason": completion_reason,
                                                }
                                                if _fs_ab:
                                                    _dev_a_result["rf_position"] = {"pan": round(current_pan, 2), "tilt": round(current_tilt, 2)}
                                                    _dev_a_result["antenna_bias"] = {"pan_bias_deg": _fs_ab["pan_bias_deg"], "tilt_bias_deg": _fs_ab["tilt_bias_deg"]}
                                                round_results[point_id] = _dev_a_result
                                                _apply_identity_to_round_results(round_results, notify_data)
                                                _write_full_scan_realtime_result(
                                                    r,
                                                    _build_full_scan_round_payload(
                                                        latest_round=full_scan_round,
                                                        round_id=_full_round_id,
                                                        round_status="RUNNING",
                                                        stop_reason=None,
                                                        expected_points=total_points,
                                                        results=round_results,
                                                        scan_time_limit=scan_time_limit,
                                                        time_interval=time_interval,
                                                        window_index=window_index,
                                                        window_started_at=window_started_at,
                                                        window_deadline_at=window_deadline_at,
                                                        round_started_at=_round_started_at,
                                                        timing_trace=timing_trace,
                                                        timing_fields={
                                                            "strategy": "point_priority",
                                                            "phase": "deviation_a",
                                                            "point_id": point_id,
                                                            "point_type": pixel_entry.get("point_type"),
                                                        },
                                                    ),
                                                    timing_trace=timing_trace,
                                                    timing_fields={
                                                        "strategy": "point_priority",
                                                        "phase": "deviation_a",
                                                        "point_id": point_id,
                                                        "point_type": pixel_entry.get("point_type"),
                                                    },
                                                )
                                                logger.info(
                                                    f"{'✅' if sampling_complete else '⏸️'} [偏差区] "
                                                    f"点位{point_id}{'完成' if sampling_complete else '部分保留'}，"
                                                    f"发现 {mac_count} 个真实MAC"
                                                )
                                                timing_trace.finish(
                                                    "point_cycle",
                                                    point_cycle_started,
                                                    **timing_fields,
                                                    step_index=point_idx,
                                                    is_first_point=point_idx == 0,
                                                    is_last_point=point_idx == len(dev_a_points) - 1,
                                                    sampling_complete=sampling_complete,
                                                    success=sampling_complete,
                                                    reason=completion_reason,
                                                )
                                        except Exception as e:
                                            logger.error(f"❌ [偏差区] 点位{point_id}异常: {e}")

                                        if stop_requested:
                                            break
                                    _finish_stage_summary(
                                        _dev_stage_summary,
                                        "stopped" if stop_requested else "completed",
                                        completed_points - _dev_stage_completed_before,
                                    )

                            # 下行 Data 关系可能没有同 slice 的客户端 RSSI 项；
                            # 轮次收尾时按 scan_id 快照回填所有已出现客户端。
                            _apply_full_scan_relationship_snapshot(
                                r,
                                _full_scan_id,
                                round_results,
                            )

                            # ── 保存本轮结果（追加，不覆盖历史轮次）────────────────
                            total_macs = sum(
                                _full_scan_real_mac_count(p.get('macs'))
                                for p in round_results.values()
                            )
                            if not round_stop_reason and completed_points < total_points:
                                round_stop_reason = "incomplete"
                            round_status = (
                                "SUCCESS"
                                if completed_points >= total_points and not round_stop_reason
                                else "PARTIAL"
                            )
                            if round_status == "SUCCESS":
                                round_stop_reason = "completed"
                            logger.info(
                                f"💾 第{full_scan_round}轮完成："
                                f"正式完成{completed_points}点，结果记录{len(round_results)}条，"
                                f"共发现 {total_macs} 个 MAC, status={round_status}, reason={round_stop_reason}"
                            )
                            _final_counts = _full_scan_count_snapshot(
                                pixel_execution_plan,
                                round_results,
                                executed_point_count=executed_point_count,
                            )
                            _patch_full_scan_counts(r, {
                                **_final_counts,
                                "total_points": _final_counts["converted_point_count"],
                                "current_point": _final_counts["executed_point_count"],
                            })
                            _round_finished_at = local_now_iso()
                            if round_summary is not None:
                                round_summary["round_finished_at"] = _round_finished_at
                                try:
                                    round_start_dt = datetime.fromisoformat(round_summary["round_started_at"])
                                    round_finish_dt = datetime.fromisoformat(_round_finished_at)
                                    round_summary["total_duration_seconds"] = round(
                                        (round_finish_dt - round_start_dt).total_seconds(),
                                        3,
                                    )
                                except Exception:
                                    pass
                                round_summary["status"] = round_status
                                round_summary["stop_reason"] = round_stop_reason
                                round_summary["expected_points"] = total_points
                                round_summary.update(_final_counts)
                                round_summary["completed_points"] = _final_counts["completed_fixed_point_count"]
                                if round_summary.get("total_duration_seconds") is not None and completed_points > 0:
                                    round_summary["avg_seconds_per_completed_point"] = round(
                                        round_summary["total_duration_seconds"] / completed_points,
                                        3,
                                    )
                            round_payload = _build_full_scan_round_payload(
                                latest_round=full_scan_round,
                                round_id=_full_round_id,
                                round_status=round_status,
                                stop_reason=round_stop_reason,
                                expected_points=total_points,
                                results=round_results,
                                scan_time_limit=scan_time_limit,
                                time_interval=time_interval,
                                window_index=window_index,
                                window_started_at=window_started_at,
                                window_deadline_at=window_deadline_at,
                                round_started_at=_round_started_at,
                                round_finished_at=_round_finished_at,
                                timing_trace=timing_trace,
                                timing_fields={"phase": "round_finalize"},
                                mode=scan_mode,
                                image_context=image_context,
                                work_ranges=work_ranges,
                                target_ranges=[target_pixel_range] if target_pixel_range else None,
                            )
                            round_payload.update(_final_counts)
                            last_round_payload = round_payload

                            whitelist_key   = None
                            whitelist_count = None
                            if round_status == "SUCCESS":
                                try:
                                    whitelist_payload = _build_full_scan_whitelist_payload(
                                        round_payload=round_payload,
                                        work_ranges=work_ranges,
                                        filter_config=full_scan_filter_config,
                                        antenna_bias=_fs_ab,
                                        round_dev_area_ratio=round_dev_area_ratio,
                                        is_large_area_mode=is_large_area_mode,
                                    )
                                    whitelist_key   = _write_full_scan_whitelist_result(r, whitelist_payload)
                                    whitelist_count = whitelist_payload.get("mac_count", 0)
                                    prev_whitelist_count = whitelist_count  # 供下轮细扫次数动态调整
                                    round_payload["whitelist_key"]   = whitelist_key
                                    round_payload["whitelist_count"] = whitelist_count
                                    if (
                                        FULL_SCAN_WHITELIST_REFINEMENT_ENABLED
                                        and whitelist_count > 0
                                    ):
                                        try:
                                            refinement_summary = (
                                                _run_full_scan_whitelist_refinement(
                                                    r=r,
                                                    ptz=ptz,
                                                    scan_id=_full_scan_id,
                                                    whitelist_payload=whitelist_payload,
                                                    round_results=round_results,
                                                    scan_mode=scan_mode,
                                                    image_context=image_context,
                                                    move_range=_fs_direct_move_range,
                                                    stop_check_fn=lambda: bool(
                                                        _full_scan_stop_reason()
                                                    ),
                                                    target_ranges=round_payload.get("target_ranges"),
                                                )
                                            )
                                            whitelist_payload["refinement_summary"] = (
                                                refinement_summary
                                            )
                                            _write_full_scan_whitelist_result(
                                                r,
                                                whitelist_payload,
                                            )
                                            round_payload["whitelist_refinement"] = (
                                                refinement_summary
                                            )
                                            if (
                                                refinement_summary.get("status")
                                                == "stopped"
                                            ):
                                                round_stop_reason = (
                                                    _full_scan_stop_reason()
                                                    or "manual_stop"
                                                )
                                                round_status = "STOPPED"
                                                round_payload["round_status"] = (
                                                    round_status
                                                )
                                                round_payload["stop_reason"] = (
                                                    round_stop_reason
                                                )
                                        except Exception as refinement_exc:
                                            logger.exception(
                                                "❌ 白名单位置复核失败，保留基础白名单: "
                                                f"{refinement_exc}"
                                            )
                                            refinement_summary = {
                                                "status": "failed",
                                                "reason": str(refinement_exc),
                                            }
                                            whitelist_payload[
                                                "refinement_summary"
                                            ] = refinement_summary
                                            _write_full_scan_whitelist_result(
                                                r,
                                                whitelist_payload,
                                            )
                                            round_payload[
                                                "whitelist_refinement"
                                            ] = refinement_summary
                                    logger.info(
                                        f"✅ 第{full_scan_round}轮白名单已生成: "
                                        f"{whitelist_count} 个 MAC, "
                                        f"淘汰 {whitelist_payload.get('rejected_mac_count', 0)} 个, "
                                        f"key={whitelist_key}"
                                    )
                                except Exception as e:
                                    round_payload["whitelist_error"] = str(e)
                                    logger.error(f"❌ 第{full_scan_round}轮白名单生成失败: {e}")
                            else:
                                round_payload["whitelist_skipped_reason"] = "round_not_success"

                            r.set(f'full_scan:round_{full_scan_round}_results',
                                  json.dumps(round_payload))
                            _write_full_scan_realtime_result(
                                r,
                                round_payload,
                                timing_trace=timing_trace,
                                timing_fields={"phase": "round_finalize"},
                            )
                            # 最终轮次结果已经写入 Redis 后，先发布终态再做测试摘要
                            # 和历史快照。任何后处理阻塞或失败，都不能让前端永久
                            # 停留在 running；循环退出处还会再次幂等收口。
                            _round_is_task_terminal = (
                                time_interval is None
                                and (
                                    scan_time_limit is None
                                    or round_stop_reason in ("manual_stop", "time_limit")
                                )
                            )
                            if _round_is_task_terminal:
                                _terminal_reason = (
                                    "config_session_cleanup_failed"
                                    if config_session_cleanup_failed
                                    else (
                                        round_stop_reason
                                        if round_stop_reason in ("manual_stop", "time_limit")
                                        else None
                                    )
                                )
                                _publish_full_scan_terminal_status(
                                    r,
                                    scan_id=_full_scan_id,
                                    round_payload=round_payload,
                                    rounds_completed=full_scan_round,
                                    final_state=(
                                        "error"
                                        if config_session_cleanup_failed
                                        else (
                                            "stopped"
                                            if round_stop_reason == "manual_stop"
                                            else "done"
                                        )
                                    ),
                                    reason=_terminal_reason,
                                )

                            if round_summary is not None:
                                timing_trace.close()
                                _write_full_scan_test_summary(
                                    logger,
                                    round_summary,
                                    timing_trace=timing_trace,
                                )

                            if round_stop_reason != "manual_stop":
                                _save_project_snapshot_nonfatal(
                                    r=r,
                                    scan_type="full_area",
                                    round_id=_full_round_id,
                                    round_index=full_scan_round,
                                    grid_data=round_results,
                                    render_meta={
                                    "scan_type":    "full_area",
                                    "round_status": round_status,
                                    "stop_reason":  round_stop_reason,
                                    "precheck_range": precheck_range,
                                    "work_ranges":    work_ranges,
                                    "fine_step":      fine_step,
                                    "fine_count":     fine_count,
                                    "dwell_time":     _cmd_dwell,
                                    "dwell_time_source": dwell_time_source,
                                    "dwell_times":    full_scan_dwell_times,
                                    "scan_time_limit": scan_time_limit,
                                    "scan_time_limit_unit": "minutes",
                                    "time_interval":  time_interval,
                                    "time_interval_unit": "seconds",
                                    "window_index":   window_index,
                                    "window_started_at":  window_started_at,
                                    "window_deadline_at": window_deadline_at,
                                    "expected_points":    total_points,
                                    "completed_points":   _final_counts["completed_fixed_point_count"],
                                    **_final_counts,
                                    "sampling_points_count": len(_full_sampling),
                                    "initial_scan_configs_count": configs_count,
                                    "total_mac_detections": total_macs,
                                    "whitelist_key":   whitelist_key,
                                    "whitelist_count": whitelist_count,
                                    "omni_enabled":    r.get('capture:omni_enabled') == '1',
                                    },
                                    status=round_status,
                                    raw_dir=os.path.join(PANORAMA_RAW_DIR, _full_round_id)
                                    if os.path.isdir(os.path.join(PANORAMA_RAW_DIR, _full_round_id))
                                    else None,
                                )
                            else:
                                logger.info("🛑 手动停止：跳过项目快照持久化以满足 2 秒停止预算")

                            if round_stop_reason == "manual_stop":
                                window_stop_reason = "manual_stop"
                                stop_all_windows = True
                                break
                            if round_stop_reason == "time_limit":
                                window_stop_reason = "time_limit"
                                break
                            if scan_time_limit is None:
                                window_stop_reason = round_stop_reason
                                break

                        if stop_all_windows:
                            break

                        if time_interval is None:
                            break

                        try:
                            if _full_scan_stop_reason() == "time_limit":
                                r.delete('multi_scan:stop_full_area_scan')
                        except Exception:
                            pass

                        interval_seconds = int(time_interval)
                        logger.info(
                            f"⏸️ 扫描窗口结束(reason={window_stop_reason})，"
                            f"等待 {interval_seconds}s 后开始下一次全面扫描"
                        )
                        for remaining in range(interval_seconds, 0, -1):
                            patch_ptz_status(r, {
                                "state": "FULL_AREA_SCANNING",
                                "full_scan": {
                                    "active":              True,
                                    "state":               "running",
                                    "phase":               "waiting_interval",
                                    "rounds_completed":    full_scan_round,
                                    "time_interval":       time_interval,
                                    "time_interval_unit":  "seconds",
                                    "next_window_in_seconds": remaining,
                                },
                            })
                            stop_reason = _full_scan_sleep_with_stop(1.0, "全面扫描间隔等待")
                            if stop_reason:
                                logger.info(f"🛑 间隔等待期间检测到停止标志: {stop_reason}")
                                stop_all_windows = True
                                break
                        if stop_all_windows:
                            break
                        continue

                    # ── 恢复 PTZ 状态 ────────────────────────────────────────────
                    timing_trace.close()
                    state = "IDLE"
                    final_stop_reason = _full_scan_stop_reason()
                    if config_session_cleanup_failed:
                        final_stop_reason = "config_session_cleanup_failed"
                        fs_final_state = "error"
                    else:
                        fs_final_state = 'stopped' if final_stop_reason == 'manual_stop' else 'done'
                    _publish_full_scan_terminal_status(
                        r,
                        scan_id=_full_scan_id,
                        round_payload=last_round_payload,
                        rounds_completed=full_scan_round,
                        final_state=fs_final_state,
                        reason=final_stop_reason,
                        position={"pan": pan, "tilt": tilt},
                    )
                    logger.info(f"✅ 全面扫描结束，共完成 {full_scan_round} 轮")
                    # 清理 active_scan_id（允许新扫描启动）
                    # stop key 不立即删除，依靠 TTL 自然过期（120s/60s），
                    # 避免已派发给 capture_worker 的旧命令读不到 stop 标志
                    try:
                        r.delete('full_scan:active_scan_id')
                        # 确保 stop key 有 TTL（防御性设置，避免残留）
                        # ttl 返回 -1 表示无过期，需要补设
                        for _k in ('full_scan:stop', 'multi_scan:stop_full_area_scan'):
                            if r.get(_k) and r.ttl(_k) == -1:
                                r.expire(_k, 120)
                    except Exception:
                        pass
                    _finalize_current_project(r, expected_scan_type="full_area", status=final_project_status)
                    continue

                if action == 'stop_full_area_scan':
                    """停止正在进行的全面扫描"""
                    # scan_id 校验：必须带 scan_id 且与 active id 精确匹配
                    _stop_fs_id = command.get('scan_id')
                    _active_fs_id = r.get('full_scan:active_scan_id')
                    if not _stop_fs_id or not _active_fs_id or _active_fs_id != _stop_fs_id:
                        logger.info(f"⏭️ 全面扫描 stop 命令跳过: cmd_scan_id={_stop_fs_id} active={_active_fs_id}")
                        continue
                    logger.info("🛑 收到停止全面扫描命令")
                    r.set('full_scan:stop', json.dumps({
                        'scan_id': _stop_fs_id, 'reason': 'manual_stop', 'ts': time.time(),
                    }), ex=120)
                    r.set('multi_scan:stop_full_area_scan', '1', ex=60)
                    logger.info("✅ 已设置全面扫描停止标志")

                    continue

                # ==================== T3 定位扫描命令 ====================
                if action == 'start_location_scan':
                    """
                    定位扫描命令：
                    1. 信道探测：每个目标区域中心 + 四角，所有 MAC 同时探测。
                    2. 扫描：后端自动粗扫、生成扩边细扫区域，多 MAC 共享点位。
                    3. 最终定位：确认每个 MAC 的最佳点，并可选按 MAC 抓包。
                    """
                    logger.info("📍 [T3] 收到定位扫描命令")

                    ls_raw_macs = command.get('target_macs', [])
                    _ls_config = _get_location_scan_config_fn()
                    shrink_top_n = max(1, int(_ls_config.get("收缩强点数量", 5)))
                    shrink_rssi_delta = max(0.0, float(_ls_config.get("收缩RSSI差值阈值", 4.0)))
                    shrink_outlier_deg = max(0.0, float(_ls_config.get("收缩离群角度阈值", 20.0)))
                    shrink_single_pan_half = max(0.1, float(_ls_config.get("单点兜底水平半宽", 8.0)))
                    shrink_single_tilt_half = max(0.1, float(_ls_config.get("单点兜底垂直半宽", 6.0)))
                    target_guard_inner_points = max(0, int(_ls_config.get("目标区内部保底点数", 5)))
                    target_guard_min_span = max(0.0, float(_ls_config.get("目标区分点最小跨度", 1.0)))

                    _ls_cmd_summary = {
                        'target_macs': ls_raw_macs,
                        'pan_ranges': command.get('pan_ranges', []),
                        'tilt_ranges': command.get('tilt_ranges', []),
                        'dwell_time': command.get('dwell_time', 1.0),
                        'expand_deg': command.get('expand_deg', 10.0),
                        'channel': command.get('channel'),
                        'bandwidth': command.get('bandwidth'),
                        'fixed_channel_enabled': command.get('channel') is not None,
                        'target_configs': command.get('target_configs'),
                        'probe_dwell_time': command.get('probe_dwell_time', 1.0),
                        'probe_rounds_max': command.get('probe_rounds_max', 2),
                        'capture_time_limit': command.get('capture_time_limit'),
                        'time_interval': command.get('time_interval'),
                        'track_time_limit': command.get('track_time_limit'),
                        'track_rssi_threshold': command.get('track_rssi_threshold', 5.0),
                        'shrink_top_n': shrink_top_n,
                        'shrink_rssi_delta': shrink_rssi_delta,
                        'shrink_outlier_deg': shrink_outlier_deg,
                        'shrink_single_pan_half': shrink_single_pan_half,
                        'shrink_single_tilt_half': shrink_single_tilt_half,
                        'target_guard_inner_points': target_guard_inner_points,
                        'target_guard_min_span': target_guard_min_span,
                    }
                    logger.info(f"📍 [T3] 参数: {json.dumps(_ls_cmd_summary, ensure_ascii=False)}")
                    ls_target_macs = [m.lower().strip() for m in ls_raw_macs if m]
                    ls_pan_ranges = command.get('pan_ranges', [])
                    ls_tilt_ranges = command.get('tilt_ranges', [])
                    ls_dwell = float(command.get('dwell_time', 1.0))
                    ls_expand = float(command.get('expand_deg', 10.0))
                    ls_channel = command.get('channel', None)
                    ls_bandwidth = command.get('bandwidth', 'HT20')
                    ls_target_configs = command.get('target_configs') or {}
                    ls_probe_dwell = float(command.get('probe_dwell_time', 1.0))
                    ls_probe_rounds_max = max(1, int(command.get('probe_rounds_max', 2)))
                    ls_capture_limit = command.get('capture_time_limit')
                    ls_pcap_split_mb = float(command.get('pcap_split_size_mb', 100.0))
                    ls_min_free_memory_mb = command.get('min_free_memory_mb')
                    ls_min_free_disk_mb = command.get('min_free_disk_mb')
                    time_interval = _parse_optional_nonnegative_float(command.get('time_interval'))
                    track_time_limit = _parse_optional_positive_float(command.get('track_time_limit'))
                    track_rssi_threshold = float(command.get('track_rssi_threshold', 5.0))

                    # 天线偏差补偿：从命令读取 antenna_bias
                    _ls_ab = command.get('antenna_bias')
                    if _ls_ab and not _ls_ab.get('enabled'):
                        _ls_ab = None

                    # 新任务启动时清掉上一轮停止标志。
                    # 注意：不删除 location_scan:stop —— worker 的 _ls_stopped() 会按 scan_id
                    # 判断是否属于当前任务，不属于的自动清理，属于的才响应。
                    r.delete('capture:stop', 'location_scan:result')
                    try:
                        queued_capture_cmds = r.lrange('capture:command_queue', 0, -1)
                        if queued_capture_cmds:
                            kept_capture_cmds = []
                            removed_probe_cmds = 0
                            for queued_cmd in queued_capture_cmds:
                                try:
                                    queued_payload = json.loads(queued_cmd)
                                except Exception:
                                    kept_capture_cmds.append(queued_cmd)
                                    continue
                                if isinstance(queued_payload, dict) and queued_payload.get('action') == 'detect_channels':
                                    removed_probe_cmds += 1
                                else:
                                    kept_capture_cmds.append(queued_cmd)
                            if removed_probe_cmds:
                                pipe = r.pipeline()
                                pipe.delete('capture:command_queue')
                                if kept_capture_cmds:
                                    pipe.rpush('capture:command_queue', *kept_capture_cmds)
                                pipe.execute()
                                logger.warning(f"🧹 [T3] 已清理残留信道探测命令 {removed_probe_cmds} 条")
                    except Exception as e:
                        logger.warning(f"⚠️ [T3] 清理残留信道探测命令失败: {e}")

                    if not ls_target_macs:
                        logger.error("❌ [T3] start_location_scan 缺少 target_macs")
                        r.set('location_scan:status', json.dumps({
                            'phase': 'idle', 'status': 'error', 'message': 'target_macs 为空'
                        }))
                        patch_ptz_status(r, {
                            'location_scan': {'active': False, 'phase': 'idle', 'state': 'error',
                                              'terminal': True, 'reason': 'target_macs 为空'},
                        })
                        continue

                    if not ls_pan_ranges or not ls_tilt_ranges or len(ls_pan_ranges) != len(ls_tilt_ranges):
                        logger.error("❌ [T3] pan_ranges/tilt_ranges 为空或长度不匹配")
                        r.set('location_scan:status', json.dumps({
                            'phase': 'idle', 'status': 'error', 'message': 'pan_ranges/tilt_ranges 错误'
                        }))
                        patch_ptz_status(r, {
                            'location_scan': {'active': False, 'phase': 'idle', 'state': 'error',
                                              'terminal': True, 'reason': 'pan_ranges/tilt_ranges 错误'},
                        })
                        continue

                    try:
                        location_bound = full_area_precheck_range_from_redis(
                            r,
                            {
                                "pan_min": PAN_MIN,
                                "pan_max": PAN_MAX,
                                "tilt_min": TILT_MIN,
                                "tilt_max": TILT_MAX,
                            },
                            {
                                "pan_min": PTZ_DEFAULT_PAN_MIN,
                                "pan_max": PTZ_DEFAULT_PAN_MAX,
                                "tilt_min": PTZ_DEFAULT_TILT_MIN,
                                "tilt_max": PTZ_DEFAULT_TILT_MAX,
                                "pan_step": PTZ_DEFAULT_PAN_STEP,
                                "tilt_step": PTZ_DEFAULT_TILT_STEP,
                            },
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ [T3] 读取定位业务运行范围失败，回退硬件限位: {e}")
                        location_bound = {
                            "pan_min": PAN_MIN,
                            "pan_max": PAN_MAX,
                            "tilt_min": TILT_MIN,
                            "tilt_max": TILT_MAX,
                            "source": "hardware_limits_fallback",
                        }
                    logger.info(
                        f"📍 [T3] 定位业务运行范围: "
                        f"pan=[{location_bound['pan_min']:.1f}, {location_bound['pan_max']:.1f}], "
                        f"tilt=[{location_bound['tilt_min']:.1f}, {location_bound['tilt_max']:.1f}], "
                        f"source={location_bound.get('source')}, "
                        f"fallback={location_bound.get('fallback_source')}"
                    )

                    if not isinstance(ls_target_configs, dict):
                        logger.error("❌ [T3] target_configs 格式错误")
                        r.set('location_scan:status', json.dumps({
                            'phase': 'idle', 'status': 'error', 'message': 'target_configs 格式错误'
                        }))
                        patch_ptz_status(r, {
                            'location_scan': {'active': False, 'phase': 'idle', 'state': 'error',
                                              'terminal': True, 'reason': 'target_configs 格式错误'},
                        })
                        continue

                    normalized_target_configs = {}
                    try:
                        for mac, cfg in ls_target_configs.items():
                            mac_l = str(mac).lower().strip()
                            if not mac_l or mac_l not in ls_target_macs:
                                continue
                            normalized_target_configs[mac_l] = {
                                'channel': int(cfg['channel']),
                                'bandwidth': str(cfg['bandwidth']),
                            }
                    except Exception as e:
                        logger.error(f"❌ [T3] target_configs 内容错误: {e}")
                        r.set('location_scan:status', json.dumps({
                            'phase': 'idle', 'status': 'error', 'message': 'target_configs 内容错误'
                        }))
                        patch_ptz_status(r, {
                            'location_scan': {'active': False, 'phase': 'idle', 'state': 'error',
                                              'terminal': True, 'reason': 'target_configs 内容错误'},
                        })
                        continue

                    mac_history = {}
                    round_num = 1
                    track_start_time = time.time()

                    # 生成一个 project_id，所有轮次复用
                    _location_project_id = f"location_{int(time.time())}"
                    # scan_id 优先从 command 读取（由 API 层生成），兼容旧 command 无 scan_id
                    ls_scan_id = command.get('scan_id') or f"{_location_project_id}:{int(time.time() * 1000)}"
                    r.set(CURRENT_PROJECT_KEY, json.dumps({'project_id': _location_project_id}))
                    r.setex('location_scan:active_scan_id', 86400, ls_scan_id)

                    def _ls_mark_stopped(reason='manual_stop'):
                        now = time.time()
                        try:
                            r.set('capture:stop', '1', ex=120)
                        except Exception:
                            pass
                        # 旧 key 保持兼容
                        try:
                            raw_status = r.get('location_scan:status')
                            status_payload = json.loads(raw_status) if raw_status else {}
                            if status_payload.get('status') != 'stopped':
                                status_payload.update({
                                    'phase': 'idle',
                                    'status': 'stopped',
                                    'reason': reason,
                                    'ts': now,
                                })
                                r.set('location_scan:status', json.dumps(status_payload, ensure_ascii=False))
                        except Exception:
                            pass
                        # 新统一状态
                        patch_ptz_status(r, {
                            'state': 'IDLE',
                            'location_scan': {
                                'active': False,
                                'phase': 'stopped',
                                'state': 'stopped',
                                'terminal': True,
                                'reason': reason,
                            },
                        })

                    def _ls_stopped():
                        """检查 stop 标志，只在 scan_id 匹配时才返回 True。"""
                        raw = r.get('location_scan:stop')
                        if raw is None:
                            return False
                        try:
                            stop_data = json.loads(raw)
                            stop_scan_id = stop_data.get('scan_id') if isinstance(stop_data, dict) else None
                        except (json.JSONDecodeError, TypeError):
                            stop_scan_id = None
                        # 旧格式 "1" 或 scan_id 不匹配 → 清理旧 stop，不响应
                        if stop_scan_id is None or stop_scan_id != ls_scan_id:
                            try:
                                r.delete('location_scan:stop')
                            except Exception:
                                pass
                            return False
                        # scan_id 匹配 → 真正停止
                        _ls_mark_stopped()
                        return True

                    while True:
                        if track_time_limit and (time.time() - track_start_time) > track_time_limit * 60:
                            logger.info("追踪时间达到上限，自动结束定位扫描")
                            break
                        if _ls_stopped():
                            logger.info("🛑 [T3] 检测到定位扫描停止标志（scan_id 匹配），退出轮次循环")
                            break
                        ls_final_results = {}
                        mac_rssi_points = {mac: {} for mac in ls_target_macs}
                        mac_stage_points = {mac: {} for mac in ls_target_macs}
                        mac_omni_points = {mac: {} for mac in ls_target_macs}
                        mac_identity = {mac: {} for mac in ls_target_macs}
                        mac_channel_map = {}
                        ls_stop_reason = None
                        # 扫描点位记录（每阶段实际扫描的点位）
                        scan_points_record = {
                            'coarse': [],
                            'coarse_inner': [],
                            'coarse_outer': [],
                            'mid': [],
                            'fine': [],
                            'verify': [],
                        }

                        def _ls_set_status(phase, extra=None):
                            # 旧 key 保持兼容
                            payload = {'phase': phase, 'status': 'running', 'ts': time.time()}
                            if extra:
                                payload.update(extra)
                            r.set('location_scan:status', json.dumps(payload))
                            # 新统一状态：patch ptz:current_status.location_scan
                            ls_patch = {
                                'active': True,
                                'phase': phase,
                                'state': 'running',
                                'scan_id': ls_scan_id,
                                'stop_requested': False,
                                'terminal': False,
                            }
                            if extra:
                                ls_patch.update(extra)
                            patch_ptz_status(r, {'location_scan': ls_patch})

                        def _ls_publish_results():
                            r.set('location_scan:result', json.dumps({
                                'ts': time.time(),
                                'results': ls_final_results,
                                'scan_points': scan_points_record,
                            }))

                        def _ls_publish_intermediate_results():
                            """发布中间结果，供前端展示定位进度。"""
                            intermediate_results = {}
                            # 所有点位的详细数据（按 MAC 分组）
                            all_points_by_mac = {}
                            omni_points_by_mac = {}
                            for mac in mac_rssi_points:
                                points = mac_rssi_points.get(mac, {})
                                if not points:
                                    continue
                                # 定向点位详情：{pan,tilt} -> rssi
                                all_points_by_mac[mac] = {
                                    f"{pos[0]},{pos[1]}": rssi
                                    for pos, rssi in points.items()
                                }
                                # 全向点位详情：{pan,tilt} -> rssi
                                omni_points = mac_omni_points.get(mac, {})
                                if omni_points:
                                    omni_points_by_mac[mac] = {
                                        f"{pos[0]},{pos[1]}": rssi
                                        for pos, rssi in omni_points.items()
                                    }

                            for mac in mac_rssi_points:
                                points = mac_rssi_points.get(mac, {})
                                if not points:
                                    continue
                                best_pos, best_rssi = _best_for_mac(mac)
                                ch_info = mac_channel_map.get(mac, {})
                                identity = mac_identity.get(mac, {})
                                # 获取最强点的全向 RSSI
                                omni_points = mac_omni_points.get(mac, {})
                                best_omni_rssi = None
                                best_position_payload = None
                                if best_pos is not None:
                                    best_position_payload = {'pan': best_pos[0], 'tilt': best_pos[1]}
                                    best_omni_rssi = omni_points.get(best_pos)
                                    if best_omni_rssi is None:
                                        for k, v in omni_points.items():
                                            if abs(k[0] - best_pos[0]) < 0.01 and abs(k[1] - best_pos[1]) < 0.01:
                                                best_omni_rssi = v
                                                break
                                intermediate_results[mac] = {
                                    'status': 'scanning',
                                    'has_signal': best_pos is not None,
                                    'best_position': best_position_payload,
                                    'best_rssi': best_rssi,
                                    'best_omni_rssi': best_omni_rssi,
                                    'channel': ch_info.get('channel'),
                                    'bandwidth': ch_info.get('bandwidth'),
                                    'type': identity.get('type'),
                                    'subtype': identity.get('subtype'),
                                    'ssid': identity.get('ssid'),
                                    'scan_points': len(mac_rssi_points.get(mac, {})),
                                    # 所有点位详情，前端可用于绘图
                                    'all_points': all_points_by_mac.get(mac, {}),
                                    'omni_all_points': omni_points_by_mac.get(mac, {}),
                                }
                            if intermediate_results:
                                r.set('location_scan:result', json.dumps({
                                    'ts': time.time(),
                                    'status': 'scanning',
                                    'results': intermediate_results,
                                    'scan_points': scan_points_record,
                                }))

                        def _clip_to_location_bound(pan_range, tilt_range):
                            pan_min = max(location_bound["pan_min"], float(min(pan_range)))
                            pan_max = min(location_bound["pan_max"], float(max(pan_range)))
                            tilt_min = max(location_bound["tilt_min"], float(min(tilt_range)))
                            tilt_max = min(location_bound["tilt_max"], float(max(tilt_range)))
                            if pan_min > pan_max or tilt_min > tilt_max:
                                return None, None
                            return [pan_min, pan_max], [tilt_min, tilt_max]

                        def _clip_expanded(pan_rng, tilt_rng, expand):
                            return _clip_to_location_bound(
                                [float(pan_rng[0]) - expand, float(pan_rng[1]) + expand],
                                [float(tilt_rng[0]) - expand, float(tilt_rng[1]) + expand],
                            )

                        def _move_and_scan(point_id, target_pan, target_tilt, configs, phase,
                                           current_point, total_points, dwell_time=None,
                                           visual_pan=None, visual_tilt=None):
                            """移动到 RF 点位并扫描。
                            visual_pan/visual_tilt: 传给 capture 命令的视觉坐标，RSSI 数据按此坐标归档。
                            未提供时使用 target_pan/target_tilt（无偏差场景兼容）。
                            """
                            if not configs:
                                return None
                            scan_dwell = float(dwell_time) if dwell_time is not None else ls_dwell
                            if not safe_split_move(ptz, target_pan, target_tilt, order='pan_first',
                                                   settle=1.0, stop_check_fn=_ls_stopped):
                                logger.error(f"❌ [T3] 移动失败: ({target_pan},{target_tilt})")
                                return None
                            if _ls_stopped():
                                logger.warning(f"🛑 [T3] 点位 {point_id} 移动后检测到停止标志")
                                return None
                            time.sleep(0.3)
                            if _ls_stopped():
                                logger.warning(f"🛑 [T3] 点位 {point_id} 停稳等待后检测到停止标志")
                                return None
                            cur_pan, cur_tilt = ptz.get_position()
                            patch_ptz_status(r, {
                                'position': {'pan': round(cur_pan, 2), 'tilt': round(cur_tilt, 2)},
                                'state': 'LOCATION_SCANNING',
                                'location_scan': {
                                    'active': True,
                                    'phase': phase,
                                    'state': 'running',
                                    'scan_id': ls_scan_id,
                                    'current_point': current_point,
                                    'total_points': total_points,
                                },
                            })
                            # capture 命令使用视觉坐标，RSSI 数据按此坐标归档
                            _cap_pan = round(visual_pan, 2) if visual_pan is not None else round(cur_pan, 2)
                            _cap_tilt = round(visual_tilt, 2) if visual_tilt is not None else round(cur_tilt, 2)
                            r.lpush('capture:command_queue', json.dumps({
                                'action': 'scan_at_point',
                                'point_id': point_id,
                                'pan': _cap_pan,
                                'tilt': _cap_tilt,
                                'configs': configs,
                                'dwell_time': scan_dwell,
                                'extend_time': 2,
                                'stop_key': 'location_scan:stop',
                                'scan_id': ls_scan_id,
                            }))
                            sap_deadline = time.time() + max(30.0, (scan_dwell + 2) * max(1, len(configs)) * 3)
                            sap_result = None
                            while time.time() < sap_deadline:
                                if _ls_stopped():
                                    break
                                raw = r.brpop(f'multi_scan:{point_id}_notify', timeout=1)
                                if raw:
                                    sap_result = json.loads(raw[1])
                                    break
                            if _ls_stopped() or not sap_result or sap_result.get('status') != 'done':
                                logger.warning(f"⚠️ [T3] 点位 {point_id} 扫描超时或被停止")
                                return None
                            raw_pt = r.get(f'multi_scan:{point_id}')
                            return json.loads(raw_pt) if raw_pt else None

                        def _record_point_data(pt_data, source_stage=None, segment_macs=None):
                            """
                            记录点位扫描数据。

                            Args:
                                pt_data: 点位数据字典
                                source_stage: 来源阶段（coarse/mid/fine/verify）
                                segment_macs: 本segment负责扫描的MAC列表
                            """
                            if not pt_data:
                                return
                            pos = (round(float(pt_data.get('pan', 0.0)), 2),
                                   round(float(pt_data.get('tilt', 0.0)), 2))

                            # 记录实际扫到的MAC
                            scanned_macs = set()
                            for scan_res in pt_data.get('scan_results', []):
                                mac = scan_res.get('target_mac', '').lower().strip()
                                scanned_macs.add(mac)
                                if mac not in mac_rssi_points:
                                    continue
                                identity = mac_identity.setdefault(mac, {})
                                for field in ('type', 'subtype', 'ssid'):
                                    value = scan_res.get(field)
                                    if value is not None or field not in identity:
                                        identity[field] = value
                                rssi = scan_res.get('rssi_avg')
                                if rssi is not None:
                                    # 真实RSSI可以覆盖-100
                                    existing_rssi = mac_rssi_points[mac].get(pos)
                                    if existing_rssi is None or existing_rssi == -100 or rssi > existing_rssi:
                                        mac_rssi_points[mac][pos] = rssi
                                        if source_stage:
                                            stage_bucket = mac_stage_points.setdefault(mac, {}).setdefault(source_stage, {})
                                            stage_bucket[pos] = rssi
                                omni_rssi = scan_res.get('omni_rssi_avg')
                                if omni_rssi is not None:
                                    mac_omni_points[mac][pos] = omni_rssi

                            # 对本segment负责但未扫到的MAC记录-100
                            if segment_macs:
                                for mac in segment_macs:
                                    mac_lower = mac.lower().strip()
                                    if mac_lower not in mac_rssi_points:
                                        continue
                                    if mac_lower not in scanned_macs:
                                        # 未扫到，记录-100（但不覆盖已有真实RSSI）
                                        existing_rssi = mac_rssi_points[mac_lower].get(pos)
                                        if existing_rssi is None:
                                            mac_rssi_points[mac_lower][pos] = -100
                                            if source_stage:
                                                stage_bucket = mac_stage_points.setdefault(mac_lower, {}).setdefault(source_stage, {})
                                                stage_bucket[pos] = -100

                            # 定期更新中间结果到 Redis，供前端展示定位进度。
                            _ls_publish_intermediate_results()

                        def _best_from_points(points, filter_negative=True):
                            """
                            从点位字典中获取最强点。

                            Args:
                                points: {(pan, tilt): rssi, ...}
                                filter_negative: 是否过滤掉-100及以下的点

                            Returns:
                                ((pan, tilt), rssi) 或 (None, None)
                            """
                            if not points:
                                return None, None
                            if filter_negative:
                                valid_points = {k: v for k, v in points.items() if v > -100}
                                if not valid_points:
                                    return None, None
                                best_pos = max(valid_points, key=valid_points.get)
                                return best_pos, valid_points[best_pos]
                            else:
                                best_pos = max(points, key=points.get)
                                return best_pos, points[best_pos]

                        def _best_for_mac(mac, preferred_stages=None):
                            """
                            计算MAC的best点位（加权best）。

                            优先级：fine > mid > coarse内部点 > coarse外扩点
                            使用top5强点 + 孤立尖峰过滤 + 加权中心
                            """
                            stage_map = mac_stage_points.get(mac, {})
                            stage_order = preferred_stages or (
                                'locating',
                                'fast_verify',
                                'fine',
                                'mid',
                                'coarse_inner',
                                'coarse_outer',
                                'coarse',
                            )

                            for stage in stage_order:
                                stage_points = stage_map.get(stage, {})
                                if not stage_points:
                                    continue

                                # 过滤掉-100的点
                                valid_points = {k: v for k, v in stage_points.items() if v > -100}
                                if not valid_points:
                                    continue

                                # 转换为列表格式 [(pan, tilt, rssi), ...]
                                points_with_rssi = [(p[0], p[1], r) for p, r in valid_points.items()]

                                # 取top5强点
                                sorted_points = sorted(points_with_rssi, key=lambda x: x[2], reverse=True)
                                top_points = sorted_points[:5]

                                # 孤立尖峰过滤
                                filtered_points = _filter_isolated_spike(top_points)

                                # 加权中心计算
                                best_pos = _weighted_best_point(filtered_points)
                                if best_pos is not None:
                                    # 获取对应的RSSI
                                    best_rssi = valid_points.get(best_pos)
                                    if best_rssi is None:
                                        # 如果加权中心不在原始点位中，找最近的点
                                        min_dist = float('inf')
                                        for p, r in valid_points.items():
                                            dist = math.hypot(p[0] - best_pos[0], p[1] - best_pos[1])
                                            if dist < min_dist:
                                                min_dist = dist
                                                best_rssi = r
                                    logger.info(
                                        f"📍 [T3] {mac} best计算: stage={stage}, "
                                        f"points={len(valid_points)}, filtered={len(filtered_points)}, "
                                        f"best=({best_pos[0]:.1f}, {best_pos[1]:.1f}), rssi={best_rssi}"
                                    )
                                    return best_pos, best_rssi

                            # 所有阶段都没有有效点，返回None
                            return None, None

                        def _build_fine_boxes(active_macs, source_stage, expand_deg=10.0):
                            boxes = []
                            for mac in active_macs:
                                points = mac_stage_points.get(mac, {}).get(source_stage, {})
                                if not points:
                                    logger.warning(f"⚠️ [T3] {mac} 无 {source_stage} 阶段有效点，跳过本轮收缩")
                                    continue
                                # 过滤掉-100的点
                                valid_points = {k: v for k, v in points.items() if v > -100}
                                if not valid_points:
                                    logger.warning(f"⚠️ [T3] {mac} {source_stage} 阶段全是未命中(-100)，跳过本轮收缩")
                                    continue
                                best_pos, best_rssi = _best_from_points(valid_points)
                                sorted_points = sorted(valid_points.items(), key=lambda x: x[1], reverse=True)
                                top_points = sorted_points[:shrink_top_n]
                                rssi_floor = best_rssi - shrink_rssi_delta
                                strong_points = []
                                rejected_by_rssi = 0
                                rejected_by_distance = 0
                                for point, rssi_value in top_points:
                                    if rssi_value < rssi_floor:
                                        rejected_by_rssi += 1
                                        continue
                                    pan_dist = point[0] - best_pos[0]
                                    tilt_dist = point[1] - best_pos[1]
                                    angle_dist = math.hypot(pan_dist, tilt_dist)
                                    if shrink_outlier_deg > 0 and angle_dist > shrink_outlier_deg:
                                        rejected_by_distance += 1
                                        continue
                                    strong_points.append(point)
                                if not strong_points:
                                    strong_points = [best_pos]

                                if len(strong_points) == 1:
                                    center_pan, center_tilt = strong_points[0]
                                    pan_range, tilt_range = _clip_to_location_bound(
                                        [center_pan - shrink_single_pan_half - expand_deg,
                                         center_pan + shrink_single_pan_half + expand_deg],
                                        [center_tilt - shrink_single_tilt_half - expand_deg,
                                         center_tilt + shrink_single_tilt_half + expand_deg],
                                    )
                                else:
                                    pans = [p[0] for p in strong_points]
                                    tilts = [p[1] for p in strong_points]
                                    pan_range, tilt_range = _clip_to_location_bound(
                                        [min(pans) - expand_deg, max(pans) + expand_deg],
                                        [min(tilts) - expand_deg, max(tilts) + expand_deg],
                                    )
                                if not pan_range or not tilt_range:
                                    logger.warning(f"⚠️ [T3] {mac} 收缩候选超出定位业务运行范围，跳过")
                                    continue
                                logger.info(
                                    f"📍 [T3] {mac} 基于 {source_stage} 收缩候选: best={best_rssi:.1f}, "
                                    f"top_n={shrink_top_n}, 保留={len(strong_points)}, "
                                    f"RSSI过滤={rejected_by_rssi}, 离群过滤={rejected_by_distance}, "
                                    f"box=pan{pan_range}, tilt{tilt_range}"
                                )
                                boxes.append({
                                    'macs': [mac],
                                    'pan_range': pan_range,
                                    'tilt_range': tilt_range,
                                })
                            return _merge_location_boxes(boxes)

                        def _uniform_location_path(
                            pan_range,
                            tilt_range,
                            min_points,
                            max_points,
                            seed_points=None,
                            jitter_sources=None,
                            jitter_min_deg=1.0,
                            hard_max_points=None,
                        ):
                            """
                            通用均匀点位生成函数。

                            Args:
                                pan_range: [pan_min, pan_max]
                                tilt_range: [tilt_min, tilt_max]
                                min_points: 目标最少点数
                                max_points: 目标最多点数
                                seed_points: 额外种子点列表 [(pan, tilt), ...]
                                jitter_sources: 抖动源点列表 [(pan, tilt), ...]，围绕这些点生成抖动
                                jitter_min_deg: 抖动最小角度，默认1°
                                hard_max_points: 硬上限，超过时强制降采样

                            Returns:
                                蛇形排序的点位列表 [(pan, tilt), ...]
                            """
                            pan_min, pan_max = float(pan_range[0]), float(pan_range[1])
                            tilt_min, tilt_max = float(tilt_range[0]), float(tilt_range[1])
                            hard_max = hard_max_points or max_points + 10

                            # 1. 收集所有候选点
                            all_points = []

                            # 种子点：四角
                            corners = [
                                (pan_min, tilt_min),
                                (pan_min, tilt_max),
                                (pan_max, tilt_min),
                                (pan_max, tilt_max),
                            ]
                            all_points.extend(corners)

                            # 种子点：四边中点
                            edge_midpoints = [
                                ((pan_min + pan_max) / 2.0, tilt_min),
                                ((pan_min + pan_max) / 2.0, tilt_max),
                                (pan_min, (tilt_min + tilt_max) / 2.0),
                                (pan_max, (tilt_min + tilt_max) / 2.0),
                            ]
                            all_points.extend(edge_midpoints)

                            # 种子点：中心
                            center = ((pan_min + pan_max) / 2.0, (tilt_min + tilt_max) / 2.0)
                            all_points.append(center)

                            # 额外种子点
                            if seed_points:
                                all_points.extend(seed_points)

                            # 2. 生成均匀网格
                            pan_span = abs(pan_max - pan_min)
                            tilt_span = abs(tilt_max - tilt_min)

                            # 估算网格密度
                            if pan_span < 1e-9 or tilt_span < 1e-9:
                                # 退化为线段或点
                                grid_points = all_points[:]
                            else:
                                # 按目标点数估算网格大小
                                target_count = (min_points + max_points) / 2.0
                                aspect_ratio = pan_span / tilt_span
                                tilt_count = max(3, int(math.sqrt(target_count / aspect_ratio)))
                                pan_count = max(3, int(target_count / tilt_count))

                                # 生成网格点
                                pan_samples = [
                                    round(pan_min + pan_span * i / (pan_count - 1), 2)
                                    for i in range(pan_count)
                                ]
                                tilt_samples = [
                                    round(tilt_min + tilt_span * i / (tilt_count - 1), 2)
                                    for i in range(tilt_count)
                                ]

                                grid_points = []
                                for row_idx, tilt in enumerate(tilt_samples):
                                    row = pan_samples if row_idx % 2 == 0 else list(reversed(pan_samples))
                                    for pan in row:
                                        grid_points.append((pan, tilt))

                                all_points.extend(grid_points)

                            # 3. 抖动点补充
                            if jitter_sources and jitter_min_deg > 0:
                                jitter_offsets = [
                                    (jitter_min_deg, 0),
                                    (-jitter_min_deg, 0),
                                    (0, jitter_min_deg),
                                    (0, -jitter_min_deg),
                                    (jitter_min_deg, jitter_min_deg),
                                    (jitter_min_deg, -jitter_min_deg),
                                    (-jitter_min_deg, jitter_min_deg),
                                    (-jitter_min_deg, -jitter_min_deg),
                                ]
                                for src_pan, src_tilt in jitter_sources:
                                    for d_pan, d_tilt in jitter_offsets:
                                        new_pan = round(max(pan_min, min(pan_max, src_pan + d_pan)), 2)
                                        new_tilt = round(max(tilt_min, min(tilt_max, src_tilt + d_tilt)), 2)
                                        all_points.append((new_pan, new_tilt))

                            # 4. 去重
                            deduped = []
                            seen = set()
                            for pan, tilt in all_points:
                                key = (round(pan, 2), round(tilt, 2))
                                if key not in seen:
                                    seen.add(key)
                                    deduped.append(key)

                            # 5. 降采样（如果超过上限）
                            if len(deduped) > hard_max:
                                # 保留关键点：四角、边中点、中心
                                critical_keys = set()
                                for p in corners + edge_midpoints + [center]:
                                    critical_keys.add((round(p[0], 2), round(p[1], 2)))

                                # 如果有抖动源，保留源点附近点
                                if jitter_sources:
                                    for src in jitter_sources:
                                        src_key = (round(src[0], 2), round(src[1], 2))
                                        critical_keys.add(src_key)

                                critical_points = [p for p in deduped if (round(p[0], 2), round(p[1], 2)) in critical_keys]
                                other_points = [p for p in deduped if (round(p[0], 2), round(p[1], 2)) not in critical_keys]

                                # 均匀抽样其他点
                                remaining_slots = hard_max - len(critical_points)
                                if remaining_slots > 0 and other_points:
                                    step = max(1, len(other_points) // remaining_slots)
                                    sampled = other_points[::step][:remaining_slots]
                                    deduped = critical_points + sampled
                                else:
                                    deduped = critical_points[:hard_max]

                                logger.info(f"📍 [T3] 点位降采样: {len(all_points)} → {len(deduped)} (硬上限={hard_max})")

                            # 6. 点数不足时日志说明
                            if len(deduped) < min_points:
                                logger.warning(f"⚠️ [T3] 点位不足: 目标{min_points}-{max_points}, 实际{len(deduped)}")

                            # 7. 蛇形排序
                            result = _sort_snake_path(deduped)
                            return result

                        def _sort_snake_path(points):
                            """将点位按蛇形排序（之字形）。"""
                            if not points:
                                return []

                            # 按 tilt 分组
                            tilt_groups = {}
                            for pan, tilt in points:
                                key = round(tilt, 2)
                                tilt_groups.setdefault(key, []).append((pan, tilt))

                            # 排序 tilt
                            sorted_tilts = sorted(tilt_groups.keys())

                            result = []
                            for row_idx, tilt in enumerate(sorted_tilts):
                                row = sorted(tilt_groups[tilt], key=lambda p: p[0])
                                if row_idx % 2 == 1:
                                    row = list(reversed(row))
                                result.extend(row)
                            return result

                        def _filter_isolated_spike(points_with_rssi, top_n=5, delta_threshold=6.0, distance_threshold=8.0, neighbor_radius=4.0):
                            """
                            孤立尖峰过滤。

                            Args:
                                points_with_rssi: [(pan, tilt, rssi), ...]
                                top_n: 取前N个强点
                                delta_threshold: top1比top2强的dB阈值
                                distance_threshold: top1到主体中心的距离阈值
                                neighbor_radius: 检查邻居的半径

                            Returns:
                                过滤后的点位列表，移除了孤立尖峰
                            """
                            if len(points_with_rssi) < 3:
                                return points_with_rssi

                            # 按RSSI排序
                            sorted_points = sorted(points_with_rssi, key=lambda x: x[2], reverse=True)
                            top_points = sorted_points[:top_n]

                            if len(top_points) < 3:
                                return points_with_rssi

                            top1 = top_points[0]
                            top2_to_n = top_points[1:]

                            # 计算top2~top5的主体中心
                            body_center_pan = sum(p[0] for p in top2_to_n) / len(top2_to_n)
                            body_center_tilt = sum(p[1] for p in top2_to_n) / len(top2_to_n)

                            # top1到主体中心的距离
                            dist_to_body = math.hypot(top1[0] - body_center_pan, top1[1] - body_center_tilt)

                            # top1比top2强多少
                            delta_rssi = top1[2] - top_points[1][2]

                            # 检查top1附近是否有其他强点
                            has_neighbor = False
                            for p in top2_to_n:
                                dist = math.hypot(top1[0] - p[0], top1[1] - p[1])
                                if dist <= neighbor_radius:
                                    has_neighbor = True
                                    break

                            # 判断是否为孤立尖峰
                            if (delta_rssi >= delta_threshold and
                                dist_to_body >= distance_threshold and
                                not has_neighbor):
                                logger.info(
                                    f"📍 [T3] 过滤孤立尖峰: ({top1[0]:.1f}, {top1[1]:.1f}) "
                                    f"RSSI={top1[2]:.1f}, delta={delta_rssi:.1f}dB, "
                                    f"dist={dist_to_body:.1f}°, no_neighbor"
                                )
                                # 移除top1
                                return [p for p in points_with_rssi if p != top1]

                            return points_with_rssi

                        def _weighted_best_point(points_with_rssi):
                            """
                            加权中心计算best点位。

                            Args:
                                points_with_rssi: [(pan, tilt, rssi), ...]，rssi必须>-100

                            Returns:
                                (pan, tilt) 或 None
                            """
                            valid_points = [(p, t, r) for p, t, r in points_with_rssi if r > -100]
                            if not valid_points:
                                return None

                            if len(valid_points) == 1:
                                return (valid_points[0][0], valid_points[0][1])

                            # 计算加权中心（RSSI作为权重，越强权重越大）
                            # 将RSSI转换为正权重（-30比-80强，权重更大）
                            min_rssi = min(r[2] for r in valid_points)
                            max_rssi = max(r[2] for r in valid_points)

                            if max_rssi - min_rssi < 0.1:
                                # RSSI差异很小，直接平均
                                avg_pan = sum(p for p, _, _ in valid_points) / len(valid_points)
                                avg_tilt = sum(t for _, t, _ in valid_points) / len(valid_points)
                                return (round(avg_pan, 2), round(avg_tilt, 2))

                            # 归一化权重
                            weights = []
                            for p, t, r in valid_points:
                                # 线性映射：min_rssi -> 0.1, max_rssi -> 1.0
                                w = 0.1 + 0.9 * (r - min_rssi) / (max_rssi - min_rssi)
                                weights.append(w)

                            total_weight = sum(weights)
                            weighted_pan = sum(p * w for (p, _, _), w in zip(valid_points, weights)) / total_weight
                            weighted_tilt = sum(t * w for (_, t, _), w in zip(valid_points, weights)) / total_weight

                            return (round(weighted_pan, 2), round(weighted_tilt, 2))

                        def _bounded_location_path(pan_range, tilt_range, max_points=24):
                            pan_min, pan_max = float(pan_range[0]), float(pan_range[1])
                            tilt_min, tilt_max = float(tilt_range[0]), float(tilt_range[1])
                            pan_count = 1 if abs(pan_max - pan_min) < 1e-9 else 4
                            tilt_count = 1 if abs(tilt_max - tilt_min) < 1e-9 else 4
                            while pan_count * tilt_count > max_points:
                                if pan_count >= tilt_count and pan_count > 1:
                                    pan_count -= 1
                                elif tilt_count > 1:
                                    tilt_count -= 1
                                else:
                                    break

                            def _samples(axis_min, axis_max, count):
                                if count <= 1:
                                    return [round((axis_min + axis_max) / 2.0, 2)]
                                return [
                                    round(axis_min + (axis_max - axis_min) * i / (count - 1), 2)
                                    for i in range(count)
                                ]

                            pans = _samples(pan_min, pan_max, pan_count)
                            tilts = _samples(tilt_min, tilt_max, tilt_count)
                            path = []
                            for row_idx, tilt in enumerate(tilts):
                                row = pans if row_idx % 2 == 0 else list(reversed(pans))
                                for pan in row:
                                    path.append((pan, tilt))
                            return path[:max_points]

                        def _location_path_with_cap(pan_range, tilt_range, pan_step, tilt_step,
                                                    max_points=24):
                            path = generate_scan_path(pan_range, tilt_range, pan_step, tilt_step)
                            if len(path) <= max_points:
                                return path
                            logger.warning(
                                f"⚠️ [T3] 候选框扫描点过多({len(path)}点)，兜底压缩到不超过{max_points}点"
                            )
                            return _bounded_location_path(pan_range, tilt_range, max_points=max_points)

                        def _point_key(point):
                            return (round(float(point[0]), 2), round(float(point[1]), 2))

                        def _dedupe_location_points(points):
                            deduped = []
                            seen = set()
                            for point in points:
                                key = _point_key(point)
                                if key in seen:
                                    continue
                                seen.add(key)
                                deduped.append(key)
                            return deduped

                        def _target_guard_points_for_range(pan_rng, tilt_rng):
                            clipped_pan, clipped_tilt = _clip_to_location_bound(pan_rng, tilt_rng)
                            if not clipped_pan or not clipped_tilt:
                                return []
                            pan_min, pan_max = float(clipped_pan[0]), float(clipped_pan[1])
                            tilt_min, tilt_max = float(clipped_tilt[0]), float(clipped_tilt[1])
                            center = ((pan_min + pan_max) / 2.0, (tilt_min + tilt_max) / 2.0)
                            required = [
                                (pan_min, tilt_min),
                                (pan_min, tilt_max),
                                (pan_max, tilt_min),
                                (pan_max, tilt_max),
                                center,
                            ]
                            pan_span = abs(pan_max - pan_min)
                            tilt_span = abs(tilt_max - tilt_min)
                            pan_can_split = pan_span >= target_guard_min_span
                            tilt_can_split = tilt_span >= target_guard_min_span
                            if target_guard_inner_points <= 0 or not pan_can_split and not tilt_can_split:
                                return _dedupe_location_points(required)

                            grid_side = max(3, int(math.ceil(math.sqrt(target_guard_inner_points))) + 2)
                            pan_samples = (
                                [
                                    pan_min + pan_span * i / (grid_side + 1)
                                    for i in range(1, grid_side + 1)
                                ]
                                if pan_can_split else [center[0]]
                            )
                            tilt_samples = (
                                [
                                    tilt_min + tilt_span * i / (grid_side + 1)
                                    for i in range(1, grid_side + 1)
                                ]
                                if tilt_can_split else [center[1]]
                            )
                            candidates = [(pan, tilt) for tilt in tilt_samples for pan in pan_samples]
                            picked = []
                            seed_points = [_point_key(point) for point in required]
                            while candidates and len(picked) < target_guard_inner_points:
                                refs = seed_points + [_point_key(point) for point in picked]
                                best_idx = 0
                                best_score = -1.0
                                for idx, candidate in enumerate(candidates):
                                    cand_key = _point_key(candidate)
                                    score = min(
                                        math.hypot(cand_key[0] - ref[0], cand_key[1] - ref[1])
                                        for ref in refs
                                    )
                                    if score > best_score:
                                        best_idx = idx
                                        best_score = score
                                picked.append(candidates.pop(best_idx))
                            return _dedupe_location_points(required + picked)

                        def _build_target_guard_debt():
                            debt = []
                            for rng_idx, (pan_rng, tilt_rng) in enumerate(zip(ls_pan_ranges, ls_tilt_ranges)):
                                points = _target_guard_points_for_range(pan_rng, tilt_rng)
                                if not points:
                                    logger.warning(
                                        f"⚠️ [T3] 目标区域#{rng_idx} 与定位业务运行范围无交集，无法生成保底点"
                                    )
                                    continue
                                debt.append({
                                    'range_idx': rng_idx,
                                    'remaining': points,
                                })
                                logger.info(
                                    f"📍 [T3] 目标区域#{rng_idx} 保底覆盖点 {len(points)} 个 "
                                    f"(四角+中心+内部均匀点)"
                                )
                            return debt

                        def _remove_covered_guard_points(guard_debt, path_points):
                            covered = {_point_key(point) for point in path_points}
                            if not covered:
                                return 0
                            removed = 0
                            for item in guard_debt:
                                remaining = []
                                for point in item['remaining']:
                                    if _point_key(point) in covered:
                                        removed += 1
                                    else:
                                        remaining.append(point)
                                item['remaining'] = remaining
                            return removed

                        def _build_guard_segments(guard_debt, stage_name, take_final=False, exclude_points=None):
                            exclude_keys = {_point_key(point) for point in (exclude_points or [])}
                            pending = [
                                {'range_idx': item['range_idx'], 'point': point}
                                for item in guard_debt
                                for point in item['remaining']
                                if _point_key(point) not in exclude_keys
                            ]
                            if not pending:
                                return []
                            take_count = len(pending) if take_final else int(math.ceil(len(pending) / 2.0))
                            selected = pending[:take_count]
                            by_range = {}
                            for item in selected:
                                by_range.setdefault(item['range_idx'], []).append(item['point'])
                            segments = []
                            for range_idx, points in by_range.items():
                                path = _dedupe_location_points(points)
                                if not path:
                                    continue
                                segments.append({
                                    'segment_idx': f"guard_{stage_name}_{range_idx}",
                                    'macs': list(active_macs),
                                    'path': path,
                                    'target_guard': True,
                                    'range_idx': range_idx,
                                })
                            logger.info(
                                f"📍 [T3] {STAGE_LABELS.get(stage_name, stage_name)} 追加目标区保底点 "
                                f"{sum(len(seg['path']) for seg in segments)} 个，"
                                f"待覆盖 {sum(len(item['remaining']) for item in guard_debt)} 个"
                            )
                            return segments

                        def _safe_mac(mac):
                            return mac.replace(':', '-')

                        def _current_project_id():
                            try:
                                raw_project = r.get(CURRENT_PROJECT_KEY)
                                if raw_project:
                                    project = json.loads(raw_project)
                                    if project.get('project_id'):
                                        return project['project_id']
                            except Exception:
                                pass
                            return f"location_{int(time.time())}"

                        # 每个定位流程只校准一次。
                        if _goto_calibration_point(ptz, r, context="location_scan 开始", stop_check_fn=_ls_stopped):
                            logger.info("✅ [T3] 定位扫描启动校准完成")
                        else:
                            logger.warning("⚠️ [T3] 定位扫描启动校准失败，继续")
                        if _ls_stopped():
                            r.set('location_scan:status', json.dumps({
                                'phase': 'idle', 'status': 'stopped', 'ts': time.time()
                            }))
                            _finalize_current_project(r, expected_scan_type='location', status='STOPPED')
                            continue
                        # 校准完成后立即写入 LOCATION_SCANNING 状态，避免前端一直看到 CALIBRATING
                        _cur_pan, _cur_tilt = ptz.get_position()
                        patch_ptz_status(r, {
                            'position': {'pan': round(_cur_pan, 2), 'tilt': round(_cur_tilt, 2)},
                            'state': 'LOCATION_SCANNING',
                            'location_scan': {
                                'active': True,
                                'phase': 'channel_probe',
                                'state': 'running',
                                'scan_id': ls_scan_id,
                                'stop_requested': False,
                                'terminal': False,
                            },
                        })

                        fast_tracked_macs = set()
                        fast_verified_macs = {}  # {mac: {'ch_info': ..., 'new_rssi': ..., 'pt_data': ...}}
                        fast_verified_captured = False  # 标记快速校验后是否已抓包
                        if round_num > 1 and mac_history:
                            logger.info(f"⚡ [T3] 第 {round_num} 轮：开始快速校验阶段 (针对 {len(mac_history)} 个已知设备)")
                            _ls_set_status('fast_verify')

                            # 第一步：检测所有 MAC
                            for mac, history in list(mac_history.items()):
                                if _ls_stopped():
                                    break
                                prev_pos = history['best_pos']
                                prev_rssi = history['best_rssi']
                                ch_info = {'channel': history['channel'], 'bandwidth': history['bandwidth']}

                                # visual→RF 映射
                                _fv_rf_pan, _fv_rf_tilt = prev_pos[0], prev_pos[1]
                                if _ls_ab:
                                    _fv_rf = visual_point_to_rf({"pan": prev_pos[0], "tilt": prev_pos[1]}, _ls_ab)
                                    _fv_rf_pan, _fv_rf_tilt = _fv_rf["pan"], _fv_rf["tilt"]
                                pt_data = _move_and_scan(
                                    point_id=f"verify_{mac}",
                                    target_pan=_fv_rf_pan,
                                    target_tilt=_fv_rf_tilt,
                                    configs=[{
                                        'channel': ch_info['channel'],
                                        'bandwidth': ch_info['bandwidth'],
                                        'target_macs': [mac],
                                    }],
                                    phase='fast_verify',
                                    current_point=1,
                                    total_points=1,
                                    dwell_time=6.0,
                                    visual_pan=prev_pos[0],
                                    visual_tilt=prev_pos[1],
                                )
                                if _ls_stopped():
                                    break

                                new_rssi = None
                                if pt_data:
                                    for scan_res in pt_data.get('scan_results', []):
                                        if scan_res.get('target_mac', '').lower().strip() == mac:
                                            new_rssi = scan_res.get('rssi_avg')
                                            break

                                if new_rssi is not None and new_rssi >= prev_rssi - track_rssi_threshold:
                                    logger.info(f"✅ [T3] 快速校验通过: {mac} 信号 ({prev_rssi} -> {new_rssi}) 满足阈值，跳过扫描")
                                    history['best_rssi'] = new_rssi
                                    fast_tracked_macs.add(mac)
                                    mac_channel_map[mac] = ch_info
                                    _record_point_data(pt_data, source_stage='fast_verify', segment_macs=[mac])
                                    fast_verified_macs[mac] = {'ch_info': ch_info, 'new_rssi': new_rssi, 'pt_data': pt_data}
                                else:
                                    logger.info(f"🔄 [T3] 快速校验失败: {mac} 信号 ({prev_rssi} -> {new_rssi}) 丢弃过大，需要重新定位")
                                    del mac_history[mac]

                            # 第二步：对通过的 MAC 统一抓包
                            if ls_capture_limit and fast_verified_macs:
                                logger.info(f"📦 [T3] Fast Verify 检测完成，开始抓包 {len(fast_verified_macs)} 个通过的 MAC")
                                fast_capture_items = list(fast_verified_macs.items())
                                for cap_idx, (mac, verify_info) in enumerate(fast_capture_items, start=1):
                                    if _ls_stopped():
                                        break
                                    ch_info = verify_info['ch_info']
                                    new_rssi = verify_info['new_rssi']

                                    # 移动到该 MAC 的 best_position 再抓包
                                    if mac in mac_history and mac_history[mac].get('best_pos'):
                                        best_pos = mac_history[mac]['best_pos']
                                        logger.info(f"📦 [T3] 移动到 MAC={mac} 最佳位置 ({best_pos[0]:.1f}, {best_pos[1]:.1f})")
                                        # visual→RF 映射
                                        _bp_rf_pan, _bp_rf_tilt = best_pos[0], best_pos[1]
                                        if _ls_ab:
                                            _bp_rf = visual_point_to_rf({"pan": best_pos[0], "tilt": best_pos[1]}, _ls_ab)
                                            _bp_rf_pan, _bp_rf_tilt = _bp_rf["pan"], _bp_rf["tilt"]
                                        if not safe_split_move(ptz, _bp_rf_pan, _bp_rf_tilt, order='pan_first', settle=1.0, stop_check_fn=_ls_stopped):
                                            if _ls_stopped():
                                                break
                                            logger.warning(f"⚠️ [T3] 移动到最佳位置失败，继续抓包")
                                        if _ls_stopped():
                                            break
                                        time.sleep(0.3)
                                        if _ls_stopped():
                                            break

                                    logger.info(f"📦 [T3] 抓包 MAC={mac}，时长={ls_capture_limit}s")
                                    _ls_set_status('capturing', {
                                        'mac': mac,
                                        'mac_idx': cap_idx - 1,
                                        'mac_index': cap_idx,
                                        'mac_total': len(fast_capture_items),
                                        'channel': ch_info['channel'],
                                        'bandwidth': ch_info['bandwidth'],
                                        'capture_time_limit': float(ls_capture_limit),
                                        'source_phase': 'fast_verify',
                                    })
                                    project_id = _current_project_id()
                                    notify_key = f"location_scan:capture:{_safe_mac(mac)}:{int(time.time() * 1000)}"
                                    pcap_filename = f"location/{project_id}/{_safe_mac(mac)}/part_001.pcap"
                                    r.lpush('capture:command_queue', json.dumps({
                                        'action': 'save_pcap',
                                        'channel': ch_info['channel'],
                                        'bandwidth': ch_info['bandwidth'],
                                        'target_mac': mac,
                                        'target_mac_match': 'any_addr',
                                        'pcap_filename': pcap_filename,
                                        'capture_time_limit': float(ls_capture_limit),
                                        'pcap_split_size_mb': ls_pcap_split_mb,
                                        'min_free_memory_mb': ls_min_free_memory_mb,
                                        'min_free_disk_mb': ls_min_free_disk_mb,
                                        'notify_key': notify_key,
                                    }))
                                    capture_result = None
                                    capture_deadline = time.time() + float(ls_capture_limit) + 45
                                    while time.time() < capture_deadline:
                                        if _ls_stopped():
                                            break
                                        raw = r.brpop(notify_key, timeout=1)
                                        if raw:
                                            capture_result = json.loads(raw[1])
                                            break
                                    if capture_result:
                                        logger.info(f"📦 [T3] 抓包完成 MAC={mac}，status={capture_result.get('status')}，文件={capture_result.get('pcap_files', [])}")
                                        # 记录结果
                                        best_pos, best_rssi = _best_for_mac(mac)
                                        identity = mac_identity.get(mac, {})
                                        omni_points = mac_omni_points.get(mac, {})
                                        best_omni_rssi = omni_points.get(best_pos)
                                        if best_omni_rssi is None and best_pos:
                                            for k, v in omni_points.items():
                                                if abs(k[0] - best_pos[0]) < 0.01 and abs(k[1] - best_pos[1]) < 0.01:
                                                    best_omni_rssi = v
                                                    break
                                        result_payload = {
                                            'status': 'found',
                                            'channel': ch_info['channel'],
                                            'bandwidth': ch_info['bandwidth'],
                                            'type': identity.get('type'),
                                            'subtype': identity.get('subtype'),
                                            'ssid': identity.get('ssid'),
                                            'best_position': {'pan': best_pos[0], 'tilt': best_pos[1]} if best_pos else None,
                                            'best_rssi': best_rssi,
                                            'best_omni_rssi': best_omni_rssi,
                                            'verify_rssi': new_rssi,
                                            'omni_verify_rssi': None,
                                            'scan_points': len(mac_rssi_points.get(mac, {})),
                                            'all_points': {
                                                f"{p[0]},{p[1]}": v for p, v in mac_rssi_points.get(mac, {}).items()
                                            },
                                            'omni_all_points': {
                                                f"{p[0]},{p[1]}": v for p, v in omni_points.items()
                                            },
                                            'capture_status': capture_result.get('status'),
                                            'capture_reason': capture_result.get('reason'),
                                            'capture_target_mac_match': capture_result.get('target_mac_match'),
                                            'pcap_files': capture_result.get('pcap_files', []),
                                        }
                                        ls_final_results[mac] = result_payload
                                        _ls_publish_results()
                                    else:
                                        logger.warning(f"⚠️ [T3] 抓包超时 MAC={mac}")

                                # 标记快速校验后已抓包
                                fast_verified_captured = True

                        # 1. 信道探测：固定信道或按 MAC 指定配置时跳过对应 MAC。
                        if ls_channel is not None:
                            logger.info(f"📡 [T3] 使用固定信道: ch{ls_channel} {ls_bandwidth}")
                            for mac in ls_target_macs:
                                mac_channel_map[mac] = {
                                    'channel': int(ls_channel),
                                    'bandwidth': str(ls_bandwidth),
                                }

                        if normalized_target_configs:
                            logger.info(
                                f"📡 [T3] 使用按 MAC 指定信道配置: "
                                f"{json.dumps(normalized_target_configs, ensure_ascii=False)}"
                            )
                            for mac, cfg in normalized_target_configs.items():
                                if mac in ls_target_macs:
                                    mac_channel_map[mac] = cfg

                        if len(mac_channel_map) >= len(set(ls_target_macs)):
                            logger.info("📡 [T3] 所有目标 MAC 已有信道/带宽配置，跳过自动信道探测")
                        else:
                            missing_macs = set(ls_target_macs) - fast_tracked_macs - set(mac_channel_map.keys())
                            logger.info(
                                f"📡 [T3] 需要自动信道探测的 MAC: {sorted(missing_macs)}；"
                                f"已有配置 MAC: {sorted(mac_channel_map.keys())}"
                            )
                            probe_points = _build_location_probe_points(
                                ls_pan_ranges,
                                ls_tilt_ranges,
                                limits={
                                    "pan_min": PAN_MIN,
                                    "pan_max": PAN_MAX,
                                    "tilt_min": TILT_MIN,
                                    "tilt_max": TILT_MAX,
                                },
                            )
                            total_probe_steps = len(probe_points) * ls_probe_rounds_max
                            logger.info(
                                f"📡 [T3] 开始信道探测：{len(probe_points)} 个探测点 × "
                                f"{ls_probe_rounds_max} 轮 = 共 {total_probe_steps} 步，"
                                f"待探测 MAC: {sorted(missing_macs)}"
                            )
                            for probe_round in range(1, ls_probe_rounds_max + 1):
                                if not missing_macs or _ls_stopped():
                                    break
                                logger.info(f"📡 [T3] 信道探测 第 {probe_round}/{ls_probe_rounds_max} 轮")
                                for point_idx, (probe_pan, probe_tilt) in enumerate(probe_points, start=1):
                                    if not missing_macs or _ls_stopped():
                                        break
                                    step_num = (probe_round - 1) * len(probe_points) + point_idx
                                    logger.info(
                                        f"📡 [T3] 信道探测 [{step_num}/{total_probe_steps}] "
                                        f"→ ({probe_pan:.1f}, {probe_tilt:.1f})，"
                                        f"剩余 MAC: {sorted(missing_macs)}"
                                    )
                                    _ls_set_status('channel_probe', {
                                        'probe_round': probe_round,
                                        'probe_rounds_max': ls_probe_rounds_max,
                                        'current_point': step_num,
                                        'total_points': total_probe_steps,
                                        'remaining_macs': sorted(missing_macs),
                                    })
                                    # visual→RF 映射
                                    _cp_rf_pan, _cp_rf_tilt = probe_pan, probe_tilt
                                    if _ls_ab:
                                        _cp_rf = visual_point_to_rf({"pan": probe_pan, "tilt": probe_tilt}, _ls_ab)
                                        _cp_rf_pan, _cp_rf_tilt = _cp_rf["pan"], _cp_rf["tilt"]
                                    if not safe_split_move(ptz, _cp_rf_pan, _cp_rf_tilt, order='pan_first', settle=1.0, stop_check_fn=_ls_stopped):
                                        if _ls_stopped():
                                            break
                                        logger.warning(f"⚠️ [T3] 信道探测点移动失败: ({probe_pan},{probe_tilt})")
                                        continue
                                    if _ls_stopped():
                                        break
                                    time.sleep(0.3)
                                    if _ls_stopped():
                                        break
                                    detect_notify_key = (
                                        f"detect_channels:notify:"
                                        f"{probe_round}:{point_idx}:{int(time.time() * 1000)}"
                                    )
                                    detect_stop_key = f"{detect_notify_key}:stop"
                                    r.delete(detect_notify_key, detect_stop_key)
                                    logger.info(
                                        f"🔖 [T3] 投递信道探测命令: "
                                        f"scan_id={ls_scan_id}, notify_key={detect_notify_key}, "
                                        f"stop_key={detect_stop_key}"
                                    )
                                    r.lpush('capture:command_queue', json.dumps({
                                        'action': 'detect_channels',
                                        'target_macs': sorted(missing_macs),
                                        'configs': LOCATION_SCAN_CONFIGS,
                                        'total_duration': len(LOCATION_SCAN_CONFIGS) * ls_probe_dwell,
                                        'notify_key': detect_notify_key,
                                        'stop_key': detect_stop_key,
                                        'scan_id': ls_scan_id,
                                    }))
                                    detect_result = None
                                    # 信道探测耗时 = 嗅探停留 + iw/驱动切信道开销。只按 dwell
                                    # 计算会在 66 个配置还没轮完时误判超时，导致 PTZ 进入下一点。
                                    probe_switch_overhead = max(1.5, ls_probe_dwell * 1.5)
                                    deadline = (
                                        time.time()
                                        + len(LOCATION_SCAN_CONFIGS) * (ls_probe_dwell + probe_switch_overhead)
                                        + 30
                                    )
                                    while time.time() < deadline:
                                        if _ls_stopped():
                                            break
                                        raw = r.brpop(detect_notify_key, timeout=1)
                                        if raw:
                                            detect_result = json.loads(raw[1])
                                            break
                                    if not detect_result or detect_result.get('status') != 'done':
                                        logger.warning(f"⚠️ [T3] 信道探测点失败: {detect_result}")
                                        if not _ls_stopped():
                                            r.setex(detect_stop_key, 600, 'timeout')
                                        continue
                                    r.delete(detect_stop_key)
                                    found_macs_this_point = []
                                    for mac, info in detect_result.get('results', {}).items():
                                        if isinstance(info, dict) and mac in missing_macs:
                                            mac_channel_map[mac] = info
                                            missing_macs.remove(mac)
                                            found_macs_this_point.append(mac)
                                    if found_macs_this_point:
                                        logger.info(f"📡 [T3] 信道探测 发现: {found_macs_this_point}")

                            logger.info(f"✅ [T3] 信道探测完成，已找到 {len(mac_channel_map)} 个 MAC 的信道")
                            for mac in sorted(missing_macs):
                                ls_final_results[mac] = {
                                    'status': 'not_found',
                                    'reason': 'channel_probe_not_found',
                                    'channel': None,
                                    'bandwidth': None,
                                    'best_position': None,
                                    'best_rssi': None,
                                    'scan_points': 0,
                                }
                            _ls_publish_results()

                        active_macs = [mac for mac in ls_target_macs if isinstance(mac_channel_map.get(mac), dict) and mac not in fast_tracked_macs]
                        if _ls_stopped():
                            r.set('location_scan:status', json.dumps({
                                'phase': 'idle', 'status': 'stopped', 'ts': time.time()
                            }))
                            _finalize_current_project(r, expected_scan_type='location', status='STOPPED')
                            continue

                        # 2. 扫描：第一轮 8°，第二轮 4°，第三轮 2°，逐级收缩候选区域。
                        if active_macs:
                            STAGE_LABELS = {
                                'coarse': '粗扫',
                                'mid': '中扫',
                                'fine': '细扫',
                            }

                            def _run_location_segments(stage_name, segments, macs_for_segment):
                                total_points = sum(len(seg['path']) for seg in segments)
                                seg_count = len(segments)
                                stage_label = STAGE_LABELS.get(stage_name, stage_name)
                                logger.info(
                                    f"📍 [T3] 开始 {stage_label}：{seg_count} 个候选框，共 {total_points} 个点位"
                                )
                                if not segments or total_points <= 0:
                                    return []
                                done_points = 0
                                completed_points = []
                                # 构建外扩点集合（用于区分内外点）
                                outer_keys = set()
                                for seg in segments:
                                    for point in seg.get('outer_points', []):
                                        outer_keys.add((round(point[0], 2), round(point[1], 2)))

                                for seg_idx, seg in enumerate(segments):
                                    if _ls_stopped():
                                        break
                                    seg_macs = macs_for_segment(seg)
                                    configs = _group_location_configs(mac_channel_map, seg_macs)
                                    seg_point_count = len(seg['path'])
                                    seg_type = "目标区保底" if seg.get('target_guard') else "RSSI候选"
                                    logger.info(
                                        f"📍 [T3] {stage_label} 候选框 [{seg_idx+1}/{seg_count}]："
                                        f"{seg_point_count} 点，类型={seg_type}，MAC={seg_macs}"
                                    )
                                    for pt_idx, (target_pan, target_tilt) in enumerate(seg['path']):
                                        if _ls_stopped():
                                            break
                                        done_points += 1
                                        logger.info(
                                            f"📍 [T3] {stage_label} [{done_points}/{total_points}] "
                                            f"→ ({target_pan:.1f}, {target_tilt:.1f})"
                                        )
                                        _ls_set_status(stage_name, {
                                            'current_point': done_points,
                                            'total_points': total_points,
                                            'active_macs': seg_macs,
                                        })
                                        point_id = (
                                            f"ls_{stage_name}_{seg.get('segment_idx', 0)}_"
                                            f"{pt_idx}_{int(time.time() * 1000)}"
                                        )
                                        # visual→RF 映射
                                        _ms_rf_pan, _ms_rf_tilt = target_pan, target_tilt
                                        if _ls_ab:
                                            _ms_rf = visual_point_to_rf({"pan": target_pan, "tilt": target_tilt}, _ls_ab)
                                            _ms_rf_pan, _ms_rf_tilt = _ms_rf["pan"], _ms_rf["tilt"]
                                        pt_data = _move_and_scan(
                                            point_id, _ms_rf_pan, _ms_rf_tilt, configs,
                                            phase=stage_name, current_point=done_points,
                                            total_points=total_points,
                                            visual_pan=target_pan, visual_tilt=target_tilt,
                                        )
                                        if pt_data:
                                            completed_points.append((target_pan, target_tilt))
                                        # 对于coarse阶段，区分内外点记录到不同stage
                                        if stage_name == 'coarse':
                                            point_key = (round(target_pan, 2), round(target_tilt, 2))
                                            if point_key in outer_keys:
                                                _record_point_data(pt_data, source_stage='coarse_outer', segment_macs=seg_macs)
                                            else:
                                                _record_point_data(pt_data, source_stage='coarse_inner', segment_macs=seg_macs)
                                        else:
                                            _record_point_data(pt_data, source_stage=stage_name, segment_macs=seg_macs)
                                    if _ls_stopped():
                                        break
                                logger.info(f"✅ [T3] {stage_label} 完成：{done_points}/{total_points} 点")

                                # 记录本阶段实际扫描的点位（scan_points_record保持简单数组，去重）
                                # 注意：coarse阶段由外部代码分别记录到coarse_inner和coarse_outer
                                if stage_name in scan_points_record and stage_name != 'coarse':
                                    seen_points = set()
                                    for pan, tilt in completed_points:
                                        key = (round(pan, 2), round(tilt, 2))
                                        if key not in seen_points:
                                            seen_points.add(key)
                                            scan_points_record[stage_name].append({
                                                'pan': key[0],
                                                'tilt': key[1],
                                            })

                                return completed_points

                            target_guard_debt = _build_target_guard_debt()
                            round1_segments = []
                            for rng_idx, (pan_rng, tilt_rng) in enumerate(zip(ls_pan_ranges, ls_tilt_ranges)):
                                # 粗扫：外扩8点 + 内部30-40点
                                exp_pan, exp_tilt = _clip_expanded(pan_rng, tilt_rng, ls_expand)
                                if not exp_pan or not exp_tilt:
                                    logger.warning(
                                        f"⚠️ [T3] 用户范围#{rng_idx} 扩边后与定位业务运行范围无交集，跳过: "
                                        f"pan={pan_rng}, tilt={tilt_rng}"
                                    )
                                    continue

                                # 外扩8点：四角 + 四边中点
                                outer_points = [
                                    # 四角
                                    (exp_pan[0], exp_tilt[0]),
                                    (exp_pan[0], exp_tilt[1]),
                                    (exp_pan[1], exp_tilt[0]),
                                    (exp_pan[1], exp_tilt[1]),
                                    # 四边中点
                                    ((exp_pan[0] + exp_pan[1]) / 2.0, exp_tilt[0]),
                                    ((exp_pan[0] + exp_pan[1]) / 2.0, exp_tilt[1]),
                                    (exp_pan[0], (exp_tilt[0] + exp_tilt[1]) / 2.0),
                                    (exp_pan[1], (exp_tilt[0] + exp_tilt[1]) / 2.0),
                                ]

                                # 内部点：先裁剪到location_bound，再生成30-40个均匀点
                                inner_pan, inner_tilt = _clip_to_location_bound(pan_rng, tilt_rng)
                                if not inner_pan or not inner_tilt:
                                    logger.warning(f"⚠️ [T3] 用户范围#{rng_idx} 与定位业务运行范围无交集，跳过内部点")
                                    inner_path = []
                                else:
                                    inner_path = _uniform_location_path(
                                        pan_range=inner_pan,
                                        tilt_range=inner_tilt,
                                        min_points=30,
                                        max_points=40,
                                        hard_max_points=44,
                                    )

                                # 合并外扩点和内部点（去重）
                                combined_path = []
                                seen_keys = set()
                                for point in outer_points + inner_path:
                                    key = (round(point[0], 2), round(point[1], 2))
                                    if key not in seen_keys:
                                        seen_keys.add(key)
                                        combined_path.append(key)

                                logger.info(
                                    f"📍 [T3] 粗扫区域#{rng_idx}: 外扩{len(outer_points)}点 + "
                                    f"内部{len(inner_path)}点 = 总{len(combined_path)}点"
                                )

                                round1_segments.append({
                                    'segment_idx': rng_idx,
                                    'path': combined_path,
                                    'outer_points': outer_points,
                                    'inner_points': inner_path,
                                })
                            # 分别记录外扩点和内部点到不同stage
                            # 注意：扫描时仍然合并在一起扫描，但记录时分开
                            coarse_completed_points = _run_location_segments(
                                'coarse',
                                round1_segments,
                                lambda seg: active_macs,
                            )
                            # 将completed_points按内外分开记录到scan_points_record（去重）
                            outer_keys = set()
                            inner_keys = set()
                            for seg in round1_segments:
                                for point in seg.get('outer_points', []):
                                    outer_keys.add((round(point[0], 2), round(point[1], 2)))
                                for point in seg.get('inner_points', []):
                                    inner_keys.add((round(point[0], 2), round(point[1], 2)))
                            seen_inner = set()
                            seen_outer = set()
                            for pan, tilt in coarse_completed_points:
                                key = (round(pan, 2), round(tilt, 2))
                                if key in inner_keys and key not in seen_inner:
                                    seen_inner.add(key)
                                    scan_points_record['coarse_inner'].append({'pan': key[0], 'tilt': key[1]})
                                elif key in outer_keys and key not in seen_outer:
                                    seen_outer.add(key)
                                    scan_points_record['coarse_outer'].append({'pan': key[0], 'tilt': key[1]})
                            # 同时记录到coarse（合并内外点）
                            scan_points_record['coarse'] = scan_points_record['coarse_inner'] + scan_points_record['coarse_outer']
                            covered_by_coarse = _remove_covered_guard_points(
                                target_guard_debt,
                                coarse_completed_points,
                            )
                            if covered_by_coarse:
                                logger.info(f"📍 [T3] 粗扫已实际覆盖目标区保底点 {covered_by_coarse} 个")

                            if not _ls_stopped():
                                mid_boxes = _build_fine_boxes(active_macs, source_stage='coarse_inner', expand_deg=0.0)
                                mid_segments = []
                                for box_idx, box in enumerate(mid_boxes):
                                    # 中扫：24-30个均匀点位，无外扩
                                    mid_path = _uniform_location_path(
                                        pan_range=box['pan_range'],
                                        tilt_range=box['tilt_range'],
                                        min_points=24,
                                        max_points=30,
                                        hard_max_points=34,
                                    )
                                    mid_segments.append({
                                        'segment_idx': box_idx,
                                        'macs': box['macs'],
                                        'path': mid_path,
                                    })
                                    logger.info(f"📍 [T3] 中扫候选框#{box_idx}: {len(mid_path)}点")
                                mid_existing_points = [point for seg in mid_segments for point in seg['path']]
                                mid_segments.extend(_build_guard_segments(
                                    target_guard_debt,
                                    stage_name='mid',
                                    take_final=False,
                                    exclude_points=mid_existing_points,
                                ))
                                mid_completed_points = _run_location_segments(
                                    'mid',
                                    mid_segments,
                                    lambda seg: seg['macs'],
                                )
                                covered_by_mid = _remove_covered_guard_points(
                                    target_guard_debt,
                                    mid_completed_points,
                                )
                                if covered_by_mid:
                                    logger.info(f"📍 [T3] 中扫已实际覆盖目标区保底点 {covered_by_mid} 个")

                            if not _ls_stopped():
                                fine_boxes = _build_fine_boxes(active_macs, source_stage='mid', expand_deg=0.0)
                                fine_segments = []
                                for box_idx, box in enumerate(fine_boxes):
                                    # 细扫：获取top5强点作为抖动源
                                    jitter_sources = []
                                    for mac in box.get('macs', []):
                                        mid_points = mac_stage_points.get(mac, {}).get('mid', {})
                                        if mid_points:
                                            # 过滤掉-100的点
                                            valid_points = [(p, r) for p, r in mid_points.items() if r > -100]
                                            if valid_points:
                                                # 取top5强点
                                                sorted_points = sorted(valid_points, key=lambda x: x[1], reverse=True)[:5]
                                                for point, rssi in sorted_points:
                                                    jitter_sources.append(point)

                                    # 细扫：24-30个均匀点位 + top5抖动点
                                    fine_path = _uniform_location_path(
                                        pan_range=box['pan_range'],
                                        tilt_range=box['tilt_range'],
                                        min_points=24,
                                        max_points=30,
                                        jitter_sources=jitter_sources,
                                        jitter_min_deg=1.0,
                                        hard_max_points=34,
                                    )
                                    fine_segments.append({
                                        'segment_idx': box_idx,
                                        'macs': box['macs'],
                                        'path': fine_path,
                                    })
                                    logger.info(
                                        f"📍 [T3] 细扫候选框#{box_idx}: {len(fine_path)}点, "
                                        f"抖动源{len(jitter_sources)}个"
                                    )
                                fine_existing_points = [point for seg in fine_segments for point in seg['path']]
                                fine_segments.extend(_build_guard_segments(
                                    target_guard_debt,
                                    stage_name='fine',
                                    take_final=True,
                                    exclude_points=fine_existing_points,
                                ))
                                fine_completed_points = _run_location_segments(
                                    'fine',
                                    fine_segments,
                                    lambda seg: seg['macs'],
                                )
                                covered_by_fine = _remove_covered_guard_points(
                                    target_guard_debt,
                                    fine_completed_points,
                                )
                                if covered_by_fine:
                                    logger.info(f"📍 [T3] 细扫已实际覆盖目标区保底点 {covered_by_fine} 个")

                        # 3. 最终定位确认：移动到每个 MAC 最强点再确认一次。
                        verify_total = len([m for m in ls_target_macs if m not in ls_final_results or ls_final_results[m].get('status') != 'not_found'])
                        logger.info(f"🎯 [T3] 开最终定位确认：{verify_total} 个 MAC")
                        for mac_idx, mac in enumerate(ls_target_macs):
                            if _ls_stopped():
                                break
                            if mac in ls_final_results and ls_final_results[mac].get('status') == 'not_found':
                                continue
                            ch_info = mac_channel_map.get(mac)
                            if not isinstance(ch_info, dict):
                                ls_final_results[mac] = {
                                    'status': 'not_found',
                                    'reason': 'channel_probe_not_found',
                                    'best_position': None,
                                    'best_rssi': None,
                                    'scan_points': 0,
                                }
                                _ls_publish_results()
                                continue
                            best_pos, best_rssi = _best_for_mac(mac)
                            if best_pos is None:
                                ls_final_results[mac] = {
                                    'status': 'no_signal',
                                    'channel': ch_info['channel'],
                                    'bandwidth': ch_info['bandwidth'],
                                    'best_position': None,
                                    'best_rssi': None,
                                    'scan_points': 0,
                                }
                                _ls_publish_results()
                                continue
                            logger.info(
                                f"🎯 [T3] 最终确认 [{mac_idx+1}/{len(ls_target_macs)}] "
                                f"MAC={mac} → ({best_pos[0]:.1f}, {best_pos[1]:.1f})，"
                                f"RSSI={best_rssi:.1f}，ch{ch_info['channel']} {ch_info['bandwidth']}"
                            )
                            _ls_set_status('locating', {
                                'mac': mac,
                                'mac_idx': mac_idx,
                                'mac_total': len(ls_target_macs),
                                'current_point': mac_idx + 1,
                                'total_points': len(ls_target_macs),
                                'channel': ch_info['channel'],
                                'bandwidth': ch_info['bandwidth'],
                            })
                            verify_point_id = f"ls_verify_{mac_idx}_{int(time.time() * 1000)}"
                            # visual→RF 映射
                            _vf_rf_pan, _vf_rf_tilt = best_pos[0], best_pos[1]
                            if _ls_ab:
                                _vf_rf = visual_point_to_rf({"pan": best_pos[0], "tilt": best_pos[1]}, _ls_ab)
                                _vf_rf_pan, _vf_rf_tilt = _vf_rf["pan"], _vf_rf["tilt"]
                            verify_data = _move_and_scan(
                                verify_point_id, _vf_rf_pan, _vf_rf_tilt,
                                [{
                                    'channel': ch_info['channel'],
                                    'bandwidth': ch_info['bandwidth'],
                                    'target_macs': [mac],
                                }],
                                phase='locating', current_point=mac_idx + 1,
                                total_points=len(ls_target_macs),
                                visual_pan=best_pos[0], visual_tilt=best_pos[1],
                            )
                            if _ls_stopped():
                                break
                            verify_rssi = None
                            verify_omni_rssi = None
                            if verify_data:
                                _record_point_data(verify_data, source_stage='locating', segment_macs=[mac])
                                # 记录verify点位
                                scan_points_record['verify'].append({
                                    'pan': round(best_pos[0], 2),
                                    'tilt': round(best_pos[1], 2),
                                })
                                for item in verify_data.get('scan_results', []):
                                    if item.get('target_mac', '').lower() == mac:
                                        verify_rssi = item.get('rssi_avg')
                                        verify_omni_rssi = item.get('omni_rssi_avg')
                                        break
                            # verify只确认，不重新决定best，使用verify前算出的best_pos/best_rssi
                            identity = mac_identity.get(mac, {})
                            omni_points = mac_omni_points.get(mac, {})
                            best_pos_key = f"{best_pos[0]},{best_pos[1]}"
                            best_omni_rssi = omni_points.get(best_pos)
                            if best_omni_rssi is None:
                                for k, v in omni_points.items():
                                    if abs(k[0] - best_pos[0]) < 0.01 and abs(k[1] - best_pos[1]) < 0.01:
                                        best_omni_rssi = v
                                        break
                            result_payload = {
                                'status': 'found',
                                'channel': ch_info['channel'],
                                'bandwidth': ch_info['bandwidth'],
                                'type': identity.get('type'),
                                'subtype': identity.get('subtype'),
                                'ssid': identity.get('ssid'),
                                'best_position': {'pan': best_pos[0], 'tilt': best_pos[1]},
                                'best_rssi': best_rssi,
                                'best_omni_rssi': best_omni_rssi,
                                'verify_rssi': verify_rssi,
                                'omni_verify_rssi': verify_omni_rssi,
                                'scan_points': len(mac_rssi_points.get(mac, {})),
                            }
                            if _ls_ab:
                                # best_pos 已是 visual 坐标；rf_best_position = visual - bias
                                result_payload['rf_best_position'] = visual_point_to_rf(
                                    {"pan": best_pos[0], "tilt": best_pos[1]}, _ls_ab)
                                result_payload['antenna_bias'] = {
                                    'pan_bias_deg': _ls_ab['pan_bias_deg'],
                                    'tilt_bias_deg': _ls_ab['tilt_bias_deg'],
                                }
                            result_payload.update({
                                'all_points': {
                                    f"{p[0]},{p[1]}": v for p, v in mac_rssi_points.get(mac, {}).items()
                                },
                                'omni_all_points': {
                                    f"{p[0]},{p[1]}": v for p, v in omni_points.items()
                                },
                            })

                            if ls_capture_limit and not fast_verified_captured:
                                logger.info(f"📦 [T3] 开始抓包 MAC={mac}，时长={ls_capture_limit}s")
                                _ls_set_status('capturing', {
                                    'mac': mac,
                                    'mac_idx': mac_idx,
                                    'mac_index': mac_idx + 1,
                                    'mac_total': len(ls_target_macs),
                                    'channel': ch_info['channel'],
                                    'bandwidth': ch_info['bandwidth'],
                                    'capture_time_limit': float(ls_capture_limit),
                                    'source_phase': 'locating',
                                })
                                project_id = _current_project_id()
                                notify_key = f"location_scan:capture:{_safe_mac(mac)}:{int(time.time() * 1000)}"
                                pcap_filename = f"location/{project_id}/{_safe_mac(mac)}/part_001.pcap"
                                r.lpush('capture:command_queue', json.dumps({
                                    'action': 'save_pcap',
                                    'channel': ch_info['channel'],
                                    'bandwidth': ch_info['bandwidth'],
                                    'target_mac': mac,
                                    'target_mac_match': 'any_addr',
                                    'pcap_filename': pcap_filename,
                                    'capture_time_limit': float(ls_capture_limit),
                                    'pcap_split_size_mb': ls_pcap_split_mb,
                                    'min_free_memory_mb': ls_min_free_memory_mb,
                                    'min_free_disk_mb': ls_min_free_disk_mb,
                                    'notify_key': notify_key,
                                }))
                                capture_result = None
                                capture_deadline = time.time() + float(ls_capture_limit) + 45
                                while time.time() < capture_deadline:
                                    if _ls_stopped():
                                        break
                                    raw = r.brpop(notify_key, timeout=1)
                                    if raw:
                                        capture_result = json.loads(raw[1])
                                        break
                                if capture_result:
                                    result_payload['capture_status'] = capture_result.get('status')
                                    result_payload['capture_reason'] = capture_result.get('reason')
                                    result_payload['capture_target_mac_match'] = capture_result.get('target_mac_match')
                                    result_payload['pcap_files'] = capture_result.get('pcap_files', [])
                                    logger.info(f"📦 [T3] 抓包完成 MAC={mac}，status={capture_result.get('status')}，文件={capture_result.get('pcap_files', [])}")
                                    if capture_result.get('status') == 'stopped':
                                        result_payload['status'] = 'stopped'
                                        ls_stop_reason = capture_result.get('reason') or 'capture_stopped'
                                        r.set('location_scan:stop', json.dumps({
                                            'scan_id': ls_scan_id, 'ts': time.time(),
                                        }), ex=120)
                                else:
                                    result_payload['capture_status'] = 'timeout'
                                    result_payload['pcap_files'] = [f"/mnt/data/{pcap_filename}"]
                                    logger.warning(f"⚠️ [T3] 抓包超时 MAC={mac}")

                            ls_final_results[mac] = result_payload

                            # 更新 mac_history 以备下一轮快速校验
                            if result_payload.get('status') == 'found':
                                prev_pcaps = mac_history.get(mac, {}).get('pcap_files', [])
                                curr_pcaps = result_payload.get('pcap_files', [])
                                merged_pcaps = list(set(prev_pcaps + curr_pcaps))
                                result_payload['pcap_files'] = merged_pcaps

                                mac_history[mac] = {
                                    'best_pos': (result_payload['best_position']['pan'], result_payload['best_position']['tilt']),
                                    'best_rssi': result_payload['best_rssi'],
                                    'channel': result_payload['channel'],
                                    'bandwidth': result_payload['bandwidth'],
                                    'pcap_files': merged_pcaps
                                }

                            _ls_publish_results()
                            if _ls_stopped():
                                break

                        # 输出本轮结果汇总
                        found_macs = [m for m, res in ls_final_results.items() if res.get('status') == 'found']
                        not_found_macs = [m for m, res in ls_final_results.items() if res.get('status') == 'not_found']
                        no_signal_macs = [m for m, res in ls_final_results.items() if res.get('status') == 'no_signal']
                        logger.info(
                            f"📊 [T3] 第 {round_num} 轮结果汇总："
                            f"定位成功={len(found_macs)}，未找到={len(not_found_macs)}，无信号={len(no_signal_macs)}"
                        )
                        for mac in found_macs:
                            res = ls_final_results[mac]
                            pos = res.get('best_position', {})
                            logger.info(
                                f"  ✅ {mac}: ({pos.get('pan', '?')}, {pos.get('tilt', '?')}) "
                                f"RSSI={res.get('best_rssi', '?'):.1f} "
                                f"ch{res.get('channel', '?')} {res.get('bandwidth', '?')}"
                            )

                        final_status = 'stopped' if _ls_stopped() else 'done'
                        r.set('location_scan:status', json.dumps({
                            'phase': 'idle',
                            'status': final_status,
                            'reason': ls_stop_reason or ('manual_stop' if final_status == 'stopped' else None),
                            'ts': time.time(),
                            'mac_count': len(ls_final_results),
                        }))
                        state = 'IDLE'
                        try:
                            _end_pan, _end_tilt = ptz.get_position()
                        except Exception:
                            _end_pan, _end_tilt = 0.0, 0.0
                        ls_final_state = 'stopped' if final_status == 'stopped' else 'done'
                        patch_ptz_status(r, {
                            'position': {'pan': round(float(_end_pan or 0), 2), 'tilt': round(float(_end_tilt or 0), 2)},
                            'state': state,
                            'location_scan': {
                                'active': False,
                                'phase': final_status,
                                'state': ls_final_state,
                                'terminal': True,
                                'reason': ls_stop_reason or ('manual_stop' if ls_final_state == 'stopped' else None),
                            },
                        })
                        _finalize_current_project(
                            r, expected_scan_type='location',
                            status='STOPPED' if final_status == 'stopped' else 'SUCCESS'
                        )
                        logger.info(f"✅ [T3] 定位扫描结束，status={final_status}，共 {len(ls_final_results)} 个 MAC")
                        if time_interval is None:
                            break

                        logger.info(f"⏳ [T3] 等待 {time_interval}s 后进入下一轮...")
                        r.set('location_scan:status', json.dumps({
                            'phase': 'waiting',
                            'status': 'running',
                            'ts': time.time(),
                            'round_completed': round_num,
                            'next_round': round_num + 1,
                            'time_interval': time_interval,
                        }))
                        sleep_deadline = time.time() + time_interval
                        while time.time() < sleep_deadline:
                            if _ls_stopped():
                                break
                            time.sleep(0.5)
                        if _ls_stopped():
                            break
                        round_num += 1

                    # 定位扫描结束，清理 active_scan_id（允许新扫描启动）
                    # stop key 不立即删除，依靠 TTL 自然过期（120s），
                    # 避免已派发给 capture_worker 的旧命令读不到 stop 标志
                    r.delete(CURRENT_PROJECT_KEY, 'location_scan:active_scan_id')
                    try:
                        # ttl 返回 -1 表示无过期，需要补设
                        if r.get('location_scan:stop') and r.ttl('location_scan:stop') == -1:
                            r.expire('location_scan:stop', 120)
                    except Exception:
                        pass
                    continue

                # ============= 停止定位扫描命令 =============
                if action == 'stop_location_scan':
                    # scan_id 校验：旧 stop 命令可能无 scan_id，此时不处理（由 stop key 机制兜底）
                    _stop_ls_scan_id = command.get('scan_id')
                    if _stop_ls_scan_id:
                        _active_ls_id = r.get('location_scan:active_scan_id')
                        if _active_ls_id and _active_ls_id != _stop_ls_scan_id and not _active_ls_id.startswith('stopping:'):
                            logger.info(f"⏭️ [T3] stop 命令 scan_id 不匹配，跳过: cmd={_stop_ls_scan_id} active={_active_ls_id}")
                            continue
                    logger.info("🛑 [T3] 收到停止定位扫描命令")
                    r.set('location_scan:stop', json.dumps({
                        'scan_id': _stop_ls_scan_id, 'ts': time.time(),
                    }), ex=120)
                    r.set('capture:stop', '1', ex=120)
                    try:
                        ptz.stop()
                    except Exception as e:
                        logger.warning(f"🛑 [T3] 停止定位扫描时停止云台失败: {e}")
                    r.set('location_scan:status', json.dumps({
                        'phase': 'idle',
                        'status': 'stopped',
                        'ts': time.time(),
                    }))
                    state = 'IDLE'
                    patch_ptz_status(r, {
                        'position': {'pan': pan, 'tilt': tilt},
                        'state': state,
                        'location_scan': {
                            'active': False,
                            'phase': 'stopped',
                            'state': 'stopped',
                            'terminal': True,
                            'reason': 'manual_stop',
                        },
                    })
                    continue

                # ==================== S8 客户端扫描命令 ====================
                if action == 'start_client_scan':
                    """
                    S8 客户端扫描命令（单次任务，扫完即止，不循环）

                    参数：
                        - pan_range   : 用户选区水平范围 [min, max]
                        - tilt_range  : 用户选区垂直范围 [min, max]
                        - pan_step    : 水平步进（度）
                        - tilt_step   : 垂直步进（度）
                        - dwell_time  : 每格驻留时长（秒），不填则用 CLIENT_SCAN_DEFAULT_DWELL_TIME
                        - channel     : 信道（固定信道，暂不支持跳扫）
                        - bandwidth   : 带宽

                    扫描范围 = 用户选区向外扩展 CLIENT_SCAN_GUARD_STEPS 步（防边界误判）。
                    扫完后将汇总结果（含 confidence）写入 Redis client_scan:result。
                    任务期间每移动一个点位更新 client_scan:progress。
                    """
                    logger.info("📱 [S8] 收到客户端扫描命令")

                    # ── 解析参数 ──────────────────────────────────────────────
                    cs_pan_range  = command.get('pan_range',  [GLOBAL_PAN_MIN,  GLOBAL_PAN_MAX])
                    cs_tilt_range = command.get('tilt_range', [GLOBAL_TILT_MIN, GLOBAL_TILT_MAX])
                    cs_pan_step   = float(command.get('pan_step',  10.0))
                    cs_tilt_step  = float(command.get('tilt_step', 10.0))
                    cs_dwell      = float(command.get('dwell_time', CLIENT_SCAN_DEFAULT_DWELL_TIME))

                    # 信道配置：指定单信道 → 单配置；不指定 → 全频段跳扫（同 INITIAL_SCAN_CONFIGS）
                    _raw_channel = command.get('channel', None)
                    if _raw_channel is not None:
                        _raw_bw  = command.get('bandwidth', 'HT20')
                        cs_configs = [{'channel': int(_raw_channel), 'bandwidth': str(_raw_bw)}]
                        logger.info(f"📡 [S8] 单信道模式: ch{_raw_channel} {_raw_bw}")
                    else:
                        cs_configs = [{'channel': c['channel'], 'bandwidth': c['bandwidth']}
                                      for c in INITIAL_SCAN_CONFIGS]
                        logger.info(f"📡 [S8] 全频段跳扫模式: {len(cs_configs)} 个信道配置")

                    cs_pan_min  = float(cs_pan_range[0])
                    cs_pan_max  = float(cs_pan_range[1])
                    cs_tilt_min = float(cs_tilt_range[0])
                    cs_tilt_max = float(cs_tilt_range[1])

                    # 用户选区边界（用于判断每个点是否 in_target）
                    target_pan_min  = cs_pan_min
                    target_pan_max  = cs_pan_max
                    target_tilt_min = cs_tilt_min
                    target_tilt_max = cs_tilt_max

                    # 扩展后扫描范围（加 Guard Steps，clamp 到设备限位）
                    # 使用 ptz_process_main 内已初始化的本地限位变量，而非模块级 GLOBAL_PAN_MIN（可能为 None）
                    guard_pan_min  = max(PAN_MIN,  cs_pan_min  - CLIENT_SCAN_GUARD_STEPS * cs_pan_step)
                    guard_pan_max  = min(PAN_MAX,  cs_pan_max  + CLIENT_SCAN_GUARD_STEPS * cs_pan_step)
                    guard_tilt_min = max(TILT_MIN, cs_tilt_min - CLIENT_SCAN_GUARD_STEPS * cs_tilt_step)
                    guard_tilt_max = min(TILT_MAX, cs_tilt_max + CLIENT_SCAN_GUARD_STEPS * cs_tilt_step)

                    logger.info(f"📐 [S8] 用户选区: Pan=[{cs_pan_min},{cs_pan_max}] Tilt=[{cs_tilt_min},{cs_tilt_max}]")
                    logger.info(f"📐 [S8] 扩展扫描: Pan=[{guard_pan_min},{guard_pan_max}] Tilt=[{guard_tilt_min},{guard_tilt_max}] (guard={CLIENT_SCAN_GUARD_STEPS}步)")

                    # 生成扫描路径（扩展后范围）
                    cs_scan_path = generate_scan_path(
                        [guard_pan_min, guard_pan_max],
                        [guard_tilt_min, guard_tilt_max],
                        cs_pan_step, cs_tilt_step
                    )
                    if not cs_scan_path:
                        logger.error("❌ [S8] 扫描路径为空，中止")
                        r.set('client_scan:status', json.dumps({'state': 'error', 'message': '扫描路径为空'}))
                        continue

                    total_points = len(cs_scan_path)
                    logger.info(f"🗺️ [S8] 共 {total_points} 个点位 (含 Guard Zone)，"
                                f"每点 {len(cs_configs)} 个信道配置 × {cs_dwell}s")

                    # ── 初始化 Redis 状态 ─────────────────────────────────────
                    r.delete('client_scan:stop')
                    r.set('client_scan:status', json.dumps({
                        'state': 'running',
                        'total_points': total_points,
                        'completed_points': 0,
                        'configs_count': len(cs_configs),
                        'dwell_time_per_config': cs_dwell,
                        'guard_steps': CLIENT_SCAN_GUARD_STEPS,
                        'started_at': time.time(),
                    }))

                    # 所有点位的采集结果
                    cs_all_point_results = {}
                    cs_completed_points  = 0   # 已完成采集的点数（采集完毕才+1）

                    # ── 逐格扫描 ──────────────────────────────────────────────
                    try:
                        # 校准：前往限位点
                        logger.info("[S8] 🎯 校准：前往限位点...")
                        if _goto_calibration_point(ptz, r, context="S8客户端扫描开始前"):
                            logger.info("[S8] ✅ 校准完成")
                            time.sleep(0.5)

                        for cs_point_idx, (cs_target_pan, cs_target_tilt) in enumerate(cs_scan_path):
                            # 检查停止标志
                            if r.get('client_scan:stop'):
                                logger.info("🛑 [S8] 检测到停止标志，中止扫描")
                                break

                            cs_point_id = f"cs_point_{cs_point_idx}"

                            # 判断本点是否在用户选区内
                            in_target_pt = (
                                target_pan_min  <= cs_target_pan  <= target_pan_max and
                                target_tilt_min <= cs_target_tilt <= target_tilt_max
                            )

                            logger.info(f"📍 [S8] [{cs_point_idx+1}/{total_points}] "
                                        f"移动到 ({cs_target_pan}, {cs_target_tilt}) "
                                        f"in_target={in_target_pt}")

                            # 移动到目标点
                            try:
                                if not safe_split_move(ptz, cs_target_pan, cs_target_tilt,
                                                       order='pan_first', settle=0.5):
                                    logger.error(f"❌ [S8] 移动失败: ({cs_target_pan}, {cs_target_tilt})")
                                    continue
                                time.sleep(0.3)
                                cs_actual_pan, cs_actual_tilt = ptz.get_position()
                                pan, tilt = cs_actual_pan, cs_actual_tilt
                            except Exception as move_e:
                                logger.error(f"❌ [S8] 移动异常: {move_e}")
                                continue

                            # 移动到位后统一更新 PTZ 状态（含进度，复用 ptz:current_status，不另开 Key）
                            r.set(PTZ_STATUS_KEY, json.dumps({
                                'ts': time.time(),
                                'position': {'pan': round(cs_actual_pan, 2), 'tilt': round(cs_actual_tilt, 2)},
                                'state': 'CLIENT_SCANNING',
                                'client_scan': {
                                    'active':            True,
                                    'current_point':     cs_point_idx + 1,
                                    'total_points':      total_points,
                                    'completed_points':  cs_completed_points,
                                    'in_target':         in_target_pt,
                                }
                            }))

                            # 发送 scan_clients_at_point 命令给 capture_worker
                            cs_cmd = {
                                'action':     'scan_clients_at_point',
                                'point_id':   cs_point_id,
                                'configs':    cs_configs,
                                'dwell_time': cs_dwell,
                                'in_target':  in_target_pt,
                            }
                            r.lpush('capture:command_queue', json.dumps(cs_cmd))

                            # 等待完成通知（超时 = 配置数 × dwell_time × 2 + 30s 冗余）
                            cs_notify_timeout = int(len(cs_configs) * cs_dwell * 2) + 30
                            try:
                                cs_result_raw = r.brpop(
                                    f'client_scan:{cs_point_id}_notify',
                                    timeout=cs_notify_timeout
                                )
                                if cs_result_raw:
                                    _, cs_notify_json = cs_result_raw
                                    cs_notify = json.loads(cs_notify_json)
                                    if cs_notify.get('status') == 'done':
                                        cs_all_point_results[cs_point_id] = {
                                            'pan':          round(cs_actual_pan, 2),
                                            'tilt':         round(cs_actual_tilt, 2),
                                            'in_target':    in_target_pt,
                                            'client_count': cs_notify.get('client_count', 0),
                                            'clients':      cs_notify.get('clients', {}),
                                        }
                                        cs_completed_points += 1
                                        logger.info(f"✅ [S8] [{cs_point_id}] 发现客户端 {cs_notify.get('client_count', 0)} 个 "
                                                    f"({cs_completed_points}/{total_points} 已完成)")
                                        # 采集完毕后更新 completed_points
                                        r.set(PTZ_STATUS_KEY, json.dumps({
                                            'ts': time.time(),
                                            'position': {'pan': round(cs_actual_pan, 2), 'tilt': round(cs_actual_tilt, 2)},
                                            'state': 'CLIENT_SCANNING',
                                            'client_scan': {
                                                'active':           True,
                                                'current_point':    cs_point_idx + 1,
                                                'total_points':     total_points,
                                                'completed_points': cs_completed_points,
                                                'in_target':        in_target_pt,
                                            }
                                        }))
                                    elif cs_notify.get('status') == 'stopped':
                                        logger.info("🛑 [S8] capture_worker 报告被中断")
                                        break
                                    else:
                                        logger.warning(f"⚠️ [S8] [{cs_point_id}] notify status={cs_notify.get('status')}")
                                else:
                                    logger.warning(f"⚠️ [S8] [{cs_point_id}] 等待超时 ({cs_notify_timeout}s)")
                            except Exception as notify_e:
                                logger.error(f"❌ [S8] [{cs_point_id}] 等待通知异常: {notify_e}")

                    except Exception as cs_scan_e:
                        logger.error(f"❌ [S8] 扫描主循环异常: {cs_scan_e}")

                    # ── 汇总：找各客户端最强信号点位，计算 confidence ─────────
                    logger.info(f"💾 [S8] 扫描结束，共采集 {len(cs_all_point_results)} 个点位，开始汇总...")

                    # 聚合：{client_mac: {best_rssi, best_point_id, pan, tilt, in_target, ...}}
                    cs_client_summary = {}
                    for pt_id, pt_data in cs_all_point_results.items():
                        for mac, cli_info in pt_data.get('clients', {}).items():
                            rssi_avg = cli_info.get('rssi_avg')
                            if rssi_avg is None:
                                continue
                            if mac not in cs_client_summary or rssi_avg > cs_client_summary[mac]['best_rssi']:
                                cs_client_summary[mac] = {
                                    'best_rssi':      rssi_avg,
                                    'best_point_id':  pt_id,
                                    'pan':            pt_data['pan'],
                                    'tilt':           pt_data['tilt'],
                                    'in_target':      pt_data['in_target'],
                                    'ap_bssid':       cli_info.get('ap_bssid'),
                                    'ap_ssid':        cli_info.get('ap_ssid'),
                                    'ap_ssid_source': cli_info.get('ap_ssid_source'),
                                    'probe_ssids':    cli_info.get('probe_ssids', []),
                                    'status':         cli_info.get('status'),
                                    'sample_count':   cli_info.get('sample_count', 0),
                                    'omni_rssi_avg':  cli_info.get('omni_rssi_avg'),
                                }

                    # 跨客户端 SSID 传播：某个 bssid 在任意点位已知 SSID，填给同 bssid 的其他客户端
                    bssid_ssid_cache = {
                        s['ap_bssid']: (s['ap_ssid'], s['ap_ssid_source'])
                        for s in cs_client_summary.values()
                        if s.get('ap_bssid') and s.get('ap_ssid')
                    }
                    for summary in cs_client_summary.values():
                        if summary.get('ap_bssid') and summary.get('ap_ssid') is None:
                            cached = bssid_ssid_cache.get(summary['ap_bssid'])
                            if cached:
                                summary['ap_ssid']        = cached[0]
                                summary['ap_ssid_source'] = cached[1]

                    # 计算 confidence
                    #   high   → 最强点在用户选区内
                    #   medium → 最强点在 guard zone，但选区内也采集到过该 MAC
                    #   low    → 最强点在 guard zone，选区内完全未见该 MAC
                    for mac, summary in cs_client_summary.items():
                        if summary['in_target']:
                            summary['confidence'] = 'high'
                        else:
                            has_in_target_signal = any(
                                mac in pt_data.get('clients', {}) and pt_data['in_target']
                                for pt_data in cs_all_point_results.values()
                            )
                            summary['confidence'] = 'medium' if has_in_target_signal else 'low'

                    # 按 best_rssi 降序排列
                    sorted_clients = sorted(
                        cs_client_summary.items(),
                        key=lambda x: x[1]['best_rssi'],
                        reverse=True
                    )

                    cs_final_result = {
                        'finished_at':    time.time(),
                        'total_points':   total_points,
                        'scanned_points': len(cs_all_point_results),
                        'client_count':   len(cs_client_summary),
                        'clients':        dict(sorted_clients),
                        'target_range': {
                            'pan':  [target_pan_min,  target_pan_max],
                            'tilt': [target_tilt_min, target_tilt_max],
                        },
                        'guard_steps':         CLIENT_SCAN_GUARD_STEPS,
                        'configs_count':       len(cs_configs),
                        'dwell_time_per_config': cs_dwell,
                        'point_details':       cs_all_point_results,
                    }

                    r.set('client_scan:result', json.dumps(cs_final_result))
                    r.set('client_scan:status', json.dumps({
                        'state':          'done',
                        'client_count':   len(cs_client_summary),
                        'scanned_points': len(cs_all_point_results),
                        'total_points':   total_points,
                        'finished_at':    time.time(),
                    }))
                    r.delete('client_scan:stop')

                    # 恢复 PTZ 状态
                    try:
                        pan, tilt = ptz.get_position()
                    except Exception:
                        pass
                    r.set(PTZ_STATUS_KEY, json.dumps({
                        'ts': time.time(),
                        'position': {'pan': pan, 'tilt': tilt},
                        'state': 'IDLE',
                        'client_scan': {'active': False, 'phase': 'completed'},
                    }))
                    logger.info(f"✅ [S8] 客户端扫描结束，共发现 {len(cs_client_summary)} 个客户端")
                    continue

                # ============= S8 停止客户端扫描命令 =============
                if action == 'stop_client_scan':
                    """停止正在进行的客户端扫描"""
                    logger.info("🛑 [S8] 收到停止客户端扫描命令")
                    # 若扫描已结束（done/error），忽略重复 stop，避免覆盖状态
                    _cs_raw = r.get('client_scan:status')
                    if _cs_raw:
                        try:
                            _cs_state = json.loads(_cs_raw).get('state')
                            if _cs_state in ('done', 'error'):
                                logger.info(f"⚠️ [S8] 扫描已结束（state={_cs_state}），忽略重复 stop 命令")
                                continue
                        except Exception:
                            pass
                    r.set('client_scan:stop', '1', ex=120)
                    r.set('client_scan:status', json.dumps({
                        'state': 'stopping',
                        'ts': time.time(),
                    }))
                    logger.info("✅ [S8] 已设置 client_scan:stop 标志")
                    continue

                # ============= 指定点位抓包命令 =============
                if action == 'move_to_best_capture':
                    """
                    移动云台到指定点位，然后向 capture_worker 发起抓包。
                    由 web_server.py 的 /api/v1/ptz/capture_at_best 触发。
                    命令参数：pan / tilt / mac / channel / bandwidth / pcap_filename / capture_time_limit
                    """
                    _cab_pan      = command.get('pan')
                    _cab_tilt     = command.get('tilt')
                    _cab_mac      = ''.join(str(command.get('mac', '')).split()).lower()
                    _cab_channel  = command.get('channel')
                    _cab_bw       = command.get('bandwidth', 'HT20')
                    _cab_pcap     = command.get('pcap_filename')
                    _cab_limit    = command.get('capture_time_limit')
                    _cab_min_mem  = command.get('min_free_memory_mb')
                    _cab_min_disk = command.get('min_free_disk_mb')

                    # 天线偏差补偿：命令中的 pan/tilt 是 visual 坐标，需转 RF
                    _cab_ab = command.get('antenna_bias')
                    if _cab_ab and not _cab_ab.get('enabled'):
                        _cab_ab = None
                    _cab_rf_pan = _cab_pan
                    _cab_rf_tilt = _cab_tilt
                    if _cab_ab and _cab_pan is not None and _cab_tilt is not None:
                        _cab_rf = visual_point_to_rf({"pan": float(_cab_pan), "tilt": float(_cab_tilt)}, _cab_ab)
                        _cab_rf_pan = _cab_rf["pan"]
                        _cab_rf_tilt = _cab_rf["tilt"]

                    if _cab_pan is None or _cab_tilt is None or _cab_channel is None or not _cab_mac or not _cab_bw:
                        logger.error(f"❌ [capture_at_best] 命令缺少必要参数: {command}")
                        continue

                    logger.info(
                        f"📍 [capture_at_best] 移动到指定点位 "
                        f"pan={_cab_pan}, tilt={_cab_tilt} "
                        f"MAC={_cab_mac} ch={_cab_channel} bw={_cab_bw} "
                        f"pcap={_cab_pcap} limit={_cab_limit}"
                    )

                    # 更新 PTZ 状态为移动中
                    try:
                        _cur_pan, _cur_tilt = ptz.get_position()
                    except Exception:
                        _cur_pan, _cur_tilt = pan, tilt
                    r.set(PTZ_STATUS_KEY, json.dumps({
                        'ts':       time.time(),
                        'position': {'pan': round(float(_cur_pan or 0), 2),
                                     'tilt': round(float(_cur_tilt or 0), 2)},
                        'state':    'MOVING',
                        'capture_at_best': {
                            'active':  True,
                            'target':  {'pan': _cab_pan, 'tilt': _cab_tilt},
                            'mac':     _cab_mac,
                            'channel': _cab_channel,
                            'bandwidth': _cab_bw,
                        },
                    }))

                    # 执行分轴移动（使用 RF 坐标）
                    try:
                        _moved = safe_split_move(
                            ptz, float(_cab_rf_pan), float(_cab_rf_tilt),
                            order='pan_first', settle=1.0,
                        )
                    except Exception as _move_e:
                        logger.error(f"❌ [capture_at_best] 移动异常: {_move_e}")
                        _moved = False

                    if not _moved:
                        logger.error(
                            f"❌ [capture_at_best] 移动失败 "
                            f"pan={_cab_pan}, tilt={_cab_tilt}"
                        )
                        r.set(PTZ_STATUS_KEY, json.dumps({
                            'ts':    time.time(),
                            'position': {'pan': pan, 'tilt': tilt},
                            'state': 'IDLE',
                        }))
                        continue

                    # 移动成功，读取实际到位位置
                    try:
                        pan, tilt = ptz.get_position()
                    except Exception:
                        pass

                    logger.info(
                        f"✅ [capture_at_best] 到达指定点位 "
                        f"pan={round(pan, 2)}, tilt={round(tilt, 2)}，发起抓包"
                    )

                    # 向 capture_worker 发送抓包命令；有时长则限时抓包，否则持续抓包直到手动停止。
                    _capture_cmd = {
                        'action':        'save_pcap' if _cab_limit else 'start_capture',
                        'channel':       int(_cab_channel),
                        'bandwidth':     str(_cab_bw),
                        'target_mac':    str(_cab_mac),
                        'target_mac_match': 'any_addr',
                        'ignore_location_stop': True,
                    }
                    if _cab_pcap:
                        _capture_cmd['pcap_filename'] = str(_cab_pcap)
                    if _cab_limit:
                        _capture_cmd['capture_time_limit'] = float(_cab_limit)
                    if _cab_min_mem is not None:
                        _capture_cmd['min_free_memory_mb'] = float(_cab_min_mem)
                    if _cab_min_disk is not None:
                        _capture_cmd['min_free_disk_mb'] = float(_cab_min_disk)
                    r.lpush('capture:command_queue',
                            json.dumps(_capture_cmd, ensure_ascii=False))

                    # 更新 PTZ 状态为 IDLE（云台已停在指定点位，抓包由 capture_worker 执行）
                    _cab_status = {
                        'ts':       time.time(),
                        'position': {'pan': round(pan, 2), 'tilt': round(tilt, 2)},
                        'state':    'IDLE',
                        'capture_at_best': {
                            'active':     True,
                            'phase':      'capturing',
                            'mac':        _cab_mac,
                            'channel':    int(_cab_channel),
                            'bandwidth':  str(_cab_bw),
                            'target_position': {'pan': float(_cab_pan), 'tilt': float(_cab_tilt)},
                            'target_mac_match': 'any_addr',
                            'capture_time_limit': float(_cab_limit) if _cab_limit else None,
                            'min_free_memory_mb': float(_cab_min_mem) if _cab_min_mem is not None else None,
                            'min_free_disk_mb': float(_cab_min_disk) if _cab_min_disk is not None else None,
                            'pcap':       _cab_pcap,
                        },
                    }
                    if _cab_ab:
                        _cab_status['capture_at_best']['rf_target_position'] = {'pan': round(pan, 2), 'tilt': round(tilt, 2)}
                        _cab_status['capture_at_best']['antenna_bias'] = {
                            'pan_bias_deg': _cab_ab['pan_bias_deg'],
                            'tilt_bias_deg': _cab_ab['tilt_bias_deg'],
                        }
                    r.set(PTZ_STATUS_KEY, json.dumps(_cab_status))
                    continue

                # 在智能扫描命令处理函数开始处添加全局变量声明
                if action == 'start_intelligent_scan':

                    try:
                        
                        logger.info(f"🔍 开始处理智能扫描命令: {command}")
                        
                        if intelligent_scan_active:
                            logger.warning("智能扫描已在运行，请先停止。")
                            continue

                        # 获取扫描范围参数
                        pan_range = command.get('pan_range')
                        tilt_range = command.get('tilt_range')
                        # 🔥 添加这行：获取全天候模式参数
                        retry_on_no_signal = command.get('retry_on_no_signal', INTELLIGENT_SCAN_CONFIG['retry_on_no_signal'])
                        
                        logger.info(f"📋 获取参数: pan_range={pan_range}, tilt_range={tilt_range}, retry_on_no_signal={retry_on_no_signal}")
                        
                        # 参数验证
                        if not pan_range or not isinstance(pan_range, list) or len(pan_range) != 2:
                            logger.error(f"❌ pan_range参数无效: {pan_range}")
                            continue
                            
                        if not tilt_range or not isinstance(tilt_range, list) or len(tilt_range) != 2:
                            logger.error(f"❌ tilt_range参数无效: {tilt_range}")
                            continue
                        
                        # 验证范围值
                        try:
                            pan_min, pan_max = float(pan_range[0]), float(pan_range[1])
                            tilt_min, tilt_max = float(tilt_range[0]), float(tilt_range[1])
                            logger.info(f"✅ 参数验证通过: Pan={pan_min}-{pan_max}, Tilt={tilt_min}-{tilt_max}")
                            
                            if pan_min >= pan_max or tilt_min >= tilt_max:
                                logger.error("❌ 扫描范围格式错误")
                                continue
                                
                        except (ValueError, TypeError) as e:
                            logger.error(f"❌ 范围参数类型错误: {e}")
                            continue

                        logger.info("🔄 设置智能扫描状态...")
                        intelligent_scan_active = True
                        intelligent_scan_round = 1
                        
                        # 清理旧数据，设置激活标志
                        logger.info("🧹 清理旧数据...")
                        r.delete(INTELLIGENT_SCAN_SIGNALS_KEY)
                        r.set(INTELLIGENT_SCAN_ACTIVE_KEY, '1')
                        
                        # 设置状态信息
                        logger.info("📊 设置状态信息...")
                        logger.info(f"🔍 配置检查: INTELLIGENT_SCAN_CONFIG = {INTELLIGENT_SCAN_CONFIG}")
                        logger.info(f"🔍 max_iterations = {INTELLIGENT_SCAN_CONFIG.get('max_iterations', 'NOT_FOUND')}")
                        status = { 
                            "active": True, 
                            "current_round": 1, 
                            "max_rounds": INTELLIGENT_SCAN_CONFIG['max_iterations'], 
                            "range": {"pan": pan_range, "tilt": tilt_range}, 
                            "sub_state": "scanning",
                            "retry_on_no_signal": retry_on_no_signal, # 🔥 添加全天候模式参数，
                            "original_range": {"pan": pan_range, "tilt": tilt_range} # 🔥 添加原始范围参数
                        }
                        r.set(INTELLIGENT_SCAN_STATUS_KEY, json.dumps(status))
                        # 🔥 同步更新ptz:current_status中的轮次信息
                        try:
                            current_status_raw = r.get(PTZ_STATUS_KEY)
                            if current_status_raw:
                                current_status = json.loads(current_status_raw)
                                if current_status.get("auto_scan", {}).get("active"):
                                    current_status["auto_scan"]["current_round"] = 1  # 启动时是第1轮
                                    r.set(PTZ_STATUS_KEY, json.dumps(current_status))
                                    logger.info("✅ 更新ptz:current_status轮次: 1")
                        except Exception as e:
                            logger.warning(f"更新ptz:current_status轮次失败: {e}")
                        
                        # 启动第一轮扫描
                        logger.info(f"🚀 准备启动第一轮扫描...")
                        if _goto_calibration_point(ptz, r, context="智能扫描第1轮开始前"):
                            # 🔥 立即设置PREPARING，防止sleep期间抓包污染数据
                            r.set('capture:scan_status', 'PREPARING')
                            logger.info("🔒 去限位点后立即设置PREPARING，防止间隙期抓包")
                            time.sleep(1.0)
                        
                        _start_scan_round(r, pan_range, tilt_range, 1)
                        logger.info(f"✅ 智能扫描启动成功!")
                        continue
                        
                    except Exception as e:
                        logger.error(f"❌ 智能扫描启动失败: {e}", extra={
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "command": command,
                            "traceback": str(e.__traceback__)
                        })
                        # 重置状态
                        intelligent_scan_active = False
                        r.set(INTELLIGENT_SCAN_ACTIVE_KEY, '0')
                        r.delete(INTELLIGENT_SCAN_STATUS_KEY)
                        continue
                # --- 【最终版】处理停止智能扫描的命令 ---
                if action == 'stop_intelligent_scan':
                     # 🔥 添加全局变量声明
                    
                    if intelligent_scan_active:
                        logger.info("🛑 收到命令，停止智能扫描。")
                        intelligent_scan_active = False
                        # 停止当前扫描，清理状态
                        r.lpush(PTZ_COMMAND_QUEUE, json.dumps({"action": "stop_auto_scan"}))
                        r.set(INTELLIGENT_SCAN_ACTIVE_KEY, '0')
                        r.delete(INTELLIGENT_SCAN_STATUS_KEY)
                        r.delete(INTELLIGENT_SCAN_SIGNALS_KEY)
                    continue
                # ==================== 智能扫描命令处理 ====================
                
                if state in ("MOVING", "AUTO_SCANNING") and action not in ['up', 'down', 'left', 'right', 'move_absolute']:
                    logger.warning(f"当前状态为 {state}，忽略非中断性命令: {action}")
                    pan, tilt = ptz.get_position()
                    pan_rounded = round_angle_to_2dp(pan)
                    tilt_rounded = round_angle_to_2dp(tilt)
                    status = {"ts": time.time(), "position": {"pan": pan_rounded, "tilt": tilt_rounded}, "state": state}
                    if auto_scan_active:
                        # 自动扫描模式下，对外状态始终显示为AUTO_SCANNING
                        status["state"] = "AUTO_SCANNING"  # 强制覆盖内部状态·
                        status.update({
                            "auto_scan": {
                                "active": True,
                                "range": {"pan": scan_pan_range, "tilt": scan_tilt_range},
                                "step_delay": step_delay,
                                "pan_step_size": pan_step_size,
                                "tilt_step_size": tilt_step_size,
                                "sub_state": scan_sub_state  # 新增：确保主循环也更新子状态
                            }
                        })
                    else:
                        status["auto_scan"] = {"active": False}
                    # 🔥 新增：智能扫描轮次补充
                    if intelligent_scan_active:
                        status["auto_scan"]["current_round"] = intelligent_scan_round
                        status["auto_scan"]["max_rounds"] = INTELLIGENT_SCAN_CONFIG['max_iterations']  # 🔥 新增最大轮次
                    r.set(PTZ_STATUS_KEY, json.dumps(status))
                    time.sleep(0.1)
                    continue

                if action == "move_absolute":
                    new_target_pan = command.get("pan")
                    new_target_tilt = command.get("tilt")
                    # 新增：获取步径参数
                    pan_step_size = command.get('pan_step_size')
                    tilt_step_size = command.get('tilt_step_size')
                    # 2. 参数验证
                    if new_target_pan is None or new_target_tilt is None or pan_step_size is None or tilt_step_size is None:
                        logger.warning("绝对移动命令缺少必要参数 pan 或 tilt 或 steph 或 stepv")
                        continue
                    
                    # 🔥 核心步骤：将获取到的步径更新到 Redis 🔥
                    try:
                        current_config = {
                            'source_steph': float(pan_step_size),
                            'source_stepv': float(tilt_step_size),
                            'updated_by': 'manual_move',
                            'ts': time.time()
                        }
                        r.set('gimbal:current_config', json.dumps(current_config))
                        logger.info(f"✅ 系统步径已更新为手动移动步径: ({pan_step_size}, {tilt_step_size})")
                    except Exception as e:
                        logger.error(f"❌ 更新系统步径到 Redis 失败: {e}")
                        continue # 如果更新失败，可以选择不执行移动，保证状态一致性


                    if auto_scan_active or state == "AUTO_SCANNING":
                        logger.warning(f"正在自动扫描，根据优先级规则，已忽略绝对移动命令 (Pan={command.get('pan')}, Tilt={command.get('tilt')})。")
                        # 使用 continue 跳过此命令，不进行处理
                        continue

                    logger.info(f"收到绝对移动命令至 (Pan={new_target_pan}, Tilt={new_target_tilt})。")
                    if state == "MOVING":
                        ptz.stop()
                        logger.info("新的绝对移动命令将覆盖上一个移动任务。")
                    
               
                    if state == "IDLE":
                        pan, tilt = ptz.get_position()
                        pan_rounded = round_angle_to_2dp(pan)
                        tilt_rounded = round_angle_to_2dp(tilt)
                        patch_ptz_status(r, {
                            "position": {"pan": pan_rounded, "tilt": tilt_rounded},
                            "state": state,
                        })

                if new_target_pan is not None and new_target_tilt is not None:
                    # 严格范围校验：超出直接拒绝（不再钳位）
                    if not (PAN_MIN <= new_target_pan <= PAN_MAX) or not (TILT_MIN <= new_target_tilt <= TILT_MAX):
                        try:
                            logger.warning(f"拒绝：目标超出范围 (允许Pan={PAN_MIN:.2f}~{PAN_MAX:.2f}, Tilt={TILT_MIN:.2f}~{TILT_MAX:.2f})，收到 Pan={float(new_target_pan):.2f}, Tilt={float(new_target_tilt):.2f}")
                        except Exception:
                            logger.warning("拒绝：目标超出范围")
                        # 可选：这里也可以更新一次状态到Redis，标记错误
                        continue
                    # 仅做环绕归一（不做钳位）
                    new_target_pan = new_target_pan % 360.0
                    if new_target_pan < 0:
                        new_target_pan += 360.0
                    if safe_move_to_pan_tilt(ptz, new_target_pan, new_target_tilt):
                        target_position = {"pan": new_target_pan, "tilt": new_target_tilt}
                        state = "MOVING"
                        homing_active = False
                        # 同步到设备内存
                        device_state.update_state("MOVING")
                        logger.info(f"设备内存状态更新: 状态={device_state.get_state()}")
                        logger.info(f"云台开始移动至目标 (Pan={new_target_pan:.2f}, Tilt={new_target_tilt:.2f})，切换至 MOVING 状态。")
                        continue
                    else:
                        logger.warning(f"云台移动失败：无法移动到目标位置 (Pan={new_target_pan:.2f}, Tilt={new_target_tilt:.2f})")
                        continue
                else:
                    logger.warning("移动指令解析失败或缺少必要参数，未发送移动指令。")

            # 状态处理
            if state == "IDLE":
                # 周期性上报当前位置，避免 position 为 null
                now = time.time()
                if (last_idle_status_ts is None) or (now - last_idle_status_ts >= IDLE_STATUS_INTERVAL):
                    pan, tilt = ptz.get_position()
                    pan_rounded = round_angle_to_2dp(pan)
                    tilt_rounded = round_angle_to_2dp(tilt)
                    status = {"ts": now, "position": {"pan": pan_rounded, "tilt": tilt_rounded}, "state": state}
                    # 只在位置真正变化时才更新内存
                    current_pos = device_state.get_position()
                    if (abs(current_pos['pan'] - pan_rounded) > 0.01 or 
                        abs(current_pos['tilt'] - tilt_rounded) > 0.01):
                        device_state.update_position(pan_rounded, tilt_rounded)
                        device_state.update_state("IDLE")
                        logger.info(f"设备内存状态更新: 位置=({pan_rounded}, {tilt_rounded}), 状态={device_state.get_state()}")
                    
                    try:
                        # 使用 patch_ptz_status 深合并，仅更新 ts/position/state，
                        # 不覆盖已有的 full_scan / location_scan / capture 等子对象终态。
                        patch_ptz_status(r, {
                            "position": {"pan": pan_rounded, "tilt": tilt_rounded},
                            "state": state,
                        })
                    except Exception:
                        pass
                    last_idle_status_ts = now
                                
                    # 高精度自动修复机制：检查是否需要微调
                    if target_position["pan"] is not None and target_position["tilt"] is not None:
                        pan_error = abs(_shortest_angular_diff_deg(pan, target_position["pan"]))
                        tilt_error = abs(tilt - target_position["tilt"])
                        
                        # 如果误差超过修复阈值，执行微调
                        if pan_error > PTZ_AUTO_REPAIR_THRESHOLD or tilt_error > PTZ_AUTO_REPAIR_THRESHOLD:
                            logger.info(f"检测到位置误差，执行高精度修复: Pan误差={pan_error:.3f}°, Tilt误差={tilt_error:.3f}°", extra={
                                "pan_error": pan_error,
                                "tilt_error": tilt_error,
                                "target_pan": target_position["pan"],
                                "target_tilt": target_position["tilt"],
                                "current_pan": pan,
                                "current_tilt": tilt
                            })
                            
                            # 执行微调移动
                            if high_precision_move(ptz, target_position["pan"], target_position["tilt"]):
                                state = "MOVING"
                                logger.info("高精度修复移动已启动")
                                continue
                            else:
                                logger.warning("高精度修复移动失败")
                    last_idle_status_ts = now
            elif state in ("MOVING", "AUTO_SCANNING"):
                pan, tilt = ptz.get_position()
                pan_rounded = round_angle_to_2dp(pan)
                tilt_rounded = round_angle_to_2dp(tilt)
                if pan_rounded is None or tilt_rounded is None:
                    logger.warning("移动监控中，无法获取云台位置，稍后重试。")
                    time.sleep(0.5)
                    continue

                now = time.time()
                target_pan = target_position.get("pan")
                target_tilt = target_position.get("tilt")
                if target_pan is None or target_tilt is None:
                    logger.warning("移动监控中，目标位置为空，切回IDLE。")
                    state = "IDLE"
                    continue

                pan_ok = _is_pan_close(pan, target_pan, TOLERANCE)
                tilt_ok = abs(tilt - target_tilt) <= TOLERANCE

                # 记录进展并处理无进展重发；已在到位容差内时不再重发。
                if last_progress_pos is None:
                    last_progress_pos = (pan, tilt)
                    last_progress_ts = now
                elif not (pan_ok and tilt_ok):
                    if abs(_shortest_angular_diff_deg(last_progress_pos[0], pan)) >= NO_PROGRESS_DEG or abs(tilt - last_progress_pos[1]) >= NO_PROGRESS_DEG:
                        last_progress_pos = (pan, tilt)
                        last_progress_ts = now
                    elif (now - (last_progress_ts or now)) >= RESEND_INTERVAL:
                        # 无进展，重发当前目标
                        if safe_move_to_pan_tilt(ptz, target_pan, target_tilt):
                            logger.info(f"无进展{RESEND_INTERVAL:.1f}s，重发目标 Pan={target_pan:.2f}, Tilt={target_tilt:.2f}")
                        else:
                            logger.warning("无进展重发失败: 无法移动到目标位置")
                        last_progress_ts = now
                # 同步到设备内存
                device_state.update_position(pan_rounded, tilt_rounded)
                device_state.update_state(state)
                logger.info(f"设备内存状态更新: 位置=({pan_rounded}, {tilt_rounded}), 状态={device_state.get_state()}")
                status = {"ts": now, "position": {"pan": pan_rounded, "tilt": tilt_rounded}, "state": state}
                if auto_scan_active:
                    # 自动扫描模式下，对外状态始终显示为AUTO_SCANNING
                    status["state"] = "AUTO_SCANNING"  # 强制覆盖内部状态
                    status.update({
                        "auto_scan": {
                            "active": True,
                            "range": {"pan": scan_pan_range, "tilt": scan_tilt_range},
                            "step_delay": step_delay,
                            "pan_step_size": pan_step_size,
                            "tilt_step_size": tilt_step_size
                        }
                    })
                else:
                    status["auto_scan"] = {"active": False}
                # 🔥 新增：智能扫描轮次补充
                if intelligent_scan_active:
                    status["auto_scan"]["current_round"] = intelligent_scan_round
                    status["auto_scan"]["max_rounds"] = INTELLIGENT_SCAN_CONFIG['max_iterations']  # 🔥 新增最大轮次
                r.set(PTZ_STATUS_KEY, json.dumps(status))

                logger.info(f"移动监控中... 当前位置: Pan={pan:.2f}, Tilt={tilt:.2f} (目标: Pan={target_pan:.2f}, Tilt={target_tilt:.2f}, 容差: ±{TOLERANCE:.2f}°)")

                if pan_ok and tilt_ok:
                    logger.info("已到达目标位置。")
                    # 更新设备状态为IDLE
                    device_state.update_state("IDLE")
                    # 需要添加位置更新：
                    device_state.update_position(pan_rounded, tilt_rounded)
                    logger.info(f"设备内存状态更新: 位置=({pan_rounded}, {tilt_rounded}), 状态={device_state.get_state()}")

                    ptz.stop()
                    last_progress_pos = None
                    last_progress_ts = None
                    if auto_scan_active:
                        # 首次到达扫描起点后，强制稳定等待一次
                        if current_scan_point_idx == 0 and not first_point_settled:
                            logger.info(f"首次到达扫描起点，稳定等待 {PTZ_INITIAL_SETTLE_SEC:.1f}s ...")
                            r.set('capture:scan_status', 'SCANNING')
                            logger.info("🔓 设置capture状态为SCANNING，恢复数据包处理")
                            time.sleep(PTZ_INITIAL_SETTLE_SEC)
                            first_point_settled = True
                            continue

                        next_idx = current_scan_point_idx + scan_direction
                        if 0 <= next_idx < len(scan_path_points):
                            current_scan_point_idx = next_idx
                            time.sleep(scan_step_delay)  # 使用新的步长延迟
                            next_pan, next_tilt = scan_path_points[current_scan_point_idx]
                            if safe_split_move(ptz, next_pan, next_tilt, order='pan_first', settle=PTZ_SPLIT_SETTLE_SEC):
                                target_position = {"pan": next_pan, "tilt": next_tilt}
                                ptz._last_move_time = time.time()
                                logger.info(f"自动扫描: 移动到第 {current_scan_point_idx + 1}/{len(scan_path_points)} 个点: Pan={next_pan:.2f}, Tilt={next_tilt:.2f}")
                                continue
                            else:
                                logger.warning(f"自动扫描: 移动到第 {current_scan_point_idx + 1} 个点失败，尝试下一个点")
                                continue
                        else:
                            # 🔥 智能扫描once模式：直接完成，不循环
                            if is_once_mode:
                                logger.info("🤖 智能扫描轮次完成！(once模式)")
                                auto_scan_active = False
                                state = "IDLE"
                                scan_path_points = []
                                current_scan_point_idx = 0
                            elif PTZ_AUTO_SCAN_LOOP_MODE == 'restart':
                                current_scan_point_idx = 0
                                scan_direction = 1
                                time.sleep(scan_step_delay)  # 使用新的步长延迟
                                next_pan, next_tilt = scan_path_points[current_scan_point_idx]
                                if safe_split_move(ptz, next_pan, next_tilt, order='pan_first', settle=PTZ_SPLIT_SETTLE_SEC):
                                    target_position = {"pan": next_pan, "tilt": next_tilt}
                                    ptz._last_move_time = time.time()
                                    logger.info(f"自动扫描: 重启到第 1/{len(scan_path_points)} 个点")
                                    continue
                                else:
                                    logger.warning(f"自动扫描: 重启到第 1 个点失败")
                                    continue
                            elif PTZ_AUTO_SCAN_LOOP_MODE == 'bounce':
                                if next_idx >= len(scan_path_points):
                                    scan_direction = -1
                                    current_scan_point_idx = len(scan_path_points) - 2 if len(scan_path_points) > 1 else 0
                                else:
                                            # 🔥 反向到头（回到起点），准备正向 → 此时校准
                                    if _goto_calibration_point(ptz, r, context="自动扫描-bounce模式反向到头"):
                                        time.sleep(1.0)
                                    else:
                                        logger.warning("⚠️ [自动扫描] 校准失败，但继续扫描")
                                        
                                    scan_direction = 1
                                    current_scan_point_idx = 1 if len(scan_path_points) > 1 else 0
                                time.sleep(scan_step_delay)  # 使用新的步长延迟
                                next_pan, next_tilt = scan_path_points[current_scan_point_idx]
                                if safe_split_move(ptz, next_pan, next_tilt, order='pan_first', settle=PTZ_SPLIT_SETTLE_SEC):
                                    target_position = {"pan": next_pan, "tilt": next_tilt}
                                    ptz._last_move_time = time.time()
                                    logger.info(f"自动扫描: 反向到第 {current_scan_point_idx + 1}/{len(scan_path_points)} 个点")
                                    continue
                                else:
                                    logger.warning(f"自动扫描: 反向到第 {current_scan_point_idx + 1} 个点失败")
                                    continue
                            else:
                                logger.info("自动扫描完成！")
                                auto_scan_active = False
                                state = "IDLE"
                                scan_path_points = []
                                current_scan_point_idx = 0
                                
                    else:
                        state = "IDLE"
                        target_position = {"pan": None, "tilt": None}
                        logger.info("状态切换: MOVING -> IDLE")
                    
                    # 到达后更新一次最终状态\
                    pan_rounded = round_angle_to_2dp(pan)
                    tilt_rounded = round_angle_to_2dp(tilt)
                    status = {"ts": time.time(), "position": {"pan": pan_rounded, "tilt": tilt_rounded}, "state": state}
                    if auto_scan_active:
                        # 自动扫描模式下，对外状态始终显示为AUTO_SCANNING
                        status["state"] = "AUTO_SCANNING"  # 强制覆盖内部状态
                        status.update({
                            "auto_scan": {
                                "active": True,
                                "range": {"pan": scan_pan_range, "tilt": scan_tilt_range},
                                "step_delay": step_delay,
                                "pan_step_size": pan_step_size,
                                "tilt_step_size": tilt_step_size
                            }
                        })
                    else:
                        status["auto_scan"] = {"active": False}
                    # 🔥 新增：智能扫描轮次补充
                    if intelligent_scan_active:
                        status["auto_scan"]["current_round"] = intelligent_scan_round
                        status["auto_scan"]["max_rounds"] = INTELLIGENT_SCAN_CONFIG['max_iterations']  # 🔥 新增最大轮次
                    r.set(PTZ_STATUS_KEY, json.dumps(status))

                # 超时保护（扫描）
                if auto_scan_active:
                    current_time = time.time()
                    if not hasattr(ptz, '_last_move_time'):
                        ptz._last_move_time = current_time
                    if current_time - ptz._last_move_time > 30.0:
                        logger.warning("警告：在当前位置停留超过30秒，尝试继续扫描...")
                        next_idx = current_scan_point_idx + scan_direction
                        if 0 <= next_idx < len(scan_path_points):
                            current_scan_point_idx = next_idx
                        else:
                            if PTZ_AUTO_SCAN_LOOP_MODE == 'restart':
                                current_scan_point_idx = 0
                                scan_direction = 1
                            elif PTZ_AUTO_SCAN_LOOP_MODE == 'bounce':
                                if next_idx >= len(scan_path_points):
                                    scan_direction = -1
                                    current_scan_point_idx = len(scan_path_points) - 2 if len(scan_path_points) > 1 else 0
                                else:
                                    scan_direction = 1
                                    current_scan_point_idx = 1 if len(scan_path_points) > 1 else 0
                            else:
                                logger.info("超时恢复：扫描完成")
                                auto_scan_active = False
                                state = "IDLE"
                                scan_path_points = []
                                current_scan_point_idx = 0
                                time.sleep(0.1)
                                continue
                        next_pan, next_tilt = scan_path_points[current_scan_point_idx]
                        if safe_split_move(ptz, next_pan, next_tilt, order='pan_first', settle=PTZ_SPLIT_SETTLE_SEC):
                            target_position = {"pan": next_pan, "tilt": next_tilt}
                            ptz._last_move_time = current_time
                            logger.info(f"超时恢复：移动到第 {current_scan_point_idx + 1}/{len(scan_path_points)} 个点")
                        else:
                            logger.warning(f"超时恢复：移动到第 {current_scan_point_idx + 1} 个点失败")

                # 在第1263行之前添加：
                # --- 【正确版】智能扫描主逻辑 ---
                if intelligent_scan_active and _is_scan_round_finished(r):
                    # 🔥 添加全局变量声明
                    
                    logger.info(f"🤖 智能扫描: 第 {intelligent_scan_round} 轮扫描完成，开始分析数据...")
                    
                    # 1. 【正确调用】收集并过滤信号数据（原子操作）
                    good_signals = _collect_and_filter_signals(r)
                    
                    # 2. 检查是否满足继续迭代的条件
                    if good_signals and intelligent_scan_round < INTELLIGENT_SCAN_CONFIG['max_iterations']:
                        # 3. 【正确调用】根据好信号计算新的扫描范围
                        next_ranges = _calculate_next_scan_range(good_signals, scan_pan_range, scan_tilt_range)
                        
                        if next_ranges:
                            # --- 进入下一轮 ---
                            intelligent_scan_round += 1
                            r.delete(INTELLIGENT_SCAN_SIGNALS_KEY)  # 清空信号队列，为下一轮做准备
                            
                            # 更新状态
                            status = json.loads(r.get(INTELLIGENT_SCAN_STATUS_KEY) or '{}')
                            status.update({ 
                                "current_round": intelligent_scan_round, 
                                "range": {"pan": next_ranges[0], "tilt": next_ranges[1]} 
                            })
                            r.set(INTELLIGENT_SCAN_STATUS_KEY, json.dumps(status))
                            # 🔥 同步更新ptz:current_status中的轮次信息
                            try:
                                current_status_raw = r.get(PTZ_STATUS_KEY)
                                if current_status_raw:
                                    current_status = json.loads(current_status_raw)
                                    if current_status.get("auto_scan", {}).get("active"):
                                        current_status["auto_scan"]["current_round"] = intelligent_scan_round
                                        # 应该改为（正确）
                                        current_status["auto_scan"]["max_rounds"] = INTELLIGENT_SCAN_CONFIG['max_iterations']
                                        r.set(PTZ_STATUS_KEY, json.dumps(current_status))
                                        logger.info(f"✅ 更新ptz:current_status轮次: {intelligent_scan_round}")
                            except Exception as e:
                                logger.warning(f"更新ptz:current_status轮次失败: {e}")
                            
                            # 【正确调用】发起新一轮扫描
                            time.sleep(2.0)  # 短暂等待，确保状态同步
                            _start_scan_round(r, next_ranges[0], next_ranges[1], intelligent_scan_round, ptz)
                        else:
                            # --- 结束流程 (计算范围失败) ---
                            logger.warning("无法计算出下一轮扫描范围，智能扫描提前结束。")
                            _move_to_best_point(r, good_signals)
                            intelligent_scan_active = False
                            r.set(INTELLIGENT_SCAN_ACTIVE_KEY, '0')
                            r.delete(INTELLIGENT_SCAN_STATUS_KEY)
                    else:
                        # --- 结束流程 (信号不足或达到最大轮次) ---
                        if not good_signals:
                            # 🔥 获取全天候模式配置
                            try:
                                status = json.loads(r.get(INTELLIGENT_SCAN_STATUS_KEY) or '{}')
                                retry_enabled = status.get('retry_on_no_signal', False)
                                original_range = status.get('original_range', {})
                                original_pan_range = original_range.get('pan', scan_pan_range)
                                original_tilt_range = original_range.get('tilt', scan_tilt_range)
                            except:
                                retry_enabled = False
                                original_pan_range = scan_pan_range
                                original_tilt_range = scan_tilt_range
                            if retry_enabled:
                                # 🔄 全天候模式：重复第一轮大范围扫描
                                logger.warning("🔄 信号不足，全天候模式重复第一轮扫描...")
                                
                                # 重置状态
                                r.delete(INTELLIGENT_SCAN_SIGNALS_KEY)  # 清空信号队列
                                intelligent_scan_round = 1              # 重置轮次
                                scan_pan_range = original_pan_range     # 重置扫描范围
                                scan_tilt_range = original_tilt_range
                                
                                # 延迟后重新扫描
                                retry_delay = INTELLIGENT_SCAN_CONFIG.get('retry_delay', 5.0)
                                time.sleep(retry_delay)
                                
                                _start_scan_round(r, original_pan_range, original_tilt_range, 1)
                                continue  # 🔥 跳过结束流程，继续智能扫描
                            else:
                                logger.warning("信号不足，智能扫描提前结束。")
                        else:
                            logger.info("智能扫描达到最大轮次，流程结束。")

                        # 【正确调用】移动到最佳信号点
                        _move_to_best_point(r, good_signals)
                        intelligent_scan_active = False
                        r.set(INTELLIGENT_SCAN_ACTIVE_KEY, '0')
                        r.delete(INTELLIGENT_SCAN_STATUS_KEY)


                time.sleep(0.1)

        except Exception as e:
            if timing_trace is not None:
                timing_trace.close()
            import traceback
            logger.error(f"运行时发生未知错误 [{type(e).__name__}]: {e}\n{traceback.format_exc()}")
            ptz.stop()
            state = "IDLE"
            target_position = {"pan": None, "tilt": None}
            try:
                pan, tilt = ptz.get_position()
                pan_rounded = round_angle_to_2dp(pan)
                tilt_rounded = round_angle_to_2dp(tilt)
                status = {"ts": time.time(), "position": {"pan": pan_rounded, "tilt": tilt_rounded}, "state": state, "error": str(e)}
                r.set(PTZ_STATUS_KEY, json.dumps(status))
            except Exception as inner_e:
                logger.error("尝试更新错误状态到Redis失败", extra={
                    "error": str(inner_e),
                    "error_type": type(inner_e).__name__
                })
            time.sleep(2)


# ==================== Todo 21-A: 像素路径生成与像素转角度工具函数 ====================

def _build_pixel_coarse_path(outer_range, img_w, img_h):
    """
    外层像素范围粗扫格心路径（Todo 21-A）。

    参数:
        outer_range: {"x_range": [x0, x1], "y_range": [y0, y1]}（已归一化整数像素坐标）
        img_w, img_h: 图像实际尺寸

    返回: list of {"px": int, "py": int, "phase": "coarse", "point_type": ...}
        point_type 为 "outer_probe"（外扩 8 点）或 "grid"（格心）
    """
    x0, x1 = outer_range["x_range"]
    y0, y1 = outer_range["y_range"]
    W = x1 - x0
    H = y1 - y0
    if W <= 0 or H <= 0:
        return []

    # 计算行列数，目标约 20 个格心
    cols = max(3, round(math.sqrt(20 * W / H)))
    rows = max(3, round(20 / cols))
    # 调整使总数在 [12, 24]
    while cols * rows > 24:
        if cols >= rows:
            cols -= 1
        else:
            rows -= 1
    while cols * rows < 12:
        cols += 1
    cols = max(3, cols)
    rows = max(3, rows)

    # 格心
    grid_pts = []
    seen = set()
    for i in range(cols):
        for j in range(rows):
            px = int(x0 + (i + 0.5) * W / cols)
            py = int(y0 + (j + 0.5) * H / rows)
            key = (px, py)
            if key not in seen:
                seen.add(key)
                grid_pts.append({"px": px, "py": py, "phase": "coarse", "point_type": "grid"})

    # 外扩 8 点。像素范围上界是开区间，因此可用像素最大值是 width/height - 1。
    # 当理想外扩超出图像时，统一缩小本次外扩比例，避免逐点裁剪扭曲矩形。
    dx = W / cols / 2
    dy = H / rows / 2
    max_x = max(0, int(img_w) - 1)
    max_y = max(0, int(img_h) - 1)
    base_x1 = min(max_x, x1 - 1)
    base_y1 = min(max_y, y1 - 1)
    scale_candidates = [1.0]
    if dx > 0:
        scale_candidates.extend([
            max(0.0, float(x0) / dx),
            max(0.0, float(max_x - base_x1) / dx),
        ])
    if dy > 0:
        scale_candidates.extend([
            max(0.0, float(y0) / dy),
            max(0.0, float(max_y - base_y1) / dy),
        ])
    expand_scale = min(scale_candidates)
    ex0 = x0 - dx * expand_scale
    ex1 = base_x1 + dx * expand_scale
    ey0 = y0 - dy * expand_scale
    ey1 = base_y1 + dy * expand_scale
    outer_pts = []
    corners = [(ex0, ey0), (ex1, ey0), (ex0, ey1), (ex1, ey1)]
    mid_x = (ex0 + ex1) / 2
    mid_y = (ey0 + ey1) / 2
    edges = [(mid_x, ey0), (mid_x, ey1), (ex0, mid_y), (ex1, mid_y)]
    for bx, by in corners + edges:
        bx_clip = max(0, min(max_x, int(round(bx))))
        by_clip = max(0, min(max_y, int(round(by))))
        outer_pts.append({"px": bx_clip, "py": by_clip, "phase": "coarse", "point_type": "outer_probe"})

    return outer_pts + grid_pts


def _build_pixel_fine_path(target_range, img_w, img_h):
    """
    目标区细扫格心路径（Todo 21-A）。

    参数:
        target_range: {"x_range": [x0, x1], "y_range": [y0, y1]}
        img_w, img_h: 图像实际尺寸

    返回: list of {"px": int, "py": int, "phase": "fine", "point_type": "grid"}
    """
    x0, x1 = target_range["x_range"]
    y0, y1 = target_range["y_range"]
    W = x1 - x0
    H = y1 - y0
    if W <= 0 or H <= 0:
        return []

    cols = max(3, round(math.sqrt(23 * W / H)))
    rows = max(3, round(23 / cols))
    # 调整使总数在 [20, 26]
    while cols * rows > 26:
        if cols >= rows:
            cols -= 1
        else:
            rows -= 1
    while cols * rows < 20:
        cols += 1
    cols = max(3, cols)
    rows = max(3, rows)

    seen = set()
    pts = []
    for i in range(cols):
        for j in range(rows):
            px = int(x0 + (i + 0.5) * W / cols)
            py = int(y0 + (j + 0.5) * H / rows)
            key = (px, py)
            if key not in seen:
                seen.add(key)
                pts.append({"px": px, "py": py, "phase": "fine", "point_type": "grid"})
    return pts


def _build_pixel_deviation_specs(target_range, outer_range):
    """
    偏差层像素点规格（Todo 21-A）。

    参数:
        target_range: {"x_range": [tx0, tx1], "y_range": [ty0, ty1]}
        outer_range: {"x_range": [ox0, ox1], "y_range": [oy0, oy1]}

    返回: list of {"px": int, "py": int, "layer": int, "side": str, "phase": "deviation_a"}
    """
    tx0, tx1_exclusive = target_range["x_range"]
    ty0, ty1_exclusive = target_range["y_range"]
    ox0, ox1_exclusive = outer_range["x_range"]
    oy0, oy1_exclusive = outer_range["y_range"]
    tx1 = tx1_exclusive - 1
    ty1 = ty1_exclusive - 1
    ox1 = ox1_exclusive - 1
    oy1 = oy1_exclusive - 1

    # 四边可扩展距离
    left = max(0, tx0 - ox0)
    right = max(0, ox1 - tx1)
    top = max(0, ty0 - oy0)
    bottom = max(0, oy1 - ty1)

    # 窄边判定：可扩展距离 <= 2% 图像宽/高
    # 使用 outer_range 的宽高作为图像尺寸近似
    img_w = ox1_exclusive - ox0
    img_h = oy1_exclusive - oy0
    narrow_threshold_x = img_w * 0.02
    narrow_threshold_y = img_h * 0.02
    narrow_sides = set()
    if 0 < left <= narrow_threshold_x:
        narrow_sides.add("left")
    if 0 < right <= narrow_threshold_x:
        narrow_sides.add("right")
    if 0 < top <= narrow_threshold_y:
        narrow_sides.add("top")
    if 0 < bottom <= narrow_threshold_y:
        narrow_sides.add("bottom")

    side_distances = {"left": left, "right": right, "top": top, "bottom": bottom}
    sides = ["left", "right", "top", "bottom"]

    specs = []
    for layer in range(1, 4):  # layer 1, 2, 3
        fraction = layer / 4.0
        # 计算该层的矩形范围
        layer_x0 = tx0 - left * fraction if "left" not in narrow_sides else tx0
        layer_x1 = tx1 + right * fraction if "right" not in narrow_sides else tx1
        layer_y0 = ty0 - top * fraction if "top" not in narrow_sides else ty0
        layer_y1 = ty1 + bottom * fraction if "bottom" not in narrow_sides else ty1

        # 加权分配配置点数到四边
        # 窄边每边 1 点，其余按可扩展距离加权
        points_per_layer = max(1, int(FULL_SCAN_DEVIATION_POINTS_PER_LAYER))
        counts = {s: 0 for s in sides}

        # 窄边先各分配 1 点
        for s in narrow_sides:
            counts[s] = 1

        remaining = points_per_layer - sum(counts.values())
        regular_sides = [s for s in sides if s not in narrow_sides and side_distances[s] > 0]
        if regular_sides and remaining > 0:
            total_weight = sum(side_distances[s] for s in regular_sides)
            if total_weight > 0:
                for s in regular_sides:
                    counts[s] = max(1, round(remaining * side_distances[s] / total_weight))
                # 调整到恰好 remaining
                diff = remaining - sum(counts[s] for s in regular_sides)
                if diff != 0:
                    sorted_sides = sorted(regular_sides, key=lambda s: side_distances[s], reverse=True)
                    idx = 0
                    while diff != 0:
                        s = sorted_sides[idx % len(sorted_sides)]
                        if diff > 0:
                            counts[s] += 1
                            diff -= 1
                        elif counts[s] > 1:
                            counts[s] -= 1
                            diff += 1
                        idx += 1
        elif remaining > 0:
            # 没有可扩展的常规边，均匀分配
            for idx in range(remaining):
                counts[sides[idx % 4]] += 1

        # 在每条边上均匀分布点
        for side in sides:
            count = counts[side]
            if count <= 0:
                continue
            for k in range(count):
                if side in narrow_sides:
                    # 窄边每层轮换采样位置，避免三层重复同一中点。
                    frac = (layer + k) / (count + 3)
                else:
                    frac = (k + 1) / (count + 1)
                if side == "left":
                    px = int(layer_x0)
                    py = int(round(layer_y0 + (layer_y1 - layer_y0) * frac))
                elif side == "right":
                    px = int(layer_x1)
                    py = int(round(layer_y0 + (layer_y1 - layer_y0) * frac))
                elif side == "top":
                    px = int(round(layer_x0 + (layer_x1 - layer_x0) * frac))
                    py = int(layer_y0)
                elif side == "bottom":
                    px = int(round(layer_x0 + (layer_x1 - layer_x0) * frac))
                    py = int(layer_y1)
                else:
                    continue
                specs.append({
                    "px": px, "py": py,
                    "layer": layer, "side": side,
                    "phase": "deviation_a",
                    "narrow_side": side in narrow_sides,
                })

    return specs


def _pixel_conversion_result(px, py, *, success=False, pan=None, tilt=None,
                             reason=None, exception=None):
    result = {
        "success": bool(success),
        "px": px,
        "py": py,
        "pan": pan,
        "tilt": tilt,
        "limits": {
            "pan": [PAN_MIN, PAN_MAX],
            "tilt": [TILT_MIN, TILT_MAX],
        },
        "reason": reason,
    }
    if exception is not None:
        result["exception_type"] = type(exception).__name__
        result["exception_message"] = str(exception)
    return result


def _pixel_conversion_return(result, return_detail):
    if return_detail:
        return result
    if not result.get("success"):
        return None
    return (round(float(result["pan"]), 2), round(float(result["tilt"]), 2))


def _pixel_to_angle_panorama(px, py, pmap_path, session_json, return_detail=False):
    """
    全景 PMAP 像素转云台角度（Todo 21-A）。

    参数:
        px, py: 全景图像中的像素坐标
        pmap_path: .pmap 文件路径
        session_json: session.json 路径或已加载的 dict

    返回: (pan, tilt) 元组，失败时返回 None
    """
    original_px, original_py = px, py
    try:
        from pathlib import Path
        from hugin_panorama_runtime import pixels_to_angles_pmap

        px = float(px)
        py = float(py)
        if not math.isfinite(px) or not math.isfinite(py):
            return _pixel_conversion_return(
                _pixel_conversion_result(
                    original_px, original_py, reason="non_finite_input"
                ),
                return_detail,
            )
        if not pmap_path or not session_json:
            return _pixel_conversion_return(
                _pixel_conversion_result(px, py, reason="no_pmap_source"),
                return_detail,
            )

        results = pixels_to_angles_pmap(
            Path(pmap_path),
            Path(session_json),
            [(px, py)],
        )
        if not results:
            return _pixel_conversion_return(
                _pixel_conversion_result(px, py, reason="no_pmap_source"),
                return_detail,
            )
        result = results[0]
        if not result.get("in_frame"):
            return _pixel_conversion_return(
                _pixel_conversion_result(px, py, reason="pixel_out_of_bounds"),
                return_detail,
            )
        if not result.get("has_source"):
            return _pixel_conversion_return(
                _pixel_conversion_result(px, py, reason="no_pmap_source"),
                return_detail,
            )
        pan = float(result.get("pan"))
        tilt = float(result.get("tilt"))
        if not math.isfinite(pan) or not math.isfinite(tilt):
            reason = "non_finite_angle"
        elif not PAN_MIN <= pan <= PAN_MAX:
            reason = "hardware_pan_limit"
        elif not TILT_MIN <= tilt <= TILT_MAX:
            reason = "hardware_tilt_limit"
        else:
            return _pixel_conversion_return(
                _pixel_conversion_result(
                    px, py, success=True, pan=round(pan, 2), tilt=round(tilt, 2)
                ),
                return_detail,
            )
        return _pixel_conversion_return(
            _pixel_conversion_result(px, py, pan=pan, tilt=tilt, reason=reason),
            return_detail,
        )
    except Exception as exc:
        return _pixel_conversion_return(
            _pixel_conversion_result(
                original_px,
                original_py,
                reason="conversion_exception",
                exception=exc,
            ),
            return_detail,
        )


def _pixel_to_angle_single(px, py, image_context, return_detail=False):
    """
    单图内参像素转云台角度（Todo 21-A）。

    参数:
        px, py: 单张图像中的像素坐标
        image_context: Todo 18 锁定的单图上下文；内参位于 intrinsics 子对象

    返回: (pan, tilt) 元组；裁剪到硬件限位后超出范围返回 None
    """
    original_px, original_py = px, py
    try:
        intrinsics = image_context["intrinsics"]
        fx = float(intrinsics["fx"])
        fy = float(intrinsics["fy"])
        cx = float(intrinsics["cx"])
        cy = float(intrinsics["cy"])
        capture_pan = float(image_context["capture_pan"])
        capture_tilt = float(image_context["capture_tilt"])
        width = int(image_context["width"])
        height = int(image_context["height"])
        px = float(px)
        py = float(py)
        values = (fx, fy, cx, cy, capture_pan, capture_tilt, px, py)
        if not all(math.isfinite(value) for value in values):
            return _pixel_conversion_return(
                _pixel_conversion_result(px, py, reason="non_finite_input"),
                return_detail,
            )
        if fx <= 0 or fy <= 0 or width <= 0 or height <= 0:
            return _pixel_conversion_return(
                _pixel_conversion_result(px, py, reason="invalid_intrinsics"),
                return_detail,
            )
        if not (0 <= px < width and 0 <= py < height):
            return _pixel_conversion_return(
                _pixel_conversion_result(px, py, reason="pixel_out_of_bounds"),
                return_detail,
            )

        # 与 hugin_coordinate_mapper._pixel_to_absolute_ptz_with_intrinsics
        # 使用相同的三维射线旋转数学，避免非零 tilt 下的角度相加近似误差。
        pan_rad = math.radians(capture_pan)
        tilt_rad = math.radians(capture_tilt)
        rx = (px - cx) / fx
        ry = (cy - py) / fy
        rz = 1.0
        norm = math.sqrt(rx * rx + ry * ry + rz * rz)
        rx, ry, rz = rx / norm, ry / norm, rz / norm

        cos_t, sin_t = math.cos(tilt_rad), math.sin(tilt_rad)
        tx = rx
        ty = ry * cos_t + rz * sin_t
        tz = -ry * sin_t + rz * cos_t

        cos_p, sin_p = math.cos(pan_rad), math.sin(pan_rad)
        wx = tx * cos_p + tz * sin_p
        wy = ty
        wz = -tx * sin_p + tz * cos_p

        pan = math.degrees(math.atan2(wx, wz))
        if pan < 0:
            pan += 360.0
        tilt = math.degrees(math.atan2(wy, math.sqrt(wx * wx + wz * wz)))
        if not math.isfinite(pan) or not math.isfinite(tilt):
            reason = "non_finite_angle"
        elif not PAN_MIN <= pan <= PAN_MAX:
            reason = "hardware_pan_limit"
        elif not TILT_MIN <= tilt <= TILT_MAX:
            reason = "hardware_tilt_limit"
        else:
            return _pixel_conversion_return(
                _pixel_conversion_result(
                    px, py, success=True, pan=round(pan, 2), tilt=round(tilt, 2)
                ),
                return_detail,
            )
        return _pixel_conversion_return(
            _pixel_conversion_result(px, py, pan=pan, tilt=tilt, reason=reason),
            return_detail,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return _pixel_conversion_return(
            _pixel_conversion_result(
                original_px,
                original_py,
                reason="invalid_intrinsics",
                exception=exc,
            ),
            return_detail,
        )
    except Exception as exc:
        return _pixel_conversion_return(
            _pixel_conversion_result(
                original_px,
                original_py,
                reason="conversion_exception",
                exception=exc,
            ),
            return_detail,
        )


def _build_full_scan_pixel_execution_plan(mode, image_context, outer_range, target_range):
    """把单目标区像素逻辑点转换为全面扫描现有执行循环可消费的角度点。"""
    if mode not in ("panorama", "single"):
        raise ValueError(f"不支持的全面扫描图像模式: {mode!r}")
    if not isinstance(image_context, dict):
        raise ValueError("全面扫描缺少 image_context")
    width = int(image_context.get("width") or 0)
    height = int(image_context.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("全面扫描 image_context 缺少有效 width/height")

    if mode == "panorama":
        pmap_path = image_context.get("pmap_path")
        session_json = image_context.get("session_json")
        if not pmap_path or not session_json:
            raise ValueError("全景全面扫描缺少 pmap_path/session_json")

        def convert(px, py):
            return _pixel_to_angle_panorama(
                px, py, pmap_path, session_json, return_detail=True
            )
    else:
        intrinsics = image_context.get("intrinsics")
        if not isinstance(intrinsics, dict) or not intrinsics:
            raise ValueError("单图全面扫描缺少 intrinsics")

        def convert(px, py):
            return _pixel_to_angle_single(px, py, image_context, return_detail=True)

    raw_stages = {
        "coarse": _build_pixel_coarse_path(outer_range, width, height),
        "fine": _build_pixel_fine_path(target_range, width, height),
        "deviation_a": _build_pixel_deviation_specs(target_range, outer_range),
    }
    stages = {}
    skipped = {}
    for stage_name, specs in raw_stages.items():
        entries = []
        seen_angles = set()
        skipped_items = []
        for spec in specs:
            px = int(spec["px"])
            py = int(spec["py"])
            conversion = convert(px, py)
            if not conversion.get("success"):
                skipped_item = dict(spec)
                skipped_item.update(conversion)
                skipped_items.append(skipped_item)
                continue
            pan, tilt = float(conversion["pan"]), float(conversion["tilt"])
            angle_key = (round(pan, 2), round(tilt, 2))
            if angle_key in seen_angles:
                skipped_items.append({
                    "px": px,
                    "py": py,
                    "reason": "duplicate_execution_pose",
                    "pan": angle_key[0],
                    "tilt": angle_key[1],
                    "limits": conversion.get("limits"),
                    "position": {"pan": angle_key[0], "tilt": angle_key[1]},
                })
                continue
            seen_angles.add(angle_key)
            entry = dict(spec)
            entry["px"] = px
            entry["py"] = py
            entry["pan"] = angle_key[0]
            entry["tilt"] = angle_key[1]
            entry["conversion_status"] = "executable"
            entries.append(entry)
        stages[stage_name] = entries
        skipped[stage_name] = skipped_items
    return {
        "mode": mode,
        "image_context": image_context,
        "outer_range": outer_range,
        "target_range": target_range,
        "stages": stages,
        "skipped": skipped,
    }


def _log_full_scan_execution_plan(logger_obj, pixel_execution_plan, *,
                                  config_priority=False):
    """按阶段逐点输出正式生产计划，避免把几十个点压成单条日志。"""
    stage_meta = (
        ("coarse_grid", "粗扫内部格心", "coarse",
         lambda item: item.get("point_type") != "outer_probe"),
        ("coarse_outer", "粗扫外扩点", "coarse",
         lambda item: item.get("point_type") == "outer_probe"),
        ("fine", "细扫点", "fine", lambda item: True),
        ("deviation_a", "偏差区点", "deviation_a", lambda item: True),
    )
    execution_index = 0
    stages = pixel_execution_plan.get("stages") or {}
    skipped = pixel_execution_plan.get("skipped") or {}
    for _key, title, source_stage, predicate in stage_meta:
        executable = [item for item in stages.get(source_stage, []) if predicate(item)]
        skipped_items = [item for item in skipped.get(source_stage, []) if predicate(item)]
        logger_obj.info(
            f"[{title}计划] 可执行{len(executable)}点，跳过{len(skipped_items)}点"
        )
        policy = (
            "config_priority"
            if config_priority and source_stage in ("coarse", "fine")
            else "point_priority"
        )
        for local_index, item in enumerate(executable, 1):
            execution_index += 1
            point_id = (
                f"coarse_point_{stages.get('coarse', []).index(item)}"
                if source_stage == "coarse"
                else f"{source_stage}_point_{stages.get(source_stage, []).index(item)}"
            )
            item["execution_index"] = execution_index
            item["point_id"] = point_id
            logger_obj.info(
                f"[{title} {local_index}/{len(executable)}] "
                f"execution_index={execution_index} point_id={point_id} "
                f"type={item.get('point_type', source_stage)} "
                f"pixel=({item.get('px')},{item.get('py')}) "
                f"angle=({item.get('pan')},{item.get('tilt')}) phase={source_stage} "
                f"policy={policy} status=executable"
            )
        for skip_index, item in enumerate(skipped_items, 1):
            logger_obj.warning(
                f"[{title}跳过 {skip_index}/{len(skipped_items)}] "
                f"execution_index=None point_id=None type={item.get('point_type', source_stage)} "
                f"pixel=({item.get('px')},{item.get('py')}) "
                f"computed_angle=({item.get('pan')},{item.get('tilt')}) "
                f"phase={source_stage} policy={policy} status=skipped "
                f"reason={item.get('reason')} limits={item.get('limits')} "
                f"exception_type={item.get('exception_type')} "
                f"exception_message={item.get('exception_message')}"
            )


def _limit_full_scan_minimal_test_entries(entries, *, stage_name):
    """Limit converted execution entries for the guarded internal minimal-point test mode."""
    if stage_name == "coarse":
        outer_probe_entries = [
            entry for entry in (entries or [])
            if entry.get("point_type") == "outer_probe"
        ][:FULL_SCAN_MINIMAL_TEST_COARSE_OUTER_PROBE_POINTS]
        grid_entries = [
            entry for entry in (entries or [])
            if entry.get("point_type") != "outer_probe"
        ][:FULL_SCAN_MINIMAL_TEST_COARSE_GRID_POINTS]
        return outer_probe_entries + grid_entries
    if stage_name == "fine":
        return list(entries or [])[:FULL_SCAN_MINIMAL_TEST_FINE_POINTS]
    if stage_name == "deviation_a":
        limited = []
        counts_by_layer = {}
        for entry in entries or []:
            layer = entry.get("layer")
            count = counts_by_layer.get(layer, 0)
            if count >= FULL_SCAN_MINIMAL_TEST_DEVIATION_POINTS_PER_LAYER:
                continue
            counts_by_layer[layer] = count + 1
            limited.append(entry)
        return limited
    return list(entries or [])


def _apply_full_scan_minimal_test_execution_plan(pixel_execution_plan):
    """Apply guarded internal minimal-point limits after pixel-to-angle conversion."""
    if not isinstance(pixel_execution_plan, dict):
        return pixel_execution_plan
    stages = pixel_execution_plan.get("stages")
    if not isinstance(stages, dict):
        return pixel_execution_plan
    original_counts = {
        name: len(items or [])
        for name, items in stages.items()
    }
    limited_stages = {}
    for stage_name, entries in stages.items():
        limited_stages[stage_name] = _limit_full_scan_minimal_test_entries(
            entries,
            stage_name=stage_name,
        )
    pixel_execution_plan = dict(pixel_execution_plan)
    pixel_execution_plan["stages"] = limited_stages
    pixel_execution_plan["internal_test_minimal_points"] = True
    pixel_execution_plan["minimal_point_limits"] = {
        "coarse": {
            "outer_probe": FULL_SCAN_MINIMAL_TEST_COARSE_OUTER_PROBE_POINTS,
            "grid": FULL_SCAN_MINIMAL_TEST_COARSE_GRID_POINTS,
        },
        "fine": {
            "grid": FULL_SCAN_MINIMAL_TEST_FINE_POINTS,
        },
        "deviation_a": {
            "points_per_layer": FULL_SCAN_MINIMAL_TEST_DEVIATION_POINTS_PER_LAYER,
        },
    }
    pixel_execution_plan["original_stage_counts"] = original_counts
    pixel_execution_plan["limited_stage_counts"] = {
        name: len(items or [])
        for name, items in limited_stages.items()
    }
    return pixel_execution_plan


def _full_scan_angle_range_from_entries(entries):
    """从转换成功的像素正式点生成旧筛选层暂时需要的执行角度包络。"""
    if not entries:
        return None
    pans = [float(item["pan"]) for item in entries]
    tilts = [float(item["tilt"]) for item in entries]
    return {
        "pan_range": [min(pans), max(pans)],
        "tilt_range": [min(tilts), max(tilts)],
    }


# 如果直接运行这个脚本，就调用主函数
if __name__ == "__main__":
    ptz_process_main()
