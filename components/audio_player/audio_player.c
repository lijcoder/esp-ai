/*
 * 播放引擎（T3.3 三通道混音）：把提示音 / TTS / 音乐统一成三个 48k 单声道通道，
 * 混音 → 音量 → 软限幅 → I2S 输出。
 *
 *   CH_TTS / CH_PROMPT / CH_MUSIC（PSRAM ring）
 *        └─> 饱和相加 ─> 音量(0~100，20ms 线性插值) ─> 软限幅(-1dBFS) ─> I2S1 48k 立体声
 *
 * 任务模型：单播放任务（核 1/优先级 5），一次只跑一个"作业"（提示音 PCM / 整段 MP3 /
 * TTS 流 / 混音探针泵）；解码、重采样、混音、写 I2S 全部串行，不新增任务
 * （内部 RAM 水位只有 ~2.9KB 余量，见 CONVENTIONS §4）。
 *
 * 内存：输出块在内部 RAM（I2S DMA 需要）；通道 ring、MP3 解码状态/临时区、流式 ring
 * 全部 PSRAM（player 任务内不做 malloc）。
 *
 * 半双工（T3.2 语义，T3.3 保留）：
 * - R1/R2：由业务层 audio_fe_feed_hold/release 做门控，完成回调（尾部 300ms 延时）后释放；
 * - R4：audio_player_fade_out_and_stop() 在混音输出级线性淡出，淡完即停（last_stop_ms 可观测）；
 * - R3：duck 不再压整个输出，而是 CH_MUSIC 的通道增益（100ms 斜坡），为 P4 音乐叠加做准备。
 *
 * 流式（T2.4）关键设计不变：生产者只拷 PSRAM ring，解码在 player 任务里按 I2S 节奏拉；
 * 起播预缓冲 4 帧或 120ms；欠载补静音；tts_end 后解完即收尾（100ms 淡出由通道包络统一做）。
 */

#include "audio_player.h"

#include <math.h>
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

#define PLAYER_TASK_STACK 6144
#define PLAYER_TASK_PRIO  5
#define PLAYER_CORE       1
#define PLAYER_QUEUE_LEN  4
#define OUT_FRAMES_CHUNK  480 /* 10ms @48k */

/* ---------------- 混音参数（T3.3） ----------------
 * ring 取 2 的幂（8192 帧 = 170ms @48k），下标用掩码而不是取模：
 * 混音是每 10ms 跑 3 通道 × 480 样本的热路径，避免 32 位除法。 */
#define MIX_RING_FRAMES    8192u
#define MIX_RING_MASK      (MIX_RING_FRAMES - 1u)
#define FADE_IN_FRAMES     (AUDIO_IO_SPK_SAMPLE_RATE / 100u) /* 10ms */
#define FADE_OUT_FRAMES    (AUDIO_IO_SPK_SAMPLE_RATE / 10u)  /* 100ms */
#define VOL_RAMP_FRAMES    (AUDIO_IO_SPK_SAMPLE_RATE / 50u)  /* 20ms（ARCH §3.5） */
#define GAIN_RAMP_FRAMES   (CONFIG_AUDIO_PLAYER_DUCK_RAMP_MS * AUDIO_IO_SPK_SAMPLE_RATE / 1000u) /* 通道 duck 斜坡 */
#define LIMIT_CEIL         29196 /* -1dBFS = 32767 × 10^(-1/20) */
#define LIMIT_KNEE         26276 /* 0.9×ceiling：以下完全透明，以上压向 ceiling */
#define LIMIT_SPAN         (LIMIT_CEIL - LIMIT_KNEE)
#define MIX_CAP_MAX_FRAMES 16384 /* 探针抓取上限 341ms @48k */
#define MIX_TONE_MAX_MS    10000u
#define PEAK_RING_LEN      32 /* 32 × 10ms = 320ms 输出峰值窗口 */

/* ---------------- 流式参数（T2.4） ---------------- */
#define STREAM_RING_BYTES        CONFIG_AUDIO_PLAYER_STREAM_RING_BYTES
#define STREAM_RD_CHUNK          2048 /* 单次临界区 memcpy 上限（契约 §1.2.2） */
#define STREAM_MAX_FRAME_SAMPLES 1152 /* MPEG Layer III 单帧最多 1152 样本/声道 */
#define STREAM_STAGE_SAMPLES     ((CONFIG_AUDIO_PLAYER_STREAM_PREROLL_FRAMES + 1) * STREAM_MAX_FRAME_SAMPLES)
#define STREAM_DONE_EXTRA_SLOTS  3

