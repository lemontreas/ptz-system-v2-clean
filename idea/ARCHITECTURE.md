# 项目架构文档 - WiFi信号定位设备后端 v2.2

> **AI 使用说明**：这是为后续 AI 准备的项目地图。接到需求时先读本文件了解进程、文件职责和 Redis 通信方式，再用 `rg` / `@文件名` 精准定位具体代码。
>
> **重要限制**：本文只负责"项目在哪里、流程大概怎么串"，不替代修改规则。凡是涉及全面扫描、定位扫描、抓包、stop、`/api/v1/ptz/status`、`scan_id`、`active_scan_id` 的修改，必须同时阅读 `rule/状态管理规则.md`。
>
> **定位约定**：本文中的行号和行数只作为历史线索，代码变化后可能偏移。实际修改前必须用 `rg -n "关键函数或 action 名称" 文件名` 重新定位。

---

## 一、AI 必读顺序

1. 先读本文：理解进程边界、核心文件、Redis key 和主流程。
2. 如果改状态、停止、扫描生命周期或前端状态展示，继续读 `rule/状态管理规则.md`。
3. 如果需要让另一个 AI 修改代码，先读 `rule/AI修改入口指南.md`，再给出具体任务。
4. 如果修改前端对接说明，参考 `idea/前端更新说明_全面扫描_定位扫描与摄像头.md`。

---

## 二、系统概述

本系统是一个 **PTZ云台 + WiFi抓包** 联动的信号定位后端。
- 云台（PTZ）扫描各个方向，抓包模块同步采集WiFi信号强度（RSSI）。
- 通过对比不同角度的信号强度，定位WiFi设备的方向角。
- 所有进程通过 **Redis** 进行通信（无共享内存，无直接函数调用）。
- 摄像头/全景链路由 `web_server.py` 接收 HTTP 请求后同步拍照、调用 Hugin 拼接，并生成全景坐标映射文件。
- 当前 Hugin 全景坐标转换以 `pixel_map.pmap` 为权威映射：像素转角度直接查 PMAP，角度转像素基于 PMAP 做 source 像素近邻反查，并在进程内按 owner/source 坐标建立分桶索引以支持批量点快速查询。`coordinate_map.npz` / `correction_map.npz` 仅作为 legacy/fallback 或诊断产物；旧 CSV/SQLite 仅保留为历史诊断兼容。

**运行方式**：`python run_manager.py` 启动所有子进程。

---

## 三、文件清单与职责

| 文件 | 行数 | 职责 | 进程角色 |
|------|------|------|----------|
| `run_manager.py` | ~200行 | **入口**：用 subprocess 启动并监控子进程，处理 SIGTERM/SIGKILL；启动时清理残留 Redis 运行态 Key | 主进程 |
| `web_server.py` | ~3000行 | **REST API**：Flask服务，接收外部指令，写入 Redis 队列；读取 Redis 状态返回给客户端；全面扫描/定位扫描的 API 参数校验和状态聚合也在这里 | 子进程 |
| `ptz_control.py` | ~7600行 | **PTZ控制器**：从 Redis 队列取命令，驱动云台执行移动/扫描；全面扫描、定位扫描、移动到最强点抓包的主流程在这里 | 子进程 |
| `capture_worker.py` | ~3500行 | **抓包工人**：用 Scapy 嗅探 802.11 数据包，解析RSSI，写入 Redis 网格数据；负责全面扫描点位抓包、定位扫描信道探测/采样、独立抓包和任务级取消检查 | 子进程 |
| `config.json` | - | **配置文件**：中文 key 配置（网卡、云台、摄像头、redis），换设备只需改此文件 | 配置文件 |
| `config_loader.py` | ~525行 | **配置加载器**：从 config.json 加载配置，支持环境变量覆盖，优先级：环境变量 > config.json > 代码默认值 | 工具库（无独立进程） |
| `starlink_detector.py` | ~130 | **S9 星链识别**：从 Beacon 帧提取三条指纹特征（TPC/WMM/Vendor），被 capture_worker import | 工具库（无独立进程） |
| `grid_utils.py` | ~600 | **共享工具库**：网格索引计算、Redis网格数据读写，被 ptz_control 和 capture_worker 共同 import | 工具库（无独立进程） |
| `pelco_d_controller.py` | ~766 | **硬件驱动**：Pelco-D串口协议实现，`PtzControl` 类，被 ptz_control.py import | 工具库（无独立进程） |
| `ptz_text.py` | 小 | 独立测试脚本，手动测试PTZ运动 | 独立脚本 |
| `ptz_precision_test.py` | 小 | 精度测试脚本 | 独立脚本 |
| `camera_utils.py` | ~97 | **S1 摄像头**：UVC 拍照，V4L2 接口 | 工具库 |
| `panorama_sampling.py` | ~90 | **S1 采样点**：从路径筛选最优停留点 | 工具库 |
| `panorama_stitch.py` | ~100 | **S1 拼接**：多图拼接为全景 | 工具库 |
| `test_camera_stitch.py` | ~180 | **S1 独立测试**：摄像头端到端抓拍+拼接流 | 独立脚本 |
| `test_cv2_stitch.py` | ~210 | **全景拼接算法沙盒**：包含 pano/scans 自动降级双轨核心逻辑 | 独立脚本 |
| `hugin_panorama_runtime.py` | ~2000 | **Hugin 全景运行时**：中心裁剪输入图、生成 PTO、调用 Hugin/nona、生成 `pixel_map.pmap`，并为角度转像素维护进程内 PMAP 分桶反查索引；`coordinate_map.npz` / `correction_map.npz` 仅作 legacy/fallback 或诊断 | 工具库 |
| `hugin_coordinate_mapper.py` | - | **Hugin 坐标映射 legacy 工具**：根据 PTO/session 构建 `coordinate_map.npz` 与 metadata，保留给诊断或 PMAP 不可用时的 fallback | 工具库 |
| `pmap_utils.py` | - | **PMAP 二进制工具**：读写 `PMAP0001` dense planar `.pmap` 与 `.pcand` 候选诊断格式 | 工具库 |
| `hugin/hugin_pto_math_mapper.py` | - | **Hugin/PMAP 诊断工具**：支持 `.pmap` lookup；CSV/SQLite 仅为 legacy 模式 | 独立脚本 |
| `HUGIN_MAPPING_BUNDLE_README.md` | - | **映射 bundle 说明**：记录导出目录、PMAP 检查命令和 `HUGIN_NONA_BIN` / `HUGIN_BUILD_PMAP` 用法 | 文档 |
| `camera_worker.py` | - | **实验/独立摄像头 worker**：当前未由 `run_manager.py` 常驻启动；主全景流程仍在 `web_server.py` 中同步执行 | 独立/待集成 |
| `rebuild_full_scan_whitelist.py` | ~180 | **全面扫描白名单重建脚本**：不重扫，基于 Redis 里已有 `full_scan:round_N_results` 重新生成白名单 | 独立脚本 |
| `S1_TEST_PROGRESS.md` | - | **S1 测试进度**：已完成项、待测项、常用命令 | 文档 |
| `1.json` | - | 数据文件（扫描结果/配置，具体用途看上下文） | 数据文件 |
| `TODO.md` | - | **待办清单**：与 ROADMAP 对应，记录各需求完成状态，完成后更新 | 文档 |

