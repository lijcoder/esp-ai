#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""prompt_gen.py —— 生成/校验 assets 组件的错误提示音 PCM C 数组（T3.6）

为什么需要：T3.6 的异常反馈要播中文提示语音，而提示音以 `const int16_t[]`
编进固件（临时方案，T3.5 迁 assets 分区后再换）。手工用 Audacity 剪音频无法复现、
无法审查，所以把「say 合成 → 裁静音 → 归一化 → 淡入淡出 → 写 C 数组」固化成脚本，
并带 `--check` 把契约里的硬指标（时长/峰值/首尾静音/直流）落到可复跑的断言上。

处理链路（与契约 §4 一致）：
  1. `say -v Tingting -r <rate>` 合成中文提示语，`afconvert` 转 16kHz / 16bit / mono；
  2. 按峰值相对门限（-40dBFS，绝对下限 200）找首/尾语音边界，裁静音并保留 30ms 余量；
     个别文案语音紧贴文件开头（say 无前导静音）时，补 30ms 数字静音，保证淡入落在静音区；
  3. 去直流 → 峰值归一化到 13311（= 「我在呢」实测峰值 -7.82 dBFS）→ 首尾 15ms 线性淡入淡出；
  4. 写 C 数组（格式与原 `prompt_wo_zai_ne.c` 一致）+ 采样点宏头文件 `prompt_pcm_sizes.h`；
  5. 可选把成品 PCM 写成 16k 单声道 WAV 供人工试听（默认 build/t36_prompts）。

为什么按 speech rate 自动挑选：默认语速下「服务出了点问题」裁剪后 1641ms，超出契约
上限 1.6s；脚本按确定性梯子试语速，选第一个让 4 条全部满足约束的语速（本机为 -r 200），
不使用随机数，因此同机重复运行输出逐字节稳定（sha256 可查）。

用法：
  # 生成 4 条错误提示音 C 数组 + 试听 WAV（默认写 build/t36_prompts）
  uv run tools/prompt_gen.py

  # 指定试听目录 / 关闭试听输出
  uv run tools/prompt_gen.py --audition-dir build/t36_prompts
  uv run tools/prompt_gen.py --no-audition

  # 只校验已生成的 C 数组（不依赖 say/afconvert，可在 CI/非 macOS 跑）
  uv run tools/prompt_gen.py --check

  # 强制某个语速（调试用；默认自动按梯子挑选）
  uv run tools/prompt_gen.py --rate 190

