#include "audio_fe.h"

#include <string.h>

#include "audio_io.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wn_models.h"
#include "event_bus.h"
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
static char s_wn_name[32] = {0};
static bool s_fetch_chunk_logged; /* 只在 fetch 任务里读写，单线程无需保护 */

/* ---- 半双工门控仲裁（T3.2，R1/R2/R5） ----
 * 位图 = "谁要求暂停收音"；归零才真正恢复。所有字段由 s_feed_mux 保护：
 * 写入方是业务任务（evt/ws_client/player 回调/console），读取方是 feed 任务（每 32ms）。
 */
static portMUX_TYPE s_feed_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t s_feed_hold_mask;      /* 当前原因位图（0 = 收音中） */
static volatile uint32_t s_feed_discard_left;   /* 恢复后还要喂零的样本数（R5） */
static uint32_t s_feed_discard_pending_ms;      /* 下次恢复时生效的丢弃窗口（ms） */
static uint32_t s_feed_holds;                   /* hold() 次数（累计） */
static uint32_t s_feed_releases;                /* release() 次数（累计） */
static uint32_t s_feed_discards;                /* 触发过的丢弃窗口次数 */
static uint32_t s_feed_discard_samples;         /* 累计喂零样本数 */
static uint32_t s_feed_last_discard_ms;         /* 最近一次丢弃窗口长度（ms） */

/* ---- 观测统计（mic stat）：只在 feed/fetch 任务里单写，命令侧只读 ---- */
static portMUX_TYPE s_stats_mux = portMUX_INITIALIZER_UNLOCKED;
static uint64_t s_stat_sum_sq;
static uint32_t s_stat_samples;
static int32_t s_stat_peak;
static uint32_t s_stat_frames;
static uint32_t s_stat_read_errors;
static uint32_t s_stat_wake_count;
static uint32_t s_stat_chunksize;
static volatile bool s_stat_vad_active;

/*
 * 把一帧麦克风原始数据累加进统计窗口。
 * 平方和/峰值先在临界区外算好，临界区只做合并，避免长时间关中断（音频任务敏感）。
 */
static void stats_accumulate(const int16_t *buf, int frames)
{
    uint64_t sum_sq = 0;
    int32_t peak = 0;

    for (int i = 0; i < frames; i++) {
        int32_t v = buf[i];
        if (v < 0) {
            v = -v;
        }
        if (v > peak) {
            peak = v;
        }
        sum_sq += (uint64_t)((int64_t)v * v);
    }

    portENTER_CRITICAL(&s_stats_mux);
    s_stat_sum_sq += sum_sq;
    s_stat_samples += (uint32_t)frames;
    if (peak > s_stat_peak) {
        s_stat_peak = peak;
    }
    portEXIT_CRITICAL(&s_stats_mux);
}

/* ---------------- 上行 16k PCM 环形缓冲（T2.1） ----------------
 *
 * 生产者 = afe_fetch 任务（单写），消费者 = 调用 audio_fe_uplink_read 的任务（单读）。
 * 为什么不用回调：编码（Opus）+ 发送（ws_send_bin 内部要分配队列节点）若放进
 * afe_fetch 会违反 CONVENTIONS §3（音频任务禁止 malloc/网络/阻塞）；环形缓冲把
 * 抖动隔离到消费端任务，AFE 任务里只剩一次 memcpy + 一次任务通知。
 *
 * 两端在同一把临界区里做 memcpy + 索引更新：单次拷贝 ≤ 1KB、关中断微秒级，
 * 比 mutex 更稳（mutex 可能让 fetch 任务阻塞在持锁方）。
 */
