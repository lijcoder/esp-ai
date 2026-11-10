/*
 * T4.1 远端 MP3 音源：HTTP(S) 拉流 → 独立 minimp3 实例 → 48k 单声道 → CH_MUSIC。
 *
 * 为什么是独立任务：HTTP/TLS 会阻塞、单帧解码 4~5ms，都不能挤进 10ms 的混音块。
 * 解码后的 PCM 只写 ring；音量/duck/限幅仍由 mix_render() 统一做（CONVENTIONS §3）。
 * 任务只在播放期间存在：停止后自行退出并销毁，不占常驻内部 RAM。
 */

#include "source_http.h"

#include <string.h>

#include "audio_io.h"
#include "audio_player.h"
#include "decoder_mp3.h"

#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "PLAYER"; /* 复用 PLAYER TAG：不新增 TAG 清单/测试口径 */

#define MUSIC_TASK_STACK     CONFIG_AUDIO_PLAYER_MUSIC_TASK_STACK
#define MUSIC_TASK_PRIO      4
#define MUSIC_TASK_CORE      0
#define MUSIC_HTTP_BUF       4096
#define MUSIC_OUT_FRAMES     2048   /* 44.1k 1152 样本 → 48k ≈ 1254，留余量 */
#define MUSIC_PREFILL_FRAMES (AUDIO_IO_SPK_SAMPLE_RATE * 150u / 1000u) /* 起播前预填 ~150ms */

typedef struct {
    int16_t *out;      /* PSRAM：单帧重采样输出 */
    int16_t *prefill;  /* PSRAM：起播预填 */
    size_t prefill_n;
    uint32_t src_rate;
    uint32_t step_q16; /* src_rate × 65536 / 48000 */
    uint32_t pos_q16;  /* 当前帧内读位置（q16） */
} music_ctx_t;

static char s_url[CONFIG_AUDIO_PLAYER_MUSIC_URL_MAX];
static volatile bool s_stop_req;
static volatile bool s_running;
static portMUX_TYPE s_stats_mux = portMUX_INITIALIZER_UNLOCKED;
static source_http_stats_t s_stats;

/* ---------------- 统计（生产者写、diag 读） ---------------- */

#define STAT_ADD(field, value)                       \
    do {                                             \
        portENTER_CRITICAL(&s_stats_mux);            \
        s_stats.field += (value);                    \
        portEXIT_CRITICAL(&s_stats_mux);             \
    } while (0)

#define STAT_SET(field, value)                       \
    do {                                             \
        portENTER_CRITICAL(&s_stats_mux);            \
        s_stats.field = (value);                     \
        portEXIT_CRITICAL(&s_stats_mux);             \
    } while (0)

bool source_http_active(void)
{
    return s_running;
}

const char *source_http_url(void)
{
    return s_url;
}

esp_err_t source_http_stats_read(source_http_stats_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_stats_mux);
    *out = s_stats;
    portEXIT_CRITICAL(&s_stats_mux);
    out->active = s_running;
    return ESP_OK;
}

/* 日志里只留 scheme://host/path，不打印 query（可能带签名/token） */
static void log_url_safe(const char *url)
{
    char safe[CONFIG_AUDIO_PLAYER_MUSIC_URL_MAX];
    size_t n = 0;
    while (url[n] && url[n] != '?' && n + 1 < sizeof(safe)) {
        safe[n] = url[n];
        n++;
    }
    safe[n] = '\0';
    ESP_LOGI(TAG, "拉流 %s", safe);
}

/* ---------------- 重采样（src_rate → 48k 单声道，线性 + 跨帧 q16 游标） ---------------- */

static size_t resample_frame(music_ctx_t *ctx, const int16_t *in, size_t n, int16_t *out, size_t cap)
{
    size_t o = 0;
    while (o < cap) {
        const uint32_t i = ctx->pos_q16 >> 16;
        if (i >= n) {
            break;
        }
        const uint32_t frac = ctx->pos_q16 & 0xFFFFu;
        const int32_t s0 = in[i];
        const int32_t s1 = (i + 1u < n) ? in[i + 1u] : s0;
        out[o++] = (int16_t)(s0 + (((s1 - s0) * (int32_t)frac) >> 16));
        ctx->pos_q16 += ctx->step_q16;
    }
    const uint32_t consumed = (uint32_t)n << 16;
    if (ctx->pos_q16 >= consumed) {
        ctx->pos_q16 -= consumed;
    } else {
        ctx->pos_q16 = 0;
    }
    return o;
}

/* ---------------- 拉流任务 ---------------- */

