# 2026-07-03 全面扫描停止状态机 race condition

> 文件命名建议：`YYYY-MM-DD_HHMM_问题简述---状态.md`
> 固定状态：`待排查` / `排查中` / `待验证` / `已解决` / `暂缓` / `无法复现`
> 创建条件：仅在用户明确标记或确认需要沉淀时创建。
> 时间格式：`YYYY-MM-DD HH:mm`，默认时区 `Asia/Shanghai`

## 问题概述

- 时间：2026-07-03 00:15
- 状态：已解决
- 影响范围：全面扫描 stop 路径在 dwell 抓包期间触发后，worker 未能立即停止，导致后续点位继续执行；HTTP 视图与 Redis 状态进入 `active=True + terminal=False + reason=manual_stop` 的脏态；后续 start_scan 持续返回 409 CONFLICT
- 简单描述：smoke 压力测试（100ms 内 stop）发现 worker 实际停止延迟数秒，并出现 active 残留与 stop key 丢失同时存在的脏状态
- 关联计划：`plan/2026-06-24_全面扫描路径与偏差区优化计划.md`
- 关联 Todo：Todo 24（停止语义）

## 现场现象

- 触发条件：smoke4 / smoke5 测试用 P1（最小点位 + 单信道 ch=36）启动全面扫描，100ms 延迟后调用 POST /api/v1/ptz/full_area_scan/stop
- 期望结果：worker 在 2 秒内停止，state=stopped/terminal=true，active=false，后续 start_scan 可正常启动
- 实际结果：worker 持续执行 point_3、point_4、fine_0_point_0、deviation_a_point_1~4 等后续点位；最终观察到 `active=True, terminal=False, reason=manual_stop, stop_key_TTL=-2` 的脏状态；后续 start_scan 持续返回 HTTP 409 CONFLICT
- 相关命令 / 接口 / 日志：

```text
23:54:10   smoke4 启动 full_1783007650715 (POST /start, 200)
23:54:11   第1轮开始
23:55:03-  capture worker 开始 point_0..point_4, fine_0_point_0
23:55:49   fine_0_point_0 开始
23:56:13   dev_a_point_1 开始 (进入偏差区!)
23:56:18   dev_a_point_2
23:56:24   dev_a_point_3
23:56:30   dev_a_point_4
[stop API 23:54-23:56 期间的调用未被 journal 记录，但 smoke4 脚本确认已调用]
23:57:47   POST /start 200 - 启动 full_1783007867200 (smoke5)
23:58:14   smoke5 开始第1轮粗扫
23:58:17   POST /stop 200 - 调用 stop
23:58:17.334  worker: 移动失败 reason=stopped
23:58:17.336  worker: 🛑 移动期间收到停止命令 manual_stop
23:58:17.347  worker: 手动停止：跳过项目快照持久化以满足 2 秒停止预算
23:58:17.439  worker: 项目已结束 -> STOPPED
23:58:17   POST /start 200 - 立即启动 full_1783007897625
23:58:31   POST /start 409 CONFLICT
23:58:43   POST /start 409 CONFLICT
```

脏状态快照（force cleanup 前）：

```json
{
  "scan_id": "full_1783007650715",
  "state": "running",
  "active": true,
  "terminal": false,
  "stop_requested": false,
  "reason": "manual_stop",
  "stop_key_TTL": -2,
  "active_scan_id": "full_1783007650715"
}
```

## 排查记录

### 2026-07-03 00:15

- 操作：测试 AI 用 P1 配置 smoke4（100ms 延迟 stop）+ smoke5 启动后观察到 stop 后仍持续执行 capture 点位；读取 journal + Redis + HTTP 状态字段
- 观察：worker 在 smoke4 stop 后实际延迟到偏差区 point_4 才开始响应（与 stop 调用时间相差约 2 分钟）；HTTP 状态进入 `active=True + terminal=False + reason=manual_stop` 的脏态；force cleanup 后 worker 自动恢复 idle
- 结论：smoke4 stop 调用未被及时处理；smoke5 stop 调用被 worker 正常处理（0.5 秒内停止）；两者都观察到后续 start_scan 返回 409
- 下一步：暂停压力测试，等待开发 AI 分析 stop 信号延迟 / 状态收口 / 视图兜底三处代码

### 2026-07-03 00:25