static int16_t *s_up_ring; /* PSRAM，容量为 2 的幂 */
static uint32_t s_up_ring_samples;
static uint32_t s_up_head; /* 累计写入样本（减去 tail 即当前占用） */
static uint32_t s_up_tail; /* 累计读出样本 */
static volatile bool s_up_active;
static TaskHandle_t s_up_reader; /* 最近一次调用 read 的任务，写入后通知它 */
static portMUX_TYPE s_up_mux = portMUX_INITIALIZER_UNLOCKED;
static uint32_t s_up_written;
static uint32_t s_up_read;
static uint32_t s_up_dropped;
static uint32_t s_up_timeouts;
static uint32_t s_up_high_water;
static uint32_t s_up_first_frame_us;
static int64_t s_up_start_us;

static esp_err_t uplink_init(void)
{
#if CONFIG_AUDIO_FE_UPLINK_RING_MS > 0
    uint32_t want = (uint32_t)CONFIG_AUDIO_FE_UPLINK_RING_MS * AUDIO_IO_MIC_SAMPLE_RATE / 1000u;
    uint32_t cap = 1;

    /* 容量取 2 的幂：取模用掩码，且 2000ms → 32000 样本 → 32768（2.048s） */
    while (cap < want) {
        cap <<= 1;
    }
    s_up_ring = heap_caps_malloc((size_t)cap * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!s_up_ring) {
        ESP_LOGE(TAG, "uplink ring alloc failed: %u B (PSRAM)",
                 (unsigned)(cap * sizeof(int16_t)));
        return ESP_ERR_NO_MEM;
    }
    s_up_ring_samples = cap;
    ESP_LOGI(TAG, "uplink ring: %u samples (%u ms), %u B PSRAM", (unsigned)cap,
             (unsigned)(cap * 1000u / AUDIO_IO_MIC_SAMPLE_RATE),
             (unsigned)(cap * sizeof(int16_t)));
    return ESP_OK;
#else
    ESP_LOGW(TAG, "uplink disabled (CONFIG_AUDIO_FE_UPLINK_RING_MS=0)");
    return ESP_OK;
#endif
}

/* 写入一帧 AFE 输出；由 afe_fetch 任务调用，禁止日志/分配/阻塞 */
static void uplink_write(const int16_t *src, size_t samples)
{
    if (!s_up_active || !src || samples == 0) {
        return;
    }

    TaskHandle_t reader;

    portENTER_CRITICAL(&s_up_mux);
    const uint32_t cap = s_up_ring_samples;
    uint32_t used = s_up_head - s_up_tail;

    /* 异常大帧（> 整环）只保留尾部，避免绕圈写两遍 */
    if (samples > cap) {
        src += samples - cap;
        samples = cap;
    }
    if (samples > cap - used) {
        /* 环满：丢最旧样本，保证上行时延上限（宁可丢数据也不让延迟无限增长） */
        uint32_t over = (uint32_t)samples - (cap - used);
        s_up_tail += over;
        s_up_dropped += over;
        used = cap;
    }
    uint32_t idx = s_up_head & (cap - 1u);
    uint32_t first = cap - idx;
    if (first > samples) {
        first = (uint32_t)samples;
    }
    memcpy(&s_up_ring[idx], src, first * sizeof(int16_t));
    if (samples > first) {
        memcpy(&s_up_ring[0], src + first, (samples - first) * sizeof(int16_t));
    }
    s_up_head += (uint32_t)samples;
    s_up_written += (uint32_t)samples;
    used = s_up_head - s_up_tail;
    if (used > s_up_high_water) {
        s_up_high_water = used;
    }
    reader = s_up_reader;
    portEXIT_CRITICAL(&s_up_mux);

    /* 丢弃只记统计、不在音频任务里 WARN：UART 输出可能阻塞（CONVENTIONS §3），
     * 由 console/消费端读 stats 时发现 samples_dropped 增长再告警 */
    if (reader) {
        xTaskNotifyGive(reader);
    }
}

