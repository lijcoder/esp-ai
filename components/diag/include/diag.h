/*
 * diag —— 调试与观测能力（L3 能力层）的唯一对外头文件。
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 初始化调试观测能力：打印启动信息横幅，并在 UART0 起 esp_console REPL。
 *
 * 注册的命令（本轮）：
 *   - `sys info` / `sys heap` / `sys tasks` / `sys reboot`
 *   - `log [<tag>|*] [none|error|warn|info|debug|verbose]`
 *   - `help`
 *
 * @note 只应在 app_main 的最前部调用一次；重复调用返回 ESP_ERR_INVALID_STATE。
 * @note 内部会安装 UART0 驱动并接管 stdin/stdout，故应早于其它模块的日志输出。
 *
 * @return
 *   - ESP_OK 成功
 *   - ESP_ERR_INVALID_STATE 已初始化
 *   - 其它：console 初始化/命令注册失败的错误码
 */
esp_err_t diag_init(void);

#ifdef __cplusplus
}
#endif