static void music_task(void *arg)
{
    (void)arg;
    music_ctx_t ctx = {0};
    esp_http_client_handle_t cli = NULL;
    mp3_stream_t *dec = NULL;
    uint8_t *rbuf = NULL;
    bool started = false;
    const int64_t t0 = esp_timer_get_time();

    ctx.out = heap_caps_malloc((size_t)MUSIC_OUT_FRAMES * sizeof(int16_t),
                               MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    /* 预填缓冲多留一帧：解码帧重采样后可能比剩余空位长，整帧写入不能截断
     * （截断会丢掉该帧后半段，起播处出现可闻缺口） */
    ctx.prefill = heap_caps_malloc((size_t)(MUSIC_PREFILL_FRAMES + MUSIC_OUT_FRAMES) * sizeof(int16_t),
                                   MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    rbuf = heap_caps_malloc(MUSIC_HTTP_BUF, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    dec = mp3_stream_create();
    if (!ctx.out || !ctx.prefill || !rbuf || !dec) {
        ESP_LOGE(TAG, "缓冲/解码器分配失败（PSRAM/内部 RAM 不足）");
        goto done;
    }

    esp_http_client_config_t cfg = {
        .url = s_url,
        .timeout_ms = CONFIG_AUDIO_PLAYER_MUSIC_HTTP_TIMEOUT_MS,
        .buffer_size = MUSIC_HTTP_BUF,
        .buffer_size_tx = 512,
        .user_agent = "esp-ai-music/1.0",
        .keep_alive_enable = true,
        .crt_bundle_attach = esp_crt_bundle_attach,
    };
    cli = esp_http_client_init(&cfg);
    if (!cli) {
        ESP_LOGE(TAG, "http client init 失败");
        STAT_ADD(http_errors, 1);
        goto done;
    }

    log_url_safe(s_url);
    const int64_t t_open = esp_timer_get_time();
    if (esp_http_client_open(cli, 0) != ESP_OK) {
        ESP_LOGE(TAG, "HTTP open 失败（%s）", s_url);
        STAT_ADD(http_errors, 1);
        goto done;
    }
    const int content_len = esp_http_client_fetch_headers(cli);
    const int status = esp_http_client_get_status_code(cli);
    STAT_SET(http_status, status);
    STAT_SET(content_length, content_len);
    STAT_SET(connect_ms, (uint32_t)((esp_timer_get_time() - t_open) / 1000));
    ESP_LOGI(TAG, "HTTP %d，Content-Length=%d，连接 %u ms", status, content_len,
             (unsigned)((esp_timer_get_time() - t_open) / 1000));
    if (status != 200 && status != 206) {
        ESP_LOGE(TAG, "HTTP 状态 %d 不可播放", status);
        STAT_ADD(http_errors, 1);
        goto done;
    }

    /* 预填 ~150ms：先不进混音，避免起播时的"空 ring 欠载" */
    while (!s_stop_req && ctx.prefill_n < MUSIC_PREFILL_FRAMES) {
        const int r = esp_http_client_read(cli, (char *)rbuf, MUSIC_HTTP_BUF);
        if (r < 0) {
            ESP_LOGE(TAG, "HTTP read 失败");
            STAT_ADD(http_errors, 1);
            goto done;
        }
        if (r == 0) {
            break;
        }
        if (!s_stats.first_byte_ms) {
            STAT_SET(first_byte_ms, (uint32_t)((esp_timer_get_time() - t0) / 1000));
        }
        STAT_ADD(http_bytes, (uint32_t)r);
        STAT_ADD(read_calls, 1);
        if (mp3_stream_feed(dec, rbuf, (size_t)r) != ESP_OK) {
            continue; /* pending 溢出：解码器已自行 resync */
        }
        while (ctx.prefill_n < MUSIC_PREFILL_FRAMES) {
            const int16_t *pcm = NULL;
            uint32_t samples = 0, rate = 0, frame_bytes = 0;
            uint8_t ch = 0;
            const esp_err_t err = mp3_stream_frame(dec, &pcm, &samples, &rate, &ch, &frame_bytes);
            if (err == ESP_ERR_NOT_FOUND) {
                break;
            }
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "解码失败：%s", esp_err_to_name(err));
                STAT_ADD(http_errors, 1);
                goto done;
            }
            if (ctx.src_rate == 0) {
                ctx.src_rate = rate;
                ctx.step_q16 = (uint32_t)(((uint64_t)rate << 16) / 48000u);
                ESP_LOGI(TAG, "源参数：%u Hz / %u ch（→48k 单声道）", (unsigned)rate, (unsigned)ch);
            } else if (rate != ctx.src_rate) {
                ESP_LOGE(TAG, "流中采样率变化（%u → %u Hz），本轮中止", (unsigned)ctx.src_rate,
                         (unsigned)rate);
                STAT_ADD(http_errors, 1);
                goto done;
            }
            /* 整帧重采样后再判断是否够 150ms：cap 固定为单帧上限，避免截断丢样本 */
            const size_t out = resample_frame(&ctx, pcm, samples, ctx.prefill + ctx.prefill_n,
                                              (size_t)MUSIC_OUT_FRAMES);
            ctx.prefill_n += out;
            STAT_ADD(decoded_frames, 1);
        }
    }

    if (s_stop_req) {
        goto done;
    }
    if (ctx.prefill_n == 0) {
        ESP_LOGE(TAG, "没有解出有效音频（不是 MP3 或流为空）");
        STAT_ADD(http_errors, 1);
        goto done;
    }

    if (audio_player_music_start(ctx.src_rate) != ESP_OK) {
        ESP_LOGE(TAG, "音乐通道启动失败（播放器忙？）");
        STAT_ADD(http_errors, 1);
        goto done;
    }
    started = true;
    size_t written = audio_player_music_write(ctx.prefill, ctx.prefill_n, 500);
    STAT_ADD(pcm_frames, (uint32_t)written);
    if (written < ctx.prefill_n) {
        STAT_ADD(write_timeouts, 1);
    }

    while (!s_stop_req) {
        const int r = esp_http_client_read(cli, (char *)rbuf, MUSIC_HTTP_BUF);
        if (r < 0) {
            ESP_LOGW(TAG, "HTTP read 失败（流中断）");
            STAT_ADD(http_errors, 1);
            break;
        }
        if (r == 0) {
            break; /* EOF / 服务端关闭 */
        }
        STAT_ADD(http_bytes, (uint32_t)r);
        STAT_ADD(read_calls, 1);
        if (mp3_stream_feed(dec, rbuf, (size_t)r) != ESP_OK) {
            continue;
        }
        for (;;) {
            const int16_t *pcm = NULL;
            uint32_t samples = 0, rate = 0, frame_bytes = 0;
            uint8_t ch = 0;
            const esp_err_t err = mp3_stream_frame(dec, &pcm, &samples, &rate, &ch, &frame_bytes);
            if (err == ESP_ERR_NOT_FOUND) {
                break;
            }
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "解码失败：%s", esp_err_to_name(err));
                s_stop_req = true;
                break;
            }
            if (rate != ctx.src_rate) {
                ESP_LOGE(TAG, "流中采样率变化（%u → %u Hz），本轮中止", (unsigned)ctx.src_rate,
                         (unsigned)rate);
                s_stop_req = true;
                break;
            }
            const size_t out = resample_frame(&ctx, pcm, samples, ctx.out, MUSIC_OUT_FRAMES);
            size_t off = 0;
            while (off < out && !s_stop_req) {
                const size_t n = audio_player_music_write(ctx.out + off, out - off, 200);
                STAT_ADD(pcm_frames, (uint32_t)n);
                off += n;
                if (n == 0) {
                    break; /* 停止 */
                }
                if (off < out) {
                    STAT_ADD(write_timeouts, 1);
                }
            }
            STAT_ADD(decoded_frames, 1);
        }
    }

