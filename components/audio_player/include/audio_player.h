#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* ---------------- 完成回调（T2.4 签名变更） ----------------
 * 回调在 player 任务上下文执行（尾音处理完成后）；ok=false 表示失败/放弃。
 * 分发顺序：主槽（main 用）+ 附加槽 1..3（voice_cloud 等业务用）。
 */
typedef enum {
    AUDIO_PLAYER_SRC_PCM = 0,    /* play_pcm（提示音等） */
    AUDIO_PLAYER_SRC_MP3_FILE,   /* play_mp3（整段解码） */
    AUDIO_PLAYER_SRC_TTS_STREAM, /* 流式（TTS） */
} audio_player_src_t;

typedef void (*audio_player_done_cb_t)(audio_player_src_t src, bool ok, void *arg);

/* 主槽：覆盖语义（main 用） */
void audio_player_register_done_cb(audio_player_done_cb_t cb, void *arg);

/* 附加槽（≤3）：槽满返回 ESP_ERR_NO_MEM；未注册 unsubscribe 返回 ESP_ERR_NOT_FOUND */
esp_err_t audio_player_subscribe_done(audio_player_done_cb_t cb, void *arg);
esp_err_t audio_player_unsubscribe_done(audio_player_done_cb_t cb);

/**
 * @brief 初始化播放引擎（单播放任务，绑核 1）
 */
esp_err_t audio_player_init(void);

/**
 * @brief 播放一段 PCM（16bit 单声道，任意采样率，内部重采样到 48k）
 *
 * @param pcm          PCM 数据（调用方保证生命周期覆盖播放过程）
 * @param samples      采样点数量（单声道）
 * @param sample_rate  源采样率，如 16000
 * @return ESP_OK / ESP_ERR_INVALID_ARG / ESP_ERR_TIMEOUT（队列满）/ ESP_ERR_INVALID_STATE（流式播放中）
 */
esp_err_t audio_player_play_pcm(const int16_t *pcm, size_t samples, uint32_t sample_rate);

/* 是否有播放任务在进行（含队列中的任务；流式播放期间为 true） */
bool audio_player_is_busy(void);

/* ---------------- MP3 解码播放（T2.3，minimp3） ----------------
 * 状态与 PCM 缓冲在 audio_player_init() 里一次性分配到 PSRAM（播放任务内禁止 malloc）；
 * 同一时刻只允许一段 MP3（解码输出复用同一块 PSRAM 缓冲），播放中再调用返回
 * ESP_ERR_INVALID_STATE。解码 + 重采样 + 淡入淡出 + 音量都在播放任务内串行完成。
 */
esp_err_t audio_player_play_mp3(const uint8_t *mp3, size_t len);

/* 是否具备 MP3 能力（PSRAM 分配失败时降级为不可用，提示音/上行链路不受影响） */
bool audio_player_mp3_ready(void);

/* ---------------- 流式播放（T2.4，TTS 下行） ----------------
 * 全部接口可在任意任务调用、均非阻塞：只做 PSRAM ring 拷贝/置标志，
 * 解码与 I2S 输出在 player 任务内串行完成。
 */

/* codec: 1=MP3（当前唯一实现）。open 失败时不占用播放器。
 * 前置：MP3 能力可用、播放器空闲；否则 ESP_ERR_INVALID_STATE。已 active 再 open 同样失败。 */
esp_err_t audio_player_stream_open(uint8_t codec, uint32_t sample_rate);

/* 追加一段下行 MP3 字节（单次 ≤2KB）。ring 满返回 ESP_ERR_NO_MEM（丢弃整段并计数），
 * 未 open / 已 end 返回 ESP_ERR_INVALID_STATE。 */
esp_err_t audio_player_stream_write(const uint8_t *data, size_t len);

/* 云端 tts_end：排空已收数据后收尾（fade-out 100ms + 静音冲刷 + 关 TX），
 * 触发 ok=true 的完成回调（src=TTS_STREAM）。 */
esp_err_t audio_player_stream_end(void);

/* 打断/error/断链/新轮次：立刻停声，丢弃未播数据，回调 ok=false。 */
esp_err_t audio_player_stream_abort(void);

