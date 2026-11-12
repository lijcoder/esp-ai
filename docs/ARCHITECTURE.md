# 智能语音硬件 —— 系统架构（v1.0，已确定方案）

> 适用范围：ESP32-S3-N16R8 + INMP441 麦克风 + MAX98357A 功放 + 3020 喇叭
> 文档定位：**本文件只描述已经确定、需要实现的方案**。尚未纳入的功能统一放在文末「附录 A」。
> 文档索引：本文 = 架构决策；[ROADMAP.md](ROADMAP.md) = 任务拆分与验收；[PRODUCT_IDEAS.md](PRODUCT_IDEAS.md) = 待决策方向。
> 硬件接线、电气参数、自检清单见 [HARDWARE.md](HARDWARE.md)。云端服务端由后端实现，端侧按第 6 节契约对接。

---

## 1. 功能范围（确定）

| 功能 | 实现方式 |
|---|---|
| 本地唤醒 | ESP-SR WakeNet9，唤醒词"嗨，乐鑫"（`wn9_hilexin`） |
| 唤醒应答 | 播放本地提示音（assets 分区，如"我在呢"） |
| 语音对话 | 唤醒 → 上行 Opus 音频 → 云端 ASR/LLM → TTS(MP3) 流式下行 → 边下边播 |
| 打断 | 按键打断（半双工，播放中不听麦克风） |
| 音乐播放 | MP3 播放；**只有 HTTP(S) 远端 URL 一种入口**（2026-11-10 用户决定：不做本地文件/SD 卡曲库）；曲目 URL 由服务端下发 |
| 异常反馈 | **本地语音提示音（T3.6：`feedback` 组件，四类异常）+ `APP_EVT_FEEDBACK` 语义事件（屏幕/灯光等未来通道订阅）**；板载 LED 状态灯已于 2026-11-02 移除（PENDING PD-003） |
| 交互按键 | 短按：打断/开始对话；长按：静音/恢复 |
| 调试 | esp_console 命令行 + 分级日志 + 堆/任务监控 |
| 网络 | WiFi STA，账号密码编译期写死；断线自动重连 |

当前版本明确不做：配网、OTA、屏幕与 UI、AEC/语音打断、定制唤醒词、SD 卡、电池与低功耗、离线命令词。这些在附录 A 列出预留点。

---

## 2. 已确定的硬件约束

| 项目 | 确定值 | 对软件的影响 |
|---|---|---|
| 主控 | ESP32-S3，240MHz，16MB Flash（QIO 80MHz），8MB Octal PSRAM 80MHz | 音频大缓冲全部放 PSRAM |
| 麦克风 | INMP441 接 `I2S_NUM_0` RX：**16kHz**、32bit 槽位、单声道、左声道（L/R 接 GND） | AFE 输入固定 16k；代码中 `>>16` 取高 16bit |
| 功放 | MAX98357A 接 `I2S_NUM_1` TX：**48kHz**、16bit、立体声（单声道数据复制到 L/R） | 所有播放音频统一重采样到 48k |
| 回采参考 | **无**（麦克风只有数字输出，功放只有数字输入） | 无 AEC，当前采用半双工；AFE 输入格式固定 `"M"` |
| 音量控制 | 无数字音量（MAX98357A 增益由 SD_MODE 电阻固定） | 软件 16bit 数字音量 + 软限幅 |
| 拾音 | 单麦全向，远场约 2~3m | 唤醒阈值按实测标定 |
| 引脚 | MIC: 5/6/4；AMP: 41/42/40；LED: 48；按键: 0 | 避开 26-37/19-20/43-44 |
| 内部 RAM | DIRAM 已用 196,462 B / 341,760 B（2026-10-31，含 coredump；T2.2 起含 libopus 静态表） | 设计目标（X3-E 定稿）：**静态 DIRAM + 堆峰值 ≤ 440KB**，且 wss 握手期最低空闲 ≥ 30KB |
| 分区 | 见第 9.1 节（定稿） | app 双槽位预留给后续 OTA，代码当前版本不实现 |

**结论**：音频链路为 16k 采集 + 48k 播放的双时钟结构；当前不实现 AEC，采用半双工。

---

## 3. 音频链路设计（确定）

### 3.1 采集

```
INMP441 ──I2S0 RX 16k──> audio_io(32bit→int16) ──> audio_fe 环形输入
                                                        │
                                    AFE: NS + VAD + WakeNet（无 AEC）
                                                        │
                        ┌───────────────────────────────┴──────────────┐
                        ▼                                              ▼
                唤醒事件 / VAD 事件                          fetch 16k 单声道音频
                        │                                              │
                        ▼                                              ▼
                  event_bus                                   codec_enc(Opus 16kbps)
```

- AFE 配置：`afe_config_init("M", models, AFE_TYPE_SR, AFE_MODE_LOW_COST)`；
- 模型：`wn9_hilexin` + `vadnet1_medium`（已在 model 分区）；
- 采集分帧由 AFE `get_feed_chunksize()` 决定，采集任务只做 I2S 读 + 格式转换 + `feed()`。
- 上行取数：`fetch` 输出（16k/单声道/int16）写入 PSRAM 环形缓冲（`AUDIO_FE_UPLINK_RING_MS`，默认 2s），
  消费端按 320 样本精确读取。**AFE fetch 每帧 512 样本（32ms，T2.1 实测）与 Opus 20ms 帧不整除，编码端必须自行重组。**

### 3.2 播放（T3.3 三通道混音）

```
TTS(MP3 流/整段) ─┐
提示音(PCM)      ├─> 通道 ring(PSRAM，48k 单声道) ─> 饱和相加 ─> 音量(0~100，20ms 插值)
音乐(MP3/PCM)    ─┘                                                   │
                                                                      v
                                          I2S1 TX 48k 立体声 <─ 软限幅(-1dBFS 渐近) <─ R4 淡出
```

- 三个独立通道：`CH_TTS`、`CH_PROMPT`、`CH_MUSIC`，每通道一个 **8192 帧（170ms）PSRAM ring**；
  生产者（整段 PCM 重采样、探针音调、TTS 解码器）把 48k 单声道样本填进 ring，混音级每 10ms
  （480 帧）从三通道各取一帧相加。**单播放任务串行完成泵数据、混音、写 I2S，不新增任务**。
- **优先级语义（T3.3 定稿）**：三路**相加**，不做硬抢占 —— `CH_MUSIC` 在语音（TTS 或提示音）
  活跃时 duck 到 **-12dB**（q16 增益 16384，100ms 斜坡），`CH_PROMPT` 在 `CH_TTS` 活跃时让位到
  **-6dB**（32768），`CH_TTS` 恒 0dB。**T3.6 实现**：异常反馈路径会先中止 TTS 流（error/断网/
  设备侧 TTS 失败），再播 `CH_PROMPT` 提示音，所以实际并发很少；`CH_PROMPT` 的 -6dB 让位保留给
  "提示音恰好在 TTS 活跃期开始"的边界（由 `feedback` 的 pending 机制保证不叠音）。
  真实音乐源在 P4 接入后，会话结束恢复音乐由 T4.4 负责。
- **饱和加法**：三通道相加用 int32 累加（最大 ±98301），随后统一做音量/限幅，**不在中间做 16bit 截断**。
- **淡入淡出**：通道起播 10ms 线性淡入（包络 q16 步进）；源耗尽后（`closed`）剩余 ≤100ms 时对
  该通道做 100ms 线性淡出，播空即退出。全局 R4 打断淡出仍在输出级（见 §3.3）。
- **软限幅（-1dBFS）**：上限 `LIMIT_CEIL=29196`（32767×10^(-1/20)），膝点 `LIMIT_KNEE=26276`
  （0.9×上限）。|x| ≤ 膝点完全透明；超过后按 `y = KNEE + SPAN·over/(over+SPAN)` 有理函数**渐近**
  压向上限（单调、无硬削波；三路满幅相加的极限输出 ≈ -1.0dBFS）。逐样本除法只在超过膝点时发生；
  另统计 `clip`（|y| 触到 int16 边界）应恒 0，用于自证"无削波"。
- **音量**：0~100 线性映射到 q16（100% = 65536），**20ms 线性插值**（每块重算步进，块内逐样本推进）；
  施加位置在混音之后、限幅之前（先混音再压限，符合"混音 → 音量 → 限幅"顺序）。静音 = 0；
  `audio_player_set_volume()` 只改目标值，当前值由混音循环斜坡跟进。
- **混音 CPU（T3.3 实测）**：稳态单块（480 帧、3 通道）泵+混音+限幅 **0.6~1.7ms**（10ms 预算）；限幅饱和
  （三路 amp20000 叠加）单块最大实测 **~8.6ms**（仍 <10ms，但余量只有 ~1.4ms）——P4 音乐叠加/更多通道
  同时活跃时必须实测单块耗时，不要默认还有余量。
- **T3.3 边界**：作业仍是一次一个（提示音 / 整段 MP3 / TTS 流 / 探针混音泵），但通道本身独立，
  P4 只需让音乐源任务持续填 `CH_MUSIC` ring 即可得到"音乐继续播 + 语音叠加 + 自动 duck"；
  T3.6 的错误提示音已验证"先停 TTS、再播提示音"的会话路径，probe 混音仍用于三路并发正确性。

### 3.3 半双工规则（明确约定）

| 规则 | 内容 |
|---|---|
| R1 | 进入 `SPEAKING`/提示音播放时暂停收音（`audio_fe_feed_hold()` 置原因位），麦克风数据喂静音（不送 AFE） |
| R2 | `TTS_END` 后延迟 **300ms** 恢复 feed，吸收喇叭尾音与功放余响（延时在 `audio_player` 的完成回调前统一做） |
| R3 | 音乐播放不关闭麦克风（保持唤醒检测）；打断命中后 100ms 内**暂停音乐并保留**（T4.4 pause 模式；语音打断依赖 AEC，duck 留作后续体验模式） |
| R4 | 按键打断：语音类内容（提示音/TTS）→ **80ms** 淡出并丢弃 → 发 `interrupt` 报文 + 作废 sid → 恢复 feed（丢弃 50ms）→ `IDLE`；**只有音乐在播 → 暂停并保留**（T4.4） |
| R5 | 恢复 feed 后丢弃最早 **50ms** 的音频（`CONFIG_AUDIO_FE_RESUME_DISCARD_MS`），避免打断瞬间的噪声触发 VAD |

**门控仲裁（T3.2 定稿，唯一入口）**：`audio_fe` 维护一个原因位图，业务方各自 `audio_fe_feed_hold(reason)` / `audio_fe_feed_release(reason)`，**位图归零才真正恢复收音**。原因位：`PROMPT`（唤醒应答提示音，`voice_session`）、`TTS`（云端 TTS 流式播放，`voice_cloud`）、`PROBE`（console/旧 `audio_fe_set_feed_enabled()` 兼容）。这解决了 T3.1 之前"提示音 R1/R2"与"TTS R1/R2"两套独立开关互相覆盖的问题（谁后执行谁生效 → 可能提前开麦自激，或永久关麦卡死会话）。

> **T3.2 实现状态（2026-11-01）**：
> - **R1/R2**：提示音与 TTS 统一经原因位仲裁（`hold_mask` 在 `mic stat` 可观测：提示音期 `0x1`、TTS 期 `0x2`、恢复后 `0x0`）；300ms 尾部延时仍在 `audio_player`（PCM 与流式一致），完成回调 → 各自 `release` 自己那一位。
> - **R4**：`voice_session() → vs_interrupt()`：语音类内容在播时 `audio_player_fade_out_and_stop(80ms)`（输出级线性淡出，`player stat` 的 `last_stop_ms` 真机实测 64~80ms；80ms 淡出被 10ms 输出块量化，故只设上界 <100ms）→ `vc_interrupt()` 发 `{"t":"interrupt","sid":N}` 并 `vc_cancel()` → `audio_fe_feed_discard_ms(50)` → 回 `IDLE`。播放完成回调以 `ok=false` 上报。淡出期间 `vc_cancel` 的立即 abort 不会截断淡出（`run_stream` 里"有淡出请求时忽略 abort_req"）。**T4.4 补充**：若 `audio_player_voice_active()==false` 且音乐在播（例如会话中音乐被显式恢复），则走 `audio_player_music_pause(80ms)` **暂停保留**，不发音乐相关报文；正常路径下音乐在语音进入活跃态时已 auto-pause。按键来源：`APP_EVT_BUTTON`（payload 见 §4.2），**自 T3.4 起由 `dev_ctrl` 扫描真实 GPIO0 投递**；console 仍可用 `vs interrupt` / `evt post BUTTON` 注入验证。**停声口径**：从按键/命令到输出静音 ≤100ms（80ms 淡出 + 一个 10ms 输出块）。
> - **R5**：`audio_fe_feed_discard_ms()` 在恢复后把最早 50ms 的麦克风数据喂零并计数（`mic stat` 的 `discard_samples=800`）；只在打断/断链恢复路径使用，正常 TTS 收尾由 R2 的 300ms 覆盖、**不叠加** R5。
> - **R3（T4.4 定稿：pause 模式）**：音乐播放中不关麦；打断命中后 100ms 内**暂停并保留**（保 ring/进度），会话结束**不自动恢复**，等用户明确说"继续"；`duck`（CH_MUSIC 通道增益，-12dB/100ms 斜坡）仍保留为探针/后续体验模式，本轮不做叠加。T3.3 的混音链路（音乐 ring 持续供数 + 提示音/TTS 叠加 + 自动 duck）为后续 duck 模式预留，当前不使用。真机探针：T3.2 的 `out_peak` 15563→3890（0.250）；T3.3 用 `player mix` 三通道复验 1760Hz 峰 8000→2000、440Hz 峰 8000→4000。

