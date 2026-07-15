# 历史数据存储与时间轴架构设计 (S3-C & S5)

## 0. 2026-03-12 实际落地状态

当前代码已经先按 **V1 简化版** 落地，和本文最初的“独立 `History Worker` + 单窄表 `scan_history`”方案相比，有以下实际变化：

- 已实现本地 SQLite 历史库，但当前落地结构是 **`projects` + `snapshots` 两张表**，不是单表 `scan_history`。
- 当前写入方式是 **扫描流程内直接落库**：
  - 启动扫描时创建 `project`
  - 每轮扫描结束时写入 `snapshot`
  - 停止扫描并完成收尾时结束 `project`
- 当前已经落库并实测通过的快照字段包括：
  - `project_id`
  - `round_id`
  - `captured_at`
  - `scan_type`
  - `status`
  - `raw_dir`
  - `grid_data_json`
  - `render_meta_json`
- 当前真机 smoke test 已验证：
  - `multi_point` 项目可创建、停止并写入 `projects`
  - 最近快照可通过 `/api/v1/history/projects/<project_id>/snapshot` 查询
  - SQLite 中 `projects` / `snapshots` 均可查到测试记录
  - `full_area` 项目可创建、停止并写入 `projects`
  - `full_area` 在停止场景下可落一条 `PARTIAL` 状态快照，且已验证 `round_id` 与 `raw_dir` 可追踪
- 当前仍未闭环的部分：
  - `panorama_path` / `thumbnail_path` 还没有真正接到全景拼接产物链路
  - `full_area` 完整一轮跑完后的 `SUCCESS` 快照还可以再补一次真机 smoke test
  - 前端时间轴页面还未完成联调

因此，这份文档当前应理解为：
- **上半部分是长期目标架构**
- **本节描述的是 2026-03-12 已经实际落地的 V1 方案**

## 一、 核心架构总结 (Edge Device Optimized)

综合业务需求、前端时间轴体验，以及**树莓派 (Raspberry Pi)** 等边缘设备算力弱、内存小、易受 I/O 拖垮的硬件约束，本（V2）版历史数据系统采用：
**“异步独立 Worker + 单写者 SQLite + 纯文件系统存图 + 前端重计算”** 的极度解耦架构。

**核心设计原则 (The Raspberry Pi Way)：**
- **稳字当头**：先保证长期运行不 OOM、不饿死主线程、不卡爆磁盘，再追求花哨的查询特性。
- **重器轻用**：后端绝不搞复杂的 JSON SQL 查询和实时图片渲染，让树莓派回归最纯粹的“取文件路径 + 扔 JSON 文本”的苦力活，一切可视化的合并计算通通**甩给性能几百倍于它的前端浏览器**。

**关键组件拆解：**
- **图片存储**：这轮生成的高清全景、缩略图，按时间戳规范直存本地文件系统。**数据库里绝不塞一滴图片的二进制流**。
- **数据元存储**：网格热力（`grid_data`）、渲染配置（`render_meta`）在入库前就强行序列化为纯字符串（`TEXT`）。不依赖 SQLite 版本自带的 JSON 解析扩展，提升在各种古怪固件上的兼容性。
- **任务极度解耦 (Worker 职责边界定死)**：
  - `PTZ Worker` 与 `Capture Worker`：只管完成一轮扫描、更新运行状态和保留本轮原始结果，不直接写历史库。
  - `Panorama Worker`（现有）：**专职负责矩阵图拼接**。只有当它完成本轮拼图结果判定后，才往 `history:pending_queue` 推送轻量历史任务。
  - `History Worker`（新增）：**纯粹的“组装入库”工**。拿到拼接完成后的路径和网格数据，生成缩略图，计算渲染元数据，单写者执行 SQLite 落库和文件归档。绝不越权抢拼图的活。

## 二、 数据库表结构设计

仅需建立唯一核心窄表：`scan_history`