---

## 四、进程间通信（Redis 为唯一总线）

```
外部客户端
    │  HTTP REST
    ▼
web_server.py ──LPUSH──► ptz:command_queue ──BRPOP──► ptz_control.py
    │                                                        │
    │  读取状态                                          写入状态
    └──GET──────────────── ptz:current_status ◄────────────┘
    │
    └──LPUSH──► capture:command_queue ──BRPOP──► capture_worker.py
                                                      │
                                              写入网格RSSI数据
                                              (定向天线 + 全向天线双路)
                                                      │
                                               Redis: p_data:{index}
                                               Redis: capture:data_stream

web_server.py ──同步调用──► hugin_panorama_runtime.py ──subprocess──► Hugin tools / nona-pixelmap
      │                                │
      │                                ├── coordinate_map.npz / correction_map.npz
      │                                ├── pixel_map.pmap（启用且 nona 支持 --pixel-map 时）
      │                                └── /home/ultiwill/hugin/<bundle>/ 导出副本（Linux 默认）
      └──SET camera:last_panorama_meta
```

---

## 五、Redis 键名速查表

| Redis Key | 类型 | 写入方 | 读取方 | 含义 |
|-----------|------|--------|--------|------|
| `ptz:command_queue` | List | web_server | ptz_control | PTZ指令队列（LPUSH/BRPOP） |
| `ptz:current_status` | String(JSON) | ptz_control | web_server | 云台当前状态（pan/tilt/state） |
| `ptz:limits` | String(JSON) | ptz_control | web_server | 设备物理限位范围 |
| `capture:command_queue` | List | web_server | capture_worker | 抓包指令队列 |
| `capture:data_stream` | Stream | capture_worker | web_server | 抓包数据流 |
| `p_data:{index}` | Hash | capture_worker/grid_utils | web_server | 每个网格的RSSI统计数据 |
| `gimbal:default_config` | Hash | 前端/配置模块 | web_server/ptz_control | 云台默认配置；全面扫描中 `work_pan_range` / `work_tilt_range` 表示初筛范围，缺失时回退到 `pan_range` / `tilt_range` |
| `intelligent_scan:active` | String | web_server | ptz_control | 智能扫描激活标志 |
| `intelligent_scan:signals` | List | capture_worker | ptz_control | 信号采集队列（供智能扫描使用） |
| `intelligent_scan:status` | String(JSON) | ptz_control | web_server | 智能扫描当前状态 |
| `ptz:step_config` | String(JSON) | web_server | grid_utils | 网格步长配置（pan_step/tilt_step） |
| `starlink:detected_bssids` | String(JSON) | capture_worker | web_server | S9：已识别星链设备 {bssid: {is_starlink, features, ssid, channel}}，TTL=24h |
| `multi_scan:stop_full_area_scan` | String | web_server/ptz_control | ptz_control/capture_worker | 全面扫描停止标志，TTL=120s；`1` 表示手动停止，`time_limit` 表示扫描窗口到时 |
| `full_scan:results` | String(JSON) | ptz_control | web_server | 全面扫描最新轮次外层结果，含 `round_status/stop_reason/window_*` 与 `results` 点位字典 |
| `full_scan:active_scan_id` | String | web_server/ptz_control | web_server/ptz_control/capture_worker | 当前全面扫描任务 ID；stop key 和 capture_worker 已派发任务必须精确匹配，防止旧任务串扰 |
| `full_scan:latest_round` | String | ptz_control | web_server | 全面扫描最新轮次号 |
| `full_scan:round_{N}_results` | String(JSON) | ptz_control | web_server | 全面扫描第 N 轮外层结果存档，结构同 `full_scan:results` |
| `full_scan:whitelist:round_{N}` | String(JSON) | ptz_control | web_server | 全面扫描第 N 轮成功完成后的筛选白名单，只对 `SUCCESS` 轮次生成 |
| `full_scan:whitelist:latest_success` | String(JSON) | ptz_control | web_server | 最新成功轮次的白名单快捷读取 |
| `full_scan:whitelist:latest_round` | String | ptz_control | web_server/脚本 | 最新成功白名单对应轮次号 |
| `full_scan:whitelist:rounds` | String(JSON) | ptz_control | web_server | 已生成白名单的轮次摘要列表 |
| `location_scan:stop` | String | web_server | ptz_control/capture_worker | T3：定位扫描停止标志，TTL=120s |
| `location_scan:active_scan_id` | String | web_server/ptz_control | web_server/ptz_control/capture_worker | 当前定位扫描任务 ID；定位信道探测和点位采样都必须携带并校验 |
| `location_scan:status` | String(JSON) | ptz_control | web_server | T3：定位扫描当前阶段，`phase` 使用 `queued` / `channel_probe` / `fast_verify` / `coarse` / `mid` / `fine` / `locating` / `capturing` / `waiting` / `idle`，`status` 表示 running/done/stopped/error |
| `location_scan:result` | String(JSON) | ptz_control | web_server | T3：定位扫描结果，按 MAC 增量写入，含 `channel/bandwidth/best_position/best_rssi/verify_rssi/pcap_files`；RSSI 只按 TA 统计，最终 pcap 可按任意 802.11 地址字段命中目标 MAC 保存 |
| `detect_channels:notify:{probe_round}:{point_idx}:{timestamp}` | List | capture_worker | ptz_control | T3：信道探测完成通知；定位扫描使用唯一队列，避免旧通知残留串轮次 |
| `detect_channels:notify` | List | capture_worker | ptz_control | T3：旧兼容默认队列；新定位扫描流程不应复用固定队列 |
| `location_scan:capture:{mac}:{ts}` | List | capture_worker | ptz_control | T3：定位后按 MAC 抓包完成通知，返回 pcap 文件组与停止原因 |
| `camera:last_panorama_meta` | String(JSON) | web_server | web_server | 最新一次全景图元数据（pan_range/tilt_range/canvas_size/map_path/pmap_path/pmap_summary/pmap_error 等），供 coordinate_convert 接口和 Hugin/PMAP 验证使用 |
| `capture:running` | String | capture_worker/web_server | web_server/capture_worker | 抓包是否运行；前端判断抓包不能只看 PTZ 顶层 `state` |
| `capture:status` | String(JSON) | capture_worker | web_server | 抓包详细状态；`capture_at_best` 的实时抓包状态也从这里合并到 `/api/v1/ptz/status` |

