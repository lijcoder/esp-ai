/*
 * diag —— 调试与观测能力：启动信息横幅、esp_console REPL、系统/日志命令、可选堆监控。
 *
 * 为什么独立成组件：后续每个任务都靠它自测（ROADMAP §2.0「先建观测能力」），
 * 而它只读系统状态、不改业务状态，属于 L3 能力层，由 main 调用、不被下层依赖。
 */
#include "diag.h"

#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "audio_fe.h"
#include "config_store.h"
#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_console.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "event_bus.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net_mgr.h"
#include "nvs.h"
#include "sdkconfig.h"
#include "transport_ws.h"

static const char *TAG = "DIAG";

/*
 * 已知 TAG 表：唯一数据源是 CONVENTIONS §5 的固定清单（与 ARCHITECTURE §10.2 同步）。
 * `log` 命令只接受表内 TAG，防止拼错后静默生效、白调半天。
 */
static const char *const k_tags[] = {
    "APP", "BOARD", "AUDIO_IO", "AFE", "PLAYER", "ENC", "NET",
    "WS", "VC", "VS", "MC", "MS", "DEV", "DIAG", "CFG", "EVT",
};
#define TAG_COUNT (sizeof(k_tags) / sizeof(k_tags[0]))

/* 下标与 esp_log_level_t 数值一一对应（ESP_LOG_NONE=0 ... ESP_LOG_VERBOSE=5） */
static const char *const k_level_names[] = {
    "none", "error", "warn", "info", "debug", "verbose",
};
#define LEVEL_COUNT (sizeof(k_level_names) / sizeof(k_level_names[0]))

static bool s_inited;

/* ---------------- 基础格式化 ---------------- */

static const char *reset_reason_str(esp_reset_reason_t reason)
{
    switch (reason) {
    case ESP_RST_POWERON:
        return "POWERON";
    case ESP_RST_EXT:
        return "EXT";
    case ESP_RST_SW:
        return "SW";
    case ESP_RST_PANIC:
        return "PANIC";
    case ESP_RST_INT_WDT:
        return "INT_WDT";
    case ESP_RST_TASK_WDT:
        return "TASK_WDT";
    case ESP_RST_WDT:
        return "WDT";
    case ESP_RST_DEEPSLEEP:
        return "DEEPSLEEP";
    case ESP_RST_BROWNOUT:
        return "BROWNOUT";
    case ESP_RST_SDIO:
        return "SDIO";
    default:
        return "UNKNOWN";
    }
}

static const char *chip_model_str(esp_chip_model_t model)
{
    switch (model) {
    case CHIP_ESP32:
        return "ESP32";
    case CHIP_ESP32S2:
        return "ESP32-S2";
    case CHIP_ESP32S3:
        return "ESP32-S3";
    case CHIP_ESP32C3:
        return "ESP32-C3";
    case CHIP_ESP32C2:
        return "ESP32-C2";
    case CHIP_ESP32C6:
        return "ESP32-C6";
    case CHIP_ESP32H2:
        return "ESP32-H2";
    default:
        return "UNKNOWN";
    }
}

#if CONFIG_FREERTOS_USE_TRACE_FACILITY
static char task_state_char(eTaskState state)
{
    switch (state) {
    case eRunning:
        return 'R';
    case eReady:
        return 'r';
    case eBlocked:
        return 'B';
    case eSuspended:
        return 'S';
    case eDeleted:
        return 'D';
    default:
        return '?';
    }
}
#endif

/* ---------------- 堆水位 ---------------- */

typedef struct {
    size_t total;
    size_t free;
    size_t largest;
    size_t min_free;
} heap_stats_t;

static void heap_stats_get(heap_stats_t *out, uint32_t caps)
{
    multi_heap_info_t info;
    heap_caps_get_info(&info, caps);
    /* multi_heap 没有"总容量"字段，用已分配 + 空闲近似（两者之和恒等于可分配总量） */
    out->total = info.total_allocated_bytes + info.total_free_bytes;
    out->free = info.total_free_bytes;
    out->largest = info.largest_free_block;
    /* minimum_free_bytes 是开机以来的最低空闲记录，用来反推内部 RAM 动态峰值 */
    out->min_free = info.minimum_free_bytes;
}

static void heap_stats_print(const char *name, const heap_stats_t *s)
{
    printf("%s: 总量 %u B, 剩余 %u B, 最大块 %u B, 最低水位 %u B, 峰值占用 %u B\n",
           name, (unsigned)s->total, (unsigned)s->free, (unsigned)s->largest,
           (unsigned)s->min_free, (unsigned)(s->total - s->min_free));
}

/* ---------------- 启动横幅 ---------------- */