> **T3.7 连续对话**：`FOLLOW_UP` 窗口建立在 TTS 的 R2（feed 已恢复）之后；窗口内起轮不再 hold PROMPT（不播提示音），直接进入上行；窗口内按键/断网按 R4/R5 退出。窗口的"人声确认"只依赖 VAD 事件，不改变 R1–R5 语义。
>
> **T3.7 续听预录（2026-11-03 修，PR-1）**：续听轮要等「VAD 判定 + 250ms 确认」才 `vc_start()`，若到起轮才 `audio_fe_uplink_start()`（该调用会清空环形缓冲），用户这句话的**开头会被整段丢掉**（实测 ASR 只识别到后半句）。现改为：`FOLLOW_UP` 一开窗就 `vc_preroll_start()` 启动上行环形缓冲（只写不读、不发包），起轮时 `vc_begin_round()` **不清空**缓冲、按 `CONFIG_VC_UPLINK_PREROLL_MS`（默认 1000ms）裁掉更靠前的静音后直接上传；窗口结束/重新唤醒/起轮失败 → `vc_preroll_stop()` 丢弃并释放上行接口。窗口内无播放，R1–R5 语义不变；满环（2048ms）时按环形缓冲语义丢最旧。

### 3.4 提示音（T3.5 迁 assets 分区）

- 资源：`assets` 分区（2M @0x7a0000）内的**自定义只读 pack**（T3.5 方案 D，用户 2026-11-02 拍板）：
  - 格式：24B 头（magic `ESPAPK01` / version 1 / count / total_len / 头部 CRC32）+ 20B×count 条目
    （id / offset / length / sample_rate=16000 / PCM CRC32）+ PCM 区（16k/16bit/单声道，4 字节对齐）；
  - 生成：`tools/audio_pack.py`（源 WAV 在 `tools/prompts_src/`，**单独烧 assets 分区即可替换提示音**，
    app 固件不用重编、不用重烧）；
  - 加载：`components/prompt_store`（L1）启动校验 pack → **`esp_partition_mmap` 只读映射**（零拷贝，
    PCM 仍在 flash，与 T3.6 的 rodata 行为一致）；映射失败回退「分块读入 PSRAM + 逐条 CRC 校验」；
    pack 缺失/损坏不致命（`prompt_assets_get()` 返回 NULL，唤醒跳过应答提示音直接起轮、错误反馈只日志）；
- 上层接口不变：`prompt_assets_get(prompt_id_t)` / `prompt_assets_name()`（T3.6 冻结，迁移零改动）；
- 资源组（5 条）：`PROMPT_WAKE_OK`（我在呢）、`PROMPT_NET_OFFLINE`、`PROMPT_CLOUD_ERROR`、
  `PROMPT_TIMEOUT`、`PROMPT_TTS_FAIL`；均 16k/16bit/mono、峰值对齐 -7.82 dBFS、首尾淡入淡出防 pop；
- 播放：重采样到 48k，走 `CH_PROMPT`（`audio_player_play_pcm()`）；
- **错误提示音的播放归 `feedback` 组件**（T3.6，见 §8.6）：语音只是**当前唯一反馈通道**，
  语义事件 `APP_EVT_FEEDBACK` 对外发布，未来屏幕/灯光等通道订阅事件即可；
- 提示音播放中若 TTS 到达：T3.3 定稿的 `CH_PROMPT` -6dB 让位仍保留（异常路径通常先停 TTS 再播）。

> **为什么不用 SPIFFS**（ROADMAP 原措辞）：SPIFFS mount 占内部堆，而本项目 wss 水位余量只有 ~2.6KB
> （CONVENTIONS §4）；自定义 pack 的校验/映射几乎零 RAM，且"单独烧分区替换"目标同样满足。
> 已按用户 2026-11-02 拍板改为此方案（决策见 PRODUCT_IDEAS §6）。

### 3.5 音量与限幅

- 音量 0~100，**默认 100**，存 NVS（`config_store`）；
  > 2026-10-31 用户听感定稿：T2.4 实播云端 TTS（测试素材峰值仅 -13.7 dBFS，比提示音低 ~6dB）时，软件音量 60 偏小、100 合适；提示音峰值 -7.8 dBFS，100 不削波。真实云端 TTS 电平通常更高，若后续觉得过响可再调 `CFG_VOLUME_DEFAULT`。
- 软件音量：`mixed * vol_q16 / 65536`（q16，100% = 65536），静音即 0；
- **T3.3 实现**：音量在**混音之后、限幅之前**施加；变化时 **20ms 线性插值**（`VOL_RAMP_FRAMES=960`@48k），
  避免阶跃；`player mix vol` 探针可运行时改（不落盘），真机抓取验证 100/50/0 的幅度比 1:0.5:0。
- 限幅：混音后超过 -1dBFS（膝点 -1.92dBFS）时按有理函数渐近压回，**无硬削波**；参数见 §3.2。
  `player stat`/`player mix stat` 的 `limiter`（触发样本数）、`limiter_peak`（限幅前峰值）与
  `clip`（硬削波数，恒 0）是真机自证字段。

### 3.6 上行编码（确定）

- 编码：Opus，16kHz、单声道、16kbps 目标码率、20ms 一帧（320 采样）；
- 实现（T2.2 定稿）：libopus **定点**实现（`esphome/micro-opus` 0.4.1，Apache-2.0）；
  编码器状态 15.4KB + 临时内存（伪栈）120KB 全部放 **PSRAM**，内部 RAM 增量只有静态表；
- 码率模式：**VBR**（目标 16kbps）。实测平均 13~16kbps（~1.7~2KB/s，随内容浮动）；
  CBR 每帧严格 40B 但 libopus 做码率循环重编码，单帧耗时 2.4×（16.2ms vs 6.7ms），
  20ms 帧预算下不采用；
- 帧头 8 字节（见 6.2），seq 单调递增；
- VAD 判定静音持续 800ms 或累计收音 8s 时发送 `audio_end`；
- 编码不新建任务：在调用方任务（T2.5 的 `voice_cloud`）内串行执行，任务栈 8KB 足够
  （伪栈已把 libopus 的临时数组移到 PSRAM）。

### 3.7 下行编码（确定）

- TTS 与音乐统一使用 **MP3** 解码（端侧只实现 MP3 一种解码器）；
- TTS：24kHz 单声道（服务端按契约输出）；
- 音乐：MP3，44.1k/48k 单声道或立体声，解码后重采样到 48k 输出；
- **实现（T2.3 定稿）**：vendored **minimp3**（CC0 单文件，`components/audio_player/third_party/minimp3`，3 处补丁）。解码状态 `mp3dec_t` 6,668 B + 临时区 `mp3dec_scratch_t` 16,256 B + 单帧输出 4,608 B **全部 PSRAM**（内部 RAM 增量 0）；解码在 `player` 任务内串行，不新建任务。输出单声道 PCM（双声道取 (L+R)/2）写入 PSRAM 预分配缓冲（`AUDIO_PLAYER_MP3_PCM_MAX_BYTES`，默认 1.5MB），再走既有「线性重采样 48k → 软件音量 → 淡入淡出 → 限幅 → I2S」。
- **为什么不是 Helix**：Helix 的 8 个解码结构体实测 23,856 B，且托管组件 `chmorgan/esp-libhelix-mp3` 的 `AllocateBuffers()` 用裸 `malloc`、改不了分配器 → 必然占内部 RAM 并击穿 30KB 水位；minimp3 零 `malloc`，状态只有 6,668 B。与 FFmpeg 逐样本对拍 max|Δ| ≤ 1 LSB（STATUS §5）。
- **T2.3 边界**：整段解码后播放，同一时刻只允许一段 MP3（解码输出复用同一块 PSRAM 缓冲）；帧参数中途变化（分段流）按截断处理；超长素材（超出 PCM 上限）拒绝并 WARN。
- **流式播放（T2.4 定稿；替代旧表述「预缓冲 200ms 再出声」）**：`tts_start` 时 `audio_player_stream_open(1, sr)`，下行 0x02 帧在 `audio_end → tts_end` 窗口内经 PSRAM ring（`AUDIO_PLAYER_STREAM_RING_BYTES`，默认 64KB）交给 `player` 任务边收边解；不新建任务、内部 RAM 增量 0。
  - **起播门槛**：已解出 `AUDIO_PLAYER_STREAM_PREROLL_FRAMES` 帧（**默认 4 = 96ms**）**或**距首字节 `PREROLL_MS`（120ms），取先到者；此前 I2S TX 不出声。4 帧用于覆盖 I2S DMA 预填（~40ms）后仍留 ~56ms 抗 WiFi/调度抖动（实测 2/3 帧会偶发起播欠载，4 帧后全量 193/193 + heap-only 20 轮 0 欠载）；首声实测 14~29ms，远低于 150ms 目标。
  - **边收边解**：只把「凑满整帧」的数据交给 minimp3（其在数据不足分支会重置解码器、丢比特池）；跨帧连续线性重采样（游标 + 上一块末样本 carry），帧边界不重复淡入淡出、无缝隙；欠载时补静音继续走 I2S 时钟，每次连续欠载计 1 次 `underruns` 并限频 WARN。
  - **收尾**：`tts_end` 后先解完 ring 内数据，切 100ms 收尾环线性淡出 → 既有静音冲刷/关 TX → 300ms 尾部延时 → 完成回调（`src=TTS_STREAM`）；abort（cancel/error/断链/新轮次/响应超时）立即关 TX、丢弃 ring、回调 `ok=false`。
  - **云端要求（T2.4 实测新增）**：服务端必须关闭 Nagle（`TCP_NODELAY`）。Nagle + 端侧 delayed ACK 会把均匀 TTS 帧攒批 ~200ms 再交付，任何预缓冲都会被打穿（mock 已默认开启，真实云端同样必须）。
  - **T4.1 远端音源（`source_http.c`）**：`music play <url>` → 独立任务 `music_http`（核 0/优先级 4/栈 `CONFIG_AUDIO_PLAYER_MUSIC_TASK_STACK`=6KB，**按需创建、停止后销毁**）→ `esp_http_client` HTTP(S) 拉流 → **独立 minimp3 实例**（PSRAM ~38KB，与 TTS 默认实例互不共享）→ 48k 单声道重采样 → PSRAM PCM ring（`AUDIO_PLAYER_MUSIC_RING_MS`，默认 1000ms）→ `CH_MUSIC`。起播前预填 150ms（避免空 ring 欠载）；停止 = 丢弃 ring + 100ms 淡出，拉流任务在 HTTP 读超时（2s）内自行退出。URL 只接受 `http(s)`、长度 ≤ `AUDIO_PLAYER_MUSIC_URL_MAX`（320），流中采样率变化即中止；**不做 seek/Range**（T4.5）。
  - **T4.1 内存口径**：音乐 PCM ring 96KB 在 PSRAM（启动时分配）；解码实例/预填/单帧输出 ~56KB 只在播放期占 PSRAM；`music_http` 任务栈 6KB + HTTP client 缓冲（4KB）+ 读缓冲（4KB）只在播放期占内部 RAM。**真机实测待补（PENDING PV-012）**：wss 握手期若与播放重叠，水位需 ≥30KB；T4.1 先按"播放中不重连"验证，T5.3 复测。
  - **T4.2 音乐协议（`music_cloud`，2026-11-10 实现）**：WS 附加订阅槽解析 `music_track`/`music_control`，按连接内 `mid` 过滤后驱动 `audio_player_music_play_url/stop_url`；播放中每 `CONFIG_MC_STATUS_PERIOD_MS`（5s）上行 `music_status`，状态变化立即一次；起播/切歌在 `esp_timer` 里串行执行（切歌先 stop 再 play，按 `MC_PLAY_RETRY_MS` 重试旧任务退出窗口）。`pause/resume` 的执行、`music_request` 与切歌状态机自 T4.3 起由 `music_session` 接管（见下条）。T4.1 的 console 探针 `music play/stop/stat` 保留。
  - **T4.3 音乐控制（`music_session`，2026-11-10 实现）**：新增 L4 组件 `music_session`（**不建任务**），`music_cloud` 瘦身为纯协议（解析/`mid` 过滤/JSON 收发，不再调用播放器）。状态 IDLE/PLAYING/PAUSED/STOPPED/ERROR；**暂停 = `CH_MUSIC` 100ms 淡出后结束 PUMP 作业但保留 ring 与读写位置**，`source_http` 停止读 socket（靠 TCP 背压，不丢数据），**继续 = 重新入队 PUMP 从暂停点续播**（不重置 ring、不从文件头重放）；播放中每 `MS_STATUS_PERIOD_MS`（5s）上报 `music_status`（新增 `state:"paused"`，暂停期 `progress_ms` 冻结）+ 状态变化立即一次。**与语音抢占**：订阅 `APP_EVT_VOICE_STATE_CHANGED`，语音进入 LISTENING/THINKING/SPEAKING/FOLLOW_UP/ERROR → auto-pause（100ms），回 IDLE 时**保持暂停、不自动恢复**（T4.4 pause 模式）；云端 `pause` 是**用户暂停 latch**，会话结束不自动恢复。`music_request`（端侧主动点歌）与"继续失败"兜底归本任务。duck 叠加不再做（T4.4 定稿 pause 模式），seek/Range/断流重连归 T4.5。
  - **整段路径**保留给本地/探针（`audio_player_play_mp3`），**T3.3 起走 `CH_MUSIC`**（音乐通道，`player duck` 可验证 R3 duck；`mp3_test.py` 48/48 仍过）；PCM 预分配上限从 1.5MB 下调到 **512KB**。流式 TTS 走 `CH_TTS`，提示音走 `CH_PROMPT`。

---

## 4. 分层架构与模块（确定）

