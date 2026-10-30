/*
 * net_mgr —— WiFi STA 连接、断线重连、SNTP 对时（L1 基础服务）的唯一对外头文件。
 *
 * 凭证来源当前为 Kconfig（CONFIG_AI_WIFI_SSID/PASSWORD），见 ARCHITECTURE §10.1；
 * 网络状态变化通过 event_bus 投递 APP_EVT_NET_UP / APP_EVT_NET_DOWN（ARCHITECTURE §8.5）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* IPv4 点分字符串最大 15 字符 + '\0'（与 event_bus 的 APP_EVT_IP_MAX 一致） */
#define NET_IP_STR_MAX 16
/* 802.11 SSID 最大 32 字节 + '\0' */
#define NET_SSID_STR_MAX 33

typedef enum {
    NET_STATE_IDLE = 0,  /* 未初始化，或未配置 SSID（联网功能关闭，本地功能仍可用） */
    NET_STATE_CONNECTING,/* 已发起连接/正在退避重连，等待地址 */
    NET_STATE_UP,        /* 已获得 IP，可收发 */
} net_state_t;

typedef struct {
    net_state_t state;
    bool time_synced;       /* SNTP 是否已对时（TLS 证书校验前需要） */
    char ip[NET_IP_STR_MAX]; /* 本机 IPv4；未联网为空串 */
    int8_t rssi;            /* 关联 AP 的 RSSI（dBm）；未关联为 0 */
    uint32_t retry_count;   /* 自上次联网成功以来的累计重连次数 */
    uint32_t next_retry_ms; /* 当前退避值；0 表示没有在等待重连 */
    char ssid[NET_SSID_STR_MAX]; /* 配置的 SSID（不保存/不打印密码） */
} net_info_t;

/**
 * @brief 初始化 WiFi STA 并开始连接（非阻塞，连接结果经事件上报）
 *
 * 未配置 `CONFIG_AI_WIFI_SSID` 时只打 WARN 并进入 NET_STATE_IDLE：
 * 本地唤醒/播放不受影响，T1.4 的 transport_ws 会因 `net_is_up()` 为 false 而拒绝启动。
 *
 * @note 只应在 app_main 中调用一次；重复调用返回 ESP_ERR_INVALID_STATE。
 * @note 内部会创建系统默认 esp_event 循环（WiFi/IP 事件挂在那里），与 event_bus 的独立循环互不影响。
 */
esp_err_t net_init(void);

/* 是否已获得 IP（可收发数据） */
bool net_is_up(void);

/**
 * @brief 等待网络就绪（获得 IP）
 *
 * @param timeout_ms 0 表示只查询不等待
 *
 * @note 只能在任务上下文调用；音频实时任务禁止调用（会阻塞）。
 * @return ESP_OK / ESP_ERR_TIMEOUT / ESP_ERR_INVALID_STATE（未初始化或未配置 SSID）
 */
esp_err_t net_wait_ready(uint32_t timeout_ms);

/* SNTP 是否已对时（判断系统时间是否可信，TLS 校验前使用） */
bool net_is_time_synced(void);

/* 读取网络状态快照（供 diag 的 wifi status 使用；字段可能被事件任务并发更新，仅用于观测） */
esp_err_t net_get_info(net_info_t *out);

/**
 * @brief 主动断开并重连（调试/恢复用）
 *
 * 会重置退避索引（下一次重试回到最短间隔）并立即发起重连；
 * 若当前已联网，会先投递 APP_EVT_NET_DOWN 再重连。
 */
esp_err_t net_reconnect(void);

/* 状态名（日志/调试口用） */
const char *net_state_name(net_state_t state);

#ifdef __cplusplus
}
#endif
