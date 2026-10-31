/*
 * MP3 解码（T2.3）：minimp3 单文件解码器，状态与临时区全放 PSRAM。
 *
 * 为什么是 minimp3 而不是 Helix（ROADMAP 旧版写的是 Helix）：Helix 的 8 个解码
 * 结构体实测 23,856 B，且 AllocateBuffers() 用裸 malloc，托管组件改不了分配器；
 * minimp3 零 malloc，状态 6,668 B + 临时区 16,256 B 都由调用方提供（见
 * third_party/minimp3/README.md 的 3 处补丁），两者都放 PSRAM 后内部 RAM 增量 0，
 * 满足 CONVENTIONS §4 的 wss 握手期水位约束。
 *
 * 解码在播放任务内做（ARCH §9.3），所以这里不允许任何动态分配：状态 + PCM 缓冲
 * 在 mp3_dec_init()（app_main 上下文）一次性预分配。
 */

#include "decoder_mp3.h"

#if CONFIG_AUDIO_PLAYER_ENABLE_MP3

#define MINIMP3_IMPLEMENTATION
#define MINIMP3_ONLY_MP3
#include "minimp3.h"

#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "sdkconfig.h"

static const char *TAG = "PLAYER";

/* PSRAM：解码状态 6.7KB + 临时区 16.3KB + 单帧输出 4.6KB + PCM 缓冲（Kconfig 上限） */
typedef struct {
    mp3dec_t dec;
    mp3dec_scratch_t scratch;
    mp3d_sample_t frame[MINIMP3_MAX_SAMPLES_PER_FRAME];
} mp3_state_t;

static mp3_state_t *s_state;
static int16_t *s_pcm;
static size_t s_pcm_cap; /* 字节 */
static mp3_dec_stats_t s_stats;
static bool s_ready;

static size_t skip_id3v2(const uint8_t *p, size_t len)
{
    size_t off = 0;

    /* 连续跳过多个 ID3v2 标签（少数文件有多个）；大小是 syncsafe 4×7bit */
    while (off + 10 <= len && memcmp(p + off, "ID3", 3) == 0) {
        const uint8_t *h = p + off;
        size_t tag_size = ((size_t)(h[6] & 0x7f) << 21) | ((size_t)(h[7] & 0x7f) << 14) |
                          ((size_t)(h[8] & 0x7f) << 7) | (size_t)(h[9] & 0x7f);
        size_t total = 10 + tag_size;
        if (h[5] & 0x10) { /* 带 footer */
            total += 10;
        }
        if (off + total > len) {
            return len; /* 标签不完整：整段丢弃 */
        }
        off += total;
    }
    return off;
}

