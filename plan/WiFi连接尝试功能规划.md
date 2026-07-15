# WiFi 连接尝试功能规划

**版本**：v0.5（实现修订）
**日期**：2026-06-11
**状态**：已实现

---

## 1. 功能概述

允许前端用户在定位扫描、全面扫描结果或独立触发的情况下，**选择一个目标 AP 设备，输入密码，发起 WiFi 连接尝试**。

这是一个**用户确认后的高优先级独占任务**。前端弹窗让用户确认后调用接口，后端**在 API 层立即停止**当前正在运行的全面扫描、定位扫描、capture_at_best、独立抓包，等待它们进入终态，再切换网卡模式执行连接。`capture_worker` 侧的 `stopping_others` 阶段是兜底等待，不是唯一停止入口。

连接结束（无论结果），**必须将网卡还原为 monitor 模式并恢复就绪状态**，让系统可以重新开始扫描。

> **连接语义**：本功能的目的是"验证可连接性"，不是"接入网络使用"。WPA 关联成功（`wpa_state=COMPLETED`）即视为连接成功，**不启动 DHCP、不获取 IP 地址、不修改系统路由**。对被连接的路由器来说，设备只完成了认证握手，没有真正上线，不会占用 IP 地址。

### 1.1 使用场景

| 场景 | 说明 |
|------|------|
| 全面扫描结果页 | 扫描完成后，从白名单 MAC 列表中选中一个 AP，点击"连接尝试" |
| 定位扫描结果页 | 定位完成后，选中目标 MAC，点击"连接尝试" |
| 扫描中途触发 | 全面扫描或定位扫描仍在进行中，用户已在实时结果里发现目标设备，直接发起连接；后端会自动停止当前扫描再切换网卡 |
| 独立触发 | 不依赖扫描，直接手动输入 SSID / BSSID / 密码发起连接 |

### 1.2 核心约束

> **这是系统中最具破坏性的操作之一。**

- 定向网卡从 monitor 模式切换到 managed 模式，**会立即中断所有正在使用该网卡的扫描和抓包任务**。
- 连接过程中，`capture_worker` 无法做任何嗅探工作。
- 连接结束（无论结果）后，必须走统一恢复路径，切回 monitor 模式并调用 `setup_monitor_mode_once()` 恢复可用状态。
- **只有在 monitor 模式恢复成功后，才能写连接终态。**
- 一旦开始触碰网卡模式（down/up、monitor/managed 切换、启动 wpa_supplicant），后续任何失败都必须先尝试恢复 monitor，再写终态；不能把网卡留在半切换状态。
- **不得影响以太网连接**：WiFi 连接过程中不能修改系统默认路由，不能导致 SSH 断连。

---

## 2. 触发与前置条件

### 2.1 前端触发方式

前端选中目标设备（AP），弹窗确认后提供：

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ssid` | 是 | string | 目标网络 SSID |
| `bssid` | 否 | string | 目标 AP 的 BSSID（精准锁定 AP，防止同名 SSID 干扰） |
| `password` | 是 | string | WPA/WPA2 密码；空字符串表示开放网络 |
| `timeout` | 否 | int | 连接超时秒数，默认 120，范围 10~300 |

### 2.2 前置检查（API 层，仅拒绝不可恢复的情况）

API 层只做以下检查，其余情况**不拒绝，自动停止后继续**：

| 检查项 | 处理策略 |
|--------|----------|
| 参数不合法（ssid 为空、timeout 超范围等） | 返回 400，reason=invalid_args |
| 依赖工具不可用（wpa_supplicant / wpa_cli 缺失） | 返回 503，reason=dependency_missing |
| 已有一个 wifi_connect 任务正在运行（state 不是终态） | 返回 409，reason=wifi_connect_already_running |

> 全面扫描、定位扫描、独立抓包正在运行时**不拒绝，由 API 层立即发出 stop 信号**（见第 3.1 节）。

---

## 3. API 设计

### 3.1 发起连接（POST /api/v1/wifi/connect）

```http
POST /api/v1/wifi/connect
```

**请求 Body：**

```json
{
  "ssid": "TargetNetwork",
  "bssid": "aa:bb:cc:dd:ee:ff",
  "password": "mypassword123",
  "timeout": 120
}
```

**API 层执行顺序（不等待，全部立即写 Redis 后返回）：**

```
① 参数校验
② 检查依赖工具（wpa_supplicant / wpa_cli）
③ 检查是否已有 wifi_connect 任务（非终态则 409）
④ 生成 connect_id = "wificonn_{timestamp_ms}"
⑤ 写 wifi_connect:active_connect_id = connect_id     ← [P0] 必须在入队前写
⑥ 写 wifi_connect:status（state=queued）
⑦ 执行"等价于现有 stop API"的全量 stop 操作（见 3.1.1）
⑧ LPUSH capture:command_queue（action=wifi_connect）
⑨ 立即返回 queued=true
```

**立即返回（不等连接完成）：**

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "queued": true,
    "connect_id": "wificonn_1710000000000",
    "message": "连接任务已排队，已发出停止当前任务信号"
  }
}
```

**错误码：**

| HTTP 状态 | code | reason | 含义 |
|-----------|------|--------|------|
| 400 | 1001 | invalid_args | 缺少 ssid、password，或 timeout 超范围 |
| 409 | 2001 | wifi_connect_already_running | 已有连接任务进行中 |
| 503 | 3001 | dependency_missing | wpa_supplicant / wpa_cli / DHCP 客户端等依赖未安装 |

