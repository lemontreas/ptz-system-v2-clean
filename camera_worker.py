"""
===================================================================================
S1 步骤二 - Camera Worker：专职拍照进程
===================================================================================
独立进程，监听 Redis 队列 camera:command_queue，收到拍照命令后执行拍照。
完成后将 ACK 回执写入 camera:ack:{request_id}。

支持 MOCK_CAMERA=1 环境变量，无硬件时生成带水印的测试图片。

通信协议：
  命令（LPUSH → BRPOP）:
    {
      "action": "capture_frame",
      "request_id": "uuid",
      "round_id": "mp_20260305_0012",
      "scan_type": "multi_point|full_area",
      "pan": 12.5, "tilt": -3.0,
      "save_dir": "/data/panorama/raw/mp_20260305_0012",
      "filename": "p12.5_t-3.0_ts1710000000.jpg",
      "camera_params": {"width": 3840, "height": 3040}
    }
  回执（SET + TTL）:
    camera:ack:{request_id} = {"code": 0, "msg": "ok", ...}
"""

import redis
import json
import time
import os
import sys
import logging
import logging.handlers
import uuid

CAMERA_COMMAND_QUEUE = "camera:command_queue"
CAMERA_ACK_PREFIX = "camera:ack:"
CAMERA_ACK_TTL = 120  # ACK 过期时间（秒）

MOCK_CAMERA = os.environ.get("MOCK_CAMERA", "0") == "1"

# Redis 配置（与其他 worker 保持一致）
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))


def setup_logging():
    """设置 Camera Worker 日志，仅输出到控制台，不写文件。"""
    logger = logging.getLogger("camera_worker")
    logger.setLevel(logging.INFO)

    # 清理旧 handler，避免重复添加
    for h in logger.handlers[:]:
        logger.removeHandler(h)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] [camera_worker] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(console_handler)
    return logger


CAMERA_SETTLE_DELAY = float(os.environ.get("CAMERA_SETTLE_DELAY", "2.0"))  # PTZ停稳等待（秒）
CAMERA_FLUSH_FRAMES = int(os.environ.get("CAMERA_FLUSH_FRAMES", "5"))     # 丢弃旧帧数和


def _do_capture(save_path, camera_params, logger, persistent_cam=None, flush_frames=0):
    """执行真实拍照。优先使用常驻摄像头（毫秒级），无则回退到冷启动（~12秒）。"""
    if persistent_cam is not None and persistent_cam.is_open():
        # ⚡ 关键修复：不要在主线程调用 persistent_cam._cap.grab()
        # PersistentCamera 内部有一个后台线程一直在循环调用 read()。
        # V4L2 不支持多线程同时并发读取同一个设备节点，并发抓取会导致死锁或极长超时。
        # 只需要等待 0.1 秒，后台线程（约 30fps）自然就会把最新的画面推入缓冲区
        if flush_frames > 0:
            time.sleep(0.1)
        # 常驻模式：等待曝光稳定后拍照（解决从暗处移到亮处的过曝问题）
        ok, _ = persistent_cam.capture_stable(save_path=save_path)
        return ok
    else:
        # 回退：冷启动（兼容无常驻相机的情况）
        from camera_utils import capture_frame
        width = camera_params.get("width", 3840)
        height = camera_params.get("height", 3040)
        device_id = camera_params.get("device_id", 0)
        ok, _ = capture_frame(
            device_id=device_id, width=width, height=height, save_path=save_path
        )
        return ok


