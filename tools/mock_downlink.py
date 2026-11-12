"""mock_downlink —— 一轮语音的「下行」：ASR 文本 → TTS 音频帧，以及错误注入。

从 Session 拆出的混入类 DownlinkMixin：只负责 audio_end 之后云端该做的事——
ASR 文本（固定文案 / 本地 SenseVoice）、TTS 音频（合成帧 / 真实 MP3 / Qwen3-TTS 复读）、
发送节奏与欠载注入、云端错误码注入。连接与上行仍由 Session 负责，继承后行为与拆分前一致。

DownlinkMixin 依赖宿主（tools/mock_server.py 的 Session）提供：
    cfg / stats / sid / server / tts_task
    _log() / _send_json() / _send_bin()
本文件不单独实例化该类。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from websockets.exceptions import ConnectionClosed

import llm_client
from mock_core import (
    ERROR_TTS_FRAMES,
    FRAME_HEADER_LEN,
    LOG,
    TTS_BAD_CODEC,
    TTS_CODEC,
    TTS_SAMPLE_RATE,
    Mp3Frame,
    parse_mp3_frames,
)
from mock_pipeline import LlmTtsPipeline, PipelineConfig, SegmenterConfig
import music_tools
import responses_client
from mock_music import (ACTION_NEXT, ACTION_PLAY, ACTION_PREV, CONTROL_ACTIONS,
                        build_control_event)


def module_speakable(text: str) -> bool:
    """文本里是否有真正可发音的字符（中文/字母/数字）；纯标点/空白视为"没有内容"。

    ASR 静音时常返回 "."、TTS 拿到这种文本会生成 0 帧并让 piper 抛
    `wave.Error: # channels not specified`，所以链路前后都要判一次。
    """
    return any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in text or "")


class DownlinkMixin:
    """audio_end 之后的下行流程（TTS 流 / 错误注入）；由 Session 继承。"""

    def _synth_tts_payload(self, index: int) -> bytes:
        """合成 TTS payload，仅用于帧级联调，**不是可解码的 MP3**。

        前 4 字节写成 MP3 帧同步头样式，便于 hexdump 肉眼核对；T1.5 不依赖真实
        MP3 素材（本机无 ffmpeg），端侧 MP3 解码在 T2.3 实现时再补真实样例。
        """
        size = max(FRAME_HEADER_LEN, self.cfg.chunk_bytes)
        head = b"\xff\xfb\x90\x64" + (index & 0xFFFF).to_bytes(2, "big")
        return (head + b"\x00" * size)[:size]

    def _llm_config(self) -> llm_client.LlmConfig:
        """--llm 的配置（地址/模型/token 只来自 CLI 或环境变量，不落文件）。"""
        cfg = self.cfg
        return llm_client.LlmConfig.from_env(
            base_url=cfg.llm_base_url or None,
            api_key=cfg.llm_api_key or None,
            model=cfg.llm_model or None,
            system=cfg.llm_system or None,
            max_tokens=cfg.llm_max_tokens or None,
        )

    async def _llm_answer(self, text: str) -> str:
        """ASR 文本 → LLM（OpenAI 兼容流式）→ 回答；边收边发 `llm_delta`。

        阻塞的 SSE 读取放独立线程，增量经 asyncio 队列回主循环（不能堵 WS 收包/心跳）。
        失败抛异常，由 `_tts_flow` 回退成"复读"（answer = ASR 文本）。

        注意：这是**非流水线**路径（等整段回答再交给 TTS）。`--llm --tts-qwen` 默认走
        `_llm_tts_stream_flow()`：边出字边分句合成，首声更早。
        """
        lcfg = self._llm_config()
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        sentinel = object()
        stop = threading.Event()

        def emit(item) -> bool:
            """线程安全入队；已取消/事件循环关闭时返回 False（让 SSE 线程尽快收手）。"""
            if stop.is_set():
                return False
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:  # 事件循环已关闭（断连/进程退出）
                return False
            return True

        def producer() -> None:
            try:
                for delta in llm_client.stream_chat(text, lcfg):
                    if not emit(delta):
                        return
            except BaseException as exc:  # noqa: BLE001 - 异常回传主线程
                if not emit(exc):
                    return
            finally:
                emit(sentinel)

        threading.Thread(target=producer, name="llm", daemon=True).start()
        parts: list[str] = []
        first_delta_ms = 0
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                if isinstance(item, BaseException):
                    raise item
                if not first_delta_ms:
                    first_delta_ms = int((time.monotonic() - started) * 1000)
                parts.append(item)
                await self._send_json({"t": "llm_delta", "sid": self.sid, "text": item})
        finally:
            stop.set()  # 本轮取消/断连：让后台 SSE 线程停止继续读
        answer = "".join(parts).strip()
        self.stats.llm_rounds += 1
        self.stats.llm_first_ms = first_delta_ms
        self.stats.llm_chars += len(answer)
        self._log(
            logging.INFO,
            f"[LLM] {lcfg.model} 回答 {len(answer)} 字（首包 {first_delta_ms}ms / "
            f"总 {(time.monotonic() - started) * 1000:.0f}ms）：{answer[:40]!r}",
        )
        return answer

    def _open_tts_stream(self):
        """为流水线开一个多段连续 MP3 编码器（同一个编码器，段间无接缝）。"""
        module = self.server.tts_qwen_module
        assert module is not None
        cfg = self.cfg
        return module.Mp3SpeechStream(
            model=cfg.tts_model or module.DEFAULT_MODEL,
            voice=cfg.tts_voice or None,
            lang_code=cfg.tts_lang,
            instruct=cfg.tts_instruct,
            stream_interval=cfg.tts_stream_interval,
            log=lambda message: self._log(logging.INFO, message),
        )

    async def _begin_stream(self, sid: int | None, frames: list[tuple[bytes, float]],
                            prebuffer_ms: float) -> tuple[float, int, int]:
        """发 tts_start + 预缓冲帧（一次性灌给设备），之后由调用方按实时节奏发。

        预缓冲的目的：设备起播只攒 4 帧（96ms），而 TTS 段与段之间会有首块延迟；
        先给 800ms 音频，设备就有足够余量扛住段间抖动，不会一上来就 underrun。
        返回 (首声时刻, 预缓冲帧数, 预缓冲字节数)。
        """
        cfg = self.cfg
        if cfg.tts_late_bin:
            # tts_start 之前：端侧还没开 0x02 接收窗口，必须丢弃（T2.4 契约 §2.3）
            await self._send_bin(self._late_bin_payload(), note="late-pre")
        await self._send_tts_start(sid, TTS_CODEC)
        self._reset_pacing()
        total_bytes = 0
        for payload, _duration_ms in frames:
            await self._send_bin(payload)
            total_bytes += len(payload)
            self.stats.tts_frames += 1
            self.stats.tts_bytes += len(payload)
        # 预缓冲帧已经一次发完，后面第一帧从这里开始按帧时长节流
        self._pace_due = time.monotonic()
        now = time.monotonic()
        started_at = getattr(self, "_round_started_at", now)
        self.stats.tts_first_audio_ms = int((now - started_at) * 1000)
        self.stats.tts_prebuffer_ms = int(prebuffer_ms)
        self._log(
            logging.INFO,
            f"[TTS] 首声 +{now - started_at:.2f}s（预缓冲 {prebuffer_ms:.0f}ms / "
            f"{len(frames)} 帧；之后按实时节奏）",
        )
        return now, len(frames), total_bytes

    async def _llm_tts_stream_flow(self, sid: int | None, text: str) -> None:
        """LLM 流式输出 → 自然分句 → Qwen3-TTS 流式合成（边出字边播，2026-11-10）。

        旧路径 `_llm_answer()` 要等 LLM **整段**回答写完，才把整段交给 TTS；长回答时设备在
        THINKING 里干等。这里把 LLM 增量按标点切成语义段，第一段一到就交给 TTS；攒够
        `tts_prebuffer_ms` 音频就 `tts_start`，LLM 还在出后续句子时设备已经在说话。

        失败策略：LLM 没吐过内容 → 回退复读 ASR 文本（与旧行为一致）；TTS 中途失败 →
        已下发的音频保留，提前 `tts_end`，不拖垮协议链路。
        """
        cfg = self.cfg
        lcfg = self._llm_config()
        module = self.server.tts_qwen_module
        assert module is not None
        log = lambda message: self._log(logging.INFO, message)  # noqa: E731

        pipeline = LlmTtsPipeline(
            text=text,
            stream_llm=lambda: llm_client.stream_chat(text, lcfg),
            open_tts=self._open_tts_stream,
            config=PipelineConfig(
                prebuffer_ms=cfg.tts_prebuffer_ms,
                segmenter=SegmenterConfig(
                    first_min_chars=cfg.tts_seg_first_chars,
                    min_chars=cfg.tts_seg_min_chars,
                    max_chars=cfg.tts_seg_max_chars,
                    first_max_chars=cfg.tts_seg_first_max_chars,
                    idle_ms=cfg.tts_seg_idle_ms,
                    first_idle_ms=max(600, cfg.tts_seg_idle_ms + 150),
                ),
            ),
            log=log,
        )
        pipeline.start(asyncio.get_running_loop())
        self.stats.llm_stream_rounds += 1

        prebuffer: list[tuple[bytes, float]] = []
        prebuffer_ms = 0.0
        started = False
        sent_frames = 0
        sent_bytes = 0
        pace_index = 0
        first_audio_at: float | None = None
        llm_done_at: float | None = None
        first_delta_logged = False
        finished_answer = ""

        try:
            while True:
                event = await pipeline.events.get()
                kind = event[0]

                if kind == "delta":
                    if not first_delta_logged:
                        first_delta_logged = True
                        started_at = getattr(self, "_round_started_at", time.monotonic())
                        self._log(
                            logging.INFO,
                            f"[LLM] 首包 +{time.monotonic() - started_at:.2f}s（首个 llm_delta）",
                        )
                    await self._send_json({"t": "llm_delta", "sid": sid, "text": event[1]})

                elif kind == "llm_done":
                    finished_answer, first_ms, ok = event[1], event[2], event[3]
                    llm_done_at = time.monotonic()
                    self.stats.llm_first_ms = first_ms
                    self.stats.llm_chars += len(finished_answer)
                    if ok:
                        self.stats.llm_rounds += 1
                    started_at = getattr(self, "_round_started_at", llm_done_at)
                    self._log(
                        logging.INFO,
                        f"[LLM] {lcfg.model} 回答 {len(finished_answer)} 字"
                        f"（首包 {first_ms}ms / 总 {(llm_done_at - started_at) * 1000:.0f}ms）："
                        f"{finished_answer[:40]!r}",
                    )

                elif kind == "llm_error":
                    self.stats.llm_errors += 1
                    self._log(logging.WARNING, f"[LLM] 失败（回退/保留已生成部分）：{event[1]}")

                elif kind == "segment":
                    self.stats.tts_segments += 1
                    age = event[2] if len(event) > 2 else 0.0
                    self._log(
                        logging.INFO,
                        f"[TTS] 切段（{len(event[1])} 字，LLM 出字 +{age:.2f}s）：{event[1]!r}",
                    )
                    if not started and age > 8.0:
                        self._log(
                            logging.WARNING,
                            f"[TTS] 首段等了 {age:.1f}s，接近端侧 THINKING 10s 超时；"
                            "若真机先报 timeout，让 LLM 更快出首句或调小 --tts-seg-first-max-chars",
                        )

                elif kind == "tts_error":
                    self.stats.tts_qwen_errors += 1
                    self._log(logging.WARNING, f"[TTS] 合成失败（提前收尾）：{event[1]}")

                elif kind == "frame":
                    payload, duration_ms = event[1], event[2]
                    if not started:
                        prebuffer.append((payload, duration_ms))
                        prebuffer_ms += duration_ms
                        if prebuffer_ms + 1e-6 >= cfg.tts_prebuffer_ms:
                            first_audio_at, count, nbytes = await self._begin_stream(
                                sid, prebuffer, prebuffer_ms
                            )
                            sent_frames += count
                            sent_bytes += nbytes
                            prebuffer = []
                            prebuffer_ms = 0.0
                            started = True
                    else:
                        await self._send_bin(payload)
                        sent_frames += 1
                        sent_bytes += len(payload)
                        self.stats.tts_frames += 1
                        self.stats.tts_bytes += len(payload)
                        await self._pace_after_frame(pace_index, duration_ms)
                        pace_index += 1
                        if cfg.max_tts_frames > 0 and sent_frames >= cfg.max_tts_frames:
                            self._log(
                                logging.INFO,
                                f"达到 --max-tts-frames={cfg.max_tts_frames}，提前收尾",
                            )
                            break

                elif kind == "tts_done":
                    if not started:
                        first_audio_at, count, nbytes = await self._begin_stream(
                            sid, prebuffer, prebuffer_ms
                        )
                        sent_frames += count
                        sent_bytes += nbytes
                        prebuffer = []
                        prebuffer_ms = 0.0
                        started = True
                    break

        except asyncio.CancelledError:
            pipeline.stop()
            raise
        finally:
            pipeline.stop()

        if not started:
            # 没有任何音频帧（TTS 失败/文本为空）：也要让端侧走完 tts_start→tts_end，
            # 否则设备会一直停在 THINKING 等云端响应超时。
            first_audio_at, _, _ = await self._begin_stream(sid, [], 0.0)
        await self._send_json({"t": "tts_end", "sid": sid})
        if cfg.tts_late_bin:
            await self._send_bin(self._late_bin_payload(), note="late-post")

        if llm_done_at is not None and first_audio_at is not None:
            overlap_ms = int((llm_done_at - first_audio_at) * 1000)
            self.stats.tts_llm_overlap_ms = overlap_ms
            if overlap_ms > 0:
                self._log(
                    logging.INFO,
                    f"[TTS] 首声早于 LLM 完成 {overlap_ms}ms（流水线生效：LLM 还在出字，设备已开口）",
                )
            else:
                self._log(
                    logging.INFO,
                    f"[TTS] 首声晚于 LLM 完成 {-overlap_ms}ms（回答较短/LLM 很快）",
                )
        self._log(
            logging.INFO,
            f"tts 下发完成 frames={sent_frames} bytes={sent_bytes} mode=pipeline",
        )

    def _echo_frame_source(self, text: str):
        """回声测试的帧来源：默认**流式生成器**（边合成边下发），`--no-tts-stream` 时是整句帧列表。

        流式生成器由 `_send_echo_frames()` 放到独立线程里消费（MLX 生成不能占事件循环）；
        整句模式在这里同步合成完，便于排查/对比。
        """
        module = self.server.tts_qwen_module
        assert module is not None
        cfg = self.cfg
        common = {
            "model": cfg.tts_model or module.DEFAULT_MODEL,
            "voice": cfg.tts_voice or None,
            "lang_code": cfg.tts_lang,
            "instruct": cfg.tts_instruct,
            "stream_interval": cfg.tts_stream_interval,
        }
        log = lambda message: LOG.info("%s", message)  # noqa: E731
        if cfg.tts_stream:
            return module.stream_frames(text, **common, log=log)
        started = time.monotonic()
        audio, duration = module.synthesize_mp3(text, **common, log=log)
        frames = parse_mp3_frames(audio, source="<asr-echo>")
        if not frames:
            raise RuntimeError("Qwen3-TTS 合成结果不是可解析的 MP3")
        self._log(
            logging.INFO,
            f"[TTS] 复读合成：{text!r} → {len(frames)} 帧 / {duration:.2f}s 音频"
            f"（耗时 {time.monotonic() - started:.2f}s）",
        )
        return frames

    async def _send_echo_frames(self, sid: int | None, source) -> tuple[int, int]:
        """把回声帧来源发出去：列表直接发；生成器放线程里跑，边生成边按实时节奏发。"""
        limit = self.cfg.max_tts_frames
        sent_frames = 0
        sent_bytes = 0

        if isinstance(source, list):
            total = len(source) if limit <= 0 else min(len(source), limit)
            for index in range(total):
                frame = source[index]
                await self._send_bin(frame.payload)
                sent_frames += 1
                sent_bytes += len(frame.payload)
                self.stats.tts_frames += 1  # 逐帧记账：长 TTS 中途被取消时 stats 不再显示 0
                self.stats.tts_bytes += len(frame.payload)
                await self._pace_after_frame(index, frame.duration_ms)
            return sent_frames, sent_bytes

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        sentinel = object()
        stop = threading.Event()

        def emit(item) -> bool:
            """线程安全入队；已取消/事件循环关闭时返回 False，让合成线程停止继续生成。"""
            if stop.is_set():
                return False
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:  # 事件循环已关闭（断连/进程退出）
                return False
            return True

        def producer() -> None:
            try:
                for payload, duration_ms in source:
                    if not emit((payload, duration_ms)):
                        return
            except BaseException as exc:  # noqa: BLE001 - 异常要回传主线程，不能吞
                if not emit(exc):
                    return
            finally:
                emit(sentinel)

        thread = threading.Thread(target=producer, name="tts-qwen", daemon=True)
        thread.start()
        index = 0
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                if isinstance(item, BaseException):
                    raise item
                payload, duration_ms = item
                await self._send_bin(payload)
                sent_frames += 1
                sent_bytes += len(payload)
                self.stats.tts_frames += 1
                self.stats.tts_bytes += len(payload)
                await self._pace_after_frame(index, duration_ms)
                index += 1
                if limit > 0 and index >= limit:
                    break
        finally:
            stop.set()  # 取消/断连时停止后台合成（MLX 生成很贵，别白跑几十秒）
        return sent_frames, sent_bytes

    def _late_bin_payload(self) -> bytes:
        """窗口外注入用的 0x02 payload：优先用真实 MP3 首帧，否则合成帧。"""
        frames = self.server.tts_mp3_frames
        if frames:
            return frames[0].payload
        return self._synth_tts_payload(0)

    def _reset_pacing(self) -> None:
        """每一轮 TTS 下发开始时重置节奏基准（绝对时刻调度）。"""
        self._pace_due: float | None = None

    async def _pace_after_frame(self, index: int, frame_ms: float) -> None:
        """一帧发完后的节奏：**按绝对时刻调度**，平均速率严格等于帧时长。

        以前的实现是"发完再 sleep(帧时长)"，于是每帧实际周期 = 帧时长 + 发送/处理开销
        （实测 24ms 的帧变成 25.6ms → 比实时慢 6%）。短句看不出来，长句播放会越积越少，
        声卡中途开始欠载 → 听感"呲呲"。stall 点仍然按毫秒停发。
        """
        cfg = self.cfg
        if index == cfg.tts_stall_at and cfg.tts_stall_ms > 0:
            self.stats.tts_stalls += 1
            self._log(
                logging.INFO,
                f"stall {cfg.tts_stall_ms} ms（第 {index} 帧后停发，制造端侧欠载）",
            )
            await asyncio.sleep(cfg.tts_stall_ms / 1000)
            self._pace_due = None  # stall 之后重新对齐
            return
        gap_ms = frame_ms * cfg.tts_pace_pct / 100.0 if cfg.tts_pace_pct > 0 else 0.0
        if gap_ms <= 0:
            return
        now = time.monotonic()
        if getattr(self, "_pace_due", None) is None:
            self._pace_due = now
        self._pace_due += gap_ms / 1000.0
        delay = self._pace_due - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            # 已经落后（生成慢/宿主抖动）：不要回头补发，重新对齐到当前时刻
            self._pace_due = time.monotonic()

    async def _asr_text(self, asr_packets: list[bytes] | None) -> str:
        """本轮 asr 文本：--asr 时用本地 SenseVoice 真识别，否则用 --tts-text 固定文案。

        识别失败只 WARN 并回退固定文案，保证 mock 的协议链路测试不被 ASR 问题拖垮。
        推理放在线程里跑（ONNX 是同步阻塞的），不卡 WS 收包与心跳。
        """
        if self.server.asr is None:
            return self.cfg.tts_text
        packets = asr_packets or []
        if not packets:
            self.stats.asr_errors += 1
            self._log(logging.WARNING, "[ASR] 本轮没有上行音频帧（设备没出声？），回退 --tts-text")
            return self.cfg.tts_text
        try:
            started = time.monotonic()
            result = await asyncio.to_thread(self.server.asr.transcribe_opus, packets)
            self.stats.asr_rounds += 1
            self.stats.asr_audio_ms += int(result["audio_seconds"] * 1000)
            self.stats.asr_infer_ms += int(result["inference_seconds"] * 1000)
            self._log(
                logging.INFO,
                f"[ASR] 识别结果: {result['text']!r}"
                f"（{len(packets)} 帧 / 音频 {result['audio_seconds']:.2f}s / "
                f"推理 {result['inference_seconds']:.2f}s / 总耗时 {time.monotonic() - started:.2f}s）",
            )
            if not result["text"]:
                self._log(logging.WARNING, "[ASR] 未识别到文字，回退 --tts-text")
                return self.cfg.tts_text
            # SenseVoice 在"整段只有静音/噪声"时常返回 "." 这类纯标点：那不是一句话，
            # 当成没听到（否则复读模式会拿标点去合成、piper 直接报错）。
            if not module_speakable(result["text"]):
                self._log(
                    logging.WARNING,
                    f"[ASR] 只识别到标点 {result['text']!r}（整段没听到语音）→ 回退 --tts-text；"
                    "若确实说了话，检查麦克风电平（设备上 `mic stat`）与说话距离",
                )
                return self.cfg.tts_text
            return result["text"]
        except Exception as exc:  # noqa: BLE001 - 单轮识别失败不能拖垮 mock
            self.stats.asr_errors += 1
            self._log(logging.WARNING, f"[ASR] 识别失败（回退 --tts-text）：{exc}")
            return self.cfg.tts_text

    async def _send_tts_start(self, sid: int | None, codec: str) -> None:
        """下发 tts_start（codec/sr 按参数：正常 mp3，--tts-bad-codec 时故意用 pcm）。"""
        await self._send_json(
            {"t": "tts_start", "sid": sid, "codec": codec, "sr": TTS_SAMPLE_RATE}
        )

    async def _error_flow(self, sid: int | None) -> None:
        """--scenario error：按 --error-stage 决定报错时机，再下发 error 事件（T3.6）。

        三个时机覆盖三条端侧路径：
          - after-audio-end（默认，T2.5 现状）：LISTENING 结束即报错 → VS 从 THINKING 进 ERROR；
          - after-asr：先发 asr_final 再报错（ASR 之后服务失败）；
          - after-tts-start：先发 tts_start + 若干**真实 MP3 帧**再报错 → 设备已进 SPEAKING，
            用于验证「SPEAKING 中云端报错」时先停 TTS 流再播错误提示音、无叠音。
        error.code 由 --error-code 控制（3001/3002 会映射到 tts-fail 提示音）。
        """
        cfg = self.cfg
        if cfg.error_stage == "after-asr":
            await self._send_json({"t": "asr_final", "sid": sid, "text": cfg.tts_text})
        elif cfg.error_stage == "after-tts-start":
            await self._send_tts_start(sid, TTS_CODEC)
            frames = self.server.error_audio_frames()
            total = min(ERROR_TTS_FRAMES, len(frames))
            for index in range(total):
                frame = frames[index]
                await self._send_bin(frame.payload)
                await self._pace_after_frame(index, frame.duration_ms)
            self._log(
                logging.INFO,
                f"--error-stage after-tts-start：已下发 {total} 帧真实 MP3 "
                f"（{frames[0].sample_rate if frames else 0} Hz）后报错",
            )

        self.stats.errors += 1
        await self._send_json(
            {
                "t": "error",
                "sid": sid,
                "code": cfg.error_code,
                "msg": f"mock: 模拟错误（code={cfg.error_code}）",
            }
        )
        self._log(
            logging.WARNING,
            f"error 阶段={cfg.error_stage} code={cfg.error_code}"
            "（码表见 PROTOCOL.md §4：3001/3002=TTS 失败，其余=通用/服务异常）",
        )

    # ---- 音乐：LLM 工具调用（Responses API / MCP），不再有关键词规则表 ----

    def _responses_config(self) -> responses_client.ResponsesConfig:
        cfg = self.cfg
        return responses_client.ResponsesConfig.from_env(
            base_url=cfg.llm_base_url or None,
            api_key=cfg.llm_api_key or None,
            model=cfg.llm_model or None,
        )

    async def _apply_music_command(self, command: Any) -> None:
        """把执行器返回的命令变成设备事件（play/next/prev → track；pause/resume/stop → control）。"""
        if not command.changed:
            self._log(logging.DEBUG,
                      f"[MUSIC] {command.action} 无状态变化（{command.message}），不下发事件")
            return
        if command.track is not None and command.action in (ACTION_PLAY, ACTION_NEXT, ACTION_PREV):
            self.music_mid = (self.music_mid + 1) & 0xFFFF
            await self._send_json(command.track.as_event(self.music_mid))
            self.stats.music_tracks += 1
            self.server.music.last_url = command.track.url
            self._log(logging.INFO,
                      f"[MUSIC] 工具 {command.action} → music_track mid={self.music_mid} "
                      f"《{command.track.title}》")
            return
        if command.action in CONTROL_ACTIONS:
            await self._send_json(build_control_event(self.music_mid, command.action))
            self.stats.music_controls += 1
            self._log(logging.INFO,
                      f"[MUSIC] 工具 {command.action} → music_control mid={self.music_mid}")
            return
        self._log(logging.DEBUG, f"[MUSIC] 工具 {command.action} 不需要下发事件")

    async def _music_flow(self, text: str) -> tuple[bool, str | None]:
        """ASR 文本 → Responses API → 工具调用 → 设备事件 + 模板确认语。

        返回 `(handled, answer)`：
          - `handled=True`：已执行音乐动作，`answer` 是固定确认语（不再走 LLM 复述）；
          - `handled=False, answer=None`：模型没给文本也没调工具（调用方照旧走 LLM/复读）；
          - `handled=False, answer="..."`：模型给了普通回答（本轮已调过模型，不要再调一次）。
        """
        music = self.server.music
        session = getattr(self, "music_session", None)
        if music is None or session is None:
            return False, None

        cfg = self.cfg
        mode = getattr(cfg, "music_tool_mode", "mcp")
        try:
            rcfg = self._responses_config()
            rcfg.require_ready()
        except Exception as exc:  # noqa: BLE001 - 缺配置只降级，不拖垮语音链路
            if not getattr(self, "_music_llm_warned", False):
                self._music_llm_warned = True
                self._log(logging.WARNING,
                          f"[MUSIC] 未配置 Responses API（{exc}）；本轮音乐不执行"
                          "（export LLM_BASE_URL / LLM_MODEL / LLM_API_KEY）")
            self.stats.music_llm_errors += 1
            return False, None

        tools = music_tools.tool_definitions() if mode == "inline" else None
        # 基础人设（--llm-system / 默认语音助手）+ 音乐工具规则；音乐轮外也走同一次调用，
        # 避免「开了 --music 之后普通问答失去助手口径」。
        base_system = (cfg.llm_system or "").strip() or llm_client.DEFAULT_SYSTEM
        system = base_system + "\n\n" + music_tools.system_prompt_for(session)
        try:
            result = await asyncio.to_thread(
                responses_client.responses_call, text, rcfg,
                system=system, tools=tools, max_output_tokens=256,
            )
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
            self.stats.music_llm_errors += 1
            self._log(logging.WARNING, f"[MUSIC] Responses 调用失败（本轮不执行音乐）：{exc}")
            return False, None

        calls = responses_client.parse_tool_calls(result)
        if not calls:
            answer = result.text.strip()
            self._log(logging.INFO,
                      f"[MUSIC] 模型未调用音乐工具（mode={mode}）"
                      + (f"，回答：{answer[:40]!r}" if answer else ""))
            return False, (answer or None)

        handled = False
        confirm: str | None = None
        for call in calls:
            canonical = music_tools.canonical_tool_name(call.name)
            if canonical not in music_tools.TOOL_NAMES:
                # 网关可能夹带其它 MCP 工具（日历/文件系统…）：静默跳过，不计数
                self._log(logging.DEBUG, f"[MUSIC] 跳过非音乐工具 {call.name!r}")
                continue
            self.stats.music_intents += 1
            command = await asyncio.to_thread(
                music_tools.execute_tool, session, canonical, call.arguments
            )
            self._log(logging.INFO,
                      f"[MUSIC] tool={call.name} args={call.arguments} → "
                      f"ok={command.ok} state={command.state} {command.message}")
            if not command.ok:
                continue
            if command.action in ("search", "status"):
                continue  # 只读工具：不下发设备事件
            if handled:
                self._log(logging.DEBUG,
                          f"[MUSIC] 一轮只执行第一个动作，忽略 {command.action}")
                continue
            await self._apply_music_command(command)
            handled = True
            confirm = music.confirm_text(command.action, command.track)

        if handled:
            return True, confirm
        # 只调了 search/status：用模型文本，没有文本就给一句候选提示
        answer = result.text.strip()
        if not answer and session.queue:
            titles = "、".join(t.title for t in session.queue[:3])
            answer = f"找到 {len(session.queue)} 首：{titles}。"
        return False, (answer or None)

    async def _tts_flow(self, asr_packets: list[bytes] | None = None) -> None:
        sid = self.sid
        self._round_started_at = time.monotonic()
        try:
            if self.cfg.think_ms > 0:
                await asyncio.sleep(self.cfg.think_ms / 1000)

            # 迟到帧注入：端侧必须丢弃 sid 不匹配的事件（ARCH §6.3 / PROTOCOL P-2）
            if self.cfg.inject_late:
                late = 1 if sid is None else (sid + 1) & 0xFFFF
                await self._send_json(
                    {"t": "asr_final", "sid": late, "text": "迟到帧（端侧应丢弃）"}
                )
                self._log(
                    logging.INFO,
                    f"已注入迟到事件 asr_final sid={late}（当前轮 sid={sid}，端侧应丢弃并计数）",
                )

            if self.cfg.scenario == "error":
                await self._error_flow(sid)
                return

            text = await self._asr_text(asr_packets)
            await self._send_json({"t": "asr_partial", "sid": sid, "text": text[: max(1, len(text) // 2)]})
            await self._send_json({"t": "asr_final", "sid": sid, "text": text})

            # 2026-11-12：音乐意图交给 LLM 工具调用（Responses API / MCP）。
            # 命中后直接下发 music_track / music_control，并用固定确认语（不二次调模型）。
            music_handled, music_answer = await self._music_flow(text)

            # LLM 流式 → TTS 流水线（2026-11-10，默认路径）：
            # 不等 LLM 整段回答，切出第一段就开始合成/下发；回答较长时首声明显提前。
            # 默认走旧路径（整段回答后合成，语音更连贯）；`--llm-tts-stream` 才启用流水线。
            if (
                music_answer is None           # 本轮已调过 Responses（含音乐/普通回答）：不再重复调模型
                and not music_handled
                and self.cfg.llm
                and self.cfg.llm_tts_stream
                and self.server.tts_qwen_module is not None
                and self.cfg.tts_stream
                and not self.cfg.no_tts
                and not self.cfg.tts_bad_codec
            ):
                await self._llm_tts_stream_flow(sid, text)
                return

            # 回答来源：音乐确认语（M1）/ --llm（大模型流式）/ 复读（默认，llm_delta 是识别文本切片）
            if music_handled:
                self._log(logging.INFO, "[MUSIC] 音乐动作已执行，跳过 LLM 复述")
            answer = text
            if music_answer is not None:
                answer = music_answer
                await self._send_json({"t": "llm_delta", "sid": sid, "text": answer})
            elif self.cfg.llm:
                try:
                    answer = await self._llm_answer(text) or text
                except BaseException as exc:  # noqa: BLE001 - 单轮失败不能拖垮 mock
                    if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                        raise
                    self.stats.llm_errors += 1
                    answer = text
                    self._log(logging.WARNING, f"[LLM] 失败（回退复读 ASR 文本）：{exc}")
                    await self._send_json({"t": "llm_delta", "sid": sid, "text": text})
            else:
                for offset in range(0, len(text), 5):
                    await self._send_json(
                        {"t": "llm_delta", "sid": sid, "text": text[offset : offset + 5]}
                    )

            # TTS：把回答（--llm 时是模型回答，否则是识别文本）用本地 Qwen3-TTS 合成
            echo_source = None
            if self.server.tts_qwen_module is not None:
                if not module_speakable(answer):
                    self._log(
                        logging.WARNING,
                        f"[TTS] 文本没有可念内容（{answer!r}），跳过合成 → 回退合成帧",
                    )
                else:
                    try:
                        echo_source = self._echo_frame_source(answer)
                    except BaseException as exc:  # noqa: BLE001
                        # 必须是 BaseException：合成链路可能抛 SystemExit，`except Exception`
                        # 抓不住它，会让整个 mock 进程退出（2026-11-02 实测踩到）。
                        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                            raise
                        self.stats.tts_qwen_errors += 1
                        self._log(logging.WARNING, f"[TTS] 复读合成失败（回退合成帧）：{exc}")

            sent_frames = 0
            sent_bytes = 0
            if self.cfg.tts_bad_codec:
                # T3.6-F：tts_start 用设备无法播放的 codec（"pcm"）后立即 tts_end、不发音频帧。
                # 端侧应上报 VC_EVT_TTS_FAIL → feedback 播「语音播放失败」，而不是静默失败。
                await self._send_tts_start(sid, TTS_BAD_CODEC)
                self._log(
                    logging.WARNING,
                    f"--tts-bad-codec：tts_start codec={TTS_BAD_CODEC} 且不发音频帧"
                    "（设备侧应报「无法播放」→ VC_EVT_TTS_FAIL → tts-fail 提示音）",
                )
                await self._send_json({"t": "tts_end", "sid": sid})
                self._log(logging.INFO, "tts 下发完成 frames=0 bytes=0 mode=bad-codec")
                return

            self._reset_pacing()
            if self.cfg.no_tts:
                await self._send_tts_start(sid, TTS_CODEC)
                self._log(logging.INFO, "（--no-tts：跳过二进制帧，仅验证 JSON 链路）")
            else:
                if self.cfg.tts_late_bin:
                    # tts_start 之前：端侧还没开 0x02 接收窗口，必须丢弃（T2.4 契约 §2.3）
                    await self._send_bin(self._late_bin_payload(), note="late-pre")
                await self._send_tts_start(sid, TTS_CODEC)
                if echo_source is not None:
                    try:
                        sent_frames, sent_bytes = await self._send_echo_frames(sid, echo_source)
                    except BaseException as exc:  # noqa: BLE001
                        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                            raise
                        self.stats.tts_qwen_errors += 1
                        self._log(logging.WARNING, f"[TTS] 复读合成中断（本轮提前 tts_end）：{exc}")
                elif self.server.tts_mp3_frames is None:
                    total = max(1, self.cfg.max_tts_frames)
                    for index in range(total):
                        payload = self._synth_tts_payload(index)
                        await self._send_bin(payload)
                        sent_frames += 1
                        sent_bytes += len(payload)
                        self.stats.tts_frames += 1
                        self.stats.tts_bytes += len(payload)
                        await self._pace_after_frame(index, self.cfg.chunk_ms)
                else:
                    frames = self.server.tts_mp3_frames
                    total = len(frames) if self.cfg.max_tts_frames <= 0 else min(
                        len(frames), self.cfg.max_tts_frames
                    )
                    for index in range(total):
                        frame = frames[index]
                        await self._send_bin(frame.payload)
                        sent_frames += 1
                        sent_bytes += len(frame.payload)
                        self.stats.tts_frames += 1
                        self.stats.tts_bytes += len(frame.payload)
                        await self._pace_after_frame(index, frame.duration_ms)
                    if self.cfg.tts_stall_ms > 0 and self.cfg.tts_stall_at >= total:
                        self._log(
                            logging.WARNING,
                            f"--tts-stall-at={self.cfg.tts_stall_at} 超出本轮帧数 {total}，stall 未触发",
                        )
            await self._send_json({"t": "tts_end", "sid": sid})
            if not self.cfg.no_tts and self.cfg.tts_late_bin:
                # tts_end 之后：端侧窗口已关闭，必须丢弃（不计数、不播放）
                await self._send_bin(self._late_bin_payload(), note="late-post")
            mode = "mp3" if (echo_source is not None or self.server.tts_mp3_frames is not None) else "synth"
            if not self.cfg.no_tts:
                # tts_frames/tts_bytes 已在逐帧下发时记账（取消时也能看到真实进度）
                self._log(
                    logging.INFO,
                    f"tts 下发完成 frames={sent_frames} bytes={sent_bytes} mode={mode}",
                )

        except asyncio.CancelledError:
            self.stats.tts_cancelled += 1
            self._log(logging.INFO, f"下发被取消 sid={sid}（interrupt / 新轮次 / 断连）")
            raise
        except ConnectionClosed:
            self._log(logging.INFO, "连接已断开，结束本轮下发")
        finally:
            self.tts_task = None
