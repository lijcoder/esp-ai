#pragma once

#include <stddef.h>
#include <stdint.h>

/*
 * 本地提示音资源表（临时方案：编译进固件；T3.5 迁到 assets 分区后删除本组件）。
 *
 * 统一格式：16bit 单声道 PCM，16kHz。
 * 上层（voice_session 唤醒应答 / feedback 异常反馈）只通过 prompt_assets_get() 取资源，
 * 不直接引用具体数组——T3.5 改成"从 assets 分区加载索引"时只需换实现，上层不动。
 */

typedef enum {
    PROMPT_WAKE_OK = 0,   /* 唤醒应答"我在呢"（0.80s，峰值约 -7.8 dBFS） */
    PROMPT_NET_OFFLINE,   /* 网络未连接（会话起轮失败/中途断网） */
    PROMPT_CLOUD_ERROR,   /* 云端/服务异常（云端 error 的通用类） */
    PROMPT_TIMEOUT,       /* 响应超时（THINKING 10s 无数据） */
    PROMPT_TTS_FAIL,      /* 语音播放失败（云端 TTS 错误 3001/3002 或设备侧播不出来） */
    PROMPT_COUNT,
} prompt_id_t;

typedef struct {
    const int16_t *pcm;   /* 静态 PCM（flash rodata，生命周期 = 固件） */
    size_t samples;       /* 采样点数（单声道） */
    uint32_t sample_rate; /* 固定 16000 */
} prompt_clip_t;

/**
 * @brief 取提示音资源
 *
 * @param id 提示音 ID（PROMPT_WAKE_OK ~ PROMPT_COUNT-1）
 * @return 静态表项指针；id 非法返回 NULL。调用方不得释放/修改。
 */
const prompt_clip_t *prompt_assets_get(prompt_id_t id);

/**
 * @brief 提示音短名（日志/测试脚本断言用）
 *
 * @return "wake" / "net-offline" / "cloud-error" / "timeout" / "tts-fail"；非法 id 返回 "?"
 */
const char *prompt_assets_name(prompt_id_t id);

/* ---- 兼容旧符号（T3.1 之前的唤醒提示音直接引用；T3.5 迁移后删除） ---- */
extern const int16_t prompt_wo_zai_ne_pcm[];
extern const size_t prompt_wo_zai_ne_samples;
