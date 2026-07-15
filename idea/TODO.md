# 功能待办清单 - WiFi信号定位设备后端

> 与 `ROADMAP.md` 对应，每完成一项请更新状态，避免遗忘。
> 状态：`[ ]` 未开始 | `[x]` 已完成 | `[-]` 暂缓/待明确 | `[~]` 已废弃

---

## 一、硬件部分

- [x] **H1. 云台与设备连接方案**
  - 连接方式：云台侧 + 设备侧 Type-C 母头，标准 Type-C 线材
  - 已确认可正常控制云台
  - ✅ **2026-05-09 测试通过**：CH340 串口识别正常，Pelco-D 协议通信正常，云台可正常移动

- [x] **H2. 摄像头选型要求**
  - UVC 协议，USB 3.0，4K 级别
  - 低畸变镜头，必须支持关闭自动对焦
  - 已确认型号/供应商

---

## 二、软件功能（已完成）

### S1. 全景图采样点算法
- [x] 从移动路径点中筛选最优停留点，最小化拼接次数（Pan 列严格对齐与 30° 硬上限）
- [x] 解决密集矩阵多图拼接崩溃问题（pano→scans 自动降级）
- [x] 广角畸变处理（pano 模式自带球面平整；scans 为 2D 仿射）

### S3-B. 全面扫描运行机制
- [x] 默认不传时间参数时完整执行一轮后停止
- [x] `scan_time_limit` 可选，单位分钟：按时间窗口扫描，到点可提前结束本轮并标记 `PARTIAL`
- [x] `time_interval` 可选，单位秒：窗口/轮次之间等待后继续下一次全面扫描
- [x] 每轮开始时校准一次
- [x] 三段点位扫描：`precheck` 初筛粗扫、`guard` 工作区外扩边、`work` 工作区细扫
- [x] 全面扫描步径改为后端固定：粗筛/外扩边 8°，工作区细扫 4°，前端不再传步径
- [x] 工作区按定向天线 9° 半功率波瓣宽度扩边，`precheck` 排除外扩矩形，`guard` 只扫外扩矩形四条边
- [x] 点位结果新增 `round_id/round_index/phase/area_index/scan_range/scan_step`
- [x] 实时结果新增轮次外层状态：`round_status` / `stop_reason` / `window_index` / `remaining_seconds`

### S3-C. 历史数据存储
- [x] 已落地 SQLite 本地历史库（`projects + snapshots`）
- [x] 全面扫描在停止收尾场景下可写入 `PARTIAL snapshot`
- [x] 数据库类型已确定：SQLite
- [ ] 全面扫描完整一轮跑完后的 `SUCCESS snapshot` 补一次 smoke test

### S5. 时间轴功能（后端）
- [x] 后端已支持按项目查看历史快照列表
- [x] 后端已支持按项目 + 时间戳查询最近快照
- [ ] 前端时间轴页面联调（依赖前端实现）

### S7. 全向天线并行采集
- [x] 环境变量 `CAPTURE_OMNI_INTERFACE`，`OMNI_ENABLED` 开关
- [x] 全向网卡 Monitor 初始化
- [x] `dual_sniff()` 双卡并行 sniff 工具函数
- [x] 所有扫描结果新增 `omni_rssi_avg` / `omni_sample_count` / `omni_status`
- [x] 回归测试（无全向网卡时行为不变）
- [x] 双卡联调真机验证通过

### S8. 客户端扫描
- [x] `scan_clients_at_point` 命令处理（TA=客户端帧过滤）
- [x] `start_client_scan` 命令（guard zone + 逐格移动 + 汇总结果）
- [x] 4 个 API 端点：`client_scan/start` / `stop` / `status` / `result`
- [x] confidence 计算（high/medium/low）
- [x] 信道跳扫机制（不填信道自动全频段跳扫）
- [x] 实测验证通过

### S9. 星链设备识别
- [x] 三特征判定（TPC 63dBm + WMM 字节特征 + Vendor OUI 组合）
- [x] `starlink_detector.py` 实现，集成进 `capture_worker.py`
- [x] Redis `starlink:detected_bssids` 实时写入 + 白名单持久化
- [x] `/api/v1/starlink/detected` 查询接口
- [x] 真机测试通过

### 配置外部化
- [x] 创建 `config.json` 配置文件（中文 key：网卡、云台、摄像头、redis）
- [x] 创建 `config_loader.py` 配置加载器（优先级：环境变量 > config.json > 代码默认值）
- [x] 修改 `capture_worker.py` 加载网卡配置
- [x] 修改 `web_server.py` 加载 redis、ptz 配置
- [x] 修改 `run_manager.py` 加载 redis 配置
- [x] 换设备时只需改 `config.json`，无需修改代码

---

## 三、当前待办（按优先级）

### 🔴 高优先

#### T1. 摄像头接口文档
> 前端自行从摄像头拉流，后端不负责推流，只需确保开机初始化摄像头并告知前端接入方式。
- [x] 确定摄像头流地址、协议、帧率、分辨率等参数
- [x] `run_manager.py` 确保摄像头随系统开机初始化
- [x] 编写 `idea/前端接口_camera.md`：拍照接口 + 坐标转换接口 ✅ **2026-05-15 完成**
- [ ] 前端拉流方式确认（RTSP/HTTP-FLV/WebRTC，待摄像头固件确认）

