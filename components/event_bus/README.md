# event_bus —— 全局事件定义与分发（L1 基础服务）

**作用**：把 ARCHITECTURE §4.2 的全局事件固化成唯一的事件定义与投递/订阅入口；同层模块之间禁止直接互调（CONVENTIONS §2），一律经这里通信。

## 对外接口

| 接口 | 说明 |
|---|---|
| `app_evt_init()` | 创建独立 esp_event 循环 + `evt` 分发任务（核 0 / 优先级 5 / 栈 4KB）；重复调用返回 `ESP_ERR_INVALID_STATE` |
| `app_evt_register(evt, cb, arg)` | 注册处理器；`APP_EVT_ANY` 订阅全部应用事件；回调在 `evt` 任务上下文执行 |
| `app_evt_unregister(evt, cb, arg)` | 注销处理器（三个参数须与注册时完全一致） |
| `app_evt_post(evt, data)` | 非阻塞投递；`data == NULL` 表示无 payload；队列满返回 `ESP_ERR_TIMEOUT` |
| `app_evt_name(evt)` | 事件名字符串（日志/调试口用） |

## 事件与 payload

| 事件 | payload |
|---|---|
| `APP_EVT_NET_UP` | `app_evt_net_up_t { ip[16], rssi }` |
| `APP_EVT_NET_DOWN` | `app_evt_net_down_t { reason, retry_ms }` |
| 其余 11 个事件 | 暂无（post 时传 `NULL`），使用方落地时补定义并同步 ARCHITECTURE §4.2 |

payload 统一装进定长 32 字节的 `app_evt_data_t`：投递时整块拷贝，**发布方不需要保活数据**，处理器拿到的 `data` 只在本次回调内有效。文本类 payload（ASR/LLM/TTS）的所有权语义留给 T2.5/T3.1 定稿，不要自行往 union 里加变长字段。

## 依赖

- IDF：`esp_event`（本组件自建独立循环，不占用系统 default loop）。
- 组件：无（L1 只依赖 IDF；不得反向依赖 `net_mgr`/`transport_ws` 等使用方）。

## Kconfig

| 配置 | 默认 | 说明 |
|---|---|---|
| `CONFIG_EVENT_BUS_QUEUE_SIZE` | 16 | 事件循环队列深度；满时非阻塞丢弃并打 WARN |

任务属性（核 0 / 优先级 5 / 栈 4096 B / 任务名 `evt`）是 ARCHITECTURE §9.3 的定稿值，改动必须先改架构文档。

## 关键约束

- **音频实时任务（`afe_feed`/`afe_fetch`）只允许投递无 payload 事件**：esp_event 在带 payload 时会内部 `calloc` 一份拷贝（IDF 5.4 实现），在音频任务里调用违反 CONVENTIONS §3 的"禁止 malloc/阻塞"。
- 投递一律非阻塞（`ticks_to_wait = 0`），失败立即返回错误码 + WARN，不允许在音频/实时路径上等待。
- 处理器运行在 `evt` 任务上下文：不要做耗时操作，也不要在处理器里阻塞等待别的任务（会卡住整个事件分发）。
- 处理器槽位固定 8 个（`EVT_MAX_HANDLERS`），注册失败返回 `ESP_ERR_NO_MEM`；P1 用量：diag 的 `evt trace` 占 1 个。
- 事件 ID 数值即调试口可见的 ID：只在末尾追加，禁止插队/改序，改必须同步 ARCHITECTURE §4.2。

## 调试

`diag` 提供命令：

```
evt list                          # 列出 13 个事件与 payload 支持情况
evt post <name|id>                # 投递一个事件（网络事件带示例 payload）
evt burst <n>                     # 连续投递 n 个无 payload 事件，验证非阻塞与队列满路径
evt trace on|off                  # 订阅/取消订阅全部事件，在 evt 任务上下文打印 INFO
```

配合 `log EVT debug` 可看到 `app_evt_post()` 的投递打点（该打点属 DEBUG 级：本工程 `CONFIG_LOG_MAXIMUM_LEVEL=INFO` 时**编译期被裁掉**，要看需在 menuconfig 把 Log maximum level 调到 Debug 并重编；`evt trace on` 是运行期开关，不受此限制，看到的是**分发侧**，可证明事件真的跨任务送达处理器）。

## 后续

- T1.2b：把 `main`/`audio_fe` 的唤醒、VAD 接线迁到事件（随 T2.5/T3.1 落地，避免 P2 返工）。
- T1.3 `net_mgr` 投递 `NET_UP`/`NET_DOWN`；T1.4 `transport_ws` 订阅 `NET_UP` 触发连接。
- 事件数增多后如果处理器槽位不够，再评估扩容或改成"每事件独立注册"的接口，并同步 README 与 ARCHITECTURE §4.2。