/* ---------------- 半双工打断与 duck（T3.2，规则 R4/R3） ----------------
 *
 * audio_player_fade_out_and_stop()：对"当前正在播的任务"（PCM 提示音 / 整段 MP3 / TTS 流）
 * 在 fade_ms 内线性淡出到静音然后停播，完成回调 ok=false。用于按键打断（R4）：
 * 从请求到输出静音应在 fade_ms+10ms 内（`player stat` 的 last_stop_ms 可断言）。
 * 非阻塞、任意任务可调用；播放器空闲时返回 ESP_ERR_INVALID_STATE。
 * 注意：淡出后仍会做静音冲刷/关 TX（尾部约 100ms 静音，不再有可闻声音）。
 */
esp_err_t audio_player_fade_out_and_stop(uint32_t fade_ms);

/* duck（R3，T3.3 语义）：作用于 CH_MUSIC 通道增益（-CONFIG_AUDIO_PLAYER_DUCK_DB，
 * 默认 -12dB），100ms 线性斜坡；不改 s_volume。语音（TTS/提示音）活跃时也会自动 duck，
 * 本开关用于探针/强制音乐 duck。非阻塞、任意任务可调用。 */
void audio_player_set_duck(bool on);
bool audio_player_get_duck(void);

/* 当前正在播放的任务类型；播放器空闲返回 false（R3 判断"音乐/本地长播 vs 提示音/TTS"用） */
bool audio_player_current_src(audio_player_src_t *out);

bool audio_player_stream_active(void);

/* 流式统计（diag 的 player stat 用；任意任务上下文可读） */
typedef struct {
    bool active;
    uint32_t fed_frames;      /* 成功 write 的 payload 数 */
    uint32_t fed_bytes;
    uint32_t decoded_frames;  /* 成功解出的 MP3 帧 */
    uint32_t decoded_samples; /* 单声道样本数（源采样率） */
    uint32_t rate;            /* 源采样率 */
    uint8_t channels;         /* 源声道数 */
    uint32_t ring_level_ms;   /* 当前 ring 待解码数据近似毫秒 */
    uint32_t ring_high_ms;    /* 本次流最高水位 */
    uint32_t underruns;
    uint32_t dropped_bytes;
    uint32_t bad_frames;
    uint32_t first_byte_to_sound_us; /* 首个 write → 首个 I2S 写；0=未测到 */
    uint32_t duration_ms;     /* 本次流实际播放时长（首 I2S 写到收尾） */
} audio_player_stream_stats_t;

esp_err_t audio_player_stream_stats_read(audio_player_stream_stats_t *out);

/* 播放统计与最近一次 MP3 解码统计（diag 的 player stat 用；任意任务上下文可读） */
typedef struct {
    uint32_t frames;        /* 解码帧数 */
    uint32_t samples;       /* 每声道样本数（源采样率） */
    uint32_t rate;          /* 源采样率（Hz） */
    uint8_t channels;       /* 源声道数（1/2） */
    uint32_t duration_ms;   /* samples / rate */
    uint32_t skipped_bytes; /* ID3/sync 等跳过的字节 */
    uint32_t bad_frames;    /* 解帧失败次数 */
    uint32_t decode_us_avg; /* 平均单帧解码耗时 */
    uint32_t decode_us_max; /* 最大单帧解码耗时 */
} audio_player_mp3_stats_t;

typedef struct {
    uint32_t jobs_done;   /* 完成的播放次数（PCM + MP3 + 流式） */
    uint32_t pcm_jobs;    /* 其中 PCM */
    uint32_t mp3_jobs;    /* 其中整段 MP3（成功解码） */
    uint32_t mp3_errors;  /* MP3 解码失败次数 */
    uint32_t last_play_ms;/* 最近一次播放实际耗时（含尾部静音冲刷，不含 300ms R2 延时） */
    /* T3.2：半双工观测（R3/R4 真机断言用） */
    uint32_t out_peak;    /* 最近 ~320ms 输出峰值（含 duck/淡出；R3 用它对比 duck 前后） */
    uint32_t stop_count;  /* 淡出停止次数（R4） */
    uint32_t last_stop_ms;/* 最近一次淡出停止：请求 → 输出静音（ms） */
    bool duck_on;         /* 当前 duck 开关 */
    uint32_t duck_gain_q8;/* 当前 duck 增益（q8，256=0dB，64≈-12dB） */
    audio_player_mp3_stats_t mp3;
} audio_player_stats_t;

