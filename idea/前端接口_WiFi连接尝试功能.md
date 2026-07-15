# 前端接口文档：WiFi 连接尝试功能

**版本**：v1.0
**日期**：2026-06-11
**状态**：已实现，可对接

---

## 1. 功能概述

WiFi 连接尝试功能允许用户选择一个目标 AP，输入密码，验证是否可连接。

**连接语义**：验证可连接性，不是接入网络使用。
- WPA 关联成功（`wpa_state=COMPLETED`）即为连接成功
- 不获取 IP 地址，不修改系统路由
- 对被连接的路由器无影响（设备只完成认证握手，没有真正上线）

**连接耗时**：
- 成功：约 10-15 秒（扫描 + 认证 + 1 秒确认）
- 失败：约 15-30 秒（取决于失败类型）

---

## 2. 通用响应格式

所有接口统一返回：

```json
{
  "code": 0,
  "data": {},
  "msg": "ok"
}
```

失败时：

```json
{
  "code": 1001,
  "data": {"reason": "invalid_args"},
  "msg": "缺少 ssid"
}
```

前端判断规则：
- `code === 0` 表示成功
- `code !== 0` 表示失败
- 错误提示使用 `msg`
- 失败原因使用 `data.reason`

---

## 3. API 接口

### 3.1 发起 WiFi 连接

**接口**：`POST /api/v1/wifi/connect`

**用途**：发起 WiFi 连接尝试（非阻塞，立即返回）

**请求 Body**：

```json
{
  "ssid": "TargetNetwork",
  "bssid": "aa:bb:cc:dd:ee:ff",
  "password": "mypassword123",
  "timeout": 60
}
```

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ssid` | 是 | string | 目标网络 SSID |
| `bssid` | 否 | string | 目标 AP 的 BSSID（精准锁定 AP，防止同名 SSID 干扰），格式：`xx:xx:xx:xx:xx:xx` |
| `password` | 是 | string | WPA/WPA2 密码（8~63 字节 ASCII 或 64 字节 HEX）；空字符串 `""` 表示开放网络 |
| `timeout` | 否 | int | 连接超时秒数，默认 120，范围 10~300 |

**成功响应**（code=0）：

```json
{
  "code": 0,
  "data": {
    "queued": true,
    "connect_id": "wificonn_1781159332582",
    "message": "连接任务已排队，已发出停止当前任务信号"
  },
  "msg": "ok"
}
```

**失败响应**：

| HTTP 状态 | code | reason | msg | 说明 |
|-----------|------|--------|-----|------|
| 400 | 1001 | `invalid_args` | 缺少 ssid | ssid 为空 |
| 400 | 1001 | `invalid_args` | 密码长度不符合 WPA-PSK 要求（8~63 字节 ASCII 或 64 字节 HEX） | 密码长度错误 |
| 400 | 1001 | `invalid_args` | BSSID 格式错误 | BSSID 格式不对 |
| 400 | 1001 | `invalid_args` | timeout 超出范围 (10~300) | timeout 超范围 |
| 409 | 2001 | `wifi_connect_already_running` | 已有 WiFi 连接任务正在运行 | 防重入 |
| 503 | 3001 | `dependency_missing` | 依赖工具缺失: wpa_supplicant, wpa_cli | 系统工具缺失 |

**前端调用示例**：

```javascript
const response = await fetch('/api/v1/wifi/connect', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    ssid: 'QWQ',
    password: 'ultiwill2025',
    timeout: 60
  })
});
const result = await response.json();
if (result.code === 0) {
  // 成功入队，开始轮询状态
  const connectId = result.data.connect_id;
  pollStatus(connectId);
} else {
  // 失败，显示错误
  showError(result.msg);
}
```

---

### 3.2 查询 WiFi 连接状态

**接口**：`GET /api/v1/wifi/connect/status`

**用途**：查询当前 WiFi 连接任务的状态（轮询使用）

**请求参数**：无

**成功响应**（code=0）：

**状态流转示例**：

```json
// 1. 排队中
{
  "code": 0,
  "data": {
    "state": "queued",
    "active": true,
    "terminal": false,
    "connect_id": "wificonn_1781159332582",
    "ssid": "QWQ",
    "bssid": null,
    "timeout": 60,
    "started_at": 1781159332.5839915,
    "elapsed_seconds": 0.5
  }
}

