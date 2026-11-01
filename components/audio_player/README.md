# audio_player —— 播放引擎（L2 音频引擎层）

**作用**：把提示音 PCM、整段 MP3（本地/探针）与**流式 MP3**（TTS 下行）分别送进 **`CH_PROMPT` / `CH_MUSIC` / `CH_TTS` 三个通道**，混音成 48kHz 立体声输出；负责解码、重采样、混音、音量、淡入淡出、duck、软限幅、欠载补静音、尾音处理，并在播放结束后通知业务层（T3.3 起三通道混音）。

## 当前能力

- 独立播放任务（核 1、优先级 5、栈 6KB），任务队列深度 4；不新增任务；
- **三通道混音（T3.3）**：`CH_TTS`（TTS 流）/`CH_PROMPT`（提示音）/`CH_MUSIC`（整段 MP3，
  将来音乐源）各一个 **8192 帧（170ms）PSRAM ring**；混音级每 10ms 从三路取数相加（int32，
  最大 ±98301，中间不截断）→ 音量（q16，20ms 线性插值）→ **-1dBFS 软限幅**（膝点 26276、
  上限 29196，有理函数渐近、无硬削波）→ R4 淡出 → I2S；`clip` 计数恒 0 用于自证无削波；
- **通道增益/优先级**：TTS 恒 0dB；`CH_PROMPT` 在 TTS 活跃时让位到 **-6dB**（q16=32768）；
  `CH_MUSIC` 在语音（TTS 或提示音）活跃或 `set_duck(true)` 时 duck 到 **-12dB**（q16=16384）。
  斜坡**目标变化时按 100ms 线性重算步进**、块内逐样本推进（若每块按剩余差重算会变成指数逼近，
  真机实测 240ms 才到位，三通道幅度断言会偏高）；
- 通道起播 10ms 淡入；源耗尽（`closed`）且残留 ≤100ms 时做 100ms 淡出，播空即退出；
- **PCM 播放**：线性重采样到 48kHz（如提示音 16k → 48k），单声道复制到 L/R；
- **整段 MP3（T2.3）**：[minimp3](third_party/minimp3/README.md)（CC0）解码 → 双声道取 (L+R)/2 → 单声道 PSRAM 缓冲 → 同一条重采样/音量/淡入淡出链路；
- **流式 MP3（T2.4，TTS）**：下行 0x02 字节写入 PSRAM ring，player 任务边收边解；
  - 起播：解出 ≥`PREROLL_FRAMES` 帧（**默认 4 帧，24k 约 96ms**；覆盖 I2S DMA 预填 ~40ms 后仍留 ~56ms 抗 WiFi/调度抖动）**或**距首个 write ≥`PREROLL_MS`（默认 120ms），取先到者；此前 I2S TX 不出声；起播 10ms 淡入；
  - 跨帧/跨块**连续重采样**：游标 `(整数位置, 分数)` + 上一块末样本 carry，帧边界不重复淡入淡出、无缝隙；
  - 欠载：ring 空但流未结束 → 输出静音继续走 I2S 时钟，每次连续欠载计 1 次 `underruns` 并限频 WARN；恢复后继续解码（不重置解码器）；
  - 流中帧参数（采样率/声道）变化 → abort + WARN、回调 `ok=false`；
  - `stream_end`：排空已收数据、解码器无更多数据后标记通道 `closed`，通道包络对残留 ≤100ms 做线性淡出 → 静音冲刷/关 TX → 300ms 尾部延时 → 回调 `ok=true`（T3.3 起不再用独立"收尾环"，淡出由混音通道统一施加）；`decoded_frames==0` 时不使能 I2S、WARN、立即回调 `ok=false`；
  - `stream_abort`：≤20ms 内停止写音频并关 TX、丢弃 ring 剩余、回调 `ok=false`；
  - 流式 active 期间拒绝 `play_pcm`/`play_mp3`，`is_busy()` 返回 true；
