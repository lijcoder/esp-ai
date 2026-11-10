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
#include "audio_player.h"
#include "codec_enc.h"
#include "config_store.h"
#include "dev_ctrl.h"
#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_console.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "event_bus.h"
#include "feedback.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "music_cloud.h"
#include "music_session.h"
#include "net_mgr.h"
#include "nvs.h"
#include "prompt_assets.h"
#include "sdkconfig.h"
#include "transport_ws.h"
#include "voice_cloud.h"
#include "voice_session.h"

static const char *TAG = "DIAG";

/*
 * 已知 TAG 表：唯一数据源是 CONVENTIONS §5 的固定清单（与 ARCHITECTURE §10.2 同步）。
 * `log` 命令只接受表内 TAG，防止拼错后静默生效、白调半天。
 */
static const char *const k_tags[] = {
    "APP", "BOARD", "AUDIO_IO", "AFE", "PLAYER", "ENC", "NET",
    "WS", "VC", "VS", "MC", "MS", "DEV", "DIAG", "CFG", "EVT", "FB", "PROMPT",
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
    printf("  门控: holds=%" PRIu32 " releases=%" PRIu32 " hold_mask=0x%02x discards=%" PRIu32
           " discard_samples=%" PRIu32 " last_discard_ms=%" PRIu32 "\n",
           st.feed_holds, st.feed_releases, (unsigned)st.feed_hold_mask, st.feed_discards,
           st.feed_discard_samples, st.feed_last_discard_ms);
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

/* ---------------- afe 命令（上行 PCM 探针，T2.1） ----------------
 *
 * 开发期探针（同 T1.6 `ws send` 先例）：只做观测，不参与产品链路、不改协议。
 * 读动作放在 console 任务里同步做（不新增任务），按 320 样本（20ms@16k）读，
 * 顺带验证 T2.2 需要的"精确分帧读出"契约。
 */
#define AFE_UPLINK_PROBE_FRAME 320 /* 20ms @ 16k，Opus 帧长 */
#define AFE_UPLINK_SEG_MS      500 /* 分段 RMS 粒度：抗环境底噪漂移，供对比回放前后 */
#define AFE_UPLINK_SEG_MAX     20

static void print_uplink_segments(const uint64_t *seg_sq, const uint32_t *seg_n, uint32_t segs)
{
    printf("  分段 RMS(%dms):", AFE_UPLINK_SEG_MS);
    for (uint32_t i = 0; i < segs; i++) {
        uint32_t v = seg_n[i] ? (uint32_t)lround(sqrt((double)seg_sq[i] / (double)seg_n[i])) : 0;
        printf(" %u", (unsigned)v);
    }
    printf("\n");
}

static void print_afe_uplink(uint32_t window_ms)
{
    static int16_t frame[AFE_UPLINK_PROBE_FRAME];
    audio_fe_uplink_stats_t st;
    uint32_t frames = 0;
    uint32_t samples = 0;
    uint64_t sum_sq = 0;
    uint32_t peak = 0;
    uint32_t rms = 0;
    int64_t end_us;
    uint64_t seg_sq[AFE_UPLINK_SEG_MAX];
    uint32_t seg_n[AFE_UPLINK_SEG_MAX];
    const uint32_t seg_samples = (uint32_t)AFE_UPLINK_SEG_MS * 16u;
    uint32_t segs = 0;

    memset(seg_sq, 0, sizeof(seg_sq));
    memset(seg_n, 0, sizeof(seg_n));

    if (audio_fe_uplink_stats_read(&st) != ESP_OK) {
        printf("读取上行统计失败\n");
        return;
    }
    if (!st.active) {
        printf("上行未启动：先执行 `afe uplink on`（否则环形缓冲无数据）\n");
    }

    end_us = esp_timer_get_time() + (int64_t)window_ms * 1000;
    while (esp_timer_get_time() < end_us) {
        int64_t remain_ms = (end_us - esp_timer_get_time()) / 1000;
        if (remain_ms <= 0) {
            break;
        }
        int got = audio_fe_uplink_read(frame, AFE_UPLINK_PROBE_FRAME, (uint32_t)remain_ms);
        if (got <= 0) {
            break;
        }
        frames++;
        uint32_t seg = samples / seg_samples;
        samples += (uint32_t)got;
        int64_t seg_sq_acc = 0;
        for (int i = 0; i < got; i++) {
            int32_t v = frame[i];
            if (v < 0) {
                v = -v;
            }
            if ((uint32_t)v > peak) {
                peak = (uint32_t)v;
            }
            seg_sq_acc += (int64_t)v * v;
        }
        sum_sq += (uint64_t)seg_sq_acc;
        if (seg < AFE_UPLINK_SEG_MAX) {
            seg_sq[seg] += (uint64_t)seg_sq_acc;
            seg_n[seg] += (uint32_t)got;
            if (seg + 1 > segs) {
                segs = seg + 1;
            }
        }
    }
    if (samples > 0) {
        rms = (uint32_t)lround(sqrt((double)sum_sq / (double)samples));
    }

    if (audio_fe_uplink_stats_read(&st) != ESP_OK) {
        return;
    }

    printf("上行 PCM（AFE 输出：16k / 单声道 / int16）\n");
    printf("  环形缓冲: %u 样本（%u ms，%u B PSRAM），历史最高占用 %u 样本\n",
           (unsigned)st.ring_samples,
           st.ring_samples ? (unsigned)(st.ring_samples * 1000u / 16000u) : 0u,
           (unsigned)(st.ring_samples * sizeof(int16_t)), (unsigned)st.high_water);
    printf("  首帧延迟: %u us（目标 <200000 us）\n", (unsigned)st.first_frame_us);
    printf("  累计写入 %u / 读取 %u / 丢弃 %u / 读超时 %u 样本\n",
           (unsigned)st.samples_written, (unsigned)st.samples_read,
           (unsigned)st.samples_dropped, (unsigned)st.read_timeouts);
    printf("  本次窗口: %u ms，320 样本帧 %u 个（%u 样本）\n", (unsigned)window_ms,
           (unsigned)frames, (unsigned)samples);
    print_db_line("窗口电平 RMS", rms);
    print_db_line("窗口峰值", peak);
    if (segs > 0) {
        print_uplink_segments(seg_sq, seg_n, segs);
    }
    if (st.samples_dropped > 0) {
        printf("  注意: 有丢弃 —— 环形缓冲满（消费端读得太慢或未在读）\n");
    }
    if (frames == 0) {
        printf("  注意: 窗口内没读到数据 —— 确认 `afe uplink on` 且麦克风在收音\n");
    }
}

static int cmd_afe(int argc, char **argv)
{
    if (argc < 3 || strcmp(argv[1], "uplink") != 0) {
        printf("用法: afe uplink on | off | stat [ms]\n");
        return ESP_ERR_INVALID_ARG;
    }

    const char *sub = argv[2];
    if (strcmp(sub, "on") == 0) {
        if (vc_is_busy()) {
            printf("vc 轮次进行中，上行接口被 voice_cloud 占用；先 `vc cancel` 或等轮次结束\n");
            return ESP_ERR_INVALID_STATE;
        }
        esp_err_t err = audio_fe_uplink_start();
        if (err != ESP_OK) {
            printf("启动上行失败: %s\n", esp_err_to_name(err));
            return err;
        }
        printf("上行已启动（16k/单声道 PCM → PSRAM 环形缓冲）\n");
        return ESP_OK;
    }
    if (strcmp(sub, "off") == 0) {
        audio_fe_uplink_stop();
        printf("上行已停止（残留数据已丢弃）\n");
        return ESP_OK;
    }
    if (strcmp(sub, "stat") == 0) {
        int ms = (argc > 3) ? atoi(argv[3]) : 1000;
        if (ms < 100) {
            ms = 100;
        } else if (ms > 10000) {
            ms = 10000;
        }
        print_afe_uplink((uint32_t)ms);
        return ESP_OK;
    }

    printf("未知子命令: %s（可用: on | off | stat [ms]）\n", sub);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- enc 命令（Opus 上行编码探针，T2.2） ----------------
 *
 * 开发期探针（同 `afe uplink` / `ws send` 先例）：只做编码观测与帧 dump，
 * 不参与产品链路（产品路径由 T2.5 的 voice_cloud 任务调用 codec_enc）。
 *
 * 为什么"先编码进 PSRAM、循环结束再打 hex"：115200 串口每帧 hex 约 80B，
 * 边编边打会把 console 任务钉在 UART 上（11.5KB/s），上行环形缓冲随即溢出、
 * 编码统计失真；dump 先缓冲到 PSRAM、循环后再统一打印，此时已不读音频，
 * 串口阻塞不影响统计。PC 侧用 tools/opus_enc_test.py（PyAV 独立解码）。
 */
#define ENC_DUMP_FRAMES 50     /* 50 × 20ms = 1s，够 PC 侧做频率/RMS 判定 */
#define ENC_TONE_HZ 1000
#define ENC_TONE_AMPLITUDE 8000 /* ≈ -12 dBFS，不削波 */

static uint8_t *s_enc_dump; /* PSRAM：ENC_DUMP_FRAMES × CODEC_ENC_MAX_PACKET */
static uint16_t s_enc_dump_len[ENC_DUMP_FRAMES];
static uint32_t s_enc_dump_count;
static uint32_t s_enc_dump_missed;
static bool s_enc_dump_on;

static esp_err_t enc_dump_set(bool on)
{
    if (on) {
        if (s_enc_dump == NULL) {
            size_t bytes = (size_t)ENC_DUMP_FRAMES * CODEC_ENC_MAX_PACKET;
            s_enc_dump = heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM);
            if (s_enc_dump == NULL) {
                printf("PSRAM 分配 dump 缓冲失败（%u B）\n", (unsigned)bytes);
                return ESP_ERR_NO_MEM;
            }
        }
        s_enc_dump_on = true;
        printf("ENC dump on（最多 %d 帧，%u B PSRAM）\n", ENC_DUMP_FRAMES,
               (unsigned)((size_t)ENC_DUMP_FRAMES * CODEC_ENC_MAX_PACKET));
    } else {
        s_enc_dump_on = false;
        s_enc_dump_count = 0;
        s_enc_dump_missed = 0;
        if (s_enc_dump != NULL) {
            heap_caps_free(s_enc_dump);
            s_enc_dump = NULL;
        }
        printf("ENC dump off\n");
    }
    return ESP_OK;
}

/* 每次 enc_encode() 最多产出一帧（调用方每次只喂 ≤320 样本），按调用切分即可 */
static void enc_dump_capture(const uint8_t *frame, size_t len)
{
    if (!s_enc_dump_on || s_enc_dump == NULL) {
        return;
    }
    if (s_enc_dump_count >= ENC_DUMP_FRAMES) {
        s_enc_dump_missed++;
        return;
    }
    if (len > CODEC_ENC_MAX_PACKET) {
        len = CODEC_ENC_MAX_PACKET;
    }
    memcpy(s_enc_dump + (size_t)s_enc_dump_count * CODEC_ENC_MAX_PACKET, frame, len);
    s_enc_dump_len[s_enc_dump_count] = (uint16_t)len;
    s_enc_dump_count++;
}

static void enc_dump_print(void)
{
    static const char k_hex[] = "0123456789abcdef";
    static char hex[CODEC_ENC_MAX_PACKET * 2 + 1];

    if (!s_enc_dump_on || s_enc_dump == NULL) {
        return;
    }
    for (uint32_t i = 0; i < s_enc_dump_count; i++) {
        const uint8_t *frame = s_enc_dump + (size_t)i * CODEC_ENC_MAX_PACKET;
        uint32_t len = s_enc_dump_len[i];
        for (uint32_t j = 0; j < len; j++) {
            hex[j * 2] = k_hex[frame[j] >> 4];
            hex[j * 2 + 1] = k_hex[frame[j] & 0x0F];
        }
        hex[len * 2] = '\0';
        printf("ENCDUMP %u %u %s\n", (unsigned)i, (unsigned)len, hex);
    }
    if (s_enc_dump_missed > 0) {
        printf("ENC dump missed: %u 帧（超出缓冲未记录）\n", (unsigned)s_enc_dump_missed);
    }
    printf("ENC dump done: n=%u\n", (unsigned)s_enc_dump_count);
}

static double enc_bitrate_kbps(const codec_enc_stats_t *st)
{
    if (st->samples == 0) {
        return 0.0;
    }
    /* 按音频时长（不是墙钟）算码率：tone 模式是尽可能快地压完固定帧数 */
    return (double)st->bytes * 8.0 * (double)CODEC_ENC_SAMPLE_RATE / (double)st->samples / 1000.0;
}

static void print_enc_stat(void)
{
    codec_enc_stats_t st;

    if (enc_stats_read(&st) != ESP_OK) {
        printf("读取编码统计失败\n");
        return;
    }
    double bpf = st.frames ? (double)st.bytes / (double)st.frames : 0.0;
    uint32_t avg_us = st.frames ? st.encode_us_sum / st.frames : 0;
    printf("ENC stat: open=%d frames=%u samples=%u bytes=%u bytes_per_frame=%.1f "
           "bitrate_kbps=%.1f encode_avg_us=%u encode_max_us=%u max_packet=%u errors=%u carry=%u\n",
           enc_is_open() ? 1 : 0, (unsigned)st.frames, (unsigned)st.samples, (unsigned)st.bytes,
           bpf, enc_bitrate_kbps(&st), (unsigned)avg_us, (unsigned)st.encode_us_max,
           (unsigned)st.max_packet, (unsigned)st.errors, (unsigned)st.carry_samples);
}

static int enc_run_test(uint32_t window_ms, const char *source)
{
    static int16_t pcm[CODEC_ENC_FRAME_SAMPLES];
    uint8_t out[CODEC_ENC_MAX_PACKET];
    const double two_pi = 6.28318530717958647692;
    bool use_uplink;
    uint32_t read_timeouts = 0;
    esp_err_t err;

    if (source == NULL) {
        use_uplink = audio_fe_uplink_active(); /* 不指定音源：上行在跑就用真实链路 */
    } else if (strcmp(source, "uplink") == 0) {
        use_uplink = true;
    } else if (strcmp(source, "tone") == 0) {
        use_uplink = false;
    } else {
        printf("未知音源: %s（可用: tone | uplink）\n", source);
        return ESP_ERR_INVALID_ARG;
    }
    if (use_uplink && !audio_fe_uplink_active()) {
        printf("上行未启动：先 `afe uplink on`，或指定 `enc test %u tone`\n", (unsigned)window_ms);
        return ESP_ERR_INVALID_STATE;
    }

    size_t internal_before = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    size_t psram_before = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);

    err = enc_open();
    if (err != ESP_OK) {
        printf("打开编码器失败: %s\n", esp_err_to_name(err));
        return err;
    }
    enc_stats_reset();
    if (s_enc_dump_on) {
        s_enc_dump_count = 0;
        s_enc_dump_missed = 0;
    }

    if (use_uplink) {
        /* 先排空 start 前积压的音频：ring 有 2s 深度，不排空会把旧数据也编进本次窗口
         * （T2.2 实测 2000ms 窗口编出 108 帧而不是 100 帧）。timeout=0 只取现成数据。 */
        int drained = 0;
        while (audio_fe_uplink_read(pcm, CODEC_ENC_FRAME_SAMPLES, 0) > 0) {
            drained++;
        }
        if (drained > 0) {
            printf("ENC TEST: 已丢弃 %d 帧积压（start 前采集的旧数据）\n", drained);
        }
    }

    printf("ENC TEST start: source=%s window_ms=%u dump=%s\n", use_uplink ? "uplink" : "tone",
           (unsigned)window_ms, s_enc_dump_on ? "on" : "off");

    int64_t t0 = esp_timer_get_time();
    int64_t end_us = t0 + (int64_t)window_ms * 1000;
    uint32_t tone_frames = window_ms / CODEC_ENC_FRAME_MS;
    uint32_t tone_done = 0;
    double phase = 0.0;

    for (;;) {
        size_t n;
        if (use_uplink) {
            int64_t now = esp_timer_get_time();
            if (now >= end_us) {
                break;
            }
            int64_t remain_ms = (end_us - now) / 1000;
            if (remain_ms <= 0) {
                remain_ms = 1;
            } else if (remain_ms > 200) {
                remain_ms = 200;
            }
            int got = audio_fe_uplink_read(pcm, CODEC_ENC_FRAME_SAMPLES, (uint32_t)remain_ms);
            if (got <= 0) {
                read_timeouts++;
                vTaskDelay(pdMS_TO_TICKS(2)); /* 不空转：把 CPU 让给 afe_fetch */
                continue;
            }
            n = (size_t)got;
        } else {
            if (tone_done >= tone_frames) {
                break; /* tone 模式压固定帧数：测编码吞吐，不受墙钟影响 */
            }
            for (int i = 0; i < CODEC_ENC_FRAME_SAMPLES; i++) {
                pcm[i] = (int16_t)lround(sin(phase) * (double)ENC_TONE_AMPLITUDE);
                phase += two_pi * (double)ENC_TONE_HZ / (double)CODEC_ENC_SAMPLE_RATE;
                if (phase >= two_pi) {
                    phase -= two_pi;
                }
            }
            n = CODEC_ENC_FRAME_SAMPLES;
            tone_done++;
        }

        size_t out_len = 0;
        err = enc_encode(pcm, n, out, sizeof(out), &out_len);
        if (err != ESP_OK) {
            printf("编码失败: %s\n", esp_err_to_name(err));
            break;
        }
        if (out_len > 0) {
            enc_dump_capture(out, out_len);
        }
    }

    /* 会话收尾：carry 残余补零成尾帧（T2.5 的 audio_end 用同一口径，不丢最后半帧） */
    size_t tail_len = 0;
    if (enc_flush(out, sizeof(out), &tail_len) == ESP_OK && tail_len > 0) {
        enc_dump_capture(out, tail_len);
    }

    uint32_t elapsed_ms = (uint32_t)((esp_timer_get_time() - t0) / 1000);
    codec_enc_stats_t st;
    enc_stats_read(&st);
    enc_close();

    size_t internal_after = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    size_t psram_after = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);

    enc_dump_print();

    double bpf = st.frames ? (double)st.bytes / (double)st.frames : 0.0;
    double avg_ms = st.frames ? (double)st.encode_us_sum / (double)st.frames / 1000.0 : 0.0;
    printf("ENC TEST done: source=%s frames=%u samples=%u bytes=%u bytes_per_frame=%.1f "
           "bitrate_kbps=%.1f encode_avg_ms=%.2f encode_max_ms=%.2f max_packet=%u errors=%u "
           "carry=%u elapsed_ms=%u internal_free_delta=%d psram_free_delta=%d read_timeouts=%u "
           "stack_free=%u\n",
           use_uplink ? "uplink" : "tone", (unsigned)st.frames, (unsigned)st.samples,
           (unsigned)st.bytes, bpf, enc_bitrate_kbps(&st), avg_ms,
           (double)st.encode_us_max / 1000.0, (unsigned)st.max_packet, (unsigned)st.errors,
           (unsigned)st.carry_samples, (unsigned)elapsed_ms,
           (int)internal_before - (int)internal_after, (int)psram_before - (int)psram_after,
           (unsigned)read_timeouts, (unsigned)uxTaskGetStackHighWaterMark(NULL));
    return err;
}