#### 3.1.1 API 层等价 stop 操作（精确参照现有 stop API 逻辑）

API 层在步骤 ⑦ 中必须执行以下操作，**不能只写一个 key，必须完整模拟各 stop API 的行为**。

**硬规则：禁止写 `scan_id=None` 的新 stop 协议。**

- 只有确认对应任务非终态，且读到对应 `active_scan_id` 时，才写 JSON stop key 和推送对应 stop 命令。
- 如果 `active_scan_id` 为空，说明该类扫描没有可精确停止的活跃任务；此时不要写 `{scan_id: null}`，最多只保留不会误伤新任务的 legacy/capture stop 兜底。
- `wifi_connect` 触发的 preempt stop 必须带 `reason="wifi_connect_preempt"`，方便后续审计和前端提示。

**停止全面扫描（如果正在运行）：**

```python
_fs_scan_id = r.get('full_scan:active_scan_id')
if _fs_scan_id:
    # 1. 写 legacy 停止标志（ptz_control 阻塞期间靠它感知）
    r.set('multi_scan:stop_full_area_scan', '1', ex=120)

    # 2. 写新格式 JSON stop key（带 scan_id）
    r.set('full_scan:stop', json.dumps({
        'scan_id': _fs_scan_id, 'reason': 'wifi_connect_preempt', 'ts': time.time()
    }), ex=120)

    # 3. patch ptz:current_status.full_scan 为 stopping（保留原有字段）
    ptz_status['full_scan'].update({
        'active': True, 'state': 'stopping',
        'stop_requested': True, 'stop_requested_at': time.time(), 'terminal': False
    })
    r.set(PTZ_STATUS_KEY, json.dumps(ptz_status))

    # 4. 推 stop 命令（带 scan_id，兼容 ptz_control 的命令分发）
    r.lpush(PTZ_COMMAND_QUEUE, json.dumps({
        'action': 'stop_full_area_scan', 'scan_id': _fs_scan_id
    }))
```

**停止定位扫描（如果正在运行）：**

```python
_ls_scan_id = r.get('location_scan:active_scan_id')
if _ls_scan_id:
    # 1. 写 JSON stop key（带 scan_id）
    r.set('location_scan:stop', json.dumps({
        'scan_id': _ls_scan_id, 'reason': 'wifi_connect_preempt', 'ts': time.time()
    }), ex=120)

    # 2. 写 capture:stop（定位扫描内部抓包也必须停）
    r.set('capture:stop', '1', ex=120)

    # 3. 更新 location_scan:status 为 stopping
    r.set('location_scan:status', json.dumps({
        'phase': 'idle', 'status': 'stopping',
        'stop_requested': True, 'reason': 'wifi_connect_preempt', 'ts': time.time()
    }))

    # 4. patch ptz:current_status.location_scan 为 stopping（保留原有字段）
    ptz_status['location_scan'].update({
        'active': True, 'state': 'stopping',
        'stop_requested': True, 'stop_requested_at': time.time(), 'terminal': False
    })
    r.set(PTZ_STATUS_KEY, json.dumps(ptz_status))

    # 5. 推 stop_location_scan 命令（带 scan_id）
    r.lpush(PTZ_COMMAND_QUEUE, json.dumps({
        'action': 'stop_location_scan', 'scan_id': _ls_scan_id
    }))

    # 6. 推 stop_capture 命令到 capture queue
    r.lpush(CAPTURE_COMMAND_QUEUE, json.dumps({
        'action': 'stop_capture', 'reason': 'wifi_connect_preempt'
    }))
```

**停止独立抓包 / capture_at_best（如果 capture.running=true）：**

```python
r.set('capture:stop', '1', ex=120)
r.lpush(CAPTURE_COMMAND_QUEUE, json.dumps({
    'action': 'stop_capture', 'reason': 'wifi_connect_preempt'
}))
```

> **为什么 API 层做这一步，而不只靠 capture_worker 做？**
>
> `capture_worker` 是单线程串行处理命令队列。如果队列里有旧命令排在 `wifi_connect` 前面，或者 `capture_worker` 正忙于执行上一个抓包命令，`wifi_connect` 命令要等到旧任务处理完才能被取到。这段时间里，`ptz_control` 的全面扫描/定位扫描可能还在继续移动并派发新的 capture 命令。
>
> API 层直接写 stop key 和 patch 状态，`ptz_control` 和 `capture_worker` 在它们各自的检查点（每 0.5s 或更短）就能感知，不依赖 `wifi_connect` 命令被取到。

### 3.2 取消正在进行的连接

```http
POST /api/v1/wifi/connect/stop
```

后端立即：
1. 读取 `wifi_connect:active_connect_id`
2. 写 `wifi_connect:stop`（JSON 格式，含 connect_id，TTL=60s）
3. 用局部 patch 将 `wifi_connect.state` 更新为 `stopping`