static void print_banner(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    heap_stats_t internal;
    heap_stats_t psram;

    heap_stats_get(&internal, MALLOC_CAP_INTERNAL);
    heap_stats_get(&psram, MALLOC_CAP_SPIRAM);

    ESP_LOGI(TAG, "================ esp-ai diag ================");
    ESP_LOGI(TAG, "app : %s v%s (%s %s)", app->project_name, app->version, app->date, app->time);
    ESP_LOGI(TAG, "idf : %s", esp_get_idf_version());
    ESP_LOGI(TAG, "rst : %s, 已运行 %" PRIu64 " ms", reset_reason_str(esp_reset_reason()),
             (uint64_t)(esp_timer_get_time() / 1000));
    ESP_LOGI(TAG, "mem : 内部 RAM 剩余 %u B / 最低水位 %u B; PSRAM 剩余 %u B",
             (unsigned)internal.free, (unsigned)internal.min_free, (unsigned)psram.free);
    ESP_LOGI(TAG, "提示: 输入 help 查看调试命令");
}

/* ---------------- sys 子命令 ---------------- */

static void print_info(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    esp_chip_info_t chip;
    heap_stats_t internal;
    int64_t uptime_ms;

    esp_chip_info(&chip);
    heap_stats_get(&internal, MALLOC_CAP_INTERNAL);
    uptime_ms = esp_timer_get_time() / 1000;

    printf("应用     : %s v%s\n", app->project_name, app->version);
    printf("编译时间 : %s %s\n", app->date, app->time);
    printf("IDF      : %s\n", esp_get_idf_version());
    printf("芯片     : %s rev v%d.%d, %d 核\n", chip_model_str(chip.model), chip.revision / 100,
           chip.revision % 100, chip.cores);
    printf("复位原因 : %s\n", reset_reason_str(esp_reset_reason()));
    printf("运行时长 : %" PRIu64 " s\n", (uint64_t)(uptime_ms / 1000));
    printf("内存水位 : 内部 RAM 剩余 %u B / 最低水位 %u B\n", (unsigned)internal.free,
           (unsigned)internal.min_free);
}

static void print_heap(void)
{
    heap_stats_t internal;
    heap_stats_t psram;

    heap_stats_get(&internal, MALLOC_CAP_INTERNAL);
    heap_stats_get(&psram, MALLOC_CAP_SPIRAM);
    heap_stats_print("内部 RAM", &internal);
    if (psram.total > 0) {
        heap_stats_print("PSRAM   ", &psram);
    } else {
        printf("PSRAM   : 未初始化或未启用\n");
    }
}

#if CONFIG_FREERTOS_USE_TRACE_FACILITY
static void print_tasks(void)
{
    UBaseType_t capacity;
    UBaseType_t filled;
    TaskStatus_t *status;
#if CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS
    configRUN_TIME_COUNTER_TYPE total_runtime = 0;
#endif

    /* 容量必须 >= 当前任务数：本内核版本的 uxTaskGetSystemState 不支持"传 NULL 查数量"，
     * 容量不足时直接返回 0（T0.7① 真机验收发现 sys tasks 曾恒为 0 个任务）。
     * 两次调用之间任务数可能变化，多给 8 个余量。 */
    capacity = uxTaskGetNumberOfTasks() + 8;
    status = heap_caps_calloc(capacity, sizeof(TaskStatus_t), MALLOC_CAP_DEFAULT);
    if (status == NULL) {
        printf("内存不足，无法获取任务状态\n");
        return;
    }

#if CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS
    filled = uxTaskGetSystemState(status, capacity, &total_runtime);
#else
    filled = uxTaskGetSystemState(status, capacity, NULL);
#endif
    if (filled == 0) {
        printf("任务状态查询失败：容量 %u 不足\n", (unsigned)capacity);
        heap_caps_free(status);
        return;
    }

    printf("%-16s %-4s %-6s %12s %6s\n", "任务", "状态", "优先级", "栈剩余(B)", "CPU");
    for (UBaseType_t i = 0; i < filled; i++) {
        const TaskStatus_t *t = &status[i];
        /* FreeRTOS 的水位单位是 StackType_t 个数（S3 上 4 字节），这里换算成字节便于对照 */
        unsigned stack_bytes = (unsigned)(t->usStackHighWaterMark * sizeof(StackType_t));
#if CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS
        unsigned percent = 0;
        if (total_runtime > 0) {
            percent = (unsigned)((uint64_t)t->ulRunTimeCounter * 100u / (uint64_t)total_runtime);
        }
        printf("%-16s %-4c %-6u %10u B %5u%%\n", t->pcTaskName, task_state_char(t->eCurrentState),
               (unsigned)t->uxCurrentPriority, stack_bytes, percent);
#else
        printf("%-16s %-4c %-6u %10u B %6s\n", t->pcTaskName, task_state_char(t->eCurrentState),
               (unsigned)t->uxCurrentPriority, stack_bytes, "-");
#endif
    }
    printf("共 %u 个任务；CPU 为自启动以来的累计占比\n", (unsigned)filled);

    heap_caps_free(status);
}
#endif /* CONFIG_FREERTOS_USE_TRACE_FACILITY */

