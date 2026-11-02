#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""prompt_gen.py —— 可复现地生成/更新提示音**源 WAV**（T3.5 起；不再写 C 数组）

T3.5 把提示音从「编进固件的 C 数组」迁到 `assets` 分区（自定义只读 pack，见
`tools/audio_pack.py`），因此本脚本的产物从 C 数组改为 `tools/prompts_src/*.wav`：
**源 WAV 是唯一源（提交 git）**，由 `tools/audio_pack.py` 打包成 `build/assets.bin`
烧到 assets 分区；替换提示音只重烧分区，app 固件不动。

本脚本只负责 4 条**错误提示音**的可复现生成：
  1. `say -v Tingting -r <rate>` 合成中文提示语，`afconvert` 转 16kHz / 16bit / mono；
  2. 按峰值相对门限（-40dBFS，绝对下限 200）找首/尾语音边界，裁静音并保留 30ms 余量；
     个别文案语音紧贴文件开头（say 无前导静音）时补 30ms 数字静音，保证淡入落在静音区；
  3. 去直流 → 峰值归一化到 13311（=「我在呢」实测峰值 -7.82 dBFS）→ 首尾 15ms 线性淡入淡出；
  4. 写 `tools/prompts_src/<name>.wav` 并更新 `manifest.json`（样本数 + PCM sha256）；
  5. 生成后调用 `tools/audio_pack.py` 的同一套校验口径复检（`--check` 直接委托它）。

唤醒提示音「我在呢」（`tools/prompts_src/wake.wav`）是 T3.1 定稿的**冻结资源**，
不从 say 重新生成：脚本只读取、校验、保持原样（重新生成会改变播放内容，破坏迁移对拍）。

为什么按 speech rate 自动挑选：默认语速下「服务出了点问题」裁剪后 1641ms，超出契约
上限 1.6s；脚本按确定性梯子试语速，选第一个让 4 条全部满足约束的语速（本机为 -r 200），
不使用随机数，因此同机重复运行输出逐字节稳定（sha256 可查）。

用法：
  # 重新生成 4 条错误提示音源 WAV + 更新 manifest（默认写 tools/prompts_src）
  uv run tools/prompt_gen.py

  # 只校验源 WAV（= audio_pack.py --check 的校验口径；不依赖 say/afconvert）
  uv run tools/prompt_gen.py --check

  # 另存一份试听 WAV（可选；源目录本身就是 WAV，一般不需要）
  uv run tools/prompt_gen.py --audition-dir build/t35_prompts

  # 生成到别处（例如对照/回滚实验），不动正式源
  uv run tools/prompt_gen.py --src-dir build/t35_regen

  # 强制某个语速（调试用；默认按梯子自动挑选）
  uv run tools/prompt_gen.py --rate 190

