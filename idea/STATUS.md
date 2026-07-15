# 当前状态 - WiFi信号定位设备后端

> **AI 会话入口**：每次新会话先读这个文件，了解当前进度，再按需读其他文件。
> **更新规则**：每次会话结束时更新"当前停在哪里"和"下一步"两栏。

> ⚠️ **AI 强制前置规范**：每次修改代码后，必须按照 `AI协作规范.md` §§6 的步骤执行：
> 1. 修改前 `git commit`（快照）
> 2. 修改后 `git commit`（说明原因）
> 3. 同步更新 `idea/` 目录下对应的 md 文件（STATUS / TODO / ARCHITECTURE 等）
>
> 详细规则见：`AI协作规范.md` §6

> 📖 **idea 目录阅读顺序建议**（新 AI 接手时）：
> 1. `STATUS.md`（本文件）：当前进度与下一步
> 2. `AI协作规范.md`：**必读**，所有代码修改和 git 操作必须遵守此规范
> 3. `产品特性.md`：产品定位、功能边界、待确认事项
> 4. `用户使用流程.md`：用户视角的完整操作路径
> 5. `ARCHITECTURE.md`：代码结构、进程通信、接口速查
> 6. `TODO.md`：当前待办清单（按优先级）
> 7. `前端接口.md`：前端对接接口文档

---

## 当前停在哪里

**日期**：2026-07-03

