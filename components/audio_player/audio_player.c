/*
 * 播放引擎：把任意采样率的单声道 PCM 变成 48kHz 立体声输出（音量/淡入淡出/尾音处理），
 * 并在播放结束后回调业务层。T2.3 起支持整段 MP3 输入；T2.4 起支持流式 MP3
 * （TTS 下行边收边解）。
 *
 * 任务模型：单播放任务（核 1/优先级 5），队列深度 4；解码、重采样、写 I2S 全部串行。
 * 内存：输出块在内部 RAM（I2S DMA 需要）；MP3 解码状态、流式 ring/stage/收尾环全部 PSRAM
 * （见 decoder_mp3 与本文件 stream 段）。音频任务内不做 malloc。
 *
 * 流式（T2.4）关键设计：
 * - 生产者（voice_cloud 任务）只做 PSRAM ring 拷贝（≤2KB 临界区），不唤醒、不解码；
 * - 消费者（player 任务）按 I2S 节奏拉数据：先起播预缓冲（2 帧或 120ms），
 *   ring 空但流未结束时补静音并计一次 underrun，恢复后接着解（不重置解码器）；
 * - 重采样跨帧连续：游标 (idx, frac) 与上一块末样本 carry 一起保留，帧边界不重来；
 * - 收尾：end 后剩余 ≤100ms 时切到 100ms 单声道收尾环，最后一环做线性淡出，
 *   再走既有静音冲刷/关 TX + 300ms 尾部延时。
 */

#include "audio_player.h"

#include <string.h>

#include "audio_io.h"
#include "decoder_mp3.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "PLAYER";

#define PLAYER_TASK_STACK   6144
#define PLAYER_TASK_PRIO    5
#define PLAYER_CORE         1
#define PLAYER_QUEUE_LEN    4
#define OUT_FRAMES_CHUNK    480 /* 10ms @48k */
#define FADE_IN_FRAMES      (AUDIO_IO_SPK_SAMPLE_RATE / 100) /* 10ms */
#define FADE_OUT_FRAMES     (AUDIO_IO_SPK_SAMPLE_RATE / 10)  /* 100ms */

/* ---------------- 流式参数（T2.4） ---------------- */
#define STREAM_RING_BYTES        CONFIG_AUDIO_PLAYER_STREAM_RING_BYTES
#define STREAM_RD_CHUNK          2048 /* 单次临界区 memcpy 上限（契约 §1.2.2） */
#define STREAM_MAX_FRAME_SAMPLES 1152 /* MPEG Layer III 单帧最多 1152 样本/声道 */
#define STREAM_STAGE_SAMPLES     ((CONFIG_AUDIO_PLAYER_STREAM_PREROLL_FRAMES + 1) * STREAM_MAX_FRAME_SAMPLES)
#define STREAM_END_RING_SAMPLES  FADE_OUT_FRAMES /* 100ms @48k 单声道 */
#define STREAM_END_MS            (FADE_OUT_FRAMES / (AUDIO_IO_SPK_SAMPLE_RATE / 1000))
#define STREAM_DONE_EXTRA_SLOTS  3

typedef enum {
    PLAY_JOB_PCM = 0, /* 调用方持有一段 PCM */
    PLAY_JOB_MP3,     /* 调用方持有一段 MP3 码流，播放任务内解码 */
    PLAY_JOB_STREAM,  /* 流式播放（T2.4），实际数据在 PSRAM ring 里 */
} play_job_kind_t;

typedef struct {
    play_job_kind_t kind;
    const int16_t *pcm; /* PCM：采样点（单声道）；调用方保证生命周期 */
    size_t samples;     /* PCM：采样点数 */
    uint32_t rate;      /* PCM：源采样率 */
    const uint8_t *mp3; /* MP3：码流 */
    size_t mp3_len;     /* MP3：字节数 */
    uint8_t codec;      /* STREAM：1=MP3 */
    uint32_t stream_rate;
} play_job_t;

typedef struct {
    audio_player_done_cb_t cb;
    void *arg;
} done_slot_t;

static QueueHandle_t s_queue = NULL;
static TaskHandle_t s_task = NULL;
static volatile bool s_busy = false;
static uint8_t s_volume = CONFIG_AUDIO_PLAYER_DEFAULT_VOLUME;

/* 完成回调：主槽（覆盖语义）+ 3 个附加槽；快照后回调，避免回调里增删导致遍历失效 */
static portMUX_TYPE s_done_mux = portMUX_INITIALIZER_UNLOCKED;
static audio_player_done_cb_t s_done_cb = NULL;
static void *s_done_arg = NULL;
static done_slot_t s_done_extra[STREAM_DONE_EXTRA_SLOTS];

/* 统计：播放任务写、diag 读，用自旋锁保护整结构体拷贝（临界区只有几十字节） */
static portMUX_TYPE s_stats_mux = portMUX_INITIALIZER_UNLOCKED;
static audio_player_stats_t s_stats;

/* 最近一次 MP3 解码结果（PCM 指向 decoder_mp3 的静态 PSRAM 缓冲，仅探针对拍用） */
static const int16_t *s_last_mp3_pcm;
static size_t s_last_mp3_samples;

/* ---------------- 流式状态（T2.4） ---------------- */
static uint8_t *s_ring;        /* PSRAM：下行压缩数据 ring */
static int16_t *s_stage;       /* PSRAM：待播源样本（预缓冲 + 单帧中转） */
static int16_t *s_end_ring;    /* PSRAM：收尾 100ms 48k 单声道环 */
static uint8_t *s_rd_scratch;  /* PSRAM：ring 读取暂存（临界区外再喂解码器） */

static portMUX_TYPE s_stream_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool s_stream_active;
static volatile bool s_stream_end_req;
static volatile bool s_stream_abort_req;
static volatile bool s_stream_resync_req;
static uint32_t s_stream_wr; /* 单调写计数（字节） */
static uint32_t s_stream_rd; /* 单调读计数（字节） */
static uint32_t s_stream_frame_bytes_ema; /* 最近帧字节数（EMA，ring→ms 估算用） */
static uint32_t s_stream_frame_ms;        /* 最近帧时长（ms） */
static uint32_t s_stream_ring_high_bytes; /* 本次流 ring 最高水位（字节；统计读取时换算 ms） */
static int64_t s_stream_first_byte_us;
static int64_t s_stream_sound_us;
static int64_t s_stream_finish_us;
static int64_t s_stream_drop_warn_us;
static int64_t s_stream_underrun_warn_us;
static audio_player_stream_stats_t s_stream_stats; /* 受 s_stream_mux 保护 */

