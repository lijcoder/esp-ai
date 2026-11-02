# test/host —— T3.6 异常反馈/提示音资源的 host 单测

**作用**：不依赖 ESP-IDF、不上板、不占串口，用主机 `cc` 直接编译并验证两件纯逻辑：

1. `components/assets` 的提示音资源表（`prompt_assets_get/name` + 4 条错误提示 PCM 的时长/峰值/
   直流/首尾淡入淡出）；
2. `components/feedback/include/feedback_policy.h` 的 `fb_kind_from_cloud_code()` 映射
   （3001/3002 → `tts-fail`，其余 → `cloud-error`）与 `fb_kind_t` 数值契约。

契约依据：`build/t36_contract.md` §3.2 / §3.3 / §5.3（T3.6）。

## 怎么跑

```bash
make -C test/host              # 编译 + 跑全部用例（推荐）
make -C test/host feedback     # 只跑反馈策略用例（assets 未生成时也能跑）
make -C test/host clean        # 清理编译产物
```

期望输出（每项一行 PASS，最后一行机器可读）：

```
  PASS  PROMPT_COUNT==5（实际 5）
  ...
PROMPT_ASSETS_TEST: PASS（55/55 项）

FEEDBACK_POLICY_TEST: PASS（34/34 项）
```

退出码 0 = 全 PASS，1 = 有 FAIL；`cc -std=c11 -Wall -Wextra` 零警告。

## 覆盖什么

### `prompt_assets_test.c`（提示音资源表）

| 断言 | 口径 |
|---|---|
| `PROMPT_COUNT == 5` | wake + net-offline/cloud-error/timeout/tts-fail 四条错误提示 |
| 每个 id | `prompt_assets_get` 非空、`pcm` 非空、`samples>0`、`sample_rate==16000` |
| 时长 | wake **0.75~0.85 s**；错误提示 **0.6~2.0 s** |
| 峰值 | **12000~14500**（约 -8.7~-7.1 dBFS，归一化 ±3%） |
| 直流 | `|mean| < 100` |
| 首/尾 20 ms | `|x| < 200`（淡入淡出，防 pop） |
| 名称 | `prompt_assets_name` == `wake` / `net-offline` / `cloud-error` / `timeout` / `tts-fail` |
| 非法 id | `prompt_assets_get` 返回 `NULL`、`prompt_assets_name` 返回 `"?"` |

### `feedback_policy_test.c`（反馈策略）

| 断言 | 口径 |
|---|---|
| 云端错误码映射 | `3001`/`3002` → `FB_KIND_TTS_FAIL`；`1/1001/2001/2002/9001/0/-1/未知` → `FB_KIND_CLOUD_ERROR` |
| kind 数值 | 0=NONE、1=net-offline、2=cloud-error、3=timeout、4=tts-fail、5=COUNT（写入 `APP_EVT_FEEDBACK.payload.kind` 的契约） |
| 名称 | `fb_kind_name` 返回 `net-offline`/`cloud-error`/`timeout`/`tts-fail`，非法返回 `"?"` |
| 合法性 | `fb_kind_valid` 只对 1~4 为真（NONE/COUNT/越界为假） |

## 为什么能不依赖 IDF

- `components/assets/prompt_*.c` 只引用 `<stdint.h>/<stddef.h>` 与同目录生成的
  `prompt_pcm_sizes.h`，没有任何 `esp_log`/FreeRTOS/驱动依赖；
- `feedback_policy.h` 是 `static inline` 纯逻辑头（`feedback.c` 才依赖 IDF；本目录不编它）；
- 因此 `cc -I components/assets/include -I components/feedback/include` 即可编译运行。

注意：`components/assets/prompt_assets.c`、`prompt_*.c`、`prompt_pcm_sizes.h` 由
`uv run tools/prompt_gen.py` 生成（T3.6 prompt-audio 交付）；缺失时 `make` 会打印明确提示并退出 2，
`make -C test/host feedback` 仍可单独验证反馈策略。

## 与其它测试的分工

- **本目录**：纯逻辑静态断言（离线、毫秒级、`cc` 直编），不含任何设备/串口代码；
- `tools/prompt_gen.py --check`：校验**生成物与生成参数一致**（音频工程侧）；
- `tools/error_prompt_test.py`：真机端到端（mock 注入云端错误码/时机 → 串口日志断言
  `VS:`/`FB:`/`PLAYER:` 与 `fb status`/`vs status`/`mic stat` 字段）；板测由 Lead 跑。