- 软件音量 0~100（MAX98357A 无数字音量，增益由 SD 电阻固定）：混音后、限幅前施加，20ms 插值；
- 淡入 10ms / 淡出 100ms；软限幅 -1dBFS（见上）；
- 播放结束：静音冲刷 DMA + 150ms 延时 + 关 TX；完成回调带 300ms 尾部延时（半双工规则 R2）；
- 完成回调：主槽（`register_done_cb`，覆盖语义）+ 3 个附加订阅槽（`subscribe/unsubscribe_done`），按主槽→附加槽顺序分发，参数 `(src, ok, arg)`；流式/打断/失败都回调；
- 播放/解码/流式统计（`audio_player_stats_read()` / `audio_player_stream_stats_read()`），供 `diag` 的 `player stat` 观测。

## 对外接口

| 接口 | 说明 |
|---|---|
| `audio_player_init()` | 初始化队列/播放任务；预分配 MP3 解码状态、PCM 缓冲与流式 ring/stage/收尾环（全部 PSRAM） |
| `audio_player_register_done_cb(cb, arg)` | 注册完成回调主槽（player 任务上下文，勿耗时） |
| `audio_player_subscribe_done(cb, arg)` | 追加完成回调槽（≤3，空槽；满返回 `ESP_ERR_NO_MEM`） |
| `audio_player_unsubscribe_done(cb)` | 移除附加槽；未注册返回 `ESP_ERR_NOT_FOUND` |
| `audio_player_play_pcm(pcm, samples, rate)` | 播放一段 PCM（调用方保证数据生命周期；流式 active 时拒绝） |
| `audio_player_play_mp3(mp3, len)` | 整段 MP3：播放任务内解码后播（同一时刻只允许一段；流式 active 时拒绝） |
| `audio_player_mp3_ready()` | MP3 能力是否可用（PSRAM 分配失败时降级，提示音/上行链路不受影响） |
| `audio_player_stream_open(codec, sr)` | 开一轮流式播放（codec=1 为 MP3）；前置：MP3 可用 + 播放器空闲；失败不占用播放器 |
| `audio_player_stream_write(data, len)` | 追加下行 MP3 字节（单次 ≤2KB）；ring 满丢弃整段 + 计数 + 限频 WARN 并返回 `ESP_ERR_NO_MEM`；未 open/已 end 返回 `ESP_ERR_INVALID_STATE` |
| `audio_player_stream_end()` | tts_end：排空 + 100ms 淡出 + 静音冲刷 + 关 TX，回调 `ok=true` |
| `audio_player_stream_abort()` | 打断/error/断链/新轮次：立刻停声、丢弃 ring、回调 `ok=false` |
| `audio_player_stream_active()` | 是否流式播放中（含收尾） |
| `audio_player_stream_stats_read(out)` | 流式统计（见下） |
| `audio_player_is_busy()` | 是否正在播放（含解码阶段与排队；流式期间为 true） |
| `audio_player_fade_out_and_stop(ms)` | **R4 打断（T3.2）**：对当前任务（PCM/整段 MP3/流式）线性淡出到静音后停播，回调 `ok=false`；实测 `last_stop_ms` 79~95ms（80ms 淡出） |
| `audio_player_set_duck(on)` / `get_duck()` | **R3 duck**：**T3.3 起作用于 `CH_MUSIC` 通道增益**（`-AUDIO_PLAYER_DUCK_DB` 默认 12dB，q16=16384），100ms 线性斜坡；不改 `s_volume`，关掉即恢复；语音活跃时自动 duck |
| `audio_player_current_src(&src)` | 当前正在播的任务类型（忙时有效）；R3 判断"音乐/本地长播 vs 提示音/TTS"用 |
| `audio_player_set_volume(v)` / `get_volume()` | 软件音量 0~100 |
| `audio_player_stats_read(out)` / `stats_reset()` | 播放统计 + 最近一次整段 MP3 解码统计；`stats_reset()` 同时清空流式统计 |
| `audio_player_mp3_pcm_read(dst, off, n)` | 开发期探针：读取最近一次整段解码 PCM，供 PC 侧与 FFmpeg 逐样本对拍 |
| `audio_player_mix_stats_read(out)` / `mix_stats_reset()` | 三通道混音统计（`player mix stat`）：音量/插值状态、限幅计数与限幅前峰值、`clip`、`mix_peak`、单块耗时、三通道 active/gain_q8/水位/峰值 |
| `audio_player_mix_tone(ch, freq, ms, amp)` | 开发期探针：往指定通道塞正弦音调（相位从峰值开始，便于验证 10ms 淡入）；播放器空闲时自动起混音泵，泵在跑时直接叠加 |
| `audio_player_mix_stop()` | 停掉所有探针音调源（真实播放任务不受影响） |
| `audio_player_mix_capture(frames)` / `capture_status(&captured, &requested)` / `capture_read(dst, off, n)` | 开发期探针：从下一个混音输出块开始抓 48k 单声道输出（≤16384 帧，PSRAM，首次用时分配），供 PC 侧 FFT/包络/限幅对拍；`capture_read` 只允许读已抓到的部分（播放提前结束时 `captured < requested`） |

