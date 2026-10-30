# transport_ws —— 云端 WebSocket 传输（L1 基础服务）

**作用（一句话）**：把 `net_mgr` 的「有网」变成「能收发」——连接/退避重连、`hello`/`hello_ack` 握手、20s/10s 心跳、8 字节二进制帧收发、非阻塞发送队列。

契约依据：[ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §6.1–6.3（连接/帧格式/文本帧）、§8.5（断线重连流程）；契约定稿记录见 [PROTOCOL.md](../../docs/PROTOCOL.md)「已定稿口径」。
本层**不解析业务 JSON**：`asr/llm/tts/music` 事件原样回调，由 T2.5 `voice_cloud` 解析。

## 对外接口

| 接口 | 说明 |
|---|---|
| `ws_init()` | 解析 URL、建 PSRAM 发送队列与 `ws` 任务、订阅 `APP_EVT_NET_UP/NET_DOWN`；URL 未配置/非法时**降级离线**（只 WARN，本地功能不受影响） |
| `ws_start()` | 请求保持在线：有网即建连，之后自动重连（幂等、非阻塞） |
| `ws_stop()` | 主动下线并停止自动重连（非阻塞） |
| `ws_register_cb(cb, arg)` | 注册入站回调（文本帧 `frame == NULL`，二进制帧带 `{ver,type,seq}`） |
| `ws_send_text(json)` | 文本帧**非阻塞入队**；未 READY 返回 `ESP_ERR_INVALID_STATE`，队列满返回 `ESP_ERR_TIMEOUT` |
| `ws_send_bin(type, payload, len)` | 上行二进制帧，自动填 8B 帧头与自增 `seq`；`type` 只接受 `0x01` |
| `ws_reconnect()` | 重置退避、立即重连（`diag` 的 `ws reconnect`） |
| `ws_is_ready()` / `ws_status_read()` / `ws_state_name()` | 状态查询与观测 |

## 依赖评估（CONVENTIONS §9：引入前先评估）

- **`espressif/esp_websocket_client` 1.8.0，Apache-2.0**，声明在 `idf_component.yml`（组件管理器按使用方解析）。
- 选型理由：RFC 6455 的掩码/分片/关闭握手自己写容易引入协议 bug，而 T1.5 的 mock 用 `websockets` 库、对协议严格；官方组件随 IDF 生态维护，且天然支持 `ws://` 与 `wss://` 双传输（X2 方案 A）与 CRT bundle。
- 代价：新增一个库任务 `ws_client`（核 0 / 优先级 5 / 6KB，见 Kconfig）与 TLS 相关代码（固件 +197KB：TLS 握手 + 证书 bundle）。实测内存见下。

## Kconfig

| 项 | 默认 | 说明 |
|---|---|---|
| `WS_TX_QUEUE_BYTES` | 8192 | 发送队列总容量，**放 PSRAM**（X3 方案 D：原设计 24KB 内部 RAM 改为 8KB PSRAM） |
| `WS_TX_MSG_BYTES` | 256 | 单槽上限，决定槽位数（默认 32 槽） |
| `WS_TASK_STACK` | 6144 | `ws` 状态机任务栈（ARCHITECTURE §9.3） |
| `WS_CLIENT_TASK_STACK` | 6144 | `esp_websocket_client` 库任务栈（要跑 TLS 握手；实测余量约 3.4KB） |
| `WS_RX_BUFFER_BYTES` | 2048 | 下行单帧上限，超出按分片重组、仍超出则丢弃并 WARN |
| `WS_SEND_TIMEOUT_MS` | 1000 | `ws` 任务调用 send 的超时，调用方永不等待 |

## 线程模型与约束

- `ws` 任务（核 0 / 优先级 5 / 6KB）：状态机、退避、心跳、发送队列出队；只在它里面 `start/stop/destroy` 客户端（库禁止在事件回调里调）。
- `ws_client` 库任务（核 0 / 优先级 5 / 6KB）：TCP/TLS/WS 收包，**入站回调在这里执行**——禁止阻塞/耗时操作，`payload` 只在回调内有效。
- **发送一律非阻塞**：上层（含未来 T2.2/T2.5 的音频路径）只入队；队列满丢帧 + 限频 WARN，绝不阻塞音频任务（CONVENTIONS §3）。
- `sid` 校验不在本层（ARCHITECTURE §6.3 的规则由 T3.1 `voice_session` 落地），本层只做帧头合法性、魔数/ver/len/type 校验。
- 鉴权失败（关闭码 4001/4002）：ERROR 后**不自动重试**，等 `NET_UP` 或 `ws reconnect`。
- `wss://` 建连前要求 `net_is_time_synced()`（TLS 校验需要可信时间）；`ws://` 跳过该门闸并打 WARN（仅本地联调）。
- 日志只打 `scheme://host[:port]/path`，**不回显 query**（含 token）。

## 真机联调（2026-10-31，本地 `tools/ws_mock_server.py`）

| 项 | 实测 |
|---|---|
| 上电 → 建连 → hello_ack | NET_UP 3.70s，WS 建连 3.70s，握手完成 3.65s（wss 对时后 1.6s 内完成 TLS+WS） |
| 心跳 | 20s 一次 `ping`，mock 回 `pong`；RTT 实测 94~234ms；`--drop-pong` 下 10s 判离线并重连 |
| 断线重连 | 服务端 `--close-after` 关闭后 1.43s 重建并重发 hello（验收要求 < 5s） |
| 退避 | 服务端不可达时严格 1/2/5/10/30s，恢复后下一次重试即连上 |
| 网络断开 | `wifi reconnect` → `NET_DOWN` 停止 WS；`NET_UP` 后 130ms 内重建链路 |
| 鉴权失败 | token 不匹配 → 关闭码 4002 → ERROR 且不再自动重试；`ws reconnect` 可恢复 |
| 坏帧容错 | 坏 magic/ver/len、未知 type、3021B 超长消息 → 全部丢弃 + WARN，连接保持 READY |
| hello_ack 超时 | 5s 判定失败并退避（日志因同步 teardown 晚约 1s） |

## 后续

- T2.5 `voice_cloud` 在本层之上解析业务 JSON、按 `sid` 过滤；T2.4 消费下行二进制帧做流式播放。
- 内存：wss/TLS 握手期内部 RAM 最低水位 27,655 B（略低于 CONVENTIONS §4 的保留 30KB 线），统一优化按 [PRODUCT_IDEAS §8 X3](../../docs/PRODUCT_IDEAS.md) 排在 P2 前（触发条件已命中）。
- 若 T2.4 的 TTS 帧率高，需评估 `WEBSOCKET_EVENT_DATA` 事件队列与 PSRAM 预缓冲（本层 rx 缓冲 2KB 内部 RAM）。
