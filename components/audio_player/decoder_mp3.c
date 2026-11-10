/*
 * MP3 解码（T2.3 整段 / T2.4 流式）：minimp3 单文件解码器，状态与临时区全放 PSRAM。
 *
 * 为什么是 minimp3 而不是 Helix（ROADMAP 旧版写的是 Helix）：Helix 的 8 个解码
 * 结构体实测 23,856 B，且 AllocateBuffers() 用裸 malloc，托管组件改不了分配器；
 * minimp3 零 malloc，状态 6,668 B + 临时区 16,256 B 都由调用方提供（见
 * third_party/minimp3/README.md 的 3 处补丁），两者都放 PSRAM 后内部 RAM 增量 0，
 * 满足 CONVENTIONS §4 的 wss 握手期水位约束。
 *
 * 解码在播放任务内做（ARCH §9.3），所以这里不允许任何动态分配：状态 + PCM 缓冲
 * 在 mp3_dec_init()（app_main 上下文）一次性预分配。
 *
 * T2.4 流式：下行 0x02 分块到达，块尾常是半帧。做法是先用帧头算出帧长，凑满
 * 一整帧才交给 minimp3 —— minimp3 在"数据不足"分支会 memset 解码器状态（丢比特池），
 * 每次分块都踩一次会让部分帧解出噪声。半帧数据留在 PSRAM pending 区跨 chunk 续上；
 * 每块最多 2KB，pending 8KB 足够放"上一块残留的半帧 + 本块"。
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

/* pending 上限：单帧理论最大 1441 B（MPEG1 320kbps@32k）+ 一次 feed ≤2KB，8KB 留足余量 */
#define MP3_STREAM_PENDING_BYTES 8192
/* minimp3 的 frame[] 是交织的多声道样本，单声道输出最多取一半 */
#define MP3_MAX_SAMPLES_PER_CH   (MINIMP3_MAX_SAMPLES_PER_FRAME / 2)

/* PSRAM：解码状态 6.7KB + 临时区 16.3KB + 单帧输出 4.6KB + 单声道帧 2.3KB
 * + pending 8KB + PCM 缓冲（Kconfig 上限，仅整段路径用） */
struct mp3_stream {
    mp3dec_t dec;
    mp3dec_scratch_t scratch;
    mp3d_sample_t frame[MINIMP3_MAX_SAMPLES_PER_FRAME];
    int16_t mono[MP3_MAX_SAMPLES_PER_CH]; /* 流式单声道输出（双声道取均值） */
    uint8_t pending[MP3_STREAM_PENDING_BYTES];
    size_t pending_len;
    uint32_t abs_fed; /* 累计 feed 字节，仅用于 resync 日志的绝对偏移 */
    bool stream_ready;
    bool resync_req;
    mp3_dec_stats_t stats;   /* 本实例的流式统计（TTS 实例 / 音乐实例各自独立） */
    int64_t last_warn_us;    /* 坏数据 WARN 限频 */
};

typedef struct mp3_stream mp3_state_t;

/* 默认实例：TTS 下行（mp3_dec_stream_* 旧接口）。音乐用 mp3_stream_create() 另建实例。 */
static mp3_state_t *s_tts;
static int16_t *s_pcm;
static size_t s_pcm_cap; /* 字节 */
static mp3_dec_stats_t s_all_stats; /* decode_all 的统计（整段路径） */
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

