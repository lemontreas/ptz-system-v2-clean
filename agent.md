# AI Developer Agent Guide (`agent.md`)

你好，AI 助手！当你开始在这个项目中工作时，请**务必仔细阅读并遵循**本指南。本项目有着非常清晰的架构、严格的状态管理规范和特定的 AI 协作流程。

---

## 0. 本文件功能

`agent.md` 是后续 AI / Codex / Cursor 接手本项目时的**会话启动入口**：用于快速对齐项目运行环境、核心阅读顺序、状态管理硬规则、常用代码入口和高频调试命令。后续 AI 必须先读本文件，再决定是否继续深入 `idea/`、`rule/`、`plan/` 或源码。

本文件不是完整接口文档，也不替代 `idea/前端接口.md`；它只沉淀最常被拿来现场测试的命令和最容易忘记的项目边界，避免每次为了一个 curl 指令重新搜索代码。

---

## 1. 核心阅读入口 (修改代码前必读)

为了避免全局扫描盲目猜测，在修改或分析任何代码前，请先按顺序查阅以下文件：

1. **当前进度与任务**: [`idea/STATUS.md`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/idea/STATUS.md)
   * 记录当前版本的最新进度、已完成功能、目前停留位置和下一步待办。新会话开始时必须首先阅读；当本次工作改变了项目进度、已完成能力、当前卡点或下一步时，结束前必须同步更新。纯只读检查、解释或没有改变项目状态的会话不要制造空更新。
2. **AI 修改入口指南**: [`rule/AI修改入口指南.md`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/rule/AI修改入口指南.md)
   * 这是你的“行动指南”，包含如何定位代码、不要做的事、审核清单以及 Git 保存提交规则。
3. **状态管理规则**: [`rule/状态管理规则.md`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/rule/状态管理规则.md)
   * 包含云台运动状态、全面扫描、定位扫描及抓包的生命周期转换。凡涉及状态字段变更、停止（stop）操作及 `scan_id` 校验，必须符合此规则。
4. **项目架构文档**: [`idea/ARCHITECTURE.md`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/idea/ARCHITECTURE.md)
   * 系统的整体架构、进程划分、Redis 总线通信的详细定义。

---

## 2. 项目架构概览

本项目是一个基于 **多进程 + Redis 总线** 的 WiFi 信号定位设备后端程序（Python 3）。

### 2.0 设备端运行环境事实（后续 AI 必读）

设备端与 Windows 修改机路径不同。后续 AI 给命令时必须优先使用以下已确认信息，不要继续沿用过期路径：

* **设备登录用户**：`ultiwill`。设备 IP 可能随网络环境变化，不得把历史地址写死
  为当前设备地址；执行远程命令或从局域网调用接口前，必须先向用户确认或通过
  当前现场配置发现设备 IP。
* **当前已知设备 IP（2026-07-02）**：
  - `192.168.8.239`：本地局域网主要设备 IP
  - `100.64.0.54`：远程时前端页面 + 后端调试设备
  - `100.64.0.52`：远程时测试设备
* **设备 eth0 MAC 地址**：`88:a2:9e:2e:38:39`（用于 DHCP 环境下通过 MAC 查找设备 IP）
* **设备端 Python 虚拟环境解释器**：`/home/ultiwill/ptz_capture_system/ptz_env/bin/python`。
* **项目服务实际启动命令**：

```bash
sudo /home/ultiwill/ptz_capture_system/ptz_env/bin/python run_manager.py
```

* **与项目相关的 Python 脚本必须优先用该虚拟环境解释器运行**。如果缺少依赖，也应安装到该虚拟环境，不要默认使用系统 `python` / `python3`。
* **不要默认使用历史文档中的 `/home/ultiwill/ptz_capture_system/v2.2`**。用户已确认设备端不存在该 `v2.2` 目录；设备端实际后端代码目录需按现场确认。
* **图片/全景 session 保存根目录**：`/home/ultiwill/ptz_capture_system/data/captures/`。
* **PMAP 已是稳定正式能力，不是当前待排查问题**。Hugin 全景生成的
  `pixel_map.pmap` 是像素与云台角度双向转换的首选权威映射。不要因为历史
  session、旧问题记录或 NPZ 兼容产物而默认重新开展 PMAP 正确性排查；只有
  当前任务提供了新的可复现异常证据时，才进入 PMAP/PTO 诊断。

### 2.0.1 常用后端接口 curl 速查