// 2. 正在停止其他任务
{
  "code": 0,
  "data": {
    "state": "stopping_others",
    "active": true,
    "terminal": false,
    "connect_id": "wificonn_1781159332582",
    "elapsed_seconds": 1.0
  }
}

// 3. 正在切换网卡模式
{
  "code": 0,
  "data": {
    "state": "switching_to_managed",
    "active": true,
    "terminal": false,
    "connect_id": "wificonn_1781159332582",
    "elapsed_seconds": 1.5
  }
}

// 4. 正在连接（wpa_supplicant 运行中）
{
  "code": 0,
  "data": {
    "state": "connecting",
    "active": true,
    "terminal": false,
    "connect_id": "wificonn_1781159332582",
    "elapsed_seconds": 5.0
  }
}

// 5. 连接成功（保持 1 秒后自动进入 restoring_monitor）
{
  "code": 0,
  "data": {
    "state": "connected",
    "active": true,
    "terminal": false,
    "connect_id": "wificonn_1781159332582",
    "connect_result": "success",
    "elapsed_seconds": 11.0
  }
}

// 6. 正在恢复监听模式
{
  "code": 0,
  "data": {
    "state": "restoring_monitor",
    "active": true,
    "terminal": false,
    "connect_id": "wificonn_1781159332582",
    "connect_result": "success",
    "elapsed_seconds": 15.0
  }
}

// 7. 终态：连接成功
{
  "code": 0,
  "data": {
    "state": "connected",
    "active": false,
    "terminal": true,
    "connect_id": "wificonn_1781159332582",
    "connect_result": "success",
    "result": "success",
    "reason": null,
    "ip_address": null,
    "elapsed_seconds": 18.5
  }
}
```

**状态说明**：

| state | active | terminal | 前端展示 |
|-------|--------|----------|----------|
| `queued` | true | false | "排队中..." |
| `stopping_others` | true | false | "正在停止当前任务..." |
| `switching_to_managed` | true | false | "正在切换网卡模式..." |
| `connecting` | true | false | "正在连接 {ssid}..."，展示 elapsed_seconds |
| `connected` | true/false | 见下 | 见下 |
| `stopping` | true | false | "正在取消..." |
| `restoring_monitor` | true | false | "正在恢复监听模式..." |
| `idle` | false | true | 无连接任务 |

**终态说明**：

| state | terminal | reason | 前端展示 |
|-------|----------|--------|----------|
| `connected` | true | null | ✅ "连接成功！（已验证密码和路由器可达）" |
| `failed` | true | `wrong_password` | ❌ "连接失败：密码错误" |
| `failed` | true | `auth_rejected` | ❌ "连接失败：认证被拒绝" |
| `failed` | true | `network_not_found` | ❌ "连接失败：找不到目标网络" |
| `failed` | true | `connect_timeout` | ❌ "连接失败：连接超时" |
| `timeout` | true | `connect_timeout` | ⏱ "连接超时" |
| `cancelled` | true | `manual_stop` | "已取消" |
| `error` | true | `switch_managed_failed` | ⚠ "错误：网卡切换失败" |
| `error` | true | `wpa_supplicant_start_failed` | ⚠ "错误：wpa_supplicant 启动失败" |
| `error` | true | `monitor_restore_failed` | ⚠ "错误：恢复监听模式失败" |
| `error` | true | `stop_others_timeout` | ⚠ "错误：停止其他任务超时" |
| `error` | true | `worker_restarted` | ⚠ "错误：系统重启，任务中断" |

**字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `state` | string | 当前状态 |
| `active` | bool | 是否正在运行（终态为 false） |
| `terminal` | bool | 是否终态（终态为 true，不再变化） |
| `connect_id` | string | 任务唯一标识 |
| `ssid` | string | 目标 SSID |
| `bssid` | string/null | 目标 BSSID |
| `timeout` | int | 超时秒数 |
| `started_at` | float | 任务启动时间戳 |
| `elapsed_seconds` | float | 已运行秒数（运行中实时更新） |
| `connect_result` | string/null | 连接结果：`success`/`failed`/`timeout`/`cancelled`/`error` |
| `result` | string/null | 终态结果（同 connect_result） |
| `reason` | string/null | 终态原因（见上表） |
| `ip_address` | string/null | 未使用（当前为 null） |

---

### 3.3 取消 WiFi 连接

**接口**：`POST /api/v1/wifi/connect/stop`

**用途**：取消正在进行的 WiFi 连接

**请求参数**：无

**成功响应**（code=0）：

```json
{
  "code": 0,
  "data": {
    "stopping": true,
    "message": "已请求取消，请继续轮询 /api/v1/ptz/status"
  },
  "msg": "ok"
}
```

**失败响应**：

| HTTP 状态 | code | reason | msg | 说明 |
|-----------|------|--------|-----|------|
| 404 | 2002 | `wifi_connect_not_running` | 当前没有可取消的 WiFi 连接任务 | 无任务可取消 |

**前端调用示例**：

```javascript
const response = await fetch('/api/v1/wifi/connect/stop', {
  method: 'POST'
});
const result = await response.json();
if (result.code === 0) {
  // 成功请求取消，继续轮询状态直到终态
  pollStatus();
} else {
  // 失败
  showError(result.msg);
}
```

---

### 3.4 通过主状态接口获取 WiFi 连接状态

**接口**：`GET /api/v1/ptz/status`

**用途**：获取系统整体状态，包含 WiFi 连接状态

**响应中的 wifi_connect 字段**：

```json
{
  "code": 0,
  "data": {
    "ptz": {...},
    "full_scan": {...},
    "location_scan": {...},
    "capture": {...},
    "wifi_connect": {
      "state": "connecting",
      "active": true,
      "terminal": false,
      "connect_id": "wificonn_1781159332582",
      "ssid": "QWQ",
      "elapsed_seconds": 5.0
    }
  }
}
```

**说明**：`wifi_connect` 字段的结构与 `GET /api/v1/wifi/connect/status` 返回的 `data` 字段相同。

---

## 4. 前端交互流程

### 4.1 发起连接

```
用户点击"连接尝试" → 弹窗输入密码 → 确认
    ↓
