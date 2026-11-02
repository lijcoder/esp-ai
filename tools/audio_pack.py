#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""audio_pack.py —— 提示音源 WAV → assets 分区「自定义只读 pack」（T3.5 方案 D）

为什么需要：T3.5 把 5 条提示音（我在呢 + 4 条错误提示）从**编进固件的 C 数组**迁到
assets 分区（2M @ 0x7a0000）。分区内容是自定义只读二进制 pack，固件启动时校验后用
`esp_partition_mmap()` 零拷贝映射，播放指针指向 flash 映射区，行为与原来的 rodata 一致；
替换提示音只需重烧 assets 分区，**app 固件不用动**。本工具是 pack 的唯一生成/校验入口：

  源 WAV（tools/prompts_src/，唯一源，提交 git）
      │  --check 校验（格式/样本数/时长/峰值/首尾静音/直流/ID 完整性）
      ▼
  build/assets.bin（pack：24B 头 + N×20B 条目 + PCM 数据区）
      │  --flash <port>：esptool 只写 assets 分区
      ▼
  设备（app 启动 prompt_store_init() → 校验 → mmap）

pack 格式（冻结契约 build/t35_contract.md §2；固件侧同结构见
components/prompt_store/include/prompt_pack.h，小端）：

    struct pack_hdr {                 // 24 B
      char magic[8];                  // "ESPAPK01"
      uint16_t version;               // 1
      uint16_t count;                 // 5（= PROMPT_COUNT）
      uint32_t total_len;             // 整个 pack 字节数（4 字节对齐）
      uint32_t header_crc32;          // CRC32(头部区 0..24+count*20，本字段按 0 计算)
      uint32_t reserved;              // 0
    };
    struct pack_entry {               // 20 B × count
      uint8_t  id;                    // prompt_id_t：0=wake 1=net-offline 2=cloud-error 3=timeout 4=tts-fail
      uint8_t  reserved[3];           // 0
      uint32_t offset;                // PCM 起始（相对分区 0，4 字节对齐）
      uint32_t length;                // PCM 字节数 = 样本数 × 2
      uint32_t sample_rate;           // 16000
      uint32_t crc32;                 // CRC32(PCM 字节)
    };
    // PCM 数据区起点 = align4(24 + count*20)（count=5 → 128）；每条 PCM 4 字节对齐；
    // total_len 4 字节对齐，尾部补 0。

CRC32 是**标准 CRC-32/ISO-HDLC（= Python `zlib.crc32`）**：反射多项式 0xEDB88320、
初值 0xFFFFFFFF、末尾异或 0xFFFFFFFF。工具与固件必须一致（固件用 esp_rom_crc32_le
按同一参数实现），所以这里只认 zlib.crc32，不提供其它变体。

用法：
  # 构建 pack（默认 tools/prompts_src → build/assets.bin），打印每条 id/样本数/时长/峰值/crc32
  uv run tools/audio_pack.py

  # 只校验源 WAV（不写文件；CI/非 macOS 可跑）
  uv run tools/audio_pack.py --check

  # 解析已有 pack 并校验 header_crc / 条目范围 / PCM crc
  uv run tools/audio_pack.py --dump build/assets.bin

  # 构建 + 只烧 assets 分区（0x7a0000，app 不动；需要 esptool，详见 tools/README.md）
  uv run tools/audio_pack.py --flash /dev/cu.usbmodem5CF90746951

  # 自定义源目录 / manifest / 输出路径
  uv run tools/audio_pack.py --src-dir /tmp/src --manifest /tmp/src/manifest.json --out /tmp/assets.bin

