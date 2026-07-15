"""
全景拼接工具：与 camera_calibration/panorama_test/step4_panorama_stitch.py 算法一致。
供 web_server 全景接口使用。
"""

from __future__ import annotations

import math

import cv2
import numpy as np

# Kept for older callers/configs; multiband blending now controls seam softness.
SEAM_FEATHER_PX = 5
MULTIBAND_LEVELS = 5
SEAM_EDGE_PENALTY_WEIGHT = 0.35


def _edge_penalty_map(img_a, img_b):
    gray_a = cv2.cvtColor(np.clip(img_a, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(np.clip(img_b, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY)

    def edge_mag(gray):
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        return cv2.magnitude(gx, gy)

    edge = (edge_mag(gray_a) + edge_mag(gray_b)) * 0.5
    edge_max = float(edge.max())
    if edge_max > 1e-6:
        edge = edge / edge_max * 255.0
    return edge.astype(np.float64)


def _overlap_col_bounds(overlap_mask):
    H, W = overlap_mask.shape
    overlap_rows = np.where(overlap_mask.any(axis=1))[0]
    first_col = np.full(H, W, dtype=np.int32)
    last_col = np.full(H, -1, dtype=np.int32)

    for row in overlap_rows:
        cols = np.where(overlap_mask[row, :])[0]
        first_col[row] = cols[0]
        last_col[row] = cols[-1]

    return overlap_rows, first_col, last_col


def _fallback_row_seam(diff, overlap_rows, first_col, last_col):
    H = diff.shape[0]
    seam_col = np.full(H, -1, dtype=np.int32)

    for row in overlap_rows:
        c0, c1 = first_col[row], last_col[row]
        if c1 - c0 < 2:
            seam_col[row] = (c0 + c1) // 2
            continue

        cols = np.arange(c0, c1 + 1)
        row_diff = diff[row, cols]
        cum = np.cumsum(row_diff)
        total = cum[-1]
        if total < 1e-6:
            seam_col[row] = (c0 + c1) // 2
        else:
            seam_col[row] = c0 + int(np.argmin(np.abs(2 * cum - total)))

    return seam_col


def _find_dp_horizontal_seam(diff, overlap_mask, overlap_rows, first_col, last_col):
    """Find a smooth column-per-row seam for left/right panorama ownership."""
    _H, W = diff.shape
    if len(overlap_rows) < 3:
        return _fallback_row_seam(diff, overlap_rows, first_col, last_col)

    inf = 1e12
    rows = overlap_rows.astype(np.int32)
    cost = np.where(overlap_mask[rows, :], diff[rows, :], inf).astype(np.float64)
    dp = np.empty_like(cost)
    back = np.zeros(cost.shape, dtype=np.int8)
    dp[0, :] = cost[0, :]

    for y in range(1, cost.shape[0]):
        prev = dp[y - 1, :]
        from_left = np.empty(W, dtype=np.float64)
        from_right = np.empty(W, dtype=np.float64)
        from_left[0] = inf
        from_left[1:] = prev[:-1]
        from_right[:-1] = prev[1:]
        from_right[-1] = inf

        candidates = np.vstack((from_left, prev, from_right))
        best_idx = np.argmin(candidates, axis=0)
        best = candidates[best_idx, np.arange(W)]
        dp[y, :] = cost[y, :] + best
        back[y, :] = best_idx.astype(np.int8) - 1

    col = int(np.argmin(dp[-1, :]))
    if not np.isfinite(dp[-1, col]) or dp[-1, col] >= inf:
        return _fallback_row_seam(diff, overlap_rows, first_col, last_col)

    seam_col = np.full(diff.shape[0], -1, dtype=np.int32)
    for y in range(cost.shape[0] - 1, -1, -1):
        row = int(rows[y])
        seam_col[row] = col
        col += int(back[y, col])

    for row in overlap_rows:
        seam_col[row] = int(np.clip(seam_col[row], first_col[row], last_col[row]))

    return seam_col


def _gaussian_pyramid(img, max_levels):
    pyr = [img.astype(np.float32, copy=False)]
    for _ in range(1, max_levels):
        h, w = pyr[-1].shape[:2]
        if h < 2 or w < 2:
            break
        pyr.append(cv2.pyrDown(pyr[-1]))
    return pyr


def _laplacian_pyramid(img, max_levels):
    gauss = _gaussian_pyramid(img, max_levels)
    lap = []
    for i in range(len(gauss) - 1):
        size = (gauss[i].shape[1], gauss[i].shape[0])
        up = cv2.pyrUp(gauss[i + 1], dstsize=size)
        lap.append(gauss[i] - up)
    lap.append(gauss[-1])
    return lap


def _reconstruct_laplacian_pyramid(pyr):
    img = pyr[-1]
    for level in range(len(pyr) - 2, -1, -1):
        size = (pyr[level].shape[1], pyr[level].shape[0])
        img = cv2.pyrUp(img, dstsize=size) + pyr[level]
    return img


def _multiband_blend(images, masks, levels=MULTIBAND_LEVELS):
    mask_pyrs = [_gaussian_pyramid(mask, levels) for mask in masks]
    level_count = min(len(pyr) for pyr in mask_pyrs)
    alpha_sums = []

    for level in range(level_count):
        alpha_sum = np.zeros(mask_pyrs[0][level].shape, dtype=np.float32)
        for mask_pyr in mask_pyrs:
            alpha_sum += mask_pyr[level]
        alpha_sums.append(alpha_sum)

    blended_pyr = [
        np.zeros((*alpha_sum.shape, 3), dtype=np.float32) for alpha_sum in alpha_sums
    ]

    for img, mask_pyr in zip(images, mask_pyrs):
        img_pyr = _laplacian_pyramid(img, level_count)
        for level in range(level_count):
            alpha_sum = alpha_sums[level]
            safe = np.maximum(alpha_sum, 1e-6)
            alpha = mask_pyr[level] / safe
            alpha = np.where(alpha_sum > 1e-6, alpha, 0.0)
            blended_pyr[level] += img_pyr[level] * alpha[:, :, np.newaxis]

    return _reconstruct_laplacian_pyramid(blended_pyr)


def compute_shot_grid(pan_min, pan_max, tilt_min, tilt_max, fov_h, fov_v, overlap):
    """
    根据 FOV 和重叠率计算云台拍摄点网格。
    overlap: 0~1，相邻两张图重叠比例。
    """
    step_pan = fov_h * (1.0 - overlap)
    step_tilt = fov_v * (1.0 - overlap)

    def axis_centers(a_min, a_max, fov, step):
        half = fov / 2.0
        if a_max - a_min <= fov:
            return [(a_min + a_max) / 2.0]
        centers = []
        c = a_min + half
        while c <= a_max - half + 1e-6:
            centers.append(c)
            c += step
        if centers[-1] < a_max - half - 1e-6:
            centers.append(a_max - half)
        return centers

    pan_centers = axis_centers(pan_min, pan_max, fov_h, step_pan)
    tilt_centers = axis_centers(tilt_min, tilt_max, fov_v, step_tilt)

    shots = [(p, t) for t in tilt_centers for p in pan_centers]
    return shots, step_pan, step_tilt


def _project_shot(shot, w_x, w_y, w_z, K, canvas_shape):
    """将单张去畸变图投影到画布坐标系，返回 (sampled_float64, weight_float64)。"""
    H, W = canvas_shape
    img_ud = shot["img"]
    h_img, w_img = img_ud.shape[:2]

    fx = float(K[0, 0])
    fy = float(K[1, 1])
    cx = float(K[0, 2])
    cy = float(K[1, 2])

    Ps = math.radians(shot["pan"])
    Ts = math.radians(shot["tilt"])
    cosP, sinP = math.cos(Ps), math.sin(Ps)
    cosT, sinT = math.cos(Ts), math.sin(Ts)

    a_x = w_x * cosP - w_z * sinP
    a_y = w_y
    a_z = w_x * sinP + w_z * cosP

    c_x = a_x
    c_y = a_y * cosT - a_z * sinT
    c_z = a_y * sinT + a_z * cosT

    vis = c_z > 0
    cz_s = np.where(vis, c_z, 1.0)
    src_x = np.where(vis, cx + fx * (c_x / cz_s), -1.0)
    src_y = np.where(vis, cy - fy * (c_y / cz_s), -1.0)

    margin = 1
    in_bounds = (
        vis
        & (src_x >= margin)
        & (src_x < w_img - margin)
        & (src_y >= margin)
        & (src_y < h_img - margin)
    )

    weight = np.where(in_bounds, np.maximum(0.0, c_z) ** 2, 0.0)

    map_x = src_x.astype(np.float32)
    map_y = src_y.astype(np.float32)
    map_x[~in_bounds] = 0
    map_y[~in_bounds] = 0

    sampled = cv2.remap(
        img_ud, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )
    return sampled.astype(np.float64), weight


def stitch_panorama(
    shots_meta: list,
    K,
    fov_h,
    fov_v,
    pan_min,
    pan_max,
    tilt_min,
    tilt_max,
    px_per_deg: float,
):
    """
    shots_meta: list of {"pan":..., "tilt":..., "img": ndarray(去畸变图)}
    """
    W = max(1, int(round((pan_max - pan_min) * px_per_deg)))
    H = max(1, int(round((tilt_max - tilt_min) * px_per_deg)))

    pan_arr = np.linspace(pan_min, pan_max, W, dtype=np.float64)
    tilt_arr = np.linspace(tilt_max, tilt_min, H, dtype=np.float64)
    P_grid, T_grid = np.meshgrid(np.radians(pan_arr), np.radians(tilt_arr))

    w_x = np.sin(P_grid) * np.cos(T_grid)
    w_y = np.sin(T_grid)
    w_z = np.cos(P_grid) * np.cos(T_grid)

    shot_imgs = []
    shot_weights = []

    for shot in shots_meta:
        sampled, weight = _project_shot(shot, w_x, w_y, w_z, K, (H, W))
        if not np.any(weight > 0):
            continue
        shot_imgs.append(sampled)
        shot_weights.append(weight)

    if not shot_imgs:
        return np.zeros((H, W, 3), dtype=np.uint8)

    n = len(shot_imgs)
    I_stack = np.stack(shot_imgs, axis=0)
    W_stack = np.stack(shot_weights, axis=0)

    cov0 = W_stack[0] > 0.01
    for i in range(1, n):
        cov_i = W_stack[i] > 0.01
        overlap = cov0 & cov_i
        if overlap.sum() > 500:
            mean_ref = float(I_stack[0][overlap].mean())
            mean_cur = float(I_stack[i][overlap].mean())
            if mean_cur > 1.0:
                gain = float(np.clip(mean_ref / mean_cur, 0.5, 2.0))
                I_stack[i] = (I_stack[i] * gain).clip(0, 255)

    cov = W_stack > 0.01
    owner = np.argmax(W_stack, axis=0)

    for i in range(n - 1):
        j = i + 1
        overlap_mask = cov[i] & cov[j]
        overlap_rows, first_col, last_col = _overlap_col_bounds(overlap_mask)
        if len(overlap_rows) < 3:
            continue

        # DP seam: one globally smooth column-per-row path for left/right cuts.
        diff = np.abs(I_stack[i] - I_stack[j]).mean(axis=2)
        edge_cost = _edge_penalty_map(I_stack[i], I_stack[j])
        seam_cost = diff + edge_cost * SEAM_EDGE_PENALTY_WEIGHT

        seam_col = _find_dp_horizontal_seam(
            seam_cost, overlap_mask, overlap_rows, first_col, last_col
        )

        for row in overlap_rows:
            c0 = first_col[row]
            c1 = last_col[row]
            sc = int(seam_col[row])
            owner[row, c0:sc] = i
            owner[row, sc : c1 + 1] = j

    W_total = W_stack.sum(axis=0)
    masks = []
    for i in range(n):
        masks.append(((owner == i) & cov[i]).astype(np.float32))

    canvas = _multiband_blend(I_stack, masks, MULTIBAND_LEVELS)
    alpha_sum = np.zeros((H, W), dtype=np.float32)
    for mask in masks:
        alpha_sum += mask

    gap_mask = (alpha_sum < 0.05) & (W_total > 1e-4)
    if np.any(gap_mask):
        fallback_sum = np.zeros((H, W, 3), dtype=np.float64)
        for i in range(n):
            fallback_sum += I_stack[i] * W_stack[i, :, :, np.newaxis]
        fallback_w = np.maximum(W_total, 1e-9)[:, :, np.newaxis]
        fallback = fallback_sum / fallback_w
        canvas[gap_mask] = fallback[gap_mask]
        alpha_sum[gap_mask] = 1.0

    result = canvas.clip(0, 255).astype(np.uint8)
    result[W_total < 1e-9] = 0

    return result