typedef enum {
    STREAM_RESULT_OK = 0, /* 正常收尾（ok=true，走 300ms 尾部延时） */
    STREAM_RESULT_ABORT,  /* 被打断（ok=false，无尾部延时） */
    STREAM_RESULT_FAIL,   /* 解码失败/参数变化/无数据（ok=false） */
} stream_result_t;

/* 跨块连续重采样的"下一块"语义 */
typedef enum {
    NEXT_FIRST = 0, /* 首块：从块内 0 开始 */
    NEXT_BOUNDARY,  /* 恰好落在块边界：下一块从 0 开始 */
    NEXT_CARRY,     /* 落在两块之间：用 carry 与本块首样本插值，frac 保持 */
} next_mode_t;

typedef struct {
    int16_t *out; /* I2S 输出块（内部 RAM，480 帧立体声） */
    const int16_t *blk;
    uint32_t blk_n;
    int32_t rs_i;     /* 当前块内的整数位置（-1 表示跨块 carry 段） */
    uint32_t rs_frac; /* 距下一个整数样本的分数（1/48000） */
    int16_t rs_carry; /* 上一块最后一个样本 */
    next_mode_t next_mode;
    uint32_t src_rate;
    uint8_t src_ch;
    bool have_params;
    uint32_t stage_len; /* s_stage 中的样本数 */
    uint32_t preroll_frames;
    uint32_t decoded_frames;
    uint32_t decoded_samples;
    uint32_t frame_bytes_ema;
    uint32_t frame_ms;
    uint32_t fade_in_left;
    bool in_underrun;
    uint32_t underruns;
    bool end_phase;
    bool decoder_done;
    uint32_t end_rd;
    uint32_t end_len;
    uint32_t fade_total;
    uint32_t fade_pos;
} stream_ctx_t;

/* 一次播放的公共尾巴：静音冲刷 DMA（避免余音被硬切）+ 关时钟 */
static void tx_flush_and_disable(int16_t *out)
{
    memset(out, 0, (size_t)OUT_FRAMES_CHUNK * 2 * sizeof(int16_t));
    for (int i = 0; i < (AUDIO_IO_SPK_SAMPLE_RATE / 10) / OUT_FRAMES_CHUNK; i++) {
        audio_io_write_spk(out, OUT_FRAMES_CHUNK, 100);
    }
    vTaskDelay(pdMS_TO_TICKS(150));
    audio_io_spk_enable(false);
}

/* 把一段源采样率单声道 PCM 播完：线性重采样到 48k + 音量 + 淡入淡出 + 限幅 */
static void play_block(const int16_t *pcm, size_t samples, uint32_t rate, int16_t *out)
{
    const uint32_t total_out = (uint32_t)((uint64_t)samples * AUDIO_IO_SPK_SAMPLE_RATE / rate);
    const uint32_t fade_in = total_out < FADE_IN_FRAMES ? total_out : FADE_IN_FRAMES;
    const uint32_t fade_out = total_out < FADE_OUT_FRAMES ? total_out : FADE_OUT_FRAMES;

    ESP_LOGI(TAG, "play %u samples @%u Hz (vol=%u)", (unsigned)samples, (unsigned)rate,
             (unsigned)s_volume);

    audio_io_spk_enable(true);

    uint32_t produced = 0;
    while (produced < total_out) {
        uint32_t n = total_out - produced;
        if (n > OUT_FRAMES_CHUNK) {
            n = OUT_FRAMES_CHUNK;
        }

        for (uint32_t i = 0; i < n; i++) {
            const uint32_t idx = produced + i;
            /* 线性重采样：src_pos = idx * rate / 48k */
            const uint32_t src_pos = (uint32_t)((uint64_t)idx * rate / AUDIO_IO_SPK_SAMPLE_RATE);
            const uint32_t frac = (uint32_t)((uint64_t)idx * rate % AUDIO_IO_SPK_SAMPLE_RATE);
            const int32_t s0 = pcm[src_pos];
            const int32_t s1 = (src_pos + 1 < samples) ? pcm[src_pos + 1] : s0;
            int32_t v = s0 + (int32_t)(((int64_t)(s1 - s0) * frac) / AUDIO_IO_SPK_SAMPLE_RATE);

            /* 软件音量 */
            v = v * s_volume / 100;

            /* 淡入/淡出（q8 增益） */
            int32_t gain = 256;
            if (fade_in > 0 && idx < fade_in) {
                gain = (int32_t)(256 * idx / fade_in);
            } else if (fade_out > 0 && idx + fade_out >= total_out) {
                gain = (int32_t)(256 * (total_out - idx) / fade_out);
            }
            v = v * gain / 256;

            if (v > 32767) {
                v = 32767;
            } else if (v < -32768) {
                v = -32768;
            }
            out[i * 2] = (int16_t)v;
            out[i * 2 + 1] = (int16_t)v;
        }

        if (audio_io_write_spk(out, n, 100) < 0) {
            ESP_LOGW(TAG, "spk write failed");
            break;
        }
        produced += n;
    }

    tx_flush_and_disable(out);
}

