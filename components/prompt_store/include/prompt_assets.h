/*
 * prompt_assets —— 本地提示音资源的唯一访问接口（T3.5 起由 `assets` 分区提供）。
 *
 * T3.6 时资源是编进固件的 C 数组；T3.5 迁到 assets 分区（二进制 pack，见 prompt_pack.h），
 * 上层（voice_session 的唤醒应答、feedback 的异常反馈）只通过本接口取资源：
 *   - 接口/ID 与 T3.6 完全一致（迁移对业务代码零改动）；
 *   - `prompt_assets_get()` 返回的指针指向 **flash 映射区**（或降级时的 PSRAM 缓冲），
 *     生命周期 = 进程，调用方不得释放/修改；
 *   - pack 缺失/损坏时 `prompt_store_init()` 失败、`prompt_assets_get()` 返回 NULL，
 *     业务按"提示音不可用"降级（不能崩）。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    PROMPT_WAKE_OK = 0,   /* 唤醒应答"我在呢" */
    PROMPT_NET_OFFLINE,   /* 网络未连接 */
    PROMPT_CLOUD_ERROR,   /* 云端/服务异常 */
    PROMPT_TIMEOUT,       /* 响应超时 */
    PROMPT_TTS_FAIL,      /* 语音播放失败 */
    PROMPT_COUNT,
} prompt_id_t;

typedef struct {
    const int16_t *pcm;   /* 16bit 单声道 PCM（flash 映射区或 PSRAM 缓冲） */
    size_t samples;       /* 采样点数 */
    uint32_t sample_rate; /* 固定 16000 */
} prompt_clip_t;

/* 资源来自哪里（观测用） */
typedef enum {
    PROMPT_STORE_MODE_NONE = 0, /* 未初始化/失败 */
    PROMPT_STORE_MODE_MMAP,     /* flash 映射零拷贝（正常路径） */
    PROMPT_STORE_MODE_PSRAM,    /* 映射失败 → 分块读入 PSRAM（降级路径） */
} prompt_store_mode_t;

typedef struct {
    bool ready;
    uint8_t mode;        /* prompt_store_mode_t */
    uint16_t version;    /* pack 版本 */
    uint16_t count;      /* 条目数（应为 PROMPT_COUNT） */
    uint32_t total_len;  /* pack 字节数 */
    uint32_t load_us;    /* init 耗时（含映射/读取） */
    const void *base;    /* 映射/缓冲基地址（调试用） */
    char error[56];      /* ready=false 时的失败原因 */
} prompt_store_info_t;

/**
 * @brief 初始化提示音资源：查找 assets 分区 → 校验 pack → mmap（失败回退 PSRAM 加载）
 *
 * @note 只由 `app_main` 调一次；失败**不致命**（返回错误码，提示音不可用，业务降级）。
 *       幂等：重复调用返回上次结果。
 */
esp_err_t prompt_store_init(void);

bool prompt_store_ready(void);

/* 观测快照（diag `prompt info` 用） */
esp_err_t prompt_store_info(prompt_store_info_t *out);

/**
 * @brief 取提示音资源
 *
 * @param id 提示音 ID
 * @return 固定表项指针；未就绪/非法 id 返回 NULL。调用方不得释放/修改。
 */
const prompt_clip_t *prompt_assets_get(prompt_id_t id);

/* 提示音短名（日志/测试脚本断言用）；非法 id 返回 "?" */
const char *prompt_assets_name(prompt_id_t id);

#ifdef __cplusplus
}
#endif
