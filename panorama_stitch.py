"""
===================================================================================
S1. 全景图拼接 - 多图拼接为全景
===================================================================================
依赖：opencv-python 或 opencv-contrib-python（Stitcher 在 contrib 中）
若未安装 contrib：pip install opencv-contrib-python
"""

import os
import glob
import logging

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

logger = logging.getLogger(__name__)


def stitch_images(image_paths, output_path="panorama_result.jpg"):
    """
    将多张有重叠区域的图像拼接为全景图。

    Args:
        image_paths: 图像路径列表，按拍摄顺序（从左到右或按角度顺序）
        output_path: 输出全景图路径

    Returns:
        (success: bool, output_path: str|None)
    """
    if not HAS_CV2:
        logger.error("未安装 opencv-python")
        return False, None

    # Stitcher 在 opencv-contrib 中，不同版本 API 不同
    stitcher = None
    try:
        stitcher = cv2.Stitcher.create(cv2.Stitcher_PANORAMA)
    except AttributeError:
        try:
            stitcher = cv2.Stitcher_create(getattr(cv2, "STITCHER_PANORAMA", 0))
        except AttributeError:
            try:
                stitcher = cv2.createStitcher(False)
            except AttributeError:
                pass
    if stitcher is None:
        logger.error("OpenCV 未包含 Stitcher，请安装: pip install opencv-contrib-python")
        return False, None

    if len(image_paths) < 2:
        logger.error("至少需要 2 张图像才能拼接")
        return False, None

    imgs = []
    for p in image_paths:
        img = cv2.imread(p)
        if img is None:
            logger.warning(f"无法读取: {p}")
            continue
        imgs.append(img)

    if len(imgs) < 2:
        logger.error("有效图像不足 2 张")
        return False, None

    logger.info(f"正在拼接 {len(imgs)} 张图像...")
    status, pano = stitcher.stitch(imgs)

    # Stitcher_OK = 0，兼容不同 OpenCV 版本
    ok_status = getattr(cv2, "Stitcher_OK", 0) or getattr(cv2, "STITCHER_OK", 0)
    if status != ok_status:
        err_msg = {
            1: "ERR_NEED_MORE_IMGS",
            2: "ERR_HOMOGRAPHY_EST_FAIL",
            3: "ERR_CAMERA_PARAMS_ADJUST_FAIL",
        }.get(status, f"未知错误({status})")
        logger.error(f"拼接失败: {err_msg}")
        return False, None

    cv2.imwrite(output_path, pano)
    logger.info(f"✅ 全景图已保存: {output_path} ({pano.shape[1]}x{pano.shape[0]})")
    return True, output_path


def stitch_folder(folder_path, pattern="*.jpg", output_path=None):
    """
    拼接指定文件夹内的图像（按文件名排序）。

    Args:
        folder_path: 文件夹路径
        pattern: 文件名匹配，默认 *.jpg
        output_path: 输出路径，默认 folder/panorama_result.jpg
    """
    folder_path = os.path.abspath(folder_path)
    if not os.path.isdir(folder_path):
        logger.error(f"目录不存在: {folder_path}")
        return False, None

    paths = sorted(glob.glob(os.path.join(folder_path, pattern)))
    if not paths:
        logger.error(f"未找到匹配的图像: {folder_path}/{pattern}")
        return False, None

    if output_path is None:
        output_path = os.path.join(folder_path, "panorama_result.jpg")

    return stitch_images(paths, output_path)
