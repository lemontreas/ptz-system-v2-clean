# AI 任务交接：摄像头接口开发

## 项目路径

- **Windows 编辑路径**：`d:\Workspace\10_项目集\02_202510_wifi信号定位设备\02_程序脚本\后端\v2.2\`
- **Linux 运行路径**：`~/ptz_capture_system/`

---

## 已实现：对外 HTTP 接口（2 个）

与早期设想不同：**不提供** `GET/POST /api/v1/ptz/limits`。  
业务扫描范围由**前端直接写 Redis** `gimbal:default_config`（与 `capture_worker` / `grid_utils` 同源）；后端只**读** Redis，无 Redis 或 hash 为空时**回退** `config.json` 里 **默认扫描范围**。

| 接口 | 说明 |
|------|------|
| `POST /api/v1/camera/capture` | 单拍 / 全景 |
| `POST /api/v1/camera/coordinate_convert` | 像素 ↔ 角度 |
| `GET /api/v1/camera/images/<filename>` | 取 `data/captures/` 下已保存的图片（Flask `send_from_directory`） |

相关文件：

| 文件 | 作用 |
|------|------|
| `web_server.py` | 上述路由、`pixel_to_absolute_ptz` / `absolute_ptz_to_pixel`、标定加载与拍照/全景流程 |
| `ptz_range_redis.py` | `hardware_limits_from_redis`（`ptz:limits`）、`business_scan_range_from_redis`（`gimbal:default_config` + 与硬件限位求交 + config 兜底） |
| `panorama_stitch_utils.py` | `compute_shot_grid`、`stitch_panorama`（与 `camera_calibration/panorama_test/step4_panorama_stitch.py` 一致） |
| `config.json` | `摄像头.标定文件` 指向 **`calibration.npz`**；`云台.默认扫描范围` 作 Redis 缺失时的兜底 |
| `config_loader.py` | `get_camera_config()` 含 `标定文件`；可用环境变量 **`CAMERA_CALIB_FILE`** 覆盖路径 |

---

## 接口1：拍照

`POST /api/v1/camera/capture`

### 单拍（body 为空 `{}` 或不传 `panorama`）

```
1. 读 Redis ptz:current_status → capture_pan、capture_tilt
2. HTTP GET config.json["摄像头"]["截图地址"] → JPEG raw
3. cv2.flip(img, -1)（倒装）
4. 加载 config「标定文件」→ calibration.npz：camera_matrix、dist_coeffs
5. getOptimalNewCameraMatrix + cv2.undistort（与 step3 一致，alpha=0）
6. 存 data/captures/snap_pan…_tilt…_时间戳.jpg
7. 返回 image_url、capture_pan、capture_tilt
```

返回示例：

```json
{
  "code": 0,
  "data": {
    "mode": "single",
    "image_url": "/api/v1/camera/images/snap_pan102.50_tilt-5.00_20260515_1700.jpg",
    "capture_pan": 102.5,
    "capture_tilt": -5.0
  }
}
```

### 全景（`body` 含 `"panorama": true`）

```
1. 业务扫描矩形：ptz_range_redis.business_scan_range_from_redis()
   - 优先 Redis hash gimbal:default_config（pan_range、tilt_range、pan_step、tilt_step）
   - 与 Redis 字符串 ptz:limits（PTZ Worker 写入的硬件限位）求交集
   - 无 gimbal 配置时回退 config「云台」默认扫描范围
