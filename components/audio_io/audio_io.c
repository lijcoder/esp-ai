#include "audio_io.h"

#include <string.h>

#include "board.h"
#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "AUDIO_IO";

/* 单次读取上限：1024 帧 @16k = 64ms，足够覆盖 AFE feed chunksize */
#define RX_SCRATCH_FRAMES 1024

static i2s_chan_handle_t s_rx = NULL;
static i2s_chan_handle_t s_tx = NULL;
static int32_t *s_rx_scratch = NULL;
static bool s_spk_enabled = false;

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

    s_rx_scratch = heap_caps_malloc((size_t)RX_SCRATCH_FRAMES * sizeof(int32_t),
                                    MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
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
    return tx_init();
}

int audio_io_read_mic(int16_t *dst, size_t frames, uint32_t timeout_ms)
{
    if (!s_rx || !dst || frames == 0 || frames > RX_SCRATCH_FRAMES) {
        return -1;
    }

    size_t bytes_read = 0;
    esp_err_t err = i2s_channel_read(s_rx, s_rx_scratch, frames * sizeof(int32_t),
                                     &bytes_read, pdMS_TO_TICKS(timeout_ms));
    if (err != ESP_OK) {
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
                                      &bytes_written, pdMS_TO_TICKS(timeout_ms));
    if (err != ESP_OK) {
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