static int cmd_sys(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: sys info | heap | tasks | reboot%s\n",
#if CONFIG_DIAG_ENABLE_CRASH_TEST
               " | crash"
#else
               ""
#endif
        );
        return ESP_ERR_INVALID_ARG;
    }

    if (strcmp(argv[1], "info") == 0) {
        print_info();
    } else if (strcmp(argv[1], "heap") == 0) {
        print_heap();
    } else if (strcmp(argv[1], "tasks") == 0) {
#if CONFIG_FREERTOS_USE_TRACE_FACILITY
        print_tasks();
#else
        printf("任务统计不可用：需 CONFIG_FREERTOS_USE_TRACE_FACILITY=y\n");
        return ESP_ERR_NOT_SUPPORTED;
#endif
    } else if (strcmp(argv[1], "reboot") == 0) {
        printf("正在软复位...\n");
        fflush(stdout);
        esp_restart();
        return ESP_OK;
    } else if (strcmp(argv[1], "crash") == 0) {
#if CONFIG_DIAG_ENABLE_CRASH_TEST
        /* 故意触发 panic，验证 coredump 落盘与事后解析（仅开发配置编译） */
        printf("即将触发 panic 验证 coredump；设备会重启，之后执行:\n"
               "  idf.py -p <PORT> coredump-info\n");
        fflush(stdout);
        esp_system_abort("diag crash test");
        return ESP_OK; /* 不会执行到 */
#else
        printf("crash 测试未编译：需 CONFIG_DIAG_ENABLE_CRASH_TEST=y\n");
        return ESP_ERR_NOT_SUPPORTED;
#endif
    } else {
        printf("未知子命令: %s（可用: info | heap | tasks | reboot%s）\n", argv[1],
#if CONFIG_DIAG_ENABLE_CRASH_TEST
               " | crash"
#else
               ""
#endif
        );
        return ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
}

/* ---------------- log 命令 ---------------- */

static bool tag_is_known(const char *tag)
{
    for (size_t i = 0; i < TAG_COUNT; i++) {
        if (strcmp(k_tags[i], tag) == 0) {
            return true;
        }
    }
    return false;
}

static int parse_level(const char *name, esp_log_level_t *out)
{
    for (size_t i = 0; i < LEVEL_COUNT; i++) {
        if (strcmp(k_level_names[i], name) == 0) {
            *out = (esp_log_level_t)i;
            return 0;
        }
    }
    return -1;
}

static const char *level_name(esp_log_level_t level)
{
    if ((int)level < 0 || (int)level >= (int)LEVEL_COUNT) {
        return "?";
    }
    return k_level_names[level];
}

static int cmd_log(int argc, char **argv)
{
    esp_log_level_t level;

    if (argc == 1) {
        printf("已知 TAG（%u 个）:\n", (unsigned)TAG_COUNT);
        for (size_t i = 0; i < TAG_COUNT; i++) {
            printf("  %-10s %s\n", k_tags[i], level_name(esp_log_level_get(k_tags[i])));
        }
        printf("  %-10s %s\n", "*", level_name(esp_log_level_get("*")));
        return ESP_OK;
    }

    if (argc != 3) {
        printf("用法: log [<tag>|*] <none|error|warn|info|debug|verbose>\n");
        return ESP_ERR_INVALID_ARG;
    }

    if (parse_level(argv[2], &level) != 0) {
        printf("非法日志级别: %s（可选 none/error/warn/info/debug/verbose）\n", argv[2]);
        return ESP_ERR_INVALID_ARG;
    }

    /* 只允许清单内 TAG 或全局通配符，拼错立即报错而不是静默生效 */
    if (strcmp(argv[1], "*") != 0 && !tag_is_known(argv[1])) {
        printf("未知 TAG: %s（执行 log 查看已知 TAG）\n", argv[1]);
        return ESP_ERR_INVALID_ARG;
    }

    esp_log_level_set(argv[1], level);
    printf("%s 的日志级别已设为 %s\n", argv[1], argv[2]);
    return ESP_OK;
}

/* ---------------- mic 命令（麦克风观测，T0.7a） ---------------- */

/*
 * 幅度 → dBFS。用 0.1dB 定点输出（20·log10(x/32768) 的 10 倍），
 * 不依赖 printf 的浮点格式，输出在任何 newlib 配置下都一致。
 */