流式统计字段（`audio_player_stream_stats_t`）：`active / fed_frames / fed_bytes / decoded_frames / decoded_samples / rate / channels / ring_level_ms / ring_high_ms / underruns / dropped_bytes / bad_frames / first_byte_to_sound_us / duration_ms`。

## 配置（Kconfig）

| 配置 | 默认 | 说明 |
|---|---|---|
| `AUDIO_PLAYER_DEFAULT_VOLUME` | 60 | 上电初值（app 启动后由 NVS/`config_store` 覆盖；当前用户默认 **100**，见 ARCH §3.5） |
| `AUDIO_PLAYER_TAIL_DELAY_MS` | 300 | 完成回调前的尾部延时（R2）；打断/失败不延时 |
| `AUDIO_PLAYER_ENABLE_MP3` | y | 启用 MP3 解码（关闭后 `play_mp3`/流式返回不可用，提示音链路不受影响，固件约省 40KB Flash） |
| `AUDIO_PLAYER_MP3_PCM_MAX_BYTES` | 524288（512KB） | **整段**解码路径 PCM 缓冲上限（PSRAM 预分配；流式不走这里）。512KB ≈ 24k 单声道 10.9s；T2.3 的 `mp3_test.py` 48/48 在 512KB 下仍通过 |
| `AUDIO_PLAYER_STREAM_RING_BYTES` | 65536 | 流式压缩数据 ring（PSRAM）；64KB ≈ 24k/64kbps 约 8s |
| `AUDIO_PLAYER_STREAM_PREROLL_FRAMES` | 4 | 起播最少已解码帧数（96ms；2/3 帧实测偶发起播欠载） |
| `AUDIO_PLAYER_STREAM_PREROLL_MS` | 120 | 起播超时（距首个 write） |
| `AUDIO_PLAYER_INTERRUPT_FADE_MS` | 80 | R4 打断淡出时长；P3 验收"按键打断 <100ms 停声"，改大必然超线（实测 last_stop_ms 79~95） |
| `AUDIO_PLAYER_DUCK_DB` | 12 | R3 音乐 duck 深度（q8 目标 ≈ 0.25 倍振幅） |
| `AUDIO_PLAYER_DUCK_RAMP_MS` | 100 | duck 斜坡时长（R3：100ms 内到位） |

## 依赖

`audio_io`（写 I2S）、`esp_timer`（解码/播放耗时统计）；MP3 解码器源码在 `third_party/minimp3/`（vendored，不依赖组件管理器）。

## 内存与实时约束（CONVENTIONS §3/§4）