typedef enum {
    PLAY_JOB_PCM = 0, /* 调用方持有一段 PCM（提示音） */
    PLAY_JOB_MP3,     /* 调用方持有一段 MP3 码流，播放任务内解码（探针/音乐整段） */
    PLAY_JOB_STREAM,  /* 流式播放（T2.4），实际数据在 PSRAM ring 里 */
    PLAY_JOB_PUMP,    /* 混音探针泵（T3.3 player mix tone），无完成回调 */
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

/* ---------------- 混音通道（T3.3） ---------------- */

typedef enum {
    MIX_SRC_NONE = 0,
    MIX_SRC_PCM,   /* 一段 PCM（提示音 / 整段 MP3 解码结果），播放任务内线性重采样到 48k */
    MIX_SRC_TONE,  /* 正弦音调（仅开发期探针） */
    MIX_SRC_STREAM /* TTS 流：由 tts_pump 解码后填 ring */
} mix_src_t;

typedef struct {
    int16_t *ring;            /* PSRAM：48k 单声道 ring */
    volatile uint32_t wr, rd; /* 单调帧计数（仅 player 任务写） */

    /* 外部设置（diag/作业）的请求，由 s_mix_mux 保护；req_gen 变更即重启通道 */
    bool req_active;
    uint32_t req_gen;
    mix_src_t req_src;
    const int16_t *req_pcm;
    uint32_t req_samples;
    uint32_t req_rate;
    uint32_t req_tone_freq;
    uint32_t req_tone_ms;
    uint32_t req_tone_amp;

    /* 以下全部由 player 任务独占 */
    bool run_active;
    uint32_t run_gen;
    bool closed;         /* 源已耗尽（混完 ring 即结束） */
    bool fade_started;   /* 收尾 100ms 淡出已开始 */
    mix_src_t src;
    const int16_t *pcm;
    uint32_t pcm_samples;
    uint32_t pcm_rate;
    uint64_t src_pos;   /* 已生产 48k 帧 */
    uint64_t src_total; /* 总 48k 帧 */
    uint32_t tone_freq;
    uint32_t tone_amp;
    uint64_t tone_pos;
    uint64_t tone_total;
    uint32_t tone_phase; /* q32 相位 */
    uint32_t tone_step;  /* q32 每样本相位步进（启动时按频率算一次） */
    uint32_t fade_in_env;   /* q16，65536=满 */
    uint32_t fade_out_env;  /* q16，65536=满 */
    uint32_t fade_out_left; /* 剩余淡出帧 */
    uint32_t fade_out_total;
    uint32_t fade_out_step; /* 每帧 q16 递减 */
    uint32_t gain_q16;      /* 通道增益（q16，65536=0dB） */
    uint32_t gain_tgt_q16;
    uint32_t gain_ramp_tgt; /* 上次启动斜坡时的目标（目标变化才重算步进） */
    int32_t gain_step;      /* 每帧 q16 步进（目标变化时按 100ms 线性算一次） */
    uint32_t peak;          /* 重置以来该通道贡献峰值 */
} mix_ch_t;

static mix_ch_t s_ch[AUDIO_PLAYER_CH_NUM];
static portMUX_TYPE s_mix_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool s_mix_pump_running; /* 泵循环在跑（含正在启动） */
static volatile bool s_mix_pump_job;     /* player 任务正持有 PUMP 作业（含收尾冲刷） */
static volatile bool s_mix_stop_req;

/* 256 点正弦表 Q15（sin(2πi/256)×32767），用于探针音调；放 rodata 不占内部 RAM */
static const int16_t s_sine_q15[256] = {
    0, 804, 1608, 2410, 3212, 4011, 4808, 5602,
    6393, 7179, 7962, 8739, 9512, 10278, 11039, 11793,
    12539, 13279, 14010, 14732, 15446, 16151, 16846, 17530,
    18204, 18868, 19519, 20159, 20787, 21403, 22005, 22594,
    23170, 23731, 24279, 24811, 25329, 25832, 26319, 26790,
    27245, 27683, 28105, 28510, 28898, 29268, 29621, 29956,
    30273, 30571, 30852, 31113, 31356, 31580, 31785, 31971,
    32137, 32285, 32412, 32521, 32609, 32678, 32728, 32757,
    32767, 32757, 32728, 32678, 32609, 32521, 32412, 32285,
    32137, 31971, 31785, 31580, 31356, 31113, 30852, 30571,
    30273, 29956, 29621, 29268, 28898, 28510, 28105, 27683,
    27245, 26790, 26319, 25832, 25329, 24811, 24279, 23731,
    23170, 22594, 22005, 21403, 20787, 20159, 19519, 18868,
    18204, 17530, 16846, 16151, 15446, 14732, 14010, 13279,
    12539, 11793, 11039, 10278, 9512, 8739, 7962, 7179,
    6393, 5602, 4808, 4011, 3212, 2410, 1608, 804,
    0, -804, -1608, -2410, -3212, -4011, -4808, -5602,
    -6393, -7179, -7962, -8739, -9512, -10278, -11039, -11793,
    -12539, -13279, -14010, -14732, -15446, -16151, -16846, -17530,
    -18204, -18868, -19519, -20159, -20787, -21403, -22005, -22594,
    -23170, -23731, -24279, -24811, -25329, -25832, -26319, -26790,
    -27245, -27683, -28105, -28510, -28898, -29268, -29621, -29956,
    -30273, -30571, -30852, -31113, -31356, -31580, -31785, -31971,
    -32137, -32285, -32412, -32521, -32609, -32678, -32728, -32757,
    -32767, -32757, -32728, -32678, -32609, -32521, -32412, -32285,
    -32137, -31971, -31785, -31580, -31356, -31113, -30852, -30571,
    -30273, -29956, -29621, -29268, -28898, -28510, -28105, -27683,
    -27245, -26790, -26319, -25832, -25329, -24811, -24279, -23731,
    -23170, -22594, -22005, -21403, -20787, -20159, -19519, -18868,
    -18204, -17530, -16846, -16151, -15446, -14732, -14010, -13279,
    -12539, -11793, -11039, -10278, -9512, -8739, -7962, -7179,
    -6393, -5602, -4808, -4011, -3212, -2410, -1608, -804,
};

/* ---------------- 全局状态 ---------------- */

static QueueHandle_t s_queue = NULL;
static TaskHandle_t s_task = NULL;
static volatile bool s_busy = false;
static uint8_t s_volume = CONFIG_AUDIO_PLAYER_DEFAULT_VOLUME;
static uint32_t s_vol_q16 = 65536;      /* 当前生效音量（q16，65536=100%，含插值） */
static uint32_t s_vol_tgt_q16 = 65536;  /* 目标音量 */
static uint32_t s_vol_q8 = 256;

/* 完成回调：主槽（覆盖语义）+ 3 个附加槽；快照后回调，避免回调里增删导致遍历失效 */
static portMUX_TYPE s_done_mux = portMUX_INITIALIZER_UNLOCKED;
static audio_player_done_cb_t s_done_cb = NULL;
static void *s_done_arg = NULL;
static done_slot_t s_done_extra[STREAM_DONE_EXTRA_SLOTS];

/* 统计：播放任务写、diag 读，用自旋锁保护整结构体拷贝（临界区只有几十字节） */
static portMUX_TYPE s_stats_mux = portMUX_INITIALIZER_UNLOCKED;
static audio_player_stats_t s_stats;
static audio_player_mix_stats_t s_mix_snap; /* 混音快照（与 s_stats 共用 s_stats_mux） */

/* 最近一次 MP3 解码结果（PCM 指向 decoder_mp3 的静态 PSRAM 缓冲，仅探针对拍用） */
static const int16_t *s_last_mp3_pcm;
static size_t s_last_mp3_samples;

/* ---------------- T3.2：打断淡出（R4）+ duck（R3）+ 输出观测 ----------------
 * 线程模型：请求方（evt/console 任务）只写 total/left + 记录时间戳；
 * 递减与停播判断全部在 player 任务里，临界区只在每 10ms 块首尾各一次。
 */
static portMUX_TYPE s_stop_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t s_stop_total_frames; /* 淡出总帧数；0 = 无请求 */
static volatile uint32_t s_stop_left_frames;  /* 剩余淡出帧数（player 任务递减） */
static uint32_t s_stop_acc_q16;               /* 已淡出比例（q16，player 任务写） */
static uint32_t s_stop_step_q16;              /* 每帧淡出步进（q16） */
static int64_t s_stop_req_us;                 /* 淡出请求时刻 */
static uint32_t s_last_stop_ms;               /* 请求 → 输出静音（ms） */
static uint32_t s_stop_count;                 /* 淡出停止次数 */
static uint32_t s_peak_ring[PEAK_RING_LEN];
static uint32_t s_peak_idx;

static volatile bool s_duck_on;      /* 手动 duck（player mix duck 探针 / R3 探针） */
static uint32_t s_duck_target_q16 = 16384; /* -12dB ≈ 0.25（init 里按 Kconfig 算） */

static volatile uint32_t s_cur_src; /* 正在播的 audio_player_src_t（忙时有效） */

/* ---------------- 探针抓取（T3.3） ---------------- */
static int16_t *s_cap_buf; /* PSRAM，首次使用分配 */
static uint32_t s_cap_len;
static uint32_t s_cap_pos;
static bool s_cap_armed;

/* ---------------- 流式状态（T2.4） ---------------- */
static uint8_t *s_ring;       /* PSRAM：下行压缩数据 ring */
static int16_t *s_stage;      /* PSRAM：待播源样本（预缓冲 + 单帧中转） */
static uint8_t *s_rd_scratch; /* PSRAM：ring 读取暂存（临界区外再喂解码器） */
static int16_t *s_tts_tmp;    /* PSRAM：TTS 通道泵中转（480 帧） */

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
} stream_ctx_t;

static stream_ctx_t s_tts_ctx;     /* TTS 流解码上下文（player 任务独占） */
static bool s_tts_in_underrun;     /* 连续欠载计数去重 */

/* ---------------- 基础工具 ---------------- */

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

/* 软限幅：|x| ≤ KNEE 完全透明；超过后按有理函数渐近压向 -1dBFS 上限（无硬削波）。
 * 用整数比例映射（跨越 3 通道满幅相加时输入可达 ±98301），不引入逐样本除法以外的浮点。 */
static inline int32_t mix_soft_limit(int32_t x)
{
    const int32_t a = (x < 0) ? -x : x;
    if (a <= LIMIT_KNEE) {
        return x;
    }
    const int32_t over = a - LIMIT_KNEE;
    int32_t y = LIMIT_KNEE + (LIMIT_SPAN * over) / (over + LIMIT_SPAN);
    if (y > LIMIT_CEIL) {
        y = LIMIT_CEIL;
    }
    return (x < 0) ? -y : y;
}

static inline void ch_ring_put(mix_ch_t *c, int16_t v)
{
    c->ring[c->wr & MIX_RING_MASK] = v;
    c->wr++;
}

/* ---------------- 通道泵（把源转成 48k 单声道写进 ring） ---------------- */

static inline int32_t tone_next(mix_ch_t *c)
{
    const uint32_t idx = (c->tone_phase >> 24) & 0xFFu;
    const uint32_t frac = (c->tone_phase >> 9) & 0x7FFFu; /* q15 表内插值 */
    const int32_t s0 = s_sine_q15[idx];
    const int32_t s1 = s_sine_q15[(idx + 1u) & 0xFFu];
    const int32_t v = s0 + (int32_t)(((int32_t)(s1 - s0) * (int32_t)frac) >> 15);
    c->tone_phase += c->tone_step;
    return v * (int32_t)c->tone_amp / 32768;
}

