/*
 * 应用入口：只做模块初始化编排，不放业务逻辑（CONVENTIONS §2）。
 * 业务链路（T3.1 起）：
 *   唤醒"嗨，乐鑫"（audio_fe 投 APP_EVT_WAKE_DETECTED）→ voice_session 状态机：
 *   播应答提示音"我在呢" → 恢复收音 → 起一轮端云会话（voice_cloud 上行 Opus / 下行 TTS）
 *   → IDLE。未联网/未配置 WS_URL 时本轮降级结束，本地唤醒/提示音不受影响。
 */
#include "audio_fe.h"
#include "audio_io.h"
#include "audio_player.h"
#include "board.h"
#include "config_store.h"
#include "dev_ctrl.h"
#include "diag.h"
#include "esp_log.h"
#include "event_bus.h"
#include "feedback.h"
#include "net_mgr.h"
#include "transport_ws.h"
#include "voice_cloud.h"
#include "voice_session.h"

static const char *TAG = "APP";

/* 播放主槽只做"本地整段 MP3 探针"的日志；提示音/TTS 的生命周期归 voice_session / voice_cloud
 * 的附加订阅槽（主槽与附加槽都会收到回调，见 audio_player.h）。 */
static void on_player_done(audio_player_src_t src, bool ok, void *arg)
{
    (void)arg;
    if (src == AUDIO_PLAYER_SRC_MP3_FILE) {
        ESP_LOGI(TAG, "mp3 file done（ok=%d）", (int)ok);
    }
}

void app_main(void)
{
    /* 先起观测能力：后续所有模块的初始化日志与自测命令都靠 console */
    ESP_ERROR_CHECK(diag_init());

    /* 配置持久化要早于所有使用方（net_mgr/transport_ws/播放器音量都依赖它） */
    ESP_ERROR_CHECK(cfg_init());

    /* 事件总线要早于任何会投递/订阅事件的模块（net_mgr/transport_ws/audio_fe 都依赖它） */
    ESP_ERROR_CHECK(app_evt_init());

    ESP_LOGI(TAG, "booting esp-ai (wake + prompt + cloud session)");

    ESP_ERROR_CHECK(board_init());
    ESP_ERROR_CHECK(audio_io_init());
    ESP_ERROR_CHECK(audio_player_init());
    audio_player_register_done_cb(on_player_done, NULL);

    /* 启动时把 NVS 里的音量灌进播放器：这样 cfg set volume 重启后能听到效果 */
    int volume = 0;
    if (cfg_get_volume(&volume) == ESP_OK) {
        ESP_ERROR_CHECK(audio_player_set_volume((uint8_t)volume));
        ESP_LOGI(TAG, "volume restored: %d", volume);
    } else {
        ESP_LOGW(TAG, "read volume failed, keep player default");
    }

    /* 异常反馈路由（T3.6）：放在播放器之后（要订阅播放完成回调）、事件总线之后
     * （fb_notify 会发布 APP_EVT_FEEDBACK 语义事件）。不建任务/定时器 */
    ESP_ERROR_CHECK(fb_init());

    /* 唤醒/VAD 自 T1.2b 起走 event_bus，业务回调传 NULL（产品路径不注册） */
    ESP_ERROR_CHECK(audio_fe_init(NULL, NULL));
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

    /* 会话状态机（T3.1）放在 vc 之后：订阅事件总线 + vc 回调 + 播放完成回调，不建任务 */
    ESP_ERROR_CHECK(vs_init());

    /* 物理交互（T3.4）放在 vs 之后：LED 订阅 VOICE_STATE_CHANGED、按键投 APP_EVT_BUTTON
     * （由 vs 消费为打断/起轮）；静音改音量走 audio_player。不建任务（仅两个 esp_timer） */
    ESP_ERROR_CHECK(dev_init());

    ESP_LOGI(TAG, "ready, say \"Hi Lexin\"");
}