这些命令用于模拟前端向 Flask 后端下发 HTTP 指令。默认服务运行在设备端，若在设备本机执行可用 `127.0.0.1:5000`；若从同一局域网的其他机器执行，必须先确认当前设备 IP。不要沿用历史 IP，也不要把这些 HTTP 命令误当成 Redis 内部队列命令。

```bash
BASE=http://127.0.0.1:5000
# LAN 调试时可改为：
# BASE=http://<当前设备IP>:5000
```

#### 状态查询与云台移动

```bash
# 查询 PTZ / 扫描 / 抓包聚合状态
curl -sS "$BASE/api/v1/ptz/status" | python3 -m json.tool

# 移动云台到指定位置
curl -sS -X POST "$BASE/api/v1/ptz/move" \
  -H "Content-Type: application/json" \
  -d '{
    "pan": 120.0,
    "tilt": -10.0,
    "pan_step_size": 20.0,
    "tilt_step_size": 10.0
  }' | python3 -m json.tool

# 紧急停止云台当前动作
curl -sS -X POST "$BASE/api/v1/ptz/stop" | python3 -m json.tool
```

#### 摄像头拍照与全景拼接

```bash
# 普通拍照并保存图片，返回 image_url
curl -sS -X POST "$BASE/api/v1/camera/capture" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -m json.tool

# 开启全景拍摄 + Hugin 拼接 + 映射产物生成
# 全景范围来自 Redis gimbal:default_config，经硬件限位裁剪；body 不传 pan_range/tilt_range。
curl -sS -X POST "$BASE/api/v1/camera/capture" \
  -H "Content-Type: application/json" \
  -d '{"panorama": true}' | python3 -m json.tool

# 全景图像素转云台角度；image_url 使用全景接口返回值
curl -sS -X POST "$BASE/api/v1/camera/coordinate_convert" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "panorama",
    "image_url": "/api/v1/camera/images/panorama_hugin_xxx.jpg",
    "pixels": [[1000, 260], [1500, 300]]
  }' | python3 -m json.tool
```

#### 全面扫描

```bash
# 启动一轮全面扫描；不传 scan_time_limit/time_interval 时完整执行一轮后结束
# 全面扫描使用任务图片上的像素坐标；mode 与 target_ranges 必填。
curl -sS -X POST "$BASE/api/v1/ptz/full_area_scan/start" \
  -H "Content-Type: application/json" \
  -d '{
    "project_name": "manual_full_area_test",
    "mode": "panorama",
    "target_ranges": [
      {"x_range": [1200, 2200], "y_range": [180, 620]}
    ]
  }' | python3 -m json.tool

# 启动带时间窗口的全面扫描：scan_time_limit 单位分钟，time_interval 单位秒
curl -sS -X POST "$BASE/api/v1/ptz/full_area_scan/start" \
  -H "Content-Type: application/json" \
  -d '{
    "project_name": "manual_full_area_window",
    "mode": "panorama",
    "target_ranges": [
      {"x_range": [1200, 2200], "y_range": [180, 620]}
    ],
    "scan_time_limit": 10,
    "time_interval": 60
  }' | python3 -m json.tool

# 停止全面扫描。返回 queued 只表示停止请求已接收；继续轮询 /api/v1/ptz/status 看 full_scan.terminal。
curl -sS -X POST "$BASE/api/v1/ptz/full_area_scan/stop" | python3 -m json.tool

# 获取最新全面扫描结果 / 白名单
curl -sS "$BASE/api/v1/ptz/full_area_scan/result" | python3 -m json.tool
curl -sS "$BASE/api/v1/ptz/full_area_scan/whitelist" | python3 -m json.tool
```

全面扫描的外层像素范围不由请求体传入，而是读取 Redis `gimbal:default_config`
中的 `work_x_range` / `work_y_range`。旧 `work_ranges`、`pan_range`、`tilt_range`
角度字段不再参与全面扫描路径生成或耗时估算。定位扫描、直接 PTZ 移动和
`capture_at_best` 目前仍使用云台角度参数，不要把所有接口一概改成像素。

#### 定位扫描