/* 每块最多补 2 个 10ms，避免长解码把单块时间拉长 */
static void ch_pump(mix_ch_t *c)
{
    if (!c->run_active || c->closed) {
        return;
    }
    if (c->src == MIX_SRC_STREAM) {
        return; /* TTS 流由 tts_pump 填 */
    }
    for (int blocks = 0; blocks < 2; blocks++) {
        if ((MIX_RING_FRAMES - (c->wr - c->rd)) < OUT_FRAMES_CHUNK) {
            break;
        }
        uint32_t n = OUT_FRAMES_CHUNK;
        if (c->src == MIX_SRC_PCM) {
            if (c->src_pos >= c->src_total) {
                c->closed = true;
                break;
            }
            const uint64_t left = c->src_total - c->src_pos;
            if (left < n) {
                n = (uint32_t)left;
            }
            for (uint32_t i = 0; i < n; i++) {
                /* 线性重采样：src_pos = out_pos × rate / 48k（与旧 play_block 同式） */
                const uint64_t pos = c->src_pos + i;
                const uint64_t num = pos * c->pcm_rate;
                const uint32_t sp = (uint32_t)(num / AUDIO_IO_SPK_SAMPLE_RATE);
                const uint32_t fr = (uint32_t)(num % AUDIO_IO_SPK_SAMPLE_RATE);
                const int32_t s0 = c->pcm[sp];
                const int32_t s1 = ((sp + 1) < c->pcm_samples) ? c->pcm[sp + 1] : s0;
                const int32_t v = s0 + (int32_t)(((int64_t)(s1 - s0) * (int32_t)fr) /
                                                 AUDIO_IO_SPK_SAMPLE_RATE);
                ch_ring_put(c, (int16_t)v);
            }
            c->src_pos += n;
            if (c->src_pos >= c->src_total) {
                c->closed = true;
            }
        } else if (c->src == MIX_SRC_TONE) {
            if (c->tone_pos >= c->tone_total) {
                c->closed = true;
                break;
            }
            const uint64_t left = c->tone_total - c->tone_pos;
            if (left < n) {
                n = (uint32_t)left;
            }
            for (uint32_t i = 0; i < n; i++) {
                ch_ring_put(c, (int16_t)tone_next(c));
            }
            c->tone_pos += n;
            if (c->tone_pos >= c->tone_total) {
                c->closed = true;
            }
        } else {
            c->closed = true;
            break;
        }
    }
}

/* 每块开始前同步外部请求：启动新通道 / 停止被撤销的通道 */
static void ch_sync(void)
{
    for (int i = 0; i < AUDIO_PLAYER_CH_NUM; i++) {
        mix_ch_t *c = &s_ch[i];
        bool req;
        uint32_t gen;
        mix_src_t src;
        const int16_t *pcm;
        uint32_t samples;
        uint32_t rate;
        uint32_t tf;
        uint32_t tms;
        uint32_t ta;

        portENTER_CRITICAL(&s_mix_mux);
        req = c->req_active;
        gen = c->req_gen;
        src = c->req_src;
        pcm = c->req_pcm;
        samples = c->req_samples;
        rate = c->req_rate;
        tf = c->req_tone_freq;
        tms = c->req_tone_ms;
        ta = c->req_tone_amp;
        portEXIT_CRITICAL(&s_mix_mux);

        if (req && (!c->run_active || c->run_gen != gen)) {
            c->wr = 0;
            c->rd = 0;
            c->run_gen = gen;
            c->closed = false;
            c->fade_started = false;
            c->fade_in_env = 0;
            c->fade_out_left = 0;
            c->fade_out_env = 65536;
            c->fade_out_total = 0;
            c->fade_out_step = 1;
            c->gain_q16 = 65536;
            c->gain_tgt_q16 = 65536;
            c->gain_ramp_tgt = 65536;
            c->gain_step = 0;
            c->src = src;
            c->src_pos = 0;
            c->src_total = 0;
            c->tone_pos = 0;
            c->tone_total = 0;
            c->tone_phase = 64u << 24; /* 从峰值开始（cos），用于验证 10ms 淡入 */
            if (src == MIX_SRC_PCM) {
                c->pcm = pcm;
                c->pcm_samples = samples;
                c->pcm_rate = rate;
                c->src_total = (uint64_t)samples * AUDIO_IO_SPK_SAMPLE_RATE / rate;
            } else if (src == MIX_SRC_TONE) {
                c->tone_freq = tf;
                c->tone_amp = ta;
                c->tone_total = (uint64_t)tms * AUDIO_IO_SPK_SAMPLE_RATE / 1000u;
                c->tone_step = (uint32_t)(((uint64_t)tf << 32) / AUDIO_IO_SPK_SAMPLE_RATE);
            }
            c->run_active = true;
        } else if (!req && c->run_active) {
            c->run_active = false;
            c->closed = true;
        }
    }
}

/* 收尾判定：源耗尽后标 closed；closed 且残留 ≤100ms 时启动淡出；播空即结束 */
static void ch_update_end(void)
{
    for (int i = 0; i < AUDIO_PLAYER_CH_NUM; i++) {
        mix_ch_t *c = &s_ch[i];
        if (!c->run_active) {
            continue;
        }
        const uint32_t level = c->wr - c->rd;
        if (c->closed) {
            if (level == 0) {
                c->run_active = false;
                portENTER_CRITICAL(&s_mix_mux);
                /* 新请求（req_gen 变化）不在本轮清掉，交给 ch_sync 下一块重启通道 */
                if (c->req_gen == c->run_gen) {
                    c->req_active = false;
                }
                portEXIT_CRITICAL(&s_mix_mux);
            } else if (!c->fade_started && level <= FADE_OUT_FRAMES) {
                c->fade_started = true;
                c->fade_out_total = level;
                c->fade_out_left = level;
                c->fade_out_env = 65536;
                /* 向上取整：保证淡出在剩余帧内走到 0（截断会让末样本残留 ~5% 稳态幅度） */
                const uint32_t step = (65536u + level - 1u) / level;
                c->fade_out_step = (step != 0) ? step : 1u;
            }
        }
    }
}

/* 音量斜坡：目标变化时按 20ms 线性算一次步进（块内不按剩余差重算） */
static uint32_t s_vol_ramp_tgt = 65536; /* 上次计算步进时的目标 */
static int32_t s_vol_step = 0;          /* 每帧 q16 步进 */

/* ---------------- 混音输出（每 10ms 一块） ----------------
 * 把三通道按包络/duck 增益相加 → 音量（20ms 插值）→ 软限幅（-1dBFS）→ R4 淡出 → 立体声。
 * 返回 true 表示 R4 淡出已完成（调用方应立刻停播）。
 * 只允许在 player 任务上下文调用。
 */