static int cmd_enc(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: enc test [ms] [tone|uplink] | stat | dump on|off | open | close | reset\n");
        return ESP_ERR_INVALID_ARG;
    }

    const char *sub = argv[1];
    /* 编码器同一时刻只能有一个使用者：vc 轮次进行中时拒绝会动编码器状态的子命令
     * （stat/dump/reset 只读统计，允许） */
    if ((strcmp(sub, "test") == 0 || strcmp(sub, "open") == 0 || strcmp(sub, "close") == 0) &&
        vc_is_busy()) {
        printf("vc 轮次进行中，编码器被 voice_cloud 占用；先 `vc cancel` 或等轮次结束\n");
        return ESP_ERR_INVALID_STATE;
    }
    if (strcmp(sub, "test") == 0) {
        int ms = (argc > 2) ? atoi(argv[2]) : 1000;
        if (ms < 100) {
            ms = 100;
        } else if (ms > 10000) {
            ms = 10000;
        }
        return enc_run_test((uint32_t)ms, (argc > 3) ? argv[3] : NULL);
    }
    if (strcmp(sub, "stat") == 0) {
        print_enc_stat();
        return ESP_OK;
    }
    if (strcmp(sub, "reset") == 0) {
        esp_err_t err = enc_stats_reset();
        printf("ENC stats reset\n");
        return err;
    }
    if (strcmp(sub, "dump") == 0) {
        if (argc < 3) {
            printf("用法: enc dump on|off\n");
            return ESP_ERR_INVALID_ARG;
        }
        if (strcmp(argv[2], "on") == 0) {
            return enc_dump_set(true);
        }
        if (strcmp(argv[2], "off") == 0) {
            return enc_dump_set(false);
        }
        printf("未知参数: %s（可用: on | off）\n", argv[2]);
        return ESP_ERR_INVALID_ARG;
    }
    if (strcmp(sub, "open") == 0) {
        esp_err_t err = enc_open();
        printf("%s\n", (err == ESP_OK) ? "编码器已打开" : esp_err_to_name(err));
        return err;
    }
    if (strcmp(sub, "close") == 0) {
        esp_err_t err = enc_close();
        printf("%s\n", (err == ESP_OK) ? "编码器已关闭" : esp_err_to_name(err));
        return err;
    }

    printf("未知子命令: %s（可用: test | stat | dump | open | close | reset）\n", sub);
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

