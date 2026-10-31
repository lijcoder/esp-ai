/*
 * 播放引擎：把任意采样率的单声道 PCM 变成 48kHz 立体声输出（音量/淡入淡出/尾音处理），
 * 并在播放结束后回调业务层。T2.3 起增加 MP3 输入（minimp3 解码后走同一条 PCM 链路）。
 *
 * 任务模型：单播放任务（核 1/优先级 5），队列深度 4；解码、重采样、写 I2S 全部串行。
 * 内存：输出块在内部 RAM（I2S DMA 需要）；MP3 解码状态与 PCM 缓冲在 PSRAM（见 decoder_mp3）。
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

typedef enum {
    PLAY_JOB_PCM = 0, /* 调用方持有一段 PCM */
    PLAY_JOB_MP3,     /* 调用方持有一段 MP3 码流，播放任务内解码 */
} play_job_kind_t;

typedef struct {
    play_job_kind_t kind;
    const int16_t *pcm; /* PCM：采样点（单声道）；调用方保证生命周期 */
    size_t samples;     /* PCM：采样点数 */
    uint32_t rate;      /* PCM：源采样率 */
    const uint8_t *mp3; /* MP3：码流 */
    size_t mp3_len;     /* MP3：字节数 */
} play_job_t;

static QueueHandle_t s_queue = NULL;
static TaskHandle_t s_task = NULL;
static audio_player_done_cb_t s_done_cb = NULL;
static void *s_done_arg = NULL;
static volatile bool s_busy = false;
static uint8_t s_volume = CONFIG_AUDIO_PLAYER_DEFAULT_VOLUME;

/* 统计：播放任务写、diag 读，用自旋锁保护整结构体拷贝（临界区只有几十字节） */
static portMUX_TYPE s_stats_mux = portMUX_INITIALIZER_UNLOCKED;
static audio_player_stats_t s_stats;

/* 最近一次 MP3 解码结果（PCM 指向 decoder_mp3 的静态 PSRAM 缓冲，仅探针对拍用） */
static const int16_t *s_last_mp3_pcm;
static size_t s_last_mp3_samples;

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
static void run_job(const play_job_t *job, int16_t *out)
{
    const int16_t *pcm = job->pcm;
    size_t samples = job->samples;
    uint32_t rate = job->rate;
    bool ok = true;

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
            ok = false;
        } else {
            pcm = dec_pcm;
            samples = dec_samples;
            rate = dec_rate;
            s_last_mp3_pcm = dec_pcm;
            s_last_mp3_samples = dec_samples;
        }
    }

    if (ok) {
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

static void player_task(void *arg)
{
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
        int64_t t0 = esp_timer_get_time();
        run_job(&job, out);

        uint32_t elapsed_ms = (uint32_t)((esp_timer_get_time() - t0) / 1000);
        portENTER_CRITICAL(&s_stats_mux);
        s_stats.last_play_ms = elapsed_ms;
        portEXIT_CRITICAL(&s_stats_mux);
        ESP_LOGI(TAG, "job done (%u ms)", (unsigned)elapsed_ms);

        /* 尾部延时后再通知完成（半双工恢复 feed 用，见架构 R2） */
        vTaskDelay(pdMS_TO_TICKS(CONFIG_AUDIO_PLAYER_TAIL_DELAY_MS));
        s_busy = false;
        if (s_done_cb) {
            s_done_cb(s_done_arg);
        }
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
    s_done_cb = cb;
    s_done_arg = arg;
}

esp_err_t audio_player_play_pcm(const int16_t *pcm, size_t samples, uint32_t sample_rate)
{
    if (!s_queue || !pcm || samples == 0 || sample_rate == 0) {
        return ESP_ERR_INVALID_ARG;
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

bool audio_player_mp3_ready(void)
{
    return mp3_dec_ready();
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