static bool mix_render(int16_t *out, uint32_t frames)
{
    uint32_t total;
    uint32_t left;

    portENTER_CRITICAL(&s_stop_mux);
    total = s_stop_total_frames;
    left = s_stop_left_frames;
    if (total > 0 && left == 0) {
        portEXIT_CRITICAL(&s_stop_mux);
        memset(out, 0, (size_t)frames * 2 * sizeof(int16_t)); /* 兜底：淡完后不再放出声音 */
        return true;
    }
    portEXIT_CRITICAL(&s_stop_mux);

    /* 通道增益目标（语音优先：音乐 duck；提示音在 TTS 播放时让位 -6dB） */
    const bool tts_on = s_ch[AUDIO_PLAYER_CH_TTS].run_active;
    const bool prompt_on = s_ch[AUDIO_PLAYER_CH_PROMPT].run_active;
    const bool voice_on = tts_on || prompt_on;
    s_ch[AUDIO_PLAYER_CH_TTS].gain_tgt_q16 = 65536u;
    s_ch[AUDIO_PLAYER_CH_PROMPT].gain_tgt_q16 = tts_on ? 32768u : 65536u;
    s_ch[AUDIO_PLAYER_CH_MUSIC].gain_tgt_q16 =
        (s_duck_on || voice_on) ? s_duck_target_q16 : 65536u;
    for (int i = 0; i < AUDIO_PLAYER_CH_NUM; i++) {
        mix_ch_t *c = &s_ch[i];
        if (!c->run_active) {
            continue;
        }
        /* 只在目标变化时重算步进：按 100ms 线性走完（每块按剩余差重算会变成指数逼近，
         * 真机实测 240ms 仍未到位，duck 稳态断言会偏高） */
        if (c->gain_ramp_tgt != c->gain_tgt_q16) {
            c->gain_ramp_tgt = c->gain_tgt_q16;
            const int32_t d = (int32_t)c->gain_tgt_q16 - (int32_t)c->gain_q16;
            int32_t step = d / (int32_t)GAIN_RAMP_FRAMES;
            if (step == 0 && d != 0) {
                step = (d > 0) ? 1 : -1;
            }
            c->gain_step = step;
        }
    }

    /* 音量 20ms 线性插值：目标变化时重算一次步进，块内逐样本推进 */
    const uint32_t vol_tgt = s_vol_tgt_q16;
    uint32_t vol = s_vol_q16;
    if (s_vol_ramp_tgt != vol_tgt) {
        s_vol_ramp_tgt = vol_tgt;
        const int32_t d = (int32_t)vol_tgt - (int32_t)vol;
        int32_t step = d / (int32_t)VOL_RAMP_FRAMES;
        if (step == 0 && d != 0) {
            step = (d > 0) ? 1 : -1;
        }
        s_vol_step = step;
    }
    const int32_t vstep = s_vol_step;

    /* R4 淡出：q16 比例递减，避免逐样本除法 */
    uint32_t stop_acc = s_stop_acc_q16;
    bool fade_done = false;

    uint32_t peak = 0;
    uint32_t lim_cnt = 0;
    uint32_t lim_peak = 0;
    uint32_t clip_cnt = 0;
    uint32_t ch_peak[AUDIO_PLAYER_CH_NUM] = {0};

    for (uint32_t i = 0; i < frames; i++) {
        int32_t acc = 0;
        for (int ch = 0; ch < AUDIO_PLAYER_CH_NUM; ch++) {
            mix_ch_t *c = &s_ch[ch];
            if (!c->run_active) {
                continue;
            }
            if (c->wr == c->rd) {
                continue; /* 欠载/暂时没数据：本块该通道贡献静音 */
            }
            int32_t s = c->ring[c->rd & MIX_RING_MASK];
            c->rd++;

            /* 10ms 淡入 */
            if (c->fade_in_env < 65536u) {
                s = ((int32_t)s * (int32_t)c->fade_in_env) >> 16;
                c->fade_in_env += (65536u / FADE_IN_FRAMES);
                if (c->fade_in_env > 65536u) {
                    c->fade_in_env = 65536u;
                }
            }
            /* 100ms 收尾淡出 */
            if (c->fade_out_left > 0) {
                s = ((int32_t)s * (int32_t)c->fade_out_env) >> 16;
                if (c->fade_out_env > c->fade_out_step) {
                    c->fade_out_env -= c->fade_out_step;
                } else {
                    c->fade_out_env = 0;
                }
                c->fade_out_left--;
            }
            /* 通道增益（duck，100ms 斜坡） */
            if (c->gain_q16 != c->gain_tgt_q16) {
                int32_t g = (int32_t)c->gain_q16 + c->gain_step;
                if ((c->gain_step > 0 && g > (int32_t)c->gain_tgt_q16) ||
                    (c->gain_step < 0 && g < (int32_t)c->gain_tgt_q16)) {
                    g = (int32_t)c->gain_tgt_q16;
                }
                c->gain_q16 = (uint32_t)g;
            }
            s = ((int32_t)s * (int32_t)c->gain_q16) >> 16;
            acc += s;

            const uint32_t a = (s < 0) ? (uint32_t)(-s) : (uint32_t)s;
            if (a > ch_peak[ch]) {
                ch_peak[ch] = a;
            }
        }

        /* 音量（混音后统一施加；用有符号中间值避免 uint32 下溢后回绕成巨大音量） */
        if (vol != vol_tgt) {
            int32_t v = (int32_t)vol + vstep;
            if ((vstep > 0 && v > (int32_t)vol_tgt) || (vstep < 0 && v < (int32_t)vol_tgt)) {
                v = (int32_t)vol_tgt;
            }
            vol = (uint32_t)v;
        }
        acc = (int32_t)(((int64_t)acc * (int32_t)vol) >> 16);

        /* 软限幅 -1dBFS */
        const int32_t aa = (acc < 0) ? -acc : acc;
        if (aa > LIMIT_KNEE) {
            lim_cnt++;
            if ((uint32_t)aa > lim_peak) {
                lim_peak = (uint32_t)aa;
            }
            acc = mix_soft_limit(acc);
        }

        /* R4 全局淡出 */
        if (total > 0) {
            const uint32_t factor = 65536u - (stop_acc > 65536u ? 65536u : stop_acc);
            acc = (int32_t)(((int64_t)acc * (int32_t)factor) >> 16);
            stop_acc += s_stop_step_q16;
            if (stop_acc >= 65536u) {
                stop_acc = 65536u;
                fade_done = true;
            }
        }

        /* 硬削波自证（设计上不应发生，统计出来就是 bug） */
        if (acc > 32767) {
            acc = 32767;
            clip_cnt++;
        } else if (acc < -32768) {
            acc = -32768;
            clip_cnt++;
        }

        out[i * 2] = (int16_t)acc;
        out[i * 2 + 1] = (int16_t)acc;

        const uint32_t a2 = (acc < 0) ? (uint32_t)(-acc) : (uint32_t)acc;
        if (a2 > peak) {
            peak = a2;
        }

        /* 探针抓取：抓混音后的 48k 单声道输出 */
        if (s_cap_armed && s_cap_pos < s_cap_len) {
            s_cap_buf[s_cap_pos++] = (int16_t)acc;
            if (s_cap_pos >= s_cap_len) {
                s_cap_armed = false;
            }
        }
    }

    s_vol_q16 = vol;
    s_vol_q8 = vol >> 8;

    portENTER_CRITICAL(&s_stop_mux);
    if (total > 0) {
        s_stop_acc_q16 = stop_acc;
        s_stop_left_frames = (uint32_t)(((uint64_t)total * (65536u - stop_acc)) >> 16);
        if (fade_done) {
            s_last_stop_ms = (uint32_t)((esp_timer_get_time() - s_stop_req_us) / 1000);
            s_stop_count++;
            s_stop_total_frames = 0;
            s_stop_left_frames = 0;
            s_stop_acc_q16 = 0;
        }
    }
    s_peak_ring[s_peak_idx] = peak;
    s_peak_idx = (s_peak_idx + 1) % PEAK_RING_LEN;
    portEXIT_CRITICAL(&s_stop_mux);

    /* 统计快照（diag player mix stat 用；同一把锁，临界区只拷贝几十字节） */
    portENTER_CRITICAL(&s_stats_mux);
    s_mix_snap.vol = s_volume;
    s_mix_snap.vol_q8 = s_vol_q8;
    s_mix_snap.vol_target_q8 = s_vol_tgt_q16 >> 8;
    s_mix_snap.limiter += lim_cnt;
    s_mix_snap.clip += clip_cnt;
    if (lim_peak > s_mix_snap.limiter_peak) {
        s_mix_snap.limiter_peak = lim_peak;
    }
    for (int ch = 0; ch < AUDIO_PLAYER_CH_NUM; ch++) {
        mix_ch_t *c = &s_ch[ch];
        s_mix_snap.ch[ch].active = c->run_active;
        s_mix_snap.ch[ch].gain_q8 = c->gain_q16 >> 8;
        s_mix_snap.ch[ch].level_ms = (uint32_t)(((uint64_t)(c->wr - c->rd)) * 1000u /
                                                AUDIO_IO_SPK_SAMPLE_RATE);
        if (ch_peak[ch] > c->peak) {
            c->peak = ch_peak[ch];
        }
        s_mix_snap.ch[ch].peak = c->peak;
    }
    portEXIT_CRITICAL(&s_stats_mux);

    return fade_done;
}

/* 前向声明：混音主循环用到的 TTS 泵与请求清理 */
static void mix_tts_pump(uint32_t *produced, esp_err_t *err);
static void ch_clear_pending(int ch);

/* ---------------- 混音主循环 ----------------
 * 一次作业里反复"泵数据 → 混一块 → 写 I2S"，直到三通道全部播空（或被打断）。
 * kind=PLAY_JOB_STREAM 时额外推进 TTS 解码器并处理 abort/end 标志。
 */
