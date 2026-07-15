"""
网格计算工具库 - 云台扫描系统
提供网格索引转换、区域映射等核心算法
"""

import logging
import redis
import os
import json
import time
import math
# 配置日志
logger = logging.getLogger("gridutils")
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# 格式化器
formatter = logging.Formatter(
    '%(asctime)s [%(levelname)s] [capture_worker] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S,%f'
)

console_handler.setFormatter(formatter)

logger.addHandler(console_handler)
# Redis连接配置
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))

# ====== 全局网格配置（从 Redis gimbal:default_config 读取） ======
# 初始化全局变量
GLOBAL_PAN_RANGE = None
GLOBAL_TILT_RANGE = None
GLOBAL_PAN_MIN = None
GLOBAL_PAN_MAX = None
GLOBAL_TILT_MIN = None
GLOBAL_TILT_MAX = None


# 🔥 新增：本地缓存的步径配置
_cached_step_config = {
    'steph': None,
    'stepv': None,
    'last_update': 0,
    'cache_ttl': 30  # 缓存30秒
}


def get_cached_step_config(r, force_refresh=False):
    """
    获取缓存的步径配置，减少Redis查询
    
    Args:
        r: Redis连接对象
        force_refresh: 是否强制刷新缓存
    
    Returns:
        tuple: (steph, stepv) 或 (None, None) 如果读取失败
    """
    global _cached_step_config
    current_time = time.time()
    
    # 检查缓存是否有效
    if (not force_refresh and 
        _cached_step_config['steph'] is not None and 
        _cached_step_config['stepv'] is not None and
        current_time - _cached_step_config['last_update'] < _cached_step_config['cache_ttl']):
        return _cached_step_config['steph'], _cached_step_config['stepv']
    
    # 缓存过期或无效，从Redis读取
    try:
        current_config = r.get('gimbal:current_config')
        if current_config:
            config = json.loads(current_config)
            steph = config.get('source_steph')
            stepv = config.get('source_stepv')
            if steph is not None and stepv is not None:
                # 更新缓存
                _cached_step_config.update({
                    'steph': float(steph),
                    'stepv': float(stepv),
                    'last_update': current_time
                })
                logger.debug(f"✅ 步径配置已缓存: ({steph}, {stepv})")
                return float(steph), float(stepv)
        
        # 如果没有当前配置，尝试从默认配置读取
        logger.warning("❌ Redis中没有找到当前步径配置，尝试读取默认配置")
        default_config_raw = r.hgetall('gimbal:default_config')
        if default_config_raw:
            steph = default_config_raw.get('pan_step')
            stepv = default_config_raw.get('tilt_step')
            if steph is not None and stepv is not None:
                # 更新缓存
                _cached_step_config.update({
                    'steph': float(steph),
                    'stepv': float(stepv),
                    'last_update': current_time
                })
                logger.info(f"✅ 使用默认步径配置并缓存: ({steph}, {stepv})")
                return float(steph), float(stepv)
        
        # 如果连默认配置都没有，使用硬编码默认值
        logger.warning("❌ 没有找到任何步径配置，使用硬编码默认值并缓存")
        _cached_step_config.update({
            'steph': 5.0,
            'stepv': 5.0,
            'last_update': current_time
        })
        return 5.0, 5.0
        
    except Exception as e:
        logger.error(f"❌ 获取步径配置失败: {e}")
        # 如果Redis读取失败，但有缓存，则返回缓存值
        if _cached_step_config['steph'] is not None and _cached_step_config['stepv'] is not None:
            logger.warning("Redis读取失败，使用缓存的步径配置")
            return _cached_step_config['steph'], _cached_step_config['stepv']
        return None, None


def invalidate_step_cache():
    """
    手动清除步径缓存，用于配置更新时
    """
    global _cached_step_config
    _cached_step_config.update({
        'steph': None,
        'stepv': None,
        'last_update': 0
    })
    logger.info("✅ 步径缓存已清除")