/* ---------------- 音乐 PCM 流（P4/T4.1：外部 task 解码 → CH_MUSIC） ----------------
 * 生产者（music_http 任务）负责 HTTP 拉流 + MP3 解码 + 重采样到 48k，只把**原始 48k
 * 单声道 PCM** 写进 CH_MUSIC；音量/duck/限幅仍由混音级统一做（CONVENTIONS §3）。
 * 全部函数非阻塞或只阻塞生产者（audio task 不受影响）；停止到静音 ≤ 通道缓冲 + 100ms 淡出。
 */

typedef struct {
    bool active;
    uint32_t sample_rate;     /* 源采样率（仅展示；写入的必须是 48k） */
    uint32_t ring_ms;         /* 当前音乐 PCM ring 待播毫秒（近似） */
    uint32_t written_frames;  /* 生产者写入的 48k 帧数 */
    uint32_t played_frames;   /* 混音已消费的帧数 */
    uint32_t underruns;       /* 生产者侧写超时（ring 满）/混音侧欠载次数 */
    uint32_t first_sound_ms;  /* start() → 首个音乐样本进混音（0=未出声） */
    /* ---- HTTP/T4.1 拉流统计 ---- */
    bool url_active;          /* music_http 任务在跑 */
    uint32_t http_bytes;
    int http_status;
    int32_t content_length;   /* -1 = chunked/未知 */
    uint32_t http_errors;
    uint32_t first_byte_ms;
    uint32_t connect_ms;
    uint32_t decoded_frames;
} audio_player_music_stats_t;

/* 启动音乐通道（不启动 HTTP；HTTP 在 audio_player 的 source_http 里）。
 * 前置：播放器空闲（提示音/TTS/整段 MP3 都不在播），否则 ESP_ERR_INVALID_STATE。 */
esp_err_t audio_player_music_start(uint32_t sample_rate_hint);

/* 写 48k 单声道 PCM；返回实际写入帧数（0=未启动/已停止）。
 * 只允许在 music_http 任务里调用；ring 满时最多等 timeout_ms，超时返回已写入帧数。 */
size_t audio_player_music_write(const int16_t *pcm48, size_t frames, uint32_t timeout_ms);

/* 停止：丢弃 ring 里未播数据，通道 100ms 淡出后结束（≤ 通道缓冲 + 100ms）。 */
esp_err_t audio_player_music_stop(void);

/* T4.1：直接播一个远端 http(s) URL（内部起 music_http 拉流任务）。
 * URL 只来自服务端 music_track；端侧只校验前缀/长度，不做搜索/解析。 */
esp_err_t audio_player_music_play_url(const char *url);
/* 停止拉流任务（会先让音乐通道淡出）。 */
esp_err_t audio_player_music_stop_url(void);

/* 自然结束（HTTP 流读到 EOF）：保留 ring 里已写入的数据播完再收尾。 */
esp_err_t audio_player_music_end(void);

bool audio_player_music_active(void);
esp_err_t audio_player_music_stats_read(audio_player_music_stats_t *out);

/* 开发期探针（diag 的 player pcm 用）：读取最近一次 MP3 解码的单声道 PCM，
 * 让 PC 侧能与 FFmpeg/PyAV 参考解码逐样本对拍——"能出声"之外的真机正确性证据。 */
esp_err_t audio_player_mp3_pcm_read(int16_t *dst, size_t offset, size_t count);

esp_err_t audio_player_stats_read(audio_player_stats_t *out);
/* 清空播放统计与流式统计（流式播放中调用只清计数，不影响当前流） */
void audio_player_stats_reset(void);

/* 音量 0~100（软件数字音量） */
esp_err_t audio_player_set_volume(uint8_t volume);
uint8_t audio_player_get_volume(void);

