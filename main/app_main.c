/*
 * 应用入口：只做模块初始化编排与事件接线，不放业务逻辑。
 * 当前为最小可用版本（P0 MVP）：
 *   唤醒"嗨，乐鑫" -> 播放提示音"我在呢" -> 恢复收音（半双工）
 */
#include "audio_fe.h"
#include "audio_io.h"
#include "audio_player.h"
#include "board.h"
#include "esp_log.h"
#include "prompt_assets.h"

static const char *TAG = "APP";

static void on_play_done(void *arg)
{
    (void)arg;
    /* R2：播放结束（含尾部延时）后恢复收音 */
    audio_fe_set_feed_enabled(true);
    ESP_LOGI(TAG, "prompt done, feed resumed");
}

static void on_afe_event(audio_fe_event_t evt, void *arg)
{
    (void)arg;

    switch (evt) {
    case AUDIO_FE_EVT_WAKE:
        ESP_LOGI(TAG, "wake detected");
        if (audio_player_is_busy()) {
            ESP_LOGW(TAG, "player busy, ignore wake");
            break;
        }
        /* R1：进入播放前暂停收音，避免自激 */
        audio_fe_set_feed_enabled(false);
        esp_err_t err = audio_player_play_pcm(prompt_wo_zai_ne_pcm,
                                              prompt_wo_zai_ne_samples,
                                              AUDIO_IO_MIC_SAMPLE_RATE);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "play prompt failed: %s", esp_err_to_name(err));
            audio_fe_set_feed_enabled(true);
        }
        break;

    case AUDIO_FE_EVT_VAD_SPEECH_START:
        ESP_LOGD(TAG, "vad: speech start");
        break;

    case AUDIO_FE_EVT_VAD_SPEECH_END:
        ESP_LOGD(TAG, "vad: speech end");
        break;

    default:
        break;
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "booting esp-ai (mvp: wake + prompt)");

    ESP_ERROR_CHECK(board_init());
    ESP_ERROR_CHECK(audio_io_init());
    ESP_ERROR_CHECK(audio_player_init());
    audio_player_register_done_cb(on_play_done, NULL);

    ESP_ERROR_CHECK(audio_fe_init(on_afe_event, NULL));
    const char *wn = audio_fe_wakenet_name();
    ESP_LOGI(TAG, "wake word model: %s", wn ? wn : "unknown");

    ESP_ERROR_CHECK(audio_fe_start());
    ESP_LOGI(TAG, "ready, say \"Hi Lexin\"");
}