/* MP3 Layer III 帧头解析（只做流式帧长判定；minimp3 仍负责实际解码） */
static bool mp3_parse_header(const uint8_t *h, size_t len, uint32_t *frame_bytes,
                             uint32_t *hz, uint8_t *channels, uint32_t *samples)
{
    static const uint16_t bitrate_mpeg1_l3[15] = {0,  32, 40, 48,  56,  64,  80,  96,
                                                  112, 128, 160, 192, 224, 256, 320};
    static const uint16_t bitrate_mpeg2_l3[15] = {0, 8,  16, 24, 32, 40, 48, 56,
                                                  64, 80, 96, 112, 128, 144, 160};
    static const uint32_t rate_mpeg1[3] = {44100, 48000, 32000};
    static const uint32_t rate_mpeg2[3] = {22050, 24000, 16000};
    static const uint32_t rate_mpeg25[3] = {11025, 12000, 8000};

    if (len < 4 || !frame_bytes || !hz || !channels || !samples) {
        return false;
    }
    if (h[0] != 0xFF || (h[1] & 0xE0) != 0xE0) {
        return false;
    }
    const uint8_t ver = (uint8_t)((h[1] >> 3) & 0x3); /* 0=2.5 1=reserved 2=MPEG2 3=MPEG1 */
    if (ver == 1) {
        return false;
    }
    const uint8_t layer = (uint8_t)((h[1] >> 1) & 0x3); /* 1=Layer III */
    if (layer != 1) {
        return false;
    }
    const uint8_t br_idx = (uint8_t)((h[2] >> 4) & 0xF);
    const uint8_t sr_idx = (uint8_t)((h[2] >> 2) & 0x3);
    if (br_idx == 0 || br_idx == 0xF || sr_idx == 3) {
        return false; /* free format / bad index：流式不支持，交给重新同步 */
    }
    const uint8_t padding = (uint8_t)((h[2] >> 1) & 0x1);
    const uint32_t rate = (ver == 3) ? rate_mpeg1[sr_idx]
                                     : ((ver == 2) ? rate_mpeg2[sr_idx] : rate_mpeg25[sr_idx]);
    const uint32_t kbps = (ver == 3) ? bitrate_mpeg1_l3[br_idx] : bitrate_mpeg2_l3[br_idx];
    const uint32_t frame_len = ((ver == 3) ? 144u : 72u) * kbps * 1000u / rate + padding;

    if (frame_len == 0 || frame_len > MINIMP3_MAX_SAMPLES_PER_FRAME) {
        return false;
    }
    *frame_bytes = frame_len;
    *hz = rate;
    *channels = ((h[3] >> 6) & 0x3) == 3 ? 1 : 2;
    *samples = (ver == 3) ? 1152 : 576;
    return true;
}

static size_t stream_find_sync(const uint8_t *p, size_t len)
{
    for (size_t i = 0; i + 4 <= len; i++) {
        uint32_t fb, hz, ns;
        uint8_t ch;
        if (mp3_parse_header(p + i, len - i, &fb, &hz, &ch, &ns)) {
            return i;
        }
    }
    return SIZE_MAX;
}

/* 坏数据/丢块 WARN 限频 1/s：下行抖动时不能刷屏 */
static void stream_warn_resync(mp3_state_t *st, uint32_t pos)
{
    if (!st) {
        return;
    }
    int64_t now = esp_timer_get_time();
    if (now - st->last_warn_us < 1000000) {
        return;
    }
    st->last_warn_us = now;
    ESP_LOGW(TAG, "stream bad data, resync at byte %u", (unsigned)pos);
}

static void pending_consume(mp3_state_t *st, size_t n)
{
    if (n >= st->pending_len) {
        st->pending_len = 0;
        return;
    }
    memmove(st->pending, st->pending + n, st->pending_len - n);
    st->pending_len -= n;
}

