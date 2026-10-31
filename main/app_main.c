/*
 * 应用入口：只做模块初始化编排与事件接线，不放业务逻辑。
 * 当前链路（P2/T2.5）：
 *   唤醒"嗨，乐鑫" -> 播放提示音"我在呢" -> 恢复收音 -> 起一轮端云语音会话
 *   （voice_cloud 上行 Opus；未联网/未配置 WS_URL 时自动跳过，本地功能不受影响）
 */
#include "audio_fe.h"
#include "audio_io.h"
#include "audio_player.h"
#include "board.h"
#include "config_store.h"
#include "diag.h"
#include "esp_log.h"
#include "event_bus.h"
#include "net_mgr.h"
#include "prompt_assets.h"
#include "transport_ws.h"
#include "voice_cloud.h"

static const char *TAG = "APP";

static void on_play_done(void *arg)
{
    (void)arg;
    /* R2：播放结束（含尾部延时）后恢复收音 */
    audio_fe_set_feed_enabled(true);
    ESP_LOGI(TAG, "prompt done, feed resumed");

    /* 提示音播完、收音已恢复，才是"用户开始说话"的起点：发起一轮端云会话。
     * 云端未 READY 时 vc_start_auto() 返回 INVALID_STATE 并只打 WARN —— 启动顺序
     * 不能假设 WiFi/握手已经完成（唤醒是本地能力，早于联网）。 */
    esp_err_t err = vc_start_auto();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "cloud round skipped: %s", esp_err_to_name(err));
    }
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
    /* 先起观测能力：后续所有模块的初始化日志与自测命令都靠 console */
    ESP_ERROR_CHECK(diag_init());

    /* 配置持久化要早于所有使用方（net_mgr/transport_ws/播放器音量都依赖它） */
    ESP_ERROR_CHECK(cfg_init());

    /* 事件总线要早于任何会投递/订阅事件的模块（P1 起 net_mgr/transport_ws 都依赖它） */
    ESP_ERROR_CHECK(app_evt_init());

    ESP_LOGI(TAG, "booting esp-ai (mvp: wake + prompt)");

    ESP_ERROR_CHECK(board_init());
    ESP_ERROR_CHECK(audio_io_init());
    ESP_ERROR_CHECK(audio_player_init());
    audio_player_register_done_cb(on_play_done, NULL);

    /* 启动时把 NVS 里的音量灌进播放器：这样 cfg set volume 重启后能听到效果 */
    int volume = 0;
    if (cfg_get_volume(&volume) == ESP_OK) {
        ESP_ERROR_CHECK(audio_player_set_volume((uint8_t)volume));
        ESP_LOGI(TAG, "volume restored: %d", volume);
    } else {
        ESP_LOGW(TAG, "read volume failed, keep player default");
    }

    ESP_ERROR_CHECK(audio_fe_init(on_afe_event, NULL));
    const char *wn = audio_fe_wakenet_name();
    ESP_LOGI(TAG, "wake word model: %s", wn ? wn : "unknown");

    ESP_ERROR_CHECK(audio_fe_start());

    /* 联网放在音频链路之后：唤醒/播放是本地核心能力，不能被 WiFi 初始化拖慢；
     * 未配置 SSID 时 net_init 内部降级为离线（只 WARN），不影响本地功能 */
    ESP_ERROR_CHECK(net_init());

    /* WebSocket 传输放在联网之后：它订阅 NET_UP/NET_DOWN（T1.4），
     * 未配置 WS_URL 时 ws_init 只 WARN 并降级离线，本地功能不受影响 */
    ESP_ERROR_CHECK(ws_init());
    ws_start();

    /* 语音云协议（T2.5）放在 ws 之后：vc_init 要注册 WS 主接收回调；
     * 未 READY 时按轮次跳过，不影响本地唤醒/提示音 */
    ESP_ERROR_CHECK(vc_init());

    ESP_LOGI(TAG, "ready, say \"Hi Lexin\"");
}