```bash
# 启动定位扫描：后端自动信道探测，target_macs 与 pan_ranges/tilt_ranges 必填
curl -sS -X POST "$BASE/api/v1/ptz/location_scan/start" \
  -H "Content-Type: application/json" \
  -d '{
    "project_name": "manual_location_test",
    "target_macs": ["aa:bb:cc:dd:ee:ff"],
    "pan_ranges": [[90, 180]],
    "tilt_ranges": [[-20, 20]],
    "dwell_time": 1.0,
    "probe_dwell_time": 1.0,
    "probe_rounds_max": 2
  }' | python3 -m json.tool

# 已知信道/带宽时跳过自动探测
curl -sS -X POST "$BASE/api/v1/ptz/location_scan/start" \
  -H "Content-Type: application/json" \
  -d '{
    "project_name": "manual_location_fixed_channel",
    "target_macs": ["aa:bb:cc:dd:ee:ff"],
    "pan_ranges": [[90, 180]],
    "tilt_ranges": [[-20, 20]],
    "channel": 157,
    "bandwidth": "HT20"
  }' | python3 -m json.tool

# 查询定位扫描状态 / 结果
curl -sS "$BASE/api/v1/ptz/location_scan/status" | python3 -m json.tool
curl -sS "$BASE/api/v1/ptz/location_scan/result" | python3 -m json.tool

# 停止定位扫描。返回 queued 只表示停止请求已接收；继续轮询 status 看 location_scan.terminal。
curl -sS -X POST "$BASE/api/v1/ptz/location_scan/stop" | python3 -m json.tool
```

#### 指定点位抓包与独立抓包

```bash
# 指定点位抓包：当前代码要求前端显式传 pan/tilt/mac/channel/bandwidth
# 不传 capture_time_limit 时会持续抓包，需再调用 /api/v1/capture/stop。
curl -sS -X POST "$BASE/api/v1/ptz/capture_at_best" \
  -H "Content-Type: application/json" \
  -d '{
    "pan": 120.0,
    "tilt": -10.0,
    "mac": "aa:bb:cc:dd:ee:ff",
    "channel": 157,
    "bandwidth": "HT20",
    "capture_time_limit": 30,
    "pcap_filename": "manual_target_capture.pcap"
  }' | python3 -m json.tool

# 独立抓包启动
curl -sS -X POST "$BASE/api/v1/capture/start" \
  -H "Content-Type: application/json" \
  -d '{
    "channel": 157,
    "bandwidth": "HT20",
    "target_mac": "aa:bb:cc:dd:ee:ff"
  }' | python3 -m json.tool

# 停止独立抓包，timeout 单位秒
curl -sS -X POST "$BASE/api/v1/capture/stop?timeout=15" | python3 -m json.tool

# 保存 pcap；当前后端字段名是 pcap_filename
curl -sS -X POST "$BASE/api/v1/capture/save_pcap" \
  -H "Content-Type: application/json" \
  -d '{
    "channel": 157,
    "bandwidth": "HT20",
    "target_mac": "aa:bb:cc:dd:ee:ff",
    "pcap_filename": "manual_capture.pcap"
  }' | python3 -m json.tool
```

#### WiFi 连接尝试

```bash
# 查询 WiFi 连接尝试状态
curl -sS "$BASE/api/v1/wifi/connect/status" | python3 -m json.tool

# 取消 WiFi 连接尝试
curl -sS -X POST "$BASE/api/v1/wifi/connect/stop" | python3 -m json.tool
```

注意：全面扫描和定位扫描的 stop 接口遵守两阶段停止协议，HTTP 返回成功只代表“停止请求已收到”，不是 worker 已完全退出。前端和 AI 调试都必须继续轮询 `GET /api/v1/ptz/status`，看 `full_scan.terminal` / `location_scan.terminal` 和 `capture.running`。

### 2.1 进程与通信模型

由 [`run_manager.py`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/run_manager.py) 启动和管理以下 3 个常驻核心独立进程：

```mermaid
graph TD
    run_manager.py[run_manager.py 主进程] --> web_server.py
    run_manager.py --> ptz_control.py
    run_manager.py --> capture_worker.py

    web_server.py[web_server.py Flask API] <-->|Redis: command_queue & status| ptz_control.py[ptz_control.py 云台控制进程]
    web_server.py <-->|Redis: command_queue & data_stream| capture_worker.py[capture_worker.py 嗅探抓包进程]
    ptz_control.py <-->|Redis 状态/扫描协调| capture_worker.py

    web_server.py -->|同步拍照/拼接请求| hugin_panorama_runtime.py[hugin_panorama_runtime.py Hugin全景运行时]
    hugin_panorama_runtime.py -->|生成/读取 pixel_map.pmap| pmap_utils.py[pmap_utils.py PMAP读写工具]
    hugin_panorama_runtime.py -->|PMAP 像素与角度双向转换| coordinate_convert[/api/v1/camera/coordinate_convert]
    hugin_panorama_runtime.py -->|兼容产物| legacy_maps[coordinate_map.npz + correction_map.npz]
    hugin_panorama_runtime.py -->|调用| nona[nona-pixelmap --pixel-map]
```