static void print_db_line(const char *label, uint32_t linear)
{
    int deci;
    int mag;

    if (linear == 0) {
        printf("  %s: %u（-inf，静音）\n", label, (unsigned)linear);
        return;
    }
    deci = (int)lround(200.0 * log10((double)linear / 32768.0));
    mag = deci < 0 ? -deci : deci;
    printf("  %s: %u（%s%d.%d dBFS）\n", label, (unsigned)linear, deci < 0 ? "-" : "", mag / 10,
           mag % 10);
}

static void print_mic_stats(void)
{
    audio_fe_stats_t st;
    uint32_t rms = 0;
    uint32_t window_ms = 0;

    esp_err_t err = audio_fe_stats_read(&st);
    if (err != ESP_OK) {
        printf("读取麦克风统计失败: %s\n", esp_err_to_name(err));
        return;
    }
    if (st.chunksize == 0) {
        printf("音频前端未运行（audio_fe_start 未调用或启动失败）\n");
        return;
    }

    /* RMS 用平方和现算；开方与 log 都在 console 任务侧，不占音频任务 */
    if (st.sample_count > 0) {
        rms = (uint32_t)lround(sqrt((double)st.sum_sq / (double)st.sample_count));
        if (st.sample_rate) {
            window_ms = (uint32_t)(((uint64_t)st.sample_count * 1000u) / st.sample_rate);
        }
    }

    printf("麦克风统计：窗口 %u ms / %u 采样 @ %u Hz\n", (unsigned)window_ms,
           (unsigned)st.sample_count, (unsigned)st.sample_rate);
    print_db_line("电平", rms);
    print_db_line("峰值", (uint32_t)st.peak);
    printf("  采集: %u 帧 × %u 采样，I2S 读失败 %u 次\n", (unsigned)st.frames_fed,
           (unsigned)st.chunksize, (unsigned)st.mic_read_errors);
    if (st.feed_enabled) {
        printf("  状态: 收音 开启，VAD %s，累计唤醒 %u 次\n", st.vad_active ? "人声" : "静音",
               (unsigned)st.wake_count);
    } else {
        printf("  状态: 收音 暂停（R1 半双工，AFE 输入为静音），VAD %s，累计唤醒 %u 次\n",
               st.vad_active ? "人声" : "静音", (unsigned)st.wake_count);
    }
    printf("  参考: 安静室内底噪 RMS≈40~270；正常说话峰值数百~4k\n");
}

static int cmd_mic(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: mic stat | reset\n");
        return ESP_ERR_INVALID_ARG;
    }

    if (strcmp(argv[1], "stat") == 0) {
        print_mic_stats();
        return ESP_OK;
    }
    if (strcmp(argv[1], "reset") == 0) {
        esp_err_t err = audio_fe_stats_reset();
        if (err != ESP_OK) {
            printf("清零失败: %s\n", esp_err_to_name(err));
            return err;
        }
        printf("麦克风统计已清零\n");
        return ESP_OK;
    }

    printf("未知子命令: %s（可用: stat | reset）\n", argv[1]);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- cfg 命令（配置持久化，T1.1） ---------------- */

/*
 * 注意语义：`cfg set` 是**底层存储调试口**，不做范围/语义校验（比如 volume=150 会原样落盘，
 * 由 cfg_get_volume() 在读取侧 clamp）。业务语义的写入应由对应模块的 API 完成，
 * 这里是用来验证"NVS 落盘/重启保持/缺省回退"本身的。
 */

static void print_cfg_info(void)
{
    uint32_t used = 0;
    uint32_t free_entries = 0;
    esp_err_t err = cfg_get_usage(&used, &free_entries);

    if (err == ESP_OK) {
        printf("NVS 分区：已用 %" PRIu32 " 条目 / 空闲 %" PRIu32 " 条目\n", used, free_entries);
    } else {
        printf("读取 NVS 统计失败: %s\n", esp_err_to_name(err));
    }

    int volume = 0;
    err = cfg_get_volume(&volume);
    if (err == ESP_OK) {
        printf("音量 volume = %d（0~100，缺省 %d）\n", volume, CONFIG_CFG_VOLUME_DEFAULT);
    } else {
        printf("读取音量失败: %s\n", esp_err_to_name(err));
    }
    printf("说明：cfg set 是底层存储调试口（不做范围/语义校验），重启后才生效；\n"
           "      运行中改音量走 T5.1 的 audio vol。\n");
}

