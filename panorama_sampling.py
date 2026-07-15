"""
===================================================================================
S1. 全景图采样点优化算法
===================================================================================
从云台扫描路径中筛选最优停留点，最小化拼接次数。
输入：完整路径点集合 [(pan, tilt), ...]
输出：推荐在哪些点位停止并采样
"""

import math
import logging

logger = logging.getLogger(__name__)

# IMX577 摄像头参数（店家标称：水平120°、垂直~68°）
# 实际填入值取标称的 ~83%，预留边缘畸变安全余量
# 后续实测拼接效果良好后，可逐步调大
DEFAULT_FOV_H = 100.0   # 水平视场角（保守值）
DEFAULT_FOV_V = 55.0    # 垂直视场角（保守值）
# 重叠率设为 50%，确保 cv2.Stitcher 的特征点匹配有足够的公共区域
# 水平步距 = 100° * (1.0 - 0.50) = 50°，垂直步距 = 55° * (1.0 - 0.50) = 27.5°
# 原来 0.15 → 步距约 85°，几乎没有重叠，导致拼接失败/畸变
DEFAULT_OVERLAP_RATIO = 0.50  # 相邻图像重叠率（提高到50%，改善拼接质量）


def _angular_distance(pa, ta, pb, tb):
    """计算两点之间的角度距离（度）"""
    dpan = abs(pa - pb)
    dtilt = abs(ta - tb)
    return math.sqrt(dpan * dpan + dtilt * dtilt)


def select_sampling_points(
    path_points,
    fov_h=None,
    fov_v=None,
    overlap_ratio=None,
    min_step_pan=None,
    min_step_tilt=None,
):
    """
    从完整扫描路径中筛选全景图采样点，最小化拼接次数。

    Args:
        path_points: 完整路径点 [(pan, tilt), ...]，顺序与扫描顺序一致
        fov_h: 水平视场角（度），默认 60
        fov_v: 垂直视场角（度），默认 45
        overlap_ratio: 相邻图像重叠率 0~1，默认 0.15
        min_step_pan: 最小水平步进（度），若设则覆盖 fov_h 计算
        min_step_tilt: 最小垂直步进（度），若设则覆盖 fov_v 计算

    Returns:
        list: 筛选后的采样点 [(pan, tilt), ...]，保持路径顺序

    策略说明：
        - 先按水平步长确定哪些 Pan 列需要被采样
        - 对每个入选的 Pan 列，保留该列下的全部 Tilt 角度（垂直方向不跳过）
        这保证了同一水平角度下所有垂直角度都能被拍到
    """
    if not path_points:
        return []

    fov_h = fov_h if fov_h is not None else DEFAULT_FOV_H
    fov_v = fov_v if fov_v is not None else DEFAULT_FOV_V
    overlap_ratio = overlap_ratio if overlap_ratio is not None else DEFAULT_OVERLAP_RATIO

    # 有效视场 = FOV * (1 - overlap)，相邻采样点最大间距
    if min_step_pan is not None:
        max_step_pan = min_step_pan
    else:
        max_step_pan = fov_h * (1.0 - overlap_ratio)
    # 注：之前有一个 <= 30.0 的硬上限限制，导致图片过于密集、重叠过高而产生 
    # “旋转-平移”数学歧义崩溃 (ERR_CAMERA_PARAMS_ADJUST_FAIL)。
    # 现已移除该硬上限，允许放开手脚使用 50°~90° 这样的大步长形成天然视差。
    # max_step_pan = min(max_step_pan, 30.0)  <-- 已移除

    # -----------------------------------------------------------------------
    # 第一步：计算 Pan 和 Tilt 的独立抽骨架策略 (动态网格化)
    # -----------------------------------------------------------------------
    max_step_tilt = fov_v * (1.0 - overlap_ratio) if min_step_tilt is None else min_step_tilt

    seen_pans = sorted(list(set(p[0] for p in path_points)))
    seen_tilts = sorted(list(set(p[1] for p in path_points)))

    def get_optimal_1D(items, max_step):
        if not items:
            return []
        range_val = items[-1] - items[0]
        # 如果扫描总跨度很小（如小于最大步长的 80%），单张图即可完全覆盖该层，直接取该轴的中心点
        if range_val < max_step * 0.8:
            mid = (items[0] + items[-1]) / 2.0
            return [min(items, key=lambda x: abs(x - mid))]
        
        # 否则按几何均分计算所需的最少分段数 (构成如 1列, 3列 这样的等距网格)
        intervals = int(math.ceil(range_val / max_step))
        selected = []
        for i in range(intervals + 1):
            target_val = items[0] + i * (range_val / intervals)
            closest = min(items, key=lambda x: abs(x - target_val))
            if closest not in selected:
                selected.append(closest)
        return selected

    selected_pans_set = set(get_optimal_1D(seen_pans, max_step_pan))
    selected_tilts_set = set(get_optimal_1D(seen_tilts, max_step_tilt))

    # -----------------------------------------------------------------------
    # 第二步：在原路径中，仅保留落在“幸运行列”交叉点上的坐标
    # -----------------------------------------------------------------------
    selected = [(pan, tilt) for pan, tilt in path_points if pan in selected_pans_set and tilt in selected_tilts_set]

    logger.info(
        f"采样点优化: 路径 {len(path_points)} 点 -> 采样 {len(selected)} 点 "
        f"(选中 {len(selected_pans_set)} 个Pan列，"
        f"选中 {len(selected_tilts_set)} 个Tilt行, "
        f"FOV={fov_h:.0f}°x{fov_v:.0f}°, overlap={overlap_ratio:.0%}, 水平步距≥{max_step_pan:.1f}°)"
    )
    return selected



def select_sampling_indices(path_points, fov_h=None, fov_v=None, overlap_ratio=None):
    """
    返回采样点在原路径中的索引列表，便于在扫描循环中判断是否需拍照。

    Returns:
        set: 需要采样的索引 {0, 5, 12, ...}
    """
    selected = select_sampling_points(
        path_points, fov_h=fov_h, fov_v=fov_v, overlap_ratio=overlap_ratio
    )
    path_set = {p: i for i, p in enumerate(path_points)}
    indices = set()
    for pt in selected:
        if pt in path_set:
            indices.add(path_set[pt])
    return indices