```
┌─────────────────────────────────────────────────────────┐
│ L4 业务编排   voice_session（会话状态机） music_session    │
├─────────────────────────────────────────────────────────┤
│ L3 能力       voice_cloud  music_cloud  dev_ctrl  diag   │
├─────────────────────────────────────────────────────────┤
│ L2 音频引擎   audio_fe  audio_player  codec_enc          │
├─────────────────────────────────────────────────────────┤
│ L1 基础服务   net_mgr  transport_ws  config_store  event_bus │
├─────────────────────────────────────────────────────────┤
│ L0 板级/驱动  board  audio_io                            │
└─────────────────────────────────────────────────────────┘
```

> **L 指 Layer（调用层级）**：数字越大越靠上层。依赖方向只能从大编号指向小编号（L4→L3→L2→L1→L0），禁止反向依赖或跨层直连；同层之间通过 `event_bus`/队列通信。`assets` 是纯资源组件，不参与分层。

### 4.1 模块清单

| 模块 | 层级 | 确定职责 | 对外接口 |
|---|---|---|---|
| `board` | L0 板级/驱动 | 引脚定义、按键、初始化顺序（板载 LED 引脚保留定义但**功能已移除**，见 PENDING PD-003） | `board_init()`, `board_button_pressed()` |
| `audio_io` | L0 板级/驱动 | I2S0 RX（16k 采集）/ I2S1 TX（48k 播放）HAL | `audio_io_init()`, `audio_io_read_mic()`, `audio_io_write_spk()`, `audio_io_spk_enable()` |
| `config_store` | L1 基础服务 | NVS 读写（音量、设备状态）+ 独占 `nvs_flash_init`；编译期常量（WiFi/WS/设备参数）走 Kconfig | `cfg_init()`, `cfg_get_int()`, `cfg_get_int_or()`, `cfg_set_int()`, `cfg_get_str()`, `cfg_set_str()`, `cfg_get_volume()`, `cfg_set_volume()`, `cfg_erase_key()` |
| `net_mgr` | L1 基础服务 | WiFi STA 连接、退避重连、SNTP 对时、网络事件（无独立任务：事件回调 + 一次性定时器） | `net_init()`, `net_is_up()`, `net_wait_ready(ms)`, `net_is_time_synced()`, `net_get_info()`, `net_reconnect()` |
| `transport_ws` | L1 基础服务 | wss 客户端：连接/退避重连、`hello`/`hello_ack` 握手、20s/10s 心跳、发送队列、二进制帧收发；入站回调支持主回调 + 附加订阅（T2.5，业务与 console 观测并存） | `ws_init()`, `ws_start()`, `ws_send_text()`, `ws_send_bin()`, `ws_register_cb()`, `ws_subscribe()`, `ws_unsubscribe()`, `ws_reconnect()`, `ws_is_ready()`, `ws_status_read()` |
| `event_bus` | L1 基础服务 | 全局事件定义与分发（基于 esp_event 独立循环，任务/队列可控） | `app_evt_init()`, `app_evt_register()`, `app_evt_unregister()`, `app_evt_post()`, `app_evt_name()` |
| `audio_fe` | L2 音频引擎 | AFE 封装：**半双工门控仲裁（T3.2：原因位图 `PROMPT/TTS/PROBE` + 恢复丢弃窗口 R5）**、唤醒/VAD 事件（T1.2b 起投 `APP_EVT_WAKE_DETECTED`/`APP_EVT_VAD_*` 到 event_bus，回调仅调试兼容）、上行 16k 单声道 PCM 输出（PSRAM 环形缓冲，T2.1） | `audio_fe_init(cb)`, `audio_fe_start()`, `audio_fe_feed_hold()`, `audio_fe_feed_release()`, `audio_fe_feed_discard_ms()`, `audio_fe_feed_stats_read()`, `audio_fe_feed_enabled()`, `audio_fe_set_feed_enabled()`（旧接口/探针兼容）, `audio_fe_wakenet_name()`, `audio_fe_uplink_start()`, `audio_fe_uplink_stop()`, `audio_fe_uplink_read()`, `audio_fe_uplink_stats_read()` |
| `audio_player` | L2 音频引擎 | 播放引擎：**三通道混音（T3.3：`CH_TTS`/`CH_PROMPT`/`CH_MUSIC`，PSRAM ring → 饱和相加 → 音量 20ms 插值 → -1dBFS 软限幅 → I2S）** + 重采样到 48k + 淡入淡出；提示音/整段 MP3/TTS 流式分别接入通道；**T3.2：输出级打断淡出（R4）与输出峰值/停声耗时观测；T3.3：duck 改为 `CH_MUSIC` 通道增益**；MP3 用 vendored minimp3（状态/临时区/ring 全 PSRAM），流式边收边解、欠载补静音、跨帧连续重采样；完成回调主槽 + 3 附加槽 `(src, ok, arg)`；音乐源/HTTP 随 P4 增加 | `audio_player_init()`, `audio_player_play_pcm()`, `audio_player_play_mp3()`, `audio_player_mp3_ready()`, `audio_player_register_done_cb()`, `audio_player_subscribe_done()`, `audio_player_unsubscribe_done()`, `audio_player_stream_open()`, `audio_player_stream_write()`, `audio_player_stream_end()`, `audio_player_stream_abort()`, `audio_player_stream_active()`, `audio_player_stream_stats_read()`, `audio_player_fade_out_and_stop()`, `audio_player_set_duck()`, `audio_player_get_duck()`, `audio_player_current_src()`, `audio_player_set_volume()`, `audio_player_stats_read()`, `audio_player_mix_stats_read()/reset()`, `audio_player_mix_tone()/stop()/capture()/capture_status()/capture_read()`, **T4.1：`audio_player_music_play_url(url)` / `audio_player_music_stop_url()` / `audio_player_music_start(rate)` / `audio_player_music_write(pcm48, frames, timeout)` / `audio_player_music_end()` / `audio_player_music_stop()` / `audio_player_music_active()` / `audio_player_music_stats_read()`，**T4.3：`audio_player_music_pause(fade_ms)` / `audio_player_music_resume()` / `audio_player_music_paused()`（暂停保留 ring 与读写位置、从暂停点续播）**，**T4.4：`audio_player_voice_active()`（区分语音作业与音乐泵）** |
| `codec_enc` | L2 音频引擎 | Opus 编码（16k/16kbps/20ms，定点实现 + VBR，状态/伪栈在 PSRAM，不建任务） | `enc_open()`, `enc_encode(pcm, samples, out, cap, &out_len)`, `enc_flush()`, `enc_reset()`, `enc_close()`, `enc_stats_read()` |
| `voice_cloud` | L3 能力 | 语音云协议：一轮会话的上行音频（`audio_fe` 读 PCM → Opus → `ws_send_bin`）、`audio_start/audio_end`、下行事件解析与 `sid` 过滤、事件/回调分发、**下行 0x02 接入 `audio_player` 流式播放 + R1/R2（T2.4；T3.2 起经 `audio_fe` 原因位仲裁）**；**不含会话状态机**（归 T3.1 `voice_session`）；T3.1 起回调增加 `VC_EVT_UPLINK_END`/`VC_EVT_ROUND_END` 供状态机驱动，`sid` 由会话机传入；T3.2 起 `vc_interrupt()` 负责 R4 的 `interrupt` 报文；**T3.6：`vc_is_ready()` 供唤醒前预判、`VC_EVT_TTS_FAIL`（code=`vc_tts_fail_t`）上报"设备侧 TTS 无法播放"** | `vc_init()`, `vc_start(sid)`, `vc_start_auto()`, `vc_stop()`, `vc_cancel()`, `vc_interrupt()`（T3.2：先发 `interrupt` 报文再 cancel）, `vc_is_busy()`, `vc_is_ready()`（T3.6）, `vc_preroll_start()`, `vc_preroll_stop()`（T3.7 续听预录）, `vc_get_state()`, `vc_register_cb()`, `vc_stats_read()` |
| `music_cloud` | L3 能力 | **音乐协议（T4.3 起纯协议）**：WS 附加订阅槽解析 `music_track`/`music_control`（零分配手写 JSON）、`mid` 过滤（P-3）、断链清基准、`music_status`/`music_request` 编码发送；**不调播放器、不建定时器** | `mc_init()`, `mc_register_cb()`, `mc_report_status()`, `mc_send_request()`, `mc_current_mid()`, `mc_have_mid()`, `mc_stats_read/reset()` |
| `dev_ctrl` | L3 能力 | 按键事件（GPIO0 扫描/去抖 → `APP_EVT_BUTTON`）、静音切换（长按，音量 0/恢复原值）；**T3.4 不建任务**（一个 10ms esp_timer）；**LED 状态指示已于 2026-11-02 移除**（板载 WS2812 解码不可靠→满白→BOD，见 PENDING PD-003），仅把 GPIO48 拉低保持 | `dev_init()`, `dev_register_cb()`, `dev_is_muted()`, `dev_set_mute()`, `dev_stats_read()/reset()` |
| `feedback` | L3 能力 | **异常反馈路由（T3.6）**：`fb_notify(kind, code, sid)` → ① 发布语义事件 `APP_EVT_FEEDBACK`（未来屏幕/灯光等通道订阅它，**语音只是一种反馈方式**）；② 当前语音通道：`assets` 提示音 → `audio_player_play_pcm()`（`CH_PROMPT`）+ R1 门控；TTS 流占用时 pending 到流完成回调再补播；**不建任务/定时器** | `fb_init()`, `fb_notify()`, `fb_abort()`, `fb_is_active()`, `fb_active_kind()`, `fb_register_done_cb()`, `fb_stats_read()/reset()`, `fb_kind_name()`（纯头 `feedback_policy.h`） |
| `diag` | L3 能力 | esp_console 命令、日志级别控制、堆/任务周期监控 | `diag_init()` |
| `voice_session` | L4 业务编排 | 会话状态机（IDLE/LISTENING/THINKING/SPEAKING/FOLLOW_UP/ERROR）、每轮 `sid` 分配、超时/错误策略（THINKING 超时与云端 error → ERROR，同轮不重试）、唤醒编排（T1.2b：订阅 event_bus，播应答提示音并做 R1/R2）、**T3.2：R4 完整打断（淡出→interrupt 报文→R5 丢弃→IDLE）、R3 音乐中唤醒放行、按键事件消费（短按打断/IDLE 起轮）**、**T3.7：连续对话 FOLLOW_UP 续听窗口（窗口内免唤醒起轮、人声确认、轮数上限、窗口内唤醒重开、运行期开关）**、**T3.6：四类异常（网络未连接/云端 error/THINKING 超时/TTS 失败）→ ERROR + `feedback` 播报，提示音完成回调驱动 `ERROR → IDLE`，`CONFIG_VS_ERROR_HOLD_MS` 只作兜底看门狗；唤醒时云端未 READY 直接给"网络未连接"反馈（不先播"我在呢"）；会话中途断网也出声**；**不建任务**（事件驱动，见 §9.3） | `vs_init()`, `vs_start()`, `vs_interrupt()`, `vs_is_busy()`, `vs_get_state()`, `vs_state_name()`, `vs_reason_name()`, `vs_current_sid()`, `vs_set_followup_enabled()`, `vs_followup_enabled()`, `vs_stats_read()`, `vs_stats_reset()` |
| `music_session` | L4 业务编排 | **音乐播放控制（T4.3）**：状态机（IDLE/PLAYING/PAUSED/STOPPED/ERROR）、起播/切歌重试、暂停/继续（保留 ring 与位置）、`music_request` 兜底、EOF 探测、5s 状态上报；**与语音抢占**（语音活跃 auto-pause、回 IDLE **保持暂停**（T4.4 pause 模式）、用户暂停 latch 优先）；**不建任务**（两个 esp_timer） | `ms_init()`, `ms_play()`, `ms_pause()`, `ms_resume()`, `ms_stop()`, `ms_request()`, `ms_get_state()`, `ms_info_read()`, `ms_stats_read/reset()` |
| `prompt_store` | L1 基础资源 | **提示音资源存储（T3.5）**：`assets` 分区自定义只读 pack（5 条 16k PCM）→ 启动校验 → `esp_partition_mmap` 零拷贝映射（失败回退 PSRAM + CRC 校验）；pack 缺失/损坏时降级（get 返回 NULL）；**不建任务** | `prompt_store_init()`, `prompt_store_ready()`, `prompt_store_info()`, `prompt_assets_get()`, `prompt_assets_name()` |

### 4.2 事件总线（确定）

```c
ESP_EVENT_DECLARE_BASE(APP_EVT);   /* event_bus.h；定义在 event_bus.c */

typedef enum {
    APP_EVT_ANY = -1,              /* 仅用于注册：订阅全部应用事件 */
    APP_EVT_NET_UP, APP_EVT_NET_DOWN,
    APP_EVT_WAKE_DETECTED,
    APP_EVT_VAD_SPEECH_START, APP_EVT_VAD_SPEECH_END,
    APP_EVT_ASR_PARTIAL, APP_EVT_ASR_FINAL,
    APP_EVT_TTS_START, APP_EVT_TTS_END,
    APP_EVT_VOICE_STATE_CHANGED,
    APP_EVT_BUTTON,
    APP_EVT_MUSIC_STATE_CHANGED, APP_EVT_MUSIC_ERROR,
    APP_EVT_FEEDBACK,              /* T3.6：异常反馈语义事件（语音只是当前通道） */
    APP_EVT_COUNT,                 /* 哨兵：事件数量 */
} app_evt_t;
```

**payload**：统一装进定长 32 字节的 `app_evt_data_t`（union），投递时整块拷贝，发布方无需保活；处理器拿到的指针只在回调内有效。T2.5 起语音事件也带 payload（文本超过 32B 的按定长截断，完整文本由 `voice_cloud` 日志/回调提供）：