static int cmd_cfg(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: cfg info | get <key> | set <key> <int> | setstr <key> <value> | erase <key>\n");
        return ESP_ERR_INVALID_ARG;
    }

    if (strcmp(argv[1], "info") == 0) {
        print_cfg_info();
        return ESP_OK;
    }

    if (strcmp(argv[1], "get") == 0) {
        if (argc != 3) {
            printf("用法: cfg get <key>\n");
            return ESP_ERR_INVALID_ARG;
        }
        int32_t int_value = 0;
        esp_err_t err = cfg_get_int(argv[2], &int_value);
        if (err == ESP_OK) {
            printf("%s = %" PRId32 "（int）\n", argv[2], int_value);
            return ESP_OK;
        }
        /* int 读失败（含类型不匹配）再试字符串：console 上不想让用户先声明类型 */
        char str_value[128];
        err = cfg_get_str(argv[2], str_value, sizeof(str_value));
        if (err == ESP_OK) {
            printf("%s = \"%s\"（str）\n", argv[2], str_value);
            return ESP_OK;
        }
        if (err == ESP_ERR_NVS_NOT_FOUND) {
            printf("%s 未设置（使用缺省）\n", argv[2]);
            return ESP_OK;
        }
        if (err == ESP_ERR_NVS_INVALID_LENGTH) {
            printf("%s 是字符串但超过 %u 字节，console 不显示\n", argv[2],
                   (unsigned)sizeof(str_value));
            return ESP_OK;
        }
        printf("读取 %s 失败: %s\n", argv[2], esp_err_to_name(err));
        return err;
    }

    if (strcmp(argv[1], "set") == 0) {
        if (argc != 4) {
            printf("用法: cfg set <key> <int>\n");
            return ESP_ERR_INVALID_ARG;
        }
        char *end = NULL;
        errno = 0;
        long value = strtol(argv[3], &end, 0);
        if (errno == ERANGE || end == argv[3] || *end != '\0') {
            printf("非法整数: %s（支持十进制/0x 十六进制）\n", argv[3]);
            return ESP_ERR_INVALID_ARG;
        }
        esp_err_t err = cfg_set_int(argv[2], (int32_t)value);
        if (err != ESP_OK) {
            printf("写入失败: %s\n", esp_err_to_name(err));
            return err;
        }
        printf("已写入 %s = %ld（int，重启后生效）\n", argv[2], value);
        return ESP_OK;
    }

    if (strcmp(argv[1], "setstr") == 0) {
        if (argc != 4) {
            printf("用法: cfg setstr <key> <value>（value 不支持空格）\n");
            return ESP_ERR_INVALID_ARG;
        }
        esp_err_t err = cfg_set_str(argv[2], argv[3]);
        if (err != ESP_OK) {
            printf("写入失败: %s\n", esp_err_to_name(err));
            return err;
        }
        printf("已写入 %s（str，%u 字节，重启后生效）\n", argv[2], (unsigned)strlen(argv[3]));
        return ESP_OK;
    }

    if (strcmp(argv[1], "erase") == 0) {
        if (argc != 3) {
            printf("用法: cfg erase <key>\n");
            return ESP_ERR_INVALID_ARG;
        }
        esp_err_t err = cfg_erase_key(argv[2]);
        if (err == ESP_ERR_NVS_NOT_FOUND) {
            printf("%s 本来就未设置\n", argv[2]);
            return ESP_OK;
        }
        if (err != ESP_OK) {
            printf("删除失败: %s\n", esp_err_to_name(err));
            return err;
        }
        printf("已删除 %s（重启后使用缺省）\n", argv[2]);
        return ESP_OK;
    }

    printf("未知子命令: %s（可用: info | get | set | setstr | erase）\n", argv[1]);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- wifi 命令（网络观测，T1.3） ---------------- */

static void print_wifi_status(void)
{
    net_info_t info;
    esp_err_t err = net_get_info(&info);
    if (err != ESP_OK) {
        printf("读取网络状态失败: %s\n", esp_err_to_name(err));
        return;
    }

    printf("WiFi 状态: %s\n", net_state_name(info.state));
    if (info.ssid[0] == '\0') {
        printf("  SSID: 未配置（CONFIG_AI_WIFI_SSID 为空，联网功能关闭，本地功能可用）\n");
        return;
    }

    printf("  SSID: %s\n", info.ssid);
    if (info.state == NET_STATE_UP) {
        printf("  IP: %s，RSSI: %d dBm\n", info.ip, info.rssi);
    } else {
        printf("  IP: （未联网）\n");
    }
    printf("  SNTP: %s（服务器 %s）\n", info.time_synced ? "已对时" : "未对时",
           CONFIG_NET_SNTP_SERVER);
    if (info.next_retry_ms > 0) {
        printf("  重连: 累计 %u 次，%u ms 后重试\n", (unsigned)info.retry_count,
               (unsigned)info.next_retry_ms);
    } else {
        printf("  重连: 累计 %u 次，当前没有待执行的重试\n", (unsigned)info.retry_count);
    }
}

