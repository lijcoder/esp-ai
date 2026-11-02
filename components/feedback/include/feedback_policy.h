/*
 * feedback_policy —— 异常反馈的**纯逻辑**（无 IDF 依赖）
 *
 * 单独拆出来的原因：主机侧（test/host）可以直接用 cc 编译本头文件做单测，
 * 不需要 ESP-IDF；feedback.c 只负责"怎么播/怎么发布事件"。
 *
 * 契约：kind 数值同时写入 APP_EVT_FEEDBACK 的 payload（event_bus 是 L1，
 * 不能 include 本头文件，见 ARCHITECTURE §4.2）。**只允许在末尾追加**。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    FB_KIND_NONE = 0,
    FB_KIND_NET_OFFLINE = 1, /* 网络未连接（起轮失败 / 会话中 NET_DOWN） */
    FB_KIND_CLOUD_ERROR = 2, /* 云端/服务异常（error code 1/1001/2001/2002/9001/未知） */
    FB_KIND_TIMEOUT = 3,     /* THINKING 等响应超时 */
    FB_KIND_TTS_FAIL = 4,    /* 语音播放失败（云端 3001/3002 或设备侧播不出来） */
    FB_KIND_COUNT,
} fb_kind_t;

/* PROTOCOL.md 错误码表：3001=TTS 失败、3002=TTS 超时 → "语音播放失败"；
 * 其余（1 通用 / 1001 鉴权 / 2001/2002 ASR / 9001 服务端内部）统一归"服务异常"。 */
static inline fb_kind_t fb_kind_from_cloud_code(int32_t code)
{
    return (code == 3001 || code == 3002) ? FB_KIND_TTS_FAIL : FB_KIND_CLOUD_ERROR;
}

static inline const char *fb_kind_name(fb_kind_t kind)
{
    switch (kind) {
    case FB_KIND_NET_OFFLINE: return "net-offline";
    case FB_KIND_CLOUD_ERROR: return "cloud-error";
    case FB_KIND_TIMEOUT:     return "timeout";
    case FB_KIND_TTS_FAIL:    return "tts-fail";
    default:                  return "?";
    }
}

static inline bool fb_kind_valid(fb_kind_t kind)
{
    return kind > FB_KIND_NONE && kind < FB_KIND_COUNT;
}