| 事件 | payload 结构 |
|---|---|
| `APP_EVT_NET_UP` | `app_evt_net_up_t { char ip[16]; int8_t rssi; }` |
| `APP_EVT_NET_DOWN` | `app_evt_net_down_t { uint8_t reason; uint32_t retry_ms; }` |
| `APP_EVT_ASR_PARTIAL` / `APP_EVT_ASR_FINAL` | `app_evt_asr_t { uint16_t sid; uint16_t len; char text[28]; }`（T2.5 定稿；`len` 为实际拷贝字节数，UTF-8 中文约 9 字，超出截断） |
| `APP_EVT_TTS_START` | `app_evt_tts_start_t { uint16_t sid; uint8_t codec; uint8_t reserved; uint32_t sr; }`（`codec=1` 为 MP3） |
| `APP_EVT_TTS_END` | `app_evt_tts_end_t { uint16_t sid; uint16_t frames; }`（`frames` = 本轮收到的下行音频帧数） |
| `APP_EVT_VOICE_STATE_CHANGED` | `app_evt_voice_state_t { uint16_t sid; uint8_t from, to; uint8_t reason; uint8_t reserved[3]; uint32_t dur_ms; }`（T3.1 定稿；`from`/`to` 为 §7 状态枚举数值（**T3.7 追加 `FOLLOW_UP=5`，只追加不改序**）、`reason` 为迁移原因码、`dur_ms` 为迁移前状态停留时长；由 `voice_session` 在每次迁移后投递） |
| `APP_EVT_BUTTON` | `app_evt_button_t { uint8_t action; uint8_t reserved[3]; }`（T3.2 定稿；`action`：1=`SHORT` 短按、2=`LONG` 长按，非法值按短按处理。T3.4 的 `dev_ctrl` 只负责 GPIO 扫描/去抖并投递；`voice_session` 消费：会话中短按 = R4 打断、IDLE 短按 = 起一轮；**长按的静音语义归 `dev_ctrl` 自己执行（T3.4 已交付），`voice_session` 忽略长按**） |
| `APP_EVT_FEEDBACK` | `app_evt_feedback_t { uint16_t sid; int32_t code; uint8_t kind; uint8_t reserved[3]; }`（T3.6 定稿；`kind` = `fb_kind_t` 数值：1=`net-offline`、2=`cloud-error`、3=`timeout`、4=`tts-fail`；`code` 为原始错误码/设备侧 `vc_tts_fail_t`。由 `feedback` 组件在 `fb_notify()` 调用方上下文投递——**这是"语音只是一种反馈方式"的扩展点**：未来屏幕/灯光组件订阅本事件即可，不改 `feedback`/`voice_session`） |
| `APP_EVT_MUSIC_STATE_CHANGED` / `APP_EVT_MUSIC_ERROR` | `app_evt_music_state_t { uint16_t mid; uint8_t from, to, reason; uint8_t reserved[3]; uint32_t progress_ms; uint32_t dur_ms; }`（T4.3 定稿，20B；`from`/`to` 为 `mc_state_t` 数值、`reason`：0=起播 1=协议 2=EOF 3=错误 4=语音抢占 5=用户暂停 6=用户继续 7=停止；`APP_EVT_MUSIC_ERROR` 复用同一结构，`to=ERROR`；由 `music_session` 在每次状态迁移后投递） |

**投递与线程约束**：

- 独立循环不占系统 default loop（避免与 WiFi/`esp_netif` 处理器互相阻塞），队列长度 `CONFIG_EVENT_BUS_QUEUE_SIZE`（默认 16），投递一律非阻塞（`ticks_to_wait = 0`），满则丢弃 + WARN；
- 处理器在 `evt` 任务上下文执行，不得做耗时/阻塞操作；
- **音频实时任务（`afe_feed`/`afe_fetch`）只能投递无 payload 事件**：esp_event 带 payload 时会内部 `calloc` 拷贝，违反 §4.3 第 2 条。
- **T1.2b 落地（T3.1）**：`audio_fe` 在 fetch 任务里投递 `APP_EVT_WAKE_DETECTED` / `APP_EVT_VAD_*`（均无 payload，满足上一条），业务侧由 `voice_session` 的 `APP_EVT_ANY` 处理器消费；`audio_fe_init(cb)` 的回调参数保留但产品路径传 `NULL`（仅调试/兼容用）。

### 4.3 依赖规则（强制）

1. 只允许上层依赖下层；同层通过 `event_bus` 通信。
2. 音频任务内禁止网络、Flash、malloc、阻塞等待。
3. 每个组件只暴露 `include/<module>.h`，其余符号内部可见。
4. 大块缓冲一律 `heap_caps_malloc(..., MALLOC_CAP_SPIRAM)`；I2S DMA 缓冲必须内部 RAM。
5. 每个组件带 `Kconfig`，禁止把可调参数硬编码在 `.c` 里。

---

## 5. 项目结构（确定）

```
esp-ai/
├── CMakeLists.txt
├── partitions.csv                  # 第 9.1 节定稿
├── sdkconfig.defaults
├── sdkconfig.defaults.esp32s3
├── main/
│   ├── CMakeLists.txt
│   ├── app_main.c                  # 初始化编排（≤150 行）
│   └── Kconfig.projbuild           # WiFi/服务器/设备参数
├── components/
│   ├── board/
│   ├── audio_io/
│   ├── audio_fe/
│   ├── audio_player/               # decoder_mp3.c / resample.c / mixer.c / source_http.c（不做 source_file）
│   ├── prompt_store/               # T3.5：提示音 pack（assets 分区）加载/映射（L1）
│   ├── codec_enc/
│   ├── config_store/
│   ├── net_mgr/
│   ├── transport_ws/
│   ├── event_bus/
│   ├── voice_cloud/
│   ├── music_cloud/
│   ├── voice_session/
│   ├── music_session/
│   ├── dev_ctrl/
│   └── diag/
├── docs/
│   ├── ARCHITECTURE.md             # 本文档
│   ├── HARDWARE.md
│   └── PROTOCOL.md                 # 第 6 节契约的详细版（后续实现时同步补全）
├── tools/
│   ├── ws_mock_server.py           # 本地云端模拟入口（CLI；实现见 mock_* 模块）
│   ├── mock_core.py                # 协议帧 / MP3 帧解析 / Config / Stats
│   ├── mock_server.py              # Session（单连接）+ MockServer（WS 服务）
│   ├── mock_downlink.py            # 下行 TTS 流 / ASR 文本 / 错误注入
│   ├── mock_cli.py                 # 参数、校验、Config 装配、ASR/Piper 加载
│   ├── mock_self_test.py           # 19 项进程内自测（--self-test）
│   ├── mock_fixtures.py            # 自测/错误注入用内嵌真实 MP3
│   ├── audio_pack.py               # 提示音格式转换与 assets 打包
│   └── record_wav.py               # 串口采集 WAV，用于调唤醒/AEC 实测（T5.2 计划）
└── test/host/                      # 帧头解析、状态机、协议编解码单测
```

### 5.1 现有 POC 迁移映射（确定）

| 现有文件/函数 | 迁入 |
|---|---|
| `rx_init()` / `tx_init()` | `components/board`（引脚）+ `components/audio_io` |
| `feed_Task()` | `components/audio_fe` 内部采集任务 |
| `detect_Task()` | `components/audio_fe` 回调 → `event_bus` |
| `play_voice_task()` | `components/audio_player` 的 `CH_PROMPT` 路径 |
| `voice_data.c` | 转为 `components/assets`（临时内置），P3 迁到 `assets` 分区 |
| `app_main()` | `main/app_main.c` 初始化编排 |

迁移后 `main/` 只保留组装逻辑，原 `esp-ai.c` 删除。

---

## 6. 云端对接契约（端侧实现，服务端按此对接）

### 6.1 连接与鉴权

- 地址：`wss://<host>/v1/voice?device_id=<id>&token=<token>&ver=1`
- 参数来自 Kconfig：`CONFIG_AI_WS_URL`、`CONFIG_AI_DEVICE_ID`、`CONFIG_AI_TOKEN`；URL 里缺省的 `device_id/token/ver` 由端侧补齐，日志只打印 scheme+host+path（**不回显 query**，token 不进日志）
- 连接成功后发送 `{"t":"hello","device_id":..,"ver":1}`
- **服务端必须回 `{"t":"hello_ack","ver":1}`**：端侧 5s 内未收到视为建连失败，关闭连接并按退避重连（T1.4 定稿新增，见 PROTOCOL.md P-1）
- 心跳：每 20s 发 `{"t":"ping"}`；服务端回 `{"t":"pong"}`（不带其它字段）；10s 内未收到 `pong` 判定断线，RTT 用端侧 tick 计算
- 重连退避：1s → 2s → 5s → 10s → 30s（上限），无限重试直到网络恢复；每次重连成功都重发 `hello` 并等 `hello_ack`
- 鉴权失败（`device_id`/`token` 非法）：服务端以应用关闭码 **4001（device_id）/ 4002（token）** 关闭连接；端侧 ERROR 日志后**不再自动重试**，等 `NET_UP` 或 `ws reconnect` 再试（T1.4 定稿，见 PROTOCOL.md P-5）
- 传输：线上只用 `wss://`；本地联调可用 `ws://`（由 `CONFIG_AI_WS_URL` 的 scheme 决定是否走 TLS，见 PRODUCT_IDEAS §8 X2 方案 A）；`wss` 建连前要求 SNTP 已对时（§10.1）
- **服务端必须关闭 Nagle（`TCP_NODELAY`）**：TTS 帧按 20~30ms 均匀下行，Nagle + 端侧 delayed ACK 会把帧攒批 ~200ms 再交付，端侧流式 ring 会被打空（T2.4 实测：mock 未开 NODELAY 时稳态欠载；开启后 0 欠载）。

### 6.2 二进制帧（音频）

```
 0    1    2    3    4    5    6    7
+----+----+----+----+----+----+----+
|0x56|0x4F|ver |type| seq(2B BE) | len(2B BE) |
+----+----+----+----+----+----+----+
| payload: 编码音频数据 ...          |
```

| type | 方向 | 内容 |
|---|---|---|
| 0x01 | ↑ | 上行麦克风音频（Opus 16k/16kbps/20ms） |
| 0x02 | ↓ | 下行 TTS 音频（MP3 24k 单声道） |

- `seq`：每端独立、按帧自增（uint16 回绕），由 `transport_ws` 维护，业务层不参与；
- **帧头不带 `sid`**（T2.5 定稿 P-2）：同一连接内音频帧按 `audio_start → tts_end` 的顺序归属当前轮次。端侧实现只在"已发 `audio_end`、等待响应"的窗口内接收下行 0x02 帧，窗口外的迟到帧（上一轮残留）直接丢弃；`tts_end` 归零该窗口。这样既不扩帧头、也不让跨轮迟到帧混入。
- **T2.4 端侧实现**：窗口内且未收 `tts_end` 的 0x02 → `audio_player_stream_write()`（单次 ≤2KB、非阻塞；ring 满丢弃整段）；窗口外/`tts_end` 后/播放器未开流/非 mp3/播放器忙 → 丢弃并计入 `vc stat` 的「音频丢弃 N 帧 字节 M」；`tts_end` → `stream_end()`，`vc_cancel`/error/断链/新轮次/响应超时 → `stream_abort()`。

### 6.3 文本帧（JSON）

**上行**
| t | 说明 |
|---|---|
| `hello` | 连接后握手，带 device_id/ver |
| `audio_start` | 开始一轮上行，带 `sid` |
| `audio_end` | 上行结束（VAD 静音或超时），带 `sid` |
| `interrupt` | 用户打断，带 `sid` |
| `ping` | 心跳 |
| `music_request` | 端侧主动点歌（T4.3 实现）：`{"t":"music_request","action":"play","id":<uint16 请求序号>,"index":<曲目索引>}`；`id` 端侧自增、仅用于日志关联，`index` 为 0/-1 表示"服务端自选"；当前 `action` 只有 `play` |
| `music_status` | 播放状态上报（T4.2 实现、T4.3 增 `paused`）：`{"t":"music_status","state":"playing\|paused\|stopped\|error","mid":..,"progress_ms":..}`；播放中每 `MS_STATUS_PERIOD_MS`（5s）+ 状态变化立即一次，暂停期 `progress_ms` 冻结 |

**下行**
| t | 说明 |
|---|---|
| `hello_ack` | 握手应答（T1.4 定稿），带 `ver`；5s 内未收到按建连失败处理 |
| `asr_partial` / `asr_final` | ASR 中间/最终结果，带 `sid`、`text` |
| `llm_delta` | LLM 增量文本，带 `sid`、`text` |
| `tts_start` / `tts_end` | TTS 起止，带 `sid`、`codec`、`sr`（`tts_end` 只带 `sid`） |
| `music_track` | 曲目信息（含 URL，T4.2 已实现）：`{"t":"music_track","mid":..,"url":"https://...mp3","codec":"mp3","duration_ms":..,"title":..}`；**下一首/上一首也用它**（服务端下发新曲目）；`duration_ms=0` 表示直播流/时长未知，端侧不能等 EOF 收尾 |
| `music_control` | 播放控制（T4.2 解析、T4.3 执行暂停/继续）：`{"t":"music_control","mid":..,"action":"pause\|resume\|stop"}`；`pause` = 用户暂停 latch（会话结束不自动恢复）、`resume` 清 latch 并从暂停点续播、`stop` 丢弃 ring 停止 |
| `error` | 错误码与描述：`{"t":"error","sid":..,"code":..,"msg":..}`，码表见 PROTOCOL.md §4 |
| `pong` | 心跳应答：`{"t":"pong"}`（不带其它字段） |