/* 执行一个播放任务；PCM 直接播，MP3 先解码再播（都在播放任务上下文） */
static void run_job(const play_job_t *job, int16_t *out, bool *ok)
{
    const int16_t *pcm = job->pcm;
    size_t samples = job->samples;
    uint32_t rate = job->rate;
    bool decoded = true;

    *ok = true;

    if (job->kind == PLAY_JOB_MP3) {
        const int16_t *dec_pcm = NULL;
        size_t dec_samples = 0;
        uint32_t dec_rate = 0;

        esp_err_t err = mp3_dec_decode_all(job->mp3, job->mp3_len, &dec_pcm, &dec_samples, &dec_rate);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "mp3 decode failed: %s", esp_err_to_name(err));
            portENTER_CRITICAL(&s_stats_mux);
            s_stats.mp3_errors++;
            portEXIT_CRITICAL(&s_stats_mux);
            decoded = false;
            *ok = false;
        } else {
            pcm = dec_pcm;
            samples = dec_samples;
            rate = dec_rate;
            s_last_mp3_pcm = dec_pcm;
            s_last_mp3_samples = dec_samples;
        }
    }

    if (decoded) {
        play_block(pcm, samples, rate, out);
        portENTER_CRITICAL(&s_stats_mux);
        s_stats.jobs_done++;
        if (job->kind == PLAY_JOB_MP3) {
            s_stats.mp3_jobs++;
            mp3_dec_stats_t dec;
            mp3_dec_stats_read(&dec);
            s_stats.mp3.frames = dec.frames;
            s_stats.mp3.samples = dec.samples;
            s_stats.mp3.rate = dec.rate;
            s_stats.mp3.channels = dec.channels;
            s_stats.mp3.duration_ms = dec.rate ? (uint32_t)((uint64_t)dec.samples * 1000 / dec.rate) : 0;
            s_stats.mp3.skipped_bytes = dec.skipped_bytes;
            s_stats.mp3.bad_frames = dec.bad_frames;
            s_stats.mp3.decode_us_avg = dec.frames ? dec.decode_us_total / dec.frames : 0;
            s_stats.mp3.decode_us_max = dec.decode_us_max;
        } else {
            s_stats.pcm_jobs++;
        }
        portEXIT_CRITICAL(&s_stats_mux);
    }
}

/* ---------------- 流式实现（T2.4） ---------------- */

/* ring → 暂存（临界区内拷贝，单次 ≤2KB），返回实际字节数 */
static size_t stream_ring_read(uint8_t *dst, size_t cap)
{
    size_t n = 0;
    portENTER_CRITICAL(&s_stream_mux);
    const uint32_t avail = s_stream_wr - s_stream_rd;
    if (avail > 0) {
        n = (avail > cap) ? cap : (size_t)avail;
        const uint32_t off = s_stream_rd % STREAM_RING_BYTES;
        const size_t first = (STREAM_RING_BYTES - off) < n ? (STREAM_RING_BYTES - off) : n;
        memcpy(dst, s_ring + off, first);
        if (n > first) {
            memcpy(dst + first, s_ring, n - first);
        }
        s_stream_rd += (uint32_t)n;
        uint32_t level = 0;
        if (s_stream_frame_bytes_ema && s_stream_frame_ms) {
            level = (uint32_t)((uint64_t)(s_stream_wr - s_stream_rd) *
                               s_stream_frame_ms / s_stream_frame_bytes_ema);
        }
        s_stream_stats.ring_level_ms = level;
    }
    portEXIT_CRITICAL(&s_stream_mux);
    return n;
}

/* 解码器缺数据时从 ring 补一块（≤2KB）喂进去；返回实际喂入字节数 */
static size_t stream_feed_one_chunk(void)
{
    const size_t n = stream_ring_read(s_rd_scratch, STREAM_RD_CHUNK);
    if (n == 0) {
        return 0;
    }
    /* pending 容量按"上一块残留半帧（≤1441B）+ 本块（≤2KB）"设计，不会溢出 */
    if (mp3_dec_stream_feed(s_rd_scratch, n) != ESP_OK) {
        return 0;
    }
    return n;
}

/* 源数据 → 单声道 stage（预缓冲/播放时均走这里，同时刷新统计与参数校验） */
static esp_err_t stream_decode_to_stage(stream_ctx_t *c)
{
    const int16_t *pcm = NULL;
    uint32_t samples = 0, rate = 0, frame_bytes = 0;
    uint8_t ch = 0;
    esp_err_t err;
    for (;;) {
        err = mp3_dec_stream_frame(&pcm, &samples, &rate, &ch, &frame_bytes);
        if (err != ESP_ERR_NOT_FOUND) {
            break;
        }
        if (stream_feed_one_chunk() == 0) {
            break; /* ring 已空且凑不满一帧：等下一批下行数据 */
        }
    }
    if (err != ESP_OK) {
        return err;
    }

    if (!c->have_params) {
        c->src_rate = rate;
        c->src_ch = ch;
        c->have_params = true;
        ESP_LOGI(TAG, "stream source %u Hz %u ch", (unsigned)rate, (unsigned)ch);
    } else if (rate != c->src_rate || ch != c->src_ch) {
        ESP_LOGW(TAG, "stream frame params changed (%u Hz/%u ch -> %u Hz/%u ch), abort",
                 (unsigned)c->src_rate, (unsigned)c->src_ch, (unsigned)rate, (unsigned)ch);
        return ESP_ERR_INVALID_STATE; /* 契约 §1.2.8：流中帧参数变化 → abort */
    }

    if (samples == 0 || samples > STREAM_MAX_FRAME_SAMPLES ||
        c->stage_len + samples > STREAM_STAGE_SAMPLES) {
        return ESP_ERR_INVALID_STATE;
    }
    memcpy(s_stage + c->stage_len, pcm, (size_t)samples * sizeof(int16_t));
    c->stage_len += samples;
    c->decoded_frames++;
    c->decoded_samples += samples;
    c->frame_ms = (uint32_t)((uint64_t)samples * 1000 / rate);
    if (c->frame_ms == 0) {
        c->frame_ms = 1;
    }
    c->frame_bytes_ema = c->frame_bytes_ema ? (c->frame_bytes_ema * 3 + frame_bytes) / 4 : frame_bytes;

    mp3_dec_stats_t dec;
    mp3_dec_stats_read(&dec);
    portENTER_CRITICAL(&s_stream_mux);
    s_stream_stats.decoded_frames = c->decoded_frames;
    s_stream_stats.decoded_samples = c->decoded_samples;
    s_stream_stats.rate = c->src_rate;
    s_stream_stats.channels = c->src_ch;
    s_stream_stats.bad_frames = dec.bad_frames;
    s_stream_frame_bytes_ema = c->frame_bytes_ema;
    s_stream_frame_ms = c->frame_ms;
    portEXIT_CRITICAL(&s_stream_mux);
    return ESP_OK;
}