def set_global_grid_config(pan_range, tilt_range):
    """
    从 Redis 'gimbal:default_config' 读取全局扫描范围与步径，更新模块级全局变量
    供后续函数a/函数b做 index 计算与跨步径映射用
    """
    global GLOBAL_PAN_RANGE, GLOBAL_TILT_RANGE, GLOBAL_PAN_MIN, GLOBAL_PAN_MAX, GLOBAL_TILT_MIN, GLOBAL_TILT_MAX
    
    try:
        GLOBAL_PAN_RANGE = [float(pan_range[0]), float(pan_range[1])]
        GLOBAL_TILT_RANGE = [float(tilt_range[0]), float(tilt_range[1])]
        logger.info(f"✅ grid_utils 已配置: Pan={GLOBAL_PAN_RANGE}, Tilt={GLOBAL_TILT_RANGE}")
        GLOBAL_PAN_MIN = GLOBAL_PAN_RANGE[0]
        GLOBAL_PAN_MAX = GLOBAL_PAN_RANGE[1]
        GLOBAL_TILT_MIN = GLOBAL_TILT_RANGE[0]
        GLOBAL_TILT_MAX = GLOBAL_TILT_RANGE[1]
    except Exception as e:
        logger.error(f"❌ grid_utils 配置失败: {e}")
        raise ValueError("无效的网格配置参数")

def create_p_data_if_needed(r, pan, tilt, steph=None, stepv=None):
    """
    按需创建P数据：根据角度和步径计算P索引，如果不存在则创建
    """
    try:
        # 如果步径为None，从Redis读取
        if steph is None or stepv is None:
            # steph, stepv = get_current_step_from_redis(r)
            steph, stepv = get_cached_step_config(r)
            if steph is None or stepv is None:
                logger.error("❌ 无法获取步径配置，无法创建P数据")
                return None, None
            
        # 使用现有的函数计算网格信息
    

        result = _grid_index_from_angles(pan, tilt, steph, stepv)
        if not result:
            
            return None, None
            
        p_index, pan_steps, tilt_steps, pan_idx, tilt_idx = result

        # 计算网格边界（使用现有的逻辑）
        left_pan = GLOBAL_PAN_MIN + pan_idx * steph
        right_pan = min(left_pan + steph, GLOBAL_PAN_MAX)
        bottom_tilt = GLOBAL_TILT_MIN + tilt_idx * stepv
        top_tilt = min(bottom_tilt + stepv, GLOBAL_TILT_MAX)
        
        # 生成P键名
        p_key = f"p:{p_index}_{steph:.1f}_{stepv:.1f}_{left_pan:.1f}_{right_pan:.1f}_{bottom_tilt:.1f}_{top_tilt:.1f}"
        
        # 检查是否已存在
        if r.exists(p_key):
            return p_key, r.hgetall(p_key)
        
        
        # 创建P数据
        p_data = {
            "count": 0,
            "rssi_sum": 0.0,
            "rssi_avg": -100.0,
            "last_update_time": 0.0,
            "created_at": time.time(),
            "pan_range": json.dumps([left_pan, right_pan]),
            "tilt_range": json.dumps([bottom_tilt, top_tilt])
        }
        
        # 保存到Redis
        r.hset(p_key, mapping=p_data)
        # logger.info(f"✅ 创建P数据: {p_key}, 范围=({left_pan:.1f}-{right_pan:.1f}, {bottom_tilt:.1f}-{top_tilt:.1f})")
        
        return p_key, p_data
        
    except Exception as e:
        logger.error(f"❌ 创建P数据失败: {e}")
        return None, None

def get_current_step_from_redis(r):
    """
    从Redis读取当前步径配置
    
    Args:
        r: Redis连接对象
    
    Returns:
        tuple: (steph, stepv) 或 (None, None) 如果读取失败
    """
    try:
        current_config = r.get('gimbal:current_config')
        if current_config:
            config = json.loads(current_config)
            steph = config.get('source_steph')
            stepv = config.get('source_stepv')
            if steph is not None and stepv is not None:
                return float(steph), float(stepv)
        
        # 如果没有配置，返回None表示需要外部处理
        # 🔥 如果没有当前配置，尝试从默认配置读取
        logger.warning("❌ Redis中没有找到当前步径配置，尝试读取默认配置")
        default_config = r.get('gimbal:default_config')
        if default_config:
            config = json.loads(default_config)
            steph = config.get('pan_step')
            stepv = config.get('tilt_step')
            if steph is not None and stepv is not None:
                logger.info(f"✅ 使用默认步径配置: ({steph}, {stepv})")
                return float(steph), float(stepv)
        
        # 如果连默认配置都没有，使用硬编码默认值
        logger.warning("❌ 没有找到任何步径配置，使用硬编码默认值")
        return 5.0, 5.0
        
    except Exception as e:
        logger.error(f"❌ 从Redis读取步径配置失败: {e}")
        return None, None

