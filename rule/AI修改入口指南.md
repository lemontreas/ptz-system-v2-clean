# AI修改入口指南

**适用范围**：给后续 AI、Cursor、Claude 或其他代码助手作为修改前入口。目标是让 AI 先理解项目边界，再动手定位和修改，避免每次全局乱扫或破坏状态管理。

---

## 1\. 修改前必须先读

`idea/ARCHITECTURE.md`

*   用途：项目地图，了解进程、文件职责、Redis 总线、核心 API。
*   注意：里面的行号只是线索，不能直接按旧行号改代码。

`rule/状态管理规则.md`

*   用途：全面扫描、定位扫描、抓包、stop、`/api/v1/ptz/status` 的硬规则。
*   凡是涉及状态、停止、任务生命周期、`scan_id`、`active_scan_id`、`capture:running`、`capture:status` 的修改，必须遵守。

`idea/前端更新说明_全面扫描_定位扫描与摄像头.md`

*   用途：前端对接契约。
*   修改 API 返回、状态字段、stop 行为、抓包行为时，必须同步检查这份文档是否需要更新。

---

## 2\. 先判断任务类型

收到需求后先归类，再找文件。

| 任务类型 | 优先阅读/定位 |
| --- | --- |
| REST API、前端返回、参数校验 | `web_server.py` |
| 云台移动、全面扫描、定位扫描主流程 | `ptz_control.py` |
| 信道切换、嗅探、pcap、任务级取消 | `capture_worker.py` |
| 配置项、环境变量覆盖 | `config.json`、`config_loader.py` |
| 全面扫描白名单筛选/淘汰原因 | `ptz_control.py`、`rebuild_full_scan_whitelist.py` |
| 前端对接说明 | `idea/前端更新说明_全面扫描_定位扫描与摄像头.md` |
| 状态/stop/scan\_id 相关 | `rule/状态管理规则.md` 加对应代码 |
| 状态流冒烟测试 | `tests/state_flow_smoke_test.py`、`tests/README_state_flow_smoke_test.md` |
| 天线偏差补偿 | `antenna_bias_utils.py`、`config.json["天线偏差补偿"]`、`config_loader.py` |
| 摄像头拍照/全景接口 | `web_server.py`、`camera_utils.py`、`panorama_stitch_utils.py` |
| Hugin 全景拼接运行时 | `hugin_panorama_runtime.py`、`hugin_coordinate_mapper.py`、`HUGIN_MAPPING_BUNDLE_README.md` |
| `.pmap` 二进制像素映射 | `pmap_utils.py`、`hugin_panorama_runtime.py`、`hugin/hugin_pto_math_mapper.py`、`plan/全景拼接与像素查表优化方案.md` |
| Hugin/nona 设备端验证 | `HUGIN_MAPPING_BUNDLE_README.md`、`hugin/nona_pixelmap/实际操作记录_nona改造完整流程.md` |
| WiFi 连接尝试 | `web_server.py`、`capture_worker.py`、`wifi_mode_utils.py`、`idea/前端接口_WiFi连接尝试功能.md` |

---

## 3\. 定位代码规则

不要相信旧行号。必须用 `rg` 找当前位置。

### 3.1 运行环境边界

本仓库通常在 Windows 机器上修改代码，但实际系统主要运行在 Linux 设备上。Redis、Hugin/nona、网卡、摄像头和云台硬件状态都以 Linux 设备为准。

给后续 AI 或用户提示命令时必须明确区分：

*   Windows 修改机：适合 `rg`、阅读/修改代码、跑语法级检查。
*   Linux 运行设备：适合 `redis-cli`、`python run_manager.py`、Hugin/nona、网卡抓包、摄像头和云台联调。

不要用本机 Windows 的 `redis-cli`、`localhost:6379` 或本机 HTTP 探测来判断设备端 Redis/运行状态；除非用户明确说明当前命令就在 Linux 设备上执行。

常用定位命令：