退出码：0 = 通过（构建成功 / 校验全部 PASS / dump 合法 / 烧录成功）；1 = 任一失败。
可重复构建：同源同工具输出逐字节一致（pack 内无时间戳/随机量）。
"""

from __future__ import annotations

import argparse
import array
import hashlib
import importlib.util
import json
import math
import shutil
import struct
import subprocess
import sys
import wave
import zlib
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- 路径与常量

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC_DIR = REPO_ROOT / "tools" / "prompts_src"
DEFAULT_OUT = REPO_ROOT / "build" / "assets.bin"

# ---- pack 格式（冻结，见模块 docstring / build/t35_contract.md §2）----
PACK_MAGIC = b"ESPAPK01"
PACK_VERSION = 1
HDR_STRUCT = struct.Struct("<8sHHIII")      # 24 B
ENTRY_STRUCT = struct.Struct("<B3xIIII")    # 20 B：id + 3B 保留 + offset/length/rate/crc32
HDR_SIZE = HDR_STRUCT.size                  # 24
ENTRY_SIZE = ENTRY_STRUCT.size              # 20
PACK_ALIGN = 4
MAX_ENTRIES = 16
# 契约 §2 冻结：count=5 时 PCM 数据区从 128 B 开始（头部区 24+5×20=124 B，再补到 128）。
# 固件 prompt_pack_validate 只要求 offset >= 头部区长度且 4 字节对齐，128 满足；这里按
# 契约的**字面数值**固定，避免工具/文档/验收各方对"124 还是 128"各说各话。
PCM_DATA_START_MIN = 128

SAMPLE_RATE = 16000
PROMPT_COUNT = 5
# (id, name)；顺序即日志/测试脚本使用的名字，id 必须与固件 prompt_id_t 一致
EXPECTED_PROMPTS: tuple[tuple[int, str], ...] = (
    (0, "wake"),
    (1, "net-offline"),
    (2, "cloud-error"),
    (3, "timeout"),
    (4, "tts-fail"),
)
NAME_BY_ID = dict(EXPECTED_PROMPTS)

# assets 分区：partitions.csv 定稿为 assets,data,spiffs,0x7a0000,2M
# （方案 D 不用 SPIFFS，只借用这个 2M 数据分区放自定义 pack；偏移与大小必须与分区表一致）
ASSETS_PART_OFFSET = 0x7A0000
ASSETS_PART_SIZE = 2 * 1024 * 1024
PARTITIONS_CSV = REPO_ROOT / "partitions.csv"

# ---- 音频契约（T3.6 定稿口径，--check 断言；改动需同步 docs/ARCHITECTURE.md §3.4）----
PEAK_MIN = 12000             # 峰值下限（约 -8.7 dBFS）
PEAK_MAX = 14500             # 峰值上限（约 -7.1 dBFS）；T3.6 实际统一 13311（-7.82 dBFS）
DURATION_BOUNDS_MS = {       # 每条提示音的时长范围（wake 由原「我在呢」资源定稿）
    0: (750, 850),           # wake：固定 0.80 s
}
DURATION_DEFAULT_MS = (600, 2000)  # 错误提示音 0.6~2.0 s
EDGE_MS = 20                 # 首/尾 20ms 内 |x| < EDGE_MAX（防 pop）
EDGE_MAX = 200
DC_ABS_MAX = 100             # |mean| < 100


# ---------------------------------------------------------------- 异常类型


class AudioPackError(RuntimeError):
    """输入/环境错误（无法继续）；返回码 1，原因打印到 stderr。"""


# ---------------------------------------------------------------- 基础工具


def align4(value: int) -> int:
    """向上对齐到 4 字节（pack 内所有 PCM 起点/总长都要求）。"""
    return (value + PACK_ALIGN - 1) & ~(PACK_ALIGN - 1)


def dbfs(amplitude: float) -> float:
    """线性幅度（16bit 满量程 32768）转 dBFS。"""
    if amplitude <= 0:
        return -math.inf
    return 20.0 * math.log10(amplitude / 32768.0)


def pcm_to_samples(pcm: bytes) -> array.array:
    """LE int16 PCM 字节 → 宿主序 array('h')（长度必须为偶数）。"""
    if len(pcm) % 2 != 0:
        raise AudioPackError(f"PCM 字节数为奇数（{len(pcm)}），不是 16bit 样本")
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def pcm_bytes(samples: array.array) -> bytes:
    """宿主序 array('h') → LE int16 PCM 字节（与固件内存布局/闪存内容一致）。"""
    le = array.array("h", samples)
    if sys.byteorder != "little":
        le.byteswap()
    return le.tobytes()


def crc32(data: bytes) -> int:
    """标准 CRC-32/ISO-HDLC（= zlib.crc32；固件 esp_rom_crc32_le 同参数）。"""
    return zlib.crc32(data) & 0xFFFFFFFF


# ---------------------------------------------------------------- WAV 读写


@dataclass(frozen=True)
class WavData:
    """一个严格校验过的 16kHz/16bit/mono WAV。"""

    path: Path
    pcm: bytes          # LE int16 样本字节（不含 WAV 头）
    samples: array.array


def read_wav(path: Path) -> WavData:
    """读 WAV 并严格校验为 16kHz / 16bit / 单声道 PCM；不满足抛 AudioPackError。"""
    try:
        with wave.open(str(path), "rb") as w:
            nchannels = w.getnchannels()
            sampwidth = w.getsampwidth()
            framerate = w.getframerate()
            nframes = w.getnframes()
            comptype = w.getcomptype()
            frames = w.readframes(nframes)
    except FileNotFoundError as exc:
        raise AudioPackError(f"{path}: 文件不存在") from exc
    except (wave.Error, EOFError) as exc:
        raise AudioPackError(f"{path}: 无法解析 WAV（{exc}）") from exc

    problems = []
    if nchannels != 1:
        problems.append(f"{nchannels} 声道（要求 1）")
    if sampwidth != 2:
        problems.append(f"{sampwidth * 8}bit（要求 16bit）")
    if framerate != SAMPLE_RATE:
        problems.append(f"{framerate}Hz（要求 {SAMPLE_RATE}Hz）")
    if comptype != "NONE":
        problems.append(f"压缩类型 {comptype}（要求未压缩 PCM）")
    if nframes <= 0:
        problems.append("采样点数为 0")
    if len(frames) != nframes * 2:
        problems.append(f"data 块长度 {len(frames)} B 与帧数 {nframes} 不符")
    if problems:
        raise AudioPackError(f"{path}: 格式不符：{'；'.join(problems)}")
    return WavData(path=path, pcm=frames, samples=pcm_to_samples(frames))


def write_wav_mono16(path: Path, pcm: bytes) -> None:
    """把 LE int16 PCM 字节写成标准 44 字节头的 16kHz/16bit/mono WAV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


