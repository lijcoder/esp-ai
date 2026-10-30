# WebSocket 协议（PROTOCOL）

> 本文件是 [ARCHITECTURE.md](ARCHITECTURE.md) §6 契约的详细展开。
> **当前状态：空壳**。P2（T2.5）实现 `voice_cloud` 时按 §6 补全字段表、错误码表与示例报文；在此之前一切以实现 ARCHITECTURE §6 为准。

## 待补内容（P2 时填写）

- [ ] 握手与鉴权字段完整定义（`hello` 参数、token 刷新策略）
- [ ] 二进制帧头逐字节说明与示例
- [ ] 上行事件（`audio_start` / `audio_end` / `interrupt` / `ping` / `music_request` / `music_status`）字段表
- [ ] 下行事件（`asr_partial` / `asr_final` / `llm_delta` / `tts_start` / `tts_end` / `music_track` / `error` / `pong`）字段表
- [ ] 错误码表与端侧处理策略
- [ ] 版本兼容策略（`ver` 字段、能力协商）
- [ ] 与后端联调的报文样例