```
rg -n "capture_at_best|move_to_best_capture" web_server.py ptz_control.py capture_worker.py
rg -n "start_full_area_scan|stop_full_area_scan|full_scan:active_scan_id" web_server.py ptz_control.py capture_worker.py
rg -n "start_location_scan|location_scan:stop|location_scan:active_scan_id" web_server.py ptz_control.py capture_worker.py
rg -n "capture:running|capture:status|stop_capture|save_pcap|start_capture" web_server.py capture_worker.py
rg -n "patch_ptz_status|ptz:current_status" ptz_control.py web_server.py
rg -n "antenna_bias|visual_point_to_rf|rf_point_to_visual|build_antenna_bias" antenna_bias_utils.py web_server.py ptz_control.py
rg -n "build_hugin_panorama_from_shots|coordinate_convert|camera:last_panorama_meta|compute_shot_grid" web_server.py hugin_panorama_runtime.py panorama_stitch_utils.py
rg -n "_run_nona_pmap|HUGIN_BUILD_PMAP|HUGIN_NONA_BIN|pixel_map.pmap|pmap_summary|pmap_error" hugin_panorama_runtime.py HUGIN_MAPPING_BUNDLE_README.md
rg -n "PMAP_MAGIC|PMAP_HEADER|load_pmap|read_pmap_header|write_pmap|PCAND" pmap_utils.py hugin/hugin_pto_math_mapper.py
rg -n "nona_pixel_map.csv|index-csv|sqlite|legacy|csv-both" hugin_panorama_runtime.py hugin/hugin_pto_math_mapper.py HUGIN_MAPPING_BUNDLE_README.md plan/全景拼接与像素查表优化方案.md
rg -n "wifi_connect|connect_id|capture:status|capture:stop|stopping_others" web_server.py capture_worker.py wifi_mode_utils.py
```

修改前至少阅读目标函数上下文，不要只改匹配到的一行。

---

## 4\. 当前主流程边界

当前正常使用的主流程：

*   全面扫描：`/api/v1/ptz/full_area_scan/start`、`/stop`、`/result`、`/whitelist`
*   定位扫描：`/api/v1/ptz/location_scan/start`、`/stop`、`/status`、`/result`
*   移动到指定最强点抓包：`/api/v1/ptz/capture_at_best`
*   独立抓包：`/api/v1/capture/start`、`/stop`、`/save_pcap`
*   摄像头/全景：`/api/v1/camera/capture`（`panorama=true`）、`/api/v1/camera/coordinate_convert`
*   WiFi 连接尝试：按 `idea/前端接口_WiFi连接尝试功能.md` 单独检查

历史保留或低优先级流程：

*   初始扫描、多点扫描、旧智能扫描相关代码可能仍在，但不是当前主要使用路径。
*   不要为了新需求大规模重构历史流程，除非用户明确要求。
*   `camera_worker.py` 目前不是 `run_manager.py` 常驻拉起的主路径；不要把它当成已接入的摄像头主流程。当前全景拍照和 Hugin 拼接在 `web_server.py` 请求内同步执行。
*   `.pmap` 已是 Hugin/nona 像素映射正式路线，但当前 `/api/v1/camera/coordinate_convert` 仍主要走 `coordinate_map.npz` / `correction_map.npz` 的运行时反查。旧 CSV/SQLite 只保留在诊断工具的 legacy 模式中，不要新增生产依赖。

---

## 5\. 状态修改硬要求

涉及状态时，以 `rule/状态管理规则.md` 为准，下面只是摘要。

1.  `/api/v1/ptz/status` 是前端状态主入口。
2.  顶层 `status.state` 只表示 PTZ/运动状态，不表示扫描或抓包生命周期。
3.  全面扫描生命周期看 `status.full_scan.state` / `terminal`。
4.  定位扫描生命周期看 `status.location_scan.state` / `terminal`。
5.  抓包生命周期看 `status.capture.running` / `active`。
6.  `capture_worker.py` 抓包状态写 `capture:running` / `capture:status`，`web_server.py` 在 `/api/v1/ptz/status` 中合并给前端；不要让 `capture_worker.py` 直接改 `ptz:current_status`。
7.  `phase` 只表示阶段，不表示是否终态。
8.  stop 是两阶段：API 先写 `stopping`，worker 收尾后写 `stopped` / `done` / `error`。
9.  新任务必须用新的 `scan_id`，stop key 和 capture\_worker 已派发任务必须校验 `active_scan_id`。
10.  任务结束后要清理对应 `active_scan_id`，但 stop key 可短 TTL 保留，供已派发 capture 任务取消。
11.  不要整块覆盖 `ptz:current_status` 导致 `full_scan`、`location_scan`、`capture_at_best` 子对象丢失；优先使用局部 patch 辅助函数。

---

## 6\. stop 与取消检查

任何可能耗时的循环都要能停下来：

*   全面扫描点位循环
*   定位扫描信道探测
*   定位扫描 coarse/mid/fine 点位采样
*   最终定位复验
*   capture\_worker 的信道配置循环
*   capture\_worker 的 sniff 前后
*   等待 Redis notify/result 的循环
*   移动到最强点抓包前后的扫描停止过渡