如果 `wifi_connect:active_connect_id` 暂时不存在，但 `wifi_connect:status` 仍显示非终态，应优先从 `wifi_connect:status.connect_id` 兜底读取；两处都没有 connect_id 时，返回 **404**，reason=`wifi_connect_not_running`，不要写无 connect_id 的 stop key。

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "stopping": true,
    "message": "已请求取消，请继续轮询 /api/v1/ptz/status"
  }
}
```

### 3.3 查询连接状态

**推荐方式：** 通过现有主状态接口读取 `wifi_connect` 子对象：

```http
GET /api/v1/ptz/status
```

`api_ptz_status` 处理函数需单独从 `wifi_connect:status` 读取并注入返回结果中。直接将层层小对象注入 `status['wifi_connect']`。  
**`wifi_connect` 子对象不写入 `ptz:current_status`，不需要修改 ptz_control.py。**（与现有 `capture` 子对象的处理方式相同：单独读 Redis 后手动组装。）

**辅助接口：**

```http
GET /api/v1/wifi/connect/status
```

---

## 4. 状态（生命周期）设计

### 4.1 state 完整生命周期

```
queued
  │
  └─► stopping_others           （capture_worker 兜底等待阶段：等 full/location/capture 进入终态）
        │
        ├─► error               （stop_others_timeout：等待超时，不切网卡）
        │
        └─► switching_to_managed （正在切 monitor → managed）
              │
              ├─► restoring_monitor → error（switch_managed_failed，已尝试恢复）
              │
              └─► connecting     （wpa_supplicant 正在运行，轮询 wpa_state）
                    │
                    ├─► restoring_monitor   （连接结果已拿到，正在切回 monitor）
                    │         │
                    │         ├─► connected   ✅ terminal（monitor 恢复成功 + WPA 关联成功）
                    │         ├─► failed      ✅ terminal（monitor 恢复成功 + 连接失败）
                    │         ├─► timeout     ✅ terminal（monitor 恢复成功 + 连接超时）
                    │         ├─► cancelled   ✅ terminal（monitor 恢复成功 + 用户停止）
                    │         └─► error       ✅ terminal（monitor_restore_failed；保留 connect_result）
                    │
                    ├─► timeout  → restoring_monitor
                    ├─► cancelled → restoring_monitor
                    └─► error    → restoring_monitor