static bool mix_loop_run(int16_t *out, play_job_kind_t kind, bool *aborted, bool *i2s_started)
{
    *aborted = false;
    *i2s_started = false;
    bool started = false;
    bool ok = true;
    s_tts_in_underrun = false;

    for (;;) {
        const int64_t t0 = esp_timer_get_time();
        ch_sync();

        if (kind == PLAY_JOB_STREAM) {
            /* R4：已有淡出请求时，vc_cancel 的立即 abort 不得截断淡出 */
            if (s_stream_abort_req && s_stop_total_frames == 0) {
                *aborted = true;
                ok = false;
                break;
            }
            if (s_stream_resync_req) {
                s_stream_resync_req = false;
                mp3_dec_stream_resync();
            }
            mix_ch_t *t = &s_ch[AUDIO_PLAYER_CH_TTS];
            if (t->run_active && !t->closed) {
                uint32_t produced = 0;
                esp_err_t err;
                mix_tts_pump(&produced, &err);
                if (err != ESP_OK && err != ESP_ERR_NOT_FOUND) {
                    ESP_LOGW(TAG, "stream decode failed: %s", esp_err_to_name(err));
                    ok = false;
                    break;
                }
                if (s_stream_end_req && err == ESP_ERR_NOT_FOUND) {
                    t->closed = true; /* tts_end 后解码器已无数据：进入收尾淡出 */
                }
            }
            /* 欠载：TTS 通道该出数据却没有（连续欠载只计一次） */
            if (t->run_active && !t->closed && (t->wr - t->rd) == 0) {
                if (!s_tts_in_underrun) {
                    s_tts_in_underrun = true;
                    portENTER_CRITICAL(&s_stream_mux);
                    s_stream_stats.underruns++;
                    const uint32_t n = s_stream_stats.underruns;
                    portEXIT_CRITICAL(&s_stream_mux);
                    const int64_t now = esp_timer_get_time();
                    if (now - s_stream_underrun_warn_us >= 1000000) {
                        s_stream_underrun_warn_us = now;
                        ESP_LOGW(TAG, "stream underrun #%u (ring 0 ms)", (unsigned)n);
                    }
                }
            } else {
                s_tts_in_underrun = false;
            }
        }

        for (int ch = 0; ch < AUDIO_PLAYER_CH_NUM; ch++) {
            ch_pump(&s_ch[ch]);
        }
        ch_update_end();

        if (s_mix_stop_req) {
            *aborted = true;
            ok = false;
            break;
        }

        /* 退出条件：没有运行中的通道，也没有等待启动的请求（与 tone 接口互斥处理） */
        bool running = false;
        bool pending = false;
        portENTER_CRITICAL(&s_mix_mux);
        for (int ch = 0; ch < AUDIO_PLAYER_CH_NUM; ch++) {
            if (s_ch[ch].run_active) {
                running = true;
            }
            if (s_ch[ch].req_active) {
                pending = true;
            }
        }
        if (!running && !pending) {
            s_mix_pump_running = false;
        }
        portEXIT_CRITICAL(&s_mix_mux);
        if (!running && !pending) {
            break;
        }

        if (!started) {
            audio_io_spk_enable(true);
            started = true;
            *i2s_started = true;
        }

        const bool fade_done = mix_render(out, OUT_FRAMES_CHUNK);
        const int64_t used = esp_timer_get_time() - t0;
        portENTER_CRITICAL(&s_stats_mux);
        if ((uint32_t)used > s_mix_snap.block_max_us) {
            s_mix_snap.block_max_us = (uint32_t)used;
        }
        portEXIT_CRITICAL(&s_stats_mux);

        if (audio_io_write_spk(out, OUT_FRAMES_CHUNK, 100) < 0) {
            ESP_LOGW(TAG, "spk write failed");
            ok = false;
            break;
        }
        if (fade_done) {
            *aborted = true;
            ok = false;
            break;
        }
    }

    /* 收尾：撤销所有请求 + 停通道（ring 丢弃） */
    portENTER_CRITICAL(&s_mix_mux);
    for (int ch = 0; ch < AUDIO_PLAYER_CH_NUM; ch++) {
        s_ch[ch].req_active = false;
    }
    s_mix_pump_running = false;
    portEXIT_CRITICAL(&s_mix_mux);
    for (int ch = 0; ch < AUDIO_PLAYER_CH_NUM; ch++) {
        s_ch[ch].run_active = false;
        s_ch[ch].closed = true;
        s_ch[ch].wr = 0;
        s_ch[ch].rd = 0;
    }
    return ok;
}

/* ---------------- 流式解码（T2.4 内部实现，T3.3 起输出到 CH_TTS ring） ---------------- */

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

/* 产出一块原始 48k 样本（不做音量/限幅/淡入淡出，统一交给混音级） */
static uint32_t stream_produce_block(stream_ctx_t *c, int16_t *dst, esp_err_t *err)
{
    uint32_t n = 0;
    *err = ESP_OK;
    while (n < OUT_FRAMES_CHUNK) {
        esp_err_t e = stream_next_sample(c, &dst[n]);
        if (e != ESP_OK) {
            *err = e;
            break;
        }
        n++;
    }
    return n;
}

/* 预缓冲：已解出 ≥PREROLL_FRAMES 帧 或 距首字节 ≥PREROLL_MS（取先到者）；不出声 */
static stream_result_t stream_preroll(stream_ctx_t *c)
{
    esp_err_t pre_err = ESP_OK;
    for (;;) {
        if (s_stream_abort_req) {
            return STREAM_RESULT_ABORT;
        }
        /* T3.2：起播前就收到 R4 淡出请求（还没出声）→ 直接按打断收尾，无需淡出 */
        if (s_stop_total_frames != 0) {
            return STREAM_RESULT_ABORT;
        }
        while (c->preroll_frames < CONFIG_AUDIO_PLAYER_STREAM_PREROLL_FRAMES) {
            pre_err = stream_decode_to_stage(c);
            if (pre_err != ESP_OK) {
                break;
            }
            c->preroll_frames++;
        }
        if (pre_err != ESP_OK && pre_err != ESP_ERR_NOT_FOUND) {
            return STREAM_RESULT_FAIL;
        }
        if (c->preroll_frames >= CONFIG_AUDIO_PLAYER_STREAM_PREROLL_FRAMES) {
            break;
        }
        if (s_stream_end_req) {
            /* 已经没有后续数据：解出多少播多少；一帧都没有时按失败收尾 */
            return (c->preroll_frames > 0) ? STREAM_RESULT_OK : STREAM_RESULT_FAIL;
        }
        int64_t first_us;
        portENTER_CRITICAL(&s_stream_mux);
        first_us = s_stream_first_byte_us;
        portEXIT_CRITICAL(&s_stream_mux);
        if (first_us != 0 && (esp_timer_get_time() - first_us) >=
                                 (int64_t)CONFIG_AUDIO_PLAYER_STREAM_PREROLL_MS * 1000) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }

    /* 起播：把 stage 里已解出的样本作为首块输入，跨帧重采样状态归零 */
    c->blk = c->stage_len ? s_stage : NULL;
    c->blk_n = c->stage_len;
    c->next_mode = NEXT_FIRST;
    c->rs_i = 0;
    c->rs_frac = 0;
    return STREAM_RESULT_OK;
}

/* TTS 流泵：把解码结果填进 CH_TTS ring（每块最多 2 个 10ms，避免长解码拉长块） */
static void mix_tts_pump(uint32_t *produced, esp_err_t *err)
{
    mix_ch_t *c = &s_ch[AUDIO_PLAYER_CH_TTS];
    *produced = 0;
    *err = ESP_OK;
    for (int blocks = 0; blocks < 2; blocks++) {
        if ((MIX_RING_FRAMES - (c->wr - c->rd)) < OUT_FRAMES_CHUNK) {
            return; /* ring 满：等混音消费 */
        }
        esp_err_t e;
        const uint32_t n = stream_produce_block(&s_tts_ctx, s_tts_tmp, &e);
        for (uint32_t i = 0; i < n; i++) {
            ch_ring_put(c, s_tts_tmp[i]);
        }
        *produced += n;
        if (e != ESP_OK) {
            *err = e;
            return;
        }
    }
}

/* ---------------- 作业执行（player 任务上下文） ---------------- */

/* 撤销指定通道的启动请求（作业提前失败/打断时用） */
static void ch_clear_pending(int ch)
{
    portENTER_CRITICAL(&s_mix_mux);
    s_ch[ch].req_active = false;
    portEXIT_CRITICAL(&s_mix_mux);
}