# ---------------------------------------------------------------- 统计与校验


def clip_stats(pcm: bytes) -> dict:
    """一条 PCM 的全部校验/展示指标（一次算完，避免在检查逻辑里重复遍历）。"""
    samples = pcm_to_samples(pcm)
    stats = samples_stats(samples)
    stats["crc32"] = crc32(pcm)
    stats["sha256"] = hashlib.sha256(pcm).hexdigest()
    return stats


def samples_stats(samples: array.array) -> dict:
    n = len(samples)
    peak = max(abs(x) for x in samples) if n else 0
    mean = (sum(samples) / n) if n else 0.0
    rms = math.sqrt(sum(x * x for x in samples) / n) if n else 0.0
    edge = min(EDGE_MS * SAMPLE_RATE // 1000, n // 2)
    head_max = max(abs(x) for x in samples[:edge]) if edge else 0
    tail_max = max(abs(x) for x in samples[-edge:]) if edge else 0
    return {
        "samples": n,
        "duration_ms": n * 1000.0 / SAMPLE_RATE,
        "peak": peak,
        "peak_dbfs": dbfs(peak),
        "mean": mean,
        "rms": rms,
        "rms_dbfs": dbfs(rms),
        "head_max": head_max,
        "tail_max": tail_max,
    }


def duration_bounds(prompt_id: int) -> tuple[int, int]:
    return DURATION_BOUNDS_MS.get(prompt_id, DURATION_DEFAULT_MS)


def validate_clip(stats: dict, prompt_id: int, label: str) -> list[str]:
    """单条 PCM 的契约指标（时长/峰值/首尾静音/直流）；返回问题列表（空 = PASS）。

    不校验样本数（生成/重生成时样本数由音频内容决定，尺寸基准由 manifest 单独记录）。
    """
    problems: list[str] = []
    if stats["samples"] <= 0:
        return [f"{label}: 采样点数为 0"]
    lo_ms, hi_ms = duration_bounds(prompt_id)
    if not (lo_ms <= stats["duration_ms"] <= hi_ms):
        problems.append(f"{label}: 时长 {stats['duration_ms']:.1f}ms 超出 {lo_ms}~{hi_ms}ms")
    if not (PEAK_MIN <= stats["peak"] <= PEAK_MAX):
        problems.append(
            f"{label}: 峰值 {stats['peak']} 超出 {PEAK_MIN}~{PEAK_MAX}"
            f"（{stats['peak_dbfs']:.2f} dBFS）"
        )
    if stats["head_max"] >= EDGE_MAX:
        problems.append(f"{label}: 首 {EDGE_MS}ms 峰值 {stats['head_max']} 未 < {EDGE_MAX}（防 pop）")
    if stats["tail_max"] >= EDGE_MAX:
        problems.append(f"{label}: 尾 {EDGE_MS}ms 峰值 {stats['tail_max']} 未 < {EDGE_MAX}（防 pop）")
    if abs(stats["mean"]) >= DC_ABS_MAX:
        problems.append(f"{label}: 直流偏置 |mean|={abs(stats['mean']):.2f} 未 < {DC_ABS_MAX}")
    return problems


# ---------------------------------------------------------------- manifest


@dataclass(frozen=True)
class ManifestEntry:
    id: int
    name: str
    file: str
    samples: int | None       # 可选扩展：样本数基准（原 prompt_pcm_sizes.h 的替代）
    pcm_sha256: str | None    # 可选扩展：PCM 字节 sha256（迁移/回归对拍用）

@dataclass(frozen=True)
class Manifest:
    version: int
    sample_rate: int
    entries: list[ManifestEntry]


def load_manifest(path: Path) -> Manifest:
    """解析 manifest.json（结构错误抛异常；ID/顺序/文件存在性等语义检查在 check_sources）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AudioPackError(f"manifest 不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise AudioPackError(f"manifest 不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise AudioPackError(f"manifest 顶层必须是对象：{path}")
    version = raw.get("version")
    sample_rate = raw.get("sample_rate")
    prompts = raw.get("prompts")
    if version != PACK_VERSION:
        raise AudioPackError(f"manifest.version={version!r}，期望 {PACK_VERSION}")
    if sample_rate != SAMPLE_RATE:
        raise AudioPackError(f"manifest.sample_rate={sample_rate!r}，期望 {SAMPLE_RATE}")
    if not isinstance(prompts, list) or not prompts:
        raise AudioPackError("manifest.prompts 必须是非空数组")
    entries: list[ManifestEntry] = []
    for index, item in enumerate(prompts):
        if not isinstance(item, dict):
            raise AudioPackError(f"manifest.prompts[{index}] 必须是对象")
        for key in ("id", "name", "file"):
            if key not in item:
                raise AudioPackError(f"manifest.prompts[{index}] 缺少 {key}")
        pid, name, filename = item["id"], item["name"], item["file"]
        if not isinstance(pid, int) or isinstance(pid, bool):
            raise AudioPackError(f"manifest.prompts[{index}].id 必须是整数")
        if not isinstance(name, str) or not isinstance(filename, str):
            raise AudioPackError(f"manifest.prompts[{index}].name/file 必须是字符串")
        if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
            raise AudioPackError(f"manifest.prompts[{index}].file 必须是纯文件名：{filename!r}")
        samples = item.get("samples")
        if samples is not None and (not isinstance(samples, int) or samples <= 0):
            raise AudioPackError(f"manifest.prompts[{index}].samples 必须是正整数")
        sha = item.get("pcm_sha256")
        if sha is not None and not (isinstance(sha, str) and len(sha) == 64):
            raise AudioPackError(f"manifest.prompts[{index}].pcm_sha256 必须是 64 位十六进制字符串")
        entries.append(ManifestEntry(pid, name, filename, samples, sha))
    return Manifest(version=version, sample_rate=sample_rate, entries=entries)


def write_manifest(path: Path, entries: list[dict]) -> None:
    """按契约规范写 manifest：id 升序，带 samples / pcm_sha256 扩展字段。

    path 的父目录由调用方保证；条目字段与 load_manifest 一一对应。
    """
    prompts = [
        {
            "id": int(item["id"]),
            "name": item["name"],
            "file": item["file"],
            "samples": int(item["samples"]),
            "pcm_sha256": item["pcm_sha256"],
        }
        for item in sorted(entries, key=lambda e: int(e["id"]))
    ]
    document = {"version": PACK_VERSION, "sample_rate": SAMPLE_RATE, "prompts": prompts}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 源集合（manifest + WAV）


@dataclass(frozen=True)
class SourceClip:
    id: int
    name: str
    file: str
    path: Path
    pcm: bytes
    stats: dict


@dataclass
class Sources:
    src_dir: Path
    manifest_path: Path
    clips: list[SourceClip]      # 按 id 升序，且只含校验后成功读到的条目
    problems: list[str]

    @property
    def total_pcm(self) -> int:
        return sum(len(clip.pcm) for clip in self.clips)


def data_start_for(count: int) -> int:
    """PCM 数据区起点：至少 128（契约冻结值，count=5 → 128）且 4 字节对齐。"""
    return max(align4(HDR_SIZE + count * ENTRY_SIZE), PCM_DATA_START_MIN)


def layout(count: int, lengths: list[int]) -> tuple[int, list[int], int]:
    """计算 pack 布局：返回 (PCM 起点, 每条 offset, total_len)。

    规则（契约 §2）：PCM 起点 = 128（count=5；一般化取 ≥128 且 4 字节对齐）；每条 PCM
    起点 4 字节对齐；尾部补 0 到 4 字节对齐。
    """
    data_start = data_start_for(count)
    offsets: list[int] = []
    cursor = data_start
    for length in lengths:
        offsets.append(cursor)
        cursor = align4(cursor + length)
    return data_start, offsets, cursor


def load_sources(src_dir: Path, manifest_path: Path) -> Sources:
    """读 manifest + 5 条 WAV，做契约校验；返回源集合与问题列表（不抛异常给调用方判断）。"""
    manifest = load_manifest(manifest_path)
    problems: list[str] = []
    clips: list[SourceClip] = []

    by_id: dict[int, ManifestEntry] = {}
    seen_files: dict[str, int] = {}
    for entry in manifest.entries:
        if entry.id in by_id:
            problems.append(f"manifest: id={entry.id} 重复（{by_id[entry.id].name} / {entry.name}）")
            continue
        by_id[entry.id] = entry
        if entry.file in seen_files:
            problems.append(f"manifest: 文件 {entry.file} 被 id {seen_files[entry.file]} 与 {entry.id} 复用")
        seen_files[entry.file] = entry.id

    expected_ids = [pid for pid, _ in EXPECTED_PROMPTS]
    actual_ids = [entry.id for entry in manifest.entries]
    if actual_ids != expected_ids:
        problems.append(f"manifest: id 列表 {actual_ids} 与期望 {expected_ids} 不一致（须按 id 升序且完整）")
    for pid, name in EXPECTED_PROMPTS:
        if pid not in by_id:
            problems.append(f"manifest: 缺少 id={pid}（{name}）")
    for pid in by_id:
        if pid not in expected_ids:
            problems.append(f"manifest: 多出未知 id={pid}（期望 0~{PROMPT_COUNT - 1}）")

    for pid, name in EXPECTED_PROMPTS:
        entry = by_id.get(pid)
        if entry is None:
            continue
        label = f"id={pid} {name}"
        if entry.name != name:
            problems.append(f"manifest: {label} 的 name={entry.name!r} 与契约 {name!r} 不一致")
        path = src_dir / entry.file
        try:
            wav = read_wav(path)
        except AudioPackError as exc:
            problems.append(str(exc))
            continue
        stats = clip_stats(wav.pcm)
        problems.extend(validate_clip(stats, pid, label))
        if entry.samples is not None and entry.samples != stats["samples"]:
            problems.append(
                f"manifest: {label} 声明 {entry.samples} 样本，实际 WAV {stats['samples']} 样本"
                "（若是有意重新生成，请用 tools/prompt_gen.py 或手工同步 manifest）"
            )
        if entry.pcm_sha256 is not None and entry.pcm_sha256 != stats["sha256"]:
            problems.append(f"manifest: {label} 的 pcm_sha256 与 {path.name} 实际 PCM 不一致")
        clips.append(SourceClip(pid, name, entry.file, path, wav.pcm, stats))

    clips.sort(key=lambda clip: clip.id)
    if len(clips) == len(EXPECTED_PROMPTS):
        _, _, total_len = layout(len(clips), [len(clip.pcm) for clip in clips])
        if total_len > ASSETS_PART_SIZE:
            problems.append(
                f"pack 总长 {total_len} B 超过 assets 分区 {ASSETS_PART_SIZE} B（{ASSETS_PART_SIZE // 1024} KB）"
            )
    return Sources(src_dir=src_dir, manifest_path=manifest_path, clips=clips, problems=problems)


# ---------------------------------------------------------------- 构建 pack


def build_pack_bytes(sources: Sources) -> tuple[bytes, dict]:
    """把已校验的源集合序列化成 pack（纯函数、无副作用、可重复）。"""
    if len(sources.clips) != PROMPT_COUNT:
        raise AudioPackError(f"源条目数 {len(sources.clips)} != {PROMPT_COUNT}，拒绝构建")
    if sources.problems:
        raise AudioPackError("源校验未通过，拒绝构建")

    count = len(sources.clips)
    lengths = [len(clip.pcm) for clip in sources.clips]
    data_start, offsets, total_len = layout(count, lengths)

    header_zero_crc = HDR_STRUCT.pack(PACK_MAGIC, PACK_VERSION, count, total_len, 0, 0)
    entries_bytes = bytearray()
    for clip, offset in zip(sources.clips, offsets):
        entries_bytes += ENTRY_STRUCT.pack(
            clip.id, offset, len(clip.pcm), SAMPLE_RATE, clip.stats["crc32"]
        )
    table = header_zero_crc + bytes(entries_bytes)
    header_crc = crc32(table)
    header = HDR_STRUCT.pack(PACK_MAGIC, PACK_VERSION, count, total_len, header_crc, 0)

    pack = bytearray(total_len)  # 全 0 初始化：条目间/尾部对齐填充自然为 0
    pack[0:HDR_SIZE] = header
    pack[HDR_SIZE:HDR_SIZE + len(entries_bytes)] = entries_bytes
    for clip, offset in zip(sources.clips, offsets):
        pack[offset:offset + len(clip.pcm)] = clip.pcm

    return bytes(pack), {
        "data_start": data_start,
        "offsets": offsets,
        "total_len": total_len,
        "header_crc": header_crc,
        "table_len": HDR_SIZE + count * ENTRY_SIZE,
    }


# ---------------------------------------------------------------- 分区表守卫


def check_partition_layout() -> list[str]:
    """对照 partitions.csv 的 assets 行，防止 pack 偏移/容量与分区表脱节。

    方案 D 只借用 assets 这个 2M 数据分区（SubType 仍标 spiffs 是历史遗留，固件不挂
    SPIFFS），所以偏移/大小为硬约束：分区表改了而这里没改会写坏别的分区。
    """
    if not PARTITIONS_CSV.exists():
        return [f"警告：找不到 {PARTITIONS_CSV}，跳过分区表对照"]
    for line in PARTITIONS_CSV.read_text(encoding="utf-8").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5 or parts[0] != "assets":
            continue
        try:
            offset = int(parts[3], 0)
            size_text = parts[4].upper()
            multiplier = 1
            if size_text.endswith("K"):
                multiplier, size_text = 1024, size_text[:-1]
            elif size_text.endswith("M"):
                multiplier, size_text = 1024 * 1024, size_text[:-1]
            size = int(size_text, 0) * multiplier
        except ValueError:
            return [f"警告：partitions.csv 的 assets 行无法解析：{line}"]
        problems = []
        if offset != ASSETS_PART_OFFSET:
            problems.append(
                f"partitions.csv: assets 偏移 {hex(offset)} != 工具常量 {hex(ASSETS_PART_OFFSET)}"
                "（pack 偏移/烧录地址已冻结在 build/t35_contract.md §2，需同步改动）"
            )
        if size != ASSETS_PART_SIZE:
            problems.append(
                f"partitions.csv: assets 大小 {size} B != 工具常量 {ASSETS_PART_SIZE} B（需同步改动）"
            )
        return problems
    return [f"警告：partitions.csv 中找不到 assets 分区行（工具按 {hex(ASSETS_PART_OFFSET)} 处理）"]


# ---------------------------------------------------------------- 展示


def print_source_table(clips: list[SourceClip]) -> None:
    print(f"  {'id':>2}  {'名称':<12} {'文件':<15} {'样本数':>6}  {'时长ms':>8}  "
          f"{'峰值':>5}  {'RMS':>7}  {'直流':>7}  {'首/尾静音':>10}  {'crc32':>10}  sha256")
    for clip in clips:
        st = clip.stats
        print(
            f"  {clip.id:>2}  {clip.name:<12} {clip.file:<15} {st['samples']:>6}  "
            f"{st['duration_ms']:>8.1f}  {st['peak']:>5}  {st['rms']:>7.1f}  {st['mean']:>7.2f}  "
            f"{st['head_max']:>4}/{st['tail_max']:<4} {st['crc32']:>10}  {st['sha256'][:12]}"
        )


def print_pack_table(sources: Sources, layout_info: dict) -> None:
    print(f"  {'id':>2}  {'名称':<12} {'offset':>8}  {'length':>7}  {'样本数':>6}  "
          f"{'时长ms':>8}  {'采样率':>6}  {'crc32':>10}")
    for clip, offset in zip(sources.clips, layout_info["offsets"]):
        st = clip.stats
        print(
            f"  {clip.id:>2}  {clip.name:<12} {offset:>8}  {len(clip.pcm):>7}  {st['samples']:>6}  "
            f"{st['duration_ms']:>8.1f}  {SAMPLE_RATE:>6}  {st['crc32']:>10}"
        )


def print_problems(problems: list[str]) -> None:
    for problem in problems:
        print(f"  FAIL  {problem}", file=sys.stderr)


def print_pack_summary(pack: bytes, sources: Sources, layout_info: dict) -> None:
    total_len = len(pack)
    pcm_total = sources.total_pcm
    padding = total_len - layout_info["data_start"] - pcm_total
    print(f"\n  header {HDR_SIZE} B + entries {len(sources.clips) * ENTRY_SIZE} B（头部区 "
          f"{layout_info['table_len']} B）+ PCM {pcm_total} B + 对齐填充 {padding} B")
    print(f"  pack 总长 {total_len} B（{total_len / 1024:.2f} KB）；assets 分区 "
          f"{ASSETS_PART_SIZE // 1024} KB @ {hex(ASSETS_PART_OFFSET)}，"
          f"余量 {100.0 * (1 - total_len / ASSETS_PART_SIZE):.1f}%")
    print(f"  header_crc32 0x{layout_info['header_crc']:08X}；pack sha256 {hashlib.sha256(pack).hexdigest()}")


# ---------------------------------------------------------------- 模式：--check


def cmd_check(args: argparse.Namespace) -> int:
    src_dir = Path(args.src_dir)
    manifest_path = Path(args.manifest) if args.manifest else src_dir / "manifest.json"
    print(f"校验源目录：{src_dir}")
    print(f"manifest：{manifest_path}")
    partition_problems = check_partition_layout()
    partition_checks = [p for p in partition_problems if not p.startswith("警告：")]

    try:
        sources = load_sources(src_dir, manifest_path)
    except AudioPackError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print_source_table(sources.clips)
    problems = partition_checks + sources.problems
    if sources.problems:
        print_problems(sources.problems)
        print(f"校验失败：{len(sources.problems)} 项", file=sys.stderr)
        return 1
    for warning in partition_problems:
        if warning.startswith("警告："):
            print(f"  {warning}", file=sys.stderr)
    if problems:
        print_problems(problems)
        print(f"校验失败：{len(problems)} 项", file=sys.stderr)
        return 1

    _, _, total_len = layout(len(sources.clips), [len(clip.pcm) for clip in sources.clips])
    print(f"\n  PCM 合计 {sources.total_pcm} B（{sources.total_pcm / 1024:.1f} KB）；"
          f"pack 预计总长 {total_len} B（{total_len / 1024:.2f} KB，assets 分区 "
          f"{ASSETS_PART_SIZE // 1024} KB）")
    print(f"校验通过：{len(sources.clips)} 条提示音"
          "（WAV 格式/样本数/时长/峰值/首尾静音/直流/ID 完整性）")
    return 0


# ---------------------------------------------------------------- 模式：构建（默认）


def cmd_build(args: argparse.Namespace) -> int:
    src_dir = Path(args.src_dir)
    manifest_path = Path(args.manifest) if args.manifest else src_dir / "manifest.json"
    out_path = Path(args.out)

    print(f"源目录：{src_dir}")
    print(f"manifest：{manifest_path}")
    print(f"输出：{out_path}")
    partition_problems = check_partition_layout()
    partition_errors = [p for p in partition_problems if not p.startswith("警告：")]

    try:
        sources = load_sources(src_dir, manifest_path)
    except AudioPackError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print_source_table(sources.clips)
    if partition_errors:
        print_problems(partition_errors)
        print("分区表与工具常量不一致，拒绝构建/烧录", file=sys.stderr)
        return 1
    for warning in partition_problems:
        if warning.startswith("警告："):
            print(f"  {warning}", file=sys.stderr)
    if sources.problems:
        print_problems(sources.problems)
        print(f"构建失败：源校验 {len(sources.problems)} 项不通过", file=sys.stderr)
        return 1

    try:
        pack, layout_info = build_pack_bytes(sources)
    except AudioPackError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(pack)
    print(f"\n已写 pack：{out_path}")
    print_pack_table(sources, layout_info)
    print_pack_summary(pack, sources, layout_info)

    if args.flash:
        return flash_pack(out_path, args.flash)
    print("\n完成。烧录：uv run tools/audio_pack.py --flash /dev/cu.usbmodemXXXX（只写 assets 分区，app 不动）")
    return 0


# ---------------------------------------------------------------- 模式：--dump


def cmd_dump(args: argparse.Namespace) -> int:
    path = Path(args.dump)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        print(f"错误：pack 不存在：{path}", file=sys.stderr)
        return 1
    print(f"解析 pack：{path}（{len(data)} B，sha256 {hashlib.sha256(data).hexdigest()}）")
    problems: list[str] = []

    if len(data) < HDR_SIZE:
        print(f"错误：文件只有 {len(data)} B，不足头部 {HDR_SIZE} B", file=sys.stderr)
        return 1
    magic, version, count, total_len, stored_header_crc, reserved = HDR_STRUCT.unpack_from(data, 0)
    table_len = HDR_SIZE + count * ENTRY_SIZE
    print(f"\n[header] magic={magic!r} version={version} count={count} total_len={total_len} "
          f"header_crc32=0x{stored_header_crc:08X} reserved={reserved}")
    if magic != PACK_MAGIC:
        problems.append(f"magic {magic!r} != {PACK_MAGIC!r}")
    if version != PACK_VERSION:
        problems.append(f"version {version} != {PACK_VERSION}")
    if not (1 <= count <= MAX_ENTRIES):
        problems.append(f"count {count} 不在 1~{MAX_ENTRIES}")
        print_problems(problems)
        return 1
    if count != PROMPT_COUNT:
        problems.append(f"count {count} != 固件期望 PROMPT_COUNT={PROMPT_COUNT}")
    if len(data) < table_len:
        print(f"错误：文件 {len(data)} B 不足头部区 {table_len} B", file=sys.stderr)
        return 1
    if total_len != len(data):
        problems.append(f"total_len {total_len} != 文件大小 {len(data)}（pack 应恰好占 total_len）")
    if total_len % PACK_ALIGN != 0:
        problems.append(f"total_len {total_len} 未 {PACK_ALIGN} 字节对齐")
    if total_len > ASSETS_PART_SIZE:
        problems.append(f"total_len {total_len} 超过 assets 分区 {ASSETS_PART_SIZE} B")
    if reserved != 0:
        problems.append(f"header.reserved={reserved} != 0")

    zeroed = bytearray(data[:table_len])
    zeroed[16:20] = b"\x00\x00\x00\x00"  # header_crc32 字段按 0 计算
    computed_header_crc = crc32(bytes(zeroed))
    print(f"  header_crc32 校验：存 0x{stored_header_crc:08X} / 算 0x{computed_header_crc:08X} "
          f"{'OK' if stored_header_crc == computed_header_crc else 'MISMATCH'}")
    if stored_header_crc != computed_header_crc:
        problems.append("header_crc32 不匹配")

    entries: list[dict] = []
    seen_ids: list[int] = []
    data_start = data_start_for(count)
    print(f"\n[entries] 共 {count} 条（头部区 {table_len} B；PCM 数据区从 {data_start} 开始）")
    print(f"  {'idx':>3}  {'id':>2}  {'名称':<12} {'offset':>8}  {'length':>7}  {'样本数':>6}  "
          f"{'时长ms':>8}  {'采样率':>6}  {'crc32(存)':>10}  {'crc32(算)':>10}  校验")
    for index in range(count):
        base = HDR_SIZE + index * ENTRY_SIZE
        eid, offset, length, sample_rate, stored_crc = ENTRY_STRUCT.unpack_from(data, base)
        entries.append({"index": index, "id": eid, "offset": offset, "length": length,
                        "sample_rate": sample_rate, "crc32": stored_crc})
        seen_ids.append(eid)
        label = NAME_BY_ID.get(eid, "?")
        entry_problems: list[str] = []
        if eid >= PROMPT_COUNT:
            entry_problems.append(f"id {eid} 越界")
        if sample_rate != SAMPLE_RATE:
            entry_problems.append(f"采样率 {sample_rate}")
        if offset % PACK_ALIGN != 0:
            entry_problems.append("offset 未对齐")
        if length == 0 or length % 2 != 0:
            entry_problems.append("length 非正偶数")
        if offset < table_len:
            entry_problems.append("offset 落在头部区")
        computed_crc = None
        if 0 <= offset and offset + length <= len(data) and length > 0:
            computed_crc = crc32(data[offset:offset + length])
        else:
            entry_problems.append("offset+length 越界")
        if computed_crc is not None and stored_crc != computed_crc:
            entry_problems.append("PCM crc32 不匹配")
        samples = length // 2 if length % 2 == 0 else 0
        duration_ms = samples * 1000.0 / SAMPLE_RATE
        print(
            f"  {index:>3}  {eid:>2}  {label:<12} {offset:>8}  {length:>7}  {samples:>6}  "
            f"{duration_ms:>8.1f}  {sample_rate:>6}  {stored_crc:>10}  "
            f"{(computed_crc if computed_crc is not None else -1):>10}  "
            f"{'OK' if not entry_problems else '; '.join(entry_problems)}"
        )
        problems.extend(f"entry[{index}] id={eid}: {p}" for p in entry_problems)

    if sorted(seen_ids) != [pid for pid, _ in EXPECTED_PROMPTS]:
        problems.append(f"条目 id 集合 {sorted(seen_ids)} != {[pid for pid, _ in EXPECTED_PROMPTS]}")
    if [entry["id"] for entry in entries] != sorted(entry["id"] for entry in entries):
        problems.append("条目未按 id 升序排列")

    # 布局检查：offset 单调、互不重叠、数据区外的填充为 0
    if entries and entries[0]["offset"] != data_start:
        problems.append(f"首条 offset {entries[0]['offset']} != PCM 数据区起点 {data_start}")
    cursor = data_start
    for entry in entries:
        if entry["offset"] < cursor:
            problems.append(f"entry[{entry['index']}] offset {entry['offset']} 与前一条重叠/倒序")
        elif entry["offset"] > cursor:
            gap = data[cursor:entry["offset"]]
            # 条目之间只允许 0~3 字节的 0 填充（4 字节对齐）
            if entry["offset"] - cursor > PACK_ALIGN - 1 or any(gap):
                problems.append(f"entry[{entry['index']}] 前有非对齐填充或空洞")
        cursor = max(cursor, entry["offset"] + entry["length"])
    if cursor < len(data):
        tail = data[cursor:]
        if len(tail) >= PACK_ALIGN or any(tail):
            problems.append(f"尾部 {len(tail)} B 非零或 padding 过多")

    if problems:
        print()
        print_problems(problems)
        print(f"\ndump 失败：{len(problems)} 项", file=sys.stderr)
        return 1
    print(f"\ndump 通过：header_crc32 与 {count} 条 PCM crc32 全部匹配，布局符合契约")
    return 0


# ---------------------------------------------------------------- 模式：--flash


def flash_pack(out_path: Path, port: str) -> int:
    """调 esptool 只写 assets 分区（0x7a0000）；**不在本工具内做任何板级假设/重试**。"""
    if not out_path.exists():
        print(f"错误：pack 不存在：{out_path}", file=sys.stderr)
        return 1
    offset = hex(ASSETS_PART_OFFSET)
    module_cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "-p", port,
                  "write_flash", offset, str(out_path)]
    if importlib.util.find_spec("esptool") is not None:
        cmd = module_cmd
    elif (esptool_path := shutil.which("esptool") or shutil.which("esptool.py")):
        cmd = [esptool_path, "--chip", "esp32s3", "-p", port, "write_flash", offset, str(out_path)]
    else:
        print("错误：找不到 esptool（本工具只用 stdlib，烧录需外部 esptool）。"
              "可任选其一：\n"
              "  uv run --with esptool tools/audio_pack.py --flash <port>\n"
              "  python3 -m esptool ...（IDF 环境自带）\n"
              "  esptool --chip esp32s3 -p <port> write_flash 0x7a0000 build/assets.bin", file=sys.stderr)
        return 1
    print(f"\n烧录 assets 分区（app 不动）：{offset} ← {out_path}（{out_path.stat().st_size} B）")
    print("  " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd)
    except OSError as exc:
        print(f"错误：esptool 启动失败：{exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"错误：esptool 退出码 {proc.returncode}", file=sys.stderr)
        return 1
    print("烧录完成。复位后 `prompt info` 查看 pack 版本/条目数/映射模式。")
    return 0


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="T3.5 提示音资源打包：tools/prompts_src/*.wav → assets 分区自定义只读 pack",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--src-dir", default=str(DEFAULT_SRC_DIR), help="源 WAV 目录")
    parser.add_argument("--manifest", default=None,
                        help="manifest.json 路径（默认 <src-dir>/manifest.json）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 pack 路径")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只校验源 WAV，不写文件")
    mode.add_argument("--dump", metavar="BIN", help="解析并校验已有 pack")
    mode.add_argument("--flash", metavar="PORT", help="构建后调 esptool 只烧 assets 分区（0x7a0000）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.dump:
            return cmd_dump(args)
        if args.check:
            return cmd_check(args)
        return cmd_build(args)
    except AudioPackError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