```

> **关键规则：只有在 monitor 恢复完成后才写终态。**
> 即使连接本身成功，如果 `restoring_monitor` 阶段失败，最终 `state=error, reason=monitor_restore_failed`，但同时保留 `connect_result="success"` 供前端展示连接结果。

### 4.2 wifi_connect 子对象字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `state` | string | 当前阶段，见上表 |
| `active` | bool | queued/.../stopping 时为 true；终态为 false |
| `terminal` | bool | 终态为 true，写入后不再变化 |
| `connect_id` | string | 任务唯一标识，每次启动生成 |
| `ssid` | string | 目标 SSID |
| `bssid` | string/null | 目标 BSSID |
| `timeout` | int | 超时秒数 |
| `started_at` | float | 任务启动时间戳 |
| `elapsed_seconds` | float | 已运行秒数（运行中实时更新） |
| `reason` | string/null | 终态原因，见第 5 节 |
| `result` | string/null | 终态结果：success/failed/timeout/cancelled/error |
| `connect_result` | string/null | 连接本身的结果（`success/failed/timeout/cancelled/error`）。正常终态时与 `result` 相同；在 `monitor_restore_failed` 时独立保留，供前端展示连接结果；WPA 关联成功后立即固定为 `success`，不再被 stop 改写 |

### 4.3 state 与 active / terminal 对应关系

| state | active | terminal | 前端展示 |
|-------|--------|----------|----------|
| `queued` | true | false | "排队中" |
| `stopping_others` | true | false | "正在停止当前任务..." |
| `switching_to_managed` | true | false | "正在切换网卡模式..." |
| `connecting` | true | false | "正在连接 {ssid}..."，展示 elapsed_seconds |
| `stopping` | true | false | "正在取消..." |
| `restoring_monitor` | true | false | "正在恢复监听模式..." |
| `connected` | false | true | ✅ 连接成功（已验证密码和路由器可达） |
| `failed` | false | true | ❌ 连接失败，展示 reason |
| `timeout` | false | true | ⏱ 连接超时 |
| `cancelled` | false | true | 已取消 |
| `error` | false | true | ⚠ 错误，展示 reason；若 connect_result 存在则附带展示 |

---

## 5. 失败原因（reason 枚举）

所有终态必须写明 reason：

| reason | 触发时机 |
|--------|----------|
| `invalid_args` | 参数校验失败（API 层通常拦截，此处为 worker 侧兜底） |
| `wifi_connect_already_running` | 已有连接任务进行中（API 层 409） |
| `wifi_connect_not_running` | stop 接口未找到可取消的连接任务 |
| `dependency_missing` | wpa_supplicant / wpa_cli 等工具不可用 |
| `stop_others_timeout` | 等待当前扫描/抓包停止超时，未能切换网卡 |
| `switch_managed_failed` | monitor → managed 切换失败 |
| `wpa_supplicant_start_failed` | wpa_supplicant 进程启动失败 |
| `network_not_found` | 找不到目标 SSID / BSSID（SCANNING 持续超过 timeout/2 秒） |
| `auth_rejected` | AP 拒绝认证（INACTIVE/DISCONNECTED 持续超过 5 秒） |
| `wrong_password` | 4WAY_HANDSHAKE 超时，疑似密码错误（超过 10 秒） |
| `connect_timeout` | 连接在 timeout 秒内未完成 |
| `manual_stop` | 用户主动停止 |
| `monitor_restore_failed` | 连接后恢复 monitor 模式失败（系统级错误） |
| `worker_restarted` | run_manager 重启时发现未终态任务，强制写错误终态 |
| `internal_error` | 其他未预期的系统错误 |

---

## 6. Redis 字段说明

### 6.1 新增字段及理由

| Redis Key | 类型 | 写入方 | 读取方 | 写入时机 | 理由 |
|-----------|------|--------|--------|----------|------|
| `wifi_connect:status` | String(JSON) | web_server（初始化）/ capture_worker（更新） | web_server | API 入队时写 queued，后续 worker 更新 | 跨进程状态传递，平行于 full_scan:results / location_scan:status |
| `wifi_connect:active_connect_id` | String | **web_server**（入队时写） | capture_worker / web_server stop 接口 | **API 生成 connect_id 后立即写，不等 worker 处理** | 防止用户刚点开始就点停止时 stop 接口找不到 active id；平行于 full_scan:active_scan_id |
| `wifi_connect:stop` | String(JSON) | web_server | capture_worker | stop 接口调用时 | 写入时带 TTL=60s；任务结束时**不续期、不主动删除**，依赖原始 TTL 自然过期，防止旧命令残留时 connect_id 校验失效 |
| `capture:running` | String | **capture_worker**（现有字段，值为 `"1"` 表示抓包中，key 不存在或为 `"0"` 表示空闲） | capture_worker（stopping_others 阶段轮询） | capture_worker 开始/结束抓包时写，任务退出时清零 | 现有字段，wifi_connect stopping_others 阶段复用此 key 判断抓包是否已停止 |

> **[P0] active_connect_id 为什么必须在 API 入队时写（不等 worker）：**
>
> 如果只在 worker 收到命令后才写，存在竞态：用户点击"连接"后立刻点"停止"，此时 worker 还没取到 wifi_connect 命令（可能队列有旧命令排前面），stop 接口就拿不到 active_connect_id，导致 stop 无效。API 层入队前写 active_connect_id，能保证 stop 接口在任何时机都能读到并写出合法的 stop key。

### 6.2 stop key 格式（必须 JSON，含 connect_id）

```json
{
  "connect_id": "wificonn_1710000000000",
  "reason": "manual_stop",
  "ts": 1710000000.0
}
```

`capture_worker` 收到 stop 信号后，**必须校验 `connect_id` 与 `wifi_connect:active_connect_id` 匹配**，不匹配的 stop 忽略（防止旧 TTL 误杀新任务）。

### 6.3 不新增命令队列，复用 `capture:command_queue`

WiFi 连接操作以新 action `wifi_connect` 的形式推入现有 `capture:command_queue`。  
理由：`capture_worker` 已拥有网卡所有权，扩展一个新 action 是最小改动，新建队列需要 worker 监听两个队列。

### 6.4 run_manager.py 启动清理

启动时按以下顺序处理 `wifi_connect` 残留：

1. 读取 `wifi_connect:status`
2. 如果 `terminal != true`（非终态残留），**不要删除，而是写入**：
   ```json
   {"state": "error", "reason": "worker_restarted", "terminal": true, "active": false}
   ```
3. 清理 `wifi_connect:stop`、`wifi_connect:active_connect_id`
4. 调用 `wifi_mode_utils.switch_to_monitor()` 尝试恢复 monitor 模式（失败只打日志，不阻塞启动）

> ⚠️ **实现注意：** 这段逻辑必须**单独写一段 `handle_wifi_connect_residue()` 函数**，在 `flush_stale_redis_state()` 调用后单独调用。
> 不能把 `wifi_connect:stop`、`wifi_connect:active_connect_id` 直接加进 `STALE_KEYS` 列表然后 `r.delete(*STALE_KEYS)` 批量删除，因为 `wifi_connect:status` 需要先读后写错误终态再展阶式处理。

---

## 7. 资源互斥：wifi_connect 运行中禁止启动的任务

当 `wifi_connect.state` 处于以下任一非终态时（`active=true`）：

以下接口必须返回 409，reason=`wifi_connect_running`：

| 被禁止的接口 |
|-------------|
| `POST /api/v1/ptz/full_area_scan/start` |
| `POST /api/v1/ptz/location_scan/start` |
| `POST /api/v1/capture/start` |
| `POST /api/v1/capture/save_pcap` |
| `POST /api/v1/ptz/capture_at_best` |
| `POST /api/v1/ptz/move` |

**前端应在 `wifi_connect.active=true` 时禁用以上所有操作的按钮。**

`POST /api/v1/ptz/stop`、`POST /api/v1/capture/stop`、`POST /api/v1/wifi/connect/stop` 这类停止接口不应被互斥规则拦截；它们仍然用于收尾或人工取消。

---

## 8. 执行流程详解（capture_worker 侧）

### 8.1 stopping_others 是兜底等待阶段，不是唯一停止入口

API 层已经在步骤 ⑦ 发出了所有 stop 信号。`capture_worker` 收到 `wifi_connect` 命令时进入 `stopping_others`，其职责是：
- **验证**其他任务确实已进入终态
- **等待**（轮询，最长 `stop_others_timeout` 秒）
- 超时则写 error 退出，**不继续切换网卡**

```
收到 wifi_connect 命令
    │
    ├─ [检查点] 检查 wifi_connect:stop（connect_id 匹配才响应）→ 提前退出
    │
    ├─ 校验 wifi_connect:active_connect_id 与命令中的 connect_id 匹配
    │   └─ 不匹配（旧命令残留）→ 直接忽略，不处理
    │
    ├─ 写 wifi_connect:status → state=stopping_others
    │
    ├─ 轮询等待（每 0.5s 检查一次）：
    │   以下三个条件**全部满足**才算其他任务已停止：
    │   - `full_scan:active_scan_id` 为空（该 key 不存在或被清除，表示全面扫描已结束）
    │   - `location_scan:active_scan_id` 为空（同上）
    │   - `capture:running` == `'0'`（即 `CAPTURE_RUNNING_KEY` 为 `'0'`）
    │   ⚠️ 不使用 `ptz:current_status.full_scan.terminal`。该字段由 ptz_control.py 写入，
    │   capture_worker 不直接依赖它做内部判断。`active_scan_id` 清除与否是现有代码判断扫描结束的标准。
    │   ├─ [每次等待期间检查 wifi_connect:stop]
    │   └─ 等待超过 stop_others_timeout → 写 state=error, reason=stop_others_timeout
    │       → 清理 active_connect_id → 退出（不切网卡）
    │
    ├─ [检查点] 检查 wifi_connect:stop → cancelled 路径
    │
    ├─ 写 wifi_connect:status → state=switching_to_managed
    ├─ 停止本地 sniff（兜底，确保本进程不在占用网卡）
    ├─ 记录当前 monitor 信道/带宽（restore_channel, restore_bandwidth）用于后续恢复
    ├─ 调用 wifi_mode_utils.switch_to_managed(interface)
    │   └─ 失败 → 进入 restoring_monitor，恢复后写 state=error, reason=switch_managed_failed
    │
    ├─ [检查点] 检查 wifi_connect:stop → cancelled 路径（此时尚未启动 wpa_supplicant）
    │
    ├─ 写临时 wpa_supplicant 配置文件（权限 600）
    ├─ 启动 wpa_supplicant 进程（参见 8.3）
    │   └─ 启动失败 → connect_result=error, reason=wpa_supplicant_start_failed → 进入 restoring_monitor
    │
    ├─ 写 wifi_connect:status → state=connecting
    │
    ├─ 轮询等待连接结果（每 0.5s 一次）：
    │   连接结果判断采用**轮询 `wpa_cli status`** 策略：
    │   - 主路径：每 0.5s 执行 `wpa_cli -i <iface> status`，读取 `wpa_state` 字段
    │   ├─ [检查 wifi_connect:stop] → connect_result=cancelled
    │   │   ⚠️ **固定规则：一旦 `wpa_state=COMPLETED`，`connect_result` 立即固定为 `success`，**
    │   │   **保持 1 秒确认后退出，后续任何 stop 信号只改最终 `state`，**
    │   │   **不再改写 `connect_result`。** 前端可同时看到 `state=cancelled` 和 `connect_result=success`。
    │   ├─ wpa_state=COMPLETED → connect_result=success（固定），保持 1 秒后退出
    │   ├─ 4WAY_HANDSHAKE 持续超过 10 秒 → connect_result=failed, reason=wrong_password
    │   ├─ INACTIVE/DISCONNECTED 持续超过 5 秒 → connect_result=failed, reason=auth_rejected
    │   ├─ SCANNING 持续超过 timeout/2 秒 → connect_result=failed, reason=network_not_found
    │   └─ 超过 timeout → connect_result=timeout, reason=connect_timeout
    │
    ├─ 停止 wpa_supplicant 进程
    ├─ 删除临时配置文件
    │
    ├─ 写 wifi_connect:status → state=restoring_monitor
    ├─ 调用 wifi_mode_utils.switch_to_monitor(interface)
    │       + setup_monitor_mode_once(interface, restore_channel, restore_bandwidth)
    │   ├─ 成功 → 写终态（connected/failed/timeout/cancelled）
    │   └─ 失败 → 写 state=error, reason=monitor_restore_failed, connect_result=<上面结果>
    │
    └─ 清理 wifi_connect:active_connect_id
       保留 wifi_connect:stop TTL（不立即删除，给短时间内可能排队的旧命令消化机会）