/* ---------------- ws 命令（WebSocket 观测，T1.4；mock 双向联调探针，T1.6） ---------------- */

/*
 * 为什么要有 send/sendbin/rxlog：P1 还没有 voice_cloud（T2.5），固件里没有任何上行发送方，
 * 只靠自动心跳证明不了"端侧能发、能收"。这三个命令是**开发期探针**：
 *   - 不解析业务 JSON、不发明协议字段，把控制台输入原样交给 transport_ws；
 *   - rxlog 从 T2.5 起走 ws_subscribe() 附加订阅，与 voice_cloud 的主回调**并存**（都看到下行帧）；
 *     关掉 rxlog 不会影响业务回调。
 */

static uint32_t s_ws_rxlog_bin;  /* rxlog 打开后收到的二进制帧计数（只影响打印频率） */
static uint32_t s_ws_rxlog_token; /* 附加订阅令牌；0 表示未打开 */

static void ws_rxlog_cb(const ws_frame_hdr_t *frame, const uint8_t *payload, size_t len, void *arg)
{
    (void)arg;

    /* payload 不保证 '\0' 结尾，用精度限长；文本帧是 JSON，直接打印便于肉眼核对字段 */
    if (frame == NULL) {
        const int show = (int)(len > 200 ? 200 : len);
        ESP_LOGI(TAG, "ws rx text（%u B）: %.*s", (unsigned)len, show, (const char *)payload);
        return;
    }

    /* 二进制帧是 TTS 流（约 20ms 一帧），只打首帧与每 25 帧，避免刷屏掩盖其它日志 */
    s_ws_rxlog_bin++;
    if (s_ws_rxlog_bin == 1 || (s_ws_rxlog_bin % 25) == 0) {
        ESP_LOGI(TAG, "ws rx bin（第 %u 帧）: type=0x%02X seq=%u len=%u", (unsigned)s_ws_rxlog_bin,
                 (unsigned)frame->type, (unsigned)frame->seq, (unsigned)len);
    }
}

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
    printf("  联调: ws send <json> | ws sendbin [len] | ws rxlog on|off\n");
}

/*
 * `ws send` 把剩余参数用空格拼成一条 JSON 原样发送（console 按空白分词，含空格的 JSON 也能拼回）。
 * 长度上限交给 ws_send_text 判（单槽 CONFIG_WS_TX_MSG_BYTES）；未 READY 时返回 INVALID_STATE 属预期。
 */
static int ws_cmd_send(int argc, char **argv)
{
    char buf[256];
    size_t used = 0;

    if (argc < 3) {
        printf("用法: ws send <json>\n"
               "示例: ws send {\"t\":\"audio_start\",\"sid\":1}\n");
        return ESP_ERR_INVALID_ARG;
    }

    for (int i = 2; i < argc; i++) {
        const size_t n = strlen(argv[i]);
        const size_t sep = (i > 2) ? 1 : 0;
        if (used + sep + n + 1 > sizeof(buf)) {
            printf("JSON 过长（> %u B），未发送\n", (unsigned)sizeof(buf) - 1);
            return ESP_ERR_INVALID_SIZE;
        }
        if (sep != 0) {
            buf[used++] = ' ';
        }
        memcpy(buf + used, argv[i], n);
        used += n;
    }
    buf[used] = '\0';

    const esp_err_t err = ws_send_text(buf);
    if (err != ESP_OK) {
        printf("发送失败: %s（未 READY 时属预期，先 ws status 看状态）\n", esp_err_to_name(err));
        return err;
    }
    printf("已入队文本帧 %u B（非阻塞；对端是否收到看 mock 日志）\n", (unsigned)used);
    return ESP_OK;
}

/*
 * `ws sendbin [len]` 发一帧 0x01 上行音频（内容是探针图案，不是真 Opus），
 * 用来验证 8B 帧头与 seq 自增；payload 上限取单槽 256B - 8B 头 = 248B。
 */
static int ws_cmd_sendbin(int argc, char **argv)
{
    static uint8_t probe[248]; /* 静态缓冲：不放大 console 任务栈 */
    size_t len = 40;

    if (argc >= 3) {
        char *end = NULL;
        errno = 0;
        const unsigned long value = strtoul(argv[2], &end, 0);
        if (errno != 0 || end == argv[2] || *end != '\0' || value == 0 ||
            value > sizeof(probe)) {
            printf("非法长度: %s（范围 1~%u）\n", argv[2], (unsigned)sizeof(probe));
            return ESP_ERR_INVALID_ARG;
        }
        len = (size_t)value;
    }

    for (size_t i = 0; i < len; i++) {
        probe[i] = (uint8_t)(0xA0u + (i & 0x0Fu));
    }

    const esp_err_t err = ws_send_bin(WS_FRAME_TYPE_AUDIO_UP, probe, len);
    if (err != ESP_OK) {
        printf("发送失败: %s（未 READY 或长度超过单槽上限）\n", esp_err_to_name(err));
        return err;
    }
    printf("已入队上行二进制帧 %u B（帧头 8B 由 transport_ws 填，seq 自增）\n", (unsigned)len);
    return ESP_OK;
}