规则：语音类下行事件都带 `sid`；端侧丢弃 `sid` 不匹配当前会话的迟到帧（`voice_cloud` 计数并在 DEBUG 日志里记录）。

- **音乐事件用独立 `mid`（P-3，2026-11-10 方案已定；端侧 T4.2 落地）**：音乐可以跨语音轮次播放（TTS 播完继续放），也可以在**没有语音轮次**时由按键/云端推荐开始，用语音 `sid` 过滤会被「轮次结束 / 迟到帧丢弃」误杀。端侧 T4.2 按 `t` 分流：语音事件走 `voice_cloud` 的 `sid` 过滤，音乐事件走 `music_cloud`（不参与语音窗口判定）；`mid` 在**每条连接内**单调自增（uint16 回绕），端侧用来丢弃本次连接内的过期音乐事件。T4.2 落地规则：`music_track` 只接受比当前更新的 `mid`（同 `mid` 视为重复、不重复起播），`music_control` 允许 `mid == 当前曲目`（暂停/停止本就指向正在播的这首）、只丢弃更旧的；WS 断链后清掉比较基准（音乐可跨连接继续播）。
- **URL 只出现在 `music_track`**：端侧**禁止**从 `llm_delta`/`asr_final` 文本解析 URL（防 prompt 注入、防 TTS 把链接念出来）；T4.2 只做 `http(s)` 前缀 + 长度（320B）+ `codec=mp3` 校验，**域名白名单尚未实现**（可选加固，见 PENDING/T5）。
- **音乐意图归属云端（2026-11-12 定稿：LLM 工具调用）**：端侧不做 NLU。云端把音乐能力做成 **MCP 服务**（`tools/music_mcp_server.py`，工具 `music_search/play/pause/resume/next/prev/stop/status`），由 Hermes/LLM 决定调用哪个工具；**执行结果仍翻译成既有 `music_track`/`music_control` 下行**，端侧协议零改动。工具结果默认带 `url`（客户端直接用它下发 `music_track`，mock 不再必须加载曲库）；模型因此**能看到 URL**，但客户端**只接受工具结果里的 URL**、不采纳模型在 arguments 里传的 URL，并校验 `http(s)` + 长度 <320；禁止从 LLM 文本解析 URL。若需要严格不让 URL 进模型上下文，可继续给 mock/MCP 配同一份曲库走 `track_id` 回退。原「确定性关键词规则表」已于 2026-11-12 删除（用户真机实测命中率低）。本地 mock 用 Responses API 的 `mcp_call`/`function_call` 解析（`tools/responses_client.py`），确认语用模板，一轮只调 1 次模型。见 PRODUCT_IDEAS M2、PENDING PV-019/PV-020。
- **T4.1/T4.2/T4.3 端侧边界**：T4.1 只做 `source_http`（URL → 流式 MP3 → `CH_MUSIC`）；T4.2 做 `music_cloud` 的解析/`mid` 过滤/协议收发；T4.3 做 `music_session` 的播放状态机、暂停/继续/切歌、`music_request` 与语音抢占（`music_cloud` 自 T4.3 起**不再直接驱动播放器**）。

**错误码表（T2.5 定稿 P-4）**：`1`=通用/未知、`1001`=鉴权、`2001/2002`=ASR 失败/超时、`3001/3002`=TTS 失败/超时、`9001`=服务端内部；端侧统一"ERROR 日志 + 结束本轮（不发 audio_end 重试）"，提示音由 T3.6 负责。

### 6.4 时延目标（端侧可达性约定）

| 指标 | 目标 |
|---|---|
| 唤醒 → 上行首帧 | < 200ms |
| TTS 首帧 → 出声 | < 150ms |
| 按键打断停声 | < 100ms |
| 音乐起播（首帧到出声） | < 1.5s |
| 连续播放卡顿率 | < 0.1% |

> **T2.4 实测（2026-10-31）**：TTS 首帧→出声 **14~29ms**（设备侧：首个 0x02 payload → 首个 I2S 写；prebuffer 96ms 后起播），远低于 150ms；`vc cancel` 打断停声实测 **51~52ms**（P3 的「按键打断 <100ms」在 T3.4/T3.2 的完整交互链路上再收紧）。

---

## 7. 会话状态机（确定）

| 状态 | 行为 | 进入 | 退出 |
|---|---|---|---|
| `IDLE` | 只听唤醒，音乐可播放 | 上电 / 会话结束 | 唤醒 → `LISTENING`；按键 → `LISTENING` |
| `LISTENING` | 上行音频，VAD 判停 | 唤醒/按键 | 静音 800ms 或 8s 超时 → `THINKING` |
| `THINKING` | 等 ASR/TTS，播放提示音可配置 | `LISTENING` 结束 | `tts_start` → `SPEAKING`；10s 超时 → `ERROR` |
| `SPEAKING` | 播放 TTS，关闭麦克风输入（R1） | `tts_start` | `tts_end` → 延时 300ms 恢复 feed → `FOLLOW_UP`（连续对话开启，T3.7）或 `IDLE`（关闭）；按键 → `IDLE` |
| `FOLLOW_UP` | 续听窗口：麦克风保持开启，等用户直接说话（不播提示音）；VAD 人声经 250ms 确认 → `LISTENING`（新 sid，免唤醒）；窗口内唤醒词 → 重开一轮（播提示音、轮数清零） | TTS 排空后（连续对话开启） | 窗口静音超时 / 续听轮数上限 / 按键 / 断网 → `IDLE` |
| `ERROR` | 异常反馈中：`feedback` 播放对应提示音（语音通道）+ 上报日志；同轮不重试 | 网络未连接 / 云端 error / THINKING 超时 / TTS 失败（§8.6） | 提示音完成 → `IDLE`；`CONFIG_VS_ERROR_HOLD_MS` 超时（看门狗）→ 强制 `IDLE`；按键 → 中止反馈回 `IDLE` |

规则：
1. 每轮会话 `sid` 自增（uint16），异步帧必须匹配当前 `sid`；
2. 状态迁移统一经 `voice_session`，其它模块不得直接改状态；
3. 所有迁移打 INFO 日志（`voice` tag），包含 `sid` 与停留时长；
4. `ERROR` 状态不自动重试，由用户再次唤醒触发新一轮；
5. **按键（T3.2/T3.4）**：任意活跃状态短按 = R4 打断 → `IDLE`（`FOLLOW_UP` 窗口内短按 = 退出续听回 `IDLE`）；`IDLE` 短按 = `vs_start()` 直接进 `LISTENING`（不播应答提示音）；长按 = 静音切换（由 `dev_ctrl` 自己处理，`voice_session` 忽略；静音无 LED 指示，见 PENDING PD-003）。按键来源自 T3.4 起是真实 GPIO0（`dev_ctrl` 的 10ms 扫描 + 30ms 去抖），console `evt post BUTTON` 保留为注入探针。
6. **连续对话（T3.7）**：`FOLLOW_UP` 续听窗口默认 5s、最多 5 轮续听、人声确认 250ms；窗口内直接说话免唤醒且不播提示音；窗口内唤醒词 = 重开一轮；静音超时/轮数上限/按键/断网退出；Kconfig `VS_FOLLOWUP_*` + 运行期 `vs followup on|off`。
7. **异常反馈（T3.6）**：四类异常（网络未连接 / 云端 error / THINKING 超时 / TTS 失败）→ `ERROR` + `feedback` 提示音；**唤醒时云端未 READY 不播"我在呢"，直接播"网络未连接"**（避免两段提示音连播）；**会话中途断网也出声**；`ERROR` 期间按键 = 中止反馈回 `IDLE`；同轮不重试。详细映射与扩展点见 §8.6。
8. **音乐与语音抢占（T4.3）**：音乐状态由 `music_session` 独占，`voice_session` 不直接操作音乐播放器。`music_session` 订阅 `APP_EVT_VOICE_STATE_CHANGED`：语音进入 `LISTENING/THINKING/SPEAKING/FOLLOW_UP/ERROR` → **auto-pause**（100ms 淡出，让出播放器给提示音/TTS）；回 `IDLE` 时**保持暂停、不自动恢复**（T4.4 pause 模式：等用户明确说“继续”）。云端 `music_control{pause}` = **用户暂停 latch**，会话结束不自动恢复；`resume` 清 latch 并从暂停点续播。音乐播放中唤醒**不丢弃**（R3：音乐不关麦）。

> **T3.1 实现状态（2026-11-01）**：
> - 状态机在 `components/voice_session`（L4）。迁移统一经 `vs_transition()`/`vs_begin_session()` 的 CAS（期望状态 → 目标状态），并发来源（`evt` 任务、`ws_client`、`player` 完成回调、console/timer）不会打乱状态；
> - `sid` 由 `voice_session` 分配（uint16 自增、0 保留），经 `vc_start(sid)` 交给 `voice_cloud`；`vc_start_auto()` 只留给 console 探针与测试脚本；
> - `LISTENING` 的收尾（VAD 静音 800ms / 累计 8s）仍在 `voice_cloud`（协议执行器），发出 `audio_end` 后回调 `VC_EVT_UPLINK_END` 驱动 `LISTENING → THINKING`；
> - `THINKING` 的 10s 计时也在 `voice_cloud`（`CONFIG_VC_RESPONSE_TIMEOUT_MS`；收到 TTS 数据会刷新为"数据断流超时"），超时回调 `VC_EVT_ROUND_END(TIMEOUT)` 驱动 `THINKING → ERROR`——状态机决策、协议侧计时，避免两套计时器互相打架；
> - `SPEAKING → IDLE` 在 `player` 完成回调（`STREAM_DONE`）或"本轮无可播音频"的 `VC_EVT_ROUND_END(DONE)` 时发生，保证 TTS 排空后再收状态；
> - `ERROR` 的提示音与收尾见下方 T3.6 实现状态（T3.1 时只留了 `CONFIG_VS_ERROR_HOLD_MS` 占位）；同轮不重试；
> - 断链（`NET_DOWN` / `VC_ROUND_ABORT_LINK`）→ 取消本轮 → 错误反馈（T3.6 起）→ `IDLE`；按键打断骨架 `vs_interrupt()`（完整 R4 淡出与按键来源归 T3.2/T3.4）。

> **T3.7 实现状态（2026-11-01）**：
> - 新增状态 `FOLLOW_UP`（枚举值 5，追加在末尾不改既有值）；`vc_round_end(DONE)`/`STREAM_DONE(ok=true)` → `SPEAKING → FOLLOW_UP`，ok=false（打断/失败）仍直接回 `IDLE`；
> - 窗口计时用**一个一次性 `esp_timer`**（`vs_fup`，不建任务；内存实测见 §9.2）；进入窗口时记录 deadline 供 `vs status` 显示剩余；
> - **续听预录（2026-11-03 修）**：进入窗口即 `vc_preroll_start()` 开始写上行环形缓冲，起轮时接管（裁到 `CONFIG_VC_UPLINK_PREROLL_MS`，默认 1000ms），避免"只听到后半句"；窗口退出/重新唤醒 `vc_preroll_stop()`；
> - 窗口内 VAD 人声先经 `CONFIG_VS_FOLLOWUP_CONFIRM_MS`（默认 250ms）确认，持续人声才 `FOLLOW_UP → LISTENING` 并 `vc_start(新 sid)`（不播提示音、不再唤醒）；确认期内 VAD 人声结束则取消确认并恢复剩余窗口；
> - 轮数上限 `CONFIG_VS_FOLLOWUP_MAX_ROUNDS`（默认 5，仅计续听轮）；达到上限 → `IDLE`（原因 `followup-max`）；每次重新唤醒/按键起轮清零；
> - 窗口中再次收到唤醒词 → 重开一轮（`FOLLOW_UP → LISTENING`，播应答提示音、计数清零；原因 `followup-wake`）；
> - 运行期开关 `vs followup on|off`（`vs_set_followup_enabled`，不持久化，重启恢复 Kconfig 默认；关闭时若正在窗口内立即退出）；
> - 统计：`vs status` 的「续听」行（续听轮/命中/超时退出/上限退出、窗口剩余）；
> - **已知限制**：环境人声持续时仍可能误触发续听轮（确认窗口只过滤短噪声，轮数上限兜底），嘈杂环境建议 `vs followup off` 或调小窗口/轮数；窗口内说唤醒词可能先被 VAD 当作续听语音（不播提示音），属预期竞争。

---

## 8. 关键流程

### 8.1 一轮语音对话

```
IDLE ──唤醒──> LISTENING ──VAD静音──> THINKING ──tts_start──> SPEAKING ──tts_end──> IDLE
   │                │                      │                      │
 wake 事件      上行Opus→WS            ASR/LLM事件             TTS MP3帧→解码→48k→喇叭
```

连续对话（T3.7，默认开启，`CONFIG_VS_FOLLOWUP_ENABLE=y`）：

```
SPEAKING ──TTS 排空 + R2 恢复收音──> FOLLOW_UP ──VAD 人声≥250ms──> LISTENING（新 sid，不播提示音）──> THINKING → SPEAKING → …
                                        │
                                        └── 窗口 5s 无语音 / 达到 5 轮续听 / 按键 / 断网 → IDLE
                                        └── 窗口内唤醒词 → 重开一轮（播提示音、续听计数清零）
```

### 8.2 按键打断（半双工，T3.2 实现）

```
任意活跃状态 ──短按──> player fade_out(80ms) → interrupt{sid} → vc_cancel（作废 sid）
                     → audio_fe 恢复收音（R5：丢弃最早 50ms）→ IDLE
                                                  （再按一次进入 LISTENING，见 §7）
```

