# audio_fe —— 语音前端（L2 音频引擎层）

**作用**：封装 ESP-SR AFE，把"麦克风原始采样"变成"唤醒事件 / VAD 事件 / 上行 16k 语音帧"。上层只订阅事件/读上行接口，不接触 AFE 细节。

## 当前能力（MVP）

- 从 `model` 分区加载模型（WakeNet9 `wn9_hilexin` + VADNet），模型/唤醒词缺失时**显式报错**；
- AFE 初始化：单麦格式 `"M"`（无 AEC，见架构 §2.1）、`AFE_TYPE_SR`、`AFE_MODE_LOW_COST`；
- 两个任务绑核 1：`feed`（优先级 8，I2S→AFE）、`fetch`（优先级 6，AFE→事件）；
- 事件回调：唤醒、VAD 开始/结束；
- feed 开关：半双工控制（架构 §3.3 规则 R1/R2）；
- 观测统计：麦克风电平窗口（RMS/峰值）、帧数、I2S 读失败、唤醒次数、VAD 状态，供 `diag` 的 `mic stat` 读取（T0.7a）；
- **上行音频输出（T2.1）**：fetch 任务把 AFE 输出（16k/单声道/int16）写入 PSRAM 环形缓冲，消费端按需精确读取；
  缓冲深度 `CONFIG_AUDIO_FE_UPLINK_RING_MS`（默认 2000ms，取 2 的幂 → 32768 样本 = 2.048s / 64KB PSRAM）。

## 对外接口

| 接口 | 说明 |
|---|---|
| `audio_fe_init(cb, arg)` | 加载模型、初始化 AFE，注册事件回调 |
| `audio_fe_start()` | 创建 feed/fetch 任务并运行 |
| `audio_fe_set_feed_enabled(bool)` | 暂停/恢复收音（暂停时喂静音，保持时间节奏） |
| `audio_fe_feed_enabled()` | 查询当前收音状态 |
| `audio_fe_wakenet_name()` | 返回已加载的唤醒词模型名 |
| `audio_fe_stats_read(out)` | 读取统计快照（不清零，任意任务可调用） |
| `audio_fe_stats_reset()` | 清零统计窗口（帧数/失败数/唤醒数/电平一起清零） |
| `audio_fe_uplink_start()` | 清空缓冲并开始写入；进入 LISTENING 前调用（同时复位本次会话统计） |
| `audio_fe_uplink_stop()` | 停止写入并丢弃残留；进入 SPEAKING（R1）或打断时调用 |
| `audio_fe_uplink_active()` | 当前是否在写入 |
| `audio_fe_uplink_read(dst, n, timeout_ms)` | **按 n 个样本精确成帧读取**：够 n 才返回；超时才返回不足的尾片段；返回实际样本数，<0 为 `-esp_err_t` |
| `audio_fe_uplink_stats_read/reset` | 写入/读取/丢弃/读超时/环容量/最高占用/首帧延迟等统计 |

### 上行数据约定（T2.2 消费方必读）

- 数据源是 **AFE fetch 输出**（已降噪的 16k 单声道 int16），不是原始麦克风；
- **AFE fetch 每帧 512 样本（32ms，T2.1 真机实测）**，与 Opus 20ms（320 样本）**不整除**（512 % 320 = 192），
  消费端必须自己按 320 样本重组，禁止直接把 fetch 帧当编码帧；
- `uplink_read()` 精确成帧：内部会阻塞到够 n 个样本才返回（或超时交尾帧），调用方不需要处理跨帧拼接；
- 缓冲满时**丢最旧**样本并累计 `samples_dropped`（保证上行时延有界，不越积越旧）；丢弃不在音频任务里打日志（UART 可能阻塞），由读统计方告警；
- 音频任务内只做一次 memcpy + 任务通知：没有 malloc、没有日志、没有网络。

## 事件

`AUDIO_FE_EVT_WAKE`、`AUDIO_FE_EVT_VAD_SPEECH_START`、`AUDIO_FE_EVT_VAD_SPEECH_END`。
回调运行在 fetch 任务上下文，**不要做耗时操作**。

## 配置（Kconfig）

| 配置 | 默认 | 说明 |
|---|---|---|
| `AUDIO_FE_MODEL_PARTITION` | `model` | 模型分区标签 |
| `AUDIO_FE_INPUT_FORMAT` | `M` | 单麦；后续 AEC 实验可改 `MR`/`MMR` |
| `AUDIO_FE_UPLINK_RING_MS` | `2000` | 上行 PSRAM 环形缓冲深度（ms，0=关闭）；取 2 的幂，默认 32768 样本 / 64KB |

## 关键约束

- 麦克风读取失败（`audio_io_read_mic` 返回负值）时按 0 帧处理并整帧补零，禁止让负值参与指针运算（否则 memset 会越界写堆）。
- 每次 `feed` 必须喂 **chunksize × nch** 个 int16（单麦 chunksize=512/32ms）；不足帧补零，不能改变喂数节奏。
- `fetch_with_delay` 的超时参数是 **tick**（与 `audio_io` 的毫秒不同），用于让 fetch 任务能响应退出。
- 统计口径是**送 AFE 之前的麦克风真实数据**（补零帧不计），暂停收音时仍继续累计 —— 便于区分"麦克风没声"和"AFE 吃静音"；RMS/log 等浮点计算一律在 console 侧，音频任务内只做整数累加。
- 上行接口是**拉取式**（ring + read），不是回调：编码/发送若放进 `afe_fetch` 会违反 CONVENTIONS §3（音频任务禁止 malloc/网络/阻塞）。
- `feed` 暂停（R1）时 AFE 吃静音、上行输出也是静音；**不能**用 `audio_fe_set_feed_enabled(false)` 代替 `audio_fe_uplink_stop()`。
- 上行缓冲分配失败时只降级（WARN + `uplink_start` 返回 `ESP_ERR_NOT_SUPPORTED`），唤醒/提示音不受影响。

## 依赖

`audio_io`（采集）、`espressif/esp-sr`（见 `idf_component.yml`）。

## 后续

- T2.2 `codec_enc` 用 `audio_fe_uplink_read()` 取 320 样本编码（上行接口已就绪，2026-10-31 T2.1）；
- 唤醒/VAD 回调改走 `event_bus`（T1.2b，随 T2.5/T3.1 落地）；
- 双麦/参考通道支持（`"MMR"`）随硬件升级（见 PRODUCT_IDEAS H1）。

对应架构：[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §2、§3.1、§3.3、§4。