static int ws_cmd_rxlog(int argc, char **argv)
{
    bool on;

    if (argc != 3) {
        printf("用法: ws rxlog on|off\n");
        return ESP_ERR_INVALID_ARG;
    }
    if (strcmp(argv[2], "on") == 0) {
        on = true;
    } else if (strcmp(argv[2], "off") == 0) {
        on = false;
    } else {
        printf("未知参数: %s（可用: on | off）\n", argv[2]);
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t err;
    if (on) {
        if (s_ws_rxlog_token != 0) {
            printf("下行帧打印已开启（令牌 %" PRIu32 "），无需重复订阅\n", s_ws_rxlog_token);
            return ESP_OK;
        }
        uint32_t token = 0;
        err = ws_subscribe(ws_rxlog_cb, NULL, &token);
        if (err != ESP_OK) {
            printf("订阅失败: %s%s\n", esp_err_to_name(err),
                   err == ESP_ERR_NO_MEM ? "（附加订阅槽位已满）" : "（ws 未初始化）");
            return err;
        }
        s_ws_rxlog_token = token;
    } else {
        if (s_ws_rxlog_token != 0) {
            err = ws_unsubscribe(s_ws_rxlog_token);
            if (err != ESP_OK && err != ESP_ERR_NOT_FOUND) {
                printf("取消订阅失败: %s\n", esp_err_to_name(err));
                return err;
            }
            s_ws_rxlog_token = 0;
        }
    }
    s_ws_rxlog_bin = 0;
    printf("下行帧打印: %s（文本帧全量≤200B，二进制只打首帧与每 25 帧；与 voice_cloud 回调并存）\n",
           on ? "开" : "关");
    return ESP_OK;
}

static int cmd_ws(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: ws status | reconnect | send <json> | sendbin [len] | rxlog on|off\n");
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

    if (strcmp(argv[1], "send") == 0) {
        return ws_cmd_send(argc, argv);
    }

    if (strcmp(argv[1], "sendbin") == 0) {
        return ws_cmd_sendbin(argc, argv);
    }

    if (strcmp(argv[1], "rxlog") == 0) {
        return ws_cmd_rxlog(argc, argv);
    }

    printf("未知子命令: %s（可用: status | reconnect | send | sendbin | rxlog）\n", argv[1]);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- vc 命令（voice_cloud 观测与手动起轮，T2.5） ---------------- */

static void print_vc_status(void)
{
    vc_stats_t st;
    ws_status_t wst;

    printf("voice_cloud: %s（当前 sid=%u）%s\n", vc_state_name(vc_get_state()),
           (unsigned)vc_current_sid(), vc_is_busy() ? "  [busy]" : "");
    printf("  WS: %s\n", ws_is_ready() ? "READY（可发起轮次）" : "未 READY（vc start 会被拒绝）");
    if (ws_status_read(&wst) == ESP_OK && wst.url_configured) {
        printf("  端点: %s（%s）\n", wst.endpoint, wst.tls ? "wss/TLS" : "ws 明文");
    }
    if (vc_stats_read(&st) != ESP_OK) {
        printf("  读取统计失败\n");
        return;
    }

    const double bytes_per_frame =
        st.up_frames > 0 ? (double)st.up_bytes / (double)st.up_frames : 0.0;
    printf("  轮次: 发起 %" PRIu32 " / 完成 %" PRIu32 " / 中止 %" PRIu32
           "；最近 sid=%u 用时 %" PRIu32 " ms（收尾=%s）\n",
           st.rounds_started, st.rounds_done, st.rounds_aborted, (unsigned)st.last_sid,
           st.last_round_ms, st.last_sid == 0 ? "—" : (st.last_round_vad_end ? "VAD 静音" : "时长上限"));
    printf("  上行: %" PRIu32 " 帧 / %" PRIu32 " 字节（%.1f B/帧）；编码失败 %" PRIu32
           " 入队失败 %" PRIu32 " 读超时 %" PRIu32 "；首帧 %" PRIu32 " us\n",
           st.up_frames, st.up_bytes, bytes_per_frame, st.up_encode_errors, st.up_send_errors,
           st.up_read_timeouts, st.first_frame_us);
    printf("  下行: 文本 %" PRIu32 " / 音频帧 %" PRIu32 "；sid 丢弃 %" PRIu32 " 解析失败 %" PRIu32
           " 云端 error %" PRIu32 "；音频丢弃 %" PRIu32 " 帧 字节 %" PRIu32 "\n",
           st.rx_text, st.rx_audio_frames, st.rx_dropped_sid, st.rx_parse_errors,
           st.rx_cloud_errors, st.rx_audio_dropped, st.rx_audio_dropped_bytes);
    printf("  说明: 唤醒后由 voice_session 自动起一轮；手动调试用 vc start [sid] / stop / cancel\n");
}

static int cmd_vc(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: vc status | start [sid] | stop | cancel | stat | reset\n");
        return ESP_ERR_INVALID_ARG;
    }

    const char *sub = argv[1];
    if (strcmp(sub, "status") == 0 || strcmp(sub, "stat") == 0) {
        print_vc_status();
        return ESP_OK;
    }

    if (strcmp(sub, "start") == 0) {
        esp_err_t err;
        if (argc >= 3) {
            char *end = NULL;
            errno = 0;
            const unsigned long value = strtoul(argv[2], &end, 10);
            if (errno != 0 || end == argv[2] || *end != '\0' || value > 0xFFFFUL) {
                printf("非法 sid: %s（范围 0~65535）\n", argv[2]);
                return ESP_ERR_INVALID_ARG;
            }
            err = vc_start((uint16_t)value);
        } else {
            err = vc_start_auto();
        }
        if (err != ESP_OK) {
            printf("发起失败: %s（云端未 READY 时属预期，先 ws status 看状态）\n",
                   esp_err_to_name(err));
            return err;
        }
        printf("已请求发起一轮（sid=%s），2s 后用 vc status 看上行帧数\n",
               argc >= 3 ? argv[2] : "auto");
        return ESP_OK;
    }

    if (strcmp(sub, "stop") == 0) {
        esp_err_t err = vc_stop();
        printf("%s\n", err == ESP_OK ? "已请求优雅收尾（补尾帧 + audio_end）" :
                                      esp_err_to_name(err));
        return err;
    }

    if (strcmp(sub, "cancel") == 0) {
        esp_err_t err = vc_cancel();
        printf("%s\n", err == ESP_OK ? "已请求立即放弃当前轮次" : esp_err_to_name(err));
        return err;
    }

    if (strcmp(sub, "reset") == 0) {
        esp_err_t err = vc_stats_reset();
        printf("vc 统计已清零（保留最近 sid/用时）\n");
        return err;
    }

    printf("未知子命令: %s（可用: status | start | stop | cancel | stat | reset）\n", sub);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- vs 命令（会话状态机观测，T3.1） ---------------- */

static void print_vs_status(void)
{
    vs_stats_t st;

    if (vs_stats_read(&st) != ESP_OK) {
        printf("读取 voice_session 统计失败\n");
        return;
    }
    printf("voice_session: %s%s（当前 sid=%u，VC=%s）\n", vs_state_name((vs_state_t)st.state),
           vs_is_busy() ? "  [busy]" : "", (unsigned)vs_current_sid(),
           vc_state_name(vc_get_state()));
    printf("  轮次: 开始 %" PRIu32 " / 完成 %" PRIu32 " / 异常 %" PRIu32 " / 中止 %" PRIu32
           "；打断 %" PRIu32 " 唤醒丢弃 %" PRIu32 "；超时 %" PRIu32 " 云端 error %" PRIu32 "\n",
           st.rounds_started, st.rounds_completed, st.rounds_errored, st.round_aborts,
           st.interrupts, st.wakes_dropped, st.timeouts, st.cloud_errors);
    printf("  迁移: 共 %" PRIu32 " 次；最近 sid=%u，上次状态停留 %" PRIu32 " ms\n",
           st.transitions, (unsigned)st.last_sid, st.last_state_ms);
#if CONFIG_VS_FOLLOWUP_ENABLE
    printf("  续听: 续听轮 %" PRIu32 " / 命中 %" PRIu32 "；超时退出 %" PRIu32
           " / 上限退出 %" PRIu32 "\n",
           st.followup_rounds, st.followup_hits, st.followup_timeouts, st.followup_max_exits);
    if ((vs_state_t)st.state == VS_STATE_FOLLOW_UP) {
        printf("        窗口剩余 %" PRIu32 " ms（窗口 %d ms / 上限 %d 轮）\n",
               st.followup_remaining_ms, CONFIG_VS_FOLLOWUP_WINDOW_MS,
               CONFIG_VS_FOLLOWUP_MAX_ROUNDS);
    }
#else
    printf("  续听: 关闭（CONFIG_VS_FOLLOWUP_ENABLE=n）\n");
#endif
    printf("  错误反馈: 网络 %" PRIu32 " / 云端 %" PRIu32 " / 超时 %" PRIu32 " / TTS %" PRIu32
           "；提示音完成 %" PRIu32 " 失败 %" PRIu32 "；最近 kind=%s code=%" PRId32
           " 时长 %" PRIu32 " ms\n",
           st.error_by_kind[1], st.error_by_kind[2], st.error_by_kind[3], st.error_by_kind[4],
           st.error_feedback_ok, st.error_feedback_fail,
           fb_kind_name((fb_kind_t)st.last_error_kind), st.last_error_code,
           st.last_error_prompt_ms);
    printf("  说明: 唤醒自动进 LISTENING；TTS 播完进 FOLLOW_UP 续听窗口；"
           "手动调试用 vs start / vs interrupt\n");
}

static int cmd_vs(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: vs status | start | interrupt | reset | followup on|off\n");
        return ESP_ERR_INVALID_ARG;
    }

    const char *sub = argv[1];
    if (strcmp(sub, "status") == 0 || strcmp(sub, "stat") == 0) {
        print_vs_status();
        return ESP_OK;
    }

    if (strcmp(sub, "start") == 0) {
        esp_err_t err = vs_start();
        if (err != ESP_OK) {
            printf("开始会话失败: %s（已有会话进行中或云端未 READY）\n", esp_err_to_name(err));
            return err;
        }
        printf("已请求开始一轮（不播唤醒应答，直接进入 LISTENING）\n");
        return ESP_OK;
    }

    if (strcmp(sub, "interrupt") == 0) {
        esp_err_t err = vs_interrupt();
        printf("%s\n", err == ESP_OK ?
                           "已打断当前会话（淡出 + interrupt 报文 + R5 丢弃 50ms + 回 IDLE）" :
                           esp_err_to_name(err));
        return err;
    }

    if (strcmp(sub, "reset") == 0) {
        esp_err_t err = vs_stats_reset();
        printf("vs 统计已清零（保留最近 sid/停留时长）\n");
        return err;
    }

    if (strcmp(sub, "followup") == 0) {
        if (argc < 3 || (strcmp(argv[2], "on") != 0 && strcmp(argv[2], "off") != 0)) {
            printf("用法: vs followup on|off（连续对话续听窗口运行时开关）\n");
            return ESP_ERR_INVALID_ARG;
        }
        const bool on = (strcmp(argv[2], "on") == 0);
        esp_err_t err = vs_set_followup_enabled(on);
        if (err != ESP_OK) {
            printf("切换失败: %s（CONFIG_VS_FOLLOWUP_ENABLE=n 时不可用）\n",
                   esp_err_to_name(err));
            return err;
        }
#if CONFIG_VS_FOLLOWUP_ENABLE
        printf("连续对话续听窗口：%s（窗口 %d ms / 上限 %d 轮 / 人声确认 %d ms）\n",
               on ? "开" : "关", CONFIG_VS_FOLLOWUP_WINDOW_MS, CONFIG_VS_FOLLOWUP_MAX_ROUNDS,
               CONFIG_VS_FOLLOWUP_CONFIRM_MS);
#else
        printf("连续对话续听窗口：CONFIG 未启用\n");
#endif
        return ESP_OK;
    }

    printf("未知子命令: %s（可用: status | start | interrupt | reset | followup on|off）\n", sub);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- fb 命令（异常反馈观测/手动试听，T3.6） ---------------- */

static void print_fb_status(void)
{
    fb_stats_t st;

    if (fb_stats_read(&st) != ESP_OK) {
        printf("读取 feedback 统计失败\n");
        return;
    }
    printf("feedback: 活动=%s%s\n",
           st.active_kind == FB_KIND_NONE ? "空闲" : fb_kind_name((fb_kind_t)st.active_kind),
           st.pending ? "（等 TTS 流结束补播）" : "");
    printf("  通道统计: 总播放 %" PRIu32 " / 完成 %" PRIu32 " / 失败 %" PRIu32
           "；延迟 %" PRIu32 " 丢弃 %" PRIu32 " 中止 %" PRIu32 "\n",
           st.plays, st.done_ok, st.done_fail, st.deferred, st.rejected, st.aborts);
    printf("  分类: 网络 %" PRIu32 " / 云端 %" PRIu32 " / 超时 %" PRIu32 " / TTS %" PRIu32
           "；最近时长 %" PRIu32 " ms\n",
           st.by_kind[FB_KIND_NET_OFFLINE], st.by_kind[FB_KIND_CLOUD_ERROR],
           st.by_kind[FB_KIND_TIMEOUT], st.by_kind[FB_KIND_TTS_FAIL], st.last_dur_ms);
    printf("  说明: 语音只是当前反馈通道；APP_EVT_FEEDBACK 事件供未来屏幕/灯光通道订阅（T3.6）\n");
}

static bool fb_kind_from_arg(const char *arg, fb_kind_t *out)
{
    if (strcmp(arg, "net") == 0 || strcmp(arg, "net-offline") == 0) {
        *out = FB_KIND_NET_OFFLINE;
    } else if (strcmp(arg, "cloud") == 0 || strcmp(arg, "cloud-error") == 0) {
        *out = FB_KIND_CLOUD_ERROR;
    } else if (strcmp(arg, "timeout") == 0) {
        *out = FB_KIND_TIMEOUT;
    } else if (strcmp(arg, "tts") == 0 || strcmp(arg, "tts-fail") == 0) {
        *out = FB_KIND_TTS_FAIL;
    } else {
        return false;
    }
    return true;
}

static int cmd_fb(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: fb status | reset | test <net|cloud|timeout|tts>\n");
        return ESP_ERR_INVALID_ARG;
    }

    const char *sub = argv[1];
    if (strcmp(sub, "status") == 0 || strcmp(sub, "stat") == 0) {
        print_fb_status();
        return ESP_OK;
    }
    if (strcmp(sub, "reset") == 0) {
        fb_stats_reset();
        printf("feedback 统计已清零\n");
        return ESP_OK;
    }
    if (strcmp(sub, "test") == 0) {
        if (argc < 3) {
            printf("用法: fb test <net|cloud|timeout|tts>（手动试听提示音，不起会话）\n");
            return ESP_ERR_INVALID_ARG;
        }
        fb_kind_t kind;
        if (!fb_kind_from_arg(argv[2], &kind)) {
            printf("未知反馈类别: %s（可用: net | cloud | timeout | tts）\n", argv[2]);
            return ESP_ERR_INVALID_ARG;
        }
        esp_err_t err = fb_notify(kind, 0, vs_current_sid());
        if (err != ESP_OK) {
            printf("反馈测试失败: %s（可能正在播另一条提示音）\n", esp_err_to_name(err));
            return err;
        }
        printf("已请求反馈测试：%s（提示音播放期间 R1 暂停收音）\n", fb_kind_name(kind));
        return ESP_OK;
    }

    printf("未知子命令: %s（可用: status | reset | test <net|cloud|timeout|tts>）\n", sub);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- prompt 命令（提示音资源观测，T3.5） ---------------- */

static const char *prompt_mode_name(uint8_t mode)
{
    switch (mode) {
    case PROMPT_STORE_MODE_MMAP: return "flash-mmap";
    case PROMPT_STORE_MODE_PSRAM: return "psram-fallback";
    default: return "none";
    }
}

static int cmd_prompt(int argc, char **argv)
{
    if (argc < 2 || strcmp(argv[1], "info") != 0) {
        printf("用法: prompt info（assets 分区提示音 pack 状态）\n");
        return ESP_ERR_INVALID_ARG;
    }

    prompt_store_info_t info;
    esp_err_t err = prompt_store_info(&info);
    if (err != ESP_OK) {
        printf("提示音资源未初始化: %s（prompt_store_init 是否在 app_main 调用？）\n",
               esp_err_to_name(err));
        return ESP_OK;
    }
    if (!info.ready) {
        printf("提示音资源不可用：%s\n", info.error);
        printf("  业务已降级：唤醒跳过应答提示音直接起轮；错误反馈只记日志\n");
        return ESP_OK;
    }

    printf("提示音资源: %s（pack v%u，%u 条，%u B，加载 %u us，base=%p）\n",
           prompt_mode_name(info.mode), (unsigned)info.version, (unsigned)info.count,
           (unsigned)info.total_len, (unsigned)info.load_us, info.base);
    for (unsigned i = 0; i < (unsigned)PROMPT_COUNT; i++) {
        const prompt_clip_t *clip = prompt_assets_get((prompt_id_t)i);
        if (clip == NULL) {
            printf("  [%u] %-11s <缺失>\n", i, prompt_assets_name((prompt_id_t)i));
            continue;
        }
        const uint64_t dur_us = (uint64_t)clip->samples * 1000000u / clip->sample_rate;
        printf("  [%u] %-11s %u 样本 / %u.%03u ms @ %u Hz，pcm=%p\n", i,
               prompt_assets_name((prompt_id_t)i), (unsigned)clip->samples,
               (unsigned)(dur_us / 1000u), (unsigned)(dur_us % 1000u),
               (unsigned)clip->sample_rate, clip->pcm);
    }
    printf("  说明: 资源在 assets 分区（0x7a0000）；单独烧该分区即可替换提示音（app 不动）\n");
    return ESP_OK;
}

/* ---------------- dev 命令（按键/静音观测，T3.4） ---------------- */

static void print_dev_status(void)
{
    dev_stats_t st;

    if (dev_stats_read(&st) != ESP_OK) {
        printf("读取 dev_ctrl 统计失败\n");
        return;
    }
    printf("dev_ctrl: 静音=%s（当前音量 %u）\n", dev_is_muted() ? "开" : "关",
           (unsigned)audio_player_get_volume());
    printf("  按键: 短按 %" PRIu32 " / 长按 %" PRIu32 "；事件投递失败 %" PRIu32 "\n",
           st.short_presses, st.long_presses, st.posts_dropped);
#if CONFIG_DEV_ENABLE
    printf("  参数: 去抖 %d ms / 长按 %d ms（GPIO%d 按键）\n",
           CONFIG_DEV_BUTTON_DEBOUNCE_MS, CONFIG_DEV_BUTTON_LONG_PRESS_MS, 0);
#else
    printf("  CONFIG_DEV_ENABLE=n（功能编译期裁剪）\n");
#endif
    printf("  LED: 已移除（板载 WS2812 解码不可靠+满白电流触发 BOD，见 PENDING PD-003；"
           "GPIO48 保持低电平）\n");
    printf("  说明: 短按投 APP_EVT_BUTTON 由 vs 消费（会话中=打断、IDLE=起轮）；长按=静音切换；"
           "`evt post BUTTON` 仍可模拟短按\n");
}

static int cmd_dev(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: dev status | mute [on|off] | reset\n");
        return ESP_ERR_INVALID_ARG;
    }

    const char *sub = argv[1];
    if (strcmp(sub, "status") == 0 || strcmp(sub, "stat") == 0) {
        print_dev_status();
        return ESP_OK;
    }

    if (strcmp(sub, "mute") == 0) {
        bool target = !dev_is_muted(); /* 无参数 = 切换 */
        if (argc >= 3) {
            if (strcmp(argv[2], "on") == 0) {
                target = true;
            } else if (strcmp(argv[2], "off") == 0) {
                target = false;
            } else {
                printf("用法: dev mute [on|off]\n");
                return ESP_ERR_INVALID_ARG;
            }
        }
        esp_err_t err = dev_set_mute(target);
        if (err != ESP_OK) {
            printf("静音切换失败: %s\n", esp_err_to_name(err));
            return err;
        }
        printf("静音：%s（音量 %u）\n", target ? "开" : "关",
               (unsigned)audio_player_get_volume());
        return ESP_OK;
    }

    if (strcmp(sub, "reset") == 0) {
        esp_err_t err = dev_stats_reset();
        printf("dev 统计已清零\n");
        return err;
    }

    printf("未知子命令: %s（可用: status | mute [on|off] | reset；LED 已移除）\n", sub);
    return ESP_ERR_INVALID_ARG;
}

/* ---------------- evt 命令（事件总线观测，T1.2a） ---------------- */

/*
 * 本命令只做"投递/订阅"的观测口，不模拟业务：post 用的是示例 payload，
 * trace 打开后能看到事件是否真的在 evt 任务上下文被分发到（而不是只打印了投递）。
 */

static bool evt_has_payload(app_evt_t evt)
{
    /* 带 payload 的事件：P1 的网络事件 + T2.5 的语音事件 + T3.1 的会话状态事件（见 event_bus.h） */
    return evt == APP_EVT_NET_UP || evt == APP_EVT_NET_DOWN || evt == APP_EVT_ASR_PARTIAL ||
           evt == APP_EVT_ASR_FINAL || evt == APP_EVT_TTS_START || evt == APP_EVT_TTS_END ||
           evt == APP_EVT_VOICE_STATE_CHANGED || evt == APP_EVT_BUTTON;
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
    } else if (evt == APP_EVT_ASR_PARTIAL || evt == APP_EVT_ASR_FINAL) {
        const char *sample = "示例识别文本";
        data->asr.sid = 1;
        data->asr.len = (uint16_t)strlen(sample);
        memcpy(data->asr.text, sample, data->asr.len);
    } else if (evt == APP_EVT_TTS_START) {
        data->tts_start.sid = 1;
        data->tts_start.codec = APP_EVT_CODEC_MP3;
        data->tts_start.sr = 24000;
    } else if (evt == APP_EVT_TTS_END) {
        data->tts_end.sid = 1;
        data->tts_end.frames = 100;
    } else if (evt == APP_EVT_BUTTON) {
        data->button.action = APP_EVT_BUTTON_SHORT; /* 示例：短按 */
    } else if (evt == APP_EVT_VOICE_STATE_CHANGED) {
        data->voice_state.sid = 1;
        data->voice_state.from = VS_STATE_LISTENING;
        data->voice_state.to = VS_STATE_THINKING;
        data->voice_state.reason = VS_REASON_UPLINK_END;
        data->voice_state.dur_ms = 1200;
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
    } else if ((evt == APP_EVT_ASR_PARTIAL || evt == APP_EVT_ASR_FINAL) && data != NULL) {
        ESP_LOGI(TAG, "evt trace: %s sid=%u len=%u text=%.*s", app_evt_name(evt),
                 (unsigned)data->asr.sid, (unsigned)data->asr.len,
                 (int)(data->asr.len > APP_EVT_TEXT_MAX ? APP_EVT_TEXT_MAX : data->asr.len),
                 data->asr.text);
    } else if (evt == APP_EVT_TTS_START && data != NULL) {
        ESP_LOGI(TAG, "evt trace: %s sid=%u codec=%u sr=%" PRIu32, app_evt_name(evt),
                 (unsigned)data->tts_start.sid, (unsigned)data->tts_start.codec,
                 data->tts_start.sr);
    } else if (evt == APP_EVT_TTS_END && data != NULL) {
        ESP_LOGI(TAG, "evt trace: %s sid=%u frames=%u", app_evt_name(evt),
                 (unsigned)data->tts_end.sid, (unsigned)data->tts_end.frames);
    } else if (evt == APP_EVT_BUTTON && data != NULL) {
        ESP_LOGI(TAG, "evt trace: %s action=%u", app_evt_name(evt), (unsigned)data->button.action);
    } else if (evt == APP_EVT_VOICE_STATE_CHANGED && data != NULL) {
        ESP_LOGI(TAG, "evt trace: %s sid=%u %s -> %s（原因=%s，停留 %" PRIu32 " ms）",
                 app_evt_name(evt), (unsigned)data->voice_state.sid,
                 vs_state_name((vs_state_t)data->voice_state.from),
                 vs_state_name((vs_state_t)data->voice_state.to),
                 vs_reason_name((vs_reason_t)data->voice_state.reason),
                 data->voice_state.dur_ms);
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

/* ---------------- MP3 播放探针（T2.3，仅开发期） ----------------
 *
 * 为什么走 console 上传而不是让设备自己拉流：T2.3 只验证"解码 + 播放"，
 * 素材由 PC 侧准备（含真实电台码流），链路可重复、不依赖网络与 mock；
 * 设备侧 HTTP 拉流属于 T4.1 source_http，T2.4 的流式入口走 WS 下行。
 */

#if CONFIG_DIAG_ENABLE_PLAYER_TEST

static uint8_t *s_pload_buf = NULL; /* PSRAM 上传缓冲 */
static size_t s_pload_len = 0;

static int b64_val(char c)
{
    if (c >= 'A' && c <= 'Z') {
        return c - 'A';
    }
    if (c >= 'a' && c <= 'z') {
        return c - 'a' + 26;
    }
    if (c >= '0' && c <= '9') {
        return c - '0' + 52;
    }
    if (c == '+') {
        return 62;
    }
    if (c == '/') {
        return 63;
    }
    return -1;
}

/* 标准 base64（容忍 '=' 结束与空白）；返回解码字节数，负值 = 非法输入/超出容量 */
static int b64_decode(const char *in, uint8_t *out, size_t cap)
{
    uint32_t acc = 0;
    int bits = 0;
    size_t n = 0;

    for (const char *p = in; *p; p++) {
        if (*p == '=' || *p == ' ' || *p == '\r' || *p == '\n') {
            continue;
        }
        int v = b64_val(*p);
        if (v < 0) {
            return -1;
        }
        acc = (acc << 6) | (uint32_t)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (n >= cap) {
                return -1;
            }
            out[n++] = (uint8_t)(acc >> bits);
        }
    }
    return (int)n;
}

static void print_player_stat(void)
{
    audio_player_stats_t st;
    if (audio_player_stats_read(&st) != ESP_OK) {
        printf("读取播放统计失败\n");
        return;
    }
    printf("PLAYERSTAT jobs=%u pcm=%u mp3=%u errors=%u busy=%u vol=%u last_play_ms=%u mp3_ready=%d\n",
           (unsigned)st.jobs_done, (unsigned)st.pcm_jobs, (unsigned)st.mp3_jobs,
           (unsigned)st.mp3_errors, audio_player_is_busy() ? 1 : 0,
           (unsigned)audio_player_get_volume(), (unsigned)st.last_play_ms,
           audio_player_mp3_ready() ? 1 : 0);
    printf("MP3STAT frames=%u samples=%u rate=%u ch=%u dur_ms=%u skipped=%u bad=%u "
           "dec_avg_us=%u dec_max_us=%u\n",
           (unsigned)st.mp3.frames, (unsigned)st.mp3.samples, (unsigned)st.mp3.rate,
           (unsigned)st.mp3.channels, (unsigned)st.mp3.duration_ms,
           (unsigned)st.mp3.skipped_bytes, (unsigned)st.mp3.bad_frames,
           (unsigned)st.mp3.decode_us_avg, (unsigned)st.mp3.decode_us_max);
    /* T2.4 流式统计行：格式是 tools 与 voice_cloud 的解析契约（逐字对齐契约 §4） */
    audio_player_stream_stats_t ss;
    if (audio_player_stream_stats_read(&ss) == ESP_OK) {
        printf("stream: active=%u fed=%u bytes=%u decoded=%u samples=%u rate=%u ch=%u "
               "ring_ms=%u high_ms=%u underrun=%u dropped=%u bad=%u first_sound_us=%u dur_ms=%u\n",
               ss.active ? 1U : 0U, (unsigned)ss.fed_frames, (unsigned)ss.fed_bytes,
               (unsigned)ss.decoded_frames, (unsigned)ss.decoded_samples, (unsigned)ss.rate,
               (unsigned)ss.channels, (unsigned)ss.ring_level_ms, (unsigned)ss.ring_high_ms,
               (unsigned)ss.underruns, (unsigned)ss.dropped_bytes, (unsigned)ss.bad_frames,
               (unsigned)ss.first_byte_to_sound_us, (unsigned)ss.duration_ms);
    }
    /* T3.2 半双工观测行（R3 duck / R4 淡出停声）：工具脚本按子串匹配，字段只追加不删 */
    printf("duck: on=%d gain_q8=%" PRIu32 " out_peak=%" PRIu32
           " stop: last_stop_ms=%" PRIu32 " stops=%" PRIu32 "\n",
           st.duck_on ? 1 : 0, st.duck_gain_q8, st.out_peak, st.last_stop_ms, st.stop_count);
}

/* T3.3 三通道混音探针：player mix stat 的输出格式是 tools/mixer_test.py 的解析契约 */
static void print_mix_stat(void)
{
    audio_player_mix_stats_t st;
    if (audio_player_mix_stats_read(&st) != ESP_OK) {
        printf("读取混音统计失败\n");
        return;
    }
    uint32_t cap_pos = 0;
    uint32_t cap_len = 0;
    if (audio_player_mix_capture_status(&cap_pos, &cap_len) != ESP_OK) {
        cap_len = 0;
    }
    printf("MIXSTAT vol=%u vol_q8=%u vol_target=%u limiter=%u limiter_peak=%u clip=%u "
           "mix_peak=%u block_max_us=%u cap_len=%u cap_pos=%u\n",
           (unsigned)st.vol, (unsigned)st.vol_q8, (unsigned)st.vol_target_q8,
           (unsigned)st.limiter, (unsigned)st.limiter_peak, (unsigned)st.clip,
           (unsigned)st.mix_peak, (unsigned)st.block_max_us, (unsigned)cap_len,
           (unsigned)cap_pos);
    static const char *names[AUDIO_PLAYER_CH_NUM] = {"TTS", "PROMPT", "MUSIC"};
    for (int i = 0; i < AUDIO_PLAYER_CH_NUM; i++) {
        const audio_player_ch_stats_t *c = &st.ch[i];
        printf("MIXCH ch=%s active=%u gain_q8=%u level_ms=%u peak=%u\n", names[i],
               c->active ? 1U : 0U, (unsigned)c->gain_q8, (unsigned)c->level_ms,
               (unsigned)c->peak);
    }
}

static int mix_ch_from_name(const char *name)
{
    if (strcmp(name, "TTS") == 0) {
        return AUDIO_PLAYER_CH_TTS;
    }
    if (strcmp(name, "PROMPT") == 0) {
        return AUDIO_PLAYER_CH_PROMPT;
    }
    if (strcmp(name, "MUSIC") == 0) {
        return AUDIO_PLAYER_CH_MUSIC;
    }
    return -1;
}

/* T4.1：音乐远端 URL 探针（URL 来自 mock/服务端 music_track；设备只校验前缀/长度） */
static int cmd_music(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: music play <http(s)://...> | stop | stat\n");
        return ESP_ERR_INVALID_ARG;
    }
    if (strcmp(argv[1], "play") == 0) {
        if (argc < 3) {
            printf("用法: music play <http(s)://...>\n");
            return ESP_ERR_INVALID_ARG;
        }
        const esp_err_t err = audio_player_music_play_url(argv[2]);
        printf("MUSICPLAY err=%s\n", esp_err_to_name(err));
        return err;
    }
    if (strcmp(argv[1], "stop") == 0) {
        const esp_err_t err = audio_player_music_stop_url();
        printf("MUSICSTOP err=%s\n", esp_err_to_name(err));
        return (err == ESP_ERR_INVALID_STATE) ? ESP_OK : err;
    }
    if (strcmp(argv[1], "stat") == 0) {
        audio_player_music_stats_t st;
        const esp_err_t err = audio_player_music_stats_read(&st);
        if (err != ESP_OK) {
            printf("MUSICSTAT err=%s\n", esp_err_to_name(err));
            return err;
        }
        printf("MUSICSTAT active=%d url_active=%d rate=%u ring=%ums written=%u played=%u "
               "underruns=%u first_sound=%ums\n",
               st.active ? 1 : 0, st.url_active ? 1 : 0, (unsigned)st.sample_rate,
               (unsigned)st.ring_ms, (unsigned)st.written_frames, (unsigned)st.played_frames,
               (unsigned)st.underruns, (unsigned)st.first_sound_ms);
        printf("MUSICHTTP status=%d content_length=%d bytes=%u first_byte=%ums connect=%ums "
               "decoded=%u errors=%u\n",
               st.http_status, (int)st.content_length, (unsigned)st.http_bytes,
               (unsigned)st.first_byte_ms, (unsigned)st.connect_ms,
               (unsigned)st.decoded_frames, (unsigned)st.http_errors);
        return ESP_OK;
    }
    printf("用法: music play <http(s)://...> | stop | stat\n");
    return ESP_ERR_INVALID_ARG;
}