def remove_overlapping_p_data(r, pan, tilt, steph=None, stepv=None):
    """
    【升级版：老网格转换为新网格】
    将与当前数据点重叠的老步径网格转换为新步径的多个网格
    将老网格的rssi_avg值填入所有新创建的网格中
    """
    try:
        # 获取当前步径配置
        if steph is None or stepv is None:
            steph, stepv = get_current_step_from_redis(r)
            if steph is None or stepv is None:
                logger.error("❌ 无法获取步径配置，无法检查重叠")
                return {'deleted': 0, 'created': 0}
        
        # 计算当前数据的网格范围
        result = _grid_index_from_angles(pan, tilt, steph, stepv)
        if not result:
            return {'deleted': 0, 'created': 0}
            
        p_index, pan_steps, tilt_steps, pan_idx, tilt_idx = result
        left_pan = GLOBAL_PAN_MIN + pan_idx * steph
        right_pan = min(left_pan + steph, GLOBAL_PAN_MAX)
        bottom_tilt = GLOBAL_TILT_MIN + tilt_idx * stepv
        top_tilt = min(bottom_tilt + stepv, GLOBAL_TILT_MAX)
        
        # 找到重叠的老网格
        p_keys = r.keys("p:*")
        overlapping_old_keys = []
        
        for p_key in p_keys:
            try:
                parts = p_key.split('_')
                if len(parts) != 7:
                    continue
                
                old_steph = float(parts[1])
                old_stepv = float(parts[2])
                
                # 只处理步径不同的老网格
                if abs(old_steph - steph) < 0.01 and abs(old_stepv - stepv) < 0.01:
                    continue
                
                p_left = float(parts[3])
                p_right = float(parts[4])
                p_bottom = float(parts[5])
                p_top = float(parts[6])
                
                # 检查重叠
                if (left_pan < p_right and right_pan > p_left and 
                    bottom_tilt < p_top and top_tilt > p_bottom):
                    overlapping_old_keys.append(p_key)
                    
            except Exception as e:
                logger.warning(f"解析P键名 {p_key} 时出错: {e}")
                continue
        
        if not overlapping_old_keys:
            return {'deleted': 0, 'created': 0}
        
        # 处理每个重叠的老网格：转换为新网格
        deleted_count = 0
        created_count = 0
        
        for old_key in overlapping_old_keys:
            parts = old_key.split('_')
            old_left = float(parts[3])
            old_right = float(parts[4])
            old_bottom = float(parts[5])
            old_top = float(parts[6])
            
            # 读取老网格的rssi_avg
            old_data = r.hgetall(old_key)
            if not old_data:
                if r.delete(old_key):
                    deleted_count += 1
                continue
            
            old_rssi_avg = float(old_data.get('rssi_avg', -100.0))
            
            # 🔥 新逻辑：基于索引计算老网格覆盖的新网格范围
            pan_start_idx = int((old_left - GLOBAL_PAN_MIN) / steph)
            pan_end_idx = int((old_right - GLOBAL_PAN_MIN) / steph) + 1
            tilt_start_idx = int((old_bottom - GLOBAL_TILT_MIN) / stepv)  
            tilt_end_idx = int((old_top - GLOBAL_TILT_MIN) / stepv) + 1
            
            # 为每个索引位置创建标准的新网格
            for pan_idx in range(pan_start_idx, pan_end_idx):
                for tilt_idx in range(tilt_start_idx, tilt_end_idx):
                    
                    # 计算标准网格边界
                    precise_left = GLOBAL_PAN_MIN + pan_idx * steph
                    precise_right = min(precise_left + steph, GLOBAL_PAN_MAX)
                    precise_bottom = GLOBAL_TILT_MIN + tilt_idx * stepv
                    precise_top = min(precise_bottom + stepv, GLOBAL_TILT_MAX)
                    
                    # 检查新网格是否与老网格真正重叠
                    if (precise_left < old_right and precise_right > old_left and
                        precise_bottom < old_top and precise_top > old_bottom):
                        
                        # 计算新网格的全局索引
                        total_pan_grids = int((GLOBAL_PAN_MAX - GLOBAL_PAN_MIN) / steph)
                        new_p_index = pan_idx * total_pan_grids + tilt_idx
                        
                        # 生成新网格键名
                        new_key = f"p:{new_p_index}_{steph:.1f}_{stepv:.1f}_{precise_left:.1f}_{precise_right:.1f}_{precise_bottom:.1f}_{precise_top:.1f}"
                        
                        # 检查新网格是否已存在且有数据
                        existing_count = r.hget(new_key, 'count')
                        if not existing_count or int(existing_count) == 0:
                            # 创建新网格并填入老网格的rssi_avg
                            current_time = time.time()
                            new_data = {
                                "count": 1,
                                "rssi_sum": f"{old_rssi_avg:.2f}",
                                "rssi_avg": f"{old_rssi_avg:.2f}",
                                "last_update_time": str(current_time),
                                "created_at": str(current_time),
                                "pan_range": json.dumps([precise_left, precise_right]),
                                "tilt_range": json.dumps([precise_bottom, precise_top])
                            }
                            r.hset(new_key, mapping=new_data)
                            created_count += 1
            
            # 删除老网格
            if r.delete(old_key):
                deleted_count += 1
        
        logger.info(f"✅ 网格转换完成: 删除{deleted_count}个老网格, 创建{created_count}个新网格")
        return {'deleted': deleted_count, 'created': created_count}
        
    except Exception as e:
        logger.error(f"❌ 网格转换失败: {e}")
        return {'deleted': 0, 'created': 0}