| 字段名 | 数据类型 | 说明 | 索引建设建议 |
| :--- | :--- | :--- | :--- |
| `id` | INTEGER | 自增主键 | PRIMARY KEY |
| `round_id` | TEXT | **全局追踪标识**（如 `full_20260311_153000_abcde`）。查日志、查图片、查回放和防并发冲突的神器 | UNIQUE |
| `captured_at` | DATETIME | **业务时间锚点（统一存 UTC 零时区）**，建议格式固定为 ISO8601 UTC，如 `2026-03-11T15:30:00Z` | 单列 INDEX |
| `scan_type` | TEXT | 扫描类型：`full_area` / `multi_point` / `intelligent` | 联合索引 (type, time) |
| `target_mac` | TEXT | 若是追踪某特定设备则记录 MAC，否则全域扫描记为 `ALL` | 联合索引 (mac, time) |
| `status` | TEXT | 采集落库的业务状态：`SUCCESS` / `PARTIAL` / `FAILED` | - |
| `archive_state` | TEXT | **生命周期状态（资源削减标志）**：`COMPLETE`（原片大图均在） / `NO_RAW`（已删矩阵图） / `THUMB_ONLY`（仅剩缩略图） | - |
| `panorama_path` | TEXT | **高清大图的相对路径**，如 `history/pano/20260311/full_xxx.jpg` | - |
| `thumbnail_path` | TEXT | **极致压缩缩略图的相对路径**，如 `history/thumb/20260311/full_xxx.jpg`，时间轴快速拖动加载时的主力 | - |
| `raw_dir` | TEXT | （可选）若保留拼接前的矩阵原图，记录原图目录的相对路径，如 `history/raw/20260311/full_xxx/` | - |
| `grid_data_json` | TEXT | **信号快照载体**：直接把这轮收集的网格信号字典存为普通文本 JSON。抛弃对 SQLite JSON 查询扩展的强依赖，增强环境鲁棒性 | - |
| `render_meta_json` | TEXT | **渲染回放生命线**：记录拼接和生成时的水平/俯仰物理范围、步长、全景图实际宽高、是否用了天线偏差修正。前端据此复原历史画布，避免图层错位 | - |
| `config_json` | TEXT | 当时的系统配置信息快照存底，追溯“这轮系统是怎么配的” | - |
| `schema_version` | INTEGER | 预留版本号，便于后期随业务演进做表结构热升级（平滑迭代必备） | - |

## 三、 后台异步安全写入流 (The History Worker)

树莓派上最危险的就是“并发乱写”和“大对象撑爆内存”。因此，引入全局唯一的**单写者模型（Single-Writer）**：

1. **轻量级信号交接**：当 `Panorama Worker` 完成一轮拼图结果判定后，**千万不要**把那几百个网格组成的大字典直接 `json.dumps()` 扔进 Redis（会撑爆内存并吃满序列化 CPU）。只往队列 `history:pending_queue` 推一个轻任务包，例如：`{"round_id": "full_xxx", "captured_at": "2026-03-11T15:30:00Z", "scan_type": "full_area", "target_mac": "ALL", "panorama_relpath": "history/pano/20260311/full_xxx.jpg", "raw_dir_relpath": "history/raw/20260311/full_xxx/"}`。
2. **异步摄取与单点组装**：后台唯一的 `History Worker` 拿到这个轻任务包后，**自己**去 Redis 或临时文件里把本轮网格数据捞出来拼装好。
3. **安全落盘防断电**：`History Worker` 低优先级生成缩略图。**黄金法则：** 在树莓派这种随时可能拔电源的设备上，文件先打入临时文件（如 `thumb.tmp`），所有落盘动作成功后，再一次性系统原语 `rename` 为正式文件。
4. **SQLite 运行参数固定化**：`History Worker` 启动后统一执行：
   - `PRAGMA journal_mode=WAL;`
   - `PRAGMA synchronous=NORMAL;`
   - `PRAGMA busy_timeout=5000;`
   以获得树莓派环境下足够稳妥的事务一致性和写入容忍度。
5. **朴素的事务锁库**：最后阶段，提取上述路径和 `render_meta`、`grid_data` 纯文本，套上最基础的 SQLite `BEGIN ... COMMIT`，一条干净利落的 `INSERT` 解决战斗。
6. **完整闭环**：文件不落定，就不进库；一旦进库，文件必定安好无损。

## 四、 读工作流对接 (支撑 S5 时间轴交互)

当客户在前端时间轴拖拉到指定时间（如 `2026-03-11 15:30:00`）停下：

1. 前端向后端发起形如 `GET /api/v1/history?timestamp=2026-03-11T15:30:00Z`。
2. 后端采用轻小快的 SQL 提取该时间点及以前**最新一次成功**的快照：

```sql
SELECT
    round_id,
    panorama_path,
    thumbnail_path,
    render_meta_json,
    grid_data_json,
    archive_state
FROM scan_history
WHERE captured_at <= ? AND status = 'SUCCESS'
ORDER BY captured_at DESC
LIMIT 1;
```

