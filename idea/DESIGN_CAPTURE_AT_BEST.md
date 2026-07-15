# 设计方案：扫描中一键最强点抓包

> 状态：待讨论 | 创建：2026-05-31

---

## 一、需求描述

在全面扫描或定位扫描进行中，前端实时展示已采集的点位信息（MAC、RSSI 等）。用户发现可疑目标后，可以**点击某个 MAC 直接发起抓包**。

系统自动完成以下流程：
1. 停止当前正在进行的全面扫描 / 定位扫描
2. 从 Redis 读取该 MAC 在当前扫描进度中 RSSI 最强的点位坐标
3. 将云台移动到该最强点
4. 切换到该 MAC 对应的信道/带宽，开始持续抓包（存 pcap）
5. 等待用户手动停止抓包

---

## 二、整体流程

```
前端用户点击 MAC "AA:BB:CC:DD:EE:FF"
    │
    ▼
POST /api/v1/ptz/capture_at_best
  { "mac": "AA:BB:CC:DD:EE:FF", "pcap_filename": "target.pcap" }
    │
    ├── 1. 停止当前扫描（复用现有停止机制）
    │      全面扫描 → r.set('multi_scan:stop_full_area_scan', '1')
    │      定位扫描 → r.set('location_scan:stop', '1')
    │      等待扫描状态变为非 running（轮询，超时 30s）
    │
    ├── 2. 从 Redis 读取该 MAC 最强点位
    │      全面扫描 → 遍历 full_scan:results 中所有点位，找 rssi_avg 最大的那个
    │      定位扫描 → 直接读 location_scan:result[mac].best_position
    │
    ├── 3. 推送云台移动命令到 ptz:command_queue
    │      { "action": "move_to_best_capture",
    │        "pan": ..., "tilt": ..., "mac": "...",
    │        "channel": ..., "bandwidth": ...,
    │        "pcap_filename": "..." }
    │
    ├── 4. ptz_control 移动到位后，向 capture:command_queue 推送抓包命令
    │      复用现有 start_capture 逻辑（持续抓包，不限时长）
    │
    └── 5. 返回响应，前端展示"正在抓包"状态
            用户点击停止 → POST /api/v1/capture/stop（已有接口）
            用户保存文件 → POST /api/v1/capture/save_pcap（已有接口）
```

---

## 三、API 设计

### 新增接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/ptz/capture_at_best` | 扫描中一键最强点抓包 |

#### 请求参数

```json
{
  "mac": "AA:BB:CC:DD:EE:FF",       // 必填，目标 MAC
  "pcap_filename": "target.pcap"     // 选填，不传则自动生成
}
```

#### 响应

```json
{
  "code": 0,
  "data": {
    "best_position": {"pan": 120.0, "tilt": 15.0},
    "best_rssi": -45,
    "channel": 6,
    "bandwidth": "HT20",
    "source_scan": "location_scan"   // 或 "full_area_scan"
  },
  "msg": "ok"
}
```

#### 错误场景

| code | msg | 含义 |
|------|-----|------|
| -1 | 当前无扫描任务 | Redis 中找不到扫描结果 |
| -1 | 该 MAC 无有效点位数据 | 扫描结果中没有此 MAC 的记录 |
| -1 | 扫描停止超时 | 30s 内扫描未停止 |

### 复用已有接口

| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/api/v1/capture/stop` | 用户停止抓包 |
| POST | `/api/v1/capture/save_pcap` | 用户保存 pcap 文件 |

---

## 四、最强点位查找逻辑

### 定位扫描（优先，数据最完整）

`location_scan:result` 已有 `best_position` 和 `best_rssi`，直接读取：

```python
result = json.loads(r.get('location_scan:result') or '{}')
mac_data = result.get('results', {}).get(target_mac)
if mac_data and mac_data.get('best_position'):
    pos = mac_data['best_position']  # {"pan": ..., "tilt": ...}
    rssi = mac_data['best_rssi']
    channel = mac_data['channel']
    bandwidth = mac_data['bandwidth']
