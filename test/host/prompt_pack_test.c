/*
 * prompt_pack_test.c —— assets 分区提示音 pack 校验逻辑的 host 单测（T3.5，方案 D）。
 *
 * 为什么能脱离 IDF：components/prompt_store/prompt_pack.c 只依赖 <stdint.h>/<string.h>，
 * 不含 esp_log/分区/驱动调用；pack 校验是纯内存逻辑，可在 PC 上直接验证（含坏包用例）。
 *
 * 用法：make -C test/host  （或 cc -I../../components/prompt_store/include ...）
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "prompt_pack.h"

static int g_checks;
static int g_fails;

static void check(int cond, const char *name)
{
    g_checks++;
    if (cond) {
        printf("  PASS  %s\n", name);
    } else {
        g_fails++;
        printf("  FAIL  %s\n", name);
    }
}

static void wr16(uint8_t *p, uint16_t v)
{
    memcpy(p, &v, 2);
}

static void wr32(uint8_t *p, uint32_t v)
{
    memcpy(p, &v, 4);
}

static const uint32_t k_pcm_bytes = 320; /* 160 样本/条 */

static void fix_header_crc(uint8_t *buf, uint32_t table_len)
{
    wr32(buf + 16, 0);
    wr32(buf + 16, prompt_pack_crc32(0, buf, table_len));
}

/*
 * 构造 synthetic pack：count 条、PCM 起点 128（与真实 pack 一致）或指定起点，
 * 每条 k_pcm_bytes 字节（16kHz、静音 PCM，crc 为 0 数据的 crc）。
 * total_len = pcm_off + count*k_pcm_bytes。
 */
static uint32_t build_pack(uint8_t *buf, const uint8_t *ids, uint16_t count, uint32_t pcm_off)
{
    const uint32_t table_len = prompt_pack_table_len(count);
    const uint32_t total = pcm_off + (uint32_t)count * k_pcm_bytes;
    memset(buf, 0, total);
    memcpy(buf, PROMPT_PACK_MAGIC, PROMPT_PACK_MAGIC_LEN);
    wr16(buf + 8, PROMPT_PACK_VERSION);
    wr16(buf + 10, count);
    wr32(buf + 12, total);
    wr32(buf + 16, 0);
    wr32(buf + 20, 0);
    const uint32_t pcm_crc = prompt_pack_crc32(0, buf + pcm_off, k_pcm_bytes);
    for (uint16_t i = 0; i < count; i++) {
        uint8_t *e = buf + PROMPT_PACK_HDR_SIZE + (uint32_t)i * PROMPT_PACK_ENTRY_SIZE;
        e[0] = ids[i];
        wr32(e + 4, pcm_off + (uint32_t)i * k_pcm_bytes);
        wr32(e + 8, k_pcm_bytes);
        wr32(e + 12, PROMPT_PACK_EXPECT_SAMPLE_RATE);
        wr32(e + 16, pcm_crc);
    }
    (void)table_len;
    fix_header_crc(buf, table_len);
    return total;
}

