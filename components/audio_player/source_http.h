#pragma once

/*
 * source_http —— T4.1 远端 MP3 音源（audio_player 内部接口）。
 *
 * 独立任务（music_http，核 0 / 优先级 4 / 栈见 Kconfig）：
 *   esp_http_client 拉流 → minimp3 独立解码实例 → 重采样到 48k → 写 CH_MUSIC 的 PCM ring。
 * 任务只在播放期间存在（停止后销毁），不占常驻内部 RAM；解码/HTTP 都不在音频任务里。
 * URL 只接受 http/https；设备侧不做任何搜索/解析（URL 来自服务端 music_track）。
 */

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    bool active;
    uint32_t http_bytes;
    int http_status;
    int32_t content_length; /* -1 = chunked/未知 */
    uint32_t read_calls;
    uint32_t decoded_frames;
    uint32_t pcm_frames;   /* 写进 CH_MUSIC 的 48k 帧数 */
    uint32_t write_timeouts;
    uint32_t http_errors;
    uint32_t first_byte_ms;
    uint32_t connect_ms;
} source_http_stats_t;

/* 启动拉流任务；已有一个在跑时返回 ESP_ERR_INVALID_STATE。 */
esp_err_t source_http_play(const char *url);

/* 请求停止：立即静音（music_stop），任务在 HTTP 读超时内自行退出并销毁。
 * T4.3：暂停态也必须能停止（取消暂停门控、丢弃 ring、active=false）。 */
esp_err_t source_http_stop(void);

/* T4.3 暂停门控：任务不退出、不读 socket（ring 满靠 TCP 背压），
 * `audio_player_music_write()` 在暂停期立即返回 0（不计 timeout/underrun）。
 * 暂停期用 50ms 轮询等待；stop 会把任务从等待中唤醒并退出。 */
esp_err_t source_http_pause(void);
esp_err_t source_http_resume(void);
bool source_http_paused(void);

bool source_http_active(void);

/* 最近一次 play 的 URL（空串表示没有）；用于 diag 展示，注意日志不要打印 query。 */
const char *source_http_url(void);

esp_err_t source_http_stats_read(source_http_stats_t *out);