* **禁止跨进程 import 彼此的模块**。所有进程间的通信、状态同步和任务派发必须通过 **Redis** 总线进行。
* `camera_worker.py` 目前是实验/独立 worker 文件，未由 `run_manager.py` 常驻拉起；当前全景拍照与 Hugin 拼接主路径在 `web_server.py` 请求处理中同步执行。
* Hugin 全景坐标双向转换的对外接口仍是 `/api/v1/camera/coordinate_convert`。当 `pixel_map.pmap` 可用时，像素转角度与角度转像素都以 PMAP 为首选权威映射；`coordinate_map.npz` 与 `correction_map.npz` 仅作为兼容回退或诊断产物。不要把 NPZ、旧 CSV/SQLite 当作与 PMAP 同等权威的新链路依据。

### 2.2 核心文件职责

* [`web_server.py`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/web_server.py): Flask REST API 与请求编排层。它不直接执行底层云台旋转或嗅探，但负责参数校验、全面扫描图像上下文锁定、像素范围与计划/耗时估算、PMAP 坐标转换、同步拍照/全景流程，以及通过 Redis 派发任务和聚合状态。
* [`ptz_control.py`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/ptz_control.py): 云台运动核心控制，包括全面扫描（Full Scan）、定位扫描（Location Scan）的算法逻辑与状态机维护。
* [`capture_worker.py`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/capture_worker.py): WiFi 抓包与 RSSI 嗅探工作器，通过 Scapy 捕获数据并解析，写入 Redis 数据流。
* [`hugin_panorama_runtime.py`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/hugin_panorama_runtime.py): Hugin 全景拼接运行时，负责裁剪输入图、生成 PTO、调用 Hugin/nona，并通过改造版 `nona-pixelmap --pixel-map` 生成权威映射 `pixel_map.pmap`；`coordinate_map.npz` / `correction_map.npz` 保留为兼容回退与诊断产物。
* [`pmap_utils.py`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/pmap_utils.py): `.pmap` / `.pcand` 二进制格式读写工具。`.pmap` header 为 `PMAP0001`，planar 数组为 `owner:int16`、`source_x:int16`、`source_y:int16`、`coverage:uint16`。
* [`hugin/hugin_pto_math_mapper.py`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/hugin/hugin_pto_math_mapper.py): Hugin/PTO/PMAP 诊断工具。默认优先使用 `.pmap`；CSV/SQLite 只保留为历史旧产物诊断模式。
* [`grid_utils.py`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/grid_utils.py): 网格索引与空间数据插值计算。
* [`pelco_d_controller.py`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/pelco_d_controller.py): Pelco-D 云台串口协议封装。
* [`antenna_bias_utils.py`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/antenna_bias_utils.py): 天线偏差补偿（RF 中心点与摄像头画面中心像素点的转换）。

---

## 3. 开发与修改硬性约束

1. **绝对禁止全量覆盖 Redis 状态**:
   状态更新时，不能直接用全量 `json.dumps()` 覆盖 `ptz:current_status`，否则会使其他模块写入的子项（如 `full_scan`, `location_scan`）丢失。必须使用局部更新/PATCH 机制。
   Web stop 与 PTZ worker 可能并发写状态，局部 PATCH 本身也必须通过 Redis `WATCH/MULTI/EXEC` 原子重试；不能使用无锁的“读 JSON -> 合并 -> 写 JSON”，否则停止状态可能被 worker 的旧快照覆盖。校准和移动进度也必须走同一个原子 PATCH 入口。
2. **两阶段停止协议 (Stopping -> Stopped)**:
   当接收到 `stop` 命令时，Web API 立即向前端返回并标记状态为 `stopping`，而实际 Worker 停止并清理完毕后，再将状态标记为 `stopped` / `done` / `error`，并将 `terminal` 设为 `true`。
   正常完成时，Worker 必须在最终轮次结果写入 Redis 后立即用该权威结果收口终态与计数；测试摘要、SQLite 历史快照等非关键后处理不得阻塞或破坏终态发布。
3. **带 `scan_id` 的任务校验**:
   任何耗时扫描和嗅探都使用唯一的 `scan_id`。派发的抓包任务、移动任务和停止命令必须校验 `active_scan_id`。防止已取消的旧任务包干扰新任务。
