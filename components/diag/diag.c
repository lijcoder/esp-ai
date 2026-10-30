/*
 * diag —— 调试与观测能力：启动信息横幅、esp_console REPL、系统/日志命令、可选堆监控。
 *
 * 为什么独立成组件：后续每个任务都靠它自测（ROADMAP §2.0「先建观测能力」），
 * 而它只读系统状态、不改业务状态，属于 L3 能力层，由 main 调用、不被下层依赖。
 */
#include "diag.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_console.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "DIAG";

/*
 * 已知 TAG 表：唯一数据源是 CONVENTIONS §5 的固定清单（与 ARCHITECTURE §10.2 同步）。
 * `log` 命令只接受表内 TAG，防止拼错后静默生效、白调半天。
 */
static const char *const k_tags[] = {
    "APP", "BOARD", "AUDIO_IO", "AFE", "PLAYER", "ENC", "NET",
    "WS", "VC", "VS", "MC", "MS", "DEV", "DIAG", "CFG",
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
    UBaseType_t count;
    UBaseType_t capacity;
    UBaseType_t filled;
    TaskStatus_t *status;
#if CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS
    configRUN_TIME_COUNTER_TYPE total_runtime = 0;
#endif

    /* 先取任务数量再分配：两次调用之间任务数可能变化，多给 4 个余量 */
    count = uxTaskGetSystemState(NULL, 0, NULL);
    capacity = count + 4;
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
        printf("用法: sys info | heap | tasks | reboot\n");
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
    } else {
        printf("未知子命令: %s（可用: info | heap | tasks | reboot）\n", argv[1]);
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