`run_manager.py` 启动清理只处理运行态残留：`multi_scan:stop_full_area_scan`、`location_scan:status`、`location_scan:stop`、`detect_channels:notify`、`detect_channels:notify:*` 等。它不删除 `location_scan:result`，也不负责清理全面扫描历史结果；全面扫描新任务启动时由 `ptz_control.py` 清理上一任务的 `full_scan:*` 运行态结果。

> 注意：代码和文档里同时存在旧命名与新语义。Redis `gimbal:default_config.work_pan_range/work_tilt_range` 在全面扫描里表示初筛大范围，`pan_range/tilt_range` 表示工作区范围；历史命名不要按字面误解。

---

## 六、REST API 端点速查（全部在 web_server.py）

### PTZ 控制
| 方法 | 路径 | 行号 | 功能 |
|------|------|------|------|
| POST | `/api/v1/ptz/move` | L50 | 绝对位置移动（pan/tilt） |
| POST | `/api/v1/ptz/auto_scan` | L92 | 启动自动栅格扫描 |
| POST | `/api/v1/ptz/stop_scan` | L166 | 停止扫描 |
| POST | `/api/v1/ptz/stop` | L172 | 紧急停止 |
| GET  | `/api/v1/ptz/status` | L342 | 查询当前云台状态 |
| POST | `/api/v1/ptz/check_config_update` | L471 | 检查并更新步长配置 |
| POST | `/api/v1/ptz/migrate_grid_data` | L501 | 网格数据迁移 |

### 智能扫描
| 方法 | 路径 | 行号 | 功能 |
|------|------|------|------|
| POST | `/api/v1/ptz/intelligent_scan/start` | L182 | 启动智能缩进扫描 |
| POST | `/api/v1/ptz/intelligent_scan/stop` | L237 | 停止智能扫描 |
| GET  | `/api/v1/ptz/intelligent_scan/current_round` | L252 | 查询当前扫描轮次数据 |
| GET  | `/api/v1/ptz/intelligent_scan/status` | L302 | 查询智能扫描状态 |

### 初始扫描 / 多点扫描 / 全区扫描
| 方法 | 路径 | 行号 | 功能 | 状态 |
|------|------|------|------|------|
| POST | `/api/v1/ptz/initial_scan/start` | L564 | 启动初始化扫描 | **搁置不用** |
| POST | `/api/v1/ptz/initial_scan/stop` | L602 | 停止初始扫描 | **搁置不用** |
| GET  | `/api/v1/ptz/initial_scan/result` | L609 | 获取初始扫描结果 | **搁置不用** |
| POST | `/api/v1/ptz/multi_point_scan/start` | L631 | 启动多点精细扫描 | **搁置不用** |
| POST | `/api/v1/ptz/multi_point_scan/stop` | L694 | 停止多点扫描 | **搁置不用** |
| GET  | `/api/v1/ptz/multi_point_scan/result` | L701 | 获取多点扫描结果 | **搁置不用** |
| POST | `/api/v1/ptz/full_area_scan/start` | L1421 | 启动全区域扫描 | 正常使用 |
| POST | `/api/v1/ptz/full_area_scan/stop` | L1612 | 停止全区扫描 | 正常使用 |
| GET  | `/api/v1/ptz/full_area_scan/result` | L1629 | 获取全区扫描结果 | 正常使用 |
| GET  | `/api/v1/ptz/full_area_scan/whitelist` | L1701 | 获取全面扫描成功轮次白名单 | 正常使用 |

> **注意**：初始扫描和多点扫描目前搁置不用，因删除需改动较多代码，暂时保留但不使用。

### 抓包控制
| 方法 | 路径 | 行号 | 功能 |
|------|------|------|------|
| POST | `/api/v1/capture/start` | L353 | 开始抓包（指定网卡/信道/带宽） |
| POST | `/api/v1/capture/stop` | L380 | 停止抓包 |
| POST | `/api/v1/capture/save_pcap` | L398 | 保存当前数据为pcap文件 |
| POST | `/api/v1/capture/clear_data` | L432 | 清空抓包数据 |

### 摄像头 / 全景 / Hugin
| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/v1/camera/images/<filename>` | 读取已保存的摄像头/全景图片 |
| POST | `/api/v1/camera/capture` | 普通拍照或全景拍摄；`panorama=true` 时同步执行拍照、Hugin 拼接、坐标映射与可选 `.pmap` 生成 |
| POST | `/api/v1/camera/coordinate_convert` | 单图或全景坐标转换；Hugin 全景有 PMAP 时以 `pmap_path` 指向的 `pixel_map.pmap` 为权威映射，`coordinate_map.npz` / `correction_map.npz` 仅 fallback |

### S9 星链设备识别
| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/v1/starlink/detected` | 查询当前会话已识别的星链设备列表（来自 capture_worker 实时写入 Redis） |