```

### 8.2 可中断点（所有位置都要检查 wifi_connect:stop，且校验 connect_id）

- 收到命令后立即
- 进入 stopping_others 后每次等待轮询
- switching_to_managed 前
- wpa_supplicant 启动前
- connecting 轮询中每次（0.5s 间隔）
- 拿到连接结果后（进入 restoring_monitor 前）

### 8.3 wpa_supplicant 配置模板

> **本功能只支持 WPA/WPA2-PSK 和开放网络（`key_mgmt=NONE`），明确不支持 WEP 和 WPA-Enterprise。**  
> 如果用户传入的密码长度不符合 WPA-PSK 要求（8~63 字节 ASCII 或 64 字节 HEX），在 API 层参数校验时以 `invalid_args` 拒绝。

```conf
ctrl_interface=/var/run/wpa_supplicant
network={
    ssid="TargetNetwork"
    bssid=aa:bb:cc:dd:ee:ff       # 仅在传了 bssid 时加这行
    psk="mypassword123"           # WPA/WPA2-PSK 密码（8~63 字节 ASCII）
    key_mgmt=WPA-PSK
    # 开放网络（password 为空字符串）时：key_mgmt=NONE，不写 psk 行
}
```

- 临时写入 `/tmp/wpa_connect_{connect_id}.conf`，权限 `600`
- 任务结束（无论结果）后立即删除（try/finally 保证）
- SSID 和密码必须使用专门的 wpa 配置转义函数写入，不能直接字符串拼接；需要正确处理 `"`、`\`、换行等特殊字符。
- `bssid` 必须先按 MAC 地址格式（`xx:xx:xx:xx:xx:xx`，十六进制）校验，校验通过后再写入配置。

