# 摄像头接口文档

## 说明（请先读）

- **业务扫描范围**（全景用哪块 pan/tilt 矩形）：由**前端在 Redis** 写入 hash **`gimbal:default_config`**（字段 `pan_range`、`tilt_range` 为 JSON 字符串；可选 `pan_step`、`tilt_step`），与抓包/网格模块约定一致。**后端不提供**「改范围」的 HTTP 接口。
- 后端读不到有效配置时，**回退**服务端 `config.json` 里 **`云台 → 默认扫描范围`**。
- 全景实际使用范围会与 **设备硬件限位**（PTZ Worker 写入 Redis 的 **`ptz:limits`**）**求交集**，避免超出云台。
- 标定为 **`calibration.npz`**（路径见配置 `摄像头.标定文件`）；返回给前端的图为 **去畸变图**。

---

## 接口1：拍照

`POST /api/v1/camera/capture`

### 单拍（默认）

**请求 body：**

```json
{}
```

或不带 `panorama` 字段即可。

**返回：**

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

### 全景拼图

**请求 body：**

```json
{
  "panorama": true
}
```

全景使用的 **pan/tilt 范围与步长** 来自上文 **Redis `gimbal:default_config`（经硬件限位裁剪后的结果）**；**不需要**在 body 里再传 `pan_range` / `tilt_range`。

**返回示例：**

```json
{
  "code": 0,
  "data": {
    "mode": "panorama",
    "image_url": "/api/v1/camera/images/panorama_20260515_1701.jpg",
    "metadata": {
      "pan_range": [60.0, 240.0],
      "tilt_range": [-20.0, 10.0],
      "canvas_size": [3212, 540],
      "range_source": "redis_gimbal",
      "shot_count": 12,
      "overlap": 0.4
    }
  }
}
```

`range_source` 可能为 `redis_gimbal`（走了 Redis 配置）或 `config_default`（未配置 Redis 时回退 `config.json` 默认扫描范围）。

**全景图命名规则**：
```
panorama_pan{min}-{max}_tilt{min}-{max}_{YYYYMMDD_HHMM}.jpg
```
示例：`panorama_pan90.79-209.21_tilt-20.00-10.00_20260516_1411.jpg`

文件名自带角度范围，前端无需查 metadata 即可知覆盖范围；即使 Redis 数据丢失，文件名也能自解释。

同目录会生成 **`panorama_pan{...}_tilt{...}_{时间戳}_meta.json`** 供后端/排查使用。

> 全景耗时约 **30～60 秒**，前端需 **loading**；是否改为异步 + 任务轮询可后续再定。

**取图静态地址：** `GET /api/v1/camera/images/<文件名>`（与 `image_url` 路径一致）

---

## 接口2：坐标双向转换

`POST /api/v1/camera/coordinate_convert`

支持两种模式：**单张图模式**和**全景图模式**。

---

### 模式A：单张去畸变图（默认）

像素基于 **去畸变图** 坐标系；内参来自标定 **npz**，与接口1 单拍输出一致。

**原理**：球面投影模型（考虑 fx/fy/cx/cy），适用于单张透视投影图像。

#### 像素 → 云台角度（批量）

**请求 body：**

```json
{
  "capture_pan": 102.5,
  "capture_tilt": -5.0,
  "pixels": [[800, 400], [600, 300], [1100, 550]]
}
```

**返回：**

```json
{
  "code": 0,
  "data": {
    "angles": [
      {"pan": 115.3, "tilt": -3.2},
      {"pan": 110.1, "tilt": -4.5},
      {"pan": 122.8, "tilt": -2.8}
    ]
  }
}
```

#### 云台角度 → 像素（批量）

**请求 body：**

```json
{
  "capture_pan": 102.5,
  "capture_tilt": -5.0,
  "angles": [
    {"pan": 115.3, "tilt": -3.2},
    {"pan": 110.1, "tilt": -4.5}
  ]
}
```

**返回：**

```json
{
  "code": 0,
  "data": {
    "pixels": [
      {"px": 823, "py": 412, "in_frame": true},
      {"px": 598, "py": 287, "in_frame": true}
    ]
  }
}
```

**规则：**
- 推荐带 **`capture_pan` / `capture_tilt`**（与接口1 单拍返回一致）。
- 历史单张图也可只传 **`image_url`**，后端会从 `snap_pan{pan}_tilt{tilt}_...jpg` 文件名解析拍摄角度。

---

### 模式B：全景图

像素基于 **全景图** 坐标系（等距矩形投影 equirectangular）。