- I2S 输出块（480 帧 ×2ch ×2B）在**内部 RAM**（DMA 需要），播放任务内不做动态分配；
- **PSRAM**：MP3 解码状态 38,020 B（minimp3 状态/临时区/单帧/单声道帧/pending 8KB）+ 整段 PCM 512KB + 流式 ring 64KB + 起播 stage 6,912 B + ring 读取暂存 2KB + TTS 泵中转 960 B + **三通道 ring 3×16KB** + 探针抓取 32KB（首次用时分配）；
- **内部 RAM**：T3.3 新增静态约 +1.5KB（三通道状态/混音统计/流式上下文瘦身），`player` 任务栈保持 6KB、不新增任务；实测 `sys tasks` 播放中栈余约 3.5KB；内部 RAM 预算见 CONVENTIONS §4；
- 音频任务内禁止 malloc/网络/Flash/阻塞；水位统计与 ring 拷贝用临界区（单次 memcpy ≤2KB）；
- minimp3 上游把 16.3KB 临时区放任务栈（6KB 栈必爆），本项目用 3 处补丁改成调用方注册（见 `third_party/minimp3/README.md`）；
- 流式解码用**自解析帧头求帧长**，凑满整帧才交给 minimp3：minimp3 在"数据不足"分支会 memset 解码器状态（丢比特池），每块都踩会让部分帧解出噪声；坏数据/丢块后重新找同步字并限频 WARN；
- 播放期 CPU：24k 单声道 1.77 ms/帧（帧长 24ms）、44.1k 立体声 4.45 ms/帧（帧长 26.1ms），可实时；重采样/音量/淡入淡出在 48k 逐样本计算。

## `player` 探针（`CONFIG_DIAG_ENABLE_PLAYER_TEST`，仅开发期）

```
player load begin → 分块 player load <base64>（≤240 字符/行）→ player load end
player mp3                     # 整段解码播放（T2.3）
player stream [chunk] [gap]    # T2.4：把已上传 MP3 按 chunk 喂流式路径，gap_ms>0 制造欠载（默认 512 0）
player stream abort            # 流式播放中打断（验证回调 ok=false）
player stat                    # 追加 stream: 行（active/fed/.../first_sound_us/dur_ms）
player pcm <off> <n>           # 取回整段解码 PCM 对拍
player reset                   # 清空播放统计 + 流式统计
```

量产必须关闭该选项（上传缓冲 512KB PSRAM、console 行缓冲 1024B 都只在开发期编译）。

## 关键日志（契约 §4，工具/voice_cloud 按此解析）

```
PLAYER: stream open codec=mp3 sr=24000
PLAYER: stream first sound after 14 ms (prebuffer 96 ms)
PLAYER: stream end frames=57 bytes=29088 decoded=202 dur=4850 ms underrun=0 bad=0
PLAYER: stream abort (frames=57, drop 20384 B)
W PLAYER: stream underrun #1 (ring 0 ms)
W PLAYER: stream bad data, resync at byte 399
```

`player stat` 流式行：

```
stream: active=0 fed=57 bytes=29088 decoded=202 samples=116352 rate=24000 ch=1 ring_ms=0 high_ms=4762 underrun=0 dropped=0 bad=0 first_sound_us=15144 dur_ms=4850
```

`player stat` 半双工行（T3.2）：

```
duck: on=0 gain_q8=256 out_peak=15563 stop: last_stop_ms=79 stops=1
```

- `out_peak`：最近 ~320ms（32 × 10ms 块）输出峰值，含 duck/淡出；R3 用它对比 duck 前后（实测 15563 → 3890 = 0.250 → 恢复 15563）；
- `last_stop_ms`：最近一次淡出请求 → 输出静音（不是关 TX 时钟；淡出后还有静音冲刷）；`stops` 为累计次数；
- `player duck on|off` / `player stop [ms]` 是仅开发期探针（`CONFIG_DIAG_ENABLE_PLAYER_TEST`）。

## 后续（P3/P4）

- 多通道：`CH_TTS` / `CH_PROMPT` / `CH_MUSIC` + 混音 + ducking（T3.3 做混音/限幅，T4.4 做音乐 duck 叠加与恢复；T3.2 已把打断淡出与 duck 增益统一放在输出级 `out_process()`，混音接同一位置）；
- 音源抽象：`source_http`（HTTP 流）/`source_file`（FATFS），P4；
- 流式音乐（T4.x）可复用本组件的 stream API（codec=1），只需扩 codec/声道策略。

对应架构：[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §3.2、§3.7、§9.2、§9.3；验收：[docs/STATUS.md](../../docs/STATUS.md) §5。