- 触发源（T3.4 起）：**真实 GPIO0**，由 `dev_ctrl` 的 10ms `esp_timer` 扫描 + 30ms 去抖 → 投 `APP_EVT_BUTTON`（payload `app_evt_button_t`，短按=1/长按=2）；console `vs interrupt` / `evt post BUTTON` 保留为注入探针（不经过 GPIO 路径）；长按（≥600ms）= 静音切换，由 `dev_ctrl` 自己执行，`voice_session` 只记 DEBUG 日志不动作；
- `IDLE` 下短按 = `vs_start()` 直接起一轮（不进提示音路径，提示音只在唤醒时播）；
- 淡出结束后播放完成回调以 `ok=false` 上报，TTS 的 `TTS` 门控位由回调释放（≠ 打断时立刻放麦，避免淡出尾音被麦克风拾进 AFE）；
- 云端侧：`interrupt` 报文发出即作废本轮，服务端不再下发 `tts_end`（PROTOCOL §8）。

### 8.3 音乐播放

```
music_session ──music_request──> 服务端 ──music_track{url}──> audio_player_music_play_url(url)
       │                                                        │
  music_status 每 5s 上报                      music_http 任务：HTTP(S) → 独立 minimp3
       │                                         → 48k 单声道 PCM ring → CH_MUSIC
       └────────── 暂停 / 继续 / 停止 / 切歌 ◄──────────────────┘
                          （下行 music_control）

暂停（T4.3）：music_control{pause} 或 语音进入活跃态
        ├─ audio_player_music_pause(100ms)：CH_MUSIC 淡出 → 结束 PUMP 作业（保留 ring 与 rd/wr）
        └─ source_http_pause()：music_http 停读 socket（ring 满 → TCP 背压，不丢数据）
继续：source_http_resume() → 重新入队 PUMP → 从暂停点续播（不清 ring、不回文件头）
```

> **实现状态（2026-11-10，T4.1–T4.3 真机验收通过）**：T4.1 `source_http` + `music play/stop/stat` 探针、T4.2 `music_cloud` 协议、
> T4.3 `music_session` 状态机与暂停/继续/切歌/`music_request`/语音抢占均已交付并上板验证（`music_e2e_test` 62/62、`music_proto_test` 22/22）。
> **T4.4 C1（2026-11-11 已上板）**：删除“语音回 IDLE 自动恢复”，音乐被语音/按键打断后**保持暂停**；按键打断语音=丢弃、只有音乐=暂停保留。**T4.5（2026-11-12 构建完成，待真机）**：预缓冲 200ms、断流重连 + **Range 按字节偏移续播**、初始连接退避重试、坏帧 resync；语音打断（需 AEC）/duck 叠加仍不做（C3 no-go），域名白名单未做。
> `music_cloud` 与 `music_session` 都**不建任务**：解析在 `ws_client` 上下文，播放决策/重试/周期上报在两个 `esp_timer`。
> **语音期间的动作一律排队到 IDLE**（2026-11-10 真机修复）：新 `music_track` 排队起播；`music_control{resume}` 也排队——若立即 `audio_player_music_resume()` 会占用单作业播放器，紧接着的确认语 TTS 直接 `tts-fail`（真机日志 `VS: state THINKING -> ERROR（原因=tts-fail）`）。排队只在 `LISTENING/FOLLOW_UP`（新一轮）清空，`THINKING/SPEAKING` 期间保留；暂停/停止/切歌会丢弃排队项。
> **已知限制**：暂停超过服务端 idle 超时（或 TCP 断）后"继续"：T4.5 起 `source_http` 用 `Range: bytes=N-` 从暂停点续播（206 直接续、200 丢弃前 N 字节；最多 3 次、退避 300ms）；重连失败仍转 ERROR/STOPPED 并发一次 `music_request` 兜底（PV-015 待真机验证）；**语音活跃期间到达的新 `music_track` 排队到语音回 IDLE 才起播**，期间 `music_status` 可能先带新 `mid`（PENDING **PD-006**）。

### 8.4 音乐播放中唤醒

```
音乐播放中 ──语音/按键打断──> music_session auto-pause（100ms 淡出，让出播放器）──> LISTENING
                                  │（会话结束）
                                  ▼
            一律保持暂停（T4.4 pause 模式）；用户说“继续”→ 从暂停点续播
            新 music_track → 替换暂停曲目；说“停止”→ 丢弃 ring
```

> **T4.4 定稿（2026-11-11 用户拍板，pause 模式）**：
> - 音乐播放中语音/按键打断 → 100ms 淡出 → **暂停并保留**（保 ring/进度），**会话结束不自动恢复**；用户说“继续”→ 从暂停点续播；新歌替换；说“停止”丢弃。
> - TTS 播放中按键打断 → 80ms 淡出 → **丢弃**（abort + 发 `interrupt` + 作废 sid）；语音打断 TTS 依赖 AEC + 开麦，归 T4.4b（尚未实现）。
> - **C1 已上板（2026-11-11）**：按键起轮/打断 → 音乐暂停；回 IDLE 保持暂停；`ms resume` 从暂停点续播；新 track 替换暂停曲目。
> **真机现象（2026-11-10，T4.4b 必须解决）**：音乐出声时唤醒词被掩蔽——设备混音音量 100/15/10 时 `afplay lexin.wav`（系统 45%、`-v 2`）都无法唤醒，**音量 0 才稳定唤醒**；语音打断需 `esp_aec`（SR/FD 模式），先 spike 再定。

### 8.5 断线重连

- `net_mgr` 检测到断开 → `APP_EVT_NET_DOWN`（**只在已联网后掉线时投递**；上电首次连不上不投，避免上层处理正常中间态），payload 带驱动原因码与本次退避延时；
- 正在进行的会话立即终止：取消本轮（TTS 流 abort）→ **播"网络未连接"提示音**（T3.6）→ 回 `IDLE`；
- 续听窗口（`FOLLOW_UP`）期间断网：窗口按 T3.7 退出，并补一声"网络未连接"（不进 `ERROR`，窗口本就结束）；
- 待机（`IDLE`）断网：不出声（独立"断网状态"反馈仍是 PENDING PD-002 的待决定项）；
- 自动重连成功后重新 `ws_start()` + `hello`；
- 重连期间唤醒仍可用（本地唤醒不依赖网络）；唤醒时若 `vc_is_ready()==false`（WS 未 READY），**不播"我在呢"，直接播"网络未连接"** 并回 `IDLE`（ARCH §7 规则 7）。

### 8.6 异常反馈（T3.6，可扩展通道）

```
异常来源                                  feedback 组件（L3）
────────                                  ──────────────────────────────
唤醒/起轮时 ws 未就绪 ─┐                  fb_notify(kind, code, sid)
会话中 NET_DOWN        ├─> voice_session ──┬─> ① APP_EVT_FEEDBACK（语义事件，任意订阅方）
WS 断链（ROUND_LINK）  │      （状态机）    │      ├─ 当前：无订阅者（语音在 feedback 内部）
云端 error（按 code 分流）┘                 │      └─ 未来：屏幕/灯光组件订阅 → 展示反馈
                                           └─> ② 语音通道：assets 提示音 → CH_PROMPT
                                                  + R1 hold(PROMPT) → 播完 release
                                                  → fb_done_cb → voice_session: ERROR→IDLE
THINKING 超时（10s）───┘
设备侧 TTS 播不出来（VC_EVT_TTS_FAIL）──┘
```

| 类别（`fb_kind_t`） | 触发 | 提示音 |
|---|---|---|
| 1 `net-offline` | 唤醒/起轮时 ws 未 READY、`NET_DOWN`（会话中）、`VC_ROUND_ABORT_LINK` | `网络未连接` |
| 2 `cloud-error` | 云端 `error`：1/1001/2001/2002/9001/未知（ASR 失败归服务异常） | `服务出了点问题` |
| 3 `timeout` | `THINKING` 等待超时（`CONFIG_VC_RESPONSE_TIMEOUT_MS`，含数据断流） | `响应超时` |
| 4 `tts-fail` | 云端 3001/3002；设备侧无法播放 TTS（非 MP3/缺采样率/MP3 能力不可用/播放器忙/开流失败） | `语音播放失败` |

实现要点：

- **语音只是一个通道**：`feedback` 先投 `APP_EVT_FEEDBACK`（未来屏幕/灯光订阅），再驱动语音通道；新增展示方式 = 新组件订阅事件，**不改 `feedback`/`voice_session`**；
- **与 TTS 的竞态**：TTS 流占用时 `audio_player_play_pcm()` 会被拒（流式期间拒绝 PCM）；`feedback` 置 pending，等 `TTS_STREAM` 完成回调（`voice_session` 已先 `vc_cancel` 触发 abort）再补播，保证异常不静默；实测 SPEAKING 中云端报错：TTS 先停（`job done` ~120ms）→ 再播提示音，无叠音；
- **收尾**：提示音完成回调 → `voice_session` 停看门狗并把 `ERROR → IDLE`；`CONFIG_VS_ERROR_HOLD_MS`（默认 3000ms）只作兜底，超时 `fb_abort()` + 强制回 `IDLE`；
- **按键语义**：`ERROR` 期间短按 = 中止本次反馈（淡出 + 释放 R1）回 `IDLE`，不发 `interrupt` 报文（本轮已作废）；
- **不新增任务/定时器之外的资源**：`feedback` 不建任务、不定时器；`voice_session` 复用原有一次性 `esp_timer` 做看门狗；
- **待机断网**仍不出声（PD-002 待决定）；未来若做，可扩展 `fb_kind_t` 或独立"网络状态"通道，同样订阅事件，不破坏本设计。

---

## 9. 分区与资源

### 9.1 分区表（定稿）

| Name | Type | SubType | Offset | Size | 用途 |
|---|---|---|---|---|---|
| nvs | data | nvs | 0x9000 | 24K | 音量、设备状态 |
| otadata | data | ota | 0xf000 | 8K | 预留给后续 OTA |
| phy_init | data | phy | 0x11000 | 4K | RF 校准 |
| ota_0 | app | ota_0 | 0x20000 | 3M | 当前运行固件 |
| ota_1 | app | ota_1 | 0x320000 | 3M | 预留 OTA 槽位（当前版本不用） |
| model | data | spiffs | 0x620000 | 1536K | srmodels（WakeNet + VAD） |
| assets | data | spiffs(内容非 SPIFFS) | 0x7a0000 | 2M | **提示音自定义只读 pack（T3.5）**：5 条 16k PCM，`tools/audio_pack.py` 生成/烧录；单独烧该分区即可替换提示音 |
| coredump | data | coredump | 0x9a0000 | 64K | 崩溃现场（已启用落盘：`CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y`，`idf.py coredump` 解析） |
| storage | data | fat | 0x9b0000 | 6400K | 本地音乐/缓存/日志 |

说明：分区表一次定稿，app 双槽位为后续 OTA 预留（当前版本不实现 OTA 代码）；尾部预留 64KB。

### 9.2 内存预算

| 资源 | 分配 |
|---|---|
| 内部 RAM | WS ~24KB、I2S DMA、任务栈、编码器状态；**TLS 记录缓冲与握手堆自 X3-F 起放 PSRAM**；设计目标（X3-E）：静态 DIRAM + 堆峰值 ≤ 440KB，最坏场景（wss 握手期）最低空闲 ≥ 30KB |
| PSRAM | TLS 记录缓冲 8K+2K 与握手堆（~26KB 实测）、AFE 缓冲 ~200KB、播放 ring 1MB（三通道合计）、HTTP 缓冲 128KB、解码器状态、预留 4MB+ |
| Flash | app 2×3M（一槽使用）、model 1.5M、assets 2M、storage 6.25M |