4. **循环退出检查**:
   所有云台运动循环、点位移动循环、嗅探循环内部必须定期检查 stop 信号和 `active_scan_id`，确保任务能被秒级响应并安全终止。
   全面扫描 dwell/sniff 的 stop 轮询切片不得超过 0.1 秒。stop key 只能由新任务启动事务换代；任何读取者或已接手的 worker 都不得因 `scan_id` 不匹配而删除 stop。首次观察到停止原因后必须锁存到当前任务内存，避免 key 过期或异常消失后终态退化。
   PTZ 等待全面扫描点位完成通知时不得使用1秒阻塞读取；应以不超过50ms的非阻塞轮询交替检查 stop，确保正常 worker 收口先于HTTP 2秒自愈兜底。
5. **`.pmap` 二进制映射链路约束**:
   当前 v2.2 的 Hugin 像素映射正式路线是 Linux 改造版 `nona-pixelmap --pixel-map` 生成 `pixel_map.pmap`，Python 侧用 `pmap_utils.py` 读取 header/planar 数组。Hugin 全景的像素转角度与角度转像素均应优先使用 PMAP；`coordinate_map.npz` / `correction_map.npz` 只作兼容回退或诊断，旧 `nona_pixel_map.csv` / SQLite 只能作为历史诊断兼容，不能新增生产依赖。
6. **边界补拍状态必须如实标注**:
   `left_boundary` / `right_boundary` / `stitch_role` / 非对称裁剪仍属于待评审的边界优化方案。当前主代码使用中心裁剪，并稳定生成和使用 `.pmap`；不要把边界补拍写成已接入生产主流程，也不要把稳定的 PMAP 链路写成边界补拍方案的待验证部分。
7. **运行环境与命令边界**:
   代码修改通常在 Windows 工作区完成，但系统主要运行在 Linux 设备上，Redis、Hugin/nona、网卡、摄像头和云台硬件状态以 Linux 设备为准。给提示命令或执行 AI 操作时，不要默认用本机 Windows 命令读取 Redis（例如本机 `redis-cli` / localhost 探测），也不要把本机运行结果当成设备端状态。需要查 Redis 或运行系统命令时，应明确切到 Linux 设备环境或让用户在设备端执行。
8. **全面扫描像素坐标与筛选术语边界**:
   全面扫描使用锁定任务图片的像素坐标：请求中的 `target_ranges` 是用户画的**真实目标区**，Redis `gimbal:default_config.work_x_range/work_y_range` 是外层/粗扫像素范围，结果中的 `pixel_position` 是前端展示和证据空间归属的权威逻辑位置。云台 `pan/tilt` 与 RF 补偿位置只用于实际执行或调试，不得线性映射回图片代替 `pixel_position`。白名单筛选会基于本轮 `antenna_bias` 计算**白名单判定缓冲区**，它只用于筛选证据归类，不改变扫描路径、前端显示范围或原始像素坐标。后续修改白名单时必须区分 `真实目标区命中`、`白名单缓冲区命中` 和 `缓冲区外强点`。
9. **代码修改 Git 规范**:
   * 开始前通过 `git status --short` 确认基线。
   * 坚持**小闭环**原则：代码修改 -> 验证（如 `py_compile`、测试）-> 局部提交。
   * 纯文档修改可累计后提交。
   * 严禁直接使用全局 `git add .`。
10. **代码事实与文档必须同一闭环更新**:
   * 修改 HTTP 请求/响应字段、必填项或语义时，同步更新 `agent.md` 中的 curl 速查和对应 `idea/` 接口文档。
   * 修改全面扫描坐标空间、范围来源、权威展示位置或映射链路时，同步更新 `CONTEXT.md`、`agent.md` 与相关架构/前端文档。
   * 修改进程职责、Redis 通信关系或正式运行产物时，同步更新 `idea/ARCHITECTURE.md` 和 `agent.md`。
   * 修改扫描、抓包或 stop 生命周期时，同步更新 `rule/状态管理规则.md`、`agent.md` 及相关状态流测试。
   * 当改动改变项目进度、当前焦点、卡点或下一步时，同步更新 `idea/STATUS.md`；仅重述既有事实时不更新。
   * 完成修改前必须按本次变更类型检查上述文档，不能只因为代码测试通过就结束任务。若文档与代码冲突，以当前已验证代码为事实源，并在同一任务中修正文档。

### 3.1 全面扫描白名单判定契约

