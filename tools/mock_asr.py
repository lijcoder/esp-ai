#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["av>=12", "numpy>=1.24", "sherpa-onnx>=1.10"]
# ///
"""
mock_asr.py —— mock 服务的本地真 ASR 后端（独立实现，不依赖 DSH）

为什么需要：T3.1 起 mock 默认用固定文案冒充 ASR（`--tts-text`），设备联调只能验证链路，
无法验证"识别结果"。本模块把设备上行的 Opus 帧解码成 16 kHz 单声道 PCM，再用
**SenseVoiceSmall（sherpa-onnx / ONNX Runtime，纯 CPU）** 做离线识别，返回文本。

独立性（重要）：
  - 不 import DSH 任何代码或包；模型文件可复用本机缓存，也可按固定 revision + SHA-256 下载；
  - 本文件可单独运行（自带 PEP 723 依赖声明），用于离线验证识别效果；
  - `ws_mock_server.py` 只在 `--asr` 时延迟 import 本模块，默认假 ASR 路径保持零额外依赖。

用法（模型只在首次需要下载 / 复用缓存）：
  uv run tools/mock_asr.py --test tools/今天天气如何.wav        # 直接识别 WAV
  uv run tools/mock_asr.py --test-opus /tmp/frames              # 识别一个目录里的 Opus 帧（按文件名排序）
  uv run tools/mock_asr.py --download                           # 只下载模型到缓存目录
  uv run tools/mock_asr.py --model-dir /path/to/sensevoice --threads 4 --test x.wav

接入 mock（一条命令；依赖通过 --with 临时装进 uv 环境）：
  uv run --with av --with numpy --with sherpa-onnx tools/ws_mock_server.py --asr sensevoice

模型缓存位置（按优先级）：
  1. `--model-dir` / 环境变量 `MOCK_ASR_MODEL_DIR`
  2. `~/.cache/esp-ai-mock-asr/sensevoice`（本模块下载目录）
  3. `~/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx`（本机已有 DSH 模型缓存时复用，纯数据复用）

已知限制：
  - 离线识别，**没有中间结果**（mock 只发 asr_final/asr_partial=前一半文本，不模拟流式 ASR）；
  - SenseVoice 输入固定 16 kHz 单声道；设备上行 Opus 原生解码 48 kHz，这里统一重采样；
  - 识别耗时随音频长度线性增长（单轮 1~3 s 语音在 CI 机器上约 0.2~0.8 s）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000

# 固定 revision（与 DSH 使用的 revision 相同，保证同一份权重）；下载前校验大小与 SHA-256。
_REVISION = "2365baeacb507f821a0c8120fcee3d484dba7a07"
_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
_SOURCES = (
    f"https://huggingface.co/{_REPO}/resolve/{_REVISION}",
    f"https://hf-mirror.com/{_REPO}/resolve/{_REVISION}",
)

# (文件名, 下载 URL 后缀, 字节数, sha256)
_FILES = (
    (
        "model.int8.onnx",
        "model.int8.onnx",
        239233841,
        "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
    ),
    (
        "tokens.txt",
        "tokens.txt",
        315894,
        "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
    ),
)

_MODEL_FILE = "model.int8.onnx"
_TOKENS_FILE = "tokens.txt"


# ---------------------------------------------------------------- 模型定位与下载


def cache_dir() -> Path:
    """本模块自己的模型缓存目录（XDG 规范；不写进仓库）。"""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "esp-ai-mock-asr" / "sensevoice"


def legacy_dirs() -> list[Path]:
    """可复用其它工具已下载的模型（纯数据复用，不产生代码依赖）。"""
    return [Path.home() / ".dsh" / "speech-to-text" / "sensevoice" / "models" / "sensevoice-onnx"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_ok(path: Path, size: int, sha: str) -> bool:
    try:
        return path.is_file() and path.stat().st_size == size and _sha256(path) == sha
    except OSError:
        return False


def resolve_model_dir(model_dir: str | os.PathLike[str] | None = None) -> Path | None:
    """按优先级找一个同时含模型与词表的目录；找不到返回 None。"""
    explicit = model_dir or os.environ.get("MOCK_ASR_MODEL_DIR")
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates.append(cache_dir())
    candidates.extend(legacy_dirs())
    for directory in candidates:
        if (directory / _MODEL_FILE).is_file() and (directory / _TOKENS_FILE).is_file():
            return directory
    return None


def _download_file(url: str, dest: Path, size: int, sha: str, log) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    log(f"[ASR] 下载 {url.split('/')[-1]}（{size / (1 << 20):.0f} MB）...")
    with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as handle:
        done = 0
        next_report = 32 << 20
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
            done += len(chunk)
            if done >= next_report:
                percent = done * 100 / size if size else 0
                log(f"[ASR]   已下载 {done / (1 << 20):.0f} MB（{percent:.0f}%）")
                next_report += 32 << 20
    if tmp.stat().st_size != size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载大小不符：{tmp.name} 期望 {size}B 实收 {tmp.stat().st_size if tmp.exists() else 0}B")
    actual = _sha256(tmp)
    if actual != sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载校验失败：{tmp.name}（sha256 {actual}）")
    os.replace(tmp, dest)
    log(f"[ASR] 已完成 {dest}")


def ensure_model(
    model_dir: str | os.PathLike[str] | None = None,
    *,
    download: bool = False,
    log=print,
) -> Path:
    """返回可直接加载的模型目录；缺失时按需下载并校验。"""
    explicit = model_dir or os.environ.get("MOCK_ASR_MODEL_DIR")
    target = Path(explicit).expanduser() if explicit else cache_dir()
    found = resolve_model_dir(model_dir)
    if found is not None:
        return found

    if explicit:
        raise RuntimeError(
            f"模型目录不完整：{target}（需要 {_MODEL_FILE} + {_TOKENS_FILE}）"
        )
    if not download:
        raise RuntimeError(
            "未找到 SenseVoice 模型。任选一种方式准备：\n"
            "  uv run tools/mock_asr.py --download    # 下载到 ~/.cache/esp-ai-mock-asr/sensevoice\n"
            "  uv run tools/mock_asr.py --model-dir /path/to/sensevoice"
        )

    target.mkdir(parents=True, exist_ok=True)
    for name, suffix, size, sha in _FILES:
        dest = target / name
        if _file_ok(dest, size, sha):
            log(f"[ASR] 复用已校验文件 {dest}")
            continue
        last_error: Exception | None = None
        for base in _SOURCES:
            try:
                _download_file(f"{base}/{suffix}", dest, size, sha, log)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - 换源重试，最后统一报错
                last_error = exc
                log(f"[ASR] 源下载失败（{base.split('/')[2]}）：{exc}")
        if last_error is not None:
            raise RuntimeError(f"模型下载失败：{name}（{last_error}）") from last_error
    return target


# ---------------------------------------------------------------- 音频解码


def _resample_chunks(chunks: list[np.ndarray]) -> np.ndarray:
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    pcm = np.concatenate(chunks).astype(np.float32, copy=False)
    return np.clip(pcm, -1.0, 1.0)


def decode_opus_packets(packets: list[bytes], sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """把一组裸 Opus 包（设备上行 0x01 的 payload）解码成 16 kHz 单声道 float32。"""
    import av  # 延迟 import：只有真正解码时才需要 av

    if not packets:
        return np.zeros(0, dtype=np.float32)
    decoder = av.CodecContext.create("opus", "r")
    resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)
    chunks: list[np.ndarray] = []

    def collect(frames) -> None:
        for frame in frames:
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))

    for payload in packets:
        collect(decoder.decode(av.Packet(payload)))
    collect(decoder.decode(None))
    for out in resampler.resample(None):
        chunks.append(out.to_ndarray().reshape(-1))
    return _resample_chunks(chunks)


def decode_audio_file(path: str | os.PathLike[str], sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """解码任意 WAV（16k/24k/48k，单/双声道）到 16 kHz 单声道 float32。"""
    import av

    container = av.open(str(path))
    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise RuntimeError(f"没有音频流：{path}")
        resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)
        chunks: list[np.ndarray] = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
        return _resample_chunks(chunks)
    finally:
        container.close()


# ---------------------------------------------------------------- 识别器


class MockAsr:
    """SenseVoiceSmall 离线识别器：mock 的 `--asr` 后端，也可单独用于离线验证。"""

    def __init__(
        self,
        model_dir: str | os.PathLike[str] | None = None,
        *,
        download: bool = False,
        threads: int = 2,
        language: str = "auto",
        log=print,
    ) -> None:
        import sherpa_onnx  # 延迟 import：没有 --asr 时不装/不加载

        self.model_dir = ensure_model(model_dir, download=download, log=log)
        self.language = language
        self.threads = threads
        log(f"[ASR] 加载 SenseVoice（{self.model_dir}，threads={threads}，language={language}）...")
        started = time.monotonic()
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(self.model_dir / _MODEL_FILE),
            tokens=str(self.model_dir / _TOKENS_FILE),
            num_threads=threads,
            use_itn=True,
            language=language,
            debug=False,
        )
        log(f"[ASR] 就绪（加载 {time.monotonic() - started:.2f}s）")

    def transcribe_pcm(self, pcm: np.ndarray) -> dict:
        """识别 16 kHz 单声道 float32 PCM。"""
        started = time.monotonic()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, pcm)
        self._recognizer.decode_stream(stream)
        text = (stream.result.text or "").strip()
        return {
            "text": text,
            "audio_seconds": len(pcm) / SAMPLE_RATE,
            "inference_seconds": time.monotonic() - started,
        }

    def transcribe_opus(self, packets: list[bytes]) -> dict:
        """识别设备上行的 Opus 包（先解码/重采样，再识别）。"""
        pcm = decode_opus_packets(packets)
        if pcm.size == 0:
            raise RuntimeError("Opus 解码后没有音频（上行帧为空或坏帧）")
        result = self.transcribe_pcm(pcm)
        result["frames"] = len(packets)
        return result

    def transcribe_wav(self, path: str | os.PathLike[str]) -> dict:
        pcm = decode_audio_file(path)
        if pcm.size == 0:
            raise RuntimeError(f"WAV 没有音频：{path}")
        result = self.transcribe_pcm(pcm)
        result["path"] = str(path)
        return result


# ---------------------------------------------------------------- CLI


def _print_result(result: dict) -> None:
    source = result.get("path") or f"{result.get('frames')} 个 Opus 帧"
    print(
        f"{source} -> {result['text']!r}"
        f"（音频 {result['audio_seconds']:.2f}s / 推理 {result['inference_seconds']:.2f}s）",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="mock 服务的本地真 ASR（SenseVoiceSmall / sherpa-onnx，离线 CPU）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--model-dir", default="", help="SenseVoice 模型目录（含 model.int8.onnx + tokens.txt）")
    parser.add_argument("--download", action="store_true", help="模型缺失时下载到缓存目录")
    parser.add_argument("--threads", type=int, default=2, help="ONNX 推理线程数")
    parser.add_argument("--language", default="auto", choices=("auto", "zh", "en", "yue", "ja", "ko"),
                        help="SenseVoice 语言提示")
    parser.add_argument("--test", nargs="+", default=[], metavar="WAV", help="识别这些 WAV 文件")
    parser.add_argument("--test-opus", default="", metavar="DIR", help="识别目录中的 .bin Opus 帧（按文件名排序）")
    args = parser.parse_args(argv)

    if not args.test and not args.test_opus and not args.download:
        parser.print_help()
        return 1

    try:
        if not args.test and not args.test_opus:
            directory = ensure_model(args.model_dir, download=True)
            print(f"模型就绪：{directory}", flush=True)
            return 0

        asr = MockAsr(
            args.model_dir,
            download=args.download,
            threads=args.threads,
            language=args.language,
        )
        for path in args.test:
            if not Path(path).is_file():
                print(f"文件不存在：{path}", file=sys.stderr)
                return 2
            _print_result(asr.transcribe_wav(path))
        if args.test_opus:
            directory = Path(args.test_opus)
            files = sorted(p for p in directory.glob("*.bin") if p.is_file())
            if not files:
                print(f"目录里没有 .bin 帧：{directory}", file=sys.stderr)
                return 2
            packets = [p.read_bytes() for p in files]
            _print_result(asr.transcribe_opus(packets))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI 顶层统一报错
        print(f"mock ASR 失败：{exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