/* ---------------- 三通道混音（T3.3） ----------------
 * 输出链：CH_TTS / CH_PROMPT / CH_MUSIC（48k 单声道 ring）→ 饱和相加 →
 * 音量（0~100，20ms 线性插值）→ 软限幅（上限 -1dBFS，渐近式无硬削波）→ I2S 48k 立体声。
 * 通道增益：音乐（CH_MUSIC）在语音播放时 duck 到 -12dB；提示音（CH_PROMPT）在 TTS 播放时
 * 让位到 -6dB；TTS 恒 0dB。通道起播 10ms 淡入、结束 100ms 淡出。
 * 约束：不新增任务；ring/捕获缓冲全部 PSRAM；player 任务内不做 malloc。
 */
typedef enum {
    AUDIO_PLAYER_CH_TTS = 0,   /* 云端 TTS / 整段探针 */
    AUDIO_PLAYER_CH_PROMPT,    /* 本地提示音 */
    AUDIO_PLAYER_CH_MUSIC,     /* 音乐（P4 接真实音源，本轮仅骨架 + 探针） */
    AUDIO_PLAYER_CH_NUM,
} audio_player_ch_t;

/* 单通道混音状态（diag 的 player mix stat 用） */
typedef struct {
    bool active;          /* 参与混音 */
    uint32_t gain_q8;     /* 通道增益，256=0dB、128≈-6dB、64≈-12dB */
    uint32_t level_ms;    /* ring 内待播毫秒 */
    uint32_t peak;        /* 重置以来该通道贡献的最大幅度（含包络/增益） */
} audio_player_ch_stats_t;

typedef struct {
    uint8_t vol;                /* 音量设置值 0~100 */
    uint32_t vol_q8;            /* 当前生效音量（q8，256=100%，含插值） */
    uint32_t vol_target_q8;     /* 目标音量（q8） */
    uint32_t limiter;           /* 重置以来触发软限幅的样本数 */
    uint32_t limiter_peak;      /* 触发限幅前的最大幅度（可能 >32767） */
    uint32_t clip;              /* 硬削波样本数（设计上恒 0，用于自证"无削波"） */
    uint32_t mix_peak;          /* 最近 ~320ms 混音输出峰值 */
    uint32_t block_max_us;      /* 重置以来单块（10ms）泵+混音+限幅最大耗时 */
    audio_player_ch_stats_t ch[AUDIO_PLAYER_CH_NUM];
} audio_player_mix_stats_t;

esp_err_t audio_player_mix_stats_read(audio_player_mix_stats_t *out);
void audio_player_mix_stats_reset(void);

/* ---------------- 混音探针（T3.3，仅开发期 diag 使用，量产不调用） ----------------
 * player mix tone：往指定通道塞一个正弦音调源（相位从峰值开始，用于验证 10ms 淡入），
 *   freq 100~4000 Hz、ms 50~10000、amp 100~20000。播放器空闲时自动起混音泵；
 *   泵已在跑时直接叠加；真实播放任务（提示音/TTS/MP3）在播时返回 ESP_ERR_INVALID_STATE。
 * player mix stop：停掉所有探针音调源（真实播放任务不受影响，返回 ESP_ERR_INVALID_STATE）。
 * player mix capture/read：从下一个混音输出块开始抓取 48k 单声道输出（≤16384 帧，PSRAM），
 *   供 PC 侧 FFT/幅度/包络对拍。capture 缓冲在首次调用时从 PSRAM 分配。
 */
esp_err_t audio_player_mix_tone(audio_player_ch_t ch, uint32_t freq_hz, uint32_t ms, uint32_t amp);
esp_err_t audio_player_mix_stop(void);
esp_err_t audio_player_mix_capture(uint32_t frames);
/* 已抓取样例数（播放提前结束时 < 请求帧数；PC 侧应只 dump 这一段） */
esp_err_t audio_player_mix_capture_status(uint32_t *captured, uint32_t *requested);
esp_err_t audio_player_mix_capture_read(int16_t *dst, size_t offset, size_t count);