2. 用首张去畸变图算 FOV，compute_shot_grid（重叠率默认 0.4，与 step4 一致）
3. 逐点：ptz:command_queue → move_absolute，轮询 ptz:current_status 到位（非 MOVING 等且误差 <1°），再等 2s（AE）
4. 每点：截图 → flip → undistort
5. stitch_panorama 拼接
6. 存 panorama_时间戳.jpg + panorama_时间戳_meta.json
```

`metadata` 中含实际使用的 `pan_range`、`tilt_range`、`canvas_size`、`range_source`（`redis_gimbal` / `config_default`）等。

> 全景耗时长，前端需 loading。

---

## 接口2：坐标双向转换

`POST /api/v1/camera/coordinate_convert`

内参：由 **npz 的 K、dist** 在 npz 内 **image_size** 分辨率下做 `getOptimalNewCameraMatrix(..., alpha=0)`，取 **new_K** 的 fx、fy、cx、cy（与 step3 去畸变后坐标系一致）。

- 仅传 **`pixels`**：批量像素 → 绝对 pan/tilt（`pixel_to_absolute_ptz`）。
- 仅传 **`angles`**：批量角度 → 像素 + `in_frame`（`absolute_ptz_to_pixel`）。
- 都传或都不传 → 400。

须携带 **`capture_pan` / `capture_tilt`**（与接口1返回一致）。

---

## ~~接口3：PTZ 限位 HTTP~~（已取消）

不实现 **`GET/POST /api/v1/ptz/limits`**。改范围请前端写 **`gimbal:default_config`**；设备物理范围仍以 PTZ Worker 发布的 **`ptz:limits`** 为准。

---

## 关键函数位置

| 函数 | 位置 |
|------|------|
| `pixel_to_absolute_ptz` | `web_server.py` 模块级（逻辑同 `step3_mapping_verify.py`） |
| `absolute_ptz_to_pixel` | `web_server.py` 模块级（step3 逆运算） |
| `business_scan_range_from_redis` | `ptz_range_redis.py` |
| `compute_shot_grid` / `stitch_panorama` | `panorama_stitch_utils.py` |

### absolute_ptz_to_pixel（与实现一致）

```python
import math

def absolute_ptz_to_pixel(pan_target_deg, tilt_target_deg,
                           pan0_deg, tilt0_deg,
                           cx, cy, fx, fy,
                           img_w=1920, img_h=1080):
    """绝对云台角度 → 去畸变图像素坐标。Returns: (px, py, in_frame)"""
    P_t, T_t = math.radians(pan_target_deg), math.radians(tilt_target_deg)
    P0, T0   = math.radians(pan0_deg),       math.radians(tilt0_deg)

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
    in_frame = (0 <= px < img_w and 0 <= py < img_h)
    return px, py, in_frame
```

---

## 标定文件

- **路径**：`config.json` → `"摄像头"."标定文件"`，默认 **`calib/imx334_85fov/calibration.npz`**（与设备目录一致，含已 patch 的 K 与畸变系数）。
- **读取**：`np.load` → `camera_matrix`、`dist_coeffs`；若有 `image_size` 用于坐标换算分辨率。
- 去畸变与 step3 相同，**不要**用 json 里若存在的独立 fx/fy 覆盖 npz 内矩阵做 undistort（以 npz 为准）。

---

## config.json 摄像头示例

```json
"摄像头": {
    "截图地址": "http://127.0.0.1:8080/?action=snapshot",
    "视频流地址": "http://127.0.0.1:8080/?action=stream",
    "标定文件": "calib/imx334_85fov/calibration.npz"
}
```

---

## 图片存储

- 目录：`v2.2/data/captures/`（启动时自动创建）
- 单拍：`snap_pan….jpg`
- 全景：`panorama_时间戳.jpg`、`panorama_时间戳_meta.json`
- 静态路由：`GET /api/v1/camera/images/<filename>`（Flask，非 Bottle）

---

## 其它已实现联动（非 camera 路径）

- **`POST /api/v1/ptz/auto_scan`**：未传 `pan_range` / `tilt_range`（及默认步长）时，默认来自 **`business_scan_range_from_redis`**。
- **定位扫描估算点数**：扩边裁剪使用 **`hardware_limits_from_redis`**（`ptz:limits`），而非写死仅 config 限位。

---

## 代码风格

- 成功 `ok()`，失败 `err(..., http_status=..., app_code=...)`
- Redis 连接在 `create_app()` 内 `r`

---

## 坐标系约定

- 像素均针对**去畸变图**。
- 接口1返回的即去畸变图；接口2 的 `pixels` / 结果与之对应。
- `capture_pan` / `capture_tilt` 为**该次照片对应**的 PTZ 角度，接口2原样带回。
