#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2s_std.h"
#include "driver/gpio.h"
#include "esp_log.h"

#include "esp_wn_iface.h"
#include "esp_wn_models.h"
#include "esp_afe_sr_models.h"
#include "model_path.h"

static const char *TAG = "SMART_SPEAKER";

#define I2S_RX_BCLK_PIN GPIO_NUM_5
#define I2S_RX_WS_PIN   GPIO_NUM_6
#define I2S_RX_DIN_PIN  GPIO_NUM_4

#define I2S_TX_BCLK_PIN GPIO_NUM_41
#define I2S_TX_WS_PIN   GPIO_NUM_42
#define I2S_TX_DOUT_PIN GPIO_NUM_40

#define SAMPLE_RATE     16000

static i2s_chan_handle_t rx_handle = NULL;
static i2s_chan_handle_t tx_handle = NULL;
static const esp_afe_sr_iface_t *afe_handle = NULL;
static volatile int task_flag = 0;
static volatile bool is_playing = false;

/* ========================================================================
 * 预置的“我在呢”语音数据 (16kHz, 16bit, Mono)
 * 注意：为了演示，这里用一段数组代替。你可以用工具将自己的录音转成这个格式。
 * ======================================================================== */
extern const uint8_t voice_wo_zai_ne_pcm[];
extern const uint32_t voice_wo_zai_ne_pcm_len;

/* ===================== 硬件初始化 ===================== */
static void rx_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &rx_handle));

    i2s_std_config_t cfg = {
        .clk_cfg = {
            .sample_rate_hz = SAMPLE_RATE,
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
            .bclk = I2S_RX_BCLK_PIN,
            .ws   = I2S_RX_WS_PIN,
            .dout = I2S_GPIO_UNUSED,
            .din  = I2S_RX_DIN_PIN,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx_handle, &cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(rx_handle));
}

static void tx_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &tx_handle, NULL));

    i2s_std_config_t cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = I2S_TX_BCLK_PIN,
            .ws   = I2S_TX_WS_PIN,
            .dout = I2S_TX_DOUT_PIN,
            .din  = I2S_GPIO_UNUSED,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx_handle, &cfg));
}

/* ===================== 任务定义 ===================== */
void feed_Task(void *arg)
{
    esp_afe_sr_data_t *afe_data = arg;
    int audio_chunksize = afe_handle->get_feed_chunksize(afe_data);
    int nch = afe_handle->get_feed_channel_num(afe_data);
    
    int32_t *i2s_raw_buff = malloc(audio_chunksize * sizeof(int32_t) * nch);
    int16_t *afe_buff = malloc(audio_chunksize * sizeof(int16_t) * nch);
    assert(i2s_raw_buff && afe_buff);

    while (task_flag) {
        size_t bytes_read = 0;
        esp_err_t ret = i2s_channel_read(rx_handle, i2s_raw_buff, 
                                         audio_chunksize * sizeof(int32_t) * nch, 
                                         &bytes_read, portMAX_DELAY);
        
        if (ret == ESP_OK && bytes_read > 0) {
            int frames = bytes_read / sizeof(int32_t);
            for (int i = 0; i < frames; i++) {
                afe_buff[i] = (int16_t)(i2s_raw_buff[i] >> 16);
            }
            afe_handle->feed(afe_data, afe_buff);
        }
    }
    
    free(i2s_raw_buff);
    free(afe_buff);
    vTaskDelete(NULL);
}

// 播放“我在呢”的音频
static void play_voice_task(void *arg)
{
    is_playing = true;
    ESP_LOGI(TAG, "Playing 'Wo Zai Ne' voice...");

    int16_t *mono_data = (int16_t *)voice_wo_zai_ne_pcm;
    int num_samples = voice_wo_zai_ne_pcm_len / 2; 

    int chunk_samples = 256;
    int16_t *stereo_buf = malloc(chunk_samples * 2 * sizeof(int16_t));
    
    i2s_channel_enable(tx_handle);

    for (int i = 0; i < num_samples; i += chunk_samples) {
        int n = num_samples - i;
        if (n > chunk_samples) n = chunk_samples;

        for (int j = 0; j < n; j++) {
            int16_t val = mono_data[i + j];
            
            // 【核心修改 1：尾部 200 个采样点强制淡出】
            // 防止最后一点声音被生硬切断导致爆音
            int remain = num_samples - (i + j);
            if (remain < 200) {
                val = (int16_t)((int32_t)val * remain / 200);
            }

            stereo_buf[j * 2]     = val;
            stereo_buf[j * 2 + 1] = val;
        }
        
        size_t bytes_written = 0;
        i2s_channel_write(tx_handle, stereo_buf, n * 2 * sizeof(int16_t), &bytes_written, 1000);
    }

    // 【核心修改 2：尾部强制补 100ms 静音】
    // 用零数据把 DMA 缓冲区里的残余声音全部冲刷出去
    memset(stereo_buf, 0, chunk_samples * 2 * sizeof(int16_t));
    int flush_loops = (SAMPLE_RATE / 10) / chunk_samples; // 100ms 长度
    for (int i = 0; i < flush_loops; i++) {
        size_t bytes_written = 0;
        i2s_channel_write(tx_handle, stereo_buf, chunk_samples * 2 * sizeof(int16_t), &bytes_written, 1000);
    }

    // 【核心修改 3：加长等待时间】
    // 必须等 I2S 硬件把最后一点静音也发完，再去切断时钟
    vTaskDelay(pdMS_TO_TICKS(150));
    i2s_channel_disable(tx_handle);
    
    free(stereo_buf);
    is_playing = false;
    ESP_LOGI(TAG, "Voice playback finished.");
    vTaskDelete(NULL);
}

void detect_Task(void *arg)
{
    esp_afe_sr_data_t *afe_data = arg;
    printf("------------detect start------------\n");

    while (task_flag) {
        afe_fetch_result_t* res = afe_handle->fetch(afe_data); 
        if (!res || res->ret_value == ESP_FAIL) {
            printf("fetch error!\n");
            break;
        }

        if (res->wakeup_state == WAKENET_DETECTED) {
            ESP_LOGI(TAG, ">>>>>> Wake word detected! <<<<<<");
            
            if (!is_playing) {
                xTaskCreate(play_voice_task, "play_voice", 4096, NULL, 5, NULL);
            }
        }
    }
    vTaskDelete(NULL);
}

void app_main()
{
    ESP_LOGI(TAG, "==> Initializing Smart Speaker");
    
    rx_init();
    tx_init();

    srmodel_list_t *models = esp_srmodel_init("model");
    if (models) {
        for (int i = 0; i < models->num; i++) {
            if (strstr(models->model_name[i], ESP_WN_PREFIX) != NULL) {
                ESP_LOGI(TAG, "wakenet model in flash: %s", models->model_name[i]);
            }
        }
    }

    afe_config_t *afe_config = afe_config_init("M", models, AFE_TYPE_SR, AFE_MODE_LOW_COST);
    afe_handle = esp_afe_handle_from_config(afe_config);
    esp_afe_sr_data_t *afe_data = afe_handle->create_from_config(afe_config);
    afe_config_free(afe_config);
    
    task_flag = 1;
    xTaskCreatePinnedToCore(&feed_Task, "feed", 8 * 1024, (void*)afe_data, 5, NULL, 0);
    xTaskCreatePinnedToCore(&detect_Task, "detect", 4 * 1024, (void*)afe_data, 5, NULL, 1);
}