capture\_worker 不能只等 ptz\_worker 停止。已经派发到 `capture:command_queue` 的任务，也必须自己检查 stop key 和 `active_scan_id`。

---

## 7\. 审核清单

修改完成后按下面检查。

1.  启动接口是否写入 `queued/running` 与新的 `scan_id`。
2.  stop 接口是否立即让前端看到 `stopping`。
3.  worker 是否最终写 `stopped/done/error` 和 `terminal=true`。
4.  是否清理对应 `active_scan_id`。
5.  stop key 是否包含并校验 `scan_id`。
6.  capture\_worker 是否在耗时循环里检查取消。
7.  `/api/v1/ptz/status` 是否仍兼容前端旧字段。
8.  顶层 `state="IDLE"` 时，`capture.running=true` 是否仍能被正确表达。
9.  `capture_at_best` 是否正确停止当前扫描、移动、抓包，并在失败时写终态。
10.  文档和测试是否需要同步更新。

---

## 8\. Git 保存规则

修改代码和文档前后必须管理好 Git，避免状态不清导致后续 AI 不知道哪些改动属于谁。

### 8.1 修改前

1.  每次开始任务先执行 `git status --short`。
2.  如果是当天/本轮第一次修改，先确认当前工作区是否已经有未提交改动。
3.  如果未提交改动明显属于用户或其他 AI，不要擅自回滚，也不要混进自己的提交。
4.  如果用户明确要求“先 git 保存”，先把当前相关改动保存成一次提交，再继续新修改。
5.  不要使用 `git add .`。必须显式 stage 本次相关文件。

### 8.2 代码改动

代码改动要按“小闭环”提交：

1.  每完成一组可独立说明的代码改动，先运行必要检查，例如 `py_compile`、相关测试或状态流测试。
2.  检查通过后提交一次 Git。
3.  如果代码改动同时需要文档更新，文档应和对应代码一起提交，保证提交本身可理解。
4.  不要把无关文档、临时日志、旧测试输出混进代码提交。

### 8.3 文档改动

文档改动可以累计，但要有上限：

1.  纯文档修改可以累计最多 3 次后提交一次。
2.  如果当天任务结束，即使不足 3 次，也应提交当前文档改动。
3.  如果文档改动是为了匹配刚完成的代码行为，应跟代码同次提交，不等 3 次。
4.  文档提交信息建议使用 `docs:` 前缀。

### 8.4 混合改动

如果同一轮同时涉及代码、测试和文档：

1.  优先按功能边界拆提交。
2.  代码和对应测试放在一起。
3.  与该代码强相关的前端说明或规则文档可以放在同一提交。
4.  大范围规则文档、架构文档更新可单独 `docs:` 提交。

### 8.5 提交前检查

提交前至少确认：

```
git status --short
git diff --cached
```

只提交本次任务相关文件。提交后再看一次：

```
git status --short
```

确认工作区里剩余改动是否都是用户已有改动、测试临时文件或下一步待处理内容。

---

## 9\. 不要做的事

*   不要把 `phase` 当生命周期终态。
*   不要用顶层 `state` 判断全面扫描、定位扫描或抓包是否结束。
*   不要写没有 `scan_id` 的新 stop 协议。
*   不要让 `scan_id=None` 的 stop 被当成有效停止。
*   不要让旧 capture notify 或旧 command 影响新任务。
*   不要在移动失败、抓包失败时留下 `active=true`。
*   不要为了小改动重构整个 `ptz_control.py`。
*   不要删除用户已有测试、日志或调试文件，除非用户明确要求。

---

## 10\. 给后续 AI 的推荐提示词

可以把下面这段放在每次任务前：

```
请先阅读 idea/ARCHITECTURE.md 和 rule/状态管理规则.md。
如果涉及前端对接，再阅读 idea/前端更新说明_全面扫描_定位扫描与摄像头.md。
不要相信旧行号，必须用 rg 定位当前代码。
修改前先执行 git status --short；代码改动每个小闭环检查通过后提交，纯文档改动最多累计 3 次后提交；不要使用 git add .，只 stage 本次相关文件。
本次修改要保持 /api/v1/ptz/status 的前端兼容性，状态语义必须符合 rule/状态管理规则.md。
修改后请给出审核清单：scan_id、active_scan_id、stop 两阶段、capture_worker 取消、终态、前端兼容字段、测试/py_compile。
```
