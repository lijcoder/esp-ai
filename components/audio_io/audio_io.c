#include "audio_io.h"

#include <string.h>

#include "board.h"
#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "AUDIO_IO";

/* 单次读取上限：1024 帧 @16k = 64ms，足够覆盖 AFE feed chunksize */
#define RX_SCRATCH_FRAMES 1024

/* T4.4 C3：AEC 参考环 512ms @16k（PSRAM） */
#define REF_RING_SAMPLES 8192u
#define REF_RING_MASK    (REF_RING_SAMPLES - 1u)

static i2s_chan_handle_t s_rx = NULL;
static i2s_chan_handle_t s_tx = NULL;
static int32_t *s_rx_scratch = NULL;
static bool s_spk_enabled = false;

static int16_t *s_ref_ring = NULL;
static volatile uint32_t s_ref_wr; /* 单调写样本数 */
static uint32_t s_ref_written;
static uint32_t s_ref_read;
static uint32_t s_ref_underruns;
static uint32_t s_ref_overwritten;

static esp_err_t rx_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 4;
    chan_cfg.dma_frame_num = 240;

    esp_err_t err = i2s_new_channel(&chan_cfg, NULL, &s_rx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "create rx channel failed: %s", esp_err_to_name(err));
        return err;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg = {
            .sample_rate_hz = AUDIO_IO_MIC_SAMPLE_RATE,
            .clk_src = I2S_CLK_SRC_DEFAULT,
            .mclk_multiple = I2S_MCLK_MULTIPLE_256,
        },
        .slot_cfg = {
            .data_bit_width = I2S_DATA_BIT_WIDTH_32BIT,
            .slot_bit_width = I2S_SLOT_BIT_WIDTH_32BIT,
            .slot_mode = I2S_SLOT_MODE_MONO,
            .slot_mask = I2S_STD_SLOT_LEFT,
            .ws_width = 32,
            .ws_pol = false,
            .bit_shift = true,
            .left_align = true,
            .big_endian = false,
            .bit_order_lsb = false,
        },
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)BOARD_MIC_BCLK_GPIO,
            .ws = (gpio_num_t)BOARD_MIC_WS_GPIO,
            .dout = I2S_GPIO_UNUSED,
            .din = (gpio_num_t)BOARD_MIC_DIN_GPIO,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };

    err = i2s_channel_init_std_mode(s_rx, &std_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "rx init std mode failed: %s", esp_err_to_name(err));
        return err;
    }
    err = i2s_channel_enable(s_rx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "rx enable failed: %s", esp_err_to_name(err));
        return err;
    }

    /* T4.4 C2：I2S 读暂存放 PSRAM。i2s_channel_read 内部是
     * 「DMA buffer → memcpy 到用户缓冲」，用户缓冲不参与 DMA；失败回退内部。 */
    s_rx_scratch = heap_caps_malloc((size_t)RX_SCRATCH_FRAMES * sizeof(int32_t),
                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_rx_scratch) {
        s_rx_scratch = heap_caps_malloc((size_t)RX_SCRATCH_FRAMES * sizeof(int32_t),
                                        MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        ESP_LOGW(TAG, "rx scratch PSRAM 分配失败，回退内部 RAM");
    }
    if (!s_rx_scratch) {
        ESP_LOGE(TAG, "rx scratch alloc failed");
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "mic ready: %d Hz, 32bit slot, mono/left", AUDIO_IO_MIC_SAMPLE_RATE);
    return ESP_OK;
}

static esp_err_t tx_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 4;
    chan_cfg.dma_frame_num = 480;

    esp_err_t err = i2s_new_channel(&chan_cfg, &s_tx, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "create tx channel failed: %s", esp_err_to_name(err));
        return err;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_IO_SPK_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)BOARD_AMP_BCLK_GPIO,
            .ws = (gpio_num_t)BOARD_AMP_WS_GPIO,
            .dout = (gpio_num_t)BOARD_AMP_DOUT_GPIO,
            .din = I2S_GPIO_UNUSED,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };

    err = i2s_channel_init_std_mode(s_tx, &std_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "tx init std mode failed: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "speaker ready: %d Hz, 16bit stereo (disabled until playback)", AUDIO_IO_SPK_SAMPLE_RATE);
    return ESP_OK;
}

esp_err_t audio_io_init(void)
{
    esp_err_t err = rx_init();
    if (err != ESP_OK) {
        return err;
    }
    err = tx_init();
    if (err != ESP_OK) {
        return err;
    }
#if CONFIG_AUDIO_FE_AEC_ENABLE
    /* T4.4 C3：AEC 参考环（PSRAM；分配失败只降级 AEC，不影响播放/唤醒） */
    s_ref_ring = heap_caps_malloc((size_t)REF_RING_SAMPLES * sizeof(int16_t),
                                  MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_ref_ring == NULL) {
        ESP_LOGW(TAG, "AEC 参考环分配失败（PSRAM），AEC 不可用");
    } else {
        memset(s_ref_ring, 0, (size_t)REF_RING_SAMPLES * sizeof(int16_t));
        ESP_LOGI(TAG, "AEC 参考环：%u 样本（%u ms @16k，PSRAM）", (unsigned)REF_RING_SAMPLES,
                 (unsigned)(REF_RING_SAMPLES * 1000u / AUDIO_IO_MIC_SAMPLE_RATE));
    }
#endif
    return ESP_OK;
}