- 操作：逐项检查 `_full_scan_stop_reason_from_redis()`、全面扫描启动交接、point-priority dwell、任务最终收口及 `_project_full_scan_stop_status()`。
- 观察：
  - stop 读取函数遇到不匹配 `scan_id` 时会主动删除 `full_scan:stop` / legacy stop，读取者越权消费了其他任务的停止信号。
  - Web 启动流程原本分开执行 active ID 发布与命令入队；PTZ worker 接手后还会再次删除 legacy/capture stop，存在早期 stop 被清掉的窗口。
  - point-priority dwell 的 Scapy 切片为 0.5 秒，不满足本问题要求的 0.4 秒内观察 stop。
  - PTZ 最终终态仍从 Redis stop key 重新取 reason；key 被删除后会丢失已经观察到的 `manual_stop`。
  - HTTP 视图只在 stop key 仍存在时兜底；stop key 已消失但项目仍为 `STOPPING` 时无法修复历史脏态。current project 序列化还遗漏 `updated_at`，原有僵死超时逻辑实际上无法计时。
- 结论：根因不是 WATCH 缺失，而是 stop key 所有权、早期启动交接、停止原因持久性和历史脏态兜底共同存在缺口。
- 修复：
  - stop 读取者不再删除任何不匹配 key；只有新任务启动者负责换代。
  - Web 使用单个 Redis 事务原子执行“清旧 stop → 发布新 active_scan_id → 命令入队”；PTZ worker 接手后不再清 stop。
  - full-scan dwell 切片缩短为 0.1 秒。
  - PTZ 首次观察到 stop 后在任务内存中锁存 reason，最终收口不再依赖 Redis key 存活。
  - HTTP 视图同时依据匹配 stop key 与 `history:current_project=STOPPING` 投影状态；超过2秒即自愈为 `stopped/terminal=true`，按值条件删除遗留 active ID 并释放项目门禁。
  - 同一 scan_id 的终态增加单调保护，迟到进度不能复活。
- 验证：AST 语法检查通过；停止专项 24/24、相关全面扫描回归 95/95，另含 35 个子测试通过。
- 下一步：在真实设备复跑 P1 100ms stop、dwell stop、多信道切换 stop 与连续20轮压力测试。

### 2026-07-03 09:31

- 操作：复核测试 AI 的完整设备报告，重点分析专项四 sub5 与专项七 P2 的 2.1~2.37 秒终态耗时。
- 观察：
  - smoke4 10/10、dwell 10/10、多信道 10/10、stop 所有权 sub1~4 4/4、历史自愈 6/6、终态防复活 5/5 均通过；P2 另有17轮通过。
  - sub5 中 B 在启动后2秒未观察到 running，60秒后出现不同 scan_id。代码不存在自动重启全面扫描路径；每个新 `full_*` 只能由新的 `/start` 请求产生，因此第三个 scan_id 必须结合 Web access log 和测试脚本 start 调用定位，当前不能判为旧 stop 误伤。
  - P2 的 stop→terminal 集中在 2146~2377ms，时间特征表明正常 PTZ 收口未赶在2秒前完成，由 HTTP auto-heal 兜底。
  - `_wait_full_scan_point_notify()` 每轮先检查 stop、随后仍可能阻塞 `BRPOP timeout=1`；stop 若落在二者之间会额外等待近1秒，再叠加配置会话清理，解释了 P2 的集中超时。
- 结论：此前根因修复已通过大部分真机验证；仍有一个真实的 PTZ 点位通知阻塞延迟。sub5 暂按测试时序/请求链路证据不足处理，不修改自动启动逻辑。
- 修复：点位通知等待由1秒阻塞 BRPOP 改为非阻塞 RPOP + 50ms轮询，保证 manual stop 检查不会被通知等待遮挡；HTTP 2秒 auto-heal 保留为异常兜底，不应再成为正常 P2 停止路径。
- 验证：停止专项24/24、相关全面扫描回归95/95及35个子测试再次通过。
- 下一步：
  - 复跑 P2 coarse/fine 多信道中途停止，要求 worker 自身在2秒内发布终态，实测目标应明显低于原 2.1~2.37 秒。
  - sub5 只需一次带完整 Web access log 的定向复测，按 A/B/第三个 scan_id 对应的 `/start` 请求逐一对齐。

### 2026-07-03 10:22