/* 把一段 PCM 作为指定通道的源（线程安全；gen 变更会在下一块重启该通道） */
static void ch_begin(int ch, mix_src_t src, const int16_t *pcm, uint32_t samples, uint32_t rate,
                     uint32_t tone_freq, uint32_t tone_ms, uint32_t tone_amp)
{
    portENTER_CRITICAL(&s_mix_mux);
    mix_ch_t *c = &s_ch[ch];
    c->req_active = true;
    c->req_src = src;
    c->req_pcm = pcm;
    c->req_samples = samples;
    c->req_rate = rate;
    c->req_tone_freq = tone_freq;
    c->req_tone_ms = tone_ms;
    c->req_tone_amp = tone_amp;
    c->req_gen++;
    portEXIT_CRITICAL(&s_mix_mux);
}

/* 提示音：PCM → CH_PROMPT（日志格式是 tools 的解析契约，不得改动） */
static bool run_pcm_job(const play_job_t *job, int16_t *out, bool *aborted)
{
    ESP_LOGI(TAG, "play %u samples @%u Hz (vol=%u)", (unsigned)job->samples,
             (unsigned)job->rate, (unsigned)s_volume);
    ch_begin(AUDIO_PLAYER_CH_PROMPT, MIX_SRC_PCM, job->pcm, (uint32_t)job->samples, job->rate, 0,
             0, 0);
    bool i2s_on = false;
    const bool ok = mix_loop_run(out, PLAY_JOB_PCM, aborted, &i2s_on);
    if (i2s_on) {
        tx_flush_and_disable(out);
    }
    portENTER_CRITICAL(&s_stats_mux);
    s_stats.jobs_done++;
    s_stats.pcm_jobs++;
    portEXIT_CRITICAL(&s_stats_mux);
    return ok;
}

/* 整段 MP3：先整段解码（复用 decoder_mp3 的 PSRAM 缓冲），再按 PCM 走 CH_MUSIC */
static bool run_mp3_job(const play_job_t *job, int16_t *out, bool *aborted)
{
    const int16_t *dec_pcm = NULL;
    size_t dec_samples = 0;
    uint32_t dec_rate = 0;

    esp_err_t err = mp3_dec_decode_all(job->mp3, job->mp3_len, &dec_pcm, &dec_samples, &dec_rate);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "mp3 decode failed: %s", esp_err_to_name(err));
        portENTER_CRITICAL(&s_stats_mux);
        s_stats.mp3_errors++;
        portEXIT_CRITICAL(&s_stats_mux);
        return false;
    }
    s_last_mp3_pcm = dec_pcm;
    s_last_mp3_samples = dec_samples;

    /* 日志格式是 tools 的解析契约（mp3_test / tts_stream_test --r3-duck 都依赖该行） */
    ESP_LOGI(TAG, "play %u samples @%u Hz (vol=%u)", (unsigned)dec_samples,
             (unsigned)dec_rate, (unsigned)s_volume);
    ch_begin(AUDIO_PLAYER_CH_MUSIC, MIX_SRC_PCM, dec_pcm, (uint32_t)dec_samples, dec_rate, 0, 0,
             0);
    bool i2s_on = false;
    const bool ok = mix_loop_run(out, PLAY_JOB_MP3, aborted, &i2s_on);
    if (i2s_on) {
        tx_flush_and_disable(out);
    }

    portENTER_CRITICAL(&s_stats_mux);
    s_stats.jobs_done++;
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
    portEXIT_CRITICAL(&s_stats_mux);
    return ok;
}

/* TTS 流式：预缓冲 → 混音循环（TTS 通道由解码器供数）→ 收尾/打断 */
static bool run_stream_job(int16_t *out, bool *aborted, bool *i2s_started)
{
    *aborted = false;
    *i2s_started = false;

    memset(&s_tts_ctx, 0, sizeof(s_tts_ctx));
    s_tts_ctx.next_mode = NEXT_FIRST;
    /* open 时已清空 ring/统计；这里只重置解码器（open 之后、player 起来之前
     * 可能已经收到若干 write，数据必须保留） */
    mp3_dec_stream_reset();
    s_stream_resync_req = false;

    ch_begin(AUDIO_PLAYER_CH_TTS, MIX_SRC_STREAM, NULL, 0, 0, 0, 0, 0);

    const stream_result_t pre = stream_preroll(&s_tts_ctx);
    if (pre != STREAM_RESULT_OK) {
        if (pre == STREAM_RESULT_FAIL) {
            ESP_LOGW(TAG, "stream end before start (decoded=%u)",
                     (unsigned)s_tts_ctx.decoded_frames);
        }
        ch_clear_pending(AUDIO_PLAYER_CH_TTS);
        *aborted = (pre == STREAM_RESULT_ABORT);
        if (pre == STREAM_RESULT_ABORT) {
            ESP_LOGW(TAG, "stream abort (frames=%u, drop %u B)", (unsigned)s_stream_stats.fed_frames,
                     (unsigned)(s_stream_wr - s_stream_rd));
        }
        return false;
    }

    /* 起播时刻（I2S 在本循环首块使能） */
    const int64_t now = esp_timer_get_time();
    portENTER_CRITICAL(&s_stream_mux);
    s_stream_sound_us = now;
    if (s_stream_first_byte_us != 0) {
        s_stream_stats.first_byte_to_sound_us = (uint32_t)(now - s_stream_first_byte_us);
    }
    const uint32_t first_sound_us = s_stream_stats.first_byte_to_sound_us;
    portEXIT_CRITICAL(&s_stream_mux);

    const uint32_t prebuffer_ms = s_tts_ctx.src_rate
                                      ? (uint32_t)((uint64_t)s_tts_ctx.decoded_samples * 1000 /
                                                   s_tts_ctx.src_rate)
                                      : 0;
    ESP_LOGI(TAG, "stream first sound after %u ms (prebuffer %u ms)",
             (unsigned)(first_sound_us / 1000), (unsigned)prebuffer_ms);

    bool i2s_on = false;
    const bool ok = mix_loop_run(out, PLAY_JOB_STREAM, aborted, &i2s_on);
    *i2s_started = i2s_on;

    /* 收尾统计（日志格式是 tools 的解析契约） */
    const bool started = i2s_on;
    const int64_t finish_us = esp_timer_get_time();
    portENTER_CRITICAL(&s_stream_mux);
    s_stream_finish_us = started ? finish_us : 0;
    if (started && s_stream_sound_us) {
        s_stream_stats.duration_ms = (uint32_t)((finish_us - s_stream_sound_us) / 1000);
    }
    audio_player_stream_stats_t st = s_stream_stats;
    uint32_t drop = 0;
    if (*aborted) {
        drop = s_stream_wr - s_stream_rd;
        s_stream_rd = s_stream_wr;
    }
    portEXIT_CRITICAL(&s_stream_mux);

    if (*aborted) {
        if (started) {
            audio_io_spk_enable(false); /* 打断：立刻停声（关 TX），不写静音等待 */
        }
        ESP_LOGW(TAG, "stream abort (frames=%u, drop %u B)", (unsigned)st.fed_frames,
                 (unsigned)drop);
    } else if (!ok) {
        if (started) {
            audio_io_spk_enable(false);
        }
        ESP_LOGW(TAG, "stream failed (frames=%u, decoded=%u)", (unsigned)st.fed_frames,
                 (unsigned)st.decoded_frames);
    } else {
        ESP_LOGI(TAG, "stream end frames=%u bytes=%u decoded=%u dur=%u ms underrun=%u bad=%u",
                 (unsigned)st.fed_frames, (unsigned)st.fed_bytes, (unsigned)st.decoded_frames,
                 (unsigned)st.duration_ms, (unsigned)st.underruns, (unsigned)st.bad_frames);
        tx_flush_and_disable(out);
    }
    return ok;
}

/* 混音探针泵：三通道（tone 源）混到播空；无完成回调 */
static bool run_pump_job(int16_t *out, bool *aborted)
{
    bool i2s_on = false;
    const bool ok = mix_loop_run(out, PLAY_JOB_PUMP, aborted, &i2s_on);
    if (i2s_on) {
        tx_flush_and_disable(out);
    }
    return ok;
}

/* ---------------- 完成回调分发 ---------------- */

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