退出码：0 = 生成/校验通过；1 = 约束不满足或环境缺失（原因打印在 stderr）。
"""

from __future__ import annotations

import argparse
import array
import hashlib
import math
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

# ---- 路径 ----
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSETS_DIR = REPO_ROOT / "components" / "assets"
DEFAULT_AUDITION_DIR = REPO_ROOT / "build" / "t36_prompts"

# ---- 音频契约（commit 前由 --check 断言；改动需同步 build/t36_contract.md §4）----
SAMPLE_RATE = 16000           # 全项目提示音统一 16kHz
PEAK_TARGET = 13311           # 「我在呢」实测峰值（-7.82 dBFS），错误提示音与之等响
PEAK_TOLERANCE = 0.03         # 峰值允许 ±3%
DURATION_MIN_MS = 800         # 契约时长下限
DURATION_MAX_MS = 1600        # 契约时长上限
SILENCE_MARGIN_MS = 30        # 裁静音后首尾各保留的余量
SILENCE_THRESHOLD = 200       # 静音判定绝对下限（16bit 满量程的 -44dBFS）
SILENCE_THRESHOLD_RATIO = 100  # 或相对峰值 -40dBFS（取两者较大值）
EDGE_SILENCE_MS = 20          # 契约：首/尾 20ms 内 |x| < EDGE_SILENCE_MAX
EDGE_SILENCE_MAX = 200
DC_ABS_MAX = 100              # 契约：|mean| < 100
FADE_MS = 15                  # 首尾线性淡入淡出（契约要求 10~20ms）
VALUES_PER_LINE = 12          # 与原 prompt_wo_zai_ne.c 一致：12 值/行
ARRAY_LINE_PREFIX = "    "    # 与原文件一致：4 空格缩进
ARRAY_VALUE_WIDTH = 7         # 与原文件一致：%7d,
TOTAL_BYTES_WARN = 250 * 1024  # 4 条 C 数组原始 PCM 合计约 250KB 上限

# 默认语速梯子：优先 200，其次按「更长→更短」分布；同机确定性，无随机
DEFAULT_RATE_LADDER = [200, 190, 210, 180, 220, 170, 160, 230, 150, 240, 140, 250, 300]

AUDITION_WAKE_NOTE = "原资源（未改动，仅转 WAV 供等响度对比）"


@dataclass(frozen=True)
class PromptSpec:
    """一条提示音的元数据；id/stem 同时决定 C 数组符号与尺寸宏名。"""

    id: str        # 枚举名后缀：PROMPT_<ID> / PROMPT_<ID>_SAMPLES
    stem: str      # C 符号前缀：<stem>_pcm / <stem>_samples
    c_file: str    # 资源文件名
    text: str      # say 合成文案
    name: str      # prompt_assets_name() 返回值（日志/脚本断言用）
    title: str     # 注释里的人类可读标题


# 唤醒提示音：原文件保持不动（契约要求），脚本只读取它算长度/写试听 WAV
WAKE = PromptSpec("WAKE_OK", "prompt_wo_zai_ne", "prompt_wo_zai_ne.c", "我在呢", "wake", "我在呢")

PROMPTS = [
    PromptSpec("NET_OFFLINE", "prompt_net_offline", "prompt_net_offline.c", "网络未连接", "net-offline", "网络未连接"),
    PromptSpec("CLOUD_ERROR", "prompt_cloud_error", "prompt_cloud_error.c", "服务出了点问题", "cloud-error", "服务出了点问题"),
    PromptSpec("TIMEOUT", "prompt_timeout", "prompt_timeout.c", "响应超时", "timeout", "响应超时"),
    PromptSpec("TTS_FAIL", "prompt_tts_fail", "prompt_tts_fail.c", "语音播放失败", "tts-fail", "语音播放失败"),
]
ALL_CLIPS = [WAKE, *PROMPTS]


# ---------------------------------------------------------------- 音频处理

def read_wav_mono16(path: Path) -> array.array:
    """读 16bit 单声道 WAV 为 int16 样本（校验采样率/声道，避免静默用错格式）。"""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError(f"{path}: 期望 16bit 单声道，实际 {w.getnchannels()}ch/{w.getsampwidth() * 8}bit")
        if w.getframerate() != SAMPLE_RATE:
            raise RuntimeError(f"{path}: 期望 {SAMPLE_RATE}Hz，实际 {w.getframerate()}Hz")
        data = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def write_wav_mono16(path: Path, samples: array.array) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())


def synthesize(text: str, voice: str, rate: int, work_dir: Path, tag: str) -> array.array:
    """say → AIFF → afconvert 16k 单声道 WAV → int16 样本。"""
    aiff = work_dir / f"{tag}.aiff"
    wav = work_dir / f"{tag}.wav"
    subprocess.run(
        ["say", "-v", voice, "-r", str(rate), "-o", str(aiff), text],
        check=True,
    )
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(aiff), str(wav)],
        check=True,
    )
    return read_wav_mono16(wav)


def process(raw: array.array) -> array.array:
    """裁首尾静音（留 30ms 余量）→ 去直流 → 峰值归一 → 首尾淡入淡出。"""
    n = len(raw)
    if n == 0:
        raise RuntimeError("合成音频为空")
    peak = max(abs(x) for x in raw)
    if peak < 1000:
        raise RuntimeError(f"合成音频几乎无声（峰值 {peak}），检查 say 音色/文案")
    # 门限同时兼顾绝对底噪与相对峰值：中文提示音峰值约 17k~21k，-40dBFS 约 170~210
    threshold = max(SILENCE_THRESHOLD, peak // SILENCE_THRESHOLD_RATIO)
    onset = next((i for i, x in enumerate(raw) if abs(x) > threshold), n)
    last = n - 1 - next((i for i, x in enumerate(reversed(raw)) if abs(x) > threshold), n)
    if onset >= n or last < 0:
        raise RuntimeError("未检测到语音段（全静音），检查 say 输出")

    margin = SILENCE_MARGIN_MS * SAMPLE_RATE // 1000
    start = max(0, onset - margin)
    # say 对部分短句不加前导静音（语音从第 0 个样本开始），用数字静音补足 30ms 余量
    head_pad = max(0, margin - onset)
    end = min(n, last + 1 + margin)
    tail_pad = max(0, last + 1 + margin - end)

    clip = [0.0] * head_pad + [float(x) for x in raw[start:end]] + [0.0] * tail_pad

    # 去直流：say/afconvert 的输出基本无直流，但重采样后可能有几十的偏置，契约要求 |mean|<100
    mean = sum(clip) / len(clip)
    clip = [x - mean for x in clip]

    # 峰值归一化：整段最大绝对值对齐「我在呢」的 13311，保证各提示音等响
    peak_after_dc = max(abs(x) for x in clip)
    if peak_after_dc <= 0:
        raise RuntimeError("去直流后音频为全零")
    scale = PEAK_TARGET / peak_after_dc
    clip = [x * scale for x in clip]

    # 首/尾 20ms 必须安静（防 pop）；裁静音后仍不满足时补 30ms 数字静音。
    # 这一步放在归一化之后，用最终幅度判定，避免归一化放大后超限。
    edge = EDGE_SILENCE_MS * SAMPLE_RATE // 1000
    if max(abs(x) for x in clip[:edge]) >= EDGE_SILENCE_MAX:
        clip = [0.0] * margin + clip
    if max(abs(x) for x in clip[-edge:]) >= EDGE_SILENCE_MAX:
        clip = clip + [0.0] * margin

    # 首尾 15ms 线性淡入淡出，端点严格乘 0（防 pop）
    fade = FADE_MS * SAMPLE_RATE // 1000
    for i in range(fade):
        gain = i / (fade - 1)
        clip[i] *= gain
        clip[-1 - i] *= gain

    return array.array("h", (int(round(x)) for x in clip))


# ---------------------------------------------------------------- 统计与校验

def clip_stats(samples: array.array) -> dict:
    n = len(samples)
    peak = max(abs(x) for x in samples) if n else 0
    mean = (sum(samples) / n) if n else 0.0
    rms = math.sqrt(sum(x * x for x in samples) / n) if n else 0.0
    return {
        "samples": n,
        "duration_ms": n * 1000.0 / SAMPLE_RATE,
        "peak": peak,
        "peak_dbfs": dbfs(peak),
        "mean": mean,
        "rms": rms,
        "rms_dbfs": dbfs(rms),
        "sha256": pcm_sha256(samples),
    }


def dbfs(amplitude: float) -> float:
    """线性幅度（16bit 满量程 32768）转 dBFS。"""
    if amplitude <= 0:
        return -math.inf
    return 20.0 * math.log10(amplitude / 32768.0)


def pcm_sha256(samples: array.array) -> str:
    data = samples.tobytes()
    if sys.byteorder != "little":  # 统一按小端（与固件内存布局一致）算哈希
        swapped = array.array("h", samples)
        swapped.byteswap()
        data = swapped.tobytes()
    return hashlib.sha256(data).hexdigest()


def validate(samples: array.array, label: str) -> list[str]:
    """契约硬指标；返回问题列表（空 = PASS）。"""
    problems: list[str] = []
    n = len(samples)
    if n == 0:
        return [f"{label}: 采样点数为 0"]
    stats = clip_stats(samples)
    if not (DURATION_MIN_MS <= stats["duration_ms"] <= DURATION_MAX_MS):
        problems.append(f"{label}: 时长 {stats['duration_ms']:.1f}ms 超出 {DURATION_MIN_MS}~{DURATION_MAX_MS}ms")
    if abs(stats["peak"] - PEAK_TARGET) > PEAK_TARGET * PEAK_TOLERANCE:
        lo = math.ceil(PEAK_TARGET * (1 - PEAK_TOLERANCE))
        hi = math.floor(PEAK_TARGET * (1 + PEAK_TOLERANCE))
        problems.append(f"{label}: 峰值 {stats['peak']} 超出 {PEAK_TARGET}±{PEAK_TOLERANCE:.0%}（{lo}~{hi}）")
    edge = EDGE_SILENCE_MS * SAMPLE_RATE // 1000
    head_max = max(abs(x) for x in samples[:edge])
    tail_max = max(abs(x) for x in samples[-edge:])
    if head_max >= EDGE_SILENCE_MAX:
        problems.append(f"{label}: 首 {EDGE_SILENCE_MS}ms 峰值 {head_max} 未 < {EDGE_SILENCE_MAX}（防 pop）")
    if tail_max >= EDGE_SILENCE_MAX:
        problems.append(f"{label}: 尾 {EDGE_SILENCE_MS}ms 峰值 {tail_max} 未 < {EDGE_SILENCE_MAX}（防 pop）")
    if abs(stats["mean"]) >= DC_ABS_MAX:
        problems.append(f"{label}: 直流偏置 |mean|={abs(stats['mean']):.1f} 未 < {DC_ABS_MAX}")
    return problems


# ---------------------------------------------------------------- C 文件解析/生成

ARRAY_RE = re.compile(r"const\s+int16_t\s+(\w+)\s*\[\s*\]\s*=\s*\{(.*?)\}\s*;", re.S)
SIZE_MACRO_RE = re.compile(r"#define\s+PROMPT_(\w+)_SAMPLES\s+(\d+)")
RATE_MACRO_RE = re.compile(r"#define\s+PROMPT_SAMPLE_RATE_HZ\s+(\d+)")


def parse_c_array(path: Path) -> array.array:
    """从资源 .c 里取出 const int16_t 数组内容（只认数字，忽略空白/逗号/注释外文本）。"""
    text = path.read_text(encoding="utf-8")
    match = ARRAY_RE.search(text)
    if not match:
        raise RuntimeError(f"{path}: 找不到 const int16_t <name>[] = {{...}};")
    body = match.group(2)
    values = [int(tok) for tok in re.findall(r"-?\d+", body)]
    return array.array("h", values)


def parse_sizes_header(path: Path) -> tuple[dict[str, int], int | None]:
    if not path.exists():
        return {}, None
    text = path.read_text(encoding="utf-8")
    macros = {name: int(val) for name, val in SIZE_MACRO_RE.findall(text)}
    rate_match = RATE_MACRO_RE.search(text)
    return macros, (int(rate_match.group(1)) if rate_match else None)


def format_c_array(values: array.array) -> list[str]:
    """按原 prompt_wo_zai_ne.c 风格：4 空格缩进、%7d、12 值/行。"""
    lines = []
    for i in range(0, len(values), VALUES_PER_LINE):
        chunk = values[i:i + VALUES_PER_LINE]
        lines.append(ARRAY_LINE_PREFIX + "".join(f"{v:{ARRAY_VALUE_WIDTH}d}," for v in chunk))
    return lines


def write_c_file(path: Path, spec: PromptSpec, samples: array.array, voice: str, rate: int) -> None:
    stats = clip_stats(samples)
    lines = [
        "/*",
        f' * 提示音"{spec.title}"：16kHz / 16bit / 单声道，{stats["samples"]} 采样点'
        f'（{stats["duration_ms"] / 1000:.2f} 秒），峰值 {stats["peak"]}（{stats["peak_dbfs"]:.2f} dBFS）。',
        f" * 由 tools/prompt_gen.py 生成（say -v {voice} -r {rate} → afconvert 16kHz 单声道），勿手改。",
        " * 处理：裁首尾静音（保留 30ms 余量）→ 去直流 → 峰值归一 13311 → 首尾 15ms 线性淡入淡出。",
        " * 临时资源：T3.5 迁到 assets 分区后删除本文件。",
        " */",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        '#include "prompt_assets.h"',
        '#include "prompt_pcm_sizes.h"',
        "",
        f"const int16_t {spec.stem}_pcm[] = {{",
        *format_c_array(samples),
        "};",
        "",
        f"const size_t {spec.stem}_samples = sizeof({spec.stem}_pcm) / sizeof({spec.stem}_pcm[0]);",
        "",
        "#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L",
        f"_Static_assert(sizeof({spec.stem}_pcm) / sizeof({spec.stem}_pcm[0]) == PROMPT_{spec.id}_SAMPLES,",
        '               "数组长度与 prompt_pcm_sizes.h 不一致，请重新运行 tools/prompt_gen.py");',
        "#endif",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_sizes_header(path: Path, sizes: dict[str, int]) -> None:
    lines = [
        "#pragma once",
        "",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        "/*",
        " * 提示音采样点数与 PCM 数组声明（由 tools/prompt_gen.py 生成，勿手改）。",
        " *",
        " * 为什么单独生成一个头文件：prompt_assets.c 的静态表要求采样点数是编译期常量，",
        " * 而各数组的 sizeof 在别的编译单元不可见；把长度固化成宏后，各 prompt_*.c 再用",
        " * _Static_assert 校验自己的数组长度，避免「表里长度与数组不符」导致静默越界。",
        " * 重新生成提示音后本文件随之更新；T3.5 迁到 assets 分区后与本组件一起删除。",
        " */",
        "",
        f"#define PROMPT_SAMPLE_RATE_HZ {SAMPLE_RATE}",
        f"#define PROMPT_WAKE_OK_SAMPLES {sizes['WAKE_OK']}",
        *[f"#define PROMPT_{spec.id}_SAMPLES {sizes[spec.id]}" for spec in PROMPTS],
        "",
        "extern const int16_t prompt_net_offline_pcm[];",
        "extern const int16_t prompt_cloud_error_pcm[];",
        "extern const int16_t prompt_timeout_pcm[];",
        "extern const int16_t prompt_tts_fail_pcm[];",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- 输出

def print_table(rows: list[tuple[str, dict]]) -> None:
    for label, stats in rows:
        print(
            f"  {label:<24} {stats['samples']:>6} samples  {stats['duration_ms']:>7.1f} ms  "
            f"peak {stats['peak']:>5} ({stats['peak_dbfs']:>6.2f} dBFS)  "
            f"RMS {stats['rms']:>7.1f} ({stats['rms_dbfs']:>6.2f} dBFS)  sha256 {stats['sha256']}"
        )


def print_validation(problems: list[str]) -> None:
    for problem in problems:
        print(f"  FAIL  {problem}", file=sys.stderr)


# ---------------------------------------------------------------- 生成模式

def require_generation_tools() -> None:
    if platform.system() != "Darwin":
        raise SystemExit("prompt_gen.py 生成模式只支持 macOS（依赖 say/afconvert）；校验用 --check")
    for tool in ("say", "afconvert"):
        if shutil.which(tool) is None:
            raise SystemExit(f"找不到 {tool}，无法生成提示音")
    voices = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    if "Tingting" not in voices:
        raise SystemExit("系统未安装 Tingting（zh_CN）音色：系统设置 → 辅助功能 → 朗读内容 → 系统声音")


def generate(args: argparse.Namespace) -> int:
    require_generation_tools()
    assets_dir = Path(args.assets_dir)
    if not assets_dir.is_dir():
        print(f"assets 目录不存在：{assets_dir}", file=sys.stderr)
        return 1

    wake_path = assets_dir / WAKE.c_file
    wake_samples = parse_c_array(wake_path)
    wake_problems = validate(wake_samples, WAKE.stem)
    if wake_problems:
        print_validation(wake_problems)
        print("原唤醒提示音不满足契约，先修资源再生成", file=sys.stderr)
        return 1

    rate_ladder = [args.rate] if args.rate else DEFAULT_RATE_LADDER
    work_dir = Path(tempfile.mkdtemp(prefix="t36_prompt_"))
    chosen: tuple[int, list[tuple[PromptSpec, array.array]]] | None = None
    failures: list[tuple[int, dict[str, list[str]]]] = []
    try:
        for rate in rate_ladder:
            print(f"尝试语速 -r {rate}（say -v {args.voice}）…")
            generated: list[tuple[PromptSpec, array.array]] = []
            for spec in PROMPTS:
                raw = synthesize(spec.text, args.voice, rate, work_dir, spec.stem)
                generated.append((spec, process(raw)))
            problems_by_id = {spec.id: validate(samples, spec.stem) for spec, samples in generated}
            if not any(problems_by_id.values()):
                chosen = (rate, generated)
                break
            failures.append((rate, problems_by_id))
            print("  该语速不满足契约，继续下一个", file=sys.stderr)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if chosen is None:
        print("所有候选语速都无法满足契约，请人工检查文案/环境：", file=sys.stderr)
        for rate, problems_by_id in failures:
            for label, problems in problems_by_id.items():
                for problem in problems:
                    print(f"  -r {rate}: {problem}", file=sys.stderr)
        return 1

    rate, generated = chosen
    sizes = {WAKE.id: len(wake_samples)}
    for spec, samples in generated:
        sizes[spec.id] = len(samples)

    for spec, samples in generated:
        write_c_file(assets_dir / spec.c_file, spec, samples, args.voice, rate)
    write_sizes_header(assets_dir / "prompt_pcm_sizes.h", sizes)

    rows = [(f"{WAKE.c_file}（原文件）", clip_stats(wake_samples))]
    rows += [(spec.c_file, clip_stats(samples)) for spec, samples in generated]
    total_bytes = sum(stats["samples"] for _, stats in rows) * 2
    print(f"\n已生成 4 条错误提示音（say -v {args.voice} -r {rate}）；采样点宏写入 prompt_pcm_sizes.h")
    print_table(rows)
    print(f"  PCM 合计 {total_bytes} B（{total_bytes / 1024:.1f} KB，契约上限 ≈{TOTAL_BYTES_WARN // 1024} KB）")
    if total_bytes > TOTAL_BYTES_WARN:
        print(f"  WARN 合计超过 {TOTAL_BYTES_WARN // 1024} KB，考虑压缩文案", file=sys.stderr)

    if not args.no_audition:
        audition_dir = Path(args.audition_dir) if args.audition_dir else DEFAULT_AUDITION_DIR
        write_wav_mono16(audition_dir / "wake.wav", wake_samples)
        for spec, samples in generated:
            write_wav_mono16(audition_dir / f"{spec.name}.wav", samples)
        print(f"\n试听 WAV（16kHz / 16bit / mono，{AUDITION_WAKE_NOTE}）：")
        for spec in ALL_CLIPS:
            print(f"  {audition_dir / (WAKE.name if spec is WAKE else spec.name)}.wav")
        print("  用 afplay/QuickTime 试听；正常音量约 31%，不要超过 50%")

    print("\n完成。建议继续跑： uv run tools/prompt_gen.py --check")
    return 0


# ---------------------------------------------------------------- 校验模式

def check(args: argparse.Namespace) -> int:
    assets_dir = Path(args.assets_dir)
    header_path = assets_dir / "prompt_pcm_sizes.h"
    table_path = assets_dir / "prompt_assets.c"
    macros, rate = parse_sizes_header(header_path)
    problems: list[str] = []
    rows: list[tuple[str, dict]] = []

    if rate != SAMPLE_RATE:
        problems.append(f"prompt_pcm_sizes.h: PROMPT_SAMPLE_RATE_HZ={rate} 期望 {SAMPLE_RATE}")
    if not table_path.exists():
        problems.append(f"缺少 {table_path}（静态资源表）")

    parsed: dict[str, array.array] = {}
    for spec in ALL_CLIPS:
        path = assets_dir / spec.c_file
        if not path.exists():
            problems.append(f"缺少 {path.name}")
            continue
        try:
            samples = parse_c_array(path)
        except RuntimeError as exc:
            problems.append(str(exc))
            continue
        parsed[spec.id] = samples
        rows.append((spec.c_file, clip_stats(samples)))
        problems.extend(validate(samples, spec.c_file))
        expected = len(samples)
        declared = macros.get(spec.id)
        if declared is None:
            problems.append(f"prompt_pcm_sizes.h: 缺少 PROMPT_{spec.id}_SAMPLES")
        elif declared != expected:
            problems.append(
                f"prompt_pcm_sizes.h: PROMPT_{spec.id}_SAMPLES={declared} 与数组长度 {expected} 不一致，"
                "请重新运行 tools/prompt_gen.py"
            )
        text = path.read_text(encoding="utf-8")
        if f"const size_t {spec.stem}_samples" not in text:
            problems.append(f"{path.name}: 缺少 const size_t {spec.stem}_samples 定义")

    if table_path.exists():
        table_text = table_path.read_text(encoding="utf-8")
        for spec in ALL_CLIPS:
            entry = re.compile(
                r"\{\s*" + re.escape(spec.stem) + r"_pcm\s*,\s*PROMPT_" + spec.id
                + r"_SAMPLES\s*,\s*PROMPT_SAMPLE_RATE_HZ\s*\}"
            )
            if not entry.search(table_text):
                problems.append(f"prompt_assets.c: 缺少 {spec.stem}_pcm / PROMPT_{spec.id}_SAMPLES 的表项")
            if f"[PROMPT_{spec.id}]" not in table_text:
                problems.append(f"prompt_assets.c: 缺少 [PROMPT_{spec.id}] 指定初始化")
            if f'"{spec.name}"' not in table_text:
                problems.append(f'prompt_assets.c: 缺少名字 "{spec.name}"')

    total_bytes = sum(stats["samples"] for _, stats in rows) * 2
    print(f"校验目录：{assets_dir}")
    print_table(rows)
    print(f"  PCM 合计 {total_bytes} B（{total_bytes / 1024:.1f} KB，契约上限 ≈{TOTAL_BYTES_WARN // 1024} KB）")
    if total_bytes > TOTAL_BYTES_WARN:
        print(f"  WARN 合计超过 {TOTAL_BYTES_WARN // 1024} KB", file=sys.stderr)

    if problems:
        print_validation(problems)
        print(f"校验失败：{len(problems)} 项", file=sys.stderr)
        return 1
    print(f"校验通过：{len(ALL_CLIPS)} 条提示音（时长/峰值/首尾静音/直流/尺寸宏/静态表）")
    return 0


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成/校验 assets 组件的提示音 PCM C 数组（say + afconvert，仅 macOS 生成）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--assets-dir", default=str(DEFAULT_ASSETS_DIR), help="assets 组件目录")
    parser.add_argument(
        "--audition-dir",
        default=None,
        help=f"试听 WAV 输出目录（默认 {DEFAULT_AUDITION_DIR}）",
    )
    parser.add_argument("--no-audition", action="store_true", help="不输出试听 WAV")
    parser.add_argument("--check", action="store_true", help="只校验已生成的 C 数组，不重新合成")
    parser.add_argument("--voice", default="Tingting", help="say 音色（zh_CN）")
    parser.add_argument("--rate", type=int, default=None, help="强制 say 语速；默认按梯子自动挑选")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return check(args) if args.check else generate(args)
    except subprocess.CalledProcessError as exc:
        print(f"调用系统命令失败：{exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