static int cmd_wifi(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: wifi status | reconnect\n");
        return ESP_ERR_INVALID_ARG;
    }

    if (strcmp(argv[1], "status") == 0) {
        print_wifi_status();
        return ESP_OK;
    }

    if (strcmp(argv[1], "reconnect") == 0) {
        esp_err_t err = net_reconnect();
        if (err == ESP_ERR_INVALID_STATE) {
            printf("无法重连：未初始化或未配置 CONFIG_AI_WIFI_SSID\n");
            return ESP_OK;
        }
        if (err != ESP_OK) {
            printf("重连失败: %s\n", esp_err_to_name(err));
            return err;
        }
        printf("已请求断开并重连（退避已重置，可用 wifi status 观察）\n");
        return ESP_OK;
    }

    printf("未知子命令: %s（可用: status | reconnect）\n", argv[1]);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- ws 命令（WebSocket 观测，T1.4） ---------------- */

static void print_ws_status(void)
{
    ws_status_t st;
    esp_err_t err = ws_status_read(&st);
    if (err != ESP_OK) {
        printf("读取 WebSocket 状态失败: %s\n", esp_err_to_name(err));
        return;
    }

    printf("WebSocket 状态: %s（%s）\n", ws_state_name(st.state),
           st.started ? "已请求在线" : "未请求在线");
    if (!st.url_configured) {
        printf("  端点: 未配置（CONFIG_AI_WS_URL 为空或非法，云端链路关闭）\n");
        return;
    }

    printf("  端点: %s（%s）\n", st.endpoint, st.tls ? "wss/TLS" : "ws 明文，仅本地联调");
    if (st.state == WS_STATE_BACKOFF) {
        printf("  重连: 累计 %u 次，%u ms 后重试\n", (unsigned)st.retry_count,
               (unsigned)st.next_retry_ms);
    } else {
        printf("  重连: 累计 %u 次，当前无待执行重试\n", (unsigned)st.retry_count);
    }
    printf("  握手完成: %u 次，最近心跳 RTT: %u ms\n", (unsigned)st.connect_count,
           (unsigned)st.last_rtt_ms);
    printf("  发送: 文本 %u / 二进制 %u，丢弃 %u；队列 %u/%u\n", (unsigned)st.tx_text,
           (unsigned)st.tx_bin, (unsigned)st.tx_dropped, (unsigned)st.queue_used,
           (unsigned)st.queue_cap);
    printf("  接收: 文本 %u / 二进制 %u，坏帧 %u\n", (unsigned)st.rx_text, (unsigned)st.rx_bin,
           (unsigned)st.rx_bad);
    printf("  说明: 每 20s 发 ping、10s 未收 pong 判离线；掉线按 1/2/5/10/30s 退避\n");
}

static int cmd_ws(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: ws status | reconnect\n");
        return ESP_ERR_INVALID_ARG;
    }

    if (strcmp(argv[1], "status") == 0) {
        print_ws_status();
        return ESP_OK;
    }

    if (strcmp(argv[1], "reconnect") == 0) {
        esp_err_t err = ws_reconnect();
        if (err == ESP_ERR_INVALID_STATE) {
            printf("无法重连：未初始化或未配置 CONFIG_AI_WS_URL\n");
            return ESP_OK;
        }
        if (err != ESP_OK) {
            printf("重连失败: %s\n", esp_err_to_name(err));
            return err;
        }
        printf("已请求重连（退避已重置，可用 ws status 观察）\n");
        return ESP_OK;
    }

    printf("未知子命令: %s（可用: status | reconnect）\n", argv[1]);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- evt 命令（事件总线观测，T1.2a） ---------------- */

/*
 * 本命令只做"投递/订阅"的观测口，不模拟业务：post 用的是示例 payload，
 * trace 打开后能看到事件是否真的在 evt 任务上下文被分发到（而不是只打印了投递）。
 */

static bool evt_has_payload(app_evt_t evt)
{
    return evt == APP_EVT_NET_UP || evt == APP_EVT_NET_DOWN;
}

static bool evt_parse(const char *text, app_evt_t *out)
{
    for (int i = 0; i < APP_EVT_COUNT; i++) {
        if (strcmp(text, app_evt_name((app_evt_t)i)) == 0) {
            *out = (app_evt_t)i;
            return true;
        }
    }

    char *end = NULL;
    errno = 0;
    long value = strtol(text, &end, 0);
    if (errno == 0 && end != text && *end == '\0' && value >= 0 && value < APP_EVT_COUNT) {
        *out = (app_evt_t)value;
        return true;
    }
    return false;
}

static void evt_fill_sample_payload(app_evt_t evt, app_evt_data_t *data)
{
    memset(data, 0, sizeof(*data));
    if (evt == APP_EVT_NET_UP) {
        snprintf(data->net_up.ip, sizeof(data->net_up.ip), "%s", "10.0.0.42");
        data->net_up.rssi = -42;
    } else if (evt == APP_EVT_NET_DOWN) {
        data->net_down.reason = 2; /* 示例值：模拟鉴权过期类断开原因 */
        data->net_down.retry_ms = 1000;
    }
}