#### T2. 全景图并行拍摄集成（S1 步骤二）
- [ ] `run_manager.py` 新增启动 `camera_worker` 进程（当前未接入常驻进程）
- [x] `web_server.py` 已支持 `/api/v1/camera/capture` 同步触发普通拍照/全景拍摄
- [ ] `ptz_control.py` / `camera_worker.py` 异步全景拍摄命令接入主流程
- [ ] 选区确认后立即触发，并与用户后续配置并行执行
- [ ] 新增独立全景图状态接口（拍摄中 / 已完成 / 图片路径）
- [x] 前端可通过图片接口获取已生成全景图并展示
- [ ] 【待明确】摄像头 FOV 大小（影响采样点间距计算）

### 🟡 中优先

#### T3. 定位功能后端流程梳理
- [x] 明确"定位"用独立 `location_scan` 流程（新增，不复用 full_area_scan）
- [x] 结果层新增：按目标 MAC 分组，每个 MAC 计算信号最强点位（best_position/best_rssi）
- [x] 新增 API 接口：`location_scan/start` / `stop` / `status` / `result`
- [x] 支持多 MAC（按信道/带宽分组，共用点位扫描）
- [x] 支持多范围（目标区域中心+四角探测；粗扫/细扫均按范围自动扩边）
- [x] 自动信道探测（不指定 channel 时，中心+四角轮询，最多 `probe_rounds_max` 轮）
- [x] 定位扫描新策略真机验证：中心+四角信道探测、多 MAC 共用扫描、最终定位确认、按 MAC 抓包分包 ✅ **2026-05-31 验证通过**
- [x] 定位扫描结果写入 Redis 的最终字段确认：`channel/bandwidth/best_position/best_rssi/verify_rssi/pcap_files` ✅ **2026-05-31 验证通过**
- [ ] 定位扫描资源保护真机验证：低内存/低磁盘停止并上报原因

#### T8. 定位扫描优化
- [x] 快速校验抓包前移动到 best_position，确保定向天线正对目标 ✅ **2026-05-31 完成**
- [x] 最终定位确认跳过重复抓包（快速校验已抓包则跳过） ✅ **2026-05-31 完成**
- [x] 扫描过程中实时更新中间结果到 Redis，供 capture_at_best 实时读取 ✅ **2026-05-31 完成**
- [x] capture_at_best 支持 start_capture 模式保存 pcap 文件 ✅ **2026-05-31 完成**
- [x] capture_at_best 不再等待扫描停止，立即返回 ✅ **2026-05-31 完成**
- [x] 定位扫描被停止时 pan 变量未定义错误修复 ✅ **2026-05-31 完成**

#### T9. capture_at_best 一键最强点抓包
- [x] 新增 API 接口：`/api/v1/ptz/capture_at_best` ✅ **2026-05-31 完成**
- [x] 从 Redis 查找目标 MAC 的最强点位（定位扫描优先，全面扫描其次）
- [x] 停止当前扫描（设置停止标志，不等待）
- [x] 移动云台到最强点
- [x] 开始抓包，pcap 文件保存到 `/mnt/data/`
- [x] 真机验证通过 ✅ **2026-05-31 验证通过**

#### T4. multi_point_scan 接口下线
- [x] `web_server.py` 中 `multi_point_scan` 相关接口标注废弃或移除对外暴露
- [x] `前端接口.md` 对应章节标注废弃
- [x] 后端代码暂保留，不删除 ✅ **2026-05-18 已关闭接口**

#### T7. 全面扫描目标区域判断算法
- [x] 扫描数据结构已区分 `precheck` / `guard` / `work` 阶段 ✅ **2026-05-25 完成**
- [x] 基于同一 `round_id` 内点位坐标归属、RSSI 差值、命中点数与定向/全向差值，判断 MAC 是否疑似位于目标工作范围内 ✅ **2026-05-21 完成**
- [x] 输出前端直接读取的按轮次白名单：`full_scan:whitelist:round_{N}` 与 `/api/v1/ptz/full_area_scan/whitelist` ✅ **2026-05-21 完成**
- [ ] 真机验证 8° 粗筛、9° 外扩边、4° 细扫对边界误判的抑制效果，确认默认值是否需要调整

### 🟢 低优先