**原理**：Hugin 全景优先使用该图对应的 `pixel_map.pmap` 做权威映射。像素转角度直接查 PMAP；角度转像素会把角度投影到各来源图 source 像素，再通过后端进程内 PMAP owner/source 分桶索引查找最近的全景像素。没有 PMAP 时才回退到 legacy `coordinate_map.npz` / 线性兜底。

**后端获取全景图参数**：优先使用请求里的 `image_url` 读取该全景图对应的 `_meta.json`；未传 `image_url` 时兼容旧行为，从 Redis `camera:last_panorama_meta` 读取最新一次全景元数据。

#### 像素 → 云台角度（批量）

**请求 body：**

```json
{
  "mode": "panorama",
  "image_url": "/api/v1/camera/images/panorama_hugin_xxx.jpg",
  "pixels": [[1000, 260], [1500, 300]]
}
```

**返回：**

```json
{
  "code": 0,
  "data": {
    "angles": [
      {"pan": 132.5, "tilt": -3.2},
      {"pan": 155.8, "tilt": -2.1}
    ],
    "mapping": "pmap"
  }
}
```

#### 云台角度 → 像素（批量）

**请求 body：**

```json
{
  "mode": "panorama",
  "image_url": "/api/v1/camera/images/panorama_hugin_xxx.jpg",
  "angles": [
    {"pan": 132.5, "tilt": -3.2},
    {"pan": 155.8, "tilt": -2.1}
  ]
}
```

**返回：**

```json
{
  "code": 0,
  "data": {
    "pixels": [
      {"px": 1000, "py": 260, "in_frame": true},
      {"px": 1500, "py": 300, "in_frame": true}
    ],
    "mapping": "pmap"
  }
}
```

**Redis 存储结构** `camera:last_panorama_meta`：

```json
{
  "pan_range": [90.79, 209.21],
  "tilt_range": [-20.0, 10.0],
  "canvas_size": [2842, 720]
}
```

**legacy 线性兜底公式**（仅在没有 Hugin PMAP / legacy 映射文件时使用）：
```
pan  = pan_min  + (x / (width  - 1)) × (pan_max  - pan_min)
tilt = tilt_max - (y / (height - 1)) × (tilt_max - tilt_min)
```

**规则：**
- 不需要传 `capture_pan` / `capture_tilt`。
- 历史页查看某一张全景图时应传 `image_url`，后端会使用该图旁边的 `_meta.json`，避免误用最新全景图参数。
- 不传 `image_url` 时，后端自动读取最新一次全景拍摄的 metadata（兼容旧前端）。
- `in_frame: false` 表示该角度超出全景图覆盖范围。

---

### 通用规则

- **仅**能二选一：只传 `pixels` **或** 只传 `angles`；二者都传或都不传 → **400**。
- `mode` 字段可选：不传或 `"single"` = 单张图模式，`"panorama"` = 全景图模式。
- `image_url` 字段可选：格式为 `/api/v1/camera/images/<文件名>`，也兼容只传 `<文件名>`；历史页建议始终传当前正在查看的图片 `image_url`。

### 常见错误

| HTTP | code | 场景 |
| --- | --- | --- |
| 400 | 1001 | `pixels` / `angles` 未二选一，或点位格式错误 |
| 400 | 1002 | `image_url` 格式错误 |
| 400 | 1003 | 单张图未传 `capture_pan/capture_tilt`，且 `image_url` 文件名无法解析角度 |
| 404 | 2001 | 全景图元数据不存在：未传 `image_url` 且 Redis 中没有最新全景元数据，或指定全景图缺少 `_meta.json` |
| 404 | 2002 | Hugin 全景映射文件不存在，例如 PMAP 路径或 `session.json` 缺失 |
| 404 | 2004 | `image_url` 指定的图片不存在 |
| 500 | 5004 | 全景图元数据格式错误 |
| 500 | 5005 | 全景图尺寸无效 |

---

## 坐标系约定

### 单张图
- 所有像素坐标基于 **去畸变图**。
- 接口1 单拍返回的图片即为去畸变图，在其上取点再调接口2。
- `capture_pan` / `capture_tilt` 为 **该次拍摄对应时刻**的云台角度：接口1 返回什么，接口2 就原样带回。

### 全景图
- 像素坐标基于 **全景拼接图**（等距矩形投影）。
- 全景图坐标系：左上角 = `(pan_min, tilt_max)`，x 轴向右 = pan 增大，y 轴向下 = tilt 减小。
- 像素与角度是**线性关系**，由 `pan_range`、`tilt_range`、`canvas_size` 三个参数决定。