static void evt_trace_cb(void *arg, app_evt_t evt, const app_evt_data_t *data)
{
    (void)arg;
    if (evt == APP_EVT_NET_UP && data != NULL) {
        ESP_LOGI(TAG, "evt trace: %s ip=%s rssi=%d", app_evt_name(evt), data->net_up.ip,
                 data->net_up.rssi);
    } else if (evt == APP_EVT_NET_DOWN && data != NULL) {
        ESP_LOGI(TAG, "evt trace: %s reason=%u retry=%u ms", app_evt_name(evt),
                 (unsigned)data->net_down.reason, (unsigned)data->net_down.retry_ms);
    } else {
        ESP_LOGI(TAG, "evt trace: %s", app_evt_name(evt));
    }
}

static void print_evt_list(void)
{
    printf("APP_EVT 事件（共 %d 个，定义见 ARCHITECTURE §4.2）：\n", (int)APP_EVT_COUNT);
    for (int i = 0; i < APP_EVT_COUNT; i++) {
        printf("  %2d  %-20s %s\n", i, app_evt_name((app_evt_t)i),
               evt_has_payload((app_evt_t)i) ? "带 payload" : "无 payload");
    }
    printf("说明: evt post <name|id> / burst <n> / trace on|off\n");
}

static int cmd_evt(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: evt list | post <name|id> | burst <n> | trace on|off\n");
        return ESP_ERR_INVALID_ARG;
    }

    if (strcmp(argv[1], "list") == 0) {
        print_evt_list();
        return ESP_OK;
    }

    if (strcmp(argv[1], "post") == 0) {
        if (argc != 3) {
            printf("用法: evt post <name|id>\n");
            return ESP_ERR_INVALID_ARG;
        }
        app_evt_t evt;
        if (!evt_parse(argv[2], &evt)) {
            printf("未知事件: %s（evt list 查看；支持名字或 0~%d 的 ID）\n", argv[2],
                   (int)APP_EVT_COUNT - 1);
            return ESP_ERR_INVALID_ARG;
        }

        app_evt_data_t data;
        const bool with_payload = evt_has_payload(evt);
        if (with_payload) {
            evt_fill_sample_payload(evt, &data);
        }
        esp_err_t err = app_evt_post(evt, with_payload ? &data : NULL);
        if (err != ESP_OK) {
            printf("投递失败: %s\n", esp_err_to_name(err));
            return err;
        }
        if (evt == APP_EVT_NET_UP) {
            printf("已投递 %s（示例 payload: ip=%s rssi=%d）\n", app_evt_name(evt), data.net_up.ip,
                   data.net_up.rssi);
        } else if (evt == APP_EVT_NET_DOWN) {
            printf("已投递 %s（示例 payload: reason=%u retry=%u ms）\n", app_evt_name(evt),
                   (unsigned)data.net_down.reason, (unsigned)data.net_down.retry_ms);
        } else {
            printf("已投递 %s（无 payload）\n", app_evt_name(evt));
        }
        return ESP_OK;
    }

    if (strcmp(argv[1], "burst") == 0) {
        if (argc != 3) {
            printf("用法: evt burst <n>（n=1~256）\n");
            return ESP_ERR_INVALID_ARG;
        }
        char *end = NULL;
        errno = 0;
        long count = strtol(argv[2], &end, 0);
        if (errno != 0 || end == argv[2] || *end != '\0' || count < 1 || count > 256) {
            printf("非法数量: %s（1~256）\n", argv[2]);
            return ESP_ERR_INVALID_ARG;
        }

        uint32_t ok = 0;
        uint32_t dropped = 0;
        for (long i = 0; i < count; i++) {
            if (app_evt_post(APP_EVT_WAKE_DETECTED, NULL) == ESP_OK) {
                ok++;
            } else {
                dropped++;
            }
        }
        printf("突发投递 %ld 个 WAKE_DETECTED：成功 %u / 丢弃 %u（queue=%d，非阻塞语义）\n", count,
               (unsigned)ok, (unsigned)dropped, CONFIG_EVENT_BUS_QUEUE_SIZE);
        return ESP_OK;
    }

    if (strcmp(argv[1], "trace") == 0) {
        if (argc != 3) {
            printf("用法: evt trace on|off\n");
            return ESP_ERR_INVALID_ARG;
        }
        if (strcmp(argv[2], "on") == 0) {
            esp_err_t err = app_evt_register(APP_EVT_ANY, evt_trace_cb, NULL);
            if (err == ESP_ERR_INVALID_STATE) {
                printf("事件 trace 已经开启\n");
                return ESP_OK;
            }
            if (err != ESP_OK) {
                printf("开启失败: %s\n", esp_err_to_name(err));
                return err;
            }
            printf("事件 trace 已开启：在 evt 任务上下文打印收到的每个事件\n");
            return ESP_OK;
        }
        if (strcmp(argv[2], "off") == 0) {
            esp_err_t err = app_evt_unregister(APP_EVT_ANY, evt_trace_cb, NULL);
            if (err == ESP_ERR_NOT_FOUND) {
                printf("事件 trace 本来就未开启\n");
                return ESP_OK;
            }
            if (err != ESP_OK) {
                printf("关闭失败: %s\n", esp_err_to_name(err));
                return err;
            }
            printf("事件 trace 已关闭\n");
            return ESP_OK;
        }
        printf("用法: evt trace on|off\n");
        return ESP_ERR_INVALID_ARG;
    }

    printf("未知子命令: %s（可用: list | post | burst | trace）\n", argv[1]);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- 堆水位周期监控（默认关闭） ---------------- */

