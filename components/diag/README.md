# diag —— 调试与观测（L3 能力层）

**作用**：提供 P0 之后所有任务都要用的观测能力 —— 启动信息横幅、UART0 上的 `esp_console` REPL、系统/日志调试命令、可选的堆水位周期监控。

## 对外接口

| 接口 | 说明 |
|---|---|
| `diag_init()` | 打印启动横幅 → 在 UART0（115200，GPIO43/44）起 REPL → 注册 `help`/`sys`/`log`/`mic`/`cfg`/`evt`/`wifi`/`ws`/`vc`/`vs`；只能在 `app_main` 最前部调用一次，重复调用返回 `ESP_ERR_INVALID_STATE` |

## console 命令（本轮）

| 命令 | 作用 |
|---|---|
| `help` | 列出所有命令 |
| `sys info` | 应用版本/编译时间、IDF 版本、芯片型号与核数、复位原因、运行时长、内部 RAM 水位 |
| `sys heap` | 内部 RAM 与 PSRAM 的总量/剩余/最大块/最低水位/峰值占用 |
| `sys tasks` | 每个任务的名称、状态、优先级、栈剩余（字节）、CPU 累计占比（双核总算力口径，所有任务相加≈200%） |
| `sys reboot` | 软复位 |
| `log` | 列出 16 个固定 TAG 的当前日志级别（含 `*` 全局级别） |
| `log <tag>\|\* <level>` | 运行时调整日志级别，级别取 `none/error/warn/info/debug/verbose`；未知 TAG 或非法级别直接报错（不静默生效） |
| `mic stat` | 麦克风统计窗口：电平 RMS/峰值（dBFS）、窗口时长与采样数、已喂帧数、I2S 读失败次数、收音开关、VAD、累计唤醒次数 |
| `mic reset` | 清零麦克风统计窗口（帧数、失败次数、唤醒次数、电平一起清零） |
| `cfg info` | NVS 分区条目用量（已用/空闲）+ 当前生效音量；提示 `cfg set` 的语义 |
| `cfg get <key>` | 先按 int 读、再按 str 读；未设置时明确提示"未设置（使用缺省）"，不报错；返回的是 **NVS 原始值**（不做 clamp），业务语义值看 `cfg info` 或对应模块 API |
| `cfg set <key> <int>` | **底层存储调试口**：原样写 int 到 NVS（不做范围/语义校验），重启后生效 |
| `cfg setstr <key> <value>` | 写字符串（单参数，不支持空格） |
| `cfg erase <key>` | 删除键，重启后恢复缺省；键本来不存在时提示而不算错误 |
| `evt list` | 列出 `APP_EVT` 的 13 个事件与是否带 payload（与 ARCHITECTURE §4.2 一致） |
| `evt post <name\|id>` | 投递一个事件（网络事件自动带示例 payload），用于验证投递链路 |
| `evt burst <n>` | 连续投递 n 个（1~256）无 payload 事件，验证"非阻塞 + 队列满丢弃"路径 |
| `evt trace on\|off` | 订阅/取消订阅全部事件，在 `evt` 任务上下文打印收到的每个事件（验证跨任务分发，而不是只看投递） |
| `vc status` / `vc stat` | 语音云观测（T2.5）：链路三态与 sid、轮次/上行帧/字节/首帧耗时、下行文本与音频帧、`sid` 丢弃/解析失败/云端 error 计数 |
| `vc start [sid]` | 手动发起一轮端云会话（云端未 READY 时拒绝；不带 sid 则自增） |
| `vc stop` / `vc cancel` | 优雅收尾（补尾帧 + audio_end）/ 立即放弃（不发 audio_end） |
| `vc reset` | 清零 VC 统计（保留最近 sid/用时） |
| `vs status` / `vs stat` | 会话状态机观测（T3.1）：当前状态与 sid、VC 状态、轮次（开始/完成/异常/中止）、打断/唤醒丢弃/超时/云端 error 计数、迁移次数与上次状态停留时长 |
| `vs start` | 手动开始一轮会话（不播唤醒应答提示音，直接进入 `LISTENING`；已有会话时拒绝） |
| `vs interrupt` | 打断当前会话：`vc cancel` + 回 `IDLE`（R4 完整淡出与按键来源归 T3.2/T3.4） |
| `vs reset` | 清零 VS 统计（保留最近 sid/停留时长） |
| `wifi status` | 网络状态：state、SSID、IP、RSSI、SNTP 是否对时、累计重连次数与当前退避 |
| `wifi reconnect` | 主动断开并重连（重置退避）；已联网时先投 `NET_DOWN` 再恢复，用于验证重连链路 |
| `ws status` | WebSocket 状态：状态机、脱敏端点（不回显 token）、握手次数、心跳 RTT、收发/丢弃/坏帧计数、队列占用 |
| `ws reconnect` | 重置退避并立即重连（非阻塞） |
| `ws send <json>` | **开发期探针**：参数用空格拼成 JSON 原样 `ws_send_text()`（如 `ws send {"t":"audio_start","sid":1}`），不解析字段、不造协议 |
| `ws sendbin [len]` | **开发期探针**：发一帧 `0x01` 上行音频（探针图案，默认 40B，上限 248B），验证 8B 帧头与 `seq` 自增 |
| `ws rxlog on\|off` | **开发期探针**：注册/注销下行帧打印（文本全量≤200B、二进制只打首帧与每 25 帧） |
| `sys crash` | **仅开发配置**（`CONFIG_DIAG_ENABLE_CRASH_TEST=y`）：主动触发 panic，验证 coredump 落盘与解析；未编译时明确提示 |