/* 取下一块源数据（播放期）：上一块已按块消费完，stage 重置后解一帧 */
static esp_err_t stream_fetch_block(stream_ctx_t *c)
{
    c->stage_len = 0;
    c->blk = NULL;
    esp_err_t err = stream_decode_to_stage(c);
    if (err != ESP_OK) {
        return err;
    }
    c->blk = s_stage;
    c->blk_n = c->stage_len;
    return ESP_OK;
}

/* 产生一个 48k 单声道输出样本（跨帧/跨块连续线性插值） */
static esp_err_t stream_next_sample(stream_ctx_t *c, int16_t *out)
{
    for (;;) {
        if (!c->blk) {
            esp_err_t err = stream_fetch_block(c);
            if (err != ESP_OK) {
                return err;
            }
            if (c->next_mode == NEXT_CARRY) {
                c->rs_i = -1; /* frac 保持，s0=carry / s1=blk[0] */
            } else {
                c->rs_i = 0;
                c->rs_frac = 0;
            }
            c->next_mode = NEXT_BOUNDARY;
        }

        int32_t s0;
        int32_t s1;
        if (c->rs_i < 0) {
            s0 = c->rs_carry;
            s1 = c->blk[0];
        } else if ((uint32_t)c->rs_i + 1 < c->blk_n) {
            s0 = c->blk[c->rs_i];
            s1 = c->blk[c->rs_i + 1];
        } else if ((uint32_t)c->rs_i == c->blk_n - 1) {
            if (c->rs_frac == 0) {
                s0 = c->blk[c->blk_n - 1];
                s1 = s0;
            } else {
                c->rs_carry = c->blk[c->blk_n - 1];
                c->blk = NULL;
                c->next_mode = NEXT_CARRY;
                continue;
            }
        } else {
            /* 恰好落在块边界（rs_i == blk_n 且 frac == 0） */
            c->blk = NULL;
            c->next_mode = NEXT_BOUNDARY;
            continue;
        }

        int32_t v = s0 + (int32_t)(((int64_t)(s1 - s0) * (int64_t)c->rs_frac) /
                                   AUDIO_IO_SPK_SAMPLE_RATE);
        c->rs_frac += c->src_rate;
        while (c->rs_frac >= AUDIO_IO_SPK_SAMPLE_RATE) {
            c->rs_frac -= AUDIO_IO_SPK_SAMPLE_RATE;
            c->rs_i++;
        }
        *out = (int16_t)v;
        return ESP_OK;
    }
}

/* 渲染一个 10ms 输出块：音量 + 起播 10ms 淡入 + 限幅；源数据不足时剩余补静音 */
static esp_err_t stream_render_chunk(stream_ctx_t *c)
{
    uint32_t produced = 0;
    esp_err_t err = ESP_OK;

    while (produced < OUT_FRAMES_CHUNK) {
        int16_t v;
        err = stream_next_sample(c, &v);
        if (err != ESP_OK) {
            break;
        }
        int32_t x = (int32_t)v * (int32_t)s_volume / 100;
        if (c->fade_in_left > 0) {
            const uint32_t pos = FADE_IN_FRAMES - c->fade_in_left;
            x = x * (int32_t)pos / (int32_t)FADE_IN_FRAMES;
            c->fade_in_left--;
        }
        if (x > 32767) {
            x = 32767;
        } else if (x < -32768) {
            x = -32768;
        }
        c->out[produced * 2] = (int16_t)x;
        c->out[produced * 2 + 1] = (int16_t)x;
        produced++;
    }
    for (uint32_t i = produced; i < OUT_FRAMES_CHUNK; i++) {
        c->out[i * 2] = 0;
        c->out[i * 2 + 1] = 0;
    }
    return err;
}

/* 收尾阶段：把剩余源音频解码进 100ms 环，最后一环线性淡出到 0。
 * 返回 true 表示收尾完成（环已排空且解码器无更多数据）。 */
static bool stream_end_step(stream_ctx_t *c)
{
    while (c->end_len < STREAM_END_RING_SAMPLES) {
        int16_t v;
        esp_err_t err = stream_next_sample(c, &v);
        if (err != ESP_OK) {
            if (!c->decoder_done) {
                c->decoder_done = true;
                c->fade_total = c->end_len;
                c->fade_pos = 0;
            }
            break;
        }
        s_end_ring[(c->end_rd + c->end_len) % STREAM_END_RING_SAMPLES] = v;
        c->end_len++;
    }
    if (c->end_len == 0) {
        return true;
    }

    uint32_t n = c->end_len < OUT_FRAMES_CHUNK ? c->end_len : OUT_FRAMES_CHUNK;
    for (uint32_t i = 0; i < n; i++) {
        int32_t x = s_end_ring[(c->end_rd + i) % STREAM_END_RING_SAMPLES];
        x = x * (int32_t)s_volume / 100;
        if (c->fade_in_left > 0) {
            const uint32_t pos = FADE_IN_FRAMES - c->fade_in_left;
            x = x * (int32_t)pos / (int32_t)FADE_IN_FRAMES;
            c->fade_in_left--;
        }
        if (c->decoder_done && c->fade_total > 0) {
            x = x * (int32_t)(c->fade_total - c->fade_pos) / (int32_t)c->fade_total;
            c->fade_pos++;
        }
        if (x > 32767) {
            x = 32767;
        } else if (x < -32768) {
            x = -32768;
        }
        c->out[i * 2] = (int16_t)x;
        c->out[i * 2 + 1] = (int16_t)x;
    }
    for (uint32_t i = n; i < OUT_FRAMES_CHUNK; i++) {
        c->out[i * 2] = 0;
        c->out[i * 2 + 1] = 0;
    }
    c->end_rd = (c->end_rd + n) % STREAM_END_RING_SAMPLES;
    c->end_len -= n;

    if (audio_io_write_spk(c->out, OUT_FRAMES_CHUNK, 100) < 0) {
        ESP_LOGW(TAG, "spk write failed (stream end)");
    }
    return c->decoder_done && c->end_len == 0;
}