esp_err_t audio_fe_uplink_start(void)
{
    if (!s_up_ring) {
        return ESP_ERR_NOT_SUPPORTED;
    }

    portENTER_CRITICAL(&s_up_mux);
    s_up_head = 0;
    s_up_tail = 0;
    s_up_written = 0;
    s_up_read = 0;
    s_up_dropped = 0;
    s_up_timeouts = 0;
    s_up_high_water = 0;
    s_up_first_frame_us = 0;
    s_up_start_us = esp_timer_get_time();
    s_up_reader = NULL;
    s_up_active = true;
    portEXIT_CRITICAL(&s_up_mux);

    ESP_LOGI(TAG, "uplink started");
    return ESP_OK;
}

void audio_fe_uplink_stop(void)
{
    uint32_t written;
    uint32_t dropped;
    bool was_active;

    portENTER_CRITICAL(&s_up_mux);
    was_active = s_up_active;
    s_up_active = false;
    s_up_head = 0;
    s_up_tail = 0;
    s_up_reader = NULL;
    written = s_up_written;
    dropped = s_up_dropped;
    portEXIT_CRITICAL(&s_up_mux);

    if (was_active) {
        ESP_LOGI(TAG, "uplink stopped (written %u, dropped %u samples)", (unsigned)written,
                 (unsigned)dropped);
    }
}

bool audio_fe_uplink_active(void)
{
    return s_up_active;
}

int audio_fe_uplink_read(int16_t *dst, size_t samples, uint32_t timeout_ms)
{
    if (!dst || samples == 0) {
        return -ESP_ERR_INVALID_ARG;
    }
    if (!s_up_ring) {
        return -ESP_ERR_INVALID_STATE;
    }

    portENTER_CRITICAL(&s_up_mux);
    s_up_reader = xTaskGetCurrentTaskHandle();
    portEXIT_CRITICAL(&s_up_mux);

    const int64_t deadline_us = esp_timer_get_time() + (int64_t)timeout_ms * 1000;

    for (;;) {
        uint32_t got = 0;
        /* 只在"够 samples 个"或"已到超时"时才交数据：消费端（T2.2）按 20ms 精确
         * 分帧，若一有数据就返回小块，编码器就得自己重组，契约会变模糊。 */
        bool deadline_passed = esp_timer_get_time() >= deadline_us;

        portENTER_CRITICAL(&s_up_mux);
        uint32_t avail = s_up_head - s_up_tail;
        if (avail >= samples) {
            got = (uint32_t)samples;
        } else if (avail > 0 && deadline_passed) {
            got = avail; /* 超时：把剩余不足一帧的数据交给调用方，避免丢样本 */
        }
        if (got > 0) {
            const uint32_t cap = s_up_ring_samples;
            uint32_t idx = s_up_tail & (cap - 1u);
            uint32_t first = cap - idx;
            if (first > got) {
                first = got;
            }
            memcpy(dst, &s_up_ring[idx], first * sizeof(int16_t));
            if (got > first) {
                memcpy(dst + first, &s_up_ring[0], (got - first) * sizeof(int16_t));
            }
            s_up_tail += got;
            s_up_read += got;
            if (s_up_first_frame_us == 0 && s_up_start_us != 0) {
                s_up_first_frame_us = (uint32_t)(esp_timer_get_time() - s_up_start_us);
            }
        }
        portEXIT_CRITICAL(&s_up_mux);

        if (got > 0) {
            /* 本次读结束：清掉等待者，避免 fetch 任务持续给 console 任务累积任务通知 */
            portENTER_CRITICAL(&s_up_mux);
            s_up_reader = NULL;
            portEXIT_CRITICAL(&s_up_mux);
            return (int)got;
        }
        if (!s_up_active) {
            portENTER_CRITICAL(&s_up_mux);
            s_up_reader = NULL;
            portEXIT_CRITICAL(&s_up_mux);
            return 0;
        }
        int64_t remain_ms = (deadline_us - esp_timer_get_time()) / 1000;
        if (remain_ms <= 0) {
            portENTER_CRITICAL(&s_up_mux);
            s_up_timeouts++;
            s_up_reader = NULL;
            portEXIT_CRITICAL(&s_up_mux);
            return 0;
        }
        /* 写入方在退出临界区之后才通知：即使通知先于本次等待到达，
         * ulTaskNotifyTake 也会立即返回，不会丢唤醒 */
        (void)ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS((uint32_t)remain_ms));
    }
}