### T3 定位扫描
| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/v1/ptz/location_scan/start` | 启动定位扫描（后端自动步径、中心+四角信道探测、多 MAC 共用点位、可选抓包） |
| POST | `/api/v1/ptz/location_scan/stop` | 停止定位扫描 |
| GET  | `/api/v1/ptz/location_scan/status` | 查询定位扫描当前进度 |
| GET  | `/api/v1/ptz/location_scan/result` | 获取定位扫描最终结果（按 MAC 分组，含 best_position） |
| POST | `/api/v1/ptz/capture_at_best` | 指定点位抓包（前端必传 pan/tilt/mac/channel/bandwidth；停止当前扫描 → 移动到指定点位 → 开始抓包，立即返回；pcap 按任意 802.11 地址字段命中目标 MAC 保存） |

---

## 七、核心类与函数速查

### `ptz_control.py`
| 名称 | 行号 | 说明 |
|------|------|------|
| `PTZDeviceState` 类 | L458 | 管理云台设备状态机（IDLE/AUTO_SCANNING/MOVING等） |
| `ptz_process_main()` | L2192 | Worker主循环，BRPOP从Redis取命令分发处理 |
| `safe_move_to_pan_tilt()` | L742 | 带校验的安全移动（含限位保护） |
| `high_precision_move()` | L788 | 高精度多步移动（校正偏差） |
| `safe_split_move()` | L816 | 分步移动（先pan后tilt或先tilt后pan） |
| `generate_scan_path()` | L1100 | 生成栅格扫描路径点列表 |
| `run_multi_point_scan_blocking()` | L1941 | 阻塞式多点扫描主逻辑 |
| 智能扫描系列 `_*` | L50-229 | 智能缩进扫描算法（信号收集/滤波/范围计算） |
| `_auto_location_step()` | L1236 | 定位扫描固定步径：16° / 8° / 4° |
| `_group_location_configs()` | L1280 | 定位扫描按 `(channel, bandwidth)` 分组 MAC，减少重复移动 |
| `_build_full_area_segments()` | L1336 | 全面扫描真实执行路径生成：precheck / guard / work |
| `_build_full_scan_whitelist_payload()` | L1601 | 全面扫描成功轮次白名单筛选 |
| `_write_full_scan_whitelist_result()` | L1794 | 写入 `full_scan:whitelist:*` |
| `start_full_area_scan` 分支 | L3192 | 全面扫描执行主循环 |
| `start_location_scan` 分支 | L3836 | 定位扫描执行主循环 |
| `move_to_best_capture` 分支 | 需 `rg` 定位 | `/api/v1/ptz/capture_at_best` 触发：停止当前扫描、移动到指定点位、派发抓包命令 |

### `capture_worker.py`
| 名称 | 行号 | 说明 |
|------|------|------|
| `capture_worker_main()` | L1438 | Worker主循环，监听 capture:command_queue |
| `packet_processor()` | L1131 | Scapy回调，解析每个802.11包的RSSI和MAC |
| `start_capture()` | L1276 | 启动嗅探（配置网卡监控模式）|
| `stop_capture()` | L1345 | 停止嗅探 |
| `update_grid_data_and_get_key()` | L476 | 根据pan/tilt计算网格索引，写入Redis |
| `push_signal_to_intelligent_scan()` | L375 | 向智能扫描信号队列推送数据 |
| `setup_monitor_mode_once()` | L755 | 配置WiFi网卡为监控模式 |
| `dual_sniff()` | L859 | S7：双卡并行sniff工具函数，同时在定向和全向网卡上抓包 |
| `adjust_channel_with_retry()` | L1379 | 按指定网卡切换信道/带宽，内部维护每块网卡自己的信道缓存 |
| `discover_macs_for_full_scan` 分支 | L1892 | 全面扫描点位抓包，通知 `full_scan:{point_id}_notify` |
| `detect_channels` 分支 | L2105 | 定位扫描信道探测，通知 `detect_channels:notify:*` |
| `scan_at_point` 分支 | L2260 | 定位扫描点位采样，支持 `stop_key` |
| `start_capture` / `save_pcap` / `stop_capture` 分支 | 需 `rg` 定位 | 独立抓包和 `capture_at_best` 抓包状态写入；必须维护 `capture:running` 与 `capture:status` |

### `grid_utils.py`
| 名称 | 行号 | 说明 |
|------|------|------|
| `set_global_grid_config()` | L134 | 设置全局网格范围（pan/tilt min/max） |
| `create_p_data_if_needed()` | L153 | 按需创建网格Redis Hash |
| `update_grid_data_atomic()` | L384 | 原子更新网格RSSI数据 |
| `_grid_index_from_angles()` | L444 | 角度→网格索引计算 |
| `_angles_from_grid_index()` | L476 | 网格索引→角度中心点计算 |
| `clear_all_global_grids()` | L418 | 清空所有网格数据 |
| `get_cached_step_config()` | L50 | 从Redis读取步长配置（带缓存） |

### `pelco_d_controller.py`
| 名称 | 行号 | 说明 |
|------|------|------|
| `PtzControl` 类 | L15 | Pelco-D协议串口控制器，封装所有串口通信 |

### `hugin_panorama_runtime.py` / PMAP
| 名称 | 定位命令 | 说明 |
|------|----------|------|
| `build_hugin_panorama_from_shots()` | `rg -n "def build_hugin_panorama_from_shots" hugin_panorama_runtime.py` | Hugin 全景主入口：写入裁剪图、生成 PTO、调用 Hugin、生成坐标映射与 PMAP |
| `_center_crop_for_stitch()` | `rg -n "def _center_crop_for_stitch" hugin_panorama_runtime.py` | 当前生产路径使用中心裁剪；边界补拍/非对称裁剪尚未接入 |
| `_run_nona_pmap()` | `rg -n "def _run_nona_pmap|HUGIN_BUILD_PMAP" hugin_panorama_runtime.py` | 调用改造版 `nona --pixel-map` 生成 `pixel_map.pmap`；不支持时记录 skipped/error，不阻塞全景输出 |
| `_export_mapping_bundle()` | `rg -n "def _export_mapping_bundle|pixel_map.pmap" hugin_panorama_runtime.py` | 导出 `panorama.jpg`、`coordinate_map.npz`、`pixel_map.pmap`、PTO、source images 和诊断脚本 |
| `read_pmap_header()` / `load_pmap()` | `rg -n "def read_pmap_header|def load_pmap|PMAP_HEADER" pmap_utils.py` | PMAP header 和 planar 数组读取 |
| `hugin/hugin_pto_math_mapper.py` | `rg -n "mode pmap|index-csv|legacy" hugin/hugin_pto_math_mapper.py` | PMAP/PTO 诊断。`csv`/`index-csv` 是 legacy 模式，不是新生产路线 |

---

## 八、扫描模式说明

| 扫描模式 | 触发API | 核心逻辑位置 | 说明 | 状态 |
|----------|---------|-------------|------|------|
| **自动栅格扫描** | `auto_scan` | `ptz_control.py` L821 `generate_scan_path` | 按步长遍历所有网格点 | 正常使用 |
| **智能缩进扫描** | `intelligent_scan/start` | `ptz_control.py` L50-229 | 多轮迭代，逐步缩小扫描范围 | 正常使用 |
| **初始扫描** | `initial_scan/start` | `web_server.py` L564 + ptz_control | 首次全范围扫描，建立RSSI热图 | **搁置不用** |
| **多点精细扫描** | `multi_point_scan/start` | `ptz_control.py` L942 `run_multi_point_scan_blocking` | 对候选MAC地址的多点高精度定位 | **搁置不用** |
| **全区扫描** | `full_area_scan/start` | `web_server.py` + `ptz_control.py` | 每轮先初筛粗扫，再按工作范围细扫，支持时间窗口、间隔循环与暂停/恢复 | 正常使用 |
| **客户端扫描** | `client_scan/start` | `web_server.py` L1566 | S8：扫描客户端设备（STA） | 正常使用 |
| **定位扫描** | `location_scan/start` | `web_server.py` + `ptz_control.py` | T3：信道探测、共享扫描、最终定位三阶段；后端自动步径，可选按 MAC 抓包 | 正常使用 |

### 当前重点功能代码地图

后续如果要大改全面扫描或定位扫描，按下表从上到下看，能最快区分 API 层、云台执行层、抓包层和结果层。

#### 全面扫描代码地图

| 层级 | 文件与位置 | 当前职责 |
|------|------------|----------|
| API 入参/估算 | `web_server.py` L205 `_build_full_area_scan_plan()` | 按 precheck / guard / work 生成前端可见的估算计划；必须和 `ptz_control.py` 实际点位逻辑保持一致 |
| API 启动 | `web_server.py` L1421 `/api/v1/ptz/full_area_scan/start` | 读取 Redis/请求中的初筛范围和工作区，设置 `scan_plan`、`point_count`、`estimated_time_minutes`，向 `ptz:command_queue` 写 `start_full_area_scan` |
| API 停止/结果 | `web_server.py` L1612/L1629/L1701 | 停止写 `multi_scan:stop_full_area_scan`；结果读 `full_scan:results`；白名单读 `full_scan:whitelist:*` |
| 执行点位生成 | `ptz_control.py` L1336 `_build_full_area_segments()` | 真实执行用的三段路径：precheck 8° 跳过工作区+外扩区，guard 8° 扫外扩边，work 4° 扫工作区 |
| 执行主循环 | `ptz_control.py` L3192 `start_full_area_scan` 分支 | 校准、轮次循环、时间窗口、点位移动、等待 capture 结果、写每轮结果 |
| 停止检查 | `ptz_control.py` L1443 `_full_area_stop_reason()` + L3215 后续主循环 | 同时识别手动停止和 `time_limit`，避免长时间阻塞后停不下来 |
| 白名单筛选 | `ptz_control.py` L1601 `_build_full_scan_whitelist_payload()` | 只对 `SUCCESS` 轮次筛选；按工作区内外 RSSI、全向/定向差值、命中点数生成 `mac_whitelist` |
| 白名单写入 | `ptz_control.py` L1794 `_write_full_scan_whitelist_result()` | 写 `full_scan:whitelist:round_N`、`latest_success`、`latest_round`、`rounds` |
| 抓包采样 | `capture_worker.py` L1892 `discover_macs_for_full_scan` 分支、L859 `dual_sniff()` | 每个点位定向+全向并行抓包；定向结果为主，全向只作为 `omni_rssi_avg/sample_count/status` 附加字段 |
| 离线重筛 | `rebuild_full_scan_whitelist.py` | 从已有 `full_scan:round_N_results` 重建白名单，测试阈值时不用重新跑几十分钟扫描 |

#### 定位扫描代码地图

| 层级 | 文件与位置 | 当前职责 |
|------|------------|----------|
| API 默认值/估算 | `web_server.py` L97-L99、L295 `_auto_location_step()` | 读取 `config.json` 的定位默认配置；API 层估算固定 16/8/4 度步径 |
| API 启动 | `web_server.py` L1745 `/api/v1/ptz/location_scan/start` | 校验目标 MAC/区域，支持顶层固定 `channel` + `bandwidth` 或 `target_configs` 按 MAC 指定信道/带宽，读取 `probe_dwell_time`、`probe_rounds_max`，写 `start_location_scan` 命令 |
| API 停止/状态/结果 | `web_server.py` L1911/L1920/L1943 | 停止写 `location_scan:stop`；状态读 `location_scan:status` 和 `ptz:current_status.location_scan`；结果读 `location_scan:result` |
| 信道配置列表 | `ptz_control.py` L651-L682 `LOCATION_SCAN_CONFIGS` | 定位信道探测使用的 `(channel, bandwidth)` 列表 |
| 点位步径 | `ptz_control.py` L1236 `_auto_location_step()` | `coarse16=16°`、`mid8=8°`、`fine4=4°`，不再接受前端步径 |
| MAC 分组 | `ptz_control.py` L1280 `_group_location_configs()` | 按 `(channel, bandwidth)` 合并多个 MAC，同一个点位只移动一次 |
| 执行主流程 | `ptz_control.py` L3836 `start_location_scan` 分支 | 信道探测、三轮扫描、最终定位、可选抓包全部在这里串联 |
| 候选框生成 | `ptz_control.py` L3969 `_build_fine_boxes()` | 取上一轮 `best_rssi - 5 dB` 内的强信号点生成下一轮候选扫描框 |
| 信道探测通知 | `ptz_control.py` L4035-L4059 | 每个探测点用唯一 `detect_channels:notify:{round}:{point}:{ts}`，避免固定队列残留 |
| 三轮扫描 | `ptz_control.py` L4130-L4174 | 依次执行 `coarse16`、`mid8`、`fine4`；每轮结果更新 `location_scan:result` |
| 最终定位/抓包 | `ptz_control.py` L4249 后 | 移动到每个 MAC 最强点复验；如请求了抓包，等待 `location_scan:capture:{mac}:{ts}` |
| 抓包信道探测 | `capture_worker.py` L2105 `detect_channels` 分支 | 按配置逐信道 sniff；找到所有目标 MAC 后提前结束当前点位探测 |
| 抓包点位采样 | `capture_worker.py` L2260 `scan_at_point` 分支 | 按分组配置采样点位 RSSI，支持 `stop_key='location_scan:stop'` |

### 定位扫描三阶段流程

定位扫描面向前端暴露以下阶段：

| phase | 含义 |
|---|---|
| `queued` | 启动接口已接受任务，PTZ worker 尚未进入具体执行阶段 |
| `channel_probe` | 信道探测：每个目标区域取中心 + 四角，多 MAC 同时探测，最多 `probe_rounds_max` 轮 |
| `fast_verify` | 连续追踪第二轮起快速校验上一轮最强点 |
| `coarse` | 粗扫阶段 |
| `mid` | 中扫阶段：根据粗扫强点收缩候选框，并追加目标区域保底覆盖点 |
| `fine` | 细扫阶段：根据中扫强点继续收缩候选框，并补完剩余目标区域保底覆盖点 |
| `locating` | 最终定位：移动到每个 MAC 的候选最强点确认 RSSI |
| `capturing` | 定位扫描内部正在按 MAC 抓包；抓包完成后，有下一轮进入 `waiting`，否则进入 `idle` |
| `waiting` | 连续追踪模式下，一轮完成后等待 `time_interval` |
| `idle` | 定位任务未运行或已结束 |

定位扫描不再接收前端 `pan_step/tilt_step`。步径由后端按当前定向天线参数固定：
- 第一轮：工作范围外扩 9° 后扫描，步径 16°
- 第二轮：根据第一轮强信号区域生成候选框，步径 8°
- 第三轮：根据第二轮强信号区域继续收缩候选框，步径 4°
- 最终定位：移动到每个 MAC 的当前最强点再验证一次
`config.json` 的 `定位扫描.扩边角度` 默认 9°；`定位扫描.信道探测时长` / 请求中的 `detect_duration` 仅兼容旧逻辑，新信道探测主要由 `probe_dwell_time` 与 `probe_rounds_max` 控制。定位扫描扩圈和收缩候选框会裁剪到 Redis `gimbal:default_config.work_pan_range/work_tilt_range` 表示的业务运行范围内；`work_*` 缺失时回退 `pan_range/tilt_range`，再回退 `config.json` 默认扫描范围，并始终与硬件限位求交。中扫/细扫保留 RSSI 收缩候选框，但会追加目标区域保底覆盖点：每个前端目标区域至少覆盖四角+中心，普通范围默认再追加 5 个均匀内部点；已由前序阶段实际扫到的同坐标点不会重复补扫，新增保底点不占 RSSI 候选框点数上限，phase 仍然只显示 `mid` / `fine`。启动接口可选传任务级固定 `channel` + `bandwidth`；传了顶层 `channel` 时必须同时传顶层 `bandwidth`，执行层会跳过自动信道探测并让所有目标 MAC 共用这一组配置。多 MAC 需要分别指定不同信道/带宽时，使用 `target_configs` 按 MAC 传 `{channel, bandwidth}`；已指定的 MAC 不再探测，未指定的 MAC 继续自动探测。顶层 `channel/bandwidth` 不能和 `target_configs` 同时使用。不传 `channel` 且不传 `target_configs` 时，当前信道探测到达一个点位后会遍历 `(channel, bandwidth)` 配置；一旦所有目标 MAC 都找到可用配置，就提前结束该点位探测，不再保守扫完全部 66 个配置。
扫描阶段按 `(channel, bandwidth)` 对 MAC 分组，一个点位只移动一次，再切换配置采集多个 MAC。
最终结果按 MAC 增量写入 `location_scan:result`，抓包文件按 `/mnt/data/location/{project_id}/{mac}/part_XXX.pcap` 分片保存。定位 RSSI 只按 `TA=目标 MAC` 统计；定位最终抓包和 `capture_at_best` 的 pcap 保存口径为目标 MAC 出现在 802.11 `addr1/addr2/addr3/addr4` 任意字段即保存。`capture_at_best` 不再读取 Redis 历史扫描结果，点位、MAC、信道和带宽全部由请求体显式指定。

### 全面扫描三段点位流程

全面扫描扫描本体与筛选层分离：扫描本体负责生成原始点位结果，筛选层只在 `SUCCESS` 轮次结束后生成按轮次白名单。

每一轮执行顺序：

1. 读取初筛范围：优先 Redis `gimbal:default_config.work_pan_range` / `work_tilt_range`。
2. 如果 `work_*` 缺失，回退 Redis `pan_range` / `tilt_range` 作为初筛范围。
3. 读取目标工作范围：前端可传旧单范围 `pan_range` / `tilt_range` 或新多范围 `work_ranges[]`；不传时后端使用 Redis `gimbal:default_config.pan_range` / `tilt_range`。
4. `phase="coarse"`：外部大范围使用稳定核心网格扫描，核心点数 `30-42`，网格点取单元中心，并额外追加四角与四边中心 8 个边界点。
5. `phase="fine"`：每个 `work_range` 独立生成单次稳定网格，核心点数 `20-30`，并额外追加 8 个边界点；多个目标区之间按坐标去重。
6. `phase="deviation_a"`：取消 A/B 分支，按目标区到外部大范围的 4 等分外扩环扫描中间 3 层，每层 12 点；configs 来自目标区细扫中真实 MAC 的 `{channel, bandwidth}` 去重集合，无真实配置则跳过。
7. 小范围下若相邻点距小于 `1°`，在同一轮内对点位排序做交错处理，避免连续小移动，不删除点位。
8. 每个实际完成扫描的点位都会在原始结果 `macs` 中追加 `ff:ff:ff:ff:ff:ff` synthetic marker（`rssi_avg=-100`、`synthetic=true`、`role="scan_point_marker"`），真实 MAC 保留。
9. 若本轮 `round_status="SUCCESS"`，基于原始点位结果生成 `full_scan:whitelist:round_{N}`；若为 `PARTIAL`，不生成白名单。

全面扫描路径当前不再依赖固定 8°/4° 步径估算点数；配置以核心点数上下限、边界点和偏差区外扩等分数为主。`phase` 仍只做阶段展示，不参与生命周期判断。

筛选时不按 `phase` 判断工作区，而是按点位 `position.pan/tilt` 是否落在 `work_ranges` 判断，避免阶段重叠或坐标去重时误分组。同一 MAC 在相同或非常接近坐标重复出现时按坐标桶去重，RSSI 取该坐标桶内最强值。

### 全面扫描成功轮次白名单

配置位于 `config.json` 的中文块 `全面扫描筛选`：

```json
{
  "全面扫描筛选": {
    "启用": true,
    "目标区最少命中点数": 2,
    "工作区最少命中点数": 2,
    "整轮最少命中点数": 4,
    "工作区相对其他区域最小强度差": 3,
    "定向相对全向最小强度差": 5,
    "要求目标区位置证据": true,
    "要求最强点在工作区": true,
    "坐标去重角度": 0.05
  }
}
```

白名单生成规则：

1. 只处理完整成功轮次，`PARTIAL` 轮次只保留原始结果。
2. 工作区/非工作区按点位坐标是否落在 `work_ranges` 判断。
3. 同一坐标桶内同一 MAC 只算一个命中点，RSSI 取该坐标桶内最强值。
4. 白名单构建会跳过 `synthetic=true`、`role="scan_point_marker"` 或 MAC 为 `ff:ff:ff:ff:ff:ff` 的 marker。
5. MAC 需满足目标区命中点数、整轮命中点数、目标区位置证据、工作区相对其他区域 RSSI 差值。
6. 位置证据规则：最强点在目标区直接通过；否则计算 RSSI 加权质心，质心在目标区则低置信通过，否则拒绝并在 rejected metrics 输出估计位置、目标区范围、最强点位置和失败原因。
7. 如果工作区点位存在全向 RSSI，则再检查定向 RSSI 是否比全向 RSSI 高到阈值；没有全向数据则跳过这一层。

白名单位置复核是可选的同任务尾阶段，只改变展示用最强位置与 RSSI，不改变上述
成员判定。每条矩形边/对角线仍为一次连续 PTZ 移动；capture worker 在线段内存
保留单包峰值时间，PTZ worker 记录现有到位轮询产生的时间化 Pan/Tilt 轨迹。
峰值时间在相邻轨迹样本间插值得到角度后，只在原始像素线段上执行自适应
像素→角度正向查找，避免全景扭曲和重叠区下无约束角度→像素反查跳到线外。
成功来源为 `whitelist_refinement_peak_trajectory`；局部映射空洞会被跳过，整线
无有效候选时保留初始固定点。

新全面扫描任务启动时会清理上一任务的 `full_scan:results`、`full_scan:round_*_results` 与 `full_scan:whitelist:*` 运行态 Redis key；历史回放以 SQLite 历史库为准。

### 全面扫描时间窗口与间隔

启动接口支持两个可选参数：

| 参数 | 单位 | 说明 |
|------|------|------|
| `scan_time_limit` | 分钟 | 一次扫描窗口持续时长；到时仍未完成当前轮则提前结束，轮次 `round_status="PARTIAL"` |
| `time_interval` | 秒 | 当前扫描窗口/完整轮结束后，等待多久再开始下一次全面扫描 |

组合语义：

| 参数组合 | 行为 |
|----------|------|
| 都不传 | 完整执行一轮全面扫描后自动停止 |
| 仅 `scan_time_limit` | 在该时间窗口内连续执行轮次；时间到后停止 |
| 仅 `time_interval` | 每次执行一轮完整全面扫描，等待间隔后继续下一轮，直到手动停止 |
| 两者都传 | 每个窗口按 `scan_time_limit` 连续扫描，窗口结束后等待 `time_interval`，再进入下一个窗口 |

`full_scan:results` 的外层保存当前轮状态：`RUNNING` / `SUCCESS` / `PARTIAL`，以及 `stop_reason`、`expected_points`、`completed_points`、`window_index`、`window_started_at`、`window_deadline_at`、`remaining_seconds`。

### 全面扫描设备类型识别（type/subtype/ssid 字段）

全面扫描结果中，每个 MAC 包含 `type` / `subtype` / `ssid` 字段：

| type | 含义 | 判断依据 |
|------|------|----------|
| `"ap"` | 接入点 | 发送 Beacon 帧 |
| `"client"` | 客户端 | 发送 Probe Request / Association Request / Reassociation Request，或数据帧（to_ds=1, from_ds=0） |
| `null` | 未识别 | 未收到足够帧信息判断类型 |

| subtype | 含义 | 判断依据 |
|---------|------|----------|
| `"starlink"` | 星链接入点 | `type="ap"` 且 Beacon 帧命中星链特征（TPC/WMM/Vendor OUI） |
| `null` | 无子类型 | 普通 AP、客户端或未识别设备 |

| 字段 | 含义 |
|------|------|
| `ssid` | AP 的 SSID；Beacon 明确隐藏时为 `"_wildcard_"`，非 AP、未抓到 SSID IE 或无法解析时为 `null` |

**更新机制**：
- capture_worker 在程序生命周期内累积 AP/Client/SSID/星链识别缓存，程序重启后自然清空
- 每个点位扫描结束后，用最新缓存回填本轮 `full_scan:results` 中已有 MAC 的 `type/subtype/ssid`
- 星链不再作为独立 `type`，统一表示为 `type="ap"` + `subtype="starlink"`

### 全面扫描暂停修复

**问题**：`brpop` 阻塞等待最长 400 秒，期间不检查停止标志，导致暂停卡住。

**修复**：改成每 3 秒轮询一次，检查停止标志：
- 收到停止命令 → 立即退出
- 收到结果 → 正常处理
- 超时 → 报错

---

## 九、Hugin 全景与 `.pmap` 映射链路

当前全景链路入口是 `POST /api/v1/camera/capture`，请求体中 `panorama=true` 时：

1. `web_server.py` 根据选区调用 `compute_shot_grid()` 生成拍摄点。
2. `web_server.py` 同步移动/拍照并收集 `shots_meta`。
3. `hugin_panorama_runtime.py::build_hugin_panorama_from_shots()` 对输入图做中心裁剪，生成 Hugin PTO，执行 `cpfind/autooptimiser/pano_modify/hugin_executor`。
4. 若 `HUGIN_BUILD_PMAP` 未关闭且当前 `nona` 支持 `--pixel-map`，调用改造版 `nona-pixelmap` 生成 `final/pixel_map.pmap`，并记录 `pmap_path` / `pmap_summary` / `pmap_error`。
5. `coordinate_map.npz` 与 `correction_map.npz` 仅在显式启用 legacy/fallback 时生成或使用；不能作为 PMAP 可用时的默认双向转换权威。
6. Linux 默认导出 mapping bundle 到 `/home/ultiwill/hugin/<panorama-stem>/`，包含 `pixel_map.pmap`、PTO、source images、诊断脚本和 README。

当前真实边界：

- `/api/v1/camera/coordinate_convert` 对 Hugin 全景优先使用 PMAP：`pixels -> angles` 直接查 PMAP；`angles -> pixels` 将角度投影到各来源图 source 像素后，通过进程内 owner/source 分桶索引在 PMAP 中查找最近的全景像素。
- `.pmap` 当前是正式运行时/验证/导出路线，不再扩展旧 CSV/SQLite。`hugin/hugin_pto_math_mapper.py` 中的 `csv` / `index-csv` 只用于历史产物诊断。
- 当前主代码只做中心裁剪；`left_boundary` / `right_boundary` / `stitch_role` / 非对称裁剪仍属于边界优化待评审项，不能写成已接入主流程。
- 设备端必须使用支持 `--pixel-map` 的改造版 `nona`，推荐通过 `HUGIN_NONA_BIN=/home/ultiwill/bin/nona-pixelmap` 指定。

---

## 十、开发约定

- **运行环境边界**：代码修改机通常是 Windows，本系统实际运行在 Linux 设备上。Redis、Hugin/nona、网卡、摄像头和云台状态以 Linux 设备为准；不要用本机 Windows 的 `redis-cli` / `localhost` 探测来判断设备端运行状态。需要查 Redis 或跑系统命令时，命令必须明确在 Linux 设备环境执行。
- **进程通信**：新功能若需跨进程协调，**必须通过 Redis**，禁止直接 import 其他 worker 模块
- **命令格式**：Redis 队列中的命令均为 JSON 字符串，格式 `{"action": "...", ...}`
- **状态管理**：顶层云台运动状态与扫描/抓包生命周期分离；涉及 `full_scan`、`location_scan`、`capture`、stop 或 `scan_id` 时必须遵守 `rule/状态管理规则.md`，优先用局部 patch 辅助函数更新状态，避免整块覆盖 `ptz:current_status`
- **日志**：各模块使用 `logging` + `logging.handlers.RotatingFileHandler`，日志文件在运行目录
- **配置**：所有可调参数优先从**环境变量**读取，其次使用代码默认值（见 web_server.py L9-33）
- **API响应**：统一格式 `{"code": 0, "data": {}, "msg": "ok"}` 或 `{"code": -1, "data": {}, "msg": "错误信息"}`
- **全向天线（S7）**：通过 `CAPTURE_OMNI_INTERFACE` 环境变量启用，为空则不启用。全向网卡的任何故障不能影响定向天线正常采集。扫描结果数据中新增 `omni_rssi_avg` / `omni_sample_count` / `omni_status` 字段（详见 `DESIGN_S7.md`）

---

## 十一、多点扫描每轮流程（重要）

> 此节描述 `ptz_control.py` 中 `start_multi_point_scan` 命令的每轮执行顺序。
> 后续 AI 修改多点扫描逻辑时，必须参照此流程，避免打乱顺序。

```
进入主循环前：
  记录初始扫描位置（用户手动移到的 pan/tilt）→ _initial_scan_pan, _initial_scan_tilt