以下内容只描述当前稳定判定边界，不复制实现代码。实际实现入口为
`ptz_control._build_full_scan_whitelist_payload()`；具体阈值必须读取
`config.json["全面扫描筛选"]`，不得把本节示例或历史文档中的数值另写成第二套默认值。

**参与判定的证据：**

* 只在全面扫描轮次状态为 `SUCCESS` 后生成该轮白名单。
* 只使用采样完整的正式定点证据；移动路径证据、部分采样、缺少位置/信道/RSSI
  的记录、扫描 marker 与其他 synthetic 记录不得参与。
* 同一 MAC 的证据必须先按实际观测到的 `(channel, bandwidth)` 隔离，再为该
  MAC 选择唯一判定配置；禁止跨配置拼接命中点或 RSSI。
* AP 优先使用实际观测到的 Beacon 宣告配置；宣告配置未实际观测到时不得用
  其他配置冒充。非宣告 AP、Client 关系配置和实际最强配置的选择顺序以当前
  实现及相关测试为准。
* 同一配置、同一坐标桶内重复观察只算一个独立命中点，并保留该桶内最强的
  定向 RSSI。历史字段 `rssi_avg` 在此链路中承载定向峰值，不得解释为算术平均值。

**当前淘汰条件：**

* 整轮独立命中、真实目标区命中或白名单判定缓冲区命中未达到配置阈值。
* 最强点与 RSSI 加权中心都不在白名单判定缓冲区，导致目标区位置证据不足。
* 缓冲区内最强 RSSI 相对缓冲区外最强 RSSI 的优势低于配置阈值。
* 与缓冲区内最强 RSSI 足够接近的外部强点同时分布在代码定义的对角象限，
  触发“缓冲区外强点分散”。当前实现只检查 `左上+右下` 或 `右上+左下`，
  不得把相邻象限误写成已实现规则。
* 同时存在有效定向与全向证据时，定向相对全向的优势低于配置阈值；缺少有效
  全向证据时跳过此项，不能仅因缺失而淘汰。
* AP 的权威宣告配置存在但本轮没有对应实际观测证据。

所有启用的硬条件均通过后，MAC 才进入 `mac_whitelist`；失败项写入
`rejected_macs[].failed_reasons`、`reason_codes` 与 `metrics`。星链身份只是
`type="ap"`、`subtype="starlink"` 的设备分类，不是白名单通行证，仍须通过
同一空间证据筛选。

白名单成员可在默认关闭的尾阶段执行位置复核，但该阶段只改善展示位置，不重新
决定成员资格。复核线段保持一次连续移动；capture worker 在线段内存中保留单包
峰值及其时间，PTZ worker 复用到位轮询形成带时间轨迹。获胜峰值先按轨迹插值得到
角度，再只在原始像素线段的正向像素→角度候选中查找对应像素，不使用整段中点或
无约束全图角度→像素反查。成功更新时
`best_position_source="whitelist_refinement_peak_trajectory"`；映射失败时保留
初始固定点位置，复核不得增删白名单成员。

新增或修改白名单条件时，必须同步检查：

* `config.json["全面扫描筛选"]` 的配置项与默认值；
* `_full_scan_filter_config_for_output()` 的对外配置快照；
* `_REASON_CODE_MAP` / `_REASON_PREFIX_MAP` 与淘汰 metrics；
* 白名单单元测试、前端筛选说明、`CONTEXT.md` 和本节契约；
* 条件在证据不足时应当“淘汰、通过还是无法判断”，不得无意中把小范围或稀疏
  真实信号源一票否决。

---

## 4. 优化计划与问题记录规范

为了让后续 AI 和用户能快速判断“优化做到哪一步、问题卡在哪里”，凡涉及持续优化、方案拆解、问题排查或卡点定位，必须优先沉淀到 `plan/` 或 `question/` 目录。

所有 `plan/` 与 `question/` 文档中的更新时间、排查时间、进度记录时间统一使用 `YYYY-MM-DD HH:mm` 格式；默认时区为 `Asia/Shanghai`。

历史文档和历史索引条目不强制一次性补全；从新建文档、状态变化和后续更新开始，必须遵守本节的新格式。

### 4.1 优化计划文档 (`plan/`)

默认不要为一次性小改动创建 `plan/` 优化计划。只有用户明确说“做优化计划”、“记录到 plan”、“这是长期优化”，或任务明显需要跨多步、跨文件、跨会话推进时，才在 `plan/` 目录创建或更新对应的优化计划 Markdown。