int main(void)
{
    printf("===== PROMPT_PACK_TEST（T3.5 assets pack 校验） =====\n");
    uint8_t buf[2048];
    prompt_pack_entry_t entries[PROMPT_PACK_MAX_ENTRIES];
    uint32_t total = 0;

    /* 1. CRC32 标准向量（与 Python zlib.crc32 一致） */
    check(prompt_pack_crc32(0, "123456789", 9) == 0xCBF43926u, "CRC32('123456789') == 0xCBF43926");
    check(prompt_pack_crc32(0, "", 0) == 0x00000000u, "CRC32('') == 0");

    /* 2. 合法 pack（1 条）：校验通过 + 条目可查 + total_len 输出 */
    const uint8_t one[] = {0};
    uint32_t len = build_pack(buf, one, 1, 128);
    prompt_pack_err_t err =
        prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, entries, PROMPT_PACK_MAX_ENTRIES, &total);
    check(err == PROMPT_PACK_OK, "合法单条 pack 校验通过");
    check(total == len, "total_len 输出正确");
    check(entries[0].id == 0 && entries[0].offset == 128 && entries[0].length == 320,
          "条目字段解析正确");
    check(prompt_pack_find(entries, 1, 0) == 0, "find(0) 命中");
    check(prompt_pack_find(entries, 1, 1) == -1, "find(1) 未命中返回 -1");
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, NULL, 0, NULL) == PROMPT_PACK_OK,
          "entries 传 NULL 也能校验（不导出条目）");

    /* 3. 头部错误 */
    build_pack(buf, one, 1, 128);
    buf[0] = 'X';
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, entries, 16, NULL) ==
              PROMPT_PACK_ERR_MAGIC,
          "magic 错 → ERR_MAGIC");
    build_pack(buf, one, 1, 128);
    wr16(buf + 8, 2);
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, entries, 16, NULL) ==
              PROMPT_PACK_ERR_VERSION,
          "版本错 → ERR_VERSION");
    build_pack(buf, one, 1, 128);
    wr16(buf + 10, 0);
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 0, entries, 16, NULL) ==
              PROMPT_PACK_ERR_COUNT,
          "count=0 → ERR_COUNT");
    build_pack(buf, one, 1, 128);
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 2, entries, 16, NULL) ==
              PROMPT_PACK_ERR_COUNT,
          "count 与期望不符 → ERR_COUNT");
    build_pack(buf, one, 1, 128);
    wr32(buf + 16, 0x12345678u);
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, entries, 16, NULL) ==
              PROMPT_PACK_ERR_HEADER_CRC,
          "头部 CRC 错 → ERR_HEADER_CRC");
    check(prompt_pack_validate(buf, 20, 2 * 1024 * 1024, 1, entries, 16, NULL) ==
              PROMPT_PACK_ERR_TOO_SHORT,
          "avail 不足头部 → ERR_TOO_SHORT");

    /* 4. total_len 边界/对齐 */
    build_pack(buf, one, 1, 128);
    wr32(buf + 12, 4 * 1024 * 1024);
    fix_header_crc(buf, prompt_pack_table_len(1));
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, entries, 16, NULL) ==
              PROMPT_PACK_ERR_TOTAL_LEN,
          "total_len > 分区 → ERR_TOTAL_LEN");
    build_pack(buf, one, 1, 128);
    wr32(buf + 12, 129);
    fix_header_crc(buf, prompt_pack_table_len(1));
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, entries, 16, NULL) ==
              PROMPT_PACK_ERR_TOTAL_ALIGN,
          "total_len 未 4 字节对齐 → ERR_TOTAL_ALIGN");

    /* 5. 条目错误 */
    build_pack(buf, one, 1, 128);
    wr32(buf + 24 + 12, 44100);
    fix_header_crc(buf, prompt_pack_table_len(1));
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, entries, 16, NULL) ==
              PROMPT_PACK_ERR_ENTRY_RATE,
          "采样率非 16000 → ERR_ENTRY_RATE");
    build_pack(buf, one, 1, 128);
    wr32(buf + 24 + 4, 130);
    fix_header_crc(buf, prompt_pack_table_len(1));
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, entries, 16, NULL) ==
              PROMPT_PACK_ERR_ENTRY_ALIGN,
          "offset 未 4 字节对齐 → ERR_ENTRY_ALIGN");
    build_pack(buf, one, 1, 128);
    wr32(buf + 24 + 8, 0);
    fix_header_crc(buf, prompt_pack_table_len(1));
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, entries, 16, NULL) ==
              PROMPT_PACK_ERR_ENTRY_ALIGN,
          "length=0 → ERR_ENTRY_ALIGN");
    build_pack(buf, one, 1, 128);
    wr32(buf + 24 + 4, 128 + 300); /* 128+300+320 > total(448) */
    fix_header_crc(buf, prompt_pack_table_len(1));
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 1, entries, 16, NULL) ==
              PROMPT_PACK_ERR_ENTRY_RANGE,
          "offset+length 越界 → ERR_ENTRY_RANGE");

    /* 6. 重复 id */
    const uint8_t dup[] = {1, 1};
    build_pack(buf, dup, 2, 128);
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 2, entries, 16, NULL) ==
              PROMPT_PACK_ERR_ENTRY_ID,
          "重复 id → ERR_ENTRY_ID");

    /* 7. 真实 5 条布局（与固件 PROMPT_COUNT 一致）快速回归 */
    const uint8_t five[] = {0, 1, 2, 3, 4};
    build_pack(buf, five, 5, 128);
    check(prompt_pack_validate(buf, 344, 2 * 1024 * 1024, 5, entries, 16, &total) ==
              PROMPT_PACK_OK,
          "5 条合法 pack 校验通过");
    int all = 1;
    for (int i = 0; i < 5; i++) {
        all &= (prompt_pack_find(entries, 5, (uint8_t)i) == i);
    }
    check(all, "5 条 id 全部可查");

    printf("\nPROMPT_PACK_TEST: %s（%d/%d 项）\n", g_fails == 0 ? "PASS" : "FAIL",
           g_checks - g_fails, g_checks);
    return g_fails == 0 ? 0 : 1;
}