#if CONFIG_DIAG_HEAP_MONITOR
static void monitor_task(void *arg)
{
    (void)arg;
    const TickType_t period = pdMS_TO_TICKS(CONFIG_DIAG_HEAP_MONITOR_INTERVAL_S * 1000);

    for (;;) {
        heap_stats_t internal;
        heap_stats_t psram;

        vTaskDelay(period);
        heap_stats_get(&internal, MALLOC_CAP_INTERNAL);
        heap_stats_get(&psram, MALLOC_CAP_SPIRAM);
        ESP_LOGI(TAG, "monitor: 内部 RAM 剩余 %u B / 最低水位 %u B; PSRAM 剩余 %u B",
                 (unsigned)internal.free, (unsigned)internal.min_free, (unsigned)psram.free);
    }
}
#endif

/* ---------------- 初始化 ---------------- */

static esp_err_t register_commands(void)
{
    const esp_console_cmd_t cmds[] = {
        {
            .command = "sys",
            .help = "系统信息：sys info | heap | tasks | reboot",
            .hint = "info|heap|tasks|reboot",
            .func = cmd_sys,
        },
        {
            .command = "log",
            .help = "查看/设置日志级别：log 列出；log <tag>|* <level>",
            .hint = "[tag] [none|error|warn|info|debug|verbose]",
            .func = cmd_log,
        },
        {
            .command = "mic",
            .help = "麦克风统计：mic stat | reset",
            .hint = "stat|reset",
            .func = cmd_mic,
        },
        {
            .command = "cfg",
            .help = "配置持久化：cfg info | get <key> | set <key> <int> | setstr <key> <value> | erase <key>",
            .hint = "info|get|set|setstr|erase",
            .func = cmd_cfg,
        },
        {
            .command = "evt",
            .help = "事件总线观测：evt list | post <name|id> | burst <n> | trace on|off",
            .hint = "list|post|burst|trace",
            .func = cmd_evt,
        },
        {
            .command = "wifi",
            .help = "网络状态：wifi status | reconnect",
            .hint = "status|reconnect",
            .func = cmd_wifi,
        },
        {
            .command = "ws",
            .help = "WebSocket 状态：ws status | reconnect",
            .hint = "status|reconnect",
            .func = cmd_ws,
        },
    };

    for (size_t i = 0; i < sizeof(cmds) / sizeof(cmds[0]); i++) {
        esp_err_t err = esp_console_cmd_register(&cmds[i]);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "注册命令 %s 失败: %s", cmds[i].command, esp_err_to_name(err));
            return err;
        }
    }
    return ESP_OK;
}

esp_err_t diag_init(void)
{
    esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    esp_console_dev_uart_config_t uart_config = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    esp_console_repl_t *repl = NULL;
    esp_err_t err;

    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    /* 横幅先打：后面任何模块初始化失败都能在日志里看到启动上下文 */
    print_banner();

    repl_config.prompt = "esp-ai>";
    repl_config.max_cmdline_length = 128;

    err = esp_console_new_repl_uart(&uart_config, &repl_config, &repl);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "console REPL 初始化失败: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_console_register_help_command();
    if (err == ESP_OK) {
        err = register_commands();
    }
    if (err == ESP_OK) {
        err = esp_console_start_repl(repl);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "console 启动失败: %s", esp_err_to_name(err));
        return err;
    }

#if CONFIG_DIAG_HEAP_MONITOR
    if (xTaskCreate(monitor_task, "diag_mon", 3072, NULL, 1, NULL) != pdPASS) {
        ESP_LOGW(TAG, "堆监控任务创建失败（不影响其它功能）");
    }
#endif

    s_inited = true;
    return ESP_OK;
}
