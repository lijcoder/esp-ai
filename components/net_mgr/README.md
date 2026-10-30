# net_mgr —— WiFi / 重连 / SNTP（L1 基础服务）

**作用**：把"设备联网"收敛成一个基础服务：STA 连接、断线退避重连、SNTP 对时，并把网络状态经 `event_bus` 通告给上层（`APP_EVT_NET_UP` / `APP_EVT_NET_DOWN`）。同层模块（`transport_ws` 等）不直接碰 WiFi 驱动，只订阅事件或调 `net_is_up()`。

## 对外接口

| 接口 | 说明 |
|---|---|
| `net_init()` | 初始化 STA 并开始连接（非阻塞）；未配置 `CONFIG_AI_WIFI_SSID` 时只 WARN 并进入 `IDLE`，本地功能不受影响；重复调用返回 `ESP_ERR_INVALID_STATE` |
| `net_is_up()` | 是否已获得 IP（可收发） |
| `net_wait_ready(ms)` | 等待获得 IP，`0` 表示只查询；**禁止在音频实时任务调用** |
| `net_is_time_synced()` | SNTP 是否已对时（TLS 证书校验前需要） |
| `net_get_info(&info)` | 状态快照：state / ip / rssi / ssid / 重连次数 / 当前退避 / 是否对时 |
| `net_reconnect()` | 主动断开并重连（重置退避、立即发起）；已联网时先投 `NET_DOWN` |
| `net_state_name()` | 状态名（日志/调试口用） |

## 事件契约（ARCHITECTURE §4.2 / §8.5）

| 事件 | 何时投递 | payload |
|---|---|---|
| `APP_EVT_NET_UP` | 获得 IP（DHCP 成功） | `ip`（字符串）+ `rssi`（dBm） |
| `APP_EVT_NET_DOWN` | **已联网后**掉线（`WIFI_EVENT_STA_DISCONNECTED`） | `reason`（驱动原因码）+ `retry_ms`（本次退避延时） |

上电首次连不上（从未联网过）**不投** `NET_DOWN`：上层无需处理"还没连上"这种正常中间状态。`retry_ms` 与 ROM 里的退避序列一致：1s → 2s → 5s → 10s → 30s（之后固定 30s，无限重试，ARCHITECTURE §6.1）。

## 依赖

- IDF：`esp_wifi`、`esp_netif`（含 `esp_netif_sntp`）、`esp_event`（系统默认循环）、`esp_timer`（一次性退避定时器）。
- 组件：`event_bus`（L1，投递网络事件）。
- 配置：`main/Kconfig.projbuild` 的 `CONFIG_AI_WIFI_SSID` / `CONFIG_AI_WIFI_PASSWORD`。

## Kconfig

| 配置 | 默认 | 说明 |
|---|---|---|
| `CONFIG_NET_SNTP_SERVER` | `ntp.aliyun.com` | NTP 服务器；对时失败只影响 TLS 证书校验 |

退避序列、任务/存储策略是架构常量，不做成 Kconfig（改需先改 ARCHITECTURE §6.1/§10.1）。

## 关键约束

- **无独立任务**：WiFi/IP 事件在 IDF 系统默认事件循环任务里回调，退避重连用一次性 `esp_timer`；不新增常驻任务（ARCHITECTURE §9.3 没有 net 任务）。
- 凭证唯一来源是 Kconfig：驱动存储用 `WIFI_STORAGE_RAM`，避免 NVS 里残留旧 SSID/密码覆盖配置。
- **密码与 token 不得进日志**：本组件只打印 SSID、IP、RSSI、原因码。
- 关闭 WiFi 省电（`WIFI_PS_NONE`）：语音要低时延，避免 modem sleep 引入抖动。
- `net_wait_ready()` 会阻塞调用任务，只能在任务上下文使用；音频实时任务（`afe_feed`/`afe_fetch`）禁止调用。
- 事件处理器运行在系统事件任务上下文，只做日志/状态更新/投递事件，不做耗时操作。
- 网络状态变化日志用 TAG `NET`（CONVENTIONS §5）。

## 调试

`diag` 提供命令（ARCHITECTURE §10.2）：

```
wifi status      # 状态/SSID/IP/RSSI/SNTP/重连次数与当前退避
wifi reconnect   # 主动断开并重连（重置退避），用于验证重连链路
```

配合 `evt trace on` 可直接看到 `NET_UP`/`NET_DOWN` 的分发结果（T1.2a 的观测命令）。

## 后续

- T1.4 `transport_ws` 订阅 `NET_UP` 才启动连接、收到 `NET_DOWN` 时停止并按需重连。
- P3 的 `voice_session` 在断网时播放"网络未连接"提示音（ARCHITECTURE §8.5）。
- 配网（F1）：凭证来源抽象为 `wifi_cred_provider`（当前实现为 Kconfig 常量，ARCHITECTURE 附录 A）。
- `net ping <host>` 仍在 T5.1 补（需引入 `esp_ping`，本任务不涉及）。
