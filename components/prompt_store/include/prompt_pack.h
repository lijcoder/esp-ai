/*
 * prompt_pack —— assets 分区提示音 pack 的**纯逻辑**（无 IDF 依赖，可 host 单测）。
 *
 * pack 格式（小端，详见 docs/ARCHITECTURE.md §3.4 与 tools/audio_pack.py）：
 *
 *   struct prompt_pack_hdr {            // 24 B
 *     char magic[8];                    // "ESPAPK01"
 *     uint16_t version;                 // 1
 *     uint16_t count;                   // 条目数（当前 5）
 *     uint32_t total_len;               // 整个 pack 字节数（4 字节对齐）
 *     uint32_t header_crc32;            // CRC32(头部区 0..24+count*20，本字段按 0 计算)
 *     uint32_t reserved;                // 0
 *   };
 *   struct prompt_pack_entry {          // 20 B × count
 *     uint8_t  id;                      // prompt_id_t
 *     uint8_t  reserved[3];
 *     uint32_t offset;                  // PCM 相对分区 0 的偏移（4 字节对齐）
 *     uint32_t length;                  // PCM 字节数（样本数 × 2）
 *     uint32_t sample_rate;             // 16000
 *     uint32_t crc32;                   // CRC32(PCM 字节)
 *   };
 *   // PCM 区起点 = align4(24 + count*20 + 4)（预留 4B 对齐余量；count=5 → 128）；
 *   // 每条 PCM 4 字节对齐；total_len 4 字节对齐、尾部补 0。
 *
 * CRC32 采用标准 CRC-32/ISO-HDLC（= zlib.crc32 / Python `zlib.crc32`）：
 * 反射多项式 0xEDB88320，初值 0xFFFFFFFF，末尾异或 0xFFFFFFFF。
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#define PROMPT_PACK_MAGIC "ESPAPK01"
#define PROMPT_PACK_MAGIC_LEN 8
#define PROMPT_PACK_VERSION 1
#define PROMPT_PACK_MAX_ENTRIES 16
#define PROMPT_PACK_HDR_SIZE 24
#define PROMPT_PACK_ENTRY_SIZE 20
#define PROMPT_PACK_EXPECT_SAMPLE_RATE 16000
#define PROMPT_PACK_MAP_ALIGN 0x10000 /* SPIFLASH MMU 页 64KB */

typedef struct __attribute__((packed)) {
    char magic[PROMPT_PACK_MAGIC_LEN];
    uint16_t version;
    uint16_t count;
    uint32_t total_len;
    uint32_t header_crc32;
    uint32_t reserved;
} prompt_pack_hdr_t;

typedef struct __attribute__((packed)) {
    uint8_t id;
    uint8_t reserved[3];
    uint32_t offset;
    uint32_t length;
    uint32_t sample_rate;
    uint32_t crc32;
} prompt_pack_entry_t;

typedef enum {
    PROMPT_PACK_OK = 0,
    PROMPT_PACK_ERR_TOO_SHORT,   /* 头部区不足 */
    PROMPT_PACK_ERR_MAGIC,       /* magic 不匹配 */
    PROMPT_PACK_ERR_VERSION,     /* 版本不支持 */
    PROMPT_PACK_ERR_COUNT,       /* 条目数非法 */
    PROMPT_PACK_ERR_HEADER_CRC,  /* 头部 CRC 不匹配 */
    PROMPT_PACK_ERR_TOTAL_LEN,   /* total_len 越界/过小 */
    PROMPT_PACK_ERR_TOTAL_ALIGN, /* total_len 未 4 字节对齐 */
    PROMPT_PACK_ERR_ENTRY_RANGE, /* 条目 offset/length 越界 */
    PROMPT_PACK_ERR_ENTRY_ID,    /* id 越界/重复/缺失 */
    PROMPT_PACK_ERR_ENTRY_RATE,  /* 采样率不是 16000 */
    PROMPT_PACK_ERR_ENTRY_ALIGN, /* offset 未对齐/length 非偶数字节 */
} prompt_pack_err_t;

/* 标准 CRC32（与 zlib.crc32 一致；seed 传 0 开始） */
uint32_t prompt_pack_crc32(uint32_t seed, const void *data, size_t len);

const char *prompt_pack_err_str(prompt_pack_err_t err);

/* 头部区字节数（24 + count*20） */
static inline uint32_t prompt_pack_table_len(uint16_t count)
{
    return (uint32_t)PROMPT_PACK_HDR_SIZE + (uint32_t)count * PROMPT_PACK_ENTRY_SIZE;
}

/**
 * @brief 校验 pack 头部与条目表
 *
 * @param data         pack 起始地址（调用方保证至少 avail_len 可读；≥ 头部区即可）
 * @param avail_len    可读字节数（通常是映射尺寸/读取缓冲大小，必须覆盖头部区）
 * @param part_size    assets 分区容量（total_len 不得超过）
 * @param expected_count 期望条目数（固件传 PROMPT_COUNT）
 * @param entries      输出条目表（可为 NULL）
 * @param entries_cap  entries 容量
 * @param out_total_len 输出 total_len（可为 NULL）
 * @return PROMPT_PACK_OK 或具体错误
 */
prompt_pack_err_t prompt_pack_validate(const void *data, uint32_t avail_len, uint32_t part_size,
                                       uint16_t expected_count, prompt_pack_entry_t *entries,
                                       uint32_t entries_cap, uint32_t *out_total_len);

/* 查询某个 id 在条目表中的下标；未找到返回 -1 */
int prompt_pack_find(const prompt_pack_entry_t *entries, uint32_t count, uint8_t id);
