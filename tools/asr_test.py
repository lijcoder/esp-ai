#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "av>=12",
#   "numpy>=1.24",
#   "sherpa-onnx>=1.10",
#   "sounddevice>=0.5",
#   "soundfile>=0.13",
# ]
# ///
"""asr_test.py —— 离线 ASR 模块测试：说话/给音频 → 文字（SenseVoiceSmall）

**不依赖开发板、不依赖 mock 服务**：直接用 Mac 麦克风录音（或读一个音频文件），跑
SenseVoiceSmall 识别，打印文本与耗时。用来验证 ASR 模块本身：能不能听清、识别得对不对、
噪声/语速下表现如何。

识别模型与 mock 的 `--asr` 完全同一份（`tools/mock_asr.py` 的 `MockAsr`）：SenseVoiceSmall
（sherpa-onnx / ONNX Runtime，纯 CPU），模型按优先级找：
  1. `--model-dir` / 环境变量 `MOCK_ASR_MODEL_DIR`
  2. `~/.cache/esp-ai-mock-asr/sensevoice`
  3. `~/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx`（本机已有缓存时复用）
缺模型时加 `--download`（约 239MB，固定 revision + SHA-256 校验）。

用法：
  # 录音 5 秒 → 识别（默认从系统默认麦克风）
  uv run tools/asr_test.py

  # 指定时长/设备（设备号见 --list-devices）
  uv run tools/asr_test.py --seconds 8 --device 2

  # 不录音，直接识别已有文件（wav/mp3/aiff，解码用 av）
  uv run tools/asr_test.py --file tools/今天天气如何.wav

  # 走 mock 的同一条入口：先编成 Opus 包再 transcribe_opus（与设备上行一致）
  uv run tools/asr_test.py --file tools/今天天气如何.wav --via-opus --check

  # 只准备模型 / 看设备
  uv run tools/asr_test.py --download
  uv run tools/asr_test.py --list-devices

注意（macOS）：首次运行需要给终端/宿主 App **麦克风权限**，否则录到的是全零。脚本会打印
录音峰值与 RMS，峰值 < -50 dBFS 时会提示“可能没权限/没声音”。

依赖用 PEP 723 内联声明，`uv run` 即用；识别耗时约 0.1~0.5s（1~3s 语音）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000  # SenseVoice 固定输入
RECORD_RATE = 48000  # Mac 声卡默认，录完重采样到 16k

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_asr import MockAsr, decode_audio_file  # noqa: E402


# ---------------------------------------------------------------- 采集


def resample(x: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    if src_rate == dst_rate or x.size == 0:
        return x.astype(np.float32)
    n_out = int(round(x.size * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    pos = np.arange(n_out) * (src_rate / dst_rate)
    return np.interp(pos, np.arange(x.size), x).astype(np.float32)


def list_devices() -> None:
    import sounddevice as sd

    print("输入设备（max_input_channels > 0）：")
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            default = "*" if idx == sd.default.device[0] else " "
            print(f" {default}{idx:2d}  {dev['name']}  ch={dev['max_input_channels']} "
                  f"sr={int(dev['default_samplerate'])}")
    print(f"默认输入设备：{sd.default.device[0]}")


def record(seconds: float, device: int | None) -> tuple[np.ndarray, int]:
    """录一段音频，返回 (float32 PCM，原始采样率)。"""
    import sounddevice as sd

    device = sd.default.device[0] if device is None else device
    info = sd.query_devices(device)
    rate = int(info["default_samplerate"])
    frames = int(round(seconds * rate))
    print(f"[ASR] 录音 {seconds:.1f}s（设备 {device}: {info['name']} @ {rate} Hz）... 请说话", flush=True)
    started = time.monotonic()
    rec = sd.rec(frames, samplerate=rate, channels=1, dtype="float32", device=device)
    sd.wait()
    pcm = rec.reshape(-1).astype(np.float32)
    print(f"[ASR] 采集完成（{time.monotonic() - started:.2f}s，{pcm.size} 样本）", flush=True)
    return pcm, rate


def encode_opus_packets(pcm: np.ndarray, rate: int = SAMPLE_RATE, bitrate: int = 16000) -> list[bytes]:
    """把 16k 单声道 float32 PCM 编成裸 Opus 包（20ms/包），模拟设备上行 0x01 payload。"""
    import av

    int16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate = rate
    enc.layout = "mono"
    enc.format = "s16"
    enc.bit_rate = bitrate
    step = rate // 50  # 20ms
    packets: list[bytes] = []
    for start in range(0, int16.size, step):
        chunk = int16[start : start + step]
        if chunk.size < step:
            chunk = np.pad(chunk, (0, step - chunk.size))
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        packets.extend(bytes(p) for p in enc.encode(frame))
    packets.extend(bytes(p) for p in enc.encode(None))
    return packets


def level_report(pcm: np.ndarray, rate: int) -> float:
    peak = float(np.abs(pcm).max()) if pcm.size else 0.0
    rms = float(np.sqrt((pcm.astype(np.float64) ** 2).mean())) if pcm.size else 0.0
    peak_db = 20.0 * np.log10(max(peak, 1e-9))
    rms_db = 20.0 * np.log10(max(rms, 1e-9))
    print(f"[ASR] 电平：峰值 {peak_db:.1f} dBFS / RMS {rms_db:.1f} dBFS（{pcm.size / rate:.2f}s）",
          flush=True)
    if peak_db < -50.0:
        print("[ASR] ⚠️ 峰值很低：可能没给麦克风权限、或没对着麦克风说话", flush=True)
    return peak_db


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="离线 ASR 模块测试：麦克风/音频文件 → 文字（SenseVoiceSmall）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--seconds", type=float, default=5.0, help="录音时长（秒）")
    p.add_argument("--device", type=int, default=None, help="输入设备号（见 --list-devices）")
    p.add_argument("--file", default="", help="不录音，直接识别该音频文件（wav/mp3/aiff…）")
    p.add_argument("--out", default="", help="把录音存成 WAV（16k 单声道），便于复现")
    p.add_argument("--model-dir", default="", help="SenseVoice 模型目录（默认自动查找缓存）")
    p.add_argument("--language", default="auto", help="识别语言：auto/zh/en/ja/ko/yue…")
    p.add_argument("--threads", type=int, default=2, help="ONNX Runtime 线程数")
    p.add_argument("--download", action="store_true", help="模型缺失时下载（约 239MB）")
    p.add_argument("--via-opus", action="store_true",
                   help="先把 16k PCM 编成 Opus 包再 transcribe_opus（与 mock/设备上行同一个入口）")
    p.add_argument("--check", action="store_true",
                   help="断言识别非空（配合 --via-opus 可验证 mock 的完整 ASR 入口），退出码 0/1")
    p.add_argument("--list-devices", action="store_true", help="列出输入设备后退出")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_devices:
        list_devices()
        return 0

    if args.download and not args.file:
        MockAsr(args.model_dir or None, download=True, language=args.language, log=print)
        print("[ASR] 模型已就绪（未给音频，退出）", flush=True)
        return 0

    asr = MockAsr(
        args.model_dir or None,
        download=args.download,
        threads=args.threads,
        language=args.language,
        log=print,
    )

    if args.file:
        path = args.file
        if not Path(path).expanduser().is_file():
            raise SystemExit(f"文件不存在：{path}")
        pcm = decode_audio_file(path, sample_rate=SAMPLE_RATE)
        rate = SAMPLE_RATE
        level_report(pcm, rate)
        source = path
    else:
        raw, rec_rate = record(args.seconds, args.device)
        level_report(raw, rec_rate)
        pcm = resample(raw, rec_rate)
        rate = SAMPLE_RATE
        source = f"麦克风 {args.seconds:.1f}s"
        if args.out:
            import wave

            out = str(Path(args.out).expanduser())
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            with wave.open(out, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes((np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes())
            print(f"[ASR] 录音已保存：{out}", flush=True)

    started = time.monotonic()
    if args.via_opus:
        packets = encode_opus_packets(pcm)
        result = asr.transcribe_opus(packets)  # 与 mock 的 _asr_text() 同一个入口
        source = f"{source}（Opus {len(packets)} 包）"
    else:
        result = asr.transcribe_pcm(pcm)
    wall = time.monotonic() - started
    text = result["text"]
    print("=" * 60)
    print(f"来源：{source}")
    print(f"识别：{text or '（空：没听到可识别内容）'}")
    print(f"耗时：推理 {result['inference_seconds']:.2f}s / 总计 {wall:.2f}s"
          f"（音频 {result['audio_seconds']:.2f}s）")
    print("=" * 60)
    if args.check:
        if text:
            print("[CHECK] PASS：识别非空", flush=True)
        else:
            print("[CHECK] FAIL：识别为空", flush=True)
    return 0 if text else 1


if __name__ == "__main__":
    raise SystemExit(main())
