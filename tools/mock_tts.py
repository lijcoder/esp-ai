#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["piper-tts>=1.2", "lameenc>=1.7", "numpy>=1.24"]
# ///
"""
mock_tts.py —— 本地 TTS 生成 mock 回复音频（Piper，CPU 实时，MIT）

为什么需要：mock 的下行 TTS 目前是合成帧（不可解码）或固定 MP3。想让设备在联调里
听到「与文本对应」的真实语音（例如把识别出来的问题/测试文案念出来），用本脚本在 PC 侧
生成 24kHz 单声道 MP3，再交给 `ws_mock_server.py --tts-mp3 <file>` 下发即可。

选型：Piper（rhasspy/piper-voices，VITS，MIT）—— 单个音色 20~60MB、CPU 实时、
许可干净；本脚本预置中文音色 `zh_CN-huayan-medium`，其它音色可用 `--model/--config` 指定。
（Kokoro/MeloTTS 体积/依赖更大，本次不接入；以后需要再评估。）

用法：
  # 首次准备模型（约 60MB，失败自动换 hf-mirror）
  uv run tools/mock_tts.py --download

  # 文字 → 24k 单声道 MP3（默认输出 /tmp/mock_tts.mp3）
  uv run tools/mock_tts.py --text "今天天气不错，适合出门散步。" --out /tmp/reply.mp3
  printf '好的，我来帮你查一下。' | uv run tools/mock_tts.py --out /tmp/reply.mp3

  # 交给 mock 下发（设备播真实 TTS）
  uv run tools/ws_mock_server.py --tts-mp3 /tmp/reply.mp3 --tts-pace-pct 80

参数速记：--speed >1 更慢（Piper length_scale），<1 更快；模型缓存在
`~/.cache/esp-ai-mock-tts/piper/<voice>/`（可用 --model-dir / 环境变量 MOCK_TTS_CACHE
覆盖；家目录缓存不可写时自动退到 `~/.cache/uv/esp-ai-mock-tts/` 或系统临时目录，不写进仓库）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

TARGET_RATE = 24000  # TTS 契约：24kHz 单声道（ARCH §3.7 / mock TTS_SAMPLE_RATE）
MP3_BITRATE = 64

# 预置音色（Piper voices 的固定目录结构；新增音色可加表项或直接 --model/--config）
VOICES: dict[str, dict[str, str]] = {
    "zh_CN-huayan-medium": {
        "lang": "zh",
        "locale": "zh_CN",
        "speaker": "huayan",
        "quality": "medium",
    },
}
_SOURCES = (
    "https://hf-mirror.com/rhasspy/piper-voices/resolve/main",
    "https://huggingface.co/rhasspy/piper-voices/resolve/main",
)


# ---------------------------------------------------------------- 模型定位/下载


def cache_candidates() -> list[Path]:
    """模型缓存候选目录（按优先级）：显式环境变量 → 家目录缓存 → uv 缓存 → 临时目录。"""
    candidates: list[Path] = []
    explicit = os.environ.get("MOCK_TTS_CACHE")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    xdg = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    candidates.append(xdg / "esp-ai-mock-tts" / "piper")
    candidates.append(Path.home() / ".cache" / "uv" / "esp-ai-mock-tts" / "piper")
    candidates.append(Path(tempfile.gettempdir()) / "esp-ai-mock-tts" / "piper")
    return candidates


def find_cached(voice: str) -> Path | None:
    """在候选目录里找已下载的音色。"""
    for directory in cache_candidates():
        candidate = directory / voice
        if (candidate / f"{voice}.onnx").is_file() and (candidate / f"{voice}.onnx.json").is_file():
            return candidate
    return None


def cache_root() -> Path:
    """可写的下载目录（家目录缓存不可写时自动退化，避免沙箱/只读 HOME 下失败）。"""
    for directory in cache_candidates():
        parent = directory
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(parent, os.W_OK):
            return parent
    return Path(tempfile.gettempdir()) / "esp-ai-mock-tts" / "piper"


def voice_dir(voice: str, model_dir: str | Path | None = None) -> Path:
    if model_dir:
        return Path(model_dir).expanduser()
    cached = find_cached(voice)
    return cached if cached is not None else cache_root() / voice


def _urls(voice: str) -> tuple[str, str]:
    spec = VOICES.get(voice)
    if spec is None:
        raise SystemExit(
            f"未知音色 {voice!r}（预置：{', '.join(VOICES)}）；"
            "其它音色请用 --model <onnx> --config <json> 指定"
        )
    base = f"{spec['lang']}/{spec['locale']}/{spec['speaker']}/{spec['quality']}/" \
           f"{spec['locale']}-{spec['speaker']}-{spec['quality']}"
    return f"{base}.onnx", f"{base}.onnx.json"


def _download_one(url: str, dest: Path, log) -> None:
    log(f"[TTS] 下载 {url.split('/')[-1]} ...")
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as handle:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
    if tmp.stat().st_size < 1 << 20:  # 明显不是模型（例如 HTML 错误页）
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载内容过小：{dest.name}")
    tmp.replace(dest)
    log(f"[TTS] 完成 {dest}（{dest.stat().st_size / (1 << 20):.0f} MB）")


def ensure_voice(voice: str, model_dir: str | Path | None = None, *, download: bool = False,
                 log=print) -> tuple[Path, Path]:
    """返回 (onnx, config)；缺失时按需下载。"""
    directory = voice_dir(voice, model_dir)
    onnx = directory / f"{voice}.onnx"
    config = directory / f"{voice}.onnx.json"
    if onnx.is_file() and config.is_file():
        return onnx, config
    if model_dir:
        raise SystemExit(f"模型目录不完整：{directory}（需要 {onnx.name} + {config.name}）")
    if not download:
        raise SystemExit(
            "未找到 Piper 中文音色。先执行：uv run tools/mock_tts.py --download\n"
            f"（下载到 {directory}，约 60MB）"
        )
    directory.mkdir(parents=True, exist_ok=True)
    relative = _urls(voice)
    for suffix, dest in ((relative[0], onnx), (relative[1], config)):
        last: Exception | None = None
        for base in _SOURCES:
            try:
                _download_one(f"{base}/{suffix}", dest, log)
                last = None
                break
            except Exception as exc:  # noqa: BLE001 - 换源重试
                last = exc
                log(f"[TTS] 源失败（{base.split('/')[2]}）：{exc}")
        if last is not None:
            raise SystemExit(f"模型下载失败：{suffix}（{last}）")
    return onnx, config


# ---------------------------------------------------------------- 合成与编码


def speakable_text(text: str) -> str:
    """挑出真正可发音的字符（去掉标点/空白）。

    ASR 在"没听到语音"时常返回 "." / "。" 这类纯标点文本，直接喂给 piper 会生成
    0 帧音频并让 piper 自己抛 `wave.Error: # channels not specified`（且历史上以
    SystemExit 逃出线程、整崩 mock）。调用方应先用本函数判断有没有内容可念。
    """
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def piper_tts(text: str, onnx: Path, config: Path, *, speed: float = 1.0) -> tuple[np.ndarray, int]:
    """调用 piper CLI 生成 PCM（返回 int16 单声道 + 原始采样率）。

    失败一律抛 `RuntimeError`（而不是 SystemExit）：本模块被 ws_mock_server 当库调用，
    SystemExit 会穿透 `except Exception`，把整个 mock 进程带崩。
    """
    if not speakable_text(text):
        raise RuntimeError(f"没有可合成的文字（{text!r}）")
    piper = shutil.which("piper")
    if piper is None:
        raise RuntimeError("找不到 piper 命令（PEP 723 依赖应已安装，请用 `uv run tools/mock_tts.py` 运行）")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        raw_path = Path(handle.name)
    try:
        proc = subprocess.run(
            [piper, "-m", str(onnx), "-c", str(config), "-f", str(raw_path),
             "--length-scale", f"{speed:g}"],
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"piper 合成失败（exit {proc.returncode}）："
                f"{proc.stderr.decode('utf-8', 'replace')[-400:]}"
            )
        with wave.open(str(raw_path), "rb") as wav:
            rate = wav.getframerate()
            channels = wav.getnchannels()
            frames = wav.readframes(wav.getnframes())
        pcm = np.frombuffer(frames, dtype=np.int16)
        if channels > 1:
            pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
        if pcm.size == 0:
            raise RuntimeError("piper 返回 0 帧音频（文本可能不可发音）")
        return pcm, rate
    finally:
        raw_path.unlink(missing_ok=True)


def resample(pcm: np.ndarray, rate: int, target: int = TARGET_RATE) -> np.ndarray:
    """线性重采样（测试用途足够；避免为一次重采样引入额外依赖）。"""
    if rate == target or pcm.size == 0:
        return pcm
    n_out = int(round(pcm.size * target / rate))
    src = pcm.astype(np.float32)
    positions = np.arange(n_out) * (rate / target)
    out = np.interp(positions, np.arange(src.size), src)
    return np.clip(out, -32768, 32767).astype(np.int16)


def encode_mp3(pcm: np.ndarray, rate: int = TARGET_RATE) -> bytes:
    import lameenc

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(MP3_BITRATE)
    encoder.set_in_sample_rate(rate)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return encoder.encode(pcm.tobytes()) + encoder.flush()


def synthesize_mp3(
    text: str,
    *,
    voice: str = "zh_CN-huayan-medium",
    model_dir: str | Path | None = None,
    speed: float = 1.0,
    download: bool = False,
    log=print,
) -> tuple[bytes, float]:
    """文字 → 24kHz 单声道 MP3 字节；返回 (mp3_bytes, 音频时长秒)。

    供 `ws_mock_server.py --tts-piper` 做「ASR 文本 → 复读」回声测试复用。
    """
    if not speakable_text(text):
        raise RuntimeError(f"没有可合成的文字（{text!r}）")
    onnx, config = ensure_voice(voice, model_dir, download=download, log=log)
    pcm, rate = piper_tts(text, onnx, config, speed=speed)
    pcm24 = resample(pcm, rate)
    return encode_mp3(pcm24), pcm24.size / TARGET_RATE


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地 TTS 生成 mock 回复音频（Piper，CPU 实时，中文音色）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--text", default="", help="要合成的文本；缺省从 stdin 读")
    parser.add_argument("--text-file", default="", help="从文件读文本（UTF-8）")
    parser.add_argument("--out", default="/tmp/mock_tts.mp3", help="输出 MP3（24kHz 单声道）")
    parser.add_argument("--voice", default="zh_CN-huayan-medium", help="预置音色名")
    parser.add_argument("--model-dir", default="", help="音色目录（含 .onnx + .onnx.json）")
    parser.add_argument("--download", action="store_true", help="模型缺失时下载（约 60MB）")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="语速（Piper length_scale：>1 更慢，<1 更快）")
    parser.add_argument("--model", default="", help="直接指定 onnx（忽略 --voice/--download）")
    parser.add_argument("--config", default="", help="直接指定 onnx.json")
    args = parser.parse_args(argv)

    if args.model or args.config:
        if not (args.model and args.config):
            raise SystemExit("--model 与 --config 必须同时给出")
        onnx, config = Path(args.model), Path(args.config)
        if not onnx.is_file() or not config.is_file():
            raise SystemExit(f"模型文件不存在：{onnx} / {config}")
    else:
        onnx, config = ensure_voice(
            args.voice, args.model_dir or None, download=args.download,
            log=lambda m: print(m, flush=True),
        )

    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    elif args.text:
        text = args.text.strip()
    else:
        text = sys.stdin.read().strip()
    if not text:
        if args.download:
            print(f"[TTS] 模型就绪：{onnx.parent}（未给文本，仅准备模型）", flush=True)
            return 0
        raise SystemExit("没有输入文本（用 --text / --text-file / stdin 传入）")

    started = time.monotonic()
    pcm, rate = piper_tts(text, onnx, config, speed=args.speed)
    pcm24 = resample(pcm, rate)
    audio = encode_mp3(pcm24)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio)
    duration = pcm24.size / TARGET_RATE
    print(
        f"[TTS] {out}（{duration:.2f}s，{TARGET_RATE}Hz 单声道，{len(audio)}B，"
        f"生成 {time.monotonic() - started:.2f}s）\n"
        f"      文本：{text}\n"
        f"      下发：uv run tools/ws_mock_server.py --tts-mp3 {out} --tts-pace-pct 80",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
