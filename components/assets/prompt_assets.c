/*
 * 提示音资源表 + 查询 API（临时方案：编译进固件；T3.5 迁到 assets 分区后删除本组件）。
 *
 * 为什么用静态 const 表：资源是只读 flash rodata，没有运行时初始化/加锁成本，
 * 也满足"音频路径不 malloc/不阻塞"的约束。采样点数来自 prompt_pcm_sizes.h 的宏
 * （由 tools/prompt_gen.py 生成），各 prompt_*.c 用 _Static_assert 与宏对齐，
 * 避免"表里长度与数组实际长度不符"造成静默越界。
 */
#include <stddef.h>
#include <stdint.h>

#include "prompt_assets.h"
#include "prompt_pcm_sizes.h"

static const prompt_clip_t s_prompts[PROMPT_COUNT] = {
    [PROMPT_WAKE_OK] = { prompt_wo_zai_ne_pcm, PROMPT_WAKE_OK_SAMPLES, PROMPT_SAMPLE_RATE_HZ },
    [PROMPT_NET_OFFLINE] = { prompt_net_offline_pcm, PROMPT_NET_OFFLINE_SAMPLES, PROMPT_SAMPLE_RATE_HZ },
    [PROMPT_CLOUD_ERROR] = { prompt_cloud_error_pcm, PROMPT_CLOUD_ERROR_SAMPLES, PROMPT_SAMPLE_RATE_HZ },
    [PROMPT_TIMEOUT] = { prompt_timeout_pcm, PROMPT_TIMEOUT_SAMPLES, PROMPT_SAMPLE_RATE_HZ },
    [PROMPT_TTS_FAIL] = { prompt_tts_fail_pcm, PROMPT_TTS_FAIL_SAMPLES, PROMPT_SAMPLE_RATE_HZ },
};

static const char *const s_names[PROMPT_COUNT] = {
    [PROMPT_WAKE_OK] = "wake",
    [PROMPT_NET_OFFLINE] = "net-offline",
    [PROMPT_CLOUD_ERROR] = "cloud-error",
    [PROMPT_TIMEOUT] = "timeout",
    [PROMPT_TTS_FAIL] = "tts-fail",
};

const prompt_clip_t *prompt_assets_get(prompt_id_t id)
{
    if ((unsigned)id >= (unsigned)PROMPT_COUNT) {
        return NULL;
    }
    return &s_prompts[id];
}

const char *prompt_assets_name(prompt_id_t id)
{
    if ((unsigned)id >= (unsigned)PROMPT_COUNT) {
        return "?";
    }
    return s_names[id];
}