def _do_mock_capture(save_path, pan, tilt, logger):
    """Mock 模式：生成带坐标水印的灰色测试图片"""
    try:
        import cv2
        import numpy as np
        img = np.ones((480, 640, 3), dtype=np.uint8) * 180
        text = f"MOCK pan={pan:.1f} tilt={tilt:.1f}"
        ts_text = time.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(img, text, (30, 200), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        cv2.putText(img, ts_text, (30, 260), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1)
        cv2.imwrite(save_path, img)
        logger.info(f"📷 [MOCK] 生成测试图片: {save_path}")
        return True
    except Exception as e:
        logger.error(f"❌ Mock 拍照失败: {e}")
        return False


def handle_capture_command(r, command, logger, persistent_cam=None):
    """处理一条拍照命令"""
    request_id = command.get("request_id", str(uuid.uuid4()))
    round_id = command.get("round_id", "unknown")
    pan = command.get("pan", 0.0)
    tilt = command.get("tilt", 0.0)
    save_dir = command.get("save_dir", "/data/panorama/raw")
    filename = command.get("filename", f"p{pan}_t{tilt}_ts{int(time.time())}.jpg")
    camera_params = command.get("camera_params", {})

    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)

    logger.info(f"📷 拍照命令: round={round_id}, pan={pan}, tilt={tilt}, file={filename}")

    # ── 第一步：停稳延时 ─────────────────────────────────────
    # 云台机械臂转动到位后不会立即停稳，强制等待 CAMERA_SETTLE_DELAY 秒
    # 确保轻微晴动完全消除，避免拍到模糊的画面
    if CAMERA_SETTLE_DELAY > 0:
        logger.info(f"⏳ 等待云台停稳中... ({CAMERA_SETTLE_DELAY:.1f}s)")
        time.sleep(CAMERA_SETTLE_DELAY)

    ts_start = time.time()
    if MOCK_CAMERA:
        ok = _do_mock_capture(save_path, pan, tilt, logger)
    else:
        logger.info(f"🗑️ 清空摄像头缓存旧帧 ({CAMERA_FLUSH_FRAMES} 帧)...")
        ok = _do_capture(save_path, camera_params, logger, persistent_cam=persistent_cam,
                         flush_frames=CAMERA_FLUSH_FRAMES)
    elapsed = time.time() - ts_start

    ack = {
        "code": 0 if ok else -1,
        "msg": "ok" if ok else "capture failed",
        "request_id": request_id,
        "round_id": round_id,
        "pan": pan,
        "tilt": tilt,
        "image_path": save_path if ok else "",
        "ts": time.time(),
        "elapsed": round(elapsed, 3),
    }

    ack_key = f"{CAMERA_ACK_PREFIX}{request_id}"
    r.set(ack_key, json.dumps(ack), ex=CAMERA_ACK_TTL)

    if ok:
        # 将图片路径追加到该轮次的图片列表中
        round_images_key = f"panorama:round:{round_id}:images"
        r.rpush(round_images_key, save_path)
        logger.info(f"✅ 拍照完成: {save_path} ({elapsed:.2f}s)")
    else:
        logger.error(f"❌ 拍照失败: {save_path}")

    return ack


def camera_worker_main():
    logger = setup_logging()
    mode_str = "MOCK" if MOCK_CAMERA else "REAL"
    logger.info(f"🎬 Camera Worker 启动 (模式: {mode_str})")

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

    try:
        r.ping()
        logger.info("✅ Redis 连接成功")
    except redis.exceptions.ConnectionError as e:
        logger.error(f"❌ Redis 连接失败: {e}")
        sys.exit(1)

    # 初始化常驻摄像头（非 Mock 模式）
    persistent_cam = None
    if not MOCK_CAMERA:
        from camera_utils import PersistentCamera
        persistent_cam = PersistentCamera()
        if persistent_cam.open():
            logger.info("✅ 常驻摄像头已初始化，拍照耗时将降至 ~50ms")
        else:
            logger.warning("⚠️ 常驻摄像头初始化失败，将回退到冷启动模式（~12s/张）")
            persistent_cam = None

    logger.info(f"👂 监听队列: {CAMERA_COMMAND_QUEUE}")

    while True:
        try:
            result = r.brpop(CAMERA_COMMAND_QUEUE, timeout=5)
            if result is None:
                continue

            _, cmd_json = result
            try:
                command = json.loads(cmd_json)
            except json.JSONDecodeError:
                logger.error(f"❌ 非法 JSON: {cmd_json[:200]}")
                continue

            action = command.get("action", "")
            if action == "capture_frame":
                handle_capture_command(r, command, logger, persistent_cam=persistent_cam)
            elif action == "shutdown":
                logger.info("🛑 收到 shutdown 命令，退出")
                break
            else:
                logger.warning(f"⚠️ 未知 action: {action}")

        except redis.exceptions.ConnectionError:
            logger.error("❌ Redis 断连，5秒后重试...")
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("🛑 用户中断，退出")
            break
        except Exception as e:
            logger.exception(f"❌ 未预期异常: {e}")
            time.sleep(1)

    # 退出时释放摄像头
    if persistent_cam:
        persistent_cam.release()

    logger.info("🎬 Camera Worker 已退出")


if __name__ == "__main__":
    camera_worker_main()