/* ---------------- 播放任务 ---------------- */

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
        if (job.kind == PLAY_JOB_PUMP) {
            s_mix_pump_job = true;
        }
        const int64_t t0 = esp_timer_get_time();
        audio_player_src_t src = AUDIO_PLAYER_SRC_PCM;
        bool ok = true;
        bool aborted = false;
        bool i2s_on = false;

        /* 新任务开始：清探针停止请求（player mix stop 只对当次泵生效） */
        s_mix_stop_req = false;

        /* 新任务开始：清上一次的淡出请求（R4 请求只对当次播放生效） */
        portENTER_CRITICAL(&s_stop_mux);
        s_stop_total_frames = 0;
        s_stop_left_frames = 0;
        s_stop_acc_q16 = 0;
        portEXIT_CRITICAL(&s_stop_mux);

        if (job.kind == PLAY_JOB_STREAM) {
            src = AUDIO_PLAYER_SRC_TTS_STREAM;
        } else if (job.kind == PLAY_JOB_MP3) {
            src = AUDIO_PLAYER_SRC_MP3_FILE;
        } else {
            src = AUDIO_PLAYER_SRC_PCM;
        }
        s_cur_src = (uint32_t)src;

        switch (job.kind) {
        case PLAY_JOB_STREAM:
            ok = run_stream_job(out, &aborted, &i2s_on);
            break;
        case PLAY_JOB_MP3:
            ok = run_mp3_job(&job, out, &aborted);
            break;
        case PLAY_JOB_PUMP:
            ok = run_pump_job(out, &aborted);
            break;
        case PLAY_JOB_PCM:
        default:
            ok = run_pcm_job(&job, out, &aborted);
            break;
        }

        if (aborted) {
            ok = false; /* 被打断的播放不算成功（R4：回调 ok=false） */
        }

        const uint32_t elapsed_ms = (uint32_t)((esp_timer_get_time() - t0) / 1000);
        portENTER_CRITICAL(&s_stats_mux);
        s_stats.last_play_ms = elapsed_ms;
        portEXIT_CRITICAL(&s_stats_mux);
        ESP_LOGI(TAG, "job done (%u ms)", (unsigned)elapsed_ms);

        /* 尾部延时后再通知完成（半双工恢复 feed 用，见架构 R2）；
         * 打断（R4 淡出/abort）与失败不延时，让 feed 恢复/状态机尽快收尾 */
        if (job.kind != PLAY_JOB_PUMP) {
            if (ok && !aborted) {
                vTaskDelay(pdMS_TO_TICKS(CONFIG_AUDIO_PLAYER_TAIL_DELAY_MS));
            }
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
        if (job.kind == PLAY_JOB_PUMP) {
            /* 探针泵没有业务回调；停止请求也在这里清掉 */
            s_mix_pump_job = false;
            s_mix_stop_req = false;
        } else {
            notify_done(src, ok);
        }
    }
}