done:
    if (started) {
        if (s_stop_req) {
            audio_player_music_stop(); /* stop() 已调过也没关系（幂等） */
        } else {
            audio_player_music_end(); /* 自然 EOF：播完 ring 再收尾 */
        }
    }
    if (cli) {
        esp_http_client_close(cli);
        esp_http_client_cleanup(cli);
    }
    if (dec) {
        mp3_stream_destroy(dec);
    }
    heap_caps_free(rbuf);
    heap_caps_free(ctx.prefill);
    heap_caps_free(ctx.out);
    ESP_LOGI(TAG, "拉流任务退出（%s）", s_stop_req ? "stop" : "eof/error");
    s_running = false;
    vTaskDelete(NULL);
}

esp_err_t source_http_play(const char *url)
{
    if (!url || !url[0]) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!mp3_dec_ready()) {
        return ESP_ERR_NOT_SUPPORTED;
    }
    if (s_running) {
        return ESP_ERR_INVALID_STATE;
    }
    const size_t len = strlen(url);
    if (len + 1 > sizeof(s_url)) {
        return ESP_ERR_INVALID_SIZE;
    }
    if (strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0) {
        return ESP_ERR_INVALID_ARG;
    }
    memcpy(s_url, url, len + 1);
    s_stop_req = false;
    portENTER_CRITICAL(&s_stats_mux);
    memset(&s_stats, 0, sizeof(s_stats));
    s_stats.content_length = -1;
    portEXIT_CRITICAL(&s_stats_mux);
    s_running = true;
    if (xTaskCreatePinnedToCore(music_task, "music_http", MUSIC_TASK_STACK, NULL,
                                MUSIC_TASK_PRIO, NULL, MUSIC_TASK_CORE) != pdPASS) {
        s_running = false;
        s_url[0] = '\0';
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t source_http_stop(void)
{
    if (!s_running) {
        return ESP_ERR_INVALID_STATE;
    }
    s_stop_req = true;
    audio_player_music_stop(); /* 立即静音（ring 丢弃 + 100ms 淡出） */
    return ESP_OK;
}