POST /api/v1/wifi/connect
    ↓
code === 0 ?
    ├─ 是 → 显示"正在连接..."，开始轮询状态
    └─ 否 → 显示错误信息
```

### 4.2 轮询状态

```javascript
async function pollStatus(connectId) {
  const interval = setInterval(async () => {
    const response = await fetch('/api/v1/wifi/connect/status');
    const result = await response.json();
    
    if (result.code !== 0) {
      clearInterval(interval);
      showError('查询状态失败');
      return;
    }
    
    const data = result.data;
    
    // 更新 UI
    updateStatusUI(data);
    
    // 检查是否终态
    if (data.terminal) {
      clearInterval(interval);
      showFinalResult(data);
    }
  }, 1000); // 每秒轮询一次
}
```

### 4.3 显示结果

```javascript
function showFinalResult(data) {
  if (data.state === 'connected' && data.connect_result === 'success') {
    showSuccess('✅ 连接成功！（已验证密码和路由器可达）');
  } else if (data.state === 'failed') {
    const reasonMap = {
      'wrong_password': '密码错误',
      'auth_rejected': '认证被拒绝',
      'network_not_found': '找不到目标网络',
      'connect_timeout': '连接超时'
    };
    showError(`❌ 连接失败：${reasonMap[data.reason] || data.reason}`);
  } else if (data.state === 'timeout') {
    showWarning('⏱ 连接超时');
  } else if (data.state === 'cancelled') {
    showInfo('已取消');
  } else if (data.state === 'error') {
    showError(`⚠ 错误：${data.reason}`);
  }
}
```

### 4.4 取消连接

```javascript
async function cancelConnect() {
  const response = await fetch('/api/v1/wifi/connect/stop', {
    method: 'POST'
  });
  const result = await response.json();
  
  if (result.code === 0) {
    showInfo('正在取消...');
    // 继续轮询状态，等待终态
  } else {
    showError(result.msg);
  }
}
```

---

## 5. 资源互斥

WiFi 连接运行中（`active=true`）时，以下接口会返回 409：

| 接口 | 说明 |
|------|------|
| `POST /api/v1/ptz/full_area_scan/start` | 启动全面扫描 |
| `POST /api/v1/ptz/location_scan/start` | 启动定位扫描 |
| `POST /api/v1/capture/start` | 启动独立抓包 |
| `POST /api/v1/capture/save_pcap` | 保存 pcap |
| `POST /api/v1/ptz/capture_at_best` | 最佳位置抓包 |
| `POST /api/v1/ptz/move` | 云台移动 |

**前端应在 `wifi_connect.active=true` 时禁用以上所有操作的按钮。**

以下接口不受限制：
- `POST /api/v1/ptz/stop` — 停止 PTZ
- `POST /api/v1/capture/stop` — 停止抓包
- `POST /api/v1/wifi/connect/stop` — 取消 WiFi 连接

---

## 6. 测试用例

### 6.1 连接成功

```bash
curl -sS -X POST "http://127.0.0.1:5000/api/v1/wifi/connect" \
  -H "Content-Type: application/json" \
  -d '{"ssid": "QWQ", "password": "正确的密码", "timeout": 60}' \
  | python3 -m json.tool