/* T4.2/T4.3：音乐协议观测（music_cloud 纯协议：mid 基准 + 收发统计） */
static int cmd_mc(int argc, char **argv)
{
    if (argc < 2 || strcmp(argv[1], "status") == 0) {
        mc_stats_t st;
        if (mc_stats_read(&st) != ESP_OK) {
            printf("MC 不可用（music_cloud 未初始化）\n");
            return ESP_ERR_INVALID_STATE;
        }
        printf("MC mid=%u have_mid=%d（协议层：mid 过滤基准）\n", (unsigned)mc_current_mid(),
               mc_have_mid() ? 1 : 0);
        printf("MCSTAT track=%" PRIu32 " control=%" PRIu32 " dropped_mid=%" PRIu32
               " dup=%" PRIu32 " bad=%" PRIu32 " parse_err=%" PRIu32 " status_sent=%" PRIu32
               " status_failed=%" PRIu32 " request_sent=%" PRIu32 " request_failed=%" PRIu32
               "\n",
               st.rx_track, st.rx_control, st.rx_dropped_mid, st.rx_dropped_dup,
               st.rx_dropped_bad, st.rx_parse_errors, st.status_sent, st.status_failed,
               st.request_sent, st.request_failed);
        return ESP_OK;
    }
    if (strcmp(argv[1], "report") == 0) {
        /* 协议层不再持有播放状态：从 music_session 取当前状态/进度上报一次 */
        mc_state_t state = MC_STATE_IDLE;
        uint32_t progress_ms = 0;
        ms_info_t info;
        if (ms_info_read(&info) == ESP_OK) {
            state = info.state;
            progress_ms = info.progress_ms;
        }
        const esp_err_t err = mc_report_status(state, progress_ms);
        printf("MCREPORT err=%s state=%s progress=%ums\n", esp_err_to_name(err),
               mc_state_name(state), (unsigned)progress_ms);
        return err;
    }
    if (strcmp(argv[1], "reset") == 0) {
        (void)mc_stats_reset();
        printf("MCSTAT cleared\n");
        return ESP_OK;
    }
    printf("用法: mc status | report | reset\n");
    return ESP_ERR_INVALID_ARG;
}

