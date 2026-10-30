/*
 * transport_ws —— 云端 WebSocket 传输（L1 基础服务）的唯一对外头文件。
 *
 * 职责边界（ARCHITECTURE §4.1、§6.1–6.3、§8.5）：
 *   - 只做传输层：连接/重连退避、hello 握手与 hello_ack、20s/10s 心跳、8 字节二进制帧收发；
 *   - **不解析业务 JSON**：asr/llm/tts/music 事件原样回调给上层（T2.5 voice_cloud 负责）；
 *   - 网络状态从 event_bus 的 APP_EVT_NET_UP / APP_EVT_NET_DOWN 获取，不与 net_mgr 直连。
 *
 * 线程模型（ARCHITECTURE §9.3）：本组件建一个 `ws` 任务（核 0 / 优先级 5 / 6KB），
 * 负责状态机、退避、心跳与发送队列出队；esp_websocket_client 另建 `ws_client` 任务（库任务）
 * 负责 TCP/TLS/WS 收包与事件分发。两个任务都不允许阻塞式等待调用方。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 二进制帧头（ARCHITECTURE §6.2 定稿，布局不得私改） */
#define WS_FRAME_MAGIC0          0x56 /* 'V' */
#define WS_FRAME_MAGIC1          0x4F /* 'O' */
#define WS_FRAME_VER             1
#define WS_FRAME_HEADER_LEN      8
#define WS_FRAME_TYPE_AUDIO_UP   0x01 /* ↑ 上行麦克风音频（Opus 16k） */
#define WS_FRAME_TYPE_AUDIO_DOWN 0x02 /* ↓ 下行 TTS 音频（MP3 24k） */

typedef enum {
    WS_STATE_IDLE = 0,   /* 未初始化 / 未配置 URL / 鉴权失败后停用 */
    WS_STATE_WAIT_NET,   /* 已启动，等 NET_UP（wss 还要等 SNTP 对时） */
    WS_STATE_CONNECTING, /* 客户端已 start，等 TCP/TLS/WS 握手 */
    WS_STATE_CONNECTED,  /* 已连上，hello 已发，等 hello_ack */
    WS_STATE_READY,      /* hello_ack 已收到，可收发 */
    WS_STATE_BACKOFF,    /* 等退避重连（1/2/5/10/30s） */
} ws_state_t;

/* 下行二进制帧头；文本帧回调时 frame 传 NULL */
typedef struct {
    uint8_t ver;
    uint8_t type;
    uint16_t seq; /* 大端解析后的序号 */
} ws_frame_hdr_t;

/*
 * 入站回调：在 `ws_client` 任务上下文执行（核 0），**禁止阻塞/耗时操作**；
 * payload 只在回调内有效（返回后可能被覆盖），需要保留必须自行拷贝。
 * frame == NULL 表示文本帧，payload 为 JSON 原文（不保证 '\0' 结尾）。
 * 传输层自身已消费 hello_ack / pong，不会回调给上层。
 */
typedef void (*ws_recv_cb_t)(const ws_frame_hdr_t *frame, const uint8_t *payload, size_t len,
                             void *arg);

typedef struct {
    ws_state_t state;
    bool started;        /* 上层是否已请求在线 */
    bool url_configured; /* CONFIG_AI_WS_URL 是否可用 */
    bool tls;            /* 当前是否为 wss:// */
    uint32_t connect_count;  /* 累计握手完成（hello_ack）次数 */
    uint32_t retry_count;    /* 当前退避周期内的累计重连次数 */
    uint32_t next_retry_ms;  /* BACKOFF 状态下距离下次重连的剩余毫秒；0 表示无待执行重试 */
    uint32_t last_rtt_ms;    /* 最近一次心跳 RTT（毫秒） */
    uint32_t tx_text;        /* 成功入队的文本帧数 */
    uint32_t tx_bin;         /* 成功入队的二进制帧数 */
    uint32_t tx_dropped;     /* 入队失败（未就绪/队列满）的帧数 */
    uint32_t rx_text;        /* 收到的文本帧数 */
    uint32_t rx_bin;         /* 通过校验的二进制帧数 */
    uint32_t rx_bad;         /* 丢弃的坏帧/超长消息数 */
    uint32_t queue_used;     /* 发送队列当前占用槽位 */
    uint32_t queue_cap;      /* 发送队列总槽位 */
    char endpoint[96];       /* 脱敏端点：scheme://host[:port]/path（不含 query/token） */
} ws_status_t;

/**
 * @brief 初始化：解析 URL、建发送队列（PSRAM）、建 `ws` 任务、订阅网络事件
 *
 * 未配置 CONFIG_AI_WS_URL 或 scheme 非法时**降级为离线**（只打 WARN/ERROR，不返回错误），
 * 与 net_mgr 未配 SSID 的处理一致：本地唤醒/播放不受影响。
 *
 * @note 只应在 app_main 中调用一次（须早于 ws_start/ws_send_*）；重复调用返回 ESP_ERR_INVALID_STATE。
 * @note 必须在 app_evt_init() 之后调用（要注册 APP_EVT_NET_UP / NET_DOWN 处理器）。
 */
esp_err_t ws_init(void);

/**
 * @brief 请求保持在线：网络就绪后自动建连，之后由状态机自动重连
 *
 * 幂等；未配置 URL 时只记录 WARN 并保持 IDLE。真正的连接在 `ws` 任务里异步发起。
 */
esp_err_t ws_start(void);

/**
 * @brief 主动下线：断开当前连接并停止自动重连（调试/降级路径）
 *
 * 非阻塞：命令投递给 `ws` 任务，返回时可能仍在断开中。
 */
esp_err_t ws_stop(void);

/**
 * @brief 注册入站回调（可多次调用覆盖，传 NULL 取消）
 *
 * @note 回调在 `ws_client` 任务上下文执行，见文件头线程模型说明。
 */
esp_err_t ws_register_cb(ws_recv_cb_t cb, void *arg);

/**
 * @brief 发送文本帧（JSON），**非阻塞入队**
 *
 * 只有状态为 READY 时才入队；未就绪返回 ESP_ERR_INVALID_STATE，队列满返回 ESP_ERR_TIMEOUT
 * （均累加 tx_dropped 并限频 WARN）。调用方可以是任意任务，**不允许依赖返回值阻塞重试**。
 */
esp_err_t ws_send_text(const char *json);

/**
 * @brief 发送上行二进制帧（自动填 8 字节帧头与自增 seq），**非阻塞入队**
 *
 * @param type 只允许 WS_FRAME_TYPE_AUDIO_UP（0x01）；下行类型 0x02 不允许由端侧发送
 * @param payload 编码后的音频数据（Opus 一帧约 40B）
 * @param len 必须 ≤ 发送队列单槽上限 - 8（Kconfig WS_TX_MSG_BYTES）
 *
 * @note seq 由本组件按帧自增（uint16 回绕），与 ARCHITECTURE §6.2 一致；上层不参与序号管理。
 */
esp_err_t ws_send_bin(uint8_t type, const void *payload, size_t len);

/**
 * @brief 重置退避并立即重连（diag `ws reconnect` 用）
 *
 * 若当前已连接会先断开；非阻塞。
 */
esp_err_t ws_reconnect(void);

/* 是否已握手完成、可收发（READY） */
bool ws_is_ready(void);

/* 读取状态快照（供 diag 观测；字段可能被其它任务并发更新，仅用于观测） */
esp_err_t ws_status_read(ws_status_t *out);

/* 状态名（日志/调试口用） */
const char *ws_state_name(ws_state_t state);

#ifdef __cplusplus
}
#endif