```

预期结果：
- `state` 从 `queued` → `stopping_others` → `switching_to_managed` → `connecting` → `connected` → `restoring_monitor` → `connected`（终态）
- 终态：`terminal=true`, `connect_result="success"`, `reason=null`

### 6.2 密码错误

```bash
curl -sS -X POST "http://127.0.0.1:5000/api/v1/wifi/connect" \
  -H "Content-Type: application/json" \
  -d '{"ssid": "QWQ", "password": "wrongpassword", "timeout": 60}' \
  | python3 -m json.tool
```

预期结果：
- 终态：`state="failed"`, `connect_result="failed"`, `reason="wrong_password"`

### 6.3 网络不存在

```bash
curl -sS -X POST "http://127.0.0.1:5000/api/v1/wifi/connect" \
  -H "Content-Type: application/json" \
  -d '{"ssid": "不存在的WiFi_12345", "password": "12345678", "timeout": 30}' \
  | python3 -m json.tool
```

预期结果：
- 终态：`state="failed"`, `connect_result="failed"`, `reason="network_not_found"`

### 6.4 取消连接

```bash
# 1. 发起连接
curl -sS -X POST "http://127.0.0.1:5000/api/v1/wifi/connect" \
  -H "Content-Type: application/json" \
  -d '{"ssid": "QWQ", "password": "12345678", "timeout": 60}'

# 2. 取消连接
curl -sS -X POST "http://127.0.0.1:5000/api/v1/wifi/connect/stop"
```

预期结果：
- 终态：`state="cancelled"`, `connect_result="cancelled"`, `reason="manual_stop"`

### 6.5 防重入

```bash
# 1. 发起连接
curl -sS -X POST "http://127.0.0.1:5000/api/v1/wifi/connect" \
  -H "Content-Type: application/json" \
  -d '{"ssid": "QWQ", "password": "12345678", "timeout": 60}'

# 2. 再次发起（应该返回 409）
curl -sS -X POST "http://127.0.0.1:5000/api/v1/wifi/connect" \
  -H "Content-Type: application/json" \
  -d '{"ssid": "QWQ", "password": "12345678", "timeout": 60}'
```

预期结果：
- 第二次请求返回：`code=2001`, `reason="wifi_connect_already_running"`

---

## 7. 注意事项

1. **轮询间隔**：建议 1 秒轮询一次，不要太频繁（会增加服务器负担）
2. **超时处理**：前端应设置合理的超时（建议比后端 timeout 多 10 秒），避免永久等待
3. **错误处理**：网络错误、服务器错误等情况需要妥善处理
4. **UI 状态**：连接过程中应显示加载动画，禁用相关按钮
5. **取消操作**：取消后应继续轮询直到终态，确保状态一致
