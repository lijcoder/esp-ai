# codec_enc —— Opus 上行编码（L2 音频引擎）

**作用**：把 16kHz/单声道/int16 的 PCM 编码成 Opus 帧，供 T2.5 `voice_cloud` 加 8 字节帧头后 `ws_send_bin(0x01, …)` 上行。
参数按 ARCHITECTURE §3.6 定稿：**16kHz / 单声道 / 16kbps 目标码率 / 20ms 一帧（320 样本）**。

**定位**：纯编解码器 —— **不建任务、不碰网络、不读音频设备**。调用方在自己的任务里
`audio_fe_uplink_read()` → `enc_encode()` → 发送（T2.5 的 `voice_cloud` 任务）。

## 对外接口（`include/codec_enc.h`）

| 接口 | 说明 |
|---|---|
| `enc_open()` / `enc_close()` / `enc_is_open()` | 按 Kconfig 创建/销毁编码器；`enc_open()` 幂等。首次打开会在堆上分配状态（PSRAM） |
| `enc_encode(pcm, samples, out, cap, &out_len)` | 编码任意长度 PCM，内部 carry 缓冲把不足 20ms 的尾部攒到下次；每次输出 0~N 个完整帧 |
| `enc_flush(out, cap, &out_len)` | 会话收尾：carry 残余补零成尾帧，不丢最后半帧（T2.5 的 `audio_end` 用这个） |
| `enc_reset()` | 新会话开始：复位 Opus 状态 + 清 carry（不清统计） |
| `enc_carry_samples()` | 当前 carry 里的样本数，调用方用它预估 `out` 容量 |
| `enc_stats_read()` / `enc_stats_reset()` | 帧数/字节/单帧最大包长/编码耗时均值与最大值/错误数/carry |

常量：`CODEC_ENC_SAMPLE_RATE`(16000)、`CODEC_ENC_FRAME_SAMPLES`(320)、`CODEC_ENC_MAX_PACKET`(512，单帧输出上限)。

## 依赖

- `esphome/micro-opus` 0.4.1（Apache-2.0，见「依赖评估」）
- `esp_timer`（单帧编码耗时统计）

## Kconfig

| 配置 | 默认 | 说明 |
|---|---|---|
| `CODEC_ENC_BITRATE` | 16000 | 目标码率；VBR 下实测平均 13~16kbps（约 1.7~2KB/s） |
| `CODEC_ENC_VBR` | y | **默认 VBR**：实测单帧 6.5~7.2ms；CBR 每帧严格 40B 但要 16.2ms（2.4×），20ms 预算下不采用 |
| `CODEC_ENC_COMPLEXITY` | 3 | CPU/音质权衡；实测单帧 avg 5.6~7.2ms、max 8.8ms（含被高优先级任务抢占的墙钟时间） |
| `CODEC_ENC_DTX` | n | 关：每帧稳定；开：静音帧 1~3B，接收端要按时间轴补齐 |
| `CODEC_ENC_INBAND_FEC` | n | 上行是 WiFi/TCP，无丢包，默认关 |

编码器状态与伪栈的放置由 microOpus 的 Kconfig 控制，本工程在 `sdkconfig.defaults.esp32s3` 里固定为
**PSRAM 优先 + thread-safe 伪栈 + 定点实现**。

## 关键约束

1. **不得在音频任务里调用**：编码是毫秒级 CPU 工作，且首次编码会触发伪栈分配（PSRAM malloc），
   违反 CONVENTIONS §3「音频任务内禁止 malloc/阻塞」。
2. **任务栈 8KB 足够、但 4KB 不够**：libopus 的临时数组已在 PSRAM 伪栈，剩下的局部栈仍有几 KB。
   `diag` 的 REPL 任务因此从 4KB 提到 8KB（T2.2 实测 4KB 时 `enc test` 直接栈溢出/崩溃）。
3. 编码器**不新建任务**：T2.5 在 `voice_cloud` 任务里串行「读 320 → 编码 → 发送」，一个任务担两职，
   不再额外占一份 6~8KB 内部栈。