每轮循环：
  1. 校准 → 去限位点消除累积误差
  2. (仅刷新轮次) 回到初始扫描位置 → 确保刷新时天线朝向与首次初始扫描一致
  3. (仅刷新轮次) 刷新 MAC → discover_macs，增删并行（淘汰连续 N 次未出现的 MAC）
  4. 正式扫描 → run_multi_point_scan_blocking()，遍历路径点位
  5. 保存快照 → _save_project_snapshot()
  6. 等待间隔 → sleep(repeat_interval)，期间分段检测停止标志
```

**刷新轮次判定**：`multi_scan_round == 1` 或 `multi_scan_round % MULTI_SCAN_MAC_REFRESH_ROUNDS == 0`

**MAC 淘汰机制**（auto 模式）：
- 每个 MAC 内部维护 `_miss_count` 计数器
- 刷新时出现 → 归零；未出现 → +1
- 达到 `MULTI_SCAN_MAC_MISS_THRESHOLD`（默认 3）→ 从列表移除
- manual 模式不受影响

**MAC 列表跨项目行为**：
- 停止多点扫描后，`multi_scan:target_macs` 仍保留在 Redis 中
- 直接再启动 auto 模式 → 沿用上一轮 MAC 列表
- 重新执行 `start_initial_scan` → 清掉旧列表并重新扫描

**关键常量**（ptz_control.py 顶部）：

| 常量 | 默认值 | 说明 |
|------|--------|------|
| `MULTI_SCAN_DWELL_TIME` | 5 | 每个点位停留采集时长（秒） |
| `MULTI_SCAN_EXTEND_TIME` | 3 | 无信号时延长采集时长（秒） |
| `MULTI_SCAN_REPEAT_INTERVAL` | 60 | 两轮之间间隔（秒） |
| `MULTI_SCAN_MAC_REFRESH_ROUNDS` | 5 | 每隔多少轮刷新一次 MAC 列表 |
| `MULTI_SCAN_MAC_MISS_THRESHOLD` | 3 | 连续多少次刷新未出现则淘汰 |