**本次会话完成**：
- ✅ 2026-07-03 10:22 全面扫描 stop 状态机竞态完成专项设备验收：部署新版 `ptz_control.py` 后，P2实际命中的7轮均由worker在112~189ms内发布终态，无HTTP auto-heal、状态复活或停止后继续切换/抓包；sub5通过access log确认B未被A旧stop误伤，且不存在第三个scan_id。其余9轮未进入fine或journal漏捕事件，属于测试覆盖未命中而非源码失败。问题记录已转为已解决，stop源码无需继续修改。
- 🟡 2026-07-03 09:31 复核全面扫描 stop 真机压力报告：smoke4、dwell、多信道、stop 所有权sub1~4、历史自愈、终态防复活及P2 17轮均通过；P2 stop→terminal 仍集中2.1~2.37秒，定位为 PTZ 点位通知 `BRPOP timeout=1` 在stop检查后制造额外阻塞，现改非阻塞RPOP+50ms轮询，HTTP 2秒自愈恢复为异常兜底。sub5代码侧不存在自动生成第三个scan_id路径，待access log对齐测试脚本请求。修复后本地停止专项24/24、相关回归95/95及35个子测试通过，问题保持待验证。
- 🟡 2026-07-03 00:25 修复高频 smoke 复现的全面扫描 stop 丢失与历史脏态：stop 读取者不再删除不匹配 key，新任务通过 Redis 事务原子完成清旧 stop/发布 active ID/入队，worker 接手后不再清 stop；point-priority dwell 改为100ms轮询，PTZ锁存首次停止原因；HTTP 结合 STOPPING 项目覆盖 stop key 已消失场景，2秒后自愈 stopped/terminal、按值清 active ID并释放项目门禁；同 scan_id 终态禁止迟到进度复活。停止专项24/24、相关回归95/95及35个子测试通过，当时等待真机复验；问题后续已在 `question/2026-07-03_0015_全面扫描停止状态机race_condition---已解决.md` 收口。
- ✅ 2026-07-02 21:46 隐藏 SSID 三态 Todo 1 完成设备 52 真机验收：ch48、BSSID `76:cd:c4:57:c3:dd` 的真实隐藏 Beacon 在业务结果中输出 `ssid="_wildcard_"`；正常 AP 名称保持不变、无 `ssid=""`、API 与 Redis 一致，结合 18/18 单元测试形成四向一致证据。用户已确认勾选完成，独立计划总体继续保持进行中。
- ✅ 2026-07-02 18:46 完成 Todo 26 白名单强点位置复核的中点替换优化：capture worker 在线段内存中保存首个单包峰值及捕获/回调时间，PTZ 复用现有到位轮询记录时间化 Pan/Tilt 轨迹；获胜峰值按相邻轨迹插值或首尾钳制，再只在原像素线段执行自适应像素→角度正向查找，避免全景扭曲/重叠区的无约束角度反查和整段中点误差。每条边/对角线仍只做一次连续移动，无新增停顿或逐轨迹 Redis 流；Pan 路线成本与安全跨度改用最短环绕差。相关全面扫描回归 144/144、子测试 35/35 通过，已更新前端说明和真机测试 AI 提示词，待设备验收。
- ✅ 2026-07-02 将隐藏 AP 的 SSID 输出契约收敛为三态：Beacon SSID IE 长度为 0 或内容全零时输出保留值 `"_wildcard_"`；有名称时原样输出；已识别为 AP 但未抓到 SSID IE 或无法解析时保持 `null`。不新增字段、不占用 `subtype`，并同步前端接口、架构、测试说明与领域词汇；针对性测试 18/18 与核心 5 模块语法检查通过。
- ✅ 2026-07-02 修复前端调用全面扫描 stop 后偶发仍显示 `active=true`：根因是 Web stop 与 PTZ worker 对整份 `ptz:current_status` 无锁读改写，worker 的旧快照可能在 stop 之后覆盖 `stopping`；Web/PTZ 状态 patch 现统一使用 Redis `WATCH/MULTI/EXEC` 重试，校准进度不再整块覆盖状态；`GET /api/v1/ptz/status` 增加按 `scan_id` 匹配 stop key 的兜底投影，`active_scan_id` 已消失时自愈为 `stopped/terminal=true` 并释放遗留 STOPPING 项目门禁。停止专项 19/19、相关全面扫描回归 75/75（另含30个子测试）通过，待设备并发复现验证。
- ✅ 2026-07-02 修复全面扫描“轮次结果与白名单已成功写入，但 `/api/v1/ptz/status.full_scan` 卡在 running”的终态收口缺口：最终轮次 Redis 结果落地后先发布终态，再执行测试摘要和 SQLite 历史快照；快照异常降级为非致命；终态计数统一从最终轮次重建，清除 `current_point/total_points/executed_point_count/completed_fixed_point_count` 的运行期残值，并显式回填 `scan_id/round_id`。不含受 Windows 临时目录权限影响的图像上下文模块时，本地回归 144/144 通过；该模块失败均为测试目录权限问题。
- ✅ 2026-07-02 13:12 修复 Todo 26 独立复核结果“summary 旧、MAC refinement=null”的诊断与持久化缺口：`current_pose` 兼容 dict/tuple；所有规划/会话/approach/segment 失败均写回对应 MAC 的 `refinement.status/reason/detail`；安全矩形失败包含具体边和角度差；`refine_existing` 总是覆盖白名单 `refinement_summary` 与轮次 `whitelist_refinement`。成功更新规则不变，仍要求完整7段、每段候选至少2包且峰值提升>=1dB。全面扫描本地回归 105/105 通过。
- ✅ 2026-07-02 12:56 修复独立复核在服务重启后的监听模式误判：`capture_worker` 不再只依赖会丢失的进程内 `current_monitor_interface`，会先用 `iw dev <iface> info` 检查物理网卡是否已为 monitor；若是则恢复标记并走约 70ms 的信道切换，只有真实非 monitor 或快速切换失败时才完整初始化。复核启动等待由 6 秒提高到 10 秒覆盖真正冷启动，等待仍可响应 stop；本地全面扫描回归 105/105 通过。
- ✅ 2026-07-02 12:47 分析 Todo 26 正式联合验收日志并修复复核抓包会话假超时：真机双网卡监听/切信道约需 2.2~2.6 秒，原 2 秒 notify 等待提前把实际成功的 11 个配置组判为 `capture_session_start_failed`。现改为 6 秒且等待期间继续响应 stop，同时把 worker 原始失败状态、原因及配置写入摘要；暂不放宽 `no_safe_rectangle` 安全规则。全面扫描本地回归 104/104 通过，下一步仅同步 `ptz_control.py`，用黄金轮次和一个已有计划的 MAC 运行 `refine_existing`。
- ✅ 2026-07-02 11:56 修复 Todo 26 快速真机测试发现的配置透传 bug：`config_loader.get_full_scan_config()` 现返回“启用白名单位置复核”，并支持 `FULL_SCAN_WHITELIST_REFINEMENT_ENABLED` 环境变量覆盖；Web 快速入口与 PTZ 自动尾阶段均可读取真实开关。新增 JSON/环境变量两类回归测试，全面扫描本地回归 103/103 通过。设备端无需重跑主扫描，只需同步 `config_loader.py`、重启后继续使用轮次 1 的替代白名单 MAC 验收。
- ✅ 2026-07-02 11:36 根据 Todo 26/27 真机报告修复 `rebuild_full_scan_whitelist.py` 无法从 `phase=fine` 推断工作区的问题，并新增复用已有轮次白名单、可按 MAC 单独运行的位置复核接口 `/api/v1/ptz/full_area_scan/refine_existing`，无需再次执行约 40 分钟主扫描；新轮次会保存复核所需 `mode/image_context/work_ranges`。恢复测试同步误覆盖的本地 `ptz_control.py` 完整实现后，全面扫描本地回归 101/101 通过。Todo 26 与 2 秒停止仍等待该快速入口的真机验收，Todo 27 三个现场数据分支仍未覆盖。
- ✅ 2026-07-02 00:12 完成 Todo 27 与 Todo 26 的本地代码实现，但按用户要求保持计划项未完成、等待真机测试 AI 验收。Todo 27 新增逐点真实 `observed_configs`、唯一实抓配置隔离、AP 宣告信道严格规则、Client 关系配置无实抓时回退实际最强配置及计入/排除诊断；Todo 26 新增默认关闭的白名单位置复核尾阶段、非终态 `phase=whitelist_refinement`、最短“四边+双对角线”路线、显式 segment 包归属、2 包/`+1dB` 替换、按 MAC 原子提交和停止清理。语法检查与除既有 OpenCV `imencode` 环境故障模块外的全面扫描回归 99/99 通过；已生成 `idea/2026-07-02_Todo26-27真机测试AI提示词.md`。
- ✅ 2026-07-01 23:40 使用 `$grill-with-docs` 完成 Todo 26/27 续审并沉淀到计划与 `CONTEXT.md`：Todo 27 固定按 MAC 选择唯一判定配置、禁止跨配置拼接且实时/离线重建共用判定逻辑；Todo 26 保持点位优先主扫描不变，仅增加默认关闭的白名单位置复核尾阶段，使用像素矩形“四边 + 两对角线”单遍最短覆盖、显式 segment 归属、至少 2 包、峰值提升 `>=1dB`、按 MAC 原子提交和硬件小移动安全回退。当前只完成设计收敛，尚未修改生产代码；下一步先实现 Todo 27。
- ✅ 2026-07-01 全面扫描快速停止完成设备验收：校准、移动、抓包、稳定等待、扫描间隔及多信道切换期间停止全部通过；stop API 耗时 111-162ms，`terminal=true` 耗时约 1.3-1.8s，均满足 2 秒硬指标；状态正确进入 `stopped`，`capture_worker` 无需重启，无 Traceback、旧任务复活或额外信道切换。实现包含 stop API 立即写 `active=false/state=stopping`、迟到状态防复活、手动停止跳过旧点位通知与项目快照、配置会话共享 2 秒预算及超时单独重启 `capture_worker` 兜底；本地相关回归 66/66 通过。
- ✅ 2026-07-01 16:45 新增全面扫描正式采样截图开关 `全面扫描.保存扫描采样截图` / `FULL_SCAN_SAMPLING_CAPTURE_ENABLED`，默认关闭；关闭时不选择采样点且不发起截图 HTTP 请求，扫描、抓包、移动和 timing 语义不变。相关回归 72/72 通过，尚未设备验证。
- ✅ 2026-07-01 16:30 完成全面扫描生产可观测性与移动统一的本地实现：像素转换返回兼容旧调用的结构化失败原因；四类正式点输出逐点计划；粗扫内部/外扩、细扫、偏差区统一 direct 优先且失败后 split fallback；Redis `full_scan` 新增八项计数并保留 `current_point/total_points`；ptz/capture 日志恢复毫秒。新增针对性测试已通过，尚未部署或设备验证。
- ✅ 2026-07-01 15:20 确认“部分点位未显示移动”的双重原因：配置优先内部格心实际调用直接移动但缺少普通开始/完成日志，外扩分轴移动日志完整；同时本轮确有1个coarse像素因pixel_to_angle_failed被真实跳过。下一步统一move_start/move_finish/already_reached/skip日志及计划-执行计数。
- ✅ 2026-07-01 15:18 二级设备日志已解释主要耗时：首次session的8.8秒主要是完整monitor冷启动，同轮后续session仅154ms；PTZ位置查询稳定约302ms且无2秒超时，长move来自真实大角度运动及外扩/偏差区分轴串行。配置优先重复内部路径的运动成本抵消信道切换收益，下一步转为策略评审。
- ✅ 2026-07-01 14:59 首轮设备结构化计时已将主要耗时收敛到config session启动8.8秒及move 3.4-14.5秒；代码核对发现session启动可能重复设置定向信道，直接移动会多次同步查询Pan/Tilt且单轴响应超时上限2秒。下一步补二级计时，暂不直接删校验或信道设置。
- ✅ 2026-07-01 14:42 完成全面扫描 PTZ 侧结构化计时本地实现：新增默认关闭的 `保存测试计时明细` / `FULL_SCAN_TIMING_TRACE_ENABLED=1`，每轮输出 `/tmp/logs/full_scan_{round_id}_timing.jsonl`，覆盖两种遍历策略的 14 类 operation，并在原 summary 中按开关附加写入器开销和 operation 百分位聚合；新增测试 12/12、相关全面扫描回归 117/117 通过，尚未部署或运行设备测试。
- ✅ 2026-07-01 14:18 确认全面扫描耗时诊断产物方案：现有 worker 仅有控制台文本日志，阶段 summary 无法解释单点内部耗时；下一步先实现默认关闭、逐步骤追加并 flush 的 PTZ JSONL 计时文件，由测试 AI 自动运行配置优先+单信道+最小点位测试，必要时再补 capture worker 独立计时文件。
- ✅ 2026-07-01 14:11 复核真实场景 A/B 耗时报告：纠正“新旧策略配置切换次数相同”和“配置优先逐点等待 capture notify”两项不符合代码的判断；配置优先内部点按移动0.8s+稳定0.5s+dwell0.6s理论约1.9s，实测8.1-11.3s，确定先用配置优先+单信道+最小点位隔离额外6-9s。
- ✅ 2026-07-01 14:02 建立全面扫描两种遍历策略耗时问题记录：区分“配置优先按外扩点数 + 配置数 × 内部格心数放大云台移动的固有成本”与“A 轮单点 31-37 秒、monitor 4902 秒口径不明”的异常；明确优先收集 A/B 原始 summary、scan_id 对齐日志与 monitor 计算来源，并设计单信道/最小点位 T1-T4 受控测试矩阵。
- ✅ 2026-07-01 11:43 清理全面扫描图像上下文测试遗留：精确删除项目根目录200个Python `tempfile`探测文件和67个`tmp`测试目录；将该测试的所有临时产物收敛到被Git忽略的`.test_tmp/full_scan_image_context/`，取消静默忽略清理失败并在模块结束恢复环境、移除容器。针对性测试33/33通过，测试后根目录残留和`.test_tmp`均为0。
- ✅ 2026-07-01 11:31 Todo 25 定向实机验证通过：149/HT20下AP `94:83:c4:c7:42:d9` 与Client `56:94:1f:a2:1a:49` 由同一scan_id内真实双向Data帧建立confirmed关系；AP无可解析DS/HT主信道时正确回退`beacon_capture_config/inferred`；Redis结果与现场信息一致，无泄漏、复活或运行异常。同步更新全面扫描前端实施说明，Todo 24/25字段现可接入，白名单增强不阻塞第一阶段联调。
- ✅ 2026-07-01 Todo 24 设备验证全部通过（24-T1 至 24-T6）：配置优先 manual_stop/time_limit partial path 正确；旧点位模式 stopped notify 保留真实 MAC；scan_id 隔离正确；无 Traceback、EBUSY、session 泄漏或终态复活。24-T5 session 清理终态顺序因轮询间隔未捕获严格时间线，但多次运行均无实际故障。