### 8.4 连接状态判断

**采用「主路径轮询」策略：**

- **主路径**：每 0.5s 执行 `wpa_cli -i <iface> status` 并解析 `wpa_state` 字段，驱动状态机推进。
- `wpa_state=COMPLETED` 即视为连接成功（验证了密码正确、路由器可达）。
- **不启动 DHCP、不获取 IP、不修改系统路由**，避免影响以太网连接。
- 成功后保持 1 秒确认，然后自动断开恢复 monitor 模式。

| 来源 | wpa 状态 | 对应 reason |
|------|----------|-------------|
| `status` 命令 | `wpa_state=COMPLETED` | success（固定，后续 stop 不改写） |
| `status` 命令 | `4WAY_HANDSHAKE` 持续超过 10 秒 | wrong_password |
| `status` 命令 | `INACTIVE`/`DISCONNECTED` 持续超过 5 秒 | auth_rejected |
| `status` 命令 | `SCANNING` 持续超过 timeout/2 秒 | network_not_found |
| 内部 | 轮询超过 timeout | connect_timeout |
| stop key | 收到 stop（且 connect_result 未固定） | manual_stop |

---

## 9. 网卡模式切换：统一工具模块 wifi_mode_utils.py

**[P1] 不要在多处散写 `ip/iw` 命令。不要在 run_manager.py 中 import capture_worker.py。**

新建独立工具模块 `wifi_mode_utils.py`（无 capture_worker 依赖，无任何启动副作用）：

```python
# wifi_mode_utils.py
# 无副作用的网卡模式切换工具，可被 capture_worker 和 run_manager 共同 import

def switch_to_managed(interface: str) -> bool:
    """
    将指定网卡切换为 managed 模式。
    执行：ip link set <iface> down → iw <iface> set type managed → ip link set <iface> up
    返回 True 表示成功。
    """

def switch_to_monitor(interface: str) -> bool:
    """
    将指定网卡切换为 monitor 模式（仅切模式，不设信道）。
    执行：清理 dhclient → 清理路由 → ip link set <iface> down → iw <iface> set type monitor → ip link set <iface> up
    返回 True 表示成功。
    """

def kill_wpa_supplicant(interface: str) -> None:
    """
    杀掉残留的 wpa_supplicant 进程（按 interface 匹配），清理 ctrl socket。
    run_manager 启动时调用，capture_worker 连接结束时也可调用。
    """

def kill_dhcp_client(interface: str) -> None:
    """
    杀掉残留的 DHCP 客户端进程（dhclient/udhcpc），清理路由。
    防止 WiFi 连接结束后影响以太网。
    """
```

- `capture_worker.py` 调用 `wifi_mode_utils.switch_to_managed` / `switch_to_monitor`
- `run_manager.py` 调用 `wifi_mode_utils.switch_to_monitor` + `wifi_mode_utils.kill_wpa_supplicant` 做启动清理
- **`run_manager.py` 不再 import capture_worker**，进程边界不破坏

> 注意：`setup_monitor_mode_once()` 是 capture_worker 自身的函数（设置信道、启动 scapy 等），只由 capture_worker 在恢复阶段内部调用。`wifi_mode_utils` 只负责底层 Linux 命令，不涉及 Scapy。

---

## 10. 恢复 monitor 时的信道 / 带宽来源

**[P2] 问题：`restore_channel` / `restore_bandwidth` 从哪来？**

**方案（两层兜底）：**

1. **优先**：capture_worker 在切换到 managed 前，按 interface 记录当前工作信道/带宽（来自现有 `current_channel_by_interface` / `current_bandwidth_by_interface` 缓存）
2. **回退**：如果缓存不可用（启动后未设过信道），读取 `config.json["WiFi连接"]["恢复监听信道"]` 和 `"恢复监听带宽"`（默认 36 / HT20）

```json
"WiFi连接": {
  "默认超时秒数": 120,
  "最大超时秒数": 300,
  "最小超时秒数": 10,
  "轮询间隔秒数": 0.5,
  "停止其他任务超时秒数": 30,
  "临时配置文件目录": "/tmp",
  "恢复监听信道": 36,
  "恢复监听带宽": "HT20",
  "日志明文密码": false
}
```

恢复调用链（在 capture_worker 内部）：

```python
# 切换前记录
restore_channel = current_channel_by_interface.get(interface) or cfg["恢复监听信道"]
restore_bandwidth = current_bandwidth_by_interface.get(interface) or cfg["恢复监听带宽"]

# ... 连接过程 ...

# 恢复
wifi_mode_utils.switch_to_monitor(interface)
setup_monitor_mode_once(interface, restore_channel, restore_bandwidth)
```

---

## 11. 密码与日志策略（调试优先，但默认脱敏）