> `ws send/sendbin/rxlog` 是 T1.6 为"P1 还没有 `voice_cloud`、固件里没有上行发送方"补的联调探针，不是业务接口；T2.5 落地后只作调试口保留（rxlog 从 T2.5 起走 `ws_subscribe()` 附加订阅，与语音业务回调并存）。
> **`rxlog` 与 `voice_cloud` 互斥**：两者都走 `ws_register_cb()`（单回调），T2.5 注册后调试时不要同时开 `rxlog`。

`mic stat` 的统计口径是**送 AFE 之前的麦克风真实数据**；半双工暂停收音（R1）时电平仍在累计，可用来确认"麦克风还在采、只是 AFE 吃静音"。RMS 的 `sqrt/log10` 在 console 任务侧计算，音频任务只做整数累加。

## coredump 验证流程（`sys crash`）

1. `sdkconfig.defaults.esp32s3` 里 `CONFIG_DIAG_ENABLE_CRASH_TEST=y`（**量产固件必须关掉**）；
2. 串口敲 `sys crash` → 打印 panic backtrace → 日志出现 `Core dump has been saved to flash.`；
3. 设备自动重启，启动日志出现 `Found core dump <N> bytes in flash @ 0x9a0000`（实测 14,404 B / 64K）；
4. `idf.py -p <PORT> coredump-info` 解析：崩溃任务、panic reason、寄存器、**所有任务的调用栈（含源文件行号）**；
5. 清理现场：`python -m esptool --chip esp32s3 -p <PORT> erase_region 0x9a0000 0x10000`；
6. 解析依赖崩溃固件对应的 `build/esp-ai.elf`——崩溃后**先备份 elf 再改代码**（见 CONVENTIONS §8）。

## 已知 TAG

与 `docs/CONVENTIONS.md` §5 的固定清单保持一致，是 `log` 命令的唯一数据源：
`APP / BOARD / AUDIO_IO / AFE / PLAYER / ENC / NET / WS / VC / VS / MC / MS / DEV / DIAG / CFG / EVT`。
新增组件时**两处同步**：CONVENTIONS §5（及 ARCHITECTURE §10.2）+ 本组件的 `k_tags[]`。

## 依赖

- 组件：`console`（esp_console/linenoise）、`esp_app_format`（`esp_app_get_description`）、`esp_timer`（运行时长）；`audio_fe`（`mic stat` 读统计）、`config_store`（`cfg` 读写 NVS）、`event_bus`（`evt` 投递/订阅）、`net_mgr`（`wifi status/reconnect`）、`transport_ws`（`ws status/reconnect/send/sendbin/rxlog`）、`voice_cloud`（`vc status/start/stop/cancel`）、`voice_session`（`vs status/start/interrupt/reset`），均为**私有依赖**，不对外暴露头文件。
- IDF 配置：`CONFIG_FREERTOS_USE_TRACE_FACILITY`（任务枚举）与 `CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS` + `COUNTER_TYPE_U64`（CPU 占比，64 位计数避免 esp_timer 32 位约 71 分钟回绕）。已固化在 `sdkconfig.defaults.esp32s3`；若关掉 trace facility，`sys tasks` 会提示不可用而不是编译失败。
- 硬件：UART0 115200，GPIO43/44（见 `docs/HARDWARE.md`）。

## Kconfig

| 配置 | 默认 | 说明 |
|---|---|---|
| `CONFIG_DIAG_HEAP_MONITOR` | n | 周期堆水位监控任务；默认关，避免污染常规日志 |
| `CONFIG_DIAG_HEAP_MONITOR_INTERVAL_S` | 30 | 打印间隔（5–3600 秒），仅在上项开启时生效 |

## 关键约束

- `diag_init()` 会安装 UART0 驱动并接管 stdin/stdout，必须早于其它模块的初始化日志；`main/app_main.c` 里是第一个调用。
- 命令在 console 任务上下文执行，允许 `printf`/动态分配；**禁止**在音频任务里调用这些命令实现。
- 只读系统状态、不改业务状态（唯一例外是 `sys reboot`），因此下层组件不得反向依赖 `diag`。
- 日志 TAG 固定清单有唯一来源（CONVENTIONS §5），本文件只在"新增组件时同步"这条规则上被引用。
- `sys tasks` 的数组容量用 `uxTaskGetNumberOfTasks()` 定：本内核版本的 `uxTaskGetSystemState` **不支持**"传 `NULL` 查数量"（容量不足时直接返回 0），按旧写法会恒显示"0 个任务"（2026-10-30 T0.7① 真机发现）。

## 后续

- T5.1：补 `wifi` / `net ping` / `audio` / `voice` / `music` 命令（依赖对应模块就绪；`mic stat` 已在 T0.7a 落地）。
- T5.3：用 `CONFIG_DIAG_HEAP_MONITOR` 做 24h 压测的堆水位记录。