退出码：0 = 生成/校验通过；1 = 约束不满足或环境缺失（原因打印在 stderr）。
"""

from __future__ import annotations

import argparse
import array
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# ---- 路径 ----
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:  # 同目录脚本互相复用（uv run 时 sys.path[0] 即 tools/）
    sys.path.insert(0, str(TOOLS_DIR))

import audio_pack  # noqa: E402  # 校验口径/打包格式的唯一实现，避免两套阈值漂移

DEFAULT_SRC_DIR = audio_pack.DEFAULT_SRC_DIR
SAMPLE_RATE = audio_pack.SAMPLE_RATE

# ---- 音频生成参数（只影响「怎么从 say 合成出 WAV」，校验阈值在 audio_pack.py）----
PEAK_TARGET = 13311           # 「我在呢」实测峰值（-7.82 dBFS），错误提示音与之等响
SILENCE_MARGIN_MS = 30        # 裁静音后首尾各保留的余量
SILENCE_THRESHOLD = 200       # 静音判定绝对下限（16bit 满量程的 -44dBFS）
SILENCE_THRESHOLD_RATIO = 100  # 或相对峰值 -40dBFS（取两者较大值）
FADE_MS = 15                  # 首尾线性淡入淡出（契约要求 10~20ms）

# 默认语速梯子：优先 200，其次按「更长→更短」分布；同机确定性，无随机
DEFAULT_RATE_LADDER = [200, 190, 210, 180, 220, 170, 160, 230, 150, 240, 140, 250, 300]

WAKE_ID, WAKE_NAME = audio_pack.EXPECTED_PROMPTS[0]


@dataclass(frozen=True)
class PromptSpec:
    """一条错误提示音的元数据（id/name 与固件 prompt_id_t / 日志短名一致）。"""

    id: int
    name: str    # 资源文件名 <name>.wav、manifest name、日志/脚本断言短名
    text: str    # say 合成文案


PROMPTS = [
    PromptSpec(1, "net-offline", "网络未连接"),
    PromptSpec(2, "cloud-error", "服务出了点问题"),
    PromptSpec(3, "timeout", "响应超时"),
    PromptSpec(4, "tts-fail", "语音播放失败"),
]


# ---------------------------------------------------------------- 音频处理


def read_wav_mono16(path: Path) -> array.array:
    """读 16k/16bit/mono WAV 为 int16 样本（格式校验复用 audio_pack 的唯一实现）。"""
    return audio_pack.read_wav(path).samples


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
    edge = audio_pack.EDGE_MS * SAMPLE_RATE // 1000
    if max(abs(x) for x in clip[:edge]) >= audio_pack.EDGE_MAX:
        clip = [0.0] * margin + clip
    if max(abs(x) for x in clip[-edge:]) >= audio_pack.EDGE_MAX:
        clip = clip + [0.0] * margin

    # 首尾 15ms 线性淡入淡出，端点严格乘 0（防 pop）
    fade = FADE_MS * SAMPLE_RATE // 1000
    for i in range(fade):
        gain = i / (fade - 1)
        clip[i] *= gain
        clip[-1 - i] *= gain

    return array.array("h", (int(round(x)) for x in clip))


# ---------------------------------------------------------------- 校验（复用 audio_pack 口径）


def validate(samples: array.array, spec: PromptSpec) -> list[str]:
    """单条 PCM 的契约指标；返回问题列表（空 = PASS）。阈值全部来自 audio_pack.py。"""
    stats = audio_pack.samples_stats(samples)
    return audio_pack.validate_clip(stats, spec.id, f"id={spec.id} {spec.name}")


def clip_entry(spec_id: int, name: str, samples: array.array) -> dict:
    """构造 manifest 条目：文件、样本数、PCM sha256。"""
    pcm = audio_pack.pcm_bytes(samples)
    stats = audio_pack.clip_stats(pcm)
    return {
        "id": spec_id,
        "name": name,
        "file": f"{name}.wav",
        "samples": stats["samples"],
        "pcm_sha256": stats["sha256"],
    }


def print_entry_table(rows: list[tuple[dict, dict]]) -> None:
    """rows: [(manifest entry, clip stats)]；按 manifest 顺序打印。"""
    print(f"  {'id':>2}  {'名称':<12} {'文件':<15} {'样本数':>6}  {'时长ms':>8}  {'峰值':>5}  "
          f"{'RMS':>7}  {'直流':>7}  {'crc32':>10}  sha256")
    for entry, stats in rows:
        print(
            f"  {entry['id']:>2}  {entry['name']:<12} {entry['file']:<15} "
            f"{stats['samples']:>6}  {stats['duration_ms']:>8.1f}  {stats['peak']:>5}  "
            f"{stats['rms']:>7.1f}  {stats['mean']:>7.2f}  {stats['crc32']:>10}  "
            f"{stats['sha256'][:12]}"
        )


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


def read_wake(src_dir: Path) -> array.array:
    """读取冻结的唤醒提示音 wake.wav（T3.1 定稿资源，不重新生成）。"""
    wake_path = src_dir / f"{WAKE_NAME}.wav"
    if not wake_path.exists():
        raise SystemExit(
            f"缺少冻结源 {wake_path}：『我在呢』是 T3.1 定稿资源，不能由 say 重新生成；"
            "请从 git 恢复 tools/prompts_src/wake.wav"
        )
    wake = read_wav_mono16(wake_path)
    problems = validate(wake, PromptSpec(WAKE_ID, WAKE_NAME, "我在呢"))
    if problems:
        for problem in problems:
            print(f"  FAIL  {problem}", file=sys.stderr)
        raise SystemExit("冻结的 wake.wav 不满足契约，先修资源再生成")
    return wake


def generate(args: argparse.Namespace) -> int:
    require_generation_tools()
    src_dir = Path(args.src_dir)
    manifest_path = src_dir / "manifest.json"
    read_wake(src_dir)  # 先校验冻结源（缺失/不达标立即退出），避免白跑 say 合成

    rate_ladder = [args.rate] if args.rate else DEFAULT_RATE_LADDER
    work_dir = Path(tempfile.mkdtemp(prefix="t35_prompt_"))
    chosen: tuple[int, list[tuple[PromptSpec, array.array]]] | None = None
    failures: list[tuple[int, dict[str, list[str]]]] = []
    try:
        for rate in rate_ladder:
            print(f"尝试语速 -r {rate}（say -v {args.voice}）…")
            generated: list[tuple[PromptSpec, array.array]] = []
            for spec in PROMPTS:
                raw = synthesize(spec.text, args.voice, rate, work_dir, spec.name)
                generated.append((spec, process(raw)))
            problems_by_id = {spec.name: validate(samples, spec) for spec, samples in generated}
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

    # 写 4 条错误提示音源 WAV（wake.wav 原样不动）
    for spec, samples in generated:
        audio_pack.write_wav_mono16(src_dir / f"{spec.name}.wav", audio_pack.pcm_bytes(samples))

    # manifest 重算全部 5 条（wake 也重新读一遍，保证哈希与磁盘 WAV 一致）
    entries = [clip_entry(WAKE_ID, WAKE_NAME, read_wake(src_dir))]
    for spec, samples in generated:
        entries.append(clip_entry(spec.id, spec.name, samples))
    audio_pack.write_manifest(manifest_path, entries)

    # 生成后用 audio_pack 的正式校验口径复检（格式/样本数/时长/峰值/首尾静音/直流/ID）
    sources = audio_pack.load_sources(src_dir, manifest_path)
    print(f"\n已生成 4 条错误提示音（say -v {args.voice} -r {rate}），"
          f"源 WAV + manifest 写入 {src_dir}")
    print_entry_table(
        [({"id": clip.id, "name": clip.name, "file": clip.file}, clip.stats) for clip in sources.clips]
    )
    print(f"\n  PCM 合计 {sources.total_pcm} B（{sources.total_pcm / 1024:.1f} KB）")
    if sources.problems:
        for problem in sources.problems:
            print(f"  FAIL  {problem}", file=sys.stderr)
        print(f"生成后校验失败：{len(sources.problems)} 项（源 WAV 已写入，修复后重跑）", file=sys.stderr)
        return 1
    _, _, total_len = audio_pack.layout(len(sources.clips), [len(c.pcm) for c in sources.clips])
    print(f"  pack 预计总长 {total_len} B（{total_len / 1024:.2f} KB，assets 分区 "
          f"{audio_pack.ASSETS_PART_SIZE // 1024} KB）；校验通过")
    print("\n提示音『我在呢』(wake.wav) 为冻结源，未重新生成。")

    if args.audition_dir:
        audition_dir = Path(args.audition_dir)
        for clip in sources.clips:
            audio_pack.write_wav_mono16(audition_dir / clip.file, clip.pcm)
        print(f"\n试听副本（16kHz / 16bit / mono）：{audition_dir}")
        for clip in sources.clips:
            print(f"  {audition_dir / clip.file}")
        print("  用 afplay/QuickTime 试听；正常音量约 31%，不要超过 50%")

    print("\n完成。下一步：uv run tools/audio_pack.py   # 构建 build/assets.bin")
    return 0


# ---------------------------------------------------------------- 校验模式


def check(args: argparse.Namespace) -> int:
    """--check 直接复用 audio_pack.py --check（同一套阈值/输出，避免口径漂移）。"""
    print("prompt_gen --check → 复用 tools/audio_pack.py 的源校验口径")
    return audio_pack.main(["--check", "--src-dir", str(Path(args.src_dir))])


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成/校验提示音源 WAV（tools/prompts_src；T3.5 起不再写 C 数组）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--src-dir", default=str(DEFAULT_SRC_DIR), help="源 WAV/manifest 目录")
    parser.add_argument(
        "--audition-dir",
        default=None,
        help="可选：另存一份 16kHz WAV 到该目录供人工试听（默认不另存，源目录即 WAV）",
    )
    parser.add_argument("--check", action="store_true", help="只校验源 WAV/manifest（不合成，不需要 macOS 工具）")
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
    except audio_pack.AudioPackError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
