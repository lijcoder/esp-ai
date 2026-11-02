/*
 * prompt_pack.c —— assets 分区提示音 pack 的纯逻辑实现（无 IDF 依赖）。
 * 校验规则与 tools/audio_pack.py 一一对应；host 单测见 test/host/prompt_pack_test.c。
 */
#include "prompt_pack.h"

#include <string.h>

uint32_t prompt_pack_crc32(uint32_t seed, const void *data, size_t len)
{
    /* 标准 CRC-32/ISO-HDLC（与 zlib.crc32 一致） */
    static const uint32_t k_poly = 0xEDB88320u;
    uint32_t crc = seed ^ 0xFFFFFFFFu;
    const uint8_t *p = (const uint8_t *)data;
    for (size_t i = 0; i < len; i++) {
        crc ^= p[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc >> 1) ^ (k_poly & (uint32_t)(-(int32_t)(crc & 1u)));
        }
    }
    return crc ^ 0xFFFFFFFFu;
}

const char *prompt_pack_err_str(prompt_pack_err_t err)
{
    switch (err) {
    case PROMPT_PACK_OK: return "ok";
    case PROMPT_PACK_ERR_TOO_SHORT: return "头部区不足";
    case PROMPT_PACK_ERR_MAGIC: return "magic 不匹配";
    case PROMPT_PACK_ERR_VERSION: return "版本不支持";
    case PROMPT_PACK_ERR_COUNT: return "条目数非法";
    case PROMPT_PACK_ERR_HEADER_CRC: return "头部 CRC 不匹配";
    case PROMPT_PACK_ERR_TOTAL_LEN: return "total_len 越界";
    case PROMPT_PACK_ERR_TOTAL_ALIGN: return "total_len 未对齐";
    case PROMPT_PACK_ERR_ENTRY_RANGE: return "条目 offset/length 越界";
    case PROMPT_PACK_ERR_ENTRY_ID: return "条目 id 越界/重复/缺失";
    case PROMPT_PACK_ERR_ENTRY_RATE: return "条目采样率非法";
    case PROMPT_PACK_ERR_ENTRY_ALIGN: return "条目对齐非法";
    default: return "未知错误";
    }
}

int prompt_pack_find(const prompt_pack_entry_t *entries, uint32_t count, uint8_t id)
{
    if (entries == NULL) {
        return -1;
    }
    for (uint32_t i = 0; i < count; i++) {
        if (entries[i].id == id) {
            return (int)i;
        }
    }
    return -1;
}

prompt_pack_err_t prompt_pack_validate(const void *data, uint32_t avail_len, uint32_t part_size,
                                       uint16_t expected_count, prompt_pack_entry_t *entries,
                                       uint32_t entries_cap, uint32_t *out_total_len)
{
    if (data == NULL || avail_len < PROMPT_PACK_HDR_SIZE) {
        return PROMPT_PACK_ERR_TOO_SHORT;
    }
    if (out_total_len != NULL) {
        *out_total_len = 0;
    }

    prompt_pack_hdr_t hdr;
    memcpy(&hdr, data, sizeof(hdr));

    if (memcmp(hdr.magic, PROMPT_PACK_MAGIC, PROMPT_PACK_MAGIC_LEN) != 0) {
        return PROMPT_PACK_ERR_MAGIC;
    }
    if (hdr.version != PROMPT_PACK_VERSION) {
        return PROMPT_PACK_ERR_VERSION;
    }
    if (hdr.count == 0 || hdr.count > PROMPT_PACK_MAX_ENTRIES ||
        (expected_count != 0 && hdr.count != expected_count)) {
        return PROMPT_PACK_ERR_COUNT;
    }

    const uint32_t table_len = prompt_pack_table_len(hdr.count);
    if (avail_len < table_len) {
        return PROMPT_PACK_ERR_TOO_SHORT;
    }

    /* 头部 CRC：table 区（本字段按 0 计算） */
    uint8_t tmp[PROMPT_PACK_HDR_SIZE + PROMPT_PACK_MAX_ENTRIES * PROMPT_PACK_ENTRY_SIZE];
    if (table_len > sizeof(tmp)) {
        return PROMPT_PACK_ERR_COUNT;
    }
    memcpy(tmp, data, table_len);
    prompt_pack_hdr_t *tmp_hdr = (prompt_pack_hdr_t *)tmp;
    tmp_hdr->header_crc32 = 0;
    const uint32_t crc = prompt_pack_crc32(0, tmp, table_len);
    if (crc != hdr.header_crc32) {
        return PROMPT_PACK_ERR_HEADER_CRC;
    }

    if (hdr.total_len < table_len || hdr.total_len > part_size) {
        return PROMPT_PACK_ERR_TOTAL_LEN;
    }
    if ((hdr.total_len & 3u) != 0) {
        return PROMPT_PACK_ERR_TOTAL_ALIGN;
    }

    /* 条目表 */
    prompt_pack_entry_t local[PROMPT_PACK_MAX_ENTRIES];
    const prompt_pack_entry_t *ent = local;
    const uint8_t *p = (const uint8_t *)data + PROMPT_PACK_HDR_SIZE;
    for (uint16_t i = 0; i < hdr.count; i++) {
        memcpy(&local[i], p + (size_t)i * PROMPT_PACK_ENTRY_SIZE, sizeof(local[i]));
    }

    uint32_t seen_mask = 0;
    for (uint16_t i = 0; i < hdr.count; i++) {
        const prompt_pack_entry_t *e = &ent[i];
        if (e->id >= PROMPT_PACK_MAX_ENTRIES) {
            return PROMPT_PACK_ERR_ENTRY_ID;
        }
        if (seen_mask & (1u << e->id)) {
            return PROMPT_PACK_ERR_ENTRY_ID; /* 重复 id */
        }
        seen_mask |= 1u << e->id;

        if (e->sample_rate != PROMPT_PACK_EXPECT_SAMPLE_RATE) {
            return PROMPT_PACK_ERR_ENTRY_RATE;
        }
        if (e->length == 0 || (e->length & 1u) != 0) {
            return PROMPT_PACK_ERR_ENTRY_ALIGN;
        }
        if ((e->offset & 3u) != 0 || e->offset < table_len) {
            return PROMPT_PACK_ERR_ENTRY_ALIGN;
        }
        if (e->offset > hdr.total_len || e->length > hdr.total_len - e->offset) {
            return PROMPT_PACK_ERR_ENTRY_RANGE;
        }
    }

    if (entries != NULL) {
        if (entries_cap < hdr.count) {
            return PROMPT_PACK_ERR_COUNT;
        }
        memcpy(entries, local, (size_t)hdr.count * sizeof(local[0]));
    }
    if (out_total_len != NULL) {
        *out_total_len = hdr.total_len;
    }
    return PROMPT_PACK_OK;
}