esp_err_t mp3_dec_init(void)
{
    if (s_ready) {
        return ESP_OK;
    }

    s_state = heap_caps_malloc(sizeof(mp3_state_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_pcm = heap_caps_malloc(CONFIG_AUDIO_PLAYER_MP3_PCM_MAX_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_state || !s_pcm) {
        ESP_LOGE(TAG, "MP3 解码缓冲分配失败（PSRAM 不足）");
        heap_caps_free(s_pcm);
        heap_caps_free(s_state);
        s_state = NULL;
        s_pcm = NULL;
        return ESP_ERR_NO_MEM;
    }
    s_pcm_cap = CONFIG_AUDIO_PLAYER_MP3_PCM_MAX_BYTES;

    memset(s_state, 0, sizeof(*s_state));
    mp3dec_init(&s_state->dec);
    /* 补丁：把 16.3KB 临时区从任务栈改到 PSRAM（播放任务栈只有 6KB） */
    mp3dec_set_scratch(&s_state->scratch);

    memset(&s_stats, 0, sizeof(s_stats));
    s_ready = true;
    ESP_LOGI(TAG, "mp3: minimp3 ready, state=%u B psram, pcm_cap=%u B psram",
             (unsigned)sizeof(mp3_state_t), (unsigned)s_pcm_cap);
    return ESP_OK;
}

bool mp3_dec_ready(void)
{
    return s_ready;
}

esp_err_t mp3_dec_decode_all(const uint8_t *data, size_t len,
                             const int16_t **pcm, size_t *samples, uint32_t *rate)
{
    if (!s_ready) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!data || !pcm || !samples || !rate || len < 4) {
        return ESP_ERR_INVALID_ARG;
    }

    memset(&s_stats, 0, sizeof(s_stats));
    mp3dec_init(&s_state->dec);

    size_t off = skip_id3v2(data, len);
    s_stats.skipped_bytes = (uint32_t)off;

    size_t out = 0;
    uint32_t rate_seen = 0;
    uint8_t ch_seen = 0;
    bool first = true;

    while (off + 4 <= len) {
        mp3dec_frame_info_t info;
        int64_t t0 = esp_timer_get_time();
        int got = mp3dec_decode_frame(&s_state->dec, data + off, (int)(len - off),
                                      s_state->frame, &info);
        uint32_t us = (uint32_t)(esp_timer_get_time() - t0);

        if (got <= 0) {
            if (info.frame_bytes > 0) {
                /* 首帧前的垃圾/半帧：跳过后继续找同步字（电台流从中间接入即此情形） */
                s_stats.skipped_bytes += (uint32_t)info.frame_bytes;
                off += (size_t)info.frame_bytes;
                continue;
            }
            s_stats.bad_frames++;
            break;
        }

        /* 采样率/声道以首帧为准；后续帧不一致视为损坏素材（分段流不在此期支持） */
        if (first) {
            rate_seen = (uint32_t)info.hz;
            ch_seen = (uint8_t)info.channels;
            first = false;
        } else if (rate_seen != (uint32_t)info.hz || ch_seen != (uint8_t)info.channels) {
            ESP_LOGW(TAG, "mp3: 帧参数变化（%u Hz/%u ch → %u Hz/%u ch），截断在该帧",
                     (unsigned)rate_seen, (unsigned)ch_seen, (unsigned)info.hz,
                     (unsigned)info.channels);
            break;
        }

        if ((out + (size_t)got) * sizeof(int16_t) > s_pcm_cap) {
            ESP_LOGW(TAG, "mp3: PCM 缓冲不足（上限 %u B），拒绝该素材", (unsigned)s_pcm_cap);
            return ESP_ERR_NO_MEM;
        }

        /* 双声道取 (L+R)/2 转单声道：16bit 均值误差 ≤1 LSB，与 FFmpeg downmix 对拍通过 */
        if (info.channels == 1) {
            memcpy(s_pcm + out, s_state->frame, (size_t)got * sizeof(int16_t));
        } else {
            for (int i = 0; i < got; i++) {
                s_pcm[out + i] = (int16_t)(((int32_t)s_state->frame[2 * i] +
                                            (int32_t)s_state->frame[2 * i + 1]) / 2);
            }
        }
        out += (size_t)got;

        s_stats.frames++;
        s_stats.decode_us_total += us;
        if (us > s_stats.decode_us_max) {
            s_stats.decode_us_max = us;
        }
        off += (size_t)info.frame_bytes;
    }

    if (out == 0 || rate_seen == 0) {
        ESP_LOGW(TAG, "mp3: 未解出有效音频（bytes=%u skipped=%u bad=%u）",
                 (unsigned)len, (unsigned)s_stats.skipped_bytes, (unsigned)s_stats.bad_frames);
        return ESP_ERR_INVALID_ARG;
    }

    s_stats.samples = (uint32_t)out;
    s_stats.rate = rate_seen;
    s_stats.channels = ch_seen;

    *pcm = s_pcm;
    *samples = out;
    *rate = rate_seen;

    uint32_t avg = s_stats.frames ? s_stats.decode_us_total / s_stats.frames : 0;
    ESP_LOGI(TAG, "mp3 decoded: frames=%u samples=%u rate=%u ch=%u dur=%ums "
                  "dec_avg=%uus dec_max=%uus skipped=%u bad=%u",
             (unsigned)s_stats.frames, (unsigned)out, (unsigned)rate_seen,
             (unsigned)ch_seen, (unsigned)((uint64_t)out * 1000 / rate_seen),
             (unsigned)avg, (unsigned)s_stats.decode_us_max,
             (unsigned)s_stats.skipped_bytes, (unsigned)s_stats.bad_frames);
    return ESP_OK;
}

void mp3_dec_stats_read(mp3_dec_stats_t *out)
{
    if (out) {
        *out = s_stats;
    }
}

void mp3_dec_stats_reset(void)
{
    memset(&s_stats, 0, sizeof(s_stats));
}

#else /* !CONFIG_AUDIO_PLAYER_ENABLE_MP3：保留接口、不编解码器，提示音链路不受影响 */

esp_err_t mp3_dec_init(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

bool mp3_dec_ready(void)
{
    return false;
}

esp_err_t mp3_dec_decode_all(const uint8_t *data, size_t len,
                             const int16_t **pcm, size_t *samples, uint32_t *rate)
{
    (void)data;
    (void)len;
    (void)pcm;
    (void)samples;
    (void)rate;
    return ESP_ERR_NOT_SUPPORTED;
}

void mp3_dec_stats_read(mp3_dec_stats_t *out)
{
    if (out) {
        memset(out, 0, sizeof(*out));
    }
}

void mp3_dec_stats_reset(void)
{
}

#endif /* CONFIG_AUDIO_PLAYER_ENABLE_MP3 */
