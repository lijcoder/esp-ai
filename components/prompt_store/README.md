# prompt_store —— 提示音资源存储（L1 基础资源，T3.5）

**作用（一句话）**：把 5 条提示音（唤醒应答 + 4 条错误提示）存放在 **`assets` 分区**的自定义只读
pack 里，启动时校验后 **`esp_partition_mmap` 零拷贝映射**，通过 `prompt_assets_get()` 交给上层播放；
**单独烧 `assets` 分区即可替换提示音，不用重编 app 固件**。

设计缘由（方案 D，用户 2026-11-02 拍板，详见 [ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §3.4）：
- 不用 SPIFFS：mount 要占内部堆，而本项目 `wss` 水位余量只有 ~2.6KB（CONVENTIONS §4）；
- 自定义 pack 校验/映射几乎零 RAM；`assets` 分区 2M @ 0x7a0000，内容由 `tools/audio_pack.py` 生成。

## 对外接口（`include/prompt_assets.h`）

| 接口 | 说明 |
|---|---|
| `prompt_store_init()` | 查找 `assets` 分区 → 校验 pack → mmap（失败回退 PSRAM 加载）；**失败不致命**，返回错误码，业务降级 |
| `prompt_store_ready()` / `prompt_store_info()` | 观测（`diag` 的 `prompt info`） |
| `prompt_assets_get(id)` | 取资源：返回指向 flash 映射区（或 PSRAM）的 `prompt_clip_t`；未就绪/非法 id 返回 NULL |
| `prompt_assets_name(id)` | 短名（日志/测试断言用） |

提示音 ID（与 T3.6 冻结接口一致）：`PROMPT_WAKE_OK` / `PROMPT_NET_OFFLINE` / `PROMPT_CLOUD_ERROR` /
`PROMPT_TIMEOUT` / `PROMPT_TTS_FAIL`（`PROMPT_COUNT=5`）。

## pack 格式（`include/prompt_pack.h`，纯逻辑可 host 单测）

- 头部 24B（magic `ESPAPK01`、version=1、count、total_len、header CRC32、reserved）；
- 条目 20B × count（id、offset、length、sample_rate=16000、PCM CRC32）；
- PCM 从 `align4(24+count*20)` 开始（count=5 → 128），每条 4 字节对齐，16k/16bit/单声道；
- 标准 CRC-32/ISO-HDLC（与 Python `zlib.crc32` 一致）。

## 降级语义

| 场景 | 行为 |
|---|---|
| `assets` 分区未找到 / pack 头/CRC/条目非法 | `prompt_store_init()` 返回错误 + `PROMPT` ERROR 日志；`prompt_assets_get()` 返回 NULL；**不崩溃** |
| mmap 失败 | 回退「分块读入 PSRAM」并逐条校验 PCM CRC32（`mode=psram`） |
| 提示音缺失时的业务 | wake：跳过"我在呢"直接起轮；错误反馈：只日志不播；均不影响其它链路 |

## 资源更新（不重编 app）

```bash
uv run tools/audio_pack.py                      # 由 tools/prompts_src/*.wav 生成 build/assets.bin
uv run tools/audio_pack.py --flash /dev/cu.usbmodemXXXX
# 重启后启动日志：PROMPT: assets pack 就绪：mode=mmap ver=1 entries=5 ...
```

## 依赖

`esp_partition`（分区查找/读/mmap）、`spi_flash`（MMU 页常量）、`heap`（PSRAM 降级）；无上层依赖。
