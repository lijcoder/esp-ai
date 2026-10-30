# diag —— 调试与观测（L3 能力层）

**作用**：提供 P0 之后所有任务都要用的观测能力 —— 启动信息横幅、UART0 上的 `esp_console` REPL、系统/日志调试命令、可选的堆水位周期监控。

## 对外接口

| 接口 | 说明 |
|---|---|
| `diag_init()` | 打印启动横幅 → 在 UART0（115200，GPIO43/44）起 REPL → 注册 `help`/`sys`/`log`；只能在 `app_main` 最前部调用一次，重复调用返回 `ESP_ERR_INVALID_STATE` |

## console 命令（本轮）

| 命令 | 作用 |
|---|---|
| `help` | 列出所有命令 |
| `sys info` | 应用版本/编译时间、IDF 版本、芯片型号与核数、复位原因、运行时长、内部 RAM 水位 |
| `sys heap` | 内部 RAM 与 PSRAM 的总量/剩余/最大块/最低水位/峰值占用 |
| `sys tasks` | 每个任务的名称、状态、优先级、栈剩余（字节）、CPU 累计占比 |
| `sys reboot` | 软复位 |
| `log` | 列出 15 个固定 TAG 的当前日志级别（含 `*` 全局级别） |
| `log <tag>\|\* <level>` | 运行时调整日志级别，级别取 `none/error/warn/info/debug/verbose`；未知 TAG 或非法级别直接报错（不静默生效） |

## 已知 TAG

与 `docs/CONVENTIONS.md` §5 的固定清单保持一致，是 `log` 命令的唯一数据源：
`APP / BOARD / AUDIO_IO / AFE / PLAYER / ENC / NET / WS / VC / VS / MC / MS / DEV / DIAG / CFG`。
新增组件时**两处同步**：CONVENTIONS §5（及 ARCHITECTURE §10.2）+ 本组件的 `k_tags[]`。

## 依赖

- 组件：`console`（esp_console/linenoise）、`esp_app_format`（`esp_app_get_description`）、`esp_timer`（运行时长）。
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

## 后续

- T5.1：补 `wifi` / `net ping` / `mic stat` / `audio` / `voice` / `music` 命令（依赖对应模块就绪）。
- T5.3：用 `CONFIG_DIAG_HEAP_MONITOR` 做 24h 压测的堆水位记录。
