"""
===================================================================================
UVC 摄像头工具 - 全景图采样用
===================================================================================
支持通过 HTTP 服务获取摄像头图片，用于 S1 全景图采样点优化。
依赖：opencv-python (cv2)、requests

前端提供了统一的摄像头 HTTP 服务，避免多进程抢设备：
  - 流媒体：http://127.0.0.1:8080/?action=stream
  - 截图：http://127.0.0.1:8080/?action=snapshot

两种拍照模式：
  1. capture_frame()        — 每次冷启动摄像头，约 10-12 秒（原有方式，向后兼容）
  2. PersistentCamera       — 通过 HTTP 服务获取图片，支持曝光稳定等待（推荐）
"""

import os
import time
import threading
import logging

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logger = logging.getLogger(__name__)

# 默认配置（可通过环境变量覆盖）
DEFAULT_DEVICE_ID = int(os.environ.get("UVC_DEVICE_ID", "0"))
DEFAULT_WIDTH = int(os.environ.get("UVC_WIDTH", "3840"))
DEFAULT_HEIGHT = int(os.environ.get("UVC_HEIGHT", "3040"))
DEFAULT_WARMUP_FRAMES = int(os.environ.get("UVC_WARMUP_FRAMES", "5"))
# 图像翻转：-1=上下+左右, 0=上下, 1=左右, None=不翻转
DEFAULT_FLIP_MODE = -1

# HTTP 摄像头服务配置
CAMERA_HTTP_URL = os.environ.get("CAMERA_HTTP_URL", "http://127.0.0.1:8080/?action=snapshot")
CAMERA_HTTP_TIMEOUT = float(os.environ.get("CAMERA_HTTP_TIMEOUT", "5.0"))


# ===========================================================================
# 常驻模式：PersistentCamera（HTTP 版本）
# ===========================================================================

class PersistentCamera:
    """
    摄像头常驻对象：通过前端提供的 HTTP 服务获取图片，避免多进程抢设备。

    使用方式：
        cam = PersistentCamera()
        cam.open()                      # 一次性初始化（检查 HTTP 服务可用性）
        ok, frame = cam.capture()       # 每次拍照（毫秒级）
        cam.release()                   # 程序退出时关闭
    """

    def __init__(self, http_url=None, timeout=None, **kwargs):
        self.http_url = http_url if http_url is not None else CAMERA_HTTP_URL
        self.timeout = timeout if timeout is not None else CAMERA_HTTP_TIMEOUT

        self._lock = threading.Lock()
        self._latest_frame = None
        self._latest_ok = False
        self._drain_thread = None
        self._running = False

    def open(self):
        """检查 HTTP 摄像头服务是否可用。返回是否成功。"""
        if not HAS_CV2:
            logger.error("未安装 opencv-python，无法使用摄像头功能")
            return False

        if not HAS_REQUESTS:
            logger.error("未安装 requests，无法使用 HTTP 摄像头功能")
            return False

        # 测试 HTTP 服务是否可用
        try:
            response = requests.get(self.http_url, timeout=self.timeout)
            if response.status_code == 200:
                # 解码第一帧
                img_array = np.frombuffer(response.content, dtype=np.uint8)
                frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                if frame is not None:
                    with self._lock:
                        self._latest_ok = True
                        self._latest_frame = frame
                    logger.info(f"✅ [PersistentCamera] HTTP 摄像头服务已就绪: {self.http_url}")
                    
                    # 启动后台抓帧线程
                    self._running = True
                    self._drain_thread = threading.Thread(
                        target=self._drain_loop, daemon=True, name="cam-drain"
                    )
                    self._drain_thread.start()
                    return True
                else:
                    logger.error(f"HTTP 摄像头服务返回的图片无法解码")
                    return False
            else:
                logger.error(f"HTTP 摄像头服务返回错误状态码: {response.status_code}")
                return False
        except requests.exceptions.RequestException as e:
            logger.error(f"无法连接 HTTP 摄像头服务: {e}")
            return False

    def _drain_loop(self):
        """后台线程：持续从 HTTP 服务获取最新帧，保持缓冲区里是最新帧。"""
        while self._running:
            try:
                response = requests.get(self.http_url, timeout=self.timeout)
                if response.status_code == 200:
                    img_array = np.frombuffer(response.content, dtype=np.uint8)
                    frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                    if frame is not None:
                        with self._lock:
                            self._latest_ok = True
                            self._latest_frame = frame
                    else:
                        with self._lock:
                            self._latest_ok = False
                else:
                    with self._lock:
                        self._latest_ok = False
            except Exception as e:
                with self._lock:
                    self._latest_ok = False
                logger.debug(f"[PersistentCamera] HTTP 获取失败: {e}")
            
            # 约 10fps 的速率持续抓帧
            time.sleep(0.1)

    def capture(self, save_path=None):
        """
        取最新一帧并可选保存到文件。

        Returns: (success: bool, frame: ndarray|None)
        """
        with self._lock:
            ok = self._latest_ok
            frame = self._latest_frame.copy() if (self._latest_ok and self._latest_frame is not None) else None

        if not ok or frame is None:
            logger.error("[PersistentCamera] 当前帧无效，尝试重新抓取...")
            ok, frame = self._fetch_frame()
            if not ok or frame is None:
                logger.error("[PersistentCamera] 重新抓取失败")
                return False, None

        # 翻转（摄像头倒置安装）
        if DEFAULT_FLIP_MODE is not None:
            frame = cv2.flip(frame, DEFAULT_FLIP_MODE)

        if save_path:
            try:
                cv2.imwrite(save_path, frame)
                logger.info(f"图像已保存: {save_path} ({frame.shape[1]}x{frame.shape[0]})")
            except Exception as e:
                logger.error(f"保存图像失败: {e}")
                return False, frame

        return True, frame

    def _fetch_frame(self):
        """直接从 HTTP 服务获取一帧图片。"""
        try:
            response = requests.get(self.http_url, timeout=self.timeout)
            if response.status_code == 200:
                img_array = np.frombuffer(response.content, dtype=np.uint8)
                frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                if frame is not None:
                    return True, frame
            return False, None
        except Exception as e:
            logger.error(f"[PersistentCamera] HTTP 获取失败: {e}")
            return False, None

    def capture_stable(self, save_path=None, max_wait_ms=500, stability_threshold=5, stable_frames=3):
        """
        等待曝光稳定后拍照。解决从暗处移到亮处时的过曝问题。

        原理：连续读取多帧计算亮度，当亮度变化小于阈值且持续N帧时，认为曝光稳定。

        Args:
            save_path: 图片保存路径
            max_wait_ms: 最大等待时间（毫秒），超时后直接拍照
            stability_threshold: 亮度变化阈值（0-255），低于此值认为稳定
            stable_frames: 连续稳定帧数要求

        Returns: (success: bool, frame: ndarray|None)
        """
        if not HAS_CV2:
            logger.error("未安装 opencv-python，无法使用摄像头功能")
            return False, None

        start_time = time.time() * 1000
        prev_brightness = None
        stable_count = 0

        logger.debug(f"[capture_stable] 开始等待曝光稳定，超时={max_wait_ms}ms，阈值={stability_threshold}")

        while (time.time() * 1000 - start_time) < max_wait_ms:
            # 直接从 HTTP 服务获取最新帧
            ok, frame = self._fetch_frame()
            if ok and frame is not None:
                # 计算平均亮度（灰度图）
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                current_brightness = float(np.mean(gray))

                if prev_brightness is not None:
                    diff = abs(current_brightness - prev_brightness)
                    if diff < stability_threshold:
                        stable_count += 1
                        if stable_count >= stable_frames:
                            # 曝光稳定，执行拍照
                            elapsed = time.time() * 1000 - start_time
                            logger.info(f"[capture_stable] 曝光已稳定，耗时 {elapsed:.0f}ms，亮度={current_brightness:.1f}")

                            if DEFAULT_FLIP_MODE is not None:
                                frame = cv2.flip(frame, DEFAULT_FLIP_MODE)

                            if save_path:
                                try:
                                    cv2.imwrite(save_path, frame)
                                    logger.info(f"图像已保存: {save_path} ({frame.shape[1]}x{frame.shape[0]})")
                                except Exception as e:
                                    logger.error(f"保存图像失败: {e}")
                                    return False, frame

                            return True, frame
                    else:
                        stable_count = 0  # 亮度还在变化，重置计数
                        logger.debug(f"[capture_stable] 亮度变化={diff:.1f}，重置稳定计数")

                prev_brightness = current_brightness

            time.sleep(0.05)  # 50ms 检查一次

        # 超时，直接取最新帧拍照
        elapsed = time.time() * 1000 - start_time
        logger.warning(f"[capture_stable] 曝光稳定等待超时({elapsed:.0f}ms)，使用当前帧")
        return self.capture(save_path)

    def is_open(self):
        return self._running

    def release(self):
        """关闭摄像头，停止后台线程。"""
        self._running = False
        if self._drain_thread and self._drain_thread.is_alive():
            self._drain_thread.join(timeout=2)
        logger.info("📷 [PersistentCamera] HTTP 摄像头连接已关闭")