esp_err_t audio_fe_uplink_stats_read(audio_fe_uplink_stats_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }

    portENTER_CRITICAL(&s_up_mux);
    out->samples_written = s_up_written;
    out->samples_read = s_up_read;
    out->samples_dropped = s_up_dropped;
    out->read_timeouts = s_up_timeouts;
    out->ring_samples = s_up_ring_samples;
    out->high_water = s_up_high_water;
    out->first_frame_us = s_up_first_frame_us;
    out->active = s_up_active;
    portEXIT_CRITICAL(&s_up_mux);
    return ESP_OK;
}

esp_err_t audio_fe_uplink_stats_reset(void)
{
    portENTER_CRITICAL(&s_up_mux);
    s_up_written = 0;
    s_up_read = 0;
    s_up_dropped = 0;
    s_up_timeouts = 0;
    s_up_high_water = 0;
    s_up_first_frame_us = 0;
    portEXIT_CRITICAL(&s_up_mux);
    return ESP_OK;
}

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
    s_stat_chunksize = (uint32_t)chunksize;
    while (s_running) {
        /* 始终读麦克风以保持真实时间节奏；半双工时丢弃数据只喂静音 */
        int got = audio_io_read_mic(buf, (size_t)chunksize, 100);
        if (got < 0) {
            /* 读失败按 0 帧处理，避免负值参与后续指针运算 */
            portENTER_CRITICAL(&s_stats_mux);
            s_stat_read_errors++;
            portEXIT_CRITICAL(&s_stats_mux);
            got = 0;
        }
        if (got < chunksize) {
            memset(buf + got, 0, (size_t)(chunksize - got) * sizeof(int16_t));
        }
        /* 统计真实采到的样本（补零帧不计入电平）；暂停收音时也累加，
         * 这样 mic stat 能区分"麦克风没声"和"AFE 吃静音"。 */
        if (got > 0) {
            stats_accumulate(buf, got);
        }
        /* 半双工门控（T3.2）：持有任一路原因位 → 喂静音；刚恢复时先丢弃 R5 窗口。
         * 只在音频任务里读，写入方拿 s_feed_mux，临界区只有几条比较/加法。 */
        bool mute = false;
        portENTER_CRITICAL(&s_feed_mux);
        if (s_feed_hold_mask != 0) {
            mute = true;
        } else if (s_feed_discard_left > 0) {
            /* R5：恢复后最早 N ms 的麦克风数据喂零，避免打断瞬间的噪声触发 VAD */
            const uint32_t n = (s_feed_discard_left > (uint32_t)chunksize)
                                   ? (uint32_t)chunksize
                                   : s_feed_discard_left;
            s_feed_discard_left -= n;
            s_feed_discard_samples += n;
            mute = true;
        }
        portEXIT_CRITICAL(&s_feed_mux);
        if (mute) {
            memset(buf, 0, (size_t)chunksize * nch * sizeof(int16_t));
        }
        portENTER_CRITICAL(&s_stats_mux);
        s_stat_frames++;
        portEXIT_CRITICAL(&s_stats_mux);
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

        if (res->wakeup_state == WAKENET_DETECTED) {
            portENTER_CRITICAL(&s_stats_mux);
            s_stat_wake_count++;
            portEXIT_CRITICAL(&s_stats_mux);
            /* T1.2b：业务走事件总线。音频任务只许投无 payload 事件（esp_event 带 payload
             * 会内部 calloc，违反 CONVENTIONS §3）。s_cb 保留给调试/兼容，产品路径不注册。 */
            esp_err_t perr = app_evt_post(APP_EVT_WAKE_DETECTED, NULL);
            if (perr != ESP_OK) {
                ESP_LOGD(TAG, "投递 WAKE_DETECTED 失败: %s", esp_err_to_name(perr));
            }
            if (s_cb) {
                s_cb(AUDIO_FE_EVT_WAKE, s_cb_arg);
            }
        }

        if (res->vad_state != last_vad) {
            last_vad = res->vad_state;
            s_stat_vad_active = (last_vad == VAD_SPEECH);
            esp_err_t perr = app_evt_post(
                last_vad == VAD_SPEECH ? APP_EVT_VAD_SPEECH_START : APP_EVT_VAD_SPEECH_END, NULL);
            if (perr != ESP_OK) {
                ESP_LOGD(TAG, "投递 VAD 事件失败: %s", esp_err_to_name(perr));
            }
            if (s_cb) {
                s_cb(last_vad == VAD_SPEECH ? AUDIO_FE_EVT_VAD_SPEECH_START
                                            : AUDIO_FE_EVT_VAD_SPEECH_END,
                     s_cb_arg);
            }
        }

        /* 上行音频：AFE 输出（16k/单声道/int16）。帧长由 AFE 决定，
         * 只打印一次供 T2.2 分帧参考（不要把 fetch 帧长当 Opus 帧长假设）。 */
        if (res->data && res->data_size > 0) {
            if (!s_fetch_chunk_logged) {
                s_fetch_chunk_logged = true;
                ESP_LOGI(TAG, "fetch chunk: %d samples (%d ms)", res->data_size / 2,
                         (res->data_size / 2) * 1000 / AUDIO_IO_MIC_SAMPLE_RATE);
            }
            uplink_write(res->data, (size_t)res->data_size / sizeof(int16_t));
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

    /* 上行缓冲失败只降级（P1 功能不依赖它）：uplink_start 会返回 ESP_ERR_NOT_SUPPORTED，
     * 唤醒/提示音仍可用，避免整机启动被一个可选功能卡死 */
    if (uplink_init() != ESP_OK) {
        ESP_LOGW(TAG, "uplink unavailable, wake/prompt keep working");
    }
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

/* ---------------- 半双工门控（T3.2，R1/R2/R5） ---------------- */

void audio_fe_feed_hold(uint32_t reason)
{
    uint32_t mask;
    bool entered;

    if (reason == 0) {
        return;
    }
    portENTER_CRITICAL(&s_feed_mux);
    entered = (s_feed_hold_mask == 0);
    s_feed_hold_mask |= reason;
    s_feed_holds++;
    mask = s_feed_hold_mask;
    portEXIT_CRITICAL(&s_feed_mux);

    if (entered) {
        ESP_LOGI(TAG, "feed paused (holds=0x%02x)", (unsigned)mask);
    }
}

void audio_fe_feed_release(uint32_t reason)
{
    uint32_t mask;
    bool resumed = false;
    uint32_t discard_ms = 0;

    if (reason == 0) {
        return;
    }
    portENTER_CRITICAL(&s_feed_mux);
    const uint32_t before = s_feed_hold_mask;
    s_feed_hold_mask = before & ~reason;
    s_feed_releases++;
    mask = s_feed_hold_mask;
    if (before != 0 && mask == 0) {
        resumed = true;
        discard_ms = s_feed_discard_pending_ms;
        s_feed_discard_pending_ms = 0;
        if (discard_ms > 0) {
            s_feed_discard_left = discard_ms * AUDIO_IO_MIC_SAMPLE_RATE / 1000u;
            s_feed_discards++;
            s_feed_last_discard_ms = discard_ms;
        }
    }
    portEXIT_CRITICAL(&s_feed_mux);

    if (resumed) {
        ESP_LOGI(TAG, "feed resumed (discard %u ms, hold_mask=0x%02x)", (unsigned)discard_ms,
                 (unsigned)mask);
    }
}

void audio_fe_feed_discard_ms(uint32_t ms)
{
    portENTER_CRITICAL(&s_feed_mux);
    if (s_feed_hold_mask == 0) {
        /* 已经恢复：立即开窗（等价于"现在起丢弃 N ms"） */
        if (ms > 0) {
            s_feed_discard_left = ms * AUDIO_IO_MIC_SAMPLE_RATE / 1000u;
            s_feed_discards++;
            s_feed_last_discard_ms = ms;
        }
    } else {
        s_feed_discard_pending_ms = ms;
    }
    portEXIT_CRITICAL(&s_feed_mux);
}

void audio_fe_set_feed_enabled(bool enabled)
{
    /* 兼容旧接口：映射到 PROBE 原因位（产品路径请用 hold/release） */
    if (enabled) {
        audio_fe_feed_release(AUDIO_FE_HOLD_PROBE);
    } else {
        audio_fe_feed_hold(AUDIO_FE_HOLD_PROBE);
    }
}

bool audio_fe_feed_enabled(void)
{
    return s_feed_hold_mask == 0;
}

uint32_t audio_fe_feed_hold_mask(void)
{
    return s_feed_hold_mask;
}

esp_err_t audio_fe_feed_stats_read(audio_fe_feed_stats_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_feed_mux);
    out->holds = s_feed_holds;
    out->releases = s_feed_releases;
    out->hold_mask = s_feed_hold_mask;
    out->discards = s_feed_discards;
    out->discard_samples = s_feed_discard_samples;
    out->last_discard_ms = s_feed_last_discard_ms;
    out->feed_enabled = (s_feed_hold_mask == 0);
    portEXIT_CRITICAL(&s_feed_mux);
    return ESP_OK;
}

const char *audio_fe_wakenet_name(void)
{
    return s_wn_name[0] ? s_wn_name : NULL;
}

esp_err_t audio_fe_stats_read(audio_fe_stats_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }

    portENTER_CRITICAL(&s_stats_mux);
    out->sum_sq = s_stat_sum_sq;
    out->sample_count = s_stat_samples;
    out->peak = s_stat_peak;
    out->frames_fed = s_stat_frames;
    out->mic_read_errors = s_stat_read_errors;
    out->wake_count = s_stat_wake_count;
    portEXIT_CRITICAL(&s_stats_mux);

    out->chunksize = s_stat_chunksize;
    out->sample_rate = AUDIO_IO_MIC_SAMPLE_RATE;
    out->feed_enabled = audio_fe_feed_enabled();
    out->vad_active = s_stat_vad_active;

    audio_fe_feed_stats_t feed;
    if (audio_fe_feed_stats_read(&feed) == ESP_OK) {
        out->feed_holds = feed.holds;
        out->feed_releases = feed.releases;
        out->feed_hold_mask = feed.hold_mask;
        out->feed_discards = feed.discards;
        out->feed_discard_samples = feed.discard_samples;
        out->feed_last_discard_ms = feed.last_discard_ms;
    }
    return ESP_OK;
}

esp_err_t audio_fe_stats_reset(void)
{
    portENTER_CRITICAL(&s_stats_mux);
    s_stat_sum_sq = 0;
    s_stat_samples = 0;
    s_stat_peak = 0;
    s_stat_frames = 0;
    s_stat_read_errors = 0;
    s_stat_wake_count = 0;
    portEXIT_CRITICAL(&s_stats_mux);

    /* 门控累计统计一起清零（hold_mask 是状态、不清），方便测试脚本"清零后断言一次 R5" */
    portENTER_CRITICAL(&s_feed_mux);
    s_feed_holds = 0;
    s_feed_releases = 0;
    s_feed_discards = 0;
    s_feed_discard_samples = 0;
    s_feed_last_discard_ms = 0;
    portEXIT_CRITICAL(&s_feed_mux);
    return ESP_OK;
}
