# AI 协作操作规范

> 本文件记录与 AI 协作开发时需要遵守的操作约定，确保每次修改可追溯、可回滚。

---

## 1. Git 提交策略

### 规则

**按修改规模决定提交时机，不要求每次小改动都单独提交。**

#### 大范围修改（新功能、跨文件重构、影响主流程的改动）

```
1. git add -A
2. git commit -m "chore: 修改前快照 - <简述问题>"   ← 修改前先快照
3. 进行代码修改
4. git add -A
5. git commit -m "<前缀>: <详细说明>"               ← 修改后立即提交
```

#### 小范围修改（单文件小调整、常量微调、注释修改等）

- 可以**连续做最多 3 次小改动后**再统一 commit，无需每次都提交
- 第 3 次改动完成后必须提交，不允许继续累积
- commit message 简要列出这几次改动即可

```
git add -A
git commit -m "chore: 小调整 - <改动1> / <改动2> / <改动3>"
```

### 判断标准

| 类型 | 判断依据 | 策略 |
|------|----------|------|
| 大范围 | 新增功能模块、改动 > 50 行、跨多文件、影响主流程 | 修改前快照 + 修改后立即提交 |
| 小范围 | 单文件微调、< 20 行、不影响主流程 | 最多积累 3 次后统一提交 |

### 原因

- 大改动风险高，修改前需保留退路
- 小改动频繁提交反而噪音多，批量提交更清晰
- 超过 3 次累积会导致 commit 内容太杂，难以追溯

### commit message 格式

| 前缀 | 用途 |
|------|------|
| `fix:` | 修复 Bug |
| `feat:` | 新增功能 |
| `refactor:` | 重构（不改变行为） |
| `chore:` | 杂项（快照、依赖更新等） |
| `docs:` | 文档修改 |

示例：
```
fix: 修复初始扫描停止命令不生效（stop_initial_scan 改为直接写 Redis 标志位）
fix: 删除 start_initial_scan 中步骤2的冗余限位校准
chore: 修复前快照（停止初始扫描阻塞问题待修）
```

---

## 2. 停止命令设计规范

### 问题背景

`ptz_worker` 在执行长耗时任务（初始扫描、多点扫描等）时，会**同步阻塞**在 `brpop` 或 `sniff()` 调用中，此期间无法读取命令队列。  
如果停止接口只把命令塞入命令队列，ptz_worker 根本无法感知，形成死锁。

### 规范：停止接口必须双管齐下

```python
# ① 直接写 Redis 标志位 —— 让阻塞中的 worker 通过轮询立刻感知（主要手段）
r.set('xxx:stop_yyy', '1', ex=120)

# ② 同时塞命令队列 —— 让 worker 空闲时做状态收尾（辅助手段）
r.lpush(PTZ_COMMAND_QUEUE, json.dumps({"action": "stop_yyy"}))
```

### 已实现的停止标志一览

| 停止标志 Key | 用途 |
|---|---|
| `multi_scan:stop_initial_scan` | 停止初始扫描 |
| `multi_scan:stop_multi_point_scan` | 停止多点扫描 |
| `multi_scan:stop_full_area_scan` | 停止全面扫描 |
| `location_scan:stop` | 停止定位扫描 |

### 阻塞等待必须使用轮询而不是单次 brpop

**错误写法**（阻塞 300 秒，停止命令无法响应）：
```python
result = r.brpop('xxx:notify', timeout=300)
```

**正确写法**（每 3 秒检查一次停止标志）：
```python
while time.time() - start < 300:
    if r.get('xxx:stop_flag'):
        break
    result = r.brpop('xxx:notify', timeout=3)
    if result:
        # 处理结果
        break
```

---

## 3. 校准（限位点）调用规范

### 问题背景

`_goto_calibration_point` 是耗时约 30 秒的操作（需要云台移动到物理限位点再返回），  
如果在同一个流程里重复调用，会导致用户看到"去了两次限位点"的异常行为。

### 规范

每个扫描流程（初始扫描、多点扫描每轮开始）**只调用一次** `_goto_calibration_point`，  
不允许在同一流程的不同步骤重复调用。

---

## 4. Scapy sniff 与 promiscuous mode 说明

### 现象

`dmesg` 中反复出现：
```
wlxXXX: entered promiscuous mode
wlxXXX: left promiscuous mode
```

### 原因

- **Monitor Mode**（`iw set type monitor`）：只需设置一次，系统会保持
- **Promiscuous Mode**：由 Scapy/libpcap 的 raw socket 自动管理，每次 `sniff()` 启动时开启，结束时关闭

因此每个信道的 `sniff(timeout=dwell_time)` 结束后，内核会自动报告 `left promiscuous mode`，  
下一个信道 `sniff()` 开始时报告 `entered promiscuous mode`。**这是正常行为，不影响功能。**

---

## 5. 全面扫描 / 定位扫描修改前检查清单

> 这两个功能是当前项目主流程。后续 AI 接手时，禁止只看一个文件就改逻辑，必须先按下面顺序确认入口、执行、抓包、结果写入。

### 必读顺序

1. 先读 `idea/ARCHITECTURE.md` 的「T3 定位扫描」和「全面扫描」相关章节。
2. 再看 `web_server.py`：确认 API 入参、默认值、点位估算和写入 Redis 命令的结构。
3. 再看 `ptz_control.py`：确认云台执行、扫描阶段、停止标志、结果与白名单写入。
4. 涉及抓包、信道切换、RSSI、全向/定向数据时，再看 `capture_worker.py`。
5. 涉及启动残留状态时，再看 `run_manager.py` 的启动清理逻辑。