mp3_stream_t *mp3_stream_create(void)
{
    mp3_state_t *st = heap_caps_malloc(sizeof(mp3_state_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!st) {
        ESP_LOGE(TAG, "mp3 stream state alloc failed（PSRAM 不足，%u B）", (unsigned)sizeof(*st));
        return NULL;
    }
    memset(st, 0, sizeof(*st));
    mp3dec_init(&st->dec);
    /* 补丁：把 16.3KB 临时区从任务栈改到 PSRAM（各任务栈都很小） */
    mp3dec_set_scratch(&st->scratch);
    st->stream_ready = true;
    return st;
}

void mp3_stream_destroy(mp3_stream_t *st)
{
    if (st) {
        heap_caps_free(st);
    }
}

void mp3_stream_reset(mp3_stream_t *st)
{
    if (!st) {
        return;
    }
    mp3_state_t *p = (mp3_state_t *)st;   /* 同一类型（struct mp3_stream） */
    mp3dec_init(&p->dec);
    p->pending_len = 0;
    p->abs_fed = 0;
    p->resync_req = false;
    p->stream_ready = true;
    memset(&p->stats, 0, sizeof(p->stats));
    p->last_warn_us = 0;
}

esp_err_t mp3_dec_init(void)
{
    if (s_ready) {
        return ESP_OK;
    }

    s_tts = mp3_stream_create();
    s_pcm = heap_caps_malloc(CONFIG_AUDIO_PLAYER_MP3_PCM_MAX_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_tts || !s_pcm) {
        ESP_LOGE(TAG, "MP3 解码缓冲分配失败（PSRAM 不足）");
        heap_caps_free(s_pcm);
        if (s_tts) {
            mp3_stream_destroy(s_tts);
        }
        s_tts = NULL;
        s_pcm = NULL;
        return ESP_ERR_NO_MEM;
    }
    s_pcm_cap = CONFIG_AUDIO_PLAYER_MP3_PCM_MAX_BYTES;

    memset(&s_all_stats, 0, sizeof(s_all_stats));
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

    memset(&s_all_stats, 0, sizeof(s_all_stats));
    mp3dec_init(&s_tts->dec);

    size_t off = skip_id3v2(data, len);
    s_all_stats.skipped_bytes = (uint32_t)off;

    size_t out = 0;
    uint32_t rate_seen = 0;
    uint8_t ch_seen = 0;
    bool first = true;

    while (off + 4 <= len) {
        mp3dec_frame_info_t info;
        int64_t t0 = esp_timer_get_time();
        int got = mp3dec_decode_frame(&s_tts->dec, data + off, (int)(len - off),
                                      s_tts->frame, &info);
        uint32_t us = (uint32_t)(esp_timer_get_time() - t0);

        if (got <= 0) {
            if (info.frame_bytes > 0) {
                /* 首帧前的垃圾/半帧：跳过后继续找同步字（电台流从中间接入即此情形） */
                s_all_stats.skipped_bytes += (uint32_t)info.frame_bytes;
                off += (size_t)info.frame_bytes;
                continue;
            }
            s_all_stats.bad_frames++;
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
            memcpy(s_pcm + out, s_tts->frame, (size_t)got * sizeof(int16_t));
        } else {
            for (int i = 0; i < got; i++) {
                s_pcm[out + i] = (int16_t)(((int32_t)s_tts->frame[2 * i] +
                                            (int32_t)s_tts->frame[2 * i + 1]) / 2);
            }
        }
        out += (size_t)got;

        s_all_stats.frames++;
        s_all_stats.decode_us_total += us;
        if (us > s_all_stats.decode_us_max) {
            s_all_stats.decode_us_max = us;
        }
        off += (size_t)info.frame_bytes;
    }

    if (out == 0 || rate_seen == 0) {
        ESP_LOGW(TAG, "mp3: 未解出有效音频（bytes=%u skipped=%u bad=%u）",
                 (unsigned)len, (unsigned)s_all_stats.skipped_bytes, (unsigned)s_all_stats.bad_frames);
        return ESP_ERR_INVALID_ARG;
    }

    s_all_stats.samples = (uint32_t)out;
    s_all_stats.rate = rate_seen;
    s_all_stats.channels = ch_seen;

    *pcm = s_pcm;
    *samples = out;
    *rate = rate_seen;

    uint32_t avg = s_all_stats.frames ? s_all_stats.decode_us_total / s_all_stats.frames : 0;
    ESP_LOGI(TAG, "mp3 decoded: frames=%u samples=%u rate=%u ch=%u dur=%ums "
                  "dec_avg=%uus dec_max=%uus skipped=%u bad=%u",
             (unsigned)s_all_stats.frames, (unsigned)out, (unsigned)rate_seen,
             (unsigned)ch_seen, (unsigned)((uint64_t)out * 1000 / rate_seen),
             (unsigned)avg, (unsigned)s_all_stats.decode_us_max,
             (unsigned)s_all_stats.skipped_bytes, (unsigned)s_all_stats.bad_frames);
    return ESP_OK;
}

void mp3_dec_stats_read(mp3_dec_stats_t *out)
{
    if (out) {
        *out = s_all_stats;
    }
}

void mp3_dec_stats_reset(void)
{
    memset(&s_all_stats, 0, sizeof(s_all_stats));
}

void mp3_stream_stats_read(mp3_stream_t *st, mp3_dec_stats_t *out)
{
    if (!st || !out) {
        return;
    }
    *out = ((mp3_state_t *)st)->stats;
}

/* ---------------- 流式解码（T2.4） ---------------- */

esp_err_t mp3_stream_feed(mp3_stream_t *st, const uint8_t *data, size_t len)
{
    if (!st) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!data || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!st->stream_ready) {
        mp3_stream_reset(st);
    }
    if (len > MP3_STREAM_PENDING_BYTES - st->pending_len) {
        ESP_LOGW(TAG, "stream feed overflow（%u + %u > %u），丢弃该段并重新同步",
                 (unsigned)st->pending_len, (unsigned)len, (unsigned)MP3_STREAM_PENDING_BYTES);
        st->pending_len = 0;
        st->resync_req = true;
        st->abs_fed += (uint32_t)len;
        return ESP_ERR_NO_MEM;
    }
    memcpy(st->pending + st->pending_len, data, len);
    st->pending_len += len;
    st->abs_fed += (uint32_t)len;
    return ESP_OK;
}