- ✅ 2026-06-30 18:00 Todo 22 设备端验证通过：直接双轴移动跨 Pan 115°~183°/Tilt -17°~12° 到位正常；stop 秒级响应 confirmed（stopped + terminal=true）；summary 文件正确生成；`test_channel` 未生效因设备端环境变量 `FULL_SCAN_ALLOW_SINGLE_CHANNEL_TEST` 未设置，非代码问题。Todo 21 已由提交 `d679c2a`、`8f07d98`、`ffd3c4e` 完成且用户确认设备测试通过。
- ✅ 2026-06-30 14:32 Todo 18 切换全面扫描范围输入为像素契约已完成。API 可靠解析图像模式、显式/最新全景与单图上下文，支持 rglob 递归子目录搜索；解析及归一化 target_ranges 像素范围，对比 Redis work_x/y_range 做越界包含校验，强制单目标区限制。6 项真机测试已全数通过。
- ✅ 2026-06-30 12:42 Todo 17 已完成目标设备闭环：2.4GHz/5GHz/默认双频实际配置为 13/13/26 个 HT20，非法参数不启动，偏差区保持任务频段；修复 IDLE 心跳覆盖扫描终态后，HTTP/Redis 均验证 `running -> stopping -> stopped`、`terminal=true` 持续保留及新旧 `scan_id` 隔离。同步收敛 Todo 18 图像边界：不新增 `camera:last_single_meta`，后端按 `mode` 解析最新全景/单图，可选 `image_url` 显式指定，图片历史与保存由前端负责。
- ✅ 2026-06-30 10:08 完成全面扫描计划 Todo 17：新增 `wifi_mode` 任务级频段选择，支持 2.4GHz/5GHz 各 13 个 HT20 配置和默认双频 26 个配置；API 耗时估算、worker 粗扫/细扫温启动/偏差区候选来源及摘要均受任务锁定配置约束，共享 `INITIAL_SCAN_CONFIGS` 未修改。本机纯逻辑测试 6 项与语法检查通过，设备真机待验证。
- ✅ 2026-06-30 09:59 收敛全面扫描首版范围：当前只实现单目标区；`target_ranges` 保留数组接口但后端强制长度为 1，多目标区点位共享、区域归属、白名单择优和复核合并转入计划 Todo 31 暂缓，优先推进其他基础优化。
- ✅ 2026-06-30 01:38 使用 `$grill-with-docs` 将外部 `07_全面扫描总体方案.md` 与当前领域术语、状态规则、现有 plan 和代码逐项对照；完成像素路径、直接移动/中继、配置优先持续采集、证据结构、AP/Client 信道关系、白名单强点复核和后置辅助筛选的最终决策，并更新 `CONTEXT.md` 与全面扫描计划 Todo 20-30。当前只完成审阅和文档沉淀，尚未修改生产代码。
- ✅ 2026-06-29 17:53 新建并按用户要求重写 `01_文档资料/2026-06-29_项目每周待办事项清单.md`：只保留 2026-06-29 至 2026-07-05 本周必须完成的后端、前端和共同联调事项；前后端字段已交接，不再重复列交接任务；后续追加客户端设备与 AP 连接关系优化、全面扫描数据筛选逻辑优化。
- ✅ 2026-06-29 14:45 已在项目代码目录之外创建 `01_文档资料/04-全面扫描独立优化讨论/`：目录不含 `agent.md`、代码或现有扫描参数，只保存中性业务约束、粗扫/细扫/偏差点分阶段讨论框架、证据模板、结论模板和 `$grill-me` 启动提示词；用于后续隔离会话独立评审三个阶段内部方法。
- ✅ 2026-06-29 13:57 完成录音优化事项的后端产品化梳理，并归入 `plan/2026-06-24_全面扫描路径与偏差区优化计划.md` Todo 16-19：现有点位级 `phase` 只补前端交接说明；全面扫描新增 `wifi_mode` 三模式频段选择；范围输入切换为 Redis 外层像素范围 + 请求多目标区像素范围；保留三模式现场验证 Todo。像素扫描点生成逻辑仍留给专门分支讨论，当前未修改代码。（历史方案；多目标区已于 2026-06-30 转为 Todo 31 暂缓。）
- ✅ 2026-06-26 17:49 修复云台移动在接近目标点时反复“无进展重发”的卡住问题：新增 `PTZ_MOVE_COMPLETE_TOLERANCE` 默认 `0.2°`，主移动循环先判定到位再判断无进展重发，并使用水平角最短角度差；已验证 `Pan=179.88 -> 180.00` 会判定到位。
- ✅ 2026-06-26 17:46 修复全面扫描粗扫外扩探测点路径生成的 `NameError: PAN_MIN is not defined`：为 `ptz_control.py` 增加模块级硬件限位默认值，并在 `ptz_process_main()` 初始化真实限位后同步到模块级变量；已通过 `py_compile` 和 `_build_coarse_scan_path()` 针对性调用验证。
- ✅ 2026-06-26 完成 `plan/2026-06-24_全面扫描路径与偏差区优化计划.md` Todo 12-15：粗扫改为 10° 格心稀疏覆盖 + `precheck_range` 外扩 9° 八点探测；偏差区改为 3 层、每层 8 点、按四边外扩距离分配且窄边轮换；偏差区候选配置改为真实目标区出现 MAC；白名单新增基于本轮 `antenna_bias` 的判定缓冲区、通过原因、缓冲区相对外部 2dB 差值和分散强外部点拒绝原因。
- ✅ 2026-06-25 修复 Hugin 全景坐标双向不闭合问题：根因是 `pixels -> angles` 已走 PMAP，而 `angles -> pixels` 仍可能走 legacy `coordinate_map/visual` 链路；已统一为 PMAP 权威映射，`pixels -> angles` 直接查 PMAP，`angles -> pixels` 通过进程内 PMAP owner/source 分桶索引做 source 像素近邻反查。
- ✅ 2026-06-25 完成 `plan/2026-06-24_全面扫描路径与偏差区优化计划.md` Todo 9-11：全面扫描测试摘要改为默认关闭的临时 JSON 文件，不再写入正式 API/Redis 轮次结果/历史 snapshot；偏差区候选配置改为目标区优势 MAC 的 `channel/bandwidth` 去重集合；细扫缩减为每目标区 20-26 核心网格点并取消额外边界点。
- ✅ 2026-06-25 根据全面扫描真机反馈尝试过运行详情日志草稿；该方向已在后续 Todo 9 收敛为默认关闭的测试运行摘要文件，不再使用 `run_detail/detail_logging` 作为正式 API 或历史结果字段。
- ✅ 2026-06-25 排查全面扫描 stop 返回到真正终态间隔过长问题：根因是全扫粗扫/细扫/偏差区云台移动未传入 `stop_check_fn`，stop 只能在移动完成后被主循环识别；已将全扫校准、点位移动、移动后稳定等待、抓包通知等待和窗口间隔等待接入任务级 stop 检查。
- ✅ 2026-06-24 实现 `plan/2026-06-24_全面扫描路径与偏差区优化计划.md` Todo 1-6：全面扫描粗扫/细扫路径改为稳定均匀网格 + 边界点，偏差区取消 A/B 分支并统一为 `deviation_a` 外扩环，原始点位追加 synthetic marker，白名单跳过 marker 并增加目标区位置证据与清晰 rejected metrics。
- ✅ 2026-06-24 补充 `agent.md`：新增“本文件功能”说明，并沉淀常用后端接口 curl 速查，覆盖状态查询、云台移动、普通拍照、Hugin 全景拼接、全面扫描、定位扫描、指定点位抓包、独立抓包与 WiFi 连接状态查询。
- ✅ 完成 AI 文档系统审计与同步：更新 `agent.md`、`rule/AI修改入口指南.md`、`rule/状态管理规则.md`、`idea/ARCHITECTURE.md`、`idea/TODO.md`，使入口规则与当前 v2.2 代码对齐。
- ✅ 设计并实施了**全景拼接与像素查表优化方案**的 `.pmap` 主路线：用紧凑二进制像素映射文件 `.pmap` 取代旧 CSV/SQLite 扩展路线，优化生成/读取速度和文件体积。
- ✅ nona C++ 核心功能改造与编译：在 Linux 设备上修改了 `nona.cpp`，支持输出 Planar 二进制 `.pmap` 格式（包含 Magic Header、Metadata JSON、owner、source_x、source_y、coverage 数组），并编译部署为 `nona-pixelmap`。
- ✅ 后端 Python 侧适配：完成 `hugin_panorama_runtime.py` 的 `_run_nona_pmap()` 与 bundle 导出适配，新增 `pmap_utils.py` 读取/写入 `.pmap` / `.pcand`。
- ✅ 明确当前边界：`/api/v1/camera/coordinate_convert` 的 Hugin 全景像素转角度默认走 `.pmap`；`coordinate_map.npz` / `correction_map.npz` 仅保留为显式开启的旧路线。左右边界补拍、`stitch_role`、非对称裁剪仍是待评审优化项，未接入主流程。
- ✅ 精度验证规划：制定了 `pixel_center_offset`（0.0 vs 0.5）的抽样精度校验方案与重叠区候选诊断表生成逻辑。
- ✅ PMAP 坐标系根因已定位并验证：旧 `nona.cpp` PMAP 分支误用 `createInvTransform() + x+0.5-center`；设备端 A/B 证明正确修复为对齐 nona 正常 remap 路径的 `createTransform(...) + full canvas 像素坐标`，7 组人工参考点全部 `owner_match=True`。
- ✅ 新增可重复修复脚本：`hugin/nona_pixelmap/patch_nona_pmap_remap_transform.py`，可补丁设备端 Hugin `nona.cpp`，并可选执行 `ninja nona`、部署 `/home/ultiwill/bin/nona-pixelmap` 和同 session 7 点验证。
- ✅ Hugin 默认链路切到 PMAP：默认优先 `/home/ultiwill/bin/nona-pixelmap`，默认要求生成 `final/pixel_map.pmap`，默认不再生成耗时的 `coordinate_map.npz` / `correction_map.npz`；`/api/v1/camera/coordinate_convert` 的像素转角度优先走 PMAP 查表。