4. 伪栈为 thread-safe 模式（每个调用线程各 120KB PSRAM，当前最多 2 个：console 探针 / `voice_cloud`），
   内部 RAM 增量为 0；PSRAM 余量 7.5MB，可接受。
5. 采样率固定 16kHz，运行中不切换（CONVENTIONS §3）。AFE fetch 每帧 512 样本与 320 不整除，
   重组由 `audio_fe_uplink_read()` 的精确成帧保证，codec 只兜超时尾帧（carry）。

## 依赖评估（为什么是 microOpus）

| 候选 | 结论 |
|---|---|
| `espressif/opus` | 组件注册表不存在该组件 |
| `78/esp-opus` 1.0.5（MIT，纯 C） | 不采用：libopus 实现在 `silk/fixed/encode_frame_FIX.c` 用 `VARDECL` 在**任务栈**上开 `sLPC[MAX_LPC_ORDER][MAX_FRAME_LENGTH]`（20ms@16k ≈ 10KB 以上），编码任务栈要 12~16KB。本工程内部 RAM 余量只有约 16KB，会超预算 |
| `esphome/micro-opus` 0.4.1（Apache-2.0） | **采用**：把 libopus 的临时内存改成**伪栈**（可配 PSRAM），编码任务栈 8KB；状态/伪栈均放 PSRAM，内部 RAM 只有静态表增量 |

实测（T2.2 真机，见 STATUS 基线）：编码器状态 **15,364 B PSRAM**（内部堆增量 0B）、伪栈 120,000 B PSRAM；
镜像 **+159,792 B**（1,728,272 → 1,888,064 B，app 分区 3MB、余 39%）。

## 踩坑记录（升级 microOpus 后必须复测）

1. **浮点实现会崩**：microOpus 在 S3 上默认 `CONFIG_OPUS_FLOATING_POINT=y`。实测首次 `enc_encode()` 即崩溃：
   - nonthreadsafe 伪栈 + 浮点：`silk_noise_shape_quantizer_del_dec` 里访问 PSRAM 伪栈报
     `MMU entry fault ... (invalid mmu entry)`；
   - threadsafe 伪栈 + 浮点：`LoadProhibited`（EXCVADDR=0x44）。
   **改用定点（`# CONFIG_OPUS_FLOATING_POINT is not set`）后两种伪栈模式都正常**，且 S3 上定点更省 CPU。
2. **CBR 很贵**：`OPUS_SET_VBR(0)` 会让 libopus 做码率循环重编码，16kbps/20ms 单帧 16.2ms（VBR 6.7ms），
   因此默认 VBR，验收看"平均码率/平均帧长"而不是每帧固定 40B。
3. **console 任务栈**：`esp_console` 默认 4096B，`enc test` 直接在 REPL 任务里调 libopus 会栈溢出；
   `diag` 已把 `task_stack_size` 提到 8192B（其余命令无影响）。
4. **构建不是「绝对零警告」**：libopus 第三方源 `silk/NLSF2A.c` 有 1 条 `-Wmaybe-uninitialized`
   （vendor 代码，不改 `managed_components` 无法消除）；**自有代码零警告**。升级 microOpus 后若出现新告警，先区分归属。
5. **PSRAM 常驻**：thread-safe 伪栈按线程分配、随任务退出释放；`console_repl` 与 T2.5 的 `voice_cloud`
   都是常驻任务，因此**每个用过的线程各常驻 120KB PSRAM**（当前最多 2 个 = 240KB，PSRAM 余 7.5MB 可接受）。
   若未来 PSRAM 紧张，可下调 `CONFIG_OPUS_PSEUDOSTACK_SIZE`（下限 60000）或改回非线程安全模式（须保证串行调用）。

## 后续（T2.5）

- `voice_cloud` 任务：`audio_fe_uplink_read(320)` → `enc_encode()` → 加 8 字节帧头 → `ws_send_bin(0x01)`；
  VAD 静音 800ms 或累计 8s 时 `enc_flush()` + `audio_end`（架构 §3.6）。
- 若上行码率/音质要调，只改 Kconfig，不要改代码里的帧长/采样率。