/* T4.3：音乐编排（music_session；状态机/暂停继续/抢占/兜底） */
static int cmd_ms(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: ms status | play <http(s)://...> | pause | resume | stop | "
               "request play [index] | reset\n");
        return ESP_ERR_INVALID_ARG;
    }
    if (strcmp(argv[1], "status") == 0) {
        ms_info_t info;
        ms_stats_t st;
        if (ms_info_read(&info) != ESP_OK || ms_stats_read(&st) != ESP_OK) {
            printf("MS 不可用（music_session 未初始化）\n");
            return ESP_ERR_INVALID_STATE;
        }
        printf("MS state=%s paused=%d reason=%u mid=%u have_mid=%d duration=%dms progress=%ums\n",
               mc_state_name(info.state), info.paused ? 1 : 0, (unsigned)info.pause_reason,
               (unsigned)info.mid, info.have_mid ? 1 : 0, (int)info.duration_ms,
               (unsigned)info.progress_ms);
        printf("MS title=%s\n", info.title[0] != '\0' ? info.title : "-");
        printf("MS url=%s\n", info.url[0] != '\0' ? info.url : "-");
        printf("MSSTAT transitions=%" PRIu32 " tracks=%" PRIu32 " play_err=%" PRIu32
               " pauses=%" PRIu32 " resumes=%" PRIu32 " steals=%" PRIu32 " stops=%" PRIu32
               " eof=%" PRIu32 " controls=%" PRIu32 " dropped=%" PRIu32
               " resume_fail=%" PRIu32 " fallback=%" PRIu32 "\n",
               st.transitions, st.tracks, st.play_errors, st.pauses, st.resumes, st.steals,
               st.stops, st.eofs, st.controls, st.dropped, st.resume_failures,
               st.fallback_requests);
        return ESP_OK;
    }
    if (strcmp(argv[1], "play") == 0) {
        if (argc < 3) {
            printf("用法: ms play <http(s)://...>\n");
            return ESP_ERR_INVALID_ARG;
        }
        const esp_err_t err = ms_play(argv[2]);
        printf("MSPLAY err=%s\n", esp_err_to_name(err));
        return err;
    }
    if (strcmp(argv[1], "pause") == 0) {
        const esp_err_t err = ms_pause(0);
        printf("MSPAUSE err=%s\n", esp_err_to_name(err));
        return err;
    }
    if (strcmp(argv[1], "resume") == 0) {
        const esp_err_t err = ms_resume();
        printf("MSRESUME err=%s\n", esp_err_to_name(err));
        return err;
    }
    if (strcmp(argv[1], "stop") == 0) {
        const esp_err_t err = ms_stop();
        printf("MSSTOP err=%s\n", esp_err_to_name(err));
        return (err == ESP_ERR_INVALID_STATE) ? ESP_OK : err;
    }
    if (strcmp(argv[1], "request") == 0) {
        if (argc < 3 || strcmp(argv[2], "play") != 0) {
            printf("用法: ms request play [index]\n");
            return ESP_ERR_INVALID_ARG;
        }
        const int32_t index = (argc > 3) ? (int32_t)atoi(argv[3]) : 0;
        const esp_err_t err = ms_request(MC_REQUEST_ACTION_PLAY, index);
        printf("MSREQUEST err=%s action=play index=%d\n", esp_err_to_name(err), (int)index);
        return err;
    }
    if (strcmp(argv[1], "reset") == 0) {
        (void)ms_stats_reset();
        printf("MSSTAT cleared\n");
        return ESP_OK;
    }
    printf("用法: ms status | play <http(s)://...> | pause | resume | stop | "
           "request play [index] | reset\n");
    return ESP_ERR_INVALID_ARG;
}