/* 剩余可播音频近似毫秒（ring 未解码字节 + 当前块未消费样本），用于切收尾阶段 */
static uint32_t stream_remaining_ms(stream_ctx_t *c, uint32_t ring_bytes,
                                    uint32_t frame_bytes_ema, uint32_t frame_ms)
{
    uint32_t ms = 0;
    if (frame_bytes_ema && frame_ms) {
        ms = (uint32_t)((uint64_t)ring_bytes * frame_ms / frame_bytes_ema);
    }
    if (c->blk && c->src_rate) {
        const uint32_t used = c->rs_i > 0 ? (uint32_t)c->rs_i : 0;
        const uint32_t left = c->blk_n > used ? c->blk_n - used : 0;
        ms += (uint32_t)((uint64_t)left * 1000 / c->src_rate);
    }
    return ms;
}

static stream_result_t run_stream(int16_t *out)
{
    stream_ctx_t ctx;
    memset(&ctx, 0, sizeof(ctx));
    ctx.out = out;
    ctx.next_mode = NEXT_FIRST;

    /* open 时已清空 ring/统计；这里只重置解码器（open 之后、player 起来之前
     * 可能已经收到若干 write，数据必须保留） */
    mp3_dec_stream_reset();
    s_stream_resync_req = false;

    stream_result_t result = STREAM_RESULT_OK;
    bool started = false;

    /* ---- 1. 预缓冲：已解出 ≥PREROLL_FRAMES 帧 或 距首字节 ≥PREROLL_MS（取先到者） ---- */
    esp_err_t pre_err = ESP_OK;
    bool preroll_done = false;
    for (;;) {
        if (s_stream_abort_req) {
            result = STREAM_RESULT_ABORT;
            goto finish;
        }
        while (ctx.preroll_frames < CONFIG_AUDIO_PLAYER_STREAM_PREROLL_FRAMES) {
            pre_err = stream_decode_to_stage(&ctx);
            if (pre_err != ESP_OK) {
                break;
            }
            ctx.preroll_frames++;
        }
        if (pre_err != ESP_OK && pre_err != ESP_ERR_NOT_FOUND) {
            result = STREAM_RESULT_FAIL;
            goto finish;
        }
        if (ctx.preroll_frames >= CONFIG_AUDIO_PLAYER_STREAM_PREROLL_FRAMES) {
            preroll_done = true;
            break;
        }
        if (s_stream_end_req) {
            /* 已经没有后续数据：解出多少播多少；一帧都没有时按失败收尾 */
            preroll_done = ctx.preroll_frames > 0;
            break;
        }
        int64_t first_us;
        portENTER_CRITICAL(&s_stream_mux);
        first_us = s_stream_first_byte_us;
        portEXIT_CRITICAL(&s_stream_mux);
        if (first_us != 0 && (esp_timer_get_time() - first_us) >=
                                 (int64_t)CONFIG_AUDIO_PLAYER_STREAM_PREROLL_MS * 1000) {
            preroll_done = true;
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }

    if (!preroll_done) {
        ESP_LOGW(TAG, "stream end before start (decoded=%u)", (unsigned)ctx.decoded_frames);
        result = STREAM_RESULT_FAIL;
        goto finish;
    }

    /* ---- 2. 起播：使能 I2S TX + 10ms 淡入；首帧前 I2S 不出声 ---- */
    {
        const int64_t now = esp_timer_get_time();
        portENTER_CRITICAL(&s_stream_mux);
        s_stream_sound_us = now;
        if (s_stream_first_byte_us != 0) {
            s_stream_stats.first_byte_to_sound_us = (uint32_t)(now - s_stream_first_byte_us);
        }
        const uint32_t first_sound_us = s_stream_stats.first_byte_to_sound_us;
        portEXIT_CRITICAL(&s_stream_mux);

        audio_io_spk_enable(true);
        started = true;
        ctx.fade_in_left = FADE_IN_FRAMES;
        ctx.blk = ctx.stage_len ? s_stage : NULL;
        ctx.blk_n = ctx.stage_len;
        ctx.next_mode = NEXT_FIRST;
        ctx.rs_i = 0;
        ctx.rs_frac = 0;

        const uint32_t prebuffer_ms = ctx.src_rate
                                          ? (uint32_t)((uint64_t)ctx.decoded_samples * 1000 / ctx.src_rate)
                                          : 0;
        ESP_LOGI(TAG, "stream first sound after %u ms (prebuffer %u ms)",
                 (unsigned)(first_sound_us / 1000), (unsigned)prebuffer_ms);
    }

    /* ---- 3. 边收边解：欠载补静音；end 后剩余 ≤100ms 切收尾环 ---- */
    for (;;) {
        if (s_stream_abort_req) {
            result = STREAM_RESULT_ABORT;
            goto finish;
        }
        if (s_stream_resync_req) {
            s_stream_resync_req = false;
            mp3_dec_stream_resync();
        }

        if (s_stream_end_req && !ctx.end_phase && ctx.have_params) {
            uint32_t ring_bytes;
            uint32_t fb_ema;
            uint32_t f_ms;
            portENTER_CRITICAL(&s_stream_mux);
            ring_bytes = s_stream_wr - s_stream_rd;
            fb_ema = s_stream_frame_bytes_ema;
            f_ms = s_stream_frame_ms;
            portEXIT_CRITICAL(&s_stream_mux);
            if (stream_remaining_ms(&ctx, ring_bytes, fb_ema, f_ms) <= STREAM_END_MS) {
                ctx.end_phase = true;
            }
        }

        if (ctx.end_phase) {
            if (stream_end_step(&ctx)) {
                break;
            }
            continue;
        }

        esp_err_t err = stream_render_chunk(&ctx);
        if (err == ESP_ERR_NOT_FOUND) {
            /* 欠载：输出静音继续走 I2S 时钟；每次连续欠载只计一次 */
            if (!ctx.in_underrun) {
                ctx.in_underrun = true;
                ctx.underruns++;
                portENTER_CRITICAL(&s_stream_mux);
                s_stream_stats.underruns = ctx.underruns;
                portEXIT_CRITICAL(&s_stream_mux);
                const int64_t t = esp_timer_get_time();
                if (t - s_stream_underrun_warn_us >= 1000000) {
                    s_stream_underrun_warn_us = t;
                    ESP_LOGW(TAG, "stream underrun #%u (ring 0 ms)", (unsigned)ctx.underruns);
                }
            }
        } else if (err != ESP_OK) {
            ESP_LOGW(TAG, "stream decode failed: %s", esp_err_to_name(err));
            result = STREAM_RESULT_FAIL;
            break;
        } else {
            ctx.in_underrun = false;
        }

        if (audio_io_write_spk(out, OUT_FRAMES_CHUNK, 100) < 0) {
            ESP_LOGW(TAG, "spk write failed (stream)");
            result = STREAM_RESULT_FAIL;
            break;
        }
    }

finish:
    {
        const int64_t finish_us = esp_timer_get_time();
        portENTER_CRITICAL(&s_stream_mux);
        s_stream_finish_us = started ? finish_us : 0;
        if (started && s_stream_sound_us) {
            s_stream_stats.duration_ms = (uint32_t)((finish_us - s_stream_sound_us) / 1000);
        }
        audio_player_stream_stats_t st = s_stream_stats;
        uint32_t drop = 0;
        if (result == STREAM_RESULT_ABORT) {
            drop = s_stream_wr - s_stream_rd;
            s_stream_rd = s_stream_wr;
        }
        portEXIT_CRITICAL(&s_stream_mux);

        if (result == STREAM_RESULT_OK) {
            ESP_LOGI(TAG, "stream end frames=%u bytes=%u decoded=%u dur=%u ms underrun=%u bad=%u",
                     (unsigned)st.fed_frames, (unsigned)st.fed_bytes, (unsigned)st.decoded_frames,
                     (unsigned)st.duration_ms, (unsigned)st.underruns, (unsigned)st.bad_frames);
            tx_flush_and_disable(out);
        } else if (result == STREAM_RESULT_ABORT) {
            /* 打断：立刻停声（关 TX），丢弃 ring 剩余；不写静音等待，回调要快 */
            if (started) {
                audio_io_spk_enable(false);
            }
            ESP_LOGW(TAG, "stream abort (frames=%u, drop %u B)",
                     (unsigned)st.fed_frames, (unsigned)drop);
        } else {
            if (started) {
                audio_io_spk_enable(false);
            }
            ESP_LOGW(TAG, "stream failed (frames=%u, decoded=%u)",
                     (unsigned)st.fed_frames, (unsigned)st.decoded_frames);
        }
        return result;
    }
}

/* 完成回调分发：先快照（主槽 + 3 附加槽），再在临界区外按顺序调用 */
static void notify_done(audio_player_src_t src, bool ok)
{
    audio_player_done_cb_t main_cb;
    void *main_arg;
    done_slot_t extra[STREAM_DONE_EXTRA_SLOTS];

    portENTER_CRITICAL(&s_done_mux);
    main_cb = s_done_cb;
    main_arg = s_done_arg;
    memcpy(extra, s_done_extra, sizeof(extra));
    portEXIT_CRITICAL(&s_done_mux);

    if (main_cb) {
        main_cb(src, ok, main_arg);
    }
    for (size_t i = 0; i < STREAM_DONE_EXTRA_SLOTS; i++) {
        if (extra[i].cb) {
            extra[i].cb(src, ok, extra[i].arg);
        }
    }
}

static void player_task(void *arg)
{
    (void)arg;
    int16_t *out = heap_caps_malloc((size_t)OUT_FRAMES_CHUNK * 2 * sizeof(int16_t),
                                    MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!out) {
        ESP_LOGE(TAG, "output buffer alloc failed");
        vTaskDelete(NULL);
        return;
    }

    play_job_t job;
    for (;;) {
        if (xQueueReceive(s_queue, &job, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        s_busy = true;
        const int64_t t0 = esp_timer_get_time();
        audio_player_src_t src = AUDIO_PLAYER_SRC_PCM;
        bool ok = true;
        bool tail_delay = true;

        if (job.kind == PLAY_JOB_STREAM) {
            src = AUDIO_PLAYER_SRC_TTS_STREAM;
            const stream_result_t r = run_stream(out);
            ok = (r == STREAM_RESULT_OK);
            tail_delay = ok;
        } else {
            src = (job.kind == PLAY_JOB_MP3) ? AUDIO_PLAYER_SRC_MP3_FILE : AUDIO_PLAYER_SRC_PCM;
            run_job(&job, out, &ok);
        }

        const uint32_t elapsed_ms = (uint32_t)((esp_timer_get_time() - t0) / 1000);
        portENTER_CRITICAL(&s_stats_mux);
        s_stats.last_play_ms = elapsed_ms;
        portEXIT_CRITICAL(&s_stats_mux);
        ESP_LOGI(TAG, "job done (%u ms)", (unsigned)elapsed_ms);

        /* 尾部延时后再通知完成（半双工恢复 feed 用，见架构 R2）；打断/失败不延时 */
        if (tail_delay) {
            vTaskDelay(pdMS_TO_TICKS(CONFIG_AUDIO_PLAYER_TAIL_DELAY_MS));
        }
        s_busy = false;
        if (job.kind == PLAY_JOB_STREAM) {
            portENTER_CRITICAL(&s_stream_mux);
            s_stream_active = false;
            s_stream_end_req = false;
            s_stream_abort_req = false;
            s_stream_resync_req = false;
            portEXIT_CRITICAL(&s_stream_mux);
        }
        notify_done(src, ok);
    }
}

esp_err_t audio_player_init(void)
{
    if (s_task) {
        return ESP_OK;
    }

#if CONFIG_AUDIO_PLAYER_ENABLE_MP3
    /* PSRAM 分配放在初始化期（app_main 上下文），播放任务里不做任何 malloc */
    esp_err_t err = mp3_dec_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "MP3 能力不可用（%s），提示音/上行链路不受影响", esp_err_to_name(err));
    }

    /* 流式缓冲全部 PSRAM：ring 64KB + 预缓冲 stage ~7KB + 收尾环 9.6KB + 读取暂存 2KB */
    s_ring = heap_caps_malloc(STREAM_RING_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_stage = heap_caps_malloc((size_t)STREAM_STAGE_SAMPLES * sizeof(int16_t),
                               MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_end_ring = heap_caps_malloc((size_t)STREAM_END_RING_SAMPLES * sizeof(int16_t),
                                  MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_rd_scratch = heap_caps_malloc(STREAM_RD_CHUNK, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_ring || !s_stage || !s_end_ring || !s_rd_scratch) {
        ESP_LOGW(TAG, "流式播放缓冲分配失败（PSRAM 不足），stream API 不可用");
        heap_caps_free(s_ring);
        heap_caps_free(s_stage);
        heap_caps_free(s_end_ring);
        heap_caps_free(s_rd_scratch);
        s_ring = NULL;
        s_stage = NULL;
        s_end_ring = NULL;
        s_rd_scratch = NULL;
    } else {
        ESP_LOGI(TAG, "stream: ring=%u B, stage=%u B, end_ring=%u B (PSRAM)",
                 (unsigned)STREAM_RING_BYTES,
                 (unsigned)((size_t)STREAM_STAGE_SAMPLES * sizeof(int16_t)),
                 (unsigned)((size_t)STREAM_END_RING_SAMPLES * sizeof(int16_t)));
    }
#else
    ESP_LOGW(TAG, "MP3 能力关闭，流式播放不可用");
#endif

    s_queue = xQueueCreate(PLAYER_QUEUE_LEN, sizeof(play_job_t));
    if (!s_queue) {
        ESP_LOGE(TAG, "queue create failed");
        return ESP_ERR_NO_MEM;
    }

    BaseType_t ok = xTaskCreatePinnedToCore(player_task, "player", PLAYER_TASK_STACK, NULL,
                                            PLAYER_TASK_PRIO, &s_task, PLAYER_CORE);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "task create failed");
        s_task = NULL;
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "ready: out=%d Hz, vol=%u, mp3=%s", AUDIO_IO_SPK_SAMPLE_RATE,
             (unsigned)s_volume, audio_player_mp3_ready() ? "on" : "off");
    return ESP_OK;
}

void audio_player_register_done_cb(audio_player_done_cb_t cb, void *arg)
{
    portENTER_CRITICAL(&s_done_mux);
    s_done_cb = cb;
    s_done_arg = arg;
    portEXIT_CRITICAL(&s_done_mux);
}

esp_err_t audio_player_subscribe_done(audio_player_done_cb_t cb, void *arg)
{
    if (!cb) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = ESP_ERR_NO_MEM;
    portENTER_CRITICAL(&s_done_mux);
    for (size_t i = 0; i < STREAM_DONE_EXTRA_SLOTS; i++) {
        if (!s_done_extra[i].cb) {
            s_done_extra[i].cb = cb;
            s_done_extra[i].arg = arg;
            err = ESP_OK;
            break;
        }
    }
    portEXIT_CRITICAL(&s_done_mux);
    return err;
}

esp_err_t audio_player_unsubscribe_done(audio_player_done_cb_t cb)
{
    if (!cb) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = ESP_ERR_NOT_FOUND;
    portENTER_CRITICAL(&s_done_mux);
    for (size_t i = 0; i < STREAM_DONE_EXTRA_SLOTS; i++) {
        if (s_done_extra[i].cb == cb) {
            s_done_extra[i].cb = NULL;
            s_done_extra[i].arg = NULL;
            err = ESP_OK;
            break;
        }
    }
    portEXIT_CRITICAL(&s_done_mux);
    return err;
}

esp_err_t audio_player_play_pcm(const int16_t *pcm, size_t samples, uint32_t sample_rate)
{
    if (!s_queue || !pcm || samples == 0 || sample_rate == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_stream_active) {
        return ESP_ERR_INVALID_STATE; /* 契约 §1.2.7：流式期间拒绝 PCM/MP3 */
    }

    play_job_t job = {
        .kind = PLAY_JOB_PCM,
        .pcm = pcm,
        .samples = samples,
        .rate = sample_rate,
    };
    if (xQueueSend(s_queue, &job, 0) != pdTRUE) {
        ESP_LOGW(TAG, "queue full, drop playback request");
        return ESP_ERR_TIMEOUT;
    }
    s_busy = true;
    return ESP_OK;
}

esp_err_t audio_player_play_mp3(const uint8_t *mp3, size_t len)
{
    if (!s_queue || !mp3 || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!mp3_dec_ready()) {
        return ESP_ERR_NOT_SUPPORTED;
    }
    if (s_stream_active) {
        return ESP_ERR_INVALID_STATE;
    }
    /* 解码输出复用同一块 PSRAM 缓冲：播放中再发会覆盖正在播的数据 */
    if (s_busy || uxQueueMessagesWaiting(s_queue) > 0) {
        ESP_LOGW(TAG, "player busy, reject mp3 job");
        return ESP_ERR_INVALID_STATE;
    }

    play_job_t job = {
        .kind = PLAY_JOB_MP3,
        .mp3 = mp3,
        .mp3_len = len,
    };
    if (xQueueSend(s_queue, &job, 0) != pdTRUE) {
        ESP_LOGW(TAG, "queue full, drop mp3 job");
        return ESP_ERR_TIMEOUT;
    }
    s_busy = true;
    ESP_LOGI(TAG, "mp3 job queued: %u bytes", (unsigned)len);
    return ESP_OK;
}

bool audio_player_is_busy(void)
{
    return s_busy;
}

bool audio_player_mp3_ready(void)
{
    return mp3_dec_ready();
}

/* ---------------- 流式对外 API（生产者侧，任意任务） ---------------- */

esp_err_t audio_player_stream_open(uint8_t codec, uint32_t sample_rate)
{
    if (codec != 1) {
        return ESP_ERR_INVALID_ARG; /* 当前唯一实现：1=MP3 */
    }
    if (sample_rate == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_queue) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_stream_active || audio_player_is_busy()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!audio_player_mp3_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_ring || !s_stage || !s_end_ring || !s_rd_scratch) {
        return ESP_ERR_NO_MEM;
    }

    portENTER_CRITICAL(&s_stream_mux);
    s_stream_active = true;
    s_stream_end_req = false;
    s_stream_abort_req = false;
    s_stream_resync_req = false;
    s_stream_wr = 0;
    s_stream_rd = 0;
    s_stream_first_byte_us = 0;
    s_stream_sound_us = 0;
    s_stream_finish_us = 0;
    s_stream_frame_bytes_ema = 0;
    s_stream_frame_ms = 0;
    s_stream_ring_high_bytes = 0;
    memset(&s_stream_stats, 0, sizeof(s_stream_stats));
    s_stream_stats.active = true;
    portEXIT_CRITICAL(&s_stream_mux);

    play_job_t job = {
        .kind = PLAY_JOB_STREAM,
        .codec = codec,
        .stream_rate = sample_rate,
    };
    if (xQueueSend(s_queue, &job, 0) != pdTRUE) {
        portENTER_CRITICAL(&s_stream_mux);
        s_stream_active = false;
        portEXIT_CRITICAL(&s_stream_mux);
        ESP_LOGW(TAG, "queue full, reject stream open");
        return ESP_ERR_TIMEOUT;
    }
    s_busy = true;
    ESP_LOGI(TAG, "stream open codec=mp3 sr=%u", (unsigned)sample_rate);
    return ESP_OK;
}

esp_err_t audio_player_stream_write(const uint8_t *data, size_t len)
{
    if (!data || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (len > STREAM_RD_CHUNK) {
        return ESP_ERR_INVALID_ARG; /* 单次 ≤2KB（契约 §1.2.2） */
    }

    bool full = false;
    portENTER_CRITICAL(&s_stream_mux);
    if (!s_stream_active || s_stream_end_req) {
        portEXIT_CRITICAL(&s_stream_mux);
        return ESP_ERR_INVALID_STATE;
    }
    const uint32_t used = s_stream_wr - s_stream_rd;
    if ((uint32_t)STREAM_RING_BYTES - used < (uint32_t)len) {
        /* ring 满：丢弃整段 + 计数 + 要求解码器重新同步 */
        s_stream_stats.dropped_bytes += (uint32_t)len;
        s_stream_resync_req = true;
        full = true;
    } else {
        const uint32_t off = s_stream_wr % STREAM_RING_BYTES;
        const size_t first = (STREAM_RING_BYTES - off) < len ? (STREAM_RING_BYTES - off) : len;
        memcpy(s_ring + off, data, first);
        if (len > first) {
            memcpy(s_ring, data + first, len - first);
        }
        s_stream_wr += (uint32_t)len;
        s_stream_stats.fed_frames++;
        s_stream_stats.fed_bytes += (uint32_t)len;
        if (s_stream_first_byte_us == 0) {
            s_stream_first_byte_us = esp_timer_get_time();
        }
        if (s_stream_wr - s_stream_rd > s_stream_ring_high_bytes) {
            s_stream_ring_high_bytes = s_stream_wr - s_stream_rd;
        }
    }
    portEXIT_CRITICAL(&s_stream_mux);

    if (full) {
        const int64_t now = esp_timer_get_time();
        if (now - s_stream_drop_warn_us >= 500000) {
            s_stream_drop_warn_us = now;
            ESP_LOGW(TAG, "stream write drop %u B (ring full)", (unsigned)len);
        }
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t audio_player_stream_end(void)
{
    if (!s_stream_active) {
        return ESP_ERR_INVALID_STATE;
    }
    portENTER_CRITICAL(&s_stream_mux);
    s_stream_end_req = true;
    portEXIT_CRITICAL(&s_stream_mux);
    return ESP_OK;
}

esp_err_t audio_player_stream_abort(void)
{
    if (!s_stream_active) {
        return ESP_ERR_INVALID_STATE;
    }
    portENTER_CRITICAL(&s_stream_mux);
    s_stream_abort_req = true;
    portEXIT_CRITICAL(&s_stream_mux);
    return ESP_OK;
}

bool audio_player_stream_active(void)
{
    return s_stream_active;
}

esp_err_t audio_player_stream_stats_read(audio_player_stream_stats_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_stream_mux);
    *out = s_stream_stats;
    out->active = s_stream_active;
    const uint32_t ring_bytes = s_stream_wr - s_stream_rd;
    if (s_stream_frame_bytes_ema && s_stream_frame_ms) {
        out->ring_level_ms = (uint32_t)((uint64_t)ring_bytes * s_stream_frame_ms /
                                        s_stream_frame_bytes_ema);
        out->ring_high_ms = (uint32_t)((uint64_t)s_stream_ring_high_bytes * s_stream_frame_ms /
                                       s_stream_frame_bytes_ema);
    } else {
        out->ring_level_ms = 0;
        out->ring_high_ms = 0;
    }
    if (s_stream_active && s_stream_sound_us) {
        out->duration_ms = (uint32_t)((esp_timer_get_time() - s_stream_sound_us) / 1000);
    }
    portEXIT_CRITICAL(&s_stream_mux);
    return ESP_OK;
}

esp_err_t audio_player_mp3_pcm_read(int16_t *dst, size_t offset, size_t count)
{
    if (!dst || count == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_last_mp3_pcm || offset + count > s_last_mp3_samples) {
        return ESP_ERR_INVALID_STATE;
    }
    memcpy(dst, s_last_mp3_pcm + offset, count * sizeof(int16_t));
    return ESP_OK;
}

esp_err_t audio_player_stats_read(audio_player_stats_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_stats_mux);
    *out = s_stats;
    portEXIT_CRITICAL(&s_stats_mux);
    return ESP_OK;
}

void audio_player_stats_reset(void)
{
    portENTER_CRITICAL(&s_stats_mux);
    memset(&s_stats, 0, sizeof(s_stats));
    portEXIT_CRITICAL(&s_stats_mux);
    mp3_dec_stats_reset();

    portENTER_CRITICAL(&s_stream_mux);
    memset(&s_stream_stats, 0, sizeof(s_stream_stats));
    s_stream_ring_high_bytes = s_stream_wr - s_stream_rd;
    s_stream_stats.active = s_stream_active;
    portEXIT_CRITICAL(&s_stream_mux);
}

esp_err_t audio_player_set_volume(uint8_t volume)
{
    if (volume > 100) {
        return ESP_ERR_INVALID_ARG;
    }
    s_volume = volume;
    ESP_LOGI(TAG, "volume=%u", (unsigned)volume);
    return ESP_OK;
}

uint8_t audio_player_get_volume(void)
{
    return s_volume;
}
