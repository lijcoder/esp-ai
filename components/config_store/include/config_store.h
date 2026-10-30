/*
 * config_store —— 配置持久化（L1 基础服务）的唯一对外头文件。
 *
 * 分工：
 *   - 编译期常量（WiFi / WS / device_id / token）走 Kconfig（main/Kconfig.projbuild），
 *     使用方直接读 CONFIG_AI_*，不经过本组件包一层；
 *   - 运行态参数（音量等）走本组件：统一负责 nvs_flash_init、命名空间、键名与缺省值，
 *     避免各模块各自打开 NVS 造成重复初始化与键名漂移。
 *
 * 约束：
 *   - 只能在 app_main 里 cfg_init() 一次，早于任何使用方；
 *   - 内部 Flash 写会阻塞，禁止在音频实时任务里调用；
 *   - 键名/命名空间属于持久化契约，改名等于让老设备的配置"消失"，改动需评估迁移。
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 初始化配置存储：独占 nvs_flash_init() 并打开命名空间 "cfg"。
 *
 * 分区写满或版本不兼容时（ESP_ERR_NVS_NO_FREE_PAGES / NEW_VERSION_FOUND）会擦除重建，
 * 这是唯一允许丢配置的路径（打 WARN）。
 *
 * @return ESP_OK / ESP_ERR_INVALID_STATE（重复调用）/ NVS 相关错误码
 */
esp_err_t cfg_init(void);

/**
 * @brief 读 int32。
 *
 * @return ESP_OK；ESP_ERR_NVS_NOT_FOUND 表示该键未设置（由调用方决定缺省）；
 *         ESP_ERR_INVALID_ARG / ESP_ERR_INVALID_STATE / 其它 NVS 错误码。
 */
esp_err_t cfg_get_int(const char *key, int32_t *out);

/**
 * @brief 读 int32，未设置时回退到 fallback 且返回 ESP_OK（便于"有缺省值"的参数）。
 */
esp_err_t cfg_get_int_or(const char *key, int32_t fallback, int32_t *out);

/** @brief 写 int32（立即 commit）。 */
esp_err_t cfg_set_int(const char *key, int32_t value);

/**
 * @brief 读字符串（含结尾 '\0'）。
 *
 * @param buf 输出缓冲；@param len 缓冲大小（字节）
 * @return ESP_OK / ESP_ERR_NVS_NOT_FOUND / ESP_ERR_NVS_INVALID_LENGTH（缓冲不足）
 */
esp_err_t cfg_get_str(const char *key, char *buf, size_t len);

/** @brief 写字符串（立即 commit）。值不会被打印到日志（可能是 token 等敏感内容）。 */
esp_err_t cfg_set_str(const char *key, const char *value);

/** @brief 删除一个键（用于恢复缺省）；未设置时返回 ESP_ERR_NVS_NOT_FOUND。 */
esp_err_t cfg_erase_key(const char *key);

/**
 * @brief 读音量（0~100）。未设置时返回 CONFIG_CFG_VOLUME_DEFAULT；
 *        NVS 中越界值在此处 clamp 并打 WARN（防御历史脏数据）。
 */
esp_err_t cfg_get_volume(int *out);

/** @brief 写音量：先 clamp 到 0~100（越界打 WARN）再落盘。 */
esp_err_t cfg_set_volume(int volume);

/**
 * @brief 读 NVS 分区条目统计（供 diag 的 `cfg info` 观察落盘情况）。
 */
esp_err_t cfg_get_usage(uint32_t *used_entries, uint32_t *free_entries);

#ifdef __cplusplus
}
#endif