static int cmd_player(int argc, char **argv)
{
    if (argc < 2) {
        printf("用法: player stat | reset | mp3 | pcm <off> <n> | stream [chunk] [gap_ms] | "
               "stream abort | duck on|off | stop [ms] | load begin | load end | load <base64> | "
               "mix stat|reset|vol|tone|stop|cap|dump\n");
        return ESP_ERR_INVALID_ARG;
    }

    const char *sub = argv[1];

    if (strcmp(sub, "stat") == 0) {
        print_player_stat();
        return ESP_OK;
    }
    if (strcmp(sub, "reset") == 0) {
        audio_player_stats_reset();
        printf("PLAYERSTAT cleared\n");
        return ESP_OK;
    }
    if (strcmp(sub, "pcm") == 0) {
        int offset = (argc > 2) ? atoi(argv[2]) : 0;
        int count = (argc > 3) ? atoi(argv[3]) : 64;
        if (offset < 0 || count <= 0 || count > 512) {
            printf("用法: player pcm <offset> <count，count 1~512\n");
            return ESP_ERR_INVALID_ARG;
        }
        int16_t tmp[512];
        esp_err_t err = audio_player_mp3_pcm_read(tmp, (size_t)offset, (size_t)count);
        if (err != ESP_OK) {
            printf("PLAYERPCM err=%s\n", esp_err_to_name(err));
            return err;
        }
        printf("PLAYERPCM off=%d count=%d\n", offset, count);
        for (int i = 0; i < count; i++) {
            printf("%04x", (uint16_t)tmp[i]);
            if ((i % 16) == 15 || i == count - 1) {
                printf("\n");
            }
        }
        printf("PLAYERPCMEND\n");
        return ESP_OK;
    }

    if (strcmp(sub, "mp3") == 0) {
        if (!audio_player_mp3_ready()) {
            printf("MP3 能力不可用（audio_player_init 时 PSRAM 分配失败或 Kconfig 关闭）\n");
            return ESP_ERR_NOT_SUPPORTED;
        }
        if (s_pload_len == 0) {
            printf("没有已加载的 MP3：先 player load begin → 分块 player load <base64> → player load end\n");
            return ESP_ERR_INVALID_STATE;
        }
        esp_err_t err = audio_player_play_mp3(s_pload_buf, s_pload_len);
        if (err != ESP_OK) {
            printf("PLAYERPLAY err=%s\n", esp_err_to_name(err));
            return err;
        }
        printf("PLAYERPLAY bytes=%u\n", (unsigned)s_pload_len);
        return ESP_OK;
    }
    if (strcmp(sub, "duck") == 0) {
        /* T3.2 R3 探针：不依赖音乐源即可验证 duck 增益（播放中调用看 out_peak 变化） */
        if (argc < 3 || (strcmp(argv[2], "on") != 0 && strcmp(argv[2], "off") != 0)) {
            printf("用法: player duck on|off\n");
            return ESP_ERR_INVALID_ARG;
        }
        audio_player_set_duck(strcmp(argv[2], "on") == 0);
        printf("PLAYERDUCK on=%d\n", audio_player_get_duck() ? 1 : 0);
        return ESP_OK;
    }
    if (strcmp(sub, "stop") == 0) {
        /* T3.2 R4 探针：对当前播放做淡出停止（不依赖会话/按键） */
        const int ms = (argc > 2) ? atoi(argv[2]) : CONFIG_AUDIO_PLAYER_INTERRUPT_FADE_MS;
        if (ms < 10 || ms > 1000) {
            printf("用法: player stop [ms(10~1000)]\n");
            return ESP_ERR_INVALID_ARG;
        }
        esp_err_t err = audio_player_fade_out_and_stop((uint32_t)ms);
        printf("PLAYERSTOP fade=%d err=%s\n", ms, esp_err_to_name(err));
        return err;
    }
    if (strcmp(sub, "stream") == 0) {
        /* 开发期探针（T2.4）：把 player load 上传的 MP3 按 chunk 喂进流式路径，
         * gap_ms>0 时每块之间暂停，模拟网络抖动/欠载。默认 512 0。 */
        if (argc > 2 && strcmp(argv[2], "abort") == 0) {
            esp_err_t err = audio_player_stream_abort();
            printf("PLAYERSTREAM abort err=%s\n", esp_err_to_name(err));
            return err;
        }
        int chunk = (argc > 2) ? atoi(argv[2]) : 512;
        int gap_ms = (argc > 3) ? atoi(argv[3]) : 0;
        if (chunk <= 0 || chunk > 2048 || gap_ms < 0 || gap_ms > 5000) {
            printf("用法: player stream [chunk_bytes(1~2048)] [gap_ms(0~5000)] | player stream abort\n");
            return ESP_ERR_INVALID_ARG;
        }
        if (!audio_player_mp3_ready()) {
            printf("MP3 能力不可用（audio_player_init 时 PSRAM 分配失败或 Kconfig 关闭）\n");
            return ESP_ERR_NOT_SUPPORTED;
        }
        if (!s_pload_buf || s_pload_len == 0) {
            printf("没有已加载的 MP3：先 player load begin → 分块 player load <base64> → player load end\n");
            return ESP_ERR_INVALID_STATE;
        }
        esp_err_t err = audio_player_stream_open(1, 24000);
        if (err != ESP_OK) {
            printf("PLAYERSTREAM open err=%s\n", esp_err_to_name(err));
            return err;
        }
        printf("PLAYERSTREAM open bytes=%u chunk=%d gap_ms=%d\n",
               (unsigned)s_pload_len, chunk, gap_ms);

        size_t off = 0;
        uint32_t backpressure = 0;
        while (off < s_pload_len) {
            size_t n = s_pload_len - off;
            if (n > (size_t)chunk) {
                n = (size_t)chunk;
            }
            err = audio_player_stream_write(s_pload_buf + off, n);
            if (err == ESP_ERR_NO_MEM) {
                /* ring 满：探针侧等播放消费（真实下行由云端流控），避免整段丢数据 */
                if (++backpressure > 5000) {
                    printf("PLAYERSTREAM write backpressure timeout off=%u\n", (unsigned)off);
                    break;
                }
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }
            if (err != ESP_OK) {
                printf("PLAYERSTREAM write off=%u err=%s\n", (unsigned)off, esp_err_to_name(err));
                break;
            }
            off += n;
            if (gap_ms > 0) {
                vTaskDelay(pdMS_TO_TICKS(gap_ms));
            }
        }
        err = audio_player_stream_end();
        printf("PLAYERSTREAM end fed=%u backpressure=%u err=%s\n", (unsigned)off,
               (unsigned)backpressure, esp_err_to_name(err));
        return ESP_OK;
    }
    if (strcmp(sub, "mix") == 0) {
        /* T3.3 三通道混音探针：三通道 tone 源 + 抓取输出，供 PC 侧 FFT/幅度/包络对拍 */
        if (argc < 3) {
            printf("用法: player mix stat | reset | vol <0~100> | stop | "
                   "tone <TTS|PROMPT|MUSIC> <freq 100~4000> <ms 50~10000> [amp 100~20000] | "
                   "cap <1~16384> | dump <off> <n 1~512>\n");
            return ESP_ERR_INVALID_ARG;
        }
        const char *m = argv[2];
        if (strcmp(m, "stat") == 0) {
            print_mix_stat();
            return ESP_OK;
        }
        if (strcmp(m, "reset") == 0) {
            audio_player_mix_stats_reset();
            printf("MIXRESET ok=1\n");
            return ESP_OK;
        }
        if (strcmp(m, "vol") == 0) {
            const int v = (argc > 3) ? atoi(argv[3]) : -1;
            esp_err_t err = ESP_ERR_INVALID_ARG;
            if (v >= 0 && v <= 100) {
                err = audio_player_set_volume((uint8_t)v);
            }
            printf("MIXVOL vol=%d err=%s\n", v, esp_err_to_name(err));
            return err;
        }
        if (strcmp(m, "tone") == 0) {
            if (argc < 6) {
                printf("用法: player mix tone <TTS|PROMPT|MUSIC> <freq 100~4000> <ms 50~10000> [amp 100~20000]\n");
                return ESP_ERR_INVALID_ARG;
            }
            const int ch = mix_ch_from_name(argv[3]);
            const int freq = atoi(argv[4]);
            const int ms = atoi(argv[5]);
            const int amp = (argc > 6) ? atoi(argv[6]) : 8000;
            if (ch < 0) {
                printf("MIXTONE err=通道名必须是 TTS|PROMPT|MUSIC\n");
                return ESP_ERR_INVALID_ARG;
            }
            esp_err_t err = audio_player_mix_tone((audio_player_ch_t)ch, (uint32_t)freq,
                                                  (uint32_t)ms, (uint32_t)amp);
            printf("MIXTONE ch=%s freq=%d ms=%d amp=%d err=%s\n", argv[3], freq, ms, amp,
                   esp_err_to_name(err));
            return err;
        }
        if (strcmp(m, "stop") == 0) {
            esp_err_t err = audio_player_mix_stop();
            printf("MIXSTOP ok=%d err=%s\n", (err == ESP_OK) ? 1 : 0, esp_err_to_name(err));
            return err;
        }
        if (strcmp(m, "cap") == 0) {
            const int frames = (argc > 3) ? atoi(argv[3]) : 0;
            esp_err_t err = audio_player_mix_capture((uint32_t)frames);
            printf("MIXCAP armed=%d err=%s\n", frames, esp_err_to_name(err));
            return err;
        }
        if (strcmp(m, "dump") == 0) {
            const int off = (argc > 3) ? atoi(argv[3]) : 0;
            const int count = (argc > 4) ? atoi(argv[4]) : 64;
            if (off < 0 || count <= 0 || count > 512) {
                printf("用法: player mix dump <off> <count 1~512>\n");
                return ESP_ERR_INVALID_ARG;
            }
            static int16_t tmp[512];
            esp_err_t err = audio_player_mix_capture_read(tmp, (size_t)off, (size_t)count);
            if (err != ESP_OK) {
                printf("MIXCAP err=%s\n", esp_err_to_name(err));
                return err;
            }
            /* 关键：整段 dump 一次 write 输出。逐行 printf 会被并发日志行插进字符流，
             * 造成 PC 侧解析丢行/拼行（T3.3 真机实测：512 样本偶发只解析到 480）。
             * 缓冲放 PSRAM（内部 RAM 水位只剩 ~2.8KB，2.8KB 的 BSS 会直接击穿 30KB 线）。 */
            static char *dump_buf;
            if (dump_buf == NULL) {
                dump_buf = heap_caps_malloc(256 + 512 * 4 + 512, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
                if (dump_buf == NULL) {
                    printf("MIXCAP err=no-mem\n");
                    return ESP_ERR_NO_MEM;
                }
            }
            const size_t dump_cap = 256 + 512 * 4 + 512;
            int n = snprintf(dump_buf, dump_cap, "MIXCAP off=%d count=%d\n", off, count);
            for (int i = 0; i < count; i++) {
                n += snprintf(dump_buf + n, dump_cap - (size_t)n, "%04x", (uint16_t)tmp[i]);
                if ((i % 16) == 15 || i == count - 1) {
                    n += snprintf(dump_buf + n, dump_cap - (size_t)n, "\n");
                }
            }
            n += snprintf(dump_buf + n, dump_cap - (size_t)n, "MIXCAPEND\n");
            printf("%s", dump_buf);
            return ESP_OK;
        }
        printf("未知 mix 子命令: %s\n", m);
        return ESP_ERR_INVALID_ARG;
    }

    if (strcmp(sub, "load") == 0) {
        if (argc < 3) {
            printf("用法: player load begin | load end | load <base64>\n");
            return ESP_ERR_INVALID_ARG;
        }
        if (strcmp(argv[2], "begin") == 0) {
            if (!s_pload_buf) {
                s_pload_buf = heap_caps_malloc(CONFIG_DIAG_PLAYER_TEST_MAX_BYTES,
                                               MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
                if (!s_pload_buf) {
                    printf("上传缓冲分配失败（PSRAM 不足）\n");
                    return ESP_ERR_NO_MEM;
                }
            }
            s_pload_len = 0;
            printf("PLAYERLOAD ready capacity=%u\n", (unsigned)CONFIG_DIAG_PLAYER_TEST_MAX_BYTES);
            return ESP_OK;
        }
        if (strcmp(argv[2], "end") == 0) {
            printf("PLAYERLOAD bytes=%u\n", (unsigned)s_pload_len);
            return ESP_OK;
        }
        if (!s_pload_buf) {
            printf("上传缓冲未初始化：先 player load begin\n");
            return ESP_ERR_INVALID_STATE;
        }
        int n = b64_decode(argv[2], s_pload_buf + s_pload_len, CONFIG_DIAG_PLAYER_TEST_MAX_BYTES - s_pload_len);
        if (n < 0) {
            printf("PLAYERLOAD err=base64 非法或超出容量（%u B）\n",
                   (unsigned)CONFIG_DIAG_PLAYER_TEST_MAX_BYTES);
            return ESP_ERR_INVALID_ARG;
        }
        s_pload_len += (size_t)n;
        printf("PLAYERLOAD chunk total=%u\n", (unsigned)s_pload_len);
        return ESP_OK;
    }

    printf("未知子命令: %s（可用: stat | reset | mp3 | load | duck | stop）\n", sub);
    return ESP_ERR_INVALID_ARG;
}

#endif /* CONFIG_DIAG_ENABLE_PLAYER_TEST */

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
            .command = "afe",
            .help = "上行 PCM 探针（T2.1）：afe uplink on | off | stat [ms]",
            .hint = "uplink on|off|stat [ms]",
            .func = cmd_afe,
        },
        {
            .command = "enc",
            .help = "Opus 上行编码探针（T2.2）：enc test [ms] [tone|uplink] | stat | dump on|off | open | close | reset",
            .hint = "test|stat|dump|open|close|reset",
            .func = cmd_enc,
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
            .help = "WebSocket：ws status | reconnect | send <json> | sendbin [len] | rxlog on|off",
            .hint = "status|reconnect|send|sendbin|rxlog",
            .func = cmd_ws,
        },
        {
            .command = "vc",
            .help = "语音云：vc status | start [sid] | stop | cancel | stat | reset（T2.5）",
            .hint = "status|start|stop|cancel|stat|reset",
            .func = cmd_vc,
        },
        {
            .command = "mc",
            .help = "音乐协议（T4.2/T4.3，纯协议）：mc status | report | reset",
            .hint = "status|report|reset",
            .func = cmd_mc,
        },
        {
            .command = "ms",
            .help = "音乐编排（T4.3）：ms status | play <url> | pause | resume | stop | "
                    "request play [index] | reset",
            .hint = "status|play|pause|resume|stop|request|reset",
            .func = cmd_ms,
        },
        {
            .command = "vs",
            .help = "会话状态机：vs status | start | interrupt | reset | followup on|off（T3.1/T3.7）",
            .hint = "status|start|interrupt|reset|followup",
            .func = cmd_vs,
        },
        {
            .command = "dev",
            .help = "按键/静音：dev status | mute [on|off] | reset（T3.4；LED 已移除）",
            .hint = "status|mute|reset",
            .func = cmd_dev,
        },
        {
            .command = "fb",
            .help = "异常反馈（T3.6）：fb status | reset | test <net|cloud|timeout|tts>",
            .hint = "status|reset|test",
            .func = cmd_fb,
        },
        {
            .command = "prompt",
            .help = "提示音资源（T3.5）：prompt info（assets 分区 pack 状态/条目/映射地址）",
            .hint = "info",
            .func = cmd_prompt,
        },
#if CONFIG_DIAG_ENABLE_PLAYER_TEST
        {
            .command = "music",
            .help = "音乐（T4.1）：music play <http(s)://...> | stop | stat",
            .func = cmd_music,
        },
        {
            .command = "player",
            .help = "播放探针（T2.3/T2.4/T3.2/T3.3）：player stat | reset | mp3 | duck on|off | stop [ms] | "
                    "load begin|end|<base64> | stream [chunk] [gap_ms] | stream abort | "
                    "mix stat|reset|vol|stop|tone|cap|dump",
            .hint = "stat|reset|mp3|pcm|load|stream|duck|stop|mix",
            .func = cmd_player,
        },
#endif
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
#if CONFIG_DIAG_ENABLE_PLAYER_TEST
    /* T2.3 的 MP3 素材走 base64 分块上传：行太短会让上传慢一个量级 */
    repl_config.max_cmdline_length = 1024;
#else
    repl_config.max_cmdline_length = 128;
#endif
    /* 默认 4096 对纯 console 命令够用，但 `enc test` 会在 REPL 任务里直接调 libopus：
     * 伪栈只放 temp 数组，libopus 本身还有递归/局部栈，实测 4KB 会溢出（T2.2 排查） */
    repl_config.task_stack_size = 8192;

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
