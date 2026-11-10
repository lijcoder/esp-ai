# test/host —— T3.5/T3.6/T4.3 host 单测（不依赖 ESP-IDF）

**作用**：不上板、不占串口，用主机 `cc` 直接编译并验证三块**纯逻辑**：

1. **提示音 pack 校验**（T3.5，`components/prompt_store/prompt_pack.c`）：assets 分区自定义只读 pack 的
   magic/版本/条目数/头部 CRC32/total_len/条目 offset·length·采样率/重复 id 等校验，以及真实 5 条布局；
   覆盖坏包用例（固件启动降级路径的前置条件）；
2. **异常反馈映射**（T3.6，`components/feedback/include/feedback_policy.h`）：
   `fb_kind_from_cloud_code()`（3001/3002 → `tts-fail`，其余 → `cloud-error`）与 `fb_kind_t` 数值契约；
3. **音乐编排决策**（T4.3，`components/music_session/include/music_policy.h`）：
   语音抢占 auto-pause/auto-resume、pause_reason 优先级（用户 > 语音）、`mid` accept/stale/dup、
   `music_control` 应用结果、resume 失败兜底判定与状态迁移表。

> 提示音资源的**内容**（时长/峰值/首尾静音）现在由 `tools/audio_pack.py --check` 与 `prompt_gen.py`
> 校验（源 WAV 在 `tools/prompts_src/`，不再有 C 数组）。

## 怎么跑

```bash
make -C test/host              # 编译 + 跑全部用例（推荐）
make -C test/host pack         # 只跑 pack 校验
make -C test/host feedback     # 只跑反馈策略
make -C test/host music        # 只跑音乐编排策略
make -C test/host clean        # 清理编译产物
```

期望输出（每项一行 PASS，最后一行机器可读）：

```
PROMPT_PACK_TEST: PASS（23/23 项）
FEEDBACK_POLICY_TEST: PASS（34/34 项）
MUSIC_POLICY_TEST: PASS（76/76 项）
```

退出码 0 = 全 PASS，1 = 有 FAIL；`cc -std=c11 -Wall -Wextra` 零警告。
契约依据：`build/t35_contract.md` §2（pack 格式）、`build/t36_contract.md` §3、`build/t43_contract.md` §3/§4/§5。