> T1.4 实测（见 STATUS §5 基线）：`ws://` 连接时内部堆峰值 209,252 B；`wss`/TLS 握手期峰值 236,544 B、**最低水位 27,655 B**（略低于上面「保留 ≥30KB」的目标）。
> WS 发送队列 8KB 已按约定放 PSRAM。
> **X3-E 口径定稿（2026-10-31）**：内部 RAM 预算统一为「静态 DIRAM（`idf.py size`）+ 运行期堆峰值（`sys heap` 峰值占用）≤ 440KB」，并要求最坏场景（wss 握手期）`sys heap` 最低水位 ≥ 30KB；旧口径「峰值 ≤310KB」写于 P0（未含 WiFi 栈、且只统计堆），作废。X3-F 后实测 193,250 + 216,668 = **409,918 B**、wss 最低水位 47,051~52,839 B，余量约 40.6KB / 17~23KB，留给 P2。
> **X3-F 实测（2026-10-31，见 [PRODUCT_IDEAS §8 X3](PRODUCT_IDEAS.md) 与 STATUS §5）**：`CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC=y`（只把 mbedTLS 的堆放 PSRAM，WiFi/lwIP 驱动与数据路径不变，保时延）后，wss 握手期内部堆峰值 238,448→**216,668 B**、最低水位 25,087→**47,051~52,839 B**、最大块 30,720→31,744 B；PSRAM 占用 +25,656 B；TLS 握手耗时、boot→NET_UP、RTT 均无劣化。剩余 X3 项：C（WiFi 动态缓冲裁剪）待 P2 流式实测后判断；E 口径已按上文拍板。
> **T2.2 实测（2026-10-31，见 STATUS §5）**：加入 libopus 定点编码器后，静态 DIRAM **196,462 B**；`ws://` 内部堆峰值 208,012 B、最低水位 52,675 B；**wss 握手期峰值 220,784 B、最低水位 39,763 B** → 静态 + 堆峰值 = **417,246 B ≤ 440KB（余 33.3KB）**，最低水位高于 30KB 线 9.7KB。PSRAM 峰值 902,676 B（编码器状态 15,364 + 伪栈 120,000 + 上行 ring 64KB + dump 探针 25,600 等）。
> **T2.5 实测（2026-10-31，见 STATUS §5）**：加入 `voice_cloud`（`vc` 任务 8KB）后，静态 DIRAM **196,670 B**（+208 B）；`ws://` 内部堆峰值 223,620 B / 最低水位 38,451 B；**wss 握手期（公网 `wss://`）峰值 227,988 B、最低水位 32,339 B、最大块 31,744 B** → 静态 + 堆峰值 = **424,658 B ≤ 440KB（余 25.9KB）**，wss 最低水位高于 30KB 线 2.3KB（预计 ~31KB，实测 32.3KB，符合预期但余量不大）。PSRAM 峰值 902,908 B；`vc` 任务栈 8KB，编码/上行轮次中的历史最低剩余 3,212 B（IDLE 时 7,436 B）。**T2.3/T2.4 加 MP3 解码任务栈与解码缓冲后需再按本口径复核。**
> **T2.3 实测（2026-10-31，见 STATUS §5）**：加入 minimp3（解码状态/临时区/PCM 缓冲全 PSRAM）后，静态 DIRAM **196,782 B（+112 B）**、`player` 任务栈仍 6KB（MP3 解码后余 3,672 B，补丁把 16.3KB 临时区从栈移到 PSRAM）；`ws://` 内部堆最低水位 36,535 B；**wss 握手期（公网 `wss://echo.websocket.org/.ws`，2 次）峰值 227,552~227,724 B、最低水位 32,471~32,643 B** → 静态 + 堆峰值 = **424,506 B ≤ 440KB（余 26,054 B）**，wss 最低水位高于 30KB 线 2.5~2.6KB。**PSRAM 峰值抬到 2,895,336 B**：MP3 PCM 预分配 1.5MB + 解码状态 27,532 B + `player load` 上传探针 512KB（仅开发期）；常驻（不含上传探针）2,395,008 B。T2.4 改流式后可下调 PCM 预分配。
> **T2.4 实测（2026-10-31，见 STATUS §5）**：加入流式播放（ring 64KB + stage 6.9KB + 收尾环 9.6KB + ring 读暂存 2KB 全 PSRAM）后，静态 DIRAM **196,998 B（+216 B，全为状态/计数，无新缓冲）**、`player` 栈仍 6KB（流式播放中余 3,544 B）；**wss 握手期（公网 echo，2 次）内部堆峰值 227,432~227,480 B、最低水位 32,671~32,719 B** → 静态 + 堆峰值 = **424,478 B ≤ 440KB（余 26,082 B）**，wss 最低水位高于 30KB 线 **1.95~2.05KB**；**PSRAM 峰值降到 1,446,724 B**（PCM 上限 1.5MB→512KB，常驻不含开发期上传探针）；无新增任务。**注意**：水位余量已不足 2KB，后续任务不得再增加内部 RAM 静态/堆占用。
> **T3.3 实测（2026-11-01，见 STATUS §5）**：加入三通道混音（3×8192 帧 ring、TTS 泵中转、探针抓取与混音统计）后，静态 DIRAM **199,046 B（+1,720 B）**、**无新增任务**（混音泵/解码/I2S 仍在 `player` 任务内串行，`player` 栈 6KB 不变）；**wss 握手期（公网 `wss://echo.websocket.org/.ws`，idle）内部堆峰值 224,092 B、最低水位 33,711 B** → 静态 + 堆峰值 = **423,138 B ≤ 440KB（余 26.1KB）**，水位高于 30KB 线 2,991 B；`ws://` + 真实语音轮（voice_cloud_test）峰值 226,296 B、最低水位 **31,571 B**（≥30KB，余 851 B，是当前最紧口径）→ 静态+峰值 **425,342 B**。PSRAM 峰值 **1,596,388 B**（三通道 ring 3×16KB + TTS 中转 960 B + 探针抓取 32KB 首次使用时分配；收尾环 9.6KB 随 T3.3 移除）；任务数 17 不变。**余量提醒**：`ws://` 水位只剩 851 B，下一任务仍不得新增内部 RAM 静态/堆/任务栈；wss 口径 T5.3 24h 压测再复测。
> **T3.1 实测（2026-11-01，见 STATUS §5）**：加入会话状态机 `voice_session` 后，静态 DIRAM **197,094 B（+96 B，状态/统计变量）**、**无新增任务/任务栈**（状态机事件驱动，见 §9.3）；`ws://` 内部堆峰值 **227,736 B**、最低水位 **31,891~32,971 B**（`tts_stream`/`voice_cloud`/e2e 三脚本口径）→ 静态 + 堆峰值 = **424,830 B ≤ 440KB（余 25,888 B）**，`ws://` 最低水位仍 ≥ 30KB。**wss 握手期未复测**：本次没有新增任务/缓冲，wss 路径代码未变，按 T2.4 的 32,671~32,719 B 减去 ~100B 估算仍在 30KB 以上，T5.3 24h 压测时复测入基线。
> **T3.7 实测（2026-11-01，见 STATUS §5）**：加入连续对话（FOLLOW_UP 状态 + 一个一次性 `esp_timer` `vs_fup` + 确认/统计字段）后，静态 DIRAM **199,094 B（+48 B vs T3.3，无新任务）**；`ws://` + 真实语音轮（`voice_cloud_test`）内部堆最低水位 **32,983 B（≥30KB）**、峰值占用约 226.5KB → 静态+峰值约 **425.6KB ≤440KB**；新增 esp_timer 约百余字节内部堆（含在峰值内）。**wss 握手期（公网 echo，idle）内部堆峰值 224,604 B、最低水位 33,271 B（≥30KB）** → 静态 + 峰值 = 423,698 B ≤440KB；PSRAM 峰值 1,486,944 B。

> **T3.4 实测（2026-11-01，见 STATUS §5）**：加入 `dev_ctrl`（按键扫描/LED/静音）后，静态 DIRAM **199,370 B（+276 B vs T3.7，无新任务）**；`ws://` + 真实语音轮（`voice_cloud_test`）内部堆峰值 225,688 B、**最低水位 32,579 B（≥30KB，余 2,579 B）** → 静态 + 峰值 = **425,058 B ≤440KB（余 25.5KB）**；PSRAM 峰值 1,596,456 B；任务数 17（连上 WS 时，`ws_client` 在连；断链时显示 16，与 T3.7 一致）。**关键取舍**：LED 驱动**没有用 `esp_driver_rmt`** —— 实测 RMT 会把 ~2.2KB 塞进 IRAM 且运行时还要分配通道/symbol 缓冲（合计 ~3.4KB 内部池），会把水位压到 30KB 线以下（本任务先按 RMT 实现并实测确认后才改的 bit-bang）；现方案静态只花 276 B。**2026-11-02 该 LED 驱动已整体移除**（硬件解码不可靠，见 PENDING PD-003），静态 RAM 进一步下降，`dev_ctrl` 只保留按键+静音。**wss 握手期未复测**（代码路径未变，按 T3.7 的 33,271 B 减 ~0.4KB 估算仍有 ~32.9KB），T5.3 复测。

> **T3.6 实测（2026-11-02，见 STATUS §5）**：新增 `components/feedback`（异常反馈路由，**不建任务/不定时器**）与 `assets` 提示音表后，静态 DIRAM **199,286 B（+144 B vs T3.4 收尾，全为状态/统计/资源表）**；`ws://` 真实语音轮（`voice_cloud_test`）内部堆峰值 **224,924 B**、**最低水位 32,679 B（≥30KB，余 2,679 B）** → 静态 + 峰值 = **424,210 B ≤ 440KB（余约 26KB）**；PSRAM 峰值 **1,596,348 B**（未新增缓冲；提示音 PCM 走 flash rodata 共 ~177KB，**不进 RAM**）；无新增任务；`vs_err` 看门狗复用一次性 `esp_timer`（`CONFIG_VS_ERROR_HOLD_MS=3000`）。
> **Flash 增量**：bin **2,118,464 B（+163,160 B vs T3.4 收尾）**——4 条中文提示音 PCM 占 ~177KB rodata（app 分区 3M，余量仍 ~1MB）；T3.5 迁 `assets` 分区后这部分从 app 移除。**wss 握手期水位 T3.6 未复测**（无新增任务、静态 +144 B、多一个一次 esp_timer，按 T3.7 的 33,271 B 估算 ~32.2KB），归 T5.3。

> **T3.5 实测（2026-11-02，见 STATUS §5）**：提示音迁 `assets` 分区（自定义只读 pack + `esp_partition_mmap` 零拷贝）后，app bin **1,941,840 B（−176,656 B vs T3.6 的 2,118,496 B）**、Flash Data 516,940→337,596 B（−179,344 B，PCM 不再编进 app）；静态 DIRAM **199,766 B（+480 B：`prompt_store` 状态/表 + `diag prompt info`）**、无新增任务；提示音 PCM 常驻 `assets` 分区、**不占 RAM**（mmap 只占 MMU 页）；pack 缺失/损坏时降级不崩溃。**wss 水位未复测**，T5.3 归口。

> **T4.2 构建口径（2026-11-10，未真机）**：新增 `components/music_cloud`（L3，不建任务）+ `diag` 的 `mc` 命令 + `voice_cloud` 音乐事件跳过后，`idf.py build` 通过（零告警）；bin **1,982,880 B**（+7,984 B vs T4.1 1,974,896 B）、总镜像 **1,982,760 B**、**静态 DIRAM 201,046 B（+784 B vs T4.1）**；无新任务、无堆分配（两个 esp_timer + 定长静态缓冲），PSRAM 零增量。播放期内部 RAM 预计与 T4.1 持平；**真机待板（PENDING PV-013）**。
> **T4.1–T4.3 真机实测（2026-11-10，见 STATUS §5）**：bin **1,993,664 B**、总镜像 **1,993,540 B**、Flash Code 1,499,206 / Data 349,492、**静态 DIRAM 200,870 B**（4 个修复后与 T4.3 构建口径持平）。**音乐播放期内部堆峰值 231,544 B、最低水位 24,563 B**（`ws://`，44.1k/2ch）→ 静态+峰值 **432,414 B ≤440KB（余 ~7.6KB）**；**水位低于 30KB 线**（`music_http` 6KB 栈 + HTTP 缓冲），停止后回 52,915 B；PSRAM 峰值 **1,633,992 B**。任务：播放期 17（+`music_http`），停止后回基线；无新任务、无堆分配。**wss/音乐并发水位归 T5.3**。

> **T4.3 构建口径（2026-11-10，未真机）**：`music_cloud` 瘦身为纯协议 + 新增 `music_session`（L4，不建任务）后，`idf.py build` 通过（自有代码零告警）；bin **1,993,152 B**（+10,272 B vs T4.2 的 1,982,880 B）、总镜像 **1,993,028 B**、**静态 DIRAM 200,870 B（−176 B vs T4.2 的 201,046 B）**——`music_cloud` 删掉播放状态/重试/定时器释放的约 0.6KB 抵消了 `music_session` 的状态/URL/标题缓冲。**无新任务**（`music_cloud` 的 2 个 esp_timer 迁到 `music_session`，总数不变）、**无堆/PSRAM 分配**（暂停复用已有 ring）。内部堆峰值与水位**未复测**（无板，PV-014）：按 T3.5 口径估算静态+峰值约 **426KB ≤440KB**，`ws://` 水位约 32.5KB 量级，**wss 与水位的正式复测归 T5.3**。播放期内部 RAM 预计与 T4.1/T4.2 持平（`music_http` 6KB 栈 + HTTP 缓冲仅在播放期）。

### 9.3 任务与核心（确定）

| 任务 | 核 | 优先级 | 栈 | 职责 |
|---|---|---|---|---|
| `afe_feed` | 1 | 8 | 4KB | I2S 读 + AFE feed（实时性最高） |
| `afe_fetch` | 1 | 6 | 4KB | 唤醒/VAD/上行音频输出 |
| `player` | 1 | 5 | 6KB | MP3 解码（minimp3，状态/临时区在 PSRAM）+ **流式解码/欠载补静音（T2.4）** + **三通道混音/音量 20ms 插值/-1dBFS 软限幅/通道 duck 与淡入淡出（T3.3）** + 重采样 + I2S 写（不新增任务，混音泵与解码在同一任务内串行） |
| `evt` | 0 | 5 | 4KB | 事件分发（`event_bus` 自建 esp_event 循环，T1.2a 起） |
| `ws` | 0 | 5 | 6KB | WS 状态机/退避/心跳/发送队列（TLS 收发在库任务 `ws_client`） |
| `vc` | 0 | 4 | 8KB | 上行 Opus 编码 + 发送、`audio_start/audio_end`、下行事件解析与 `sid` 过滤、**下行 0x02 → `audio_player` 流式播放 + R1/R2（T2.4）**（编码在本任务内串行，伪栈在 PSRAM，见 §3.6） |
| `music_http` | 0 | 4 | 6KB | **T4.1 起按需创建/销毁**（只在播放期间存在）：HTTP(S) 拉流 → 独立 minimp3 实例 → 重采样 48k → `CH_MUSIC` PCM ring；解码/HTTP 不在 `player` 任务里，停止后任务栈释放；**T4.3 起支持暂停/继续**：暂停时任务不退出、不读 socket（ring 满靠 TCP 背压，不丢数据），继续后从暂停点接着拉 |
| `diag` | 0 | 1 | 8KB | console + 周期监控（T2.2：`enc test` 在 REPL 任务里调 libopus，实测 4KB 会栈溢出 → 8KB） |

原则：核 1 只跑音频路径，不受网络抖动影响；跨核只通过队列/ringbuffer。

