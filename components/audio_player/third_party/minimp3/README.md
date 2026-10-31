# minimp3（vendored，MP3 解码器）

**来源**：https://github.com/lieff/minimp3 ，commit `ea99364f61c14656440e8d77e9c233ccf3124633`（2026-10-31 取），单文件 `minimp3.h`。
**许可**：CC0 1.0（公有领域奉献，见文件头；无附加义务）。

## 为什么 vendored 而不是托管组件

- ESP-IDF 组件仓库没有 Espressif 官方 MP3 解码器；社区组件 `chmorgan/esp-libhelix-mp3`
  的 `AllocateBuffers()` 用裸 `malloc` 分配 23,856 B 解码状态，托管组件无法改分配器 →
  会占内部 RAM，违反 CONVENTIONS §4（wss 握手期水位的硬约束）。
- minimp3 没有任何内部 `malloc`：解码状态 `mp3dec_t` 6,668 B、临时区 `mp3dec_scratch_t`
  16,256 B 都由调用方提供 → 全部放 PSRAM，内部 RAM 增量 0。
- 实测（同板对比，T2.3 验收）：与 FFmpeg（PyAV）参考解码逐样本对比，24k 单声道 /
  44.1k 立体声最大误差 1 LSB、样本数完全一致。

## 与上游的差异（本项目补丁，共 3 处）

1. 公共 API 增加 `void mp3dec_set_scratch(void *scratch);`；
2. 实现段增加全局指针 `g_minimp3_scratch` 与 setter；
3. `mp3dec_decode_frame()` 里的 `mp3dec_scratch_t scratch;` 改为
   `mp3dec_scratch_t *scratch = g_minimp3_scratch;`（函数体内 `&scratch` / `scratch.`
   同步改为 `scratch` / `scratch->`）。**未注册临时区时直接返回 0**，不退化为栈上分配
   —— 否则编译器仍会保留 16,256 B 栈帧，把播放任务的 6KB 栈打爆。

这样 `decoder_mp3.c` 用 `heap_caps_malloc(..., MALLOC_CAP_SPIRAM)` 分配
`mp3dec_t + mp3dec_scratch_t + 单帧输出` 后注册即可，播放任务栈不变。

## 升级注意

重新拉取上游后必须重放上述 3 处补丁，并重跑 `uv run tools/mp3_test.py`（它对
24k/44.1k 两类素材做样本级数值断言）。