**当前焦点**：
- 隐藏 SSID 三态优化已在独立计划 `plan/2026-07-02_AP身份与SSID输出优化计划.md` 完成 Todo 1；真实隐藏 AP、正常 AP、空字符串排除及 API/Redis 一致性均已由设备 52 验收。
- Todo 26 旧的“获胜线段像素中点”已替换为峰值包轨迹定位和原像素线约束查找，本地实现与回归完成，仍保持未勾选/待验证；下一步开启白名单位置复核，从普通全面扫描入口完整跑一轮，验收主扫描→基础白名单→复核→正常终态以及像素在线性、轨迹采样和成员不变。
- 全面扫描移动可观测性、统一 direct→split fallback 与 Redis 计数已完成本地实现；下一步是设备端小范围验证日志、到位精度、stop 与状态计数。问题记录见 `question/2026-07-01_1402_全面扫描两种遍历策略耗时异常---排查中.md`。
- 全面扫描 Todo 17、18、21、22、23、24、25 已完成代码与设备验证。Todo 25 已覆盖 AP 宣告/推断信道、扫描/宣告带宽分离、Data 帧 Client–AP 关系和 confirmed/inferred/uncertain 客户端配置；前端可先接入扫描证据与AP/Client关系，代码侧下一步为 Todo 27 白名单基线适配。首版只做单目标区，多目标区 Todo 31 暂缓。
- 原 Todo 16-19 已与最终方案合并：phase 交接新增 `evidence_role/sampling_complete`，`wifi_mode` 保持任务级 HT20 配置；图像上下文不维护后端历史，全景/单图按 `mode` 使用各自最新图片，可选 `image_url` 显式指定，图片历史与保存由前端负责。
- 全面扫描路径、偏差区和白名单筛选策略已按 Todo 12-15 更新，测试摘要默认保存；下一步需要设备端小范围真机验证一轮，重点看粗扫 10° 格心点 + 外扩探测点、偏差区每层 8 点分布、`target_seen_macs` 候选配置、白名单 `accept_reasons` / `rejected_macs` 原因和 `/tmp/logs/full_scan_{round_id}_summary.json`。
- Hugin 全景坐标转换已统一为 PMAP 权威映射；下一步需要用真机样例验证 `pan/tilt -> pixel -> pan/tilt` 闭环误差，并确认同一张图批量约 100 个 angle 点通过进程内分桶索引转换的耗时可接受。
- 将 PMAP 修复结论固化到文档、脚本和设备端部署流程，避免后续回退到 `createInvTransform()` 或 `x+0.5-center` 历史误判。
- 用修复后的 `nona-pixelmap` 继续做真机全流程回归，重点看 PMAP 生成、缓存查表和 bundle 导出路径。
- 确认 `hugin/hugin_pto_math_mapper.py` 中 CSV/SQLite legacy 模式不会被误用为生产路径。
- 评审下一阶段是否接入边界中心补拍、`stitch_role` 和非对称裁剪。