```

### 全面扫描（需遍历计算）

`full_scan:results` 的 `results` 是按点位存储的，需要遍历找最强：

```python
result = json.loads(r.get('full_scan:results') or '{}')
points = result.get('results', {})
best = None
for point_key, point_data in points.items():
    for sr in point_data.get('scan_results', []):
        if sr.get('mac') == target_mac:
            rssi = sr.get('rssi_avg')
            if best is None or (rssi is not None and rssi > best['rssi']):
                best = {
                    'pan': point_data['pan'],
                    'tilt': point_data['tilt'],
                    'rssi': rssi,
                    'channel': sr.get('channel'),
                    'bandwidth': sr.get('bandwidth')
                }
```

### 两个扫描都有结果时

优先使用**定位扫描**的结果（精度更高）。

---

## 五、涉及修改的文件

| 文件 | 修改内容 |
|------|----------|
| `web_server.py` | 新增 `/api/v1/ptz/capture_at_best` 端点；新增 `_find_best_position_for_mac()` 辅助函数 |
| `ptz_control.py` | 新增 `move_to_best_capture` 命令分支：移动到位 → 推送抓包命令 |
| `ARCHITECTURE.md` | 补充新接口文档 |

### 不需要修改的部分

- `capture_worker.py`：直接复用现有 `start_capture` 行为，无需改动
- `grid_utils.py`：不涉及网格计算变更
- 停止逻辑：完全复用现有的 `multi_scan:stop_full_area_scan` 和 `location_scan:stop` 机制

---

## 六、时序图

```
用户(前端)     web_server       ptz_control     capture_worker
    │               │                │                │
    │──点击MAC──────>│                │                │
    │               │                │                │
    │               │──停止扫描──────>│                │
    │               │  r.set(stop)   │                │
    │               │                │──检查stop──────>│
    │               │                │  (扫描停止)     │
    │               │                │                │
    │               │──读Redis最强点  │                │
    │               │  (本地完成)     │                │
    │               │                │                │
    │               │──移动+抓包命令─>│                │
    │               │  command_queue  │──移动到pan/tilt │
    │               │                │                │
    │               │                │──开始抓包命令──>│
    │               │                │  capture_queue  │
    │               │                │                │──开始sniff
    │               │                │                │
    │<──响应────────│                │                │
    │(best_pos/rssi)│                │                │
    │               │                │                │
    │               │                │                │ (持续抓包)
    │               │                │                │
    │──停止抓包─────>│                │                │
    │               │──stop命令──────────────────────>│
    │               │                │                │──停止sniff
    │               │                │                │
    │──保存pcap─────>│                │                │
    │               │──save_pcap命令──────────────────>│
    │               │                │                │──写入文件
```

---

## 七、边界情况与注意事项

| 场景 | 处理方式 |
|------|----------|
| 扫描已停止/已完成 | 直接读 Redis 结果，跳过停止步骤 |
| 全面扫描无结果（刚启动） | 返回错误"当前无有效扫描数据" |
| MAC 在扫描结果中不存在 | 返回错误"该 MAC 无有效点位数据" |
| 同时有定位扫描和全面扫描结果 | 优先使用定位扫描结果 |
| 用户不保存 pcap 直接开始新扫描 | pcap 数据留在内存中，新扫描启动时自然清空 |
| 扫描停止超时 | 30s 超时后返回错误，但不阻塞（扫描最终会自行停止） |
| pcap_filename 未传 | 自动生成格式：`capture_{mac}_{timestamp}.pcap` |

---

## 八、后续可扩展

- **自动恢复扫描**：抓包结束后，询问用户是否恢复之前的扫描任务
- **多 MAC 批量抓包**：支持选择多个 MAC 依次到各自最强点抓包
- **最强点实时更新**：扫描过程中前端展示最强点标记，用户可直观看到