esp_err_t mp3_stream_frame(mp3_stream_t *st, const int16_t **pcm, uint32_t *samples,
                           uint32_t *rate, uint8_t *channels, uint32_t *frame_bytes)
{
    if (!st) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!pcm || !samples || !rate || !channels) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!st->stream_ready) {
        return ESP_ERR_INVALID_STATE;
    }

    for (;;) {
        if (st->pending_len == 0) {
            return ESP_ERR_NOT_FOUND;
        }

        /* ID3v2：标签头/标签体可能跨 chunk，不完整就等下一段数据 */
        if (st->pending_len >= 3 && memcmp(st->pending, "ID3", 3) == 0) {
            if (st->pending_len < 10) {
                return ESP_ERR_NOT_FOUND;
            }
            const uint8_t *h = st->pending;
            size_t tag = ((size_t)(h[6] & 0x7f) << 21) | ((size_t)(h[7] & 0x7f) << 14) |
                         ((size_t)(h[8] & 0x7f) << 7) | (size_t)(h[9] & 0x7f);
            size_t total = 10 + tag + ((h[5] & 0x10) ? 10 : 0);
            if (total > st->pending_len) {
                return ESP_ERR_NOT_FOUND;
            }
            st->stats.skipped_bytes += (uint32_t)total;
            pending_consume(st, total);
            continue;
        }

        uint32_t fb = 0, hz = 0, ns = 0;
        uint8_t ch = 0;
        if (!mp3_parse_header(st->pending, st->pending_len, &fb, &hz, &ch, &ns)) {
            /* 开头连帧头都不完整：等下一段（可能是被切开的 0xFF/半帧头） */
            if (st->pending_len < 4 && st->pending[0] == 0xFF &&
                (st->pending_len < 2 || (st->pending[1] & 0xE0) == 0xE0)) {
                return ESP_ERR_NOT_FOUND;
            }
            size_t off = stream_find_sync(st->pending, st->pending_len);
            if (off == SIZE_MAX) {
                /* 找不到同步字：保留最后 1 字节（可能是被切开的 0xFF） */
                size_t drop = st->pending_len > 1 ? st->pending_len - 1 : 0;
                if (drop > 0) {
                    st->stats.skipped_bytes += (uint32_t)drop;
                    pending_consume(st, drop);
                    stream_warn_resync(st, st->abs_fed - (uint32_t)st->pending_len);
                }
                return ESP_ERR_NOT_FOUND;
            }
            if (off > 0) {
                st->stats.skipped_bytes += (uint32_t)off;
                pending_consume(st, off);
                stream_warn_resync(st, st->abs_fed - (uint32_t)st->pending_len);
            }
            st->resync_req = false;
            continue;
        }
        st->resync_req = false;

        /* 半帧跨界：等满一整帧再交给 minimp3（避免它重置解码器丢比特池） */
        if (st->pending_len < fb) {
            return ESP_ERR_NOT_FOUND;
        }

        int64_t t0 = esp_timer_get_time();
        mp3dec_frame_info_t info;
        int got = mp3dec_decode_frame(&st->dec, st->pending, (int)fb,
                                      st->frame, &info);
        uint32_t us = (uint32_t)(esp_timer_get_time() - t0);

        if (got <= 0) {
            /* 整帧坏数据：跳过该帧并重新找同步字 */
            st->stats.bad_frames++;
            st->stats.skipped_bytes += fb;
            pending_consume(st, fb);
            st->resync_req = true;
            stream_warn_resync(st, st->abs_fed - (uint32_t)st->pending_len);
            continue;
        }

        if (info.channels == 1) {
            memcpy(st->mono, st->frame, (size_t)got * sizeof(int16_t));
        } else {
            for (int i = 0; i < got; i++) {
                st->mono[i] = (int16_t)(((int32_t)st->frame[2 * i] +
                                              (int32_t)st->frame[2 * i + 1]) / 2);
            }
        }
        pending_consume(st, fb);

        st->stats.frames++;
        st->stats.samples += (uint32_t)got;
        st->stats.rate = (uint32_t)info.hz;
        st->stats.channels = (uint8_t)info.channels;
        st->stats.decode_us_total += us;
        if (us > st->stats.decode_us_max) {
            st->stats.decode_us_max = us;
        }

        *pcm = st->mono;
        *samples = (uint32_t)got;
        *rate = (uint32_t)info.hz;
        *channels = (uint8_t)info.channels;
        if (frame_bytes) {
            *frame_bytes = fb;
        }
        return ESP_OK;
    }
}