如果 AI 判断某个优化会持续多轮、存在多个决策分支、需要分阶段验证，或后续很可能追加优化点，可以主动创建 `plan/` 计划；如果只是单点修复、简单文档改动或一次性小调整，不要新建计划。

新建优化计划时，必须优先复制 `plan/_template_优化计划.md`，再按任务内容填写；不要临场自由发挥格式。若模板不存在，再按下方推荐结构手动创建。

计划状态必须使用固定枚举：

```text
待整理 / 待评审 / 待执行 / 进行中 / 待验证 / 已完成 / 暂缓 / 取消
```

`待整理` 仅用于历史文档或信息不足的计划；新建计划应优先使用 `待评审`、`待执行` 或 `进行中`。`plan/README.md`、计划正文状态和相关 Todo 说明中的状态必须保持一致。

命名建议：

```text
plan/YYYY-MM-DD_简短主题_优化计划.md
```

若已有同主题文档，优先更新原文档，不要重复新建相近计划。

推荐结构：

```markdown
# 简短主题优化计划

## Todo

- [ ] 1. 待办项标题
- [ ] 2. 待办项标题
- [ ] 3. 待办项标题

## 详细说明

### 1. 待办项标题

- 目标：
- 当前状态：
- 涉及文件：
- 实施步骤：
- 验证方式：
- 风险与回退：
- 进度记录：

### 2. 待办项标题

- 目标：
- 当前状态：
- 涉及文件：
- 实施步骤：
- 验证方式：
- 风险与回退：
- 进度记录：
```

维护规则：

* 一次性小改动不要新建计划；用户明确要求或任务明显跨多步、跨文件、跨会话时才创建计划。
* 每完成一个 Todo，必须把 `- [ ]` 改为 `- [x]`，并在对应详细说明的“进度记录”中写明完成时间、关键结论和验证结果。
* 如果后续发现新优化点，只追加新的 Todo 和对应详细说明，不要把未完成事项散落在正文里。
* 每个优化计划最多 10 个 Todo；准备加入第 11 个优化点时，必须新建主题明确的计划并同步更新 `plan/README.md`，避免单计划过长增加阅读和上下文成本。
* 如果某个 Todo 被取消或暂缓，保留条目并标注原因，例如 `- [ ] 4. xxx（暂缓：原因）`。
* 计划文档用于承载“怎么做、做到哪一步、如何验证”；`idea/STATUS.md` 只保留当前焦点和下一步摘要，不替代详细计划。
* 新建计划、计划状态变化或关键 Todo 完成时，必须同步更新 `plan/README.md` 索引。
* 所有进度记录时间统一使用 `YYYY-MM-DD HH:mm`，默认时区 `Asia/Shanghai`。
* 历史计划不强制一次性补全；后续触碰、状态变化或新增记录时，必须顺手补齐到当前格式。

### 4.2 问题记录文档 (`question/`)

默认不要自动创建 `question/` 问题记录。只有用户明确说“这是 question”、“记录到 question”、“问题记录一下”，或 AI 询问后用户确认需要沉淀时，才在 `question/` 目录创建或更新问题记录 Markdown。

如果 AI 判断某个问题可能值得沉淀，应先询问用户是否记录到 `question/`，不要擅自创建大量问题文件。用户明确要求追踪问题状态、跨会话继续排查，或要求沉淀已解决问题的防回退结论时，也应创建或更新问题记录。

新建问题记录时，必须优先复制 `question/_template_问题记录.md`，再按问题现场填写；不要临场自由发挥格式。若模板不存在，再按下方推荐结构手动创建。

命名建议：

```text
question/YYYY-MM-DD_HHMM_问题简述---状态.md
```

时间统一使用 `YYYY-MM-DD HH:mm`，默认时区 `Asia/Shanghai`。

问题状态必须使用固定枚举：

```text
待排查 / 排查中 / 待验证 / 已解决 / 暂缓 / 无法复现
```

推荐结构：

```markdown
# YYYY-MM-DD 问题简述

## 问题概述

- 时间：
- 状态：
- 影响范围：
- 简单描述：

## 现场现象

- 触发条件：
- 期望结果：
- 实际结果：
- 相关命令 / 接口 / 日志：

## 排查记录

### YYYY-MM-DD HH:mm

- 操作：
- 观察：
- 结论：
- 下一步：

## 当前卡点

- 卡在哪里：
- 缺少什么信息：
- 推荐下一步：

## 最终结论

- 根因：
- 修复方式：
- 验证方式：
- 后续防回退措施：
```