def update_grid_data_atomic(r, pan, tilt, rssi):
    """
    【最终核心函数】一站式处理数据包的网格逻辑。
    capture_worker 只需调用此函数即可。
    它负责：获取当前步径 -> 创建或确保网格存在 -> 执行重叠清理 -> 原子化更新数据。
    """
    try:
        # 1. 从 Redis 获取当前系统定义的源步径
        steph, stepv = get_current_step_from_redis(r)
        if steph is None or stepv is None:
            logger.warning("无法获取当前步径，跳过网格处理")
            return

        # 2. 计算新数据所属的网格信息，如果不存在则创建它
        p_key, p_data = create_p_data_if_needed(r, pan, tilt, steph, stepv)
        if not p_key:
            return # 创建失败，日志已在内部记录

        # 3. 执行重叠检测与清理
        remove_overlapping_p_data(r, pan, tilt, steph, stepv)

        # 4. 【关键】使用原子操作更新该网格的统计数据
        pipe = r.pipeline()
        pipe.hincrby(p_key, "count", 1)
        pipe.hincrbyfloat(p_key, "rssi_sum", float(rssi))
        pipe.hset(p_key, "last_update_time", time.time())
        results = pipe.execute()
        
        # 调试日志，可以看到原子操作后的结果
        logger.info(f"🔍 原子操作结果: {p_key}, count={results[0]}, rssi_sum={results[1]}")

    except Exception as e:
        logger.error(f"❌ 更新网格数据时发生未知错误: {e}", exc_info=True)

def clear_all_global_grids(r):
    """
    清除 Redis 中所有已存在的全局网格数据。
    这是一个危险操作，因为它会删除所有历史数据。

    Args:
        r: Redis 连接对象。
    """
    try:
        # 清除所有 p: 开头的网格数据（全局网格）
        p_keys = r.keys("p:*")
        if p_keys:
            deleted_count = r.delete(*p_keys)
            logger.info(f"✅ 已清除 {deleted_count} 个全局网格数据")
            return deleted_count
        else:
            logger.info("ℹ️ 没有找到全局网格数据")
            return 0
    except Exception as e:
        logger.error(f"❌ 清除全局网格数据失败: {e}")
        return 0
    