void mp3_stream_resync(mp3_stream_t *st)
{
    if (!st) {
        return;
    }
    st->stats.skipped_bytes += (uint32_t)st->pending_len;
    st->pending_len = 0;
    st->resync_req = true;
}

/* ---------------- 兼容接口（TTS 默认实例） ---------------- */

void mp3_dec_stream_reset(void)
{
    if (s_ready && s_tts) {
        mp3_stream_reset(s_tts);
    }
}

esp_err_t mp3_dec_stream_feed(const uint8_t *data, size_t len)
{
    if (!s_ready || !s_tts) {
        return ESP_ERR_INVALID_STATE;
    }
    return mp3_stream_feed(s_tts, data, len);
}

esp_err_t mp3_dec_stream_frame(const int16_t **pcm, uint32_t *samples, uint32_t *rate,
                               uint8_t *channels, uint32_t *frame_bytes)
{
    if (!s_ready || !s_tts) {
        return ESP_ERR_INVALID_STATE;
    }
    return mp3_stream_frame(s_tts, pcm, samples, rate, channels, frame_bytes);
}

void mp3_dec_stream_resync(void)
{
    if (s_ready && s_tts) {
        mp3_stream_resync(s_tts);
    }
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

void mp3_dec_stream_reset(void)
{
}

esp_err_t mp3_dec_stream_feed(const uint8_t *data, size_t len)
{
    (void)data;
    (void)len;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t mp3_dec_stream_frame(const int16_t **pcm, uint32_t *samples, uint32_t *rate,
                               uint8_t *channels, uint32_t *frame_bytes)
{
    (void)pcm;
    (void)samples;
    (void)rate;
    (void)channels;
    (void)frame_bytes;
    return ESP_ERR_NOT_SUPPORTED;
}

void mp3_dec_stream_resync(void)
{
}

mp3_stream_t *mp3_stream_create(void)
{
    return NULL;
}

void mp3_stream_destroy(mp3_stream_t *st)
{
    (void)st;
}

void mp3_stream_reset(mp3_stream_t *st)
{
    (void)st;
}

esp_err_t mp3_stream_feed(mp3_stream_t *st, const uint8_t *data, size_t len)
{
    (void)st;
    (void)data;
    (void)len;
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t mp3_stream_frame(mp3_stream_t *st, const int16_t **pcm, uint32_t *samples,
                           uint32_t *rate, uint8_t *channels, uint32_t *frame_bytes)
{
    (void)st;
    (void)pcm;
    (void)samples;
    (void)rate;
    (void)channels;
    (void)frame_bytes;
    return ESP_ERR_NOT_SUPPORTED;
}

void mp3_stream_resync(mp3_stream_t *st)
{
    (void)st;
}

void mp3_stream_stats_read(mp3_stream_t *st, mp3_dec_stats_t *out)
{
    (void)st;
    if (out) {
        memset(out, 0, sizeof(*out));
    }
}

#endif /* CONFIG_AUDIO_PLAYER_ENABLE_MP3 */