#### T5. 全景图↔云台角度映射
- [x] 实现全景图像素坐标 ↔ pan/tilt 角度双向映射算法（线性映射，等距矩形投影）
- [x] 暴露接口给前端：`coordinate_convert` + `mode: "panorama"` ✅ **2026-05-16 完成**
- [x] 全景图命名带角度范围：`panorama_pan{min}-{max}_tilt{min}-{max}_{时间戳}.jpg`
- [x] 全景图 metadata 自动存 Redis `camera:last_panorama_meta`
- [x] `config.json` 新增 `全景像素每度` 配置（默认 36，全景图高度 1080）
- [x] Hugin 全景运行时接入：`hugin_panorama_runtime.py` 生成 `pixel_map.pmap` 供 `coordinate_convert` 作为权威映射；`coordinate_map.npz` / `correction_map.npz` 保留为 legacy/fallback
- [x] `.pmap` 二进制映射读写工具：新增 `pmap_utils.py`，支持 `PMAP0001` dense planar 文件与 `.pcand` 候选诊断格式
- [x] 改造版 Linux `nona-pixelmap` 生成 `pixel_map.pmap`：`hugin_panorama_runtime.py` 通过 `HUGIN_BUILD_PMAP` / `HUGIN_NONA_BIN` 调用 `nona --pixel-map`
- [x] Hugin mapping bundle 导出：复制 `pixel_map.pmap`、PTO、source images、诊断脚本和 README 到 `/home/ultiwill/hugin/<bundle>/`
- [ ] 真机验证 `.pmap` 的 `pixel_center_offset=0.0/0.5` 精度差异，并把最终值写入 metadata 约定
- [x] 将 `/api/v1/camera/coordinate_convert` 的 Hugin 主反查源从 `coordinate_map.npz` 切换到 `.pmap`，并为 `angles -> pixels` 增加进程内 PMAP owner/source 分桶反查索引
- [ ] 真机验证 PMAP 双向闭环：`pan/tilt -> pixel -> pan/tilt` 误差应收敛到像素近邻级，不再出现多度偏差
- [ ] 边界中心补拍、`left_boundary/right_boundary/stitch_role` 与非对称裁剪接入评审（当前未接入主流程）

#### T6. 远距离偏移修正（S2）
- [ ] 【待明确】偏差为固定值还是用户可校准
- [ ] 实现摄像头与天线固定高度差的偏差修正算法
- [ ] 修正影响：十字准心叠层位置 + 结果标注映射准确性

---

## 四、暂缓功能

### [-] S3-A. 多点扫描重复机制
> 暂缓原因：功能已被新"定位"模块覆盖，前端不再接入 multi_point_scan 入口。
> 后端代码保留，不删除。

### [-] S4. 卫星图联动与空间映射
> 暂缓原因：卫星图来源未明确，后续可能用到，待明确后再规划。

### [-] S6. 多点位数据融合与测向
> 暂缓原因：依赖测向算法方向明确后再规划。

---

## 五、接下来要做（最新）

### 1. 硬件
- [x] **1.1 完成 Type-C 母头的 3D 建模和打印**（已完成）
- [x] **1.2 测试 Type-C 母头功能** ✅ 2026-05-09 测试通过（云台控制正常、摄像头识别正常）
- [ ] **1.3 固定摄像头的 3D 建模和打印**
- [ ] **1.4 固定定向天线的 3D 建模和打印**
- [ ] **1.5 全向天线固定件打印**（2026-05-11 计划）

### 2. 软件
- [x] **2.1 完成全景图拍摄** ✅ 2026-05-16 完成（step4 验证通过）
- [x] **2.2 完成全景图拍摄与实际角度映射** ✅ 2026-05-16 完成（step6 验证通过，线性映射准确；Hugin 坐标映射与 `.pmap` 生成链路后续已接入验证）
- [ ] **2.3 软件部分完成后进行整体测试**

### 3. 测试
- [ ] **3.1 云台自检死角测试**（2026-05-11 计划）

### 4. 新增功能规划
- [ ] **4.1 卫星图联动**：全景图与卫星图叠加，实现空间定位
- [ ] **4.2 多设备全景图拼接联动**：多台设备协同拍摄，拼接更大范围的全景图
- [ ] **4.3 RSSI 场景分析**：全向天线+定向天线的 RSSI 不同场景分析，判断目标信源的区域分布情况（概率室内/室外/其余分布）

---

## 六、设备类型识别（type/subtype/ssid 字段）

全面扫描结果中，每个 MAC 新增 `type` / `subtype` / `ssid` 字段：

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

---

## 七、全面扫描三段点位扫描（precheck/guard/work）

- [x] 初筛范围优先读取 Redis `gimbal:default_config.work_pan_range/work_tilt_range`
- [x] Redis 未提供 `work_*` 时，初筛范围回退到 `pan_range/tilt_range`
- [x] 目标工作范围支持旧单范围 `pan_range/tilt_range` 和新多范围 `work_ranges[]`
- [x] 每轮按 `precheck` / `guard` / `work` 顺序扫描
- [x] 粗筛/外扩边固定 8°，工作区细扫固定 4°
- [x] `precheck` 排除工作区外扩 9° 矩形，`guard` 只扫外扩矩形四条边
- [x] 启动参数支持 `scan_time_limit`（分钟）与 `time_interval`（秒）
- [x] `SUCCESS` 轮次结束后生成按轮次白名单，只对完成轮次做筛选
- [x] 筛选时按坐标是否落在 `work_ranges` 判断工作区，解决 `precheck` 与 `work` 重叠问题
- [x] 同一轮相同/接近坐标的重复命中按坐标桶去重，RSSI 取该坐标桶内最强值

---

*最后更新：2026-06-21*