**下一步 (即刻计划)**：
1. **Todo 26 普通全面扫描真机验收**：同步本次 `ptz_control.py`、`capture_worker.py`，开启 `全面扫描.启用白名单位置复核`，从 `/api/v1/ptz/full_area_scan/start` 用正式配置完整跑一轮普通全面扫描；按 `idea/2026-07-02_Todo26白名单位置复核普通全面扫描真机测试AI提示词.md` 验证主扫描到复核再到正常终态的状态连续性、白名单成员不变、路线仍为单次连续移动、峰值时间与轨迹诊断、最终像素位于获胜原线段及 `best_position_source=whitelist_refinement_peak_trajectory`。报告经用户确认前不得勾选 Todo。
2. **全面扫描设备验证**：先用单信道+最小点位验证四类计划日志、direct→split fallback、stop 退出及 Redis 八项计数，再扩展正式配置；收集 timing JSONL、summary、PTZ/capture 日志及 status。
3. **前端第一阶段联调**：按 `idea/2026-06-30_全面扫描前端实施任务说明.md` 接入Todo 24证据角色/部分采样和Todo 25 AP/Client关系；白名单增强暂不作为联调阻塞项。
4. **全流程功能联调**：验证从”全景图拍摄 -> Hugin 拼接 -> `.pmap` 生成 -> PMAP 批量像素查表 -> 云台角度换算 -> 直接双轴移动”的完整闭环。
5. **全面扫描设备端回归**：按 Todo 30 从单配置小范围闭环逐步扩展到多配置、stop/scan_id、偏差点、白名单复核和 2.4/5/双频完整回归。
6. **前端时间轴联调**（S5）。
7. **评审边界补拍优化是否进入下一阶段**：若要做，再实施 `left_boundary/right_boundary/stitch_role`、非对称裁剪和 metadata/PTO 输入图同步。