bool audio_io_ref_enabled(void)
{
    return s_ref_ring != NULL;
}

void audio_io_ref_push_stereo48(const int16_t *stereo, size_t frames)
{
    if (s_ref_ring == NULL || stereo == NULL) {
        return;
    }
    const size_t n = frames / 3u; /* 48k → 16k（3 点平均） */
    uint32_t wr = s_ref_wr;
    for (size_t i = 0; i < n; i++) {
        const int32_t a = stereo[(3u * i + 0u) * 2u];
        const int32_t b = stereo[(3u * i + 1u) * 2u];
        const int32_t c = stereo[(3u * i + 2u) * 2u];
        s_ref_ring[wr & REF_RING_MASK] = (int16_t)((a + b + c) / 3);
        wr++;
    }
    __atomic_store_n(&s_ref_wr, wr, __ATOMIC_RELEASE);
    s_ref_written += (uint32_t)n;
}

int audio_io_ref_read_16k(int16_t *dst, size_t frames, uint32_t delay_ms)
{
    if (dst == NULL || frames == 0) {
        return 0;
    }
    if (s_ref_ring == NULL) {
        memset(dst, 0, frames * sizeof(int16_t));
        return 0;
    }
    const uint32_t wr = __atomic_load_n(&s_ref_wr, __ATOMIC_ACQUIRE);
    const uint32_t delay = delay_ms * (AUDIO_IO_MIC_SAMPLE_RATE / 1000u);
    if (wr < delay + (uint32_t)frames) {
        memset(dst, 0, frames * sizeof(int16_t));
        s_ref_underruns++;
        return 0;
    }
    const uint32_t start = wr - delay - (uint32_t)frames;
    if (wr - start > REF_RING_SAMPLES) {
        memset(dst, 0, frames * sizeof(int16_t));
        s_ref_overwritten++;
        return 0;
    }
    uint32_t idx = start & REF_RING_MASK;
    for (size_t i = 0; i < frames; i++) {
        dst[i] = s_ref_ring[idx];
        idx = (idx + 1u) & REF_RING_MASK;
    }
    s_ref_read += (uint32_t)frames;
    return (int)frames;
}

void audio_io_ref_stats_read(audio_io_ref_stats_t *out)
{
    if (out == NULL) {
        return;
    }
    out->written = s_ref_written;
    out->read = s_ref_read;
    out->underruns = s_ref_underruns;
    out->overwritten = s_ref_overwritten;
}

int audio_io_read_mic(int16_t *dst, size_t frames, uint32_t timeout_ms)
{
    if (!s_rx || !dst || frames == 0 || frames > RX_SCRATCH_FRAMES) {
        return -1;
    }

    size_t bytes_read = 0;
    /* 注意：i2s_channel_read 的最后一个参数单位是毫秒（不是 tick），
     * 不能传 pdMS_TO_TICKS()，否则在 100Hz tick 下会被除以 10，
     * 读 512 帧 @16k（需 32ms）必然超时（2026-10-30 唤醒失效的根因）。 */
    esp_err_t err = i2s_channel_read(s_rx, s_rx_scratch, frames * sizeof(int32_t),
                                     &bytes_read, timeout_ms);
    /* 超时但已拿到部分数据时按实际帧数返回，由调用方补零，避免整帧丢弃 */
    if (err != ESP_OK && bytes_read == 0) {
        return -1;
    }

    size_t got = bytes_read / sizeof(int32_t);
    for (size_t i = 0; i < got; i++) {
        /* INMP441 24bit 有效数据在 32bit 槽位高 24 位，取高 16 位 */
        dst[i] = (int16_t)(s_rx_scratch[i] >> 16);
    }
    return (int)got;
}

int audio_io_write_spk(const int16_t *data, size_t frames, uint32_t timeout_ms)
{
    if (!s_tx || !data || frames == 0 || !s_spk_enabled) {
        return -1;
    }

    size_t bytes_written = 0;
    esp_err_t err = i2s_channel_write(s_tx, data, frames * 2 * sizeof(int16_t),
                                      &bytes_written, timeout_ms);
    if (err != ESP_OK && bytes_written == 0) {
        return -1;
    }
    return (int)(bytes_written / (2 * sizeof(int16_t)));
}

esp_err_t audio_io_spk_enable(bool enable)
{
    if (!s_tx) {
        return ESP_ERR_INVALID_STATE;
    }
    if (enable == s_spk_enabled) {
        return ESP_OK;
    }

    esp_err_t err = enable ? i2s_channel_enable(s_tx) : i2s_channel_disable(s_tx);
    if (err == ESP_OK) {
        s_spk_enabled = enable;
    } else {
        ESP_LOGE(TAG, "spk %s failed: %s", enable ? "enable" : "disable", esp_err_to_name(err));
    }
    return err;
}

bool audio_io_spk_is_enabled(void)
{
    return s_spk_enabled;
}