### 全面扫描重点文件

| 文件 | 重点位置 | 说明 |
|------|----------|------|
| `web_server.py` | `_build_full_area_scan_plan()`、`/api/v1/ptz/full_area_scan/start` | API 层生成 precheck / guard / work 估算计划，给前端返回点位和耗时 |
| `ptz_control.py` | `_build_full_area_segments()`、`start_full_area_scan` 分支 | 真正生成并执行三段扫描点位 |
| `ptz_control.py` | `_build_full_scan_whitelist_payload()`、`_write_full_scan_whitelist_result()` | 成功轮次结束后的白名单筛选和 Redis 写入 |
| `capture_worker.py` | `discover_macs_for_full_scan` 对应分支、`dual_sniff()` | 每个点位抓包，定向网卡为主，全向网卡只补充 omni 字段 |
| `rebuild_full_scan_whitelist.py` | 脚本入口 | 不重扫，基于已有 `full_scan:round_N_results` 重新生成白名单 |

### 定位扫描重点文件

| 文件 | 重点位置 | 说明 |
|------|----------|------|
| `web_server.py` | `_auto_location_step()`、`/api/v1/ptz/location_scan/start` | API 层读取目标 MAC、区域、探测参数，后端固定估算 16/8/4 度步径 |
| `ptz_control.py` | `start_location_scan` 分支 | 定位扫描主流程：信道探测、三轮扫描、最终定位、可选抓包 |
| `ptz_control.py` | `_group_location_configs()` | 按 `(channel, bandwidth)` 分组，多 MAC 共用同一点位扫描 |
| `ptz_control.py` | `_build_fine_boxes()` | 根据上一轮强信号点生成下一轮候选框 |
| `capture_worker.py` | `detect_channels` 分支 | 信道探测；当前逻辑找到所有目标 MAC 后会提前结束该点位探测 |
| `capture_worker.py` | `scan_at_point` 分支 | 定位扫描阶段点位采样，支持 `stop_key='location_scan:stop'` |

### 修改约束

- 不改 API 请求/响应结构时，不要更新 `idea/前端接口.md`。
- 测试命令、日志过滤命令写到 `idea/全面扫描_本机测试与日志过滤命令.md`，不要混到前端接口文档。
- 全面扫描路径逻辑必须同时检查 `web_server.py` 的估算计划和 `ptz_control.py` 的实际执行路径，避免前端看到的点位数和实际扫描不一致。
- 定位扫描涉及停止、通知队列或信道探测时，必须同时检查 `ptz_control.py` 与 `capture_worker.py`。
- 改完后至少运行：`python -m py_compile web_server.py ptz_control.py capture_worker.py config_loader.py run_manager.py`；如果改了白名单重建脚本，也把 `rebuild_full_scan_whitelist.py` 加进去。
- 纯文档更新不需要 git；代码改动是否提交按本文件 Git 提交策略执行。

---

## 6. 其他待补充规范

> 后续遇到新问题或新约定，在此继续追加。

---

## 7. 每次代码更新的完整操作规范（强制执行）

> **适用场景**：任何对后端代码的修改（修复 Bug、新增功能、重构、配置变更等）。

### 操作顺序

```
步骤一（仅大范围修改需要）：修改前快照
  git add -A
  git commit -m "chore: 修改前快照 - <简述要解决的问题>"

步骤二：执行代码修改

步骤三：提交本次修改
  - 大范围修改：立即提交
  - 小范围修改：最多积累 3 次后统一提交
  git add -A
  git commit -m "<前缀>: <详细说明修改内容和原因>"

步骤四：更新 idea/ 目录下对应的 md 文件
```

### 步骤四：md 文件更新规则

| 文件 | 何时更新 | 更新内容 |
|------|----------|----------|
| `STATUS.md` | **每次都要** | 更新「当前停在哪里」「下一步」「已完成功能清单」 |
| `TODO.md` | 有任务完成或新增时 | 将已完成项标记为 `[x]`，新增项写为 `[ ]` |
| `ARCHITECTURE.md` | 文件结构/进程/Redis Key/API 有变化时 | 更新对应速查表和说明 |
| `前端接口.md` | API 请求/响应结构有变化时 | 更新对应接口文档和示例 |
| `DESIGN_S*.md` | 对应模块的设计思路有实质变更时 | 追加变更记录或修订对应章节 |
| `ROADMAP.md` | 新增功能需求或需求有调整时 | 补充或修订需求描述 |

### STATUS.md 更新模板

```markdown
## 当前停在哪里

**日期**：YYYY-MM-DD

**本次完成**：
- <简述本次修改内容>

**当前焦点（正在进行中）**：
- <下一个待处理事项>

**下一步 (即刻计划)**：
1. <具体行动项>
```

### 示例

```bash
# 1. 快照
git add -A
git commit -m "chore: 修改前快照 - capture_worker 双卡并行待真机测试"

# 2. 修改代码...

# 3. 提交
git add -A
git commit -m "feat: S7 全向天线真机测试通过，补充 omni 字段单元测试"

# 4. 更新 md
# - STATUS.md：更新当前进度
# - TODO.md：S7-8 S7-9 标记为 [x]
```

### 为什么必须同步更新 md

- AI 每次会话都以 `STATUS.md` 为入口判断当前进度
- md 文件落后于代码 → AI 下次会给出错误的上下文判断
- **md 文件就是项目的"实时地图"，代码改了地图必须跟着改**