# ===========================================================================
# 原有冷启动方式（向后兼容）
# ===========================================================================

def capture_frame(device_id=None, width=None, height=None, warmup_frames=None, save_path=None):
    """
    从 UVC 摄像头采集一帧图像（冷启动，每次重新打开摄像头，约 10-12 秒）。

    建议使用 PersistentCamera 代替。保留此函数仅供向后兼容。
    """
    if not HAS_CV2:
        logger.error("未安装 opencv-python，无法使用摄像头功能。请执行: pip install opencv-python")
        return False, None

    device_id = device_id if device_id is not None else DEFAULT_DEVICE_ID
    width = width if width is not None else DEFAULT_WIDTH
    height = height if height is not None else DEFAULT_HEIGHT
    warmup_frames = warmup_frames if warmup_frames is not None else DEFAULT_WARMUP_FRAMES

    cap = cv2.VideoCapture(device_id, cv2.CAP_V4L2)
    if not cap.isOpened():
        logger.error(f"无法打开摄像头 /dev/video{device_id}")
        return False, None

    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        for _ in range(warmup_frames):
            cap.read()
            time.sleep(0.05)

        ret, frame = cap.read()
        if not ret:
            logger.error("无法读取视频流")
            return False, None

        if DEFAULT_FLIP_MODE is not None:
            frame = cv2.flip(frame, DEFAULT_FLIP_MODE)

        if save_path:
            cv2.imwrite(save_path, frame)
            logger.info(f"图像已保存: {save_path} ({frame.shape[1]}x{frame.shape[0]})")

        return True, frame
    finally:
        cap.release()


def test_camera(device_id=0, output_path="uvc_test_photo.jpg", width=640, height=480):
    """简单测试：打开摄像头并保存一张照片。"""
    success, _ = capture_frame(
        device_id=device_id,
        width=width,
        height=height,
        save_path=output_path,
    )
    return success


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ok = test_camera(output_path="uvc_test_photo.jpg")
    print("✅ 测试通过" if ok else "❌ 测试失败")