# ===================================================================
#  下面的函数 A, B, C 保持不变，因为它们的算法是正确的
# ===================================================================
def _grid_index_from_angles(pan,tilt,steph,stepv):
    # """
    # 根据角度与步径，计算该点所在全局网格的index（1-based）
    # 规则：按 tilt 从下到上，每行 pan 从左到右
    # 返回: (index, pan_steps, tilt_steps, pan_idx, tilt_idx)
    # """
    pan_min, pan_max = GLOBAL_PAN_RANGE
    tilt_min, tilt_max = GLOBAL_TILT_RANGE
    
    try:
        steph = float(steph)
        stepv = float(stepv)
        pan = float(pan)
        tilt = float(tilt)
    except Exception:
        return None

    if steph <= 0 or stepv <= 0:
        return None
    
    pan_steps = math.ceil((pan_max - pan_min) / steph)
    tilt_steps = math.ceil((tilt_max - tilt_min) / stepv)

    pan_idx = int((pan - pan_min) // steph) 
    tilt_idx = int((tilt - tilt_min) // stepv) 

     # 边界裁剪
    pan_idx = max(0, min(pan_idx, pan_steps - 1))
    tilt_idx = max(0, min(tilt_idx, tilt_steps - 1))
    index = tilt_idx * pan_steps + pan_idx + 1  # 线性序号（1-based）
    return index, pan_steps, tilt_steps, pan_idx, tilt_idx

def _angles_from_grid_index(index, steph, stepv):
    """
    函数B：根据index和步径，反推出该网格的矩形区域坐标
    与函数A _grid_index_from_angles 配对使用
    
    Args:
        index: 网格索引（1-based）
        steph: 水平步径
        stepv: 垂直步径
    
    Returns:
        tuple: (左下角pan, 左下角tilt, 右上角pan, 右上角tilt)
    """
    try:
        pan_min, pan_max = GLOBAL_PAN_RANGE
        tilt_min, tilt_max = GLOBAL_TILT_RANGE
        steph = float(steph)
        stepv = float(stepv)
        index = int(index)

        if steph <= 0 or stepv <= 0:
            return None
        
        pan_steps = math.ceil((pan_max - pan_min) / steph)
        tilt_steps = math.ceil((tilt_max - tilt_min) / stepv)

        index_0based = index - 1
        tilt_idx = index_0based // pan_steps
        pan_idx = index_0based % pan_steps

        if tilt_idx >= tilt_steps or pan_idx >= pan_steps:
            return None # 如果 index 过大，则判定为无效
        
        left_pan = pan_min + pan_idx * steph
        right_pan = left_pan + steph
        bottom_tilt = tilt_min + tilt_idx * stepv
        top_tilt = bottom_tilt + stepv

        right_pan = min(right_pan, pan_max)
        top_tilt = min(top_tilt, tilt_max)

        return (left_pan, bottom_tilt, right_pan, top_tilt)
    
    except Exception:
        logger.warning(f"反推网格坐标失败 index={index}, step=({steph},{stepv})")
        return None
    

# ... 在文件末尾添加 ...

def update_current_position_grid_average(r):
    """
    从Redis获取当前PTZ状态，如果状态为IDLE，根据当前角度计算对应网格并更新均值
    这个函数直接在grid_utils内部运行，避免全局变量初始化问题
    """
    try:
        # 检查全局变量是否已初始化
        if GLOBAL_PAN_MIN is None or GLOBAL_PAN_MAX is None or GLOBAL_TILT_MIN is None or GLOBAL_TILT_MAX is None:
            logger.debug("grid_utils全局变量尚未初始化，跳过定时更新")
            return False
        
        # 从Redis获取PTZ状态
        ptz_raw = r.get('ptz:current_status')
        if not ptz_raw:
            logger.debug("未找到PTZ状态，跳过定时更新")
            return False
            
        ptz_status = json.loads(ptz_raw)
        if ptz_status.get('state') != "IDLE":
            logger.debug(f"PTZ状态不是IDLE ({ptz_status.get('state')})，跳过定时更新")
            return False
            
        # 获取当前位置
        position = ptz_status.get('position', {})
        pan = position.get('pan')
        tilt = position.get('tilt')
        if pan is None or tilt is None:
            logger.debug("PTZ位置信息不完整，跳过定时更新")
            return False
            
        # 获取当前步径
        steph, stepv = get_current_step_from_redis(r)
        if steph is None or stepv is None:
            logger.debug("无法获取当前步径，跳过定时更新")
            return False

        # 计算网格索引
        result = _grid_index_from_angles(pan, tilt, steph, stepv)
        if not result:            
            logger.debug("无法计算网格索引，跳过定时更新")
            return False
            
        p_index, pan_steps, tilt_steps, pan_idx, tilt_idx = result

        p_key, p_data = create_p_data_if_needed(r, pan, tilt, steph, stepv)
        
        # 更新网格均值
        grid_data = r.hmget(p_key, 'count', 'rssi_sum')
        if not grid_data[0]:
            logger.debug(f"网格 {p_key} 不存在，跳过更新")
            return False
            
        count = int(grid_data[0])
        rssi_sum = float(grid_data[1])
        
        if count > 0:
            avg = rssi_sum / count
            current_time = time.time()
            
            r.hset(p_key, mapping={
                'rssi_avg': f"{avg:.2f}",
                'last_update_time': str(current_time)
            })
            
            logger.info(f"📊 定时更新均值: {p_key} = {avg:.2f}dBm ({count}个包), 原因:timer")
            return True
        
        return False
        
    except Exception as e:
        logger.error(f"定时更新当前位置网格均值失败: {e}")
        return False