/* ---------------- 初始化 ---------------- */

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

    /* 流式缓冲全部 PSRAM：ring 64KB + 预缓冲 stage ~7KB + 读取暂存 2KB + TTS 泵中转 1KB */
    s_ring = heap_caps_malloc(STREAM_RING_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_stage = heap_caps_malloc((size_t)STREAM_STAGE_SAMPLES * sizeof(int16_t),
                               MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_rd_scratch = heap_caps_malloc(STREAM_RD_CHUNK, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_tts_tmp = heap_caps_malloc((size_t)OUT_FRAMES_CHUNK * sizeof(int16_t),
                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_ring || !s_stage || !s_rd_scratch || !s_tts_tmp) {
        ESP_LOGW(TAG, "流式播放缓冲分配失败（PSRAM 不足），stream API 不可用");
        heap_caps_free(s_ring);
        heap_caps_free(s_stage);
        heap_caps_free(s_rd_scratch);
        heap_caps_free(s_tts_tmp);
        s_ring = NULL;
        s_stage = NULL;
        s_rd_scratch = NULL;
        s_tts_tmp = NULL;
    } else {
        ESP_LOGI(TAG, "stream: ring=%u B, stage=%u B (PSRAM)", (unsigned)STREAM_RING_BYTES,
                 (unsigned)((size_t)STREAM_STAGE_SAMPLES * sizeof(int16_t)));
    }
#else
    ESP_LOGW(TAG, "MP3 能力关闭，流式播放不可用");
#endif

    /* T3.3：三通道 ring（各 8192 帧 = 170ms @48k），全部 PSRAM */
    {
        bool rings_ok = true;
        for (int ch = 0; ch < AUDIO_PLAYER_CH_NUM; ch++) {
            s_ch[ch].ring = heap_caps_malloc((size_t)MIX_RING_FRAMES * sizeof(int16_t),
                                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            if (!s_ch[ch].ring) {
                rings_ok = false;
            }
            s_ch[ch].wr = 0;
            s_ch[ch].rd = 0;
            s_ch[ch].req_active = false;
            s_ch[ch].run_active = false;
            s_ch[ch].gain_q16 = 65536;
            s_ch[ch].gain_tgt_q16 = 65536;
        }
        if (!rings_ok) {
            ESP_LOGE(TAG, "混音通道 ring 分配失败（PSRAM 不足）");
            for (int ch = 0; ch < AUDIO_PLAYER_CH_NUM; ch++) {
                heap_caps_free(s_ch[ch].ring);
                s_ch[ch].ring = NULL;
            }
            return ESP_ERR_NO_MEM;
        }
        ESP_LOGI(TAG, "mix: 3 ch × %u frames (PSRAM)", (unsigned)MIX_RING_FRAMES);
    }

    s_queue = xQueueCreate(PLAYER_QUEUE_LEN, sizeof(play_job_t));
    if (!s_queue) {
        ESP_LOGE(TAG, "queue create failed");
        return ESP_ERR_NO_MEM;
    }

    /* duck 目标增益（powf 只在初始化跑一次）：-CONFIG_AUDIO_PLAYER_DUCK_DB */
    {
        const double lin = pow(10.0, -(double)CONFIG_AUDIO_PLAYER_DUCK_DB / 20.0);
        uint32_t target = (uint32_t)(65536.0 * lin + 0.5);
        if (target > 65536) {
            target = 65536;
        }
        s_duck_target_q16 = target;
    }

    /* 音量：初始化即生效（不插值），运行时改由 20ms 斜坡跟进 */
    s_vol_tgt_q16 = (uint32_t)s_volume * 65536u / 100u;
    s_vol_q16 = s_vol_tgt_q16;
    s_vol_q8 = s_vol_q16 >> 8;
    s_vol_ramp_tgt = s_vol_tgt_q16;
    s_vol_step = 0;

    /* 混音统计快照初值：未播放时 player mix stat 也要能看到音量/通道状态 */
    portENTER_CRITICAL(&s_stats_mux);
    memset(&s_mix_snap, 0, sizeof(s_mix_snap));
    s_mix_snap.vol = s_volume;
    s_mix_snap.vol_q8 = s_vol_q8;
    s_mix_snap.vol_target_q8 = s_vol_tgt_q16 >> 8;
    for (int ch = 0; ch < AUDIO_PLAYER_CH_NUM; ch++) {
        s_mix_snap.ch[ch].gain_q8 = 256;
    }
    portEXIT_CRITICAL(&s_stats_mux);

    BaseType_t ok = xTaskCreatePinnedToCore(player_task, "player", PLAYER_TASK_STACK, NULL,
                                            PLAYER_TASK_PRIO, &s_task, PLAYER_CORE);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "task create failed");
        s_task = NULL;
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "ready: out=%d Hz, vol=%u, mp3=%s, duck=%u q8/%u ms", AUDIO_IO_SPK_SAMPLE_RATE,
             (unsigned)s_volume, audio_player_mp3_ready() ? "on" : "off",
             (unsigned)(s_duck_target_q16 >> 8), (unsigned)CONFIG_AUDIO_PLAYER_DUCK_RAMP_MS);
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
    if (!s_ring || !s_stage || !s_rd_scratch || !s_tts_tmp) {
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

/* ---------------- 半双工打断与 duck（T3.2，R4/R3） ---------------- */

esp_err_t audio_player_fade_out_and_stop(uint32_t fade_ms)
{
    if (!s_busy) {
        return ESP_ERR_INVALID_STATE;
    }
    if (fade_ms == 0) {
        fade_ms = 1;
    }
    const uint32_t frames = fade_ms * AUDIO_IO_SPK_SAMPLE_RATE / 1000u;

    portENTER_CRITICAL(&s_stop_mux);
    if (s_stop_total_frames != 0) {
        portEXIT_CRITICAL(&s_stop_mux);
        return ESP_OK; /* 已有淡出请求：不延长/不重复 */
    }
    s_stop_total_frames = frames;
    s_stop_left_frames = frames;
    s_stop_acc_q16 = 0;
    s_stop_step_q16 = 65536u / frames;
    if (s_stop_step_q16 == 0) {
        s_stop_step_q16 = 1;
    }
    s_stop_req_us = esp_timer_get_time();
    portEXIT_CRITICAL(&s_stop_mux);

    ESP_LOGW(TAG, "fade out %u ms → stop", (unsigned)fade_ms);
    return ESP_OK;
}

void audio_player_set_duck(bool on)
{
    if (s_duck_on == on) {
        return;
    }
    s_duck_on = on;
    ESP_LOGI(TAG, "duck %s (target %u q8)", on ? "on" : "off",
             (unsigned)(on ? (s_duck_target_q16 >> 8) : 256u));
}

bool audio_player_get_duck(void)
{
    return s_duck_on;
}

bool audio_player_current_src(audio_player_src_t *out)
{
    if (!s_busy) {
        return false;
    }
    if (out != NULL) {
        *out = (audio_player_src_t)s_cur_src;
    }
    return true;
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

    portENTER_CRITICAL(&s_stop_mux);
    out->stop_count = s_stop_count;
    out->last_stop_ms = s_last_stop_ms;
    uint32_t peak = 0;
    for (uint32_t i = 0; i < PEAK_RING_LEN; i++) {
        if (s_peak_ring[i] > peak) {
            peak = s_peak_ring[i];
        }
    }
    out->out_peak = peak;
    portEXIT_CRITICAL(&s_stop_mux);

    out->duck_on = s_duck_on;
    out->duck_gain_q8 = s_mix_snap.ch[AUDIO_PLAYER_CH_MUSIC].gain_q8;
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

    /* T3.2：输出峰值窗口与打断统计一起清零（不影响 duck 开关/淡出请求） */
    portENTER_CRITICAL(&s_stop_mux);
    memset(s_peak_ring, 0, sizeof(s_peak_ring));
    s_peak_idx = 0;
    s_stop_count = 0;
    s_last_stop_ms = 0;
    portEXIT_CRITICAL(&s_stop_mux);

    audio_player_mix_stats_reset();
}

esp_err_t audio_player_set_volume(uint8_t volume)
{
    if (volume > 100) {
        return ESP_ERR_INVALID_ARG;
    }
    s_volume = volume;
    s_vol_tgt_q16 = (uint32_t)volume * 65536u / 100u;
    ESP_LOGI(TAG, "volume=%u", (unsigned)volume);
    return ESP_OK;
}

uint8_t audio_player_get_volume(void)
{
    return s_volume;
}

/* ---------------- 混音统计与探针（T3.3） ---------------- */

esp_err_t audio_player_mix_stats_read(audio_player_mix_stats_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_stats_mux);
    *out = s_mix_snap;
    portEXIT_CRITICAL(&s_stats_mux);

    /* mix_peak 与 audio_player_stats_t.out_peak 同口径（最近 320ms 窗口） */
    portENTER_CRITICAL(&s_stop_mux);
    uint32_t peak = 0;
    for (uint32_t i = 0; i < PEAK_RING_LEN; i++) {
        if (s_peak_ring[i] > peak) {
            peak = s_peak_ring[i];
        }
    }
    portEXIT_CRITICAL(&s_stop_mux);
    out->mix_peak = peak;
    return ESP_OK;
}

void audio_player_mix_stats_reset(void)
{
    portENTER_CRITICAL(&s_stats_mux);
    memset(&s_mix_snap, 0, sizeof(s_mix_snap));
    s_mix_snap.vol = s_volume;
    s_mix_snap.vol_q8 = s_vol_q16 >> 8;
    s_mix_snap.vol_target_q8 = s_vol_tgt_q16 >> 8;
    for (int ch = 0; ch < AUDIO_PLAYER_CH_NUM; ch++) {
        s_ch[ch].peak = 0;
    }
    portEXIT_CRITICAL(&s_stats_mux);

    portENTER_CRITICAL(&s_stop_mux);
    memset(s_peak_ring, 0, sizeof(s_peak_ring));
    s_peak_idx = 0;
    portEXIT_CRITICAL(&s_stop_mux);
}

esp_err_t audio_player_mix_tone(audio_player_ch_t ch, uint32_t freq_hz, uint32_t ms, uint32_t amp)
{
    if (ch >= AUDIO_PLAYER_CH_NUM || freq_hz < 100 || freq_hz > 4000 || ms < 50 ||
        ms > MIX_TONE_MAX_MS || amp < 100 || amp > 20000) {
        return ESP_ERR_INVALID_ARG;
    }
    /* 真实播放任务在播时不允许混入探针音源（避免污染 R1–R5 行为）；
     * 探针泵自己的收尾冲刷期间允许追加（下一轮 PUMP 作业会接上） */
    if (s_busy && !s_mix_pump_running && !s_mix_pump_job) {
        return ESP_ERR_INVALID_STATE;
    }
    bool need_enqueue = false;
    portENTER_CRITICAL(&s_mix_mux);
    mix_ch_t *c = &s_ch[ch];
    c->req_active = true;
    c->req_src = MIX_SRC_TONE;
    c->req_tone_freq = freq_hz;
    c->req_tone_ms = ms;
    c->req_tone_amp = amp;
    c->req_gen++;
    if (!s_mix_pump_running) {
        s_mix_pump_running = true;
        need_enqueue = true;
    }
    portEXIT_CRITICAL(&s_mix_mux);

    if (need_enqueue) {
        const play_job_t job = {.kind = PLAY_JOB_PUMP};
        if (xQueueSend(s_queue, &job, 0) != pdTRUE) {
            portENTER_CRITICAL(&s_mix_mux);
            s_mix_pump_running = false;
            s_ch[ch].req_active = false;
            portEXIT_CRITICAL(&s_mix_mux);
            ESP_LOGW(TAG, "queue full, drop mix tone");
            return ESP_ERR_TIMEOUT;
        }
    }
    return ESP_OK;
}

esp_err_t audio_player_mix_stop(void)
{
    /* 真实播放任务（提示音/TTS/MP3）不受探针停止影响；
     * 探针泵收尾冲刷（s_mix_pump_job）期间也返回 OK，避免测试脚本与冲刷时序打架 */
    if (s_busy && !s_mix_pump_running && !s_mix_pump_job) {
        return ESP_ERR_INVALID_STATE;
    }
    s_mix_stop_req = true;
    return ESP_OK;
}

esp_err_t audio_player_mix_capture(uint32_t frames)
{
    if (frames == 0 || frames > MIX_CAP_MAX_FRAMES) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_cap_buf) {
        s_cap_buf = heap_caps_malloc((size_t)MIX_CAP_MAX_FRAMES * sizeof(int16_t),
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!s_cap_buf) {
            return ESP_ERR_NO_MEM;
        }
        memset(s_cap_buf, 0, (size_t)MIX_CAP_MAX_FRAMES * sizeof(int16_t));
    }
    portENTER_CRITICAL(&s_stats_mux);
    s_cap_len = frames;
    s_cap_pos = 0;
    s_cap_armed = true;
    portEXIT_CRITICAL(&s_stats_mux);
    return ESP_OK;
}

esp_err_t audio_player_mix_capture_status(uint32_t *captured, uint32_t *requested)
{
    if (!s_cap_buf) {
        return ESP_ERR_INVALID_STATE;
    }
    portENTER_CRITICAL(&s_stats_mux);
    if (captured != NULL) {
        *captured = s_cap_pos;
    }
    if (requested != NULL) {
        *requested = s_cap_len;
    }
    portEXIT_CRITICAL(&s_stats_mux);
    return ESP_OK;
}

esp_err_t audio_player_mix_capture_read(int16_t *dst, size_t offset, size_t count)
{
    if (!dst || count == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_cap_buf) {
        return ESP_ERR_INVALID_STATE;
    }
    portENTER_CRITICAL(&s_stats_mux);
    const uint32_t filled = s_cap_pos;
    portEXIT_CRITICAL(&s_stats_mux);
    if (offset + count > filled) {
        return ESP_ERR_INVALID_STATE; /* 播放提前结束：只允许读已抓到的部分 */
    }
    memcpy(dst, s_cap_buf + offset, count * sizeof(int16_t));
    return ESP_OK;
}