**本项目当前策略：调试优先，但不要默认把密码写进日志。**

- 允许 password 经过 Redis 命令队列明文传输
- 日志默认输出 `password_present=true`、`password_length` 和可选 hash，不输出原始 password
- 只有当 `config.json["WiFi连接"]["日志明文密码"] = true` 时，才允许在现场调试日志中打印原始 password
- 即使开启明文日志，也必须只在结构化调试日志中输出，不要写入错误响应或前端状态

日志中每个阶段必须打印结构化字段：

```python
password_log = password if cfg.get("日志明文密码") else "***"
logger.info(
    "[wifi_connect] connect_id=%s state=%s interface=%s ssid=%s bssid=%s "
    "password=%s password_present=%s password_length=%s timeout=%s elapsed=%.1fs stop_others=%s mode_switch=%s "
    "wpa_state=%s reason=%s",
    connect_id, state, interface, ssid, bssid, password_log,
    bool(password), len(password or ""), timeout, elapsed,
    stop_others_status, mode_switch_result, wpa_state, reason
)
```

---

## 12. 文件改动清单

### 12.1 新增 `wifi_mode_utils.py`

- [ ] `switch_to_managed(interface)` → 底层 ip/iw 命令，返回 bool
- [ ] `switch_to_monitor(interface)` → 底层 ip/iw 命令，返回 bool
- [ ] `kill_wpa_supplicant(interface)` → 按进程名/PID 文件杀残留进程
- [ ] `kill_dhcp_client(interface)` → 杀残留 DHCP 进程，清理路由

### 12.2 `web_server.py`

- [ ] 新增 `POST /api/v1/wifi/connect`
  - 参数校验
  - 依赖检查（`wpa_supplicant` / `wpa_cli`）
  - SSID/password 写入 wpa 配置前做转义，BSSID 做 MAC 格式校验
  - wifi_connect 防重入检查
  - 生成 connect_id
  - **写 `wifi_connect:active_connect_id`（入队前写，不等 worker）**
  - 写 `wifi_connect:status`（state=queued）
  - 执行等价 stop 操作（3.1.1 所有步骤）
  - LPUSH capture:command_queue（action=wifi_connect）
  - 立即返回

- [ ] 新增 `POST /api/v1/wifi/connect/stop`
  - 读 `wifi_connect:active_connect_id`
  - 写 `wifi_connect:stop`（JSON，TTL=60s）
  - patch `wifi_connect.state = stopping`
  - 立即返回

- [ ] 新增 `GET /api/v1/wifi/connect/status`

- [ ] 修改 `GET /api/v1/ptz/status`：合并 `wifi_connect:status`

- [ ] 修改以下接口，增加 wifi_connect 互斥检查（active=true 则 409）：
  - `POST /api/v1/ptz/full_area_scan/start`
  - `POST /api/v1/ptz/location_scan/start`
  - `POST /api/v1/capture/start`
  - `POST /api/v1/capture/save_pcap`
  - `POST /api/v1/ptz/capture_at_best`
  - `POST /api/v1/ptz/move`

### 12.3 `capture_worker.py`

- [ ] 新增 `wifi_connect` action 分支（主入口函数）
- [ ] import wifi_mode_utils（替换所有散落的 ip/iw 调用）
- [ ] 新增 `_run_wpa_connect(interface, ssid, bssid, password, timeout, connect_id)` 主流程
- [ ] 新增 `_check_wifi_connect_stop(connect_id)` 取消检查（校验 connect_id）
- [ ] 新增 `_write_wifi_connect_status(**kwargs)` 局部 patch 写状态
- [ ] 新增 `_cleanup_wifi_connect(interface, connect_id, restore_channel, restore_bandwidth)` try/finally 清理函数

### 12.4 `config.json`

- [ ] 新增 `"WiFi连接"` 配置块（见第 10 节）

### 12.5 `config_loader.py`

- [ ] 新增 `get_wifi_connect_config()` 读取配置

### 12.6 `run_manager.py`

- [ ] 启动时处理 `wifi_connect` 残留：
  - 非终态 → 写 `state=error, reason=worker_restarted, terminal=true`
  - 清理 `wifi_connect:stop`、`wifi_connect:active_connect_id`
  - 调用 `wifi_mode_utils.kill_wpa_supplicant(interface)` + `wifi_mode_utils.switch_to_monitor(interface)`（失败只打日志）

### 12.7 文档同步（实现后必须更新）

- [ ] `rule/状态管理规则.md`：补充 `wifi_connect` 生命周期章节
- [ ] `rule/AI修改入口指南.md`：任务类型表加入"WiFi 连接尝试"条目
- [ ] `idea/前端更新说明_全面扫描_定位扫描与摄像头.md`：新增 WiFi 连接章节

---

## 13. 注意事项与风险

### 13.1 网卡独占

`capture_worker` 一旦从队列取到 `wifi_connect` 命令，必须在该命令进入终态并恢复 monitor 前不再处理后续抓包命令（串行队列天然保证）。命令被取到之前，API 层 stop key 已经负责让 PTZ/capture 旧任务尽快退出。

### 13.2 wpa_supplicant 残留

- 所有流程用 `try/finally` 保证清理
- `run_manager.py` 启动时调用 `wifi_mode_utils.kill_wpa_supplicant()` 处理残留

### 13.3 连接后立即断开