- 操作：复核部署 `ptz_control.py`（MD5 `8d218fdac3c339cca3c09473688d6e75`）后的专项最终报告；P2 覆盖 `coarse_capture` / `coarse_switch`，sub5 同步记录 Web access log、Redis active ID 与状态时间线。
- 观察：
  - P2 成功命中的 7 轮均由 worker 在 112~189ms 内发布终态，平均约 140ms；未命中 HTTP auto-heal、无状态复活，停止后没有继续切换/抓包。
  - 其余 9 轮均未实际触发目标停止阶段：6 轮在 120 秒内未进入 fine，3 轮因 journal 采集起点错误未捕获事件，不能计为源码停止失败。
  - sub5 中 B 的 `active_scan_id` 启动后立即生效，约 30 秒后正常进入 `running/coarse` 并持续运行；A 的旧 stop 未误伤 B；access log 只有 A、B 两次 start，不存在第三个 scan_id。
  - 最终 Redis 已清理 `full_scan:stop`、`full_scan:active_scan_id` 与当前项目门禁，`ptz:current_status.full_scan.terminal=true`。
- 结论：50ms 点位通知轮询已使中途停止从原 2.146~2.377 秒 auto-heal 路径降至 112~189ms worker 正常收口；旧 stop key 隔离也已定向验证，问题关闭。fine 阶段本轮因测试时长未直接命中，但与 coarse 共用已修复的通知等待/stop 收口路径，不构成本问题关闭阻塞项。
- 下一步：无需继续修改 stop 源码；若单独补充 fine 覆盖，应把测试等待提高到 300 秒以上并修正 journal 起点，但只作为覆盖增强。

## 当前卡点

- 卡在哪里：无，本问题已完成设备验收。
- 缺少什么信息：无阻塞信息；fine 阶段仅缺额外覆盖，不影响当前结论。
- 推荐下一步：停止专项不再重复修改源码；后续常规回归保留 coarse/fine 随机 stop、终态防复活与旧 stop key 隔离即可。

## 临时处理

- 是否有临时绕过：无（仅 force cleanup Redis 状态后让 worker 自动恢复）
- 临时方案风险：force delete `full_scan:active_scan_id` 与 `full_scan:stop` 会丢失停止信号语义，仅用于恢复测试，不能作为修复方案
- 需要回收的临时代码 / 配置：
  - 设备 `config.json["全面扫描"]["启用白名单位置复核"]` 已恢复为 False（之前 Todo 26 测试时修改）
  - 设备 `config.json["全面扫描"]["允许单信道内部测试"]` 保持 True（压力测试需要）
  - 设备 `config.json["全面扫描"]["允许最小点位内部测试"]` 保持 True（压力测试需要）
  - Redis `gimbal:default_config.work_x_range = [500, 1500]`、`work_y_range = [200, 800]` 已恢复（Todo 26 测试时扩大到 [200, 2800] × [100, 758]）

## 最终结论

- 根因：stop key 的读取者会删除不匹配信号，启动者与 worker 又分别清理 stop，导致早期/交叠 stop 被吞；dwell 轮询粒度偏大；终态 reason 依赖 Redis key 最终仍存在；HTTP 只识别仍存活的 stop key。
- 修复方式：明确 stop key 由启动者原子换代、读取者只读；dwell 改为100ms切片；PTZ锁存停止原因；HTTP 用 STOPPING 项目补足历史自愈并条件清理 active ID；同任务终态禁止复活。
- 验证方式：本地停止专项24/24、全面扫描相关回归95/95及35个子测试通过；设备最终复测中 P2 实际命中的7轮均在112~189ms由worker正常收口且无auto-heal/复活，sub5确认B不受A旧stop误伤且没有第三个scan_id。未进入fine或未捕获事件的9轮属于测试覆盖未命中，不计为源码失败。
- 后续防回退措施：保留 stop key 所有权、启动事务、历史脏态投影、终态单调性和100ms dwell切片的源码/行为测试。

## 维护规则

- 默认不要自动创建问题记录；只有用户明确标记或确认后才创建。
- 新问题创建时，至少写清楚问题简单描述、时间、状态和当前卡点。
- 每次排查有新进展，都追加一条带时间的排查记录，不要覆盖历史判断。
- 所有排查记录时间统一使用 `YYYY-MM-DD HH:mm`，默认时区 `Asia/Shanghai`。
- 状态变化时，同步更新文件名末尾状态和正文 `状态` 字段。
- 问题解决后，补全最终结论，并在关联计划的对应 Todo 下反向引用本问题。
- 如果问题关联某个计划，必须在本问题写明"关联计划 / 关联 Todo"，并在对应计划 Todo 进度记录里反向链接本问题。