---

## 已完成功能清单

| 功能 | 状态 | 说明 |
|------|------|------|
| S3-B 全面扫描运行机制 | ✅ | 默认单轮；可选限时窗口与窗口间隔，每轮校准 |
| S3-C 历史数据存储 | 🟡 进行中 | SQLite 已落地，full_area SUCCESS snapshot 待补测 |
| S5 时间轴后端接口 | 🟡 进行中 | 后端已完成，前端联调待进行 |
| S7 全向天线并行采集 | ✅ | 双卡并行 sniff，真机验证通过 |
| S8 客户端扫描 | ✅ | guard zone / 跳扫，真机验证通过 |
| S9 星链设备识别 | ✅ | 三特征判定，真机验证通过 |
| S1 摄像头模块代码 | ✅ | camera_utils / panorama_sampling / panorama_stitch 编写完成 |
| S1 camera_worker 集成 | 🔲 待执行 | `run_manager.py` 当前未常驻拉起 camera_worker；主全景流程在 `web_server.py` 同步执行 |
| T1 摄像头接口文档 | ✅ | 拍照 + 坐标转换接口已完成（2026-05-15/16） |
| T3 定位功能后端 | ✅ | 后端自动步径 + 多 MAC 共用扫描 + 可选抓包分包，真机验证通过（2026-05-31） |
| T8 定位扫描优化 | ✅ | 快速校验移动、跳过重复抓包、实时更新中间结果，真机验证通过（2026-05-31） |
| T9 capture_at_best | ✅ | 一键最强点抓包，立即返回，真机验证通过（2026-05-31） |
| T5 全景图↔角度映射 | ✅ | Hugin 像素转角度默认走 `.pmap` 查表；`coordinate_map.npz` / `correction_map.npz` 仅保留为显式开启旧路线 |
| 全面扫描设备类型识别 | ✅ | type（ap/client/null）+ subtype（starlink/null）+ AP ssid；隐藏 SSID 为 `_wildcard_`、未知为 null，2026-07-02 |
| 全面扫描三段点位扫描 | ✅ | precheck 初筛粗扫 8° 跳过工作区外扩矩形 + guard 外扩边 8° + work 工作区细扫 4°，2026-05-25 |
| 全面扫描时间窗口配置 | ✅ | `scan_time_limit` 分钟 + `time_interval` 秒，2026-05-20 |
| 全面扫描成功轮次白名单 | ✅ | `SUCCESS` 轮次按坐标去重后筛选疑似工作区 MAC，2026-05-21 |
| 全面扫描暂停修复 | ✅ | brpop 改为轮询检查停止标志，2026-05-18 |
| 全景图曝光稳定 | ✅ | capture_stable 等待亮度稳定后拍照，解决过曝问题，2026-05-20 |
| 摄像头 HTTP 服务集成 | ✅ | camera_utils 改用 HTTP 服务获取图片，避免多进程抢设备，2026-05-20 |
| 坐标转换 pan 角度修复 | ✅ | atan2 负数转换为 0~360，2026-05-18 |
| S3-A 多点扫描 | ⏸️ 暂缓 | 前端入口废弃，后端代码保留 |
| S4 卫星图联动 | ⏸️ 暂缓 | 来源未明确 |
| S6 多点位测向 | ⏸️ 暂缓 | 算法待明确 |
| H1 云台连接方案 | ✅ | Type-C 接线确认，CH340 串口 + Pelco-D 协议测试通过（2026-05-09） |
| H2 摄像头选型 | ✅ | 型号/供应商已确认 |

