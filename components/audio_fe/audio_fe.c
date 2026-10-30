#include "audio_fe.h"

#include <string.h>

#include "audio_io.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_wn_models.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "model_path.h"
#include "sdkconfig.h"

static const char *TAG = "AFE";

#define FEED_TASK_STACK  8192
#define FETCH_TASK_STACK 4096
#define FEED_TASK_PRIO   8
#define FETCH_TASK_PRIO  6
#define AFE_CORE         1

static const esp_afe_sr_iface_t *s_afe = NULL;
static esp_afe_sr_data_t *s_afe_data = NULL;
static audio_fe_cb_t s_cb = NULL;
static void *s_cb_arg = NULL;
static volatile bool s_running = false;
static volatile bool s_feed_enabled = true;
static char s_wn_name[32] = {0};

static void feed_task(void *arg)
{
    const int chunksize = s_afe->get_feed_chunksize(s_afe_data);
    const int nch = s_afe->get_feed_channel_num(s_afe_data);
    int16_t *buf = heap_caps_malloc((size_t)chunksize * nch * sizeof(int16_t),
                                    MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!buf) {
        ESP_LOGE(TAG, "feed buffer alloc failed (%d frames x %d ch)", chunksize, nch);
        s_running = false;
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "feed task start: chunksize=%d ch=%d", chunksize, nch);
    while (s_running) {
        /* 始终读麦克风以保持真实时间节奏；半双工时丢弃数据只喂静音 */
        int got = audio_io_read_mic(buf, (size_t)chunksize, 100);
        if (got < 0) {
            /* 读失败按 0 帧处理，避免负值参与后续指针运算 */
            got = 0;
        }
        if (got < chunksize) {
            memset(buf + got, 0, (size_t)(chunksize - got) * sizeof(int16_t));
        }
        if (!s_feed_enabled) {
            memset(buf, 0, (size_t)chunksize * sizeof(int16_t));
        }
        s_afe->feed(s_afe_data, buf);
    }

    free(buf);
    vTaskDelete(NULL);
}

static void fetch_task(void *arg)
{
    vad_state_t last_vad = VAD_SILENCE;
    ESP_LOGI(TAG, "fetch task start");

    while (s_running) {
        /* fetch_with_delay 的超时参数是 tick（与 audio_io 的毫秒不同）：
         * 200ms >> 一帧 32ms，仅用于让任务能响应 s_running 退出。 */
        afe_fetch_result_t *res = s_afe->fetch_with_delay(s_afe_data, pdMS_TO_TICKS(200));
        if (!res || res->ret_value == ESP_FAIL) {
            continue;
        }

        if (res->wakeup_state == WAKENET_DETECTED && s_cb) {
            s_cb(AUDIO_FE_EVT_WAKE, s_cb_arg);
        }

        if (res->vad_state != last_vad) {
            last_vad = res->vad_state;
            if (s_cb) {
                s_cb(last_vad == VAD_SPEECH ? AUDIO_FE_EVT_VAD_SPEECH_START
                                            : AUDIO_FE_EVT_VAD_SPEECH_END,
                     s_cb_arg);
            }
        }
    }

    vTaskDelete(NULL);
}

esp_err_t audio_fe_init(audio_fe_cb_t cb, void *arg)
{
    s_cb = cb;
    s_cb_arg = arg;

    srmodel_list_t *models = esp_srmodel_init(CONFIG_AUDIO_FE_MODEL_PARTITION);
    if (!models) {
        ESP_LOGE(TAG, "srmodels init failed, check '%s' partition (idf.py flash)",
                 CONFIG_AUDIO_FE_MODEL_PARTITION);
        return ESP_ERR_NOT_FOUND;
    }

    char *wn = esp_srmodel_filter(models, ESP_WN_PREFIX, NULL);
    if (!wn) {
        ESP_LOGE(TAG, "no wakenet model in '%s' partition", CONFIG_AUDIO_FE_MODEL_PARTITION);
        return ESP_ERR_NOT_FOUND;
    }
    strlcpy(s_wn_name, wn, sizeof(s_wn_name));
    ESP_LOGI(TAG, "wakenet model: %s", s_wn_name);

    afe_config_t *cfg = afe_config_init(CONFIG_AUDIO_FE_INPUT_FORMAT, models,
                                        AFE_TYPE_SR, AFE_MODE_LOW_COST);
    if (!cfg) {
        ESP_LOGE(TAG, "afe_config_init failed (input format \"%s\")", CONFIG_AUDIO_FE_INPUT_FORMAT);
        return ESP_FAIL;
    }

    s_afe = esp_afe_handle_from_config(cfg);
    if (!s_afe) {
        ESP_LOGE(TAG, "get afe handle failed");
        afe_config_free(cfg);
        return ESP_FAIL;
    }

    s_afe_data = s_afe->create_from_config(cfg);
    afe_config_free(cfg);
    if (!s_afe_data) {
        ESP_LOGE(TAG, "afe create failed");
        return ESP_FAIL;
    }

    /* 本期只支持单麦（"M"）：多通道需要在 feed 里拼参考通道 */
    int nch = s_afe->get_feed_channel_num(s_afe_data);
    if (nch != 1) {
        ESP_LOGE(TAG, "input has %d channels, MVP only supports 1 (\"M\")", nch);
        return ESP_ERR_NOT_SUPPORTED;
    }

    ESP_LOGI(TAG, "afe ready: input_format=\"%s\" mic=%d Hz", CONFIG_AUDIO_FE_INPUT_FORMAT,
             AUDIO_IO_MIC_SAMPLE_RATE);
    return ESP_OK;
}

esp_err_t audio_fe_start(void)
{
    if (!s_afe || !s_afe_data) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_running) {
        return ESP_OK;
    }

    s_running = true;

    BaseType_t ok = xTaskCreatePinnedToCore(feed_task, "afe_feed", FEED_TASK_STACK, NULL,
                                            FEED_TASK_PRIO, NULL, AFE_CORE);
    if (ok != pdPASS) {
        s_running = false;
        return ESP_ERR_NO_MEM;
    }

    ok = xTaskCreatePinnedToCore(fetch_task, "afe_fetch", FETCH_TASK_STACK, NULL,
                                 FETCH_TASK_PRIO, NULL, AFE_CORE);
    if (ok != pdPASS) {
        s_running = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void audio_fe_set_feed_enabled(bool enabled)
{
    if (s_feed_enabled != enabled) {
        ESP_LOGI(TAG, "feed %s", enabled ? "resumed" : "paused (half-duplex)");
    }
    s_feed_enabled = enabled;
}

bool audio_fe_feed_enabled(void)
{
    return s_feed_enabled;
}

const char *audio_fe_wakenet_name(void)
{
    return s_wn_name[0] ? s_wn_name : NULL;
}