维护规则：

* 只有用户明确标记或确认后，才创建新的问题记录。
* 新问题创建时，必须至少写清楚“问题简单描述、时间、状态、当前卡点”。
* 每次排查有新进展，都追加一条带时间的“排查记录”，不要覆盖历史判断。
* 问题状态变化时，同步更新文件名末尾状态、正文 `状态` 字段和 `question/README.md` 索引状态。
* 问题解决后，必须补充“最终结论”，包括根因、修复方式、验证方式和防回退措施。
* 如果问题与某个优化计划相关，必须在问题记录中链接对应 `plan/*.md`，并在计划文档对应 Todo 的“进度记录”中反向引用该问题。
* 新建问题、问题状态变化或关键排查结论更新时，必须同步更新 `question/README.md` 索引。
* 所有排查记录时间统一使用 `YYYY-MM-DD HH:mm`，默认时区 `Asia/Shanghai`。
* 历史问题不强制一次性补全；后续触碰、状态变化或新增记录时，必须顺手补齐到当前格式。

### 4.3 `plan/` 与 `question/` 双向引用

当某个问题阻塞、解释或验证某个优化计划时，必须建立双向引用：

* 在 `question/*.md` 的“关联计划”字段写入对应 `plan/*.md`。
* 在 `plan/*.md` 的对应 Todo “进度记录”中写入对应 `question/*.md`，说明它阻塞了哪一步或验证了哪一步。
* 在 `plan/README.md` 的“下一步 / 备注”中简要标注相关问题，例如 `阻塞：question/...`。
* 在 `question/README.md` 的“当前卡点 / 结论”中简要标注相关计划，例如 `关联：plan/...`。
* 问题解决后，必须回到对应计划 Todo，补充最终结论和下一步状态。

### 4.4 文档自检

修改 `agent.md`、`plan/README.md`、`question/README.md`、`plan/_template_优化计划.md` 或 `question/_template_问题记录.md` 后，不要求运行代码测试，但必须做一次文档自检：

* 确认 `plan/_template_优化计划.md` 和 `question/_template_问题记录.md` 存在。
* 确认 `plan/README.md` 和 `question/README.md` 存在，并包含对应索引表。
* 确认 `agent.md`、README 和模板中的状态枚举一致。
* 确认时间格式说明存在：`YYYY-MM-DD HH:mm`，默认时区 `Asia/Shanghai`。
* 确认 `plan/` 与 `question/` 的创建触发条件没有被改成自动滥建。
* 确认如涉及关联问题或关联计划，双向引用规则仍然成立。

---

## 5. 给 AI 的推荐提示词 (Prompt Template)

当用户通过 Codex、Cursor 等工具给你派发任务时，你可以自我初始化（或提示用户）以下内容：

```markdown
请在修改代码前执行以下准备动作：
1. 精准阅读：
   - 检查 `idea/STATUS.md` 获取当前最新开发进度；
   - 检查 `rule/AI修改入口指南.md` 确认此任务涉及文件的职责划分；
   - 检查 `rule/状态管理规则.md`（若修改涉及扫描、抓包生命周期或云台状态）。
   - 若涉及全景/Hugin/PMAP，检查 `idea/ARCHITECTURE.md` 与 `plan/全景拼接与像素查表优化方案.md`。
2. 代码定位：
   - 不要只相信历史文档中的绝对行号，先使用 `rg` 定位核心函数名（例如 `ptz_process_main`, `packet_processor`, `_run_nona_pmap`, `load_pmap`）。
   - 区分 Windows 修改机与 Linux 运行设备；Redis/硬件/Hugin 运行状态不要用本机 localhost 命令判断。
3. 遵循 Git 提交流程：
   - 仅对本次相关的改动文件进行 git add，严禁 git add .。每完成一个模块的可用代码便做一次闭环提交。
4. 修改后做事实同步：
   - 按“代码事实与文档必须同一闭环更新”检查接口文档、`CONTEXT.md`、架构文档、状态规则和 `idea/STATUS.md`。
   - 只更新本次确实发生变化的文档，但不得遗漏已被代码改动推翻的旧描述。
```

阅读完此 `agent.md` 后，请你：
1. 对齐上面的核心原则。
2. 查看 [`idea/STATUS.md`](file:///d:/Workspace/10_项目集/02_202510_wifi信号定位设备/02_程序脚本/后端/v2.2/idea/STATUS.md)，开始你的开发旅程吧！