> 上表是**应用任务**；IDF 库任务（`sys_evt`、`wifi`、`esp_timer`、`Tmr Svc`、`IDLE`、`ipc`）不在此表。
> `transport_ws` 的 `esp_websocket_client` 另建一个库任务 `ws_client`（核 0 / 优先级 5 / 6KB，T1.4 起），收包回调在它上面执行。
> `voice_session`（T3.1）**不建任务**：状态机事件驱动，handler 分别跑在 `evt`、`ws_client`、`player` 完成回调与 console/timer 上下文。原计划的 4KB 任务会击穿内部 RAM 水位（T2.4 实测 wss 握手期最低只剩 32.7KB，CONVENTIONS §4 要求 ≥30KB），故不采用；状态迁移是纯内存操作，不需要独立任务上下文。
> `dev_ctrl`（T3.4）**不建任务**：上表原计划的 `dev`（3KB）改为一个 10ms `esp_timer` 做按键扫描（LED 闪烁定时器随 LED 功能于 2026-11-02 一并移除）。内部 RAM 水位余量不足 1KB（见 §9.2），`dev` 任务栈 3KB 会直接击穿「wss 握手期 ≥30KB」；扫描/闪烁都是整数状态机 + 一次非阻塞 GPIO 写，放在 timer 任务上下文足够。
> `music_cloud`（T4.2 起）与 `music_session`（T4.3 起）**都不建任务**：下行解析在 `ws_client` 任务（transport_ws 附加订阅槽，与 `voice_cloud` 的主回调、`ws rxlog` 并存）；T4.3 把播放状态机/起播重试/周期上报整体移入 `music_session`，两个 `esp_timer` 也跟着从 `music_cloud` 迁过去（**定时器数量不增**）；`music_cloud` 只剩解析 + `mid` 过滤 + JSON 收发。内部 RAM 增量见 §9.2（不新增堆/任务栈）。
> `feedback`（T3.6）**不建任务、不定时器**：`fb_notify()` 在调用方上下文（`evt`/`ws_client`/`vc`/console）投递语义事件并请求 `audio_player` 播放提示音；TTS 流占用时的补播由 `player` 完成回调驱动；`voice_session` 侧的收尾看门狗复用 T3.1 既有的一次性 `esp_timer`（`vs_err`，`CONFIG_VS_ERROR_HOLD_MS` 默认 3000ms）。
> `net_mgr` 的 WiFi/IP 事件处理器运行在 `sys_evt` 任务上，其栈由 `CONFIG_ESP_SYSTEM_EVENT_TASK_STACK_SIZE=4096` 提供（P1 T1.3 实测默认 2304 B 时只剩 380 B，已加栈）。

---

## 10. 网络与调试（确定）

### 10.1 WiFi

- STA 模式，SSID/密码走 Kconfig（`CONFIG_AI_WIFI_SSID` / `CONFIG_AI_WIFI_PASSWORD`），编译进固件；驱动存储用 `WIFI_STORAGE_RAM`，避免 NVS 里旧凭证覆盖配置；
- 上电自动连接，失败重试；运行中断线自动重连（退避 1s→30s，无限重试）；
- 关闭 modem sleep（`WIFI_PS_NONE`）：语音要低时延，避免省电引入上下行抖动；
- 开启 SNTP（服务器 `CONFIG_NET_SNTP_SERVER`，默认 `ntp.aliyun.com`），同步时间用于 TLS 证书校验；**联网成功（GOT_IP）时立即触发对时**，并关闭 lwIP 首次请求的随机启动延迟（`CONFIG_LWIP_SNTP_STARTUP_DELAY=n`），实测对时落在地址就绪后 ~40ms（改前最坏 33s）；
- 未配置 SSID 时 `net_mgr` 降级为离线（只 WARN），本地唤醒/播放不受影响，`transport_ws` 会因 `net_is_up()` 为 false 而拒绝启动；
- WiFi 与 PSRAM 同时使用，注意 `CONFIG_ESP32S3_DATA_CACHE_64KB` 已满足（现有配置）。

### 10.2 调试能力（diag + esp_console）

串口 115200（UART0，GPIO43/44），`idf.py monitor` 直接可用。确定命令集：

| 命令 | 作用 |
|---|---|
| `help` | 列出所有命令 |
| `sys info` | 版本、复位原因、运行时长、内存水位 |
| `sys heap` | 内部/PSRAM 剩余与碎片 |
| `sys tasks` | 任务栈水位、CPU 占用 |
| `sys reboot` | 软复位 |
| `log <tag> <level>` | 运行时调整日志级别 |
| `wifi status` / `wifi reconnect` | 查看/重连 WiFi |
| `ws status` / `ws reconnect` | WebSocket 观测：状态/脱敏端点/握手与重连计数/心跳 RTT/收发送与丢弃计数/队列水位；主动重连（T1.4 提前交付） |
| `ws send <json>` / `ws sendbin [len]` / `ws rxlog on\|off` | WebSocket 双向联调探针（T1.6）：原样发送文本/上行二进制帧、打印下行帧（T2.5 起 rxlog 走附加订阅，与 voice_cloud 并存） |
| `vc status` / `vc start [sid]` / `vc stop` / `vc cancel` / `vc stat` / `vc reset` | 语音云观测与手动起轮（T2.5/T2.4）：链路状态/sid、轮次与上行帧统计、首帧耗时、下行事件与 `sid` 丢弃计数、**下行音频丢弃帧/字节（窗口外/收尾后/播放器不可用）**；手动起一轮、优雅收尾（补尾帧 + audio_end）、立即放弃（同时打断在播 TTS） |
| `vs status` / `vs start` / `vs interrupt` / `vs reset` / `vs followup on\|off` | 会话状态机观测（T3.1/T3.7）：当前状态/sid 与 VC 状态、轮次（开始/完成/异常/中止）、打断/唤醒丢弃/超时/云端 error 计数、迁移次数与上次状态停留、**续听（续听轮/命中/超时退出/上限退出、窗口剩余）**、**错误反馈（T3.6：四类计数、提示音完成/失败、最近 kind/code/时长）**；`vs start` 手动开始一轮（不播应答提示音）、`vs interrupt` 打断当前会话（`vc cancel` + 回 IDLE）、`vs followup on\|off` 运行期开关续听窗口（不持久化） |
| `fb status` / `fb reset` / `fb test <net\|cloud\|timeout\|tts>` | 异常反馈观测/试听（T3.6）：当前活动反馈、通道统计（总播放/完成/失败/延迟/丢弃/中止）、四类计数与最近时长；`fb test` 直接触发一条提示音（不起会话，回板人工验收音色/响度用） |
| `prompt info` | 提示音资源观测（T3.5）：assets pack 是否就绪、模式（flash-mmap / psram-fallback / none）、pack 版本/条目数/字节数/加载耗时/映射地址，以及 5 条提示音的采样点/时长/PC 地址；失败时显示降级原因 |
| `dev status` / `dev mute [on\|off]` / `dev reset` | 物理交互观测（T3.4）：静音状态与当前音量、按键短/长按计数与事件投递失败；`dev mute` 等价长按（静音=音量 0、恢复原音量，不落 NVS）；`dev reset` 清零统计。**LED 状态指示已于 2026-11-02 移除**（见 PENDING PD-003） |
| `player stat` / `player reset` / `player mp3` / `player pcm <off> <n>` / `player load begin\|chunk\|<base64>\|end` / `player stream [chunk] [gap_ms]` / `player stream abort` / **`player mix stat\|reset\|vol\|tone\|stop\|cap\|dump`** | 播放探针（T2.3/T2.4/T3.3，`CONFIG_DIAG_ENABLE_PLAYER_TEST` 仅开发期）：base64 分块上传素材到 PSRAM；`player mp3` 整段解码播放；`player stream` 把上传素材按 chunk 喂流式路径（`gap_ms` 制造欠载）、`player stream abort` 打断；`player stat` 追加流式统计行（含 `first_sound_us`/`underrun`/`dur_ms`）；`player pcm` 取回整段解码 PCM 对拍；**`player mix` 是三通道混音探针：`tone <CH> <freq> <ms> [amp]` 往通道塞正弦音调（可叠加）、`stat` 看 `MIXSTAT/MIXCH`（音量/限幅/峰值/通道 gain/水位/抓取状态）、`vol` 运行时改音量、`cap <n>` + `dump <off> <n>` 把混音输出（48k 单声道）抓回 PC 做 FFT/包络断言、`stop` 停探针**（tools/mixer_test.py 的验收通道） |
| `net ping <host>` | 网络连通性 |
| `mic stat` | 麦克风 RMS/峰值、VAD 状态 |
| `enc test [ms] [tone\|uplink]` / `enc stat` / `enc dump on\|off` / `enc reset` | 上行 Opus 编码观测（T2.2）：tone=1kHz 正弦、uplink=真实链路（自动排空 ring 积压）；输出帧数/字节/码率/单帧耗时/内存增量；dump 最多 50 帧 hex 供 PC 侧独立解码 |
| `cfg info` / `cfg get\|set\|setstr\|erase <key>` | 配置持久化调试：NVS 条目用量、原始值读写、删除键恢复缺省（存储层语义，不做业务校验） |
| `evt list` / `evt post <name\|id>` / `evt burst <n>` / `evt trace on\|off` | 事件总线观测：事件清单、投递（网络事件带示例 payload）、突发投递验证非阻塞/队列满、订阅打印分发结果 |
| `audio vol <0-100>` | 设置音量 |
| `audio play <file\|url>` | 手测播放 |
| `audio stop` | 停止播放 |
| `voice state` / `voice start` / `voice interrupt` / `voice wake on\|off` | 会话调试 |
| **`music play <http(s)://...>` / `music stop` / `music stat`** | **T4.1 音乐探针**：`music play` 启动 `music_http` 拉流（URL 只接受 http(s)、长度 ≤ Kconfig），`music stat` 打印 `MUSICSTAT`（active/url_active/rate/ring/written/played/underruns/first_sound）与 `MUSICHTTP`（status/content_length/bytes/first_byte/connect/decoded/errors），`music stop` 丢弃 ring + 100ms 淡出；本探针仍**直接调播放器**，暂停/继续/切歌走 `ms` 命令或云端 `music_control` |
| **`mc status` / `mc report` / `mc reset`** | **T4.2/T4.3 音乐协议观测（纯协议层）**：mid/have_mid/最近曲目/URL + 统计（track/control、过期与重复 mid、非法 URL/超长/非 mp3、`music_status` 与 `music_request` 发送成败）；`mc report` 用最近一次快照立即上报一次 `music_status`，`mc reset` 清统计 |
| **`ms status` / `ms play <http(s)://...>` / `ms pause` / `ms resume` / `ms stop` / `ms request play [index]` / `ms reset`** | **T4.3 音乐会话观测/控制**：状态机（idle/playing/paused/stopped/error）、mid/进度/时长/标题/URL/**暂停原因（用户/语音抢占）** + 统计（起播/切歌/暂停/恢复/自动暂停/EOF/错误/resume 失败兜底/music_request 发送）；`ms pause` = 用户暂停 latch（会话结束不自动恢复），`ms request play [0]` 上行 `music_request` 探针 |

日志 TAG 固定：`BOARD`、`AUDIO_IO`、`AFE`、`PLAYER`、`ENC`、`NET`、`WS`、`VC`、`VS`、`MC`、`MS`、`DEV`、`FB`（T3.6 异常反馈）、`DIAG`、`CFG`、`EVT`、`APP`（`APP` 属 `main/` 启动编排，`EVT` 属 `event_bus` 基础服务，均不占业务层级）。

---

## 附录 A：暂缓功能与架构预留点

以下功能当前版本不实现，架构已预留接入点，后续实现时按本文档对应章节扩展即可。各项评估见 [PRODUCT_IDEAS.md](PRODUCT_IDEAS.md)。

| 功能 | 架构预留点 |
|---|---|
| **配网**（BLE/SoftAP） | `net_mgr` 的凭证来源抽象为 `wifi_cred_provider`；当前实现为 Kconfig 常量 |
| **OTA** | 分区表已含 `ota_0/ota_1/otadata`；`ota_svc` 组件未创建 |
| **屏幕 / UI** | 未纳入；可新增 `ui` 组件（LVGL），消费 `event_bus` 事件 |
| **定制唤醒词** | `audio_fe` 模型名走 Kconfig（当前固定 `wn9_hilexin`） |
| **音乐源接入** | `audio_player` 的 `source_http`（T4.1）与 `music_cloud`（T4.2）已交付；扩展点：T4.3 `music_session`（暂停/继续/切歌/`music_request`）、T4.5 播放稳定性（seek/Range/断流重连） |
| **AEC / 语音打断** | AFE 支持 `"MR"`/`"MMR"` 显式参考通道；`audio_fe` 预留参考通道拼接入口；样板可加第二颗 INMP441 |
| ~~**SD 卡本地音乐**~~ | **不做**（2026-11-10 用户决定：音乐只播远端 URL；`storage` 分区保留给缓存/日志，不作为曲库） |
| **电源 / 电池管理** | 预留 `power_mgr` 组件位置与 `BAT_ADC` 引脚（GPIO1） |
| **离线命令词** | model 分区有 1.5MB 余量，可加 MultiNet 模型 |

## 附录 B：待定输入（不阻塞端侧开发）

| 事项 | 现状 | 影响范围 |
|---|---|---|
| 音乐源形态（曲库直链） | 端侧只支持远端 URL（2026-11-10 决定）；曲库/授权只在服务端 | 仅 `music_cloud` 请求字段与资源准备 |
| 云端服务端实现 | 端侧契约已定（第 6 节），服务端按契约实现 | 无端侧改动 |
| 设备鉴权方式 | 当前用 Kconfig 写死的 `device_id` + `token` | 后续可换成动态下发，仅改 `config_store` 与握手 |
