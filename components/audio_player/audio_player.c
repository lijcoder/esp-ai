#include "audio_player.h"

#include <string.h>

#include "audio_io.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
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

typedef struct {
    const int16_t *pcm;
    size_t samples;
    uint32_t rate;
} play_job_t;

static QueueHandle_t s_queue = NULL;
static TaskHandle_t s_task = NULL;
static audio_player_done_cb_t s_done_cb = NULL;
static void *s_done_arg = NULL;
static volatile bool s_busy = false;
static uint8_t s_volume = CONFIG_AUDIO_PLAYER_DEFAULT_VOLUME;

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
        const uint32_t total_out = (uint32_t)((uint64_t)job.samples * AUDIO_IO_SPK_SAMPLE_RATE / job.rate);
        const uint32_t fade_in = total_out < FADE_IN_FRAMES ? total_out : FADE_IN_FRAMES;
        const uint32_t fade_out = total_out < FADE_OUT_FRAMES ? total_out : FADE_OUT_FRAMES;

        ESP_LOGI(TAG, "play %u samples @%u Hz (vol=%u)", (unsigned)job.samples,
                 (unsigned)job.rate, (unsigned)s_volume);

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
                const uint32_t src_pos = (uint32_t)((uint64_t)idx * job.rate / AUDIO_IO_SPK_SAMPLE_RATE);
                const uint32_t frac = (uint32_t)((uint64_t)idx * job.rate % AUDIO_IO_SPK_SAMPLE_RATE);
                const int32_t s0 = job.pcm[src_pos];
                const int32_t s1 = (src_pos + 1 < job.samples) ? job.pcm[src_pos + 1] : s0;
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

        /* 尾部静音冲刷 DMA，避免余音被硬切 */
        memset(out, 0, (size_t)OUT_FRAMES_CHUNK * 2 * sizeof(int16_t));
        for (int i = 0; i < (AUDIO_IO_SPK_SAMPLE_RATE / 10) / OUT_FRAMES_CHUNK; i++) {
            audio_io_write_spk(out, OUT_FRAMES_CHUNK, 100);
        }
        vTaskDelay(pdMS_TO_TICKS(150));
        audio_io_spk_enable(false);

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

    ESP_LOGI(TAG, "ready: out=%d Hz, vol=%u", AUDIO_IO_SPK_SAMPLE_RATE, (unsigned)s_volume);
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

bool audio_player_is_busy(void)
{
    return s_busy;
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