3. 前端利用 Canvas/WebGL 机制：
   - 先拿 `thumbnail_path` 瞬间占满屏幕，避免白屏
   - 解析 `render_meta_json`，确认物理坐标尺寸刻度
   - 若 `archive_state != 'THUMB_ONLY'` 且 `panorama_path` 存在，则静默预加载高清大图
   - 若 `panorama_path` 为空、文件缺失，或当前仅剩缩略图，则保持缩略图模式继续叠加热力图
   - 最后遍历 `grid_data_json`，**用前端算力而非后端算力** 在底图上叠加热力网格

*注：此 SQL 语义被严格定义为“查出用户拖拉到的时间点及以前，系统拥有的最新一次成功快照”，而不是“前后双向最近”。且不以 `archive_state` 作为前置过滤，确保哪怕大图已被清理，只要缩略图仍在也能被查出展示。*

## 五、 容量护城河 (生命周期与清理策略)

长期来看系统真正的体量挑战在于“硬盘里的图片”，而非 SQLite 本身。为了稳定必须引入 GC（垃圾清理）策略。

1. **阶梯式截断清理**：每天后台在低峰期（如凌晨 3 点），清理 `captured_at` 超过 X 天（默认 7 天 / 15 天可配）的历史记录。
2. **清理优先级瀑布流**：
   - 首当其冲：先干掉对应的极占空间的 `raw_dir`（原始矩阵照），记录的 `archive_state` 变更为 `NO_RAW`
   - 次级清理：删除 `panorama_path`（高清大图），记录的 `archive_state` 变更为 `THUMB_ONLY`
   - 绝命保留：只要 SQLite 记录在，极小的 `thumbnail_path` 和 `grid_data_json` 就绝不删除，供时间轴长期回溯

---
*版本: v1.2.0, 状态: V1 已落地（MVP），后续继续向完整架构收敛*

---

## 2026-04-01 新增设计决策记录

### S7 全向天线并行采集

**问题**：单张定向天线网卡无法判断目标远近。

**决策**：新增第二张全向天线网卡，在 `capture_worker.py` 内部通过并行线程同时抓包，不新增进程。

**关键设计点**：
- 环境变量 `CAPTURE_OMNI_INTERFACE` 控制启用，为空则完全不启用（向后兼容）
- 两张网卡同信道、同时间窗口并行 `sniff()`，RSSI 严格可比
- 全向网卡任何故障不影响定向天线正常采集
- 后端不做远近判断，只输出双路 RSSI 数据
- 输出字段：`omni_rssi_avg` / `omni_sample_count` / `omni_status`
- `render_meta` 新增 `omni_enabled` 字段，通过 Redis `capture:omni_enabled` 传递

**影响文件**：`capture_worker.py`（主要）、`ptz_control.py`（render_meta）

### MAC 淘汰机制（auto 模式）

**问题**：原有"只增不减"策略导致偶然抓到的 MAC 永久留在列表中，每轮都要扫描，浪费时间。

**决策**：给每个 MAC 加 `_miss_count` 计数器，连续 N 次刷新扫描未出现则淘汰。

**关键设计点**：
- 常量 `MULTI_SCAN_MAC_MISS_THRESHOLD = 3`（默认连续 3 次未出现则淘汰）
- 设为 0 则禁用淘汰，回退到只增不减模式
- manual 模式完全不受影响
- `_miss_count` 字段存在 `multi_scan:target_macs` 的 JSON 中，可通过 Redis 直查

### 刷新扫描回初始点

**问题**：刷新扫描（discover_macs）原来在随机位置做，每次天线朝向不同，发现的 MAC 集合不一致，淘汰判断不公平。

**决策**：进入多点扫描主循环前记录用户手动移到的云台位置，每次刷新前先回到该位置。

**关键设计点**：
- `_initial_scan_pan` / `_initial_scan_tilt` 在主循环前通过 `ptz.get_position()` 记录
- 每轮流程顺序：校准 → 回初始扫描位置（仅刷新轮次）→ 刷新 MAC → 正式扫描
- 移动失败时降级为在当前位置刷新，不中断扫描

### MAC 列表跨项目行为

**现状记录**（非设计决策，仅记录当前行为）：
- 停止多点扫描后，`multi_scan:target_macs` 仍保留在 Redis 中
- 直接再启动 auto 模式 → 沿用上一轮 MAC 列表
- 重新执行 `start_initial_scan` → 清掉旧列表并重新扫描
- 此行为暂未改动，后续可根据需要调整