---

## 设备信息

- 远程地址：`192.168.8.239`，用户：`ultiwill`
- 项目路径：待设备端确认。历史记录曾写为 `/home/ultiwill/ptz_capture_system/v2.2`，但 2026-06-21 用户反馈设备端不存在该 `v2.2` 目录；后续命令不要再默认使用此路径。
- 图片/全景 session 保存目录：`/home/ultiwill/ptz_capture_system/data/captures/`
- 虚拟环境 Python：`/home/ultiwill/ptz_capture_system/ptz_env/bin/python`
- 实际启动命令：`sudo /home/ultiwill/ptz_capture_system/ptz_env/bin/python run_manager.py`
- 与项目相关的 Python 脚本应优先使用上述虚拟环境 Python 运行；缺少依赖时也应安装到该虚拟环境，不要默认使用系统 `python`。

---

## 待明确事项

- S1：摄像头 FOV 大小（影响采样点间距计算）
- S2：摄像头与天线的垂直偏差是固定值还是用户校准？
- S4：卫星图来源（离线图片 / 在线地图 API）

---

## 常用命令

```bash
# Python 解释器
PY=/home/ultiwill/ptz_capture_system/ptz_env/bin/python

# 再切换到设备端实际后端代码目录；不要默认 cd v2.2

# 启动系统
sudo $PY run_manager.py

# 测试摄像头
$PY test_cv2_stitch.py
```
