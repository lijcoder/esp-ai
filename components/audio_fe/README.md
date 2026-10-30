# audio_fe —— 语音前端（L2 音频引擎层）

**作用**：封装 ESP-SR AFE，把"麦克风原始采样"变成"唤醒事件 / VAD 事件 /（后续）上行语音帧"。上层只订阅事件，不接触 AFE 细节。

## 当前能力（MVP）

- 从 `model` 分区加载模型（WakeNet9 `wn9_hilexin` + VADNet），模型/唤醒词缺失时**显式报错**；
- AFE 初始化：单麦格式 `"M"`（无 AEC，见架构 §2.1）、`AFE_TYPE_SR`、`AFE_MODE_LOW_COST`；
- 两个任务绑核 1：`feed`（优先级 8，I2S→AFE）、`fetch`（优先级 6，AFE→事件）；
- 事件回调：唤醒、VAD 开始/结束；
- feed 开关：半双工控制（架构 §3.3 规则 R1/R2）；
- 观测统计：麦克风电平窗口（RMS/峰值）、帧数、I2S 读失败、唤醒次数、VAD 状态，供 `diag` 的 `mic stat` 读取（T0.7a）。

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

## 事件

`AUDIO_FE_EVT_WAKE`、`AUDIO_FE_EVT_VAD_SPEECH_START`、`AUDIO_FE_EVT_VAD_SPEECH_END`。
回调运行在 fetch 任务上下文，**不要做耗时操作**。

## 配置（Kconfig）

| 配置 | 默认 | 说明 |
|---|---|---|
| `AUDIO_FE_MODEL_PARTITION` | `model` | 模型分区标签 |
| `AUDIO_FE_INPUT_FORMAT` | `M` | 单麦；后续 AEC 实验可改 `MR`/`MMR` |

## 关键约束

- 麦克风读取失败（`audio_io_read_mic` 返回负值）时按 0 帧处理并整帧补零，禁止让负值参与指针运算（否则 memset 会越界写堆）。
- 每次 `feed` 必须喂 **chunksize × nch** 个 int16（单麦 chunksize=512/32ms）；不足帧补零，不能改变喂数节奏。
- `fetch_with_delay` 的超时参数是 **tick**（与 `audio_io` 的毫秒不同），用于让 fetch 任务能响应退出。
- 统计口径是**送 AFE 之前的麦克风真实数据**（补零帧不计），暂停收音时仍继续累计 —— 便于区分"麦克风没声"和"AFE 吃静音"；RMS/log 等浮点计算一律在 console 侧，音频任务内只做整数累加。

## 依赖

`audio_io`（采集）、`espressif/esp-sr`（见 `idf_component.yml`）。

## 后续（P2 起）

- 增加"上行音频读取"接口（fetch 数据 → Opus 编码），供 `voice_cloud` 使用；
- 双麦/参考通道支持（`"MMR"`）随硬件升级（见 PRODUCT_IDEAS H1）。

对应架构：[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §2、§3.1、§3.3、§4。