WPA 关联成功后**保持 1 秒确认，然后立即断开并切回 monitor 模式**。本功能的语义是"验证可连接性"，不是"接入网络使用"。

### 13.4 以太网保护

**关键约束：WiFi 连接过程中不能影响以太网连接。**

- **不启动 DHCP**：避免 dhclient 修改系统默认路由，导致 SSH 断连。
- **不获取 IP 地址**：WPA 关联成功即为连接成功，不需要 IP。
- **清理路由**：恢复 monitor 模式前，执行 `ip route flush dev <interface>` 清理 WiFi 接口的路由。
- **对被连接的路由器无影响**：设备只完成认证握手，没有真正上线，不会占用 IP 地址。

---

## 14. 审核清单（实现完成后逐项检查）

### 生命周期

- [ ] 所有非终态 state 是否保持 `active=true, terminal=false`？
- [ ] 所有终态是否保持 `active=false, terminal=true`？
- [ ] 只有在 monitor 恢复完成后才写终态（connected/failed/timeout/cancelled）？
- [ ] monitor 恢复失败时是否保留 `connect_result` 字段？
- [ ] managed 切换或 wpa_supplicant 任一阶段失败后，是否仍先进入恢复 monitor 路径再写终态？

### [P0] API 层 stop 与 active_connect_id

- [ ] `active_connect_id` 是否在 API 入队前（而非 worker 处理时）写入？
- [ ] API 层是否执行了完整的等价 stop 操作（包含 legacy key、JSON stop key、status patch、命令队列 push）？
- [ ] API 层是否禁止写 `scan_id=None` 的 `full_scan:stop` / `location_scan:stop`？
- [ ] 全面扫描 stop：是否同时写了 `multi_scan:stop_full_area_scan`、`full_scan:stop` JSON、patch `full_scan.state=stopping`、push `stop_full_area_scan` 命令？
- [ ] 定位扫描 stop：是否同时写了 `location_scan:stop` JSON、`capture:stop`、patch `location_scan.state=stopping`、push `stop_location_scan` 和 `stop_capture` 命令？

### [P0] stopping_others 等待逻辑

- [ ] capture_worker 是否在 stopping_others 里用以下方式判断（**不使用 ptz:current_status.terminal**）：`full_scan:active_scan_id` 为空 + `location_scan:active_scan_id` 为空 + `capture:running == '0'`？
- [ ] 等待超时后是否不切换网卡，直接写 error 终态？

### [P1] 网卡切换统一入口

- [ ] 所有网卡模式切换是否都通过 `wifi_mode_utils` 函数？
- [ ] `run_manager.py` 是否不 import `capture_worker.py`？
- [ ] 恢复 monitor 时是否调用了 `setup_monitor_mode_once()`？

### [P2] 信道恢复来源

- [ ] 切换前是否记录了 `restore_channel` / `restore_bandwidth`？
- [ ] 缓存不可用时是否回退到 config.json 默认值？

### stop 隔离

- [ ] `wifi_connect:stop` 是否 JSON 格式且含 `connect_id`？
- [ ] capture_worker 是否校验 connect_id 匹配 `active_connect_id` 才响应 stop？
- [ ] 任务结束时是否**不续期、不主动删除** stop key，依赖原始 TTL（60s）自然过期？
- [ ] `wpa_state=COMPLETED` 后，`connect_result` 是否已固定为 `success`，不再被后续 stop 改写？

### 资源互斥

- [ ] wifi_connect active 期间，6 个互斥接口是否都返回 409？
- [ ] 停止类接口是否没有被互斥规则误拦截？

### 日志

- [ ] 每个 state 切换是否打印结构化日志？
- [ ] 默认日志是否脱敏 password，仅在 `"日志明文密码": true` 时输出原始密码？

### 其他

- [ ] `wpa_state=COMPLETED` 后是否保持 1 秒确认然后退出？
- [ ] 恢复 monitor 前是否清理了 WiFi 接口路由（`ip route flush dev <interface>`）？
- [ ] `py_compile` 检查所有修改文件？
- [ ] run_manager 是否处理了非终态残留？
- [ ] 文档（状态管理规则、AI 修改入口指南、前端更新说明）是否同步更新？

---

## 15. 已解决问题记录

### 15.1 DHCP 导致以太网断连问题（已解决）

**问题表现**：
使用 `dhclient` 获取 IP 时，dhclient 会修改系统默认路由，导致以太网连接断开，SSH 断连。

**解决方案**：
**完全去掉 DHCP 步骤**。WPA 关联成功（`wpa_state=COMPLETED`）即视为连接成功，不启动 DHCP、不获取 IP、不修改路由。

**理由**：
1. 本功能的语义是"验证可连接性"，不是"接入网络使用"
2. WPA 关联成功已能证明：密码正确、路由器可达
3. 对被连接的路由器无影响：设备只完成认证握手，没有真正上线

### 15.2 logging.getLogger(__name__) 日志丢失问题（已解决）

**问题表现**：
函数内使用 `logging.getLogger(__name__)` 获取 logger，但日志不输出。

**原因**：
`capture_worker.py` 直接运行时 `__name__=='__main__'`，函数内 `logging.getLogger(__name__)` 得到的是无 handler 的 `__main__` logger，而非已配置的 `capture_worker` logger。

**解决方案**：
删除函数内 `logger = logging.getLogger(__name__)`，统一使用模块级 `logger`。

