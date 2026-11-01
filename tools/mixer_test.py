#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5", "numpy>=1.26"]
# ///
"""T3.3-B 三通道混音真机验收：混音/duck/音量线性/软限幅/淡入淡出/堆稳定。

**这是一条真机验收脚本，不修改固件、不 build/flash**。设备一侧由 T3.3-A 在
`audio_player` 里实现「三通道混音（CH_TTS/CH_PROMPT/CH_MUSIC）→ 音量 → -1dBFS 软限幅
→ I2S」，并在 `diag` 里新增一组 `player mix ...` console 探针。本脚本只消费探针输出、
按前缀过滤设备日志、用 `player mix cap/dump` 把 48k 单声道混音输出抓回 PC 做 FFT/包络断言。

**探针契约（固件输出以此为准；脚本只消费、不猜字段）**：

    # 1) 状态：固定 4 行（MIXSTAT + 3 条 MIXCH）
    MIXSTAT vol=<0..100> vol_q8=<0..256> vol_target=<0..256> limiter=<n> \
limiter_peak=<n> clip=<n> mix_peak=<n> block_max_us=<n> cap_len=<n> cap_pos=<n>
    MIXCH ch=TTS active=<0|1> gain_q8=<0..256> level_ms=<n> peak=<n>
    MIXCH ch=PROMPT active=<0|1> gain_q8=<0..256> level_ms=<n> peak=<n>
    MIXCH ch=MUSIC active=<0|1> gain_q8=<0..256> level_ms=<n> peak=<n>

    # 2) 清统计 → MIXRESET ok=1（清 limiter/clip/mix_peak/block_max 与通道 peak）
    # 3) 运行时音量 → MIXVOL vol=<n> err=ESP_OK（20ms 斜坡，不落盘）
    # 4) 探针音调 → MIXTONE ch=<CH> freq=<n> ms=<n> amp=<n> err=<ESP_OK|错误名>
    # 5) 停探针 → MIXSTOP ok=1（停所有探针音源并等混音泵退出）
    # 6) 抓取武装 → MIXCAP armed=<frames>（从下一个混音输出块开始抓 48k 单声道，取满自动停）
    # 7) 取回抓取数据：
    #      MIXCAP off=<off> count=<n>
    #      <每行 16 个 %04x **连写、无分隔符**（如 fc180000...），int16 的 uint16 表示，
    #       如 fc18 → -1000；off+count > cap_pos 时改回 `MIXCAP err=ESP_ERR_INVALID_STATE`>
    #      ...
    #      MIXCAPEND

**真机时序校准（2026-11-02，Lead 实测后回灌）**：`player mix tone` 每条命令约 100ms 往返，
PROMPT/MUSIC 分别在 ~100/~200ms 才真正起播；且 duck 斜坡从"通道真正 active"才开始走
（未 active 的通道不更新 gain）。所以 A 必须"先起三路 → 等 0.35s → 再 arm"；E 必须用 440Hz
onset 定位叠加点再取稳态窗（不能从抓取起点 0 起）。B（20ms 音量斜坡）与 C（只看限幅/峰值）
不受这个影响。D 的淡出尾值由固件淡出步进取整决定（旧实现 65536/4800 向下取整=13，尾值停在
~4.8%），脚本按"末 1ms <5% 稳态"断言，固件修成向上取整后应≈0。

已知实现细节（按 T3.3-A 固件 `components/audio_player`、`components/diag` 当前实现校准）：
`MIXSTAT` 末尾的 `cap_len`=武装帧数、`cap_pos`=**实际抓到帧数**；`capture_read` 用 cap_pos 校验
（`off+count > cap_pos` 回 `MIXCAP err=...`），所以脚本每个场景先 stat 取 cap_pos，再只 dump
`[0, cap_pos)`——A/B/C/E 播放时长 > 341ms 应取满（cap_pos==cap_len），D 的 300ms 音调提前结束
（cap_pos≈14400）就 dump 14400 帧；PROMPT 通道活跃也会令 MUSIC duck -12dB（voice_on=TTS||PROMPT），
E 的稳态幅度按此断言。

字段语义：gain_q8 256=0dB、128≈-6dB、64≈-12dB；mix_peak 是最近 ~320ms 输出峰值
（int16 幅度）；limiter 是最近窗口内触发软限幅的样本数；clip 是硬削波次数（必须恒 0）；
block_max_us 是最近单块混音+限幅耗时（10ms 预算）；tone 相位从峰值开始（用于验证 10ms 淡入）。

**覆盖的 6 个场景（对应 T3.3 验收）**：

  A 三通道混音 + duck：**先**起 tone TTS 1000Hz / PROMPT 440Hz / MUSIC 1760Hz（各 2500ms、
    amp 8000），等 0.35s（三条 tone 命令各 ~100ms 往返 + 通道 10ms 淡入 + duck 100ms 斜坡），
    **再** arm cap 16384 → 抓满后 dump [0, cap_pos)。这样抓取窗 0..341ms 全程是稳态，
    直接取 0..8192 帧做 rFFT（汉宁窗、幅度归一化 2|X|/sum(win)）即可。断言三频幅度
    ≈8000/4000/2000（±8%，即 prompt duck -6dB、music duck -12dB）、比值 1:0.5:0.25（±8%）、
    `mix_peak` 与抓取峰值一致（±2%）、`clip=0`、抓取取满 cap_pos==cap_len==16384、
    通道 `gain_q8` 256/128/64、`active=1`；`block_max_us` 超 10ms 预算只记 WARN。
  B 音量线性：单通道 MUSIC 1000Hz/amp 8000/1500ms，`player mix vol 100/50/0` 三档
    分别抓取（先 vol 再 cap 再 tone），稳态窗幅度比 ≈ 1:0.5:0（±2%），0 档稳态窗全零
    且 `mix_peak=0`（20ms 音量斜坡只在混音泵渲染时推进，0 档抓取开头 ~20ms 有残余音量，
    故查 100ms 后的稳态窗而不是整段）。
  C 软限幅：三通道 440/1000/1760Hz、amp=20000、1500ms（TTS 活跃 → prompt/music 被
    duck，叠加仍远超 ceiling）。稳态窗输出逐样本 |x| ≤ 29196（-1dBFS，+32 量化余量），
    无 |x|≥32000 的硬削样品；`limiter>0`、`limiter_peak>29196`、`clip=0`。
  D 淡入淡出：cap 16384 在 tone 之前武装 → 单通道 TTS 1000Hz/amp 8000/300ms，只 dump
    cap_pos（300ms 对应 14400 帧）。断言起始 10ms 包络从 0 单调升（前 48 帧峰值很小）、
    结束前 100ms 包络单调降且**末 1ms 包络 <5% 稳态**；全程相邻样本 |Δ| ≤ 理论正弦最大
    斜率×1.5 + 包络步进（amp=8000@1kHz ≈1600，无 pop）。
  E 通道启停不 pop：`tone MUSIC 1760 1500 8000` 先单独播 300ms，再 `cap` 武装并叠加
    `tone PROMPT 440 800 8000`。cap 与 tone 命令之间有 ~50~100ms 往返，抓取窗开头仍是
    "MUSIC 全量"段，故**用 440Hz 能量定位叠加点 onset**，稳态窗从 onset+10ms(淡入)+100ms
    (MUSIC duck 斜坡) 起取（自适应 4096~8192 帧）；断言稳态 440Hz≈8000、1760Hz≈2000
    （PROMPT 令 MUSIC duck -12dB）、RMS≈5831、onset 处 440Hz 仍低、全程 |Δ| ≤ 3480。
  F 健康：10 轮短音调后 `sys heap` 内部堆峰谷差 ≤2KB、PSRAM 峰谷差 ≤4KB；每轮 `clip=0`；
    全程日志无 `E (`/`ERROR`/`panic`（网络/WS 类既有噪声按其它脚本口径忽略）。

**用法**（脚本独占串口，先关 `idf.py monitor`；设备烧录 T3.3 固件并启动后运行）：

    uv run tools/mixer_test.py                    # 全量真机验收（约 60~90s）
    uv run tools/mixer_test.py --dry-run          # 不连设备：打印执行计划 + 跑离线自检
    uv run tools/mixer_test.py --self-test        # 离线自检解析/FFT/断言逻辑（不连设备）
    uv run tools/mixer_test.py --port /dev/cu.usbmodemXXXX --log /tmp/mixer_test.log
    uv run tools/mixer_test.py --skip-heap        # 跳过 F（快速冒烟）
    uv run tools/mixer_test.py --reset            # 先复位设备再等 console 就绪

**与其它脚本的分工**：`tts_stream_test.py` 测「真实 MP3 流式播放」的音频硬指标
（首帧/欠载/abort/打断）；`mp3_test.py` 测「整段 MP3 解码对拍」；`voice_cloud_test.py`
测端云协议/事件。本脚本只测 **T3.3 混音链路的数值正确性**（不联网、不唤醒、不外放），
是 `audio_player` 混音改动后的定点回归手段；三者的断言口径互不复用，避免一套阈值掩盖另一套问题。

**音量前置与恢复**：运行时音量可能是设备当前值（31%/50%/用户设置），会直接缩放幅度，
因此 A/C/D/E/F 场景开头都显式 `player mix vol 100` 作为幅度基准（B 场景专门测 100/50/0）；
脚本进入时读一次 `player mix stat` 的 `vol`，结束用 `player mix vol <原值>` 恢复
（该探针不落盘，重上电即回到配置值）。

**环境性失败口径**：串口超时/探针回显被日志冲掉等环境问题按既有脚本口径记 WARN 并整场景
重试一次；**不改任何阈值**。固件真的不满足阈值时只 FAIL，不重试。

依赖（PEP 723 内联，`uv run` 自动装）：`pyserial>=3.5`、`numpy>=1.26`。
"""

from __future__ import annotations

import argparse
import glob
import math
import re
import sys
import time

BAUDRATE = 115200
RATE = 48000                 # 混音输出固定 48k 单声道（ARCHITECTURE §3.2）
CAP_MAX = 16384              # 探针抓取上限 = 341ms
CONSOLE_TIMEOUT = 2.0        # 普通 console 命令等待上限（契约：命令发出后最多等 2s）
DUMP_TIMEOUT = 3.0           # dump 是数据回传（512 样本 ≈32 行），给传输留余量
DUMP_CHUNK = 512             # 契约单次 dump 上限

STEADY_START = 4800          # 100ms：跳过 10ms 淡入 + 100ms duck 斜坡后的稳态窗起点
FFT_N = 8192                 # 稳态 FFT 窗长（170ms，频率分辨率 5.86Hz）
FADE_IN_FRAMES = 480         # 10ms 淡入
FADE_OUT_FRAMES = 4800       # 100ms 淡出

CEILING = 29196              # -1dBFS 软限幅上限（契约值）
CEILING_QUANT = 32           # 量化余量
HARD_CLIP = 32000            # |x| ≥ 此值视为硬削波
SLOPE_MARGIN = 1.5           # 斜率阈值 = 理论最大斜率 × 1.5 + 包络步进 + 量化余量
SLOPE_QUANT = 8
TONE_AMP = 8000              # 场景音调默认幅度
MAX_BLOCK_US = 10000         # 单块混音+限幅预算 10ms
ONSET_AMP_MIN = 1500.0       # E：440Hz 能量 onset 检测门限（稳态 ≈8000 的 ~19%）
DUCK_TOL = 0.08              # A：每路幅度比容差 ±8%
VOL_TOL = 0.02               # B：音量线性容差 ±2%
PEAK_TOL = 0.02              # mix_peak 与抓取峰值容差 ±2%
GAIN_TOL = 8                 # 通道 gain_q8 容差（256/128/64 附近）
HEAP_INT_TOL = 2048          # F：内部堆峰谷差 ≤2KB
HEAP_PSRAM_TOL = 4096        # F：PSRAM 峰谷差 ≤4KB

# ---------------------------------------------------------------- 探针输出正则
# 按固定前缀匹配；设备日志（带时间戳/TAG）与 console 混流，不匹配前缀的行一律丢弃。
RE_MIXSTAT = re.compile(
    r"MIXSTAT vol=(\d+) vol_q8=(\d+) vol_target=(\d+) limiter=(\d+) "
    r"limiter_peak=(\d+) clip=(\d+) mix_peak=(\d+) block_max_us=(\d+)"
    r"(?: cap_len=(\d+) cap_pos=(\d+))?"  # T3.3-A 最新固件追加的抓取状态（旧固件可选）
)
MIXSTAT_KEYS = (
    "vol", "vol_q8", "vol_target", "limiter", "limiter_peak", "clip",
    "mix_peak", "block_max_us", "cap_len", "cap_pos",
)
# dump 越界/未武装时固件返回的错误行（off+count > cap_pos → 抓取不完整）
RE_MIXCAP_ERR = re.compile(r"MIXCAP err=(\w+)")
RE_MIXCH = re.compile(
    r"MIXCH ch=(TTS|PROMPT|MUSIC) active=(\d+) gain_q8=(\d+) level_ms=(\d+) peak=(\d+)"
)
RE_MIXCH_TTS = re.compile(r"MIXCH ch=TTS ")
RE_MIXCH_PROMPT = re.compile(r"MIXCH ch=PROMPT ")
RE_MIXCH_MUSIC = re.compile(r"MIXCH ch=MUSIC ")
RE_MIXRESET = re.compile(r"MIXRESET ok=(\d+)")
RE_MIXVOL = re.compile(r"MIXVOL vol=(\d+) err=(\w+)")
RE_MIXTONE = re.compile(
    r"MIXTONE ch=(TTS|PROMPT|MUSIC) freq=(\d+) ms=(\d+) amp=(\d+) err=(\w+)"
)
RE_MIXSTOP = re.compile(r"MIXSTOP ok=(\d+)")
RE_MIXCAP_ARMED = re.compile(r"MIXCAP armed=(\d+)")
RE_MIXCAP_HDR = re.compile(r"MIXCAP off=(\d+) count=(\d+)")
RE_MIXCAP_END = re.compile(r"MIXCAPEND")
# 一行 1~16 个 4 位十六进制（%04x），整行匹配避免把日志误当样本
RE_HEX_ROW = re.compile(r"^[0-9a-fA-F]+$")  # 固件一行连写 16 个 %04x（无空格），也兼容空格分隔

# console 就绪 / 未知命令
RE_BOOT_READY = re.compile(r'ready, say "Hi Lexin"')
RE_UNKNOWN_CMD = re.compile(r"未知命令|Unrecognized command|无效命令")
# `sys heap`：与 voice_cloud_test.py 同口径
RE_HEAP_LINE = re.compile(
    r"(内部 RAM|PSRAM)\s*: 总量 (\d+) B, 剩余 (\d+) B, 最大块 (\d+) B, "
    r"最低水位 (\d+) B, 峰值占用 (\d+) B"
)
RE_HEAP_END = re.compile(r"(?:内部 RAM|PSRAM)\s*:.*(?:最低水位|未初始化)")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# 日志错误判定：与 voice_test.py/tts_stream_test.py 同口径（网络/WS 类既有噪声忽略，
# 本脚本不联网，但设备可能自己重连 WiFi 产生 NET/WS 的 E 行，那不是混音的问题）。
RE_ERROR = re.compile(
    r"E \(\d+\)|ERROR|panic|Guru Meditation|stack overflow|StoreProhibited|LoadProhibited|"
    r"abort\(\) was called|assert failed"
)
RE_TAG = re.compile(r"[EW] \(\d+\) ([A-Za-z_0-9-]+):")
# 与混音无关、但会在 mixer_test 运行期刷屏的既有噪声 TAG（真机 T3.3 实测补充）：
# mixer_test 不启动 mock，设备会反复重连 192.168.1.11:8765，esp-transport/websocket_client
# 族会以 E 行报连接失败；它们与 NET/WS 同类，不应算混音回归。
IGNORED_ERROR_TAGS = {
    "NET", "WS", "wifi", "websocket_client", "esp_netif", "esp-tls", "esp_wifi",
    "wpa_supplicant", "phy_init", "net80211", "esp_https_ota", "HTTP_CLIENT",
    # T3.3 真机补充（esp-transport/esp-tls 系列）
    "transport_base", "transport_ws", "transport_ssl", "transport_tcp", "esp_transport",
}
# 行内兜底：TAG 拼写变化/无 TAG 时，连接与重连类噪声也忽略；panic/assert/Guru 等真错误
# 不含这些片段，仍会被 error_lines() 判 FAIL。
IGNORED_ERROR_LINE = re.compile(
    r"Failed to open a new connection|Error connecting to host|esp_transport_connect|"
    r"WS 错误|连接断开|后重连"
)


# ---------------------------------------------------------------- 延迟依赖
_NP = None


def _np():
    """延迟导入 numpy：--dry-run 只打印计划时不强制依赖（PEP 723 会装，但保持轻量）。"""
    global _NP
    if _NP is None:
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - uv run 会自动安装
            raise SystemExit(
                "需要 numpy：请用 `uv run tools/mixer_test.py ...`（PEP 723 自动安装），"
                "或用 pip install numpy 后再跑"
            ) from exc
        _NP = np
    return _NP


# ---------------------------------------------------------------- 纯解析函数
# 这些函数不碰串口，真机场景与 --self-test 共用，保证「离线自检的就是真机断言逻辑」。

def parse_mixstat(lines: list[str]) -> dict | None:
    """解析 `player mix stat` 的 MIXSTAT 行（cap_len/cap_pos 由最新固件追加，旧固件为 None）。"""
    for line in lines:
        m = RE_MIXSTAT.search(line)
        if m:
            out: dict = {}
            for i, key in enumerate(MIXSTAT_KEYS):
                group = m.group(i + 1)
                out[key] = int(group) if group is not None else None
            return out
    return None


def parse_mixchannels(lines: list[str]) -> dict[str, dict]:
    """解析 `player mix stat` 的 3 条 MIXCH 行（ch -> 字段）。"""
    out: dict[str, dict] = {}
    for line in lines:
        m = RE_MIXCH.search(line)
        if m:
            out[m.group(1)] = {
                "active": int(m.group(2)),
                "gain_q8": int(m.group(3)),
                "level_ms": int(m.group(4)),
                "peak": int(m.group(5)),
            }
    return out


def parse_mixvol(lines: list[str]) -> dict | None:
    for line in lines:
        m = RE_MIXVOL.search(line)
        if m:
            return {"vol": int(m.group(1)), "err": m.group(2)}
    return None


def parse_mixtone(lines: list[str]) -> dict | None:
    for line in lines:
        m = RE_MIXTONE.search(line)
        if m:
            return {
                "ch": m.group(1),
                "freq": int(m.group(2)),
                "ms": int(m.group(3)),
                "amp": int(m.group(4)),
                "err": m.group(5),
            }
    return None


def parse_ok(pattern: re.Pattern, lines: list[str]) -> int | None:
    """解析形如 `MIXRESET ok=1` / `MIXSTOP ok=1` 的单行回显，返回 ok 值。"""
    for line in lines:
        m = pattern.search(line)
        if m:
            return int(m.group(1))
    return None


def parse_cap_armed(lines: list[str]) -> int | None:
    for line in lines:
        m = RE_MIXCAP_ARMED.search(line)
        if m:
            return int(m.group(1))
    return None


def _dump_row_tokens(line: str) -> list[str] | None:
    """把一行 dump 数据切成 4 位十六进制 token。

    固件 `printf("%04x", (uint16_t)v)` 是**连写**（一行 16 个、共 64 字符、无分隔符）；
    这里同时兼容空格分隔的写法，并要求长度是 4 的倍数、1~16 个样本，避免把日志误当数据。
    """
    s = line.strip().replace(" ", "")
    if not s or len(s) % 4 != 0 or len(s) > 64:
        return None
    if not RE_HEX_ROW.match(s):
        return None
    return [s[i:i + 4] for i in range(0, len(s), 4)]


def parse_dump(lines: list[str]) -> dict | None:
    """解析 `player mix dump` 回显：header + 若干 16 进制行 + MIXCAPEND。

    返回 {"off", "count", "samples", "end"}；样本按 uint16 → int16 二补码解释
    （设备按 `printf("%04x", (uint16_t)sample)` 打印数值本身，不是字节序）。
    """
    off = None
    count = None
    tokens: list[str] = []
    ended = False
    for line in lines:
        s = line.strip()
        m = RE_MIXCAP_HDR.search(s)
        if m:
            off = int(m.group(1))
            count = int(m.group(2))
            tokens = []
            continue
        if off is None:
            continue
        if RE_MIXCAP_END.search(s):
            ended = True
            break
        row = _dump_row_tokens(s)
        if row:
            tokens.extend(row)
    if off is None or not tokens:
        return None
    raw = [int(t, 16) for t in tokens]
    samples = [v - 0x10000 if v >= 0x8000 else v for v in raw]
    return {"off": off, "count": count, "samples": samples, "end": ended}


def parse_heap(lines: list[str]) -> dict[str, dict]:
    """解析 `sys heap` 的内部 RAM / PSRAM 行。"""
    heap: dict[str, dict] = {}
    for line in lines:
        m = RE_HEAP_LINE.search(line)
        if not m:
            continue
        key = "internal" if m.group(1) == "内部 RAM" else "psram"
        heap[key] = {
            "total": int(m.group(2)),
            "free": int(m.group(3)),
            "largest": int(m.group(4)),
            "min_free": int(m.group(5)),
            "peak": int(m.group(6)),
        }
    return heap


def error_lines(lines: list[str]) -> list[str]:
    """筛出真正的运行期错误。

    忽略口径（与 voice_test.py/tts_stream_test.py 一致并扩展）：网络/WS/连接重连类既有噪声
    ——按 TAG 白名单（NET/WS/wifi/websocket_client/esp-tls/transport_* 等）或行内连接失败
    片段（Failed to open a new connection / Error connecting to host / esp_transport_connect /
    WS 错误 / 连接断开 / 后重连）忽略。panic / assert failed / Guru / 其它 ERROR 仍返回，
    由 F 场景判 FAIL（不掩盖真实错误）。
    """
    out: list[str] = []
    for line in lines:
        if not RE_ERROR.search(line):
            continue
        m = RE_TAG.search(line)
        if m and m.group(1) in IGNORED_ERROR_TAGS:
            continue
        if IGNORED_ERROR_LINE.search(line):
            continue
        out.append(line.strip())
    return out


# ---------------------------------------------------------------- FFT / 包络 / 断言工具

def _hann_kernel(delta: float) -> float:
    """汉宁窗的归一化幅度响应 W(δ) = sinc(δ)/(1-δ²)，δ 为相对 bin 的偏移（单位 bin）。

    W(0)=1；δ=0.5 时 ≈0.8488（即汉宁窗的 scalloping loss ≈ -1.42dB）。
    """
    if abs(delta) < 1e-9:
        return 1.0
    return abs(math.sin(math.pi * delta) / (math.pi * delta)) / (1.0 - delta * delta)


def tone_amplitude(samples: list[int], freq: int, start: int = 0, n: int | None = None,
                   rate: int = RATE) -> float:
    """单频幅度估计：汉宁窗 + rFFT，幅度 ≈ 2|X[k]|/sum(win)。

    频率分辨率 = rate/N；取最接近目标频率的 bin（±1 bin 内取最大）以避开频谱泄漏误差。
    目标频率一般不落在 bin 中心（如 1000Hz 在 8192 点时偏 0.33 bin、1760Hz 偏 0.37 bin），
    直接用峰值会低估 7%~9%（汉宁窗 scalloping loss），会吃掉 ±8% 的验收容差；因此再除以
    解析的窗响应 W(δ) 做幅度校正（δ = 目标频率与命中 bin 的偏移），纯 rFFT 实现、不引 scipy。
    """
    np = _np()
    seg = np.asarray(samples[start:] if n is None else samples[start:start + n],
                     dtype=np.float64)
    if len(seg) < 16:
        return 0.0
    win = np.hanning(len(seg))
    spec = np.fft.rfft(seg * win)
    mag = 2.0 * np.abs(spec) / float(np.sum(win))
    bin_hz = rate / float(len(seg))
    k0 = int(round(freq / bin_hz))
    lo = max(0, k0 - 1)
    hi = min(len(mag) - 1, k0 + 1)
    if hi < lo:
        return 0.0
    k = lo + int(np.argmax(mag[lo:hi + 1]))
    delta = min(abs(freq - k * bin_hz) / bin_hz, 0.5)  # 命中 bin 不会偏离最近 bin 超过 0.5
    return float(mag[k] / _hann_kernel(delta))


def peak_abs(samples: list[int], start: int = 0, n: int | None = None) -> int:
    seg = samples[start:] if n is None else samples[start:start + n]
    return max((abs(v) for v in seg), default=0)


def block_peaks(samples: list[int], block: int, start: int = 0,
                n: int | None = None) -> list[int]:
    """按 block 帧分块的峰值包络（块内 max|x|）。"""
    if n is None or start + n > len(samples):
        n = len(samples) - start
    out: list[int] = []
    pos = start
    while pos + block <= start + n:
        out.append(max(abs(v) for v in samples[pos:pos + block]))
        pos += block
    return out


def max_abs_step(samples: list[int], start: int = 0, n: int | None = None) -> int:
    """相邻样本最大跳变（pop 检测：正常正弦的 |Δ| 有理论上限）。"""
    if n is None or start + n > len(samples):
        n = len(samples) - start
    seg = samples[start:start + n]
    return max((abs(seg[i] - seg[i - 1]) for i in range(1, len(seg))), default=0)


def slope_limit(pairs: list[tuple[int, int]], fade_frames: int = FADE_IN_FRAMES,
                rate: int = RATE, margin: float = SLOPE_MARGIN) -> float:
    """理论相邻样本最大斜率上限。

    pairs: [(幅度, 频率)]；正弦最大斜率 = 2π·f/rate·amp，多路按线性叠加；
    再加淡入包络每样本步进 amp/fade_frames 与量化余量，最后 ×margin。
    例：amp=8000@1kHz → 1047×1.5 + 16.7 + 8 ≈ 1595（契约取 1600）。
    """
    base = sum(2.0 * math.pi * freq / float(rate) * amp for amp, freq in pairs)
    step = max((amp / float(fade_frames) for amp, _ in pairs), default=0.0)
    return margin * base + step + SLOPE_QUANT


def find_signal_bounds(samples: list[int], thr: int = 1) -> tuple[int | None, int | None]:
    """找首/末个 |x|>thr 的帧号（抓取起点前可能有静音块）。"""
    first = None
    for i, v in enumerate(samples):
        if abs(v) > thr:
            first = i
            break
    if first is None:
        return None, None
    last = None
    for i in range(len(samples) - 1, first - 1, -1):
        if abs(samples[i]) > thr:
            last = i
            break
    return first, last


def find_tone_onset(samples: list[int], freq: int, threshold: float,
                   start: int = 0, block: int = 480, step: int = 120,
                   rate: int = RATE) -> int | None:
    """用某频率的能量找音调起点（onset）。

    以 block=10ms 为窗、step=5ms 滑动，返回首个该频率幅度 > threshold 的窗起点；
    误差上界约一个窗（10ms）。E 场景用它定位 PROMPT 叠加点——cap 武装与 tone 命令之间
    有 ~50~100ms 往返，抓取窗开头仍是"MUSIC 全量"段，稳态窗不能从 0 起。
    """
    pos = start
    while pos + block <= len(samples):
        if tone_amplitude(samples, freq, pos, block, rate) > threshold:
            return pos
        pos += step
    return None


def ratio_ok(actual: float, expected: float, tol: float) -> tuple[bool, str]:
    if expected <= 0:
        return False, f"期望值 {expected:.1f} ≤ 0，无法比较"
    err = abs(actual - expected) / expected
    detail = f"实测 {actual:.1f} / 期望 {expected:.1f}（偏差 {err * 100:.2f}% ≤ {tol * 100:.1f}%）"
    return err <= tol, detail


def gain_ok(gain: int, expected: int, tol: int = GAIN_TOL) -> tuple[bool, str]:
    ok = abs(gain - expected) <= tol
    return ok, f"gain_q8={gain}（期望 {expected}±{tol}）"


def fade_in_ok(samples: list[int], start: int, steady_peak: float) -> tuple[bool, str]:
    """10ms 淡入：首帧很小 + 每 1ms 块峰值单调不降 + 末块接近稳态。"""
    seg = samples[start:start + FADE_IN_FRAMES]
    if len(seg) < FADE_IN_FRAMES:
        return False, f"抓取数据不足 10ms 淡入窗（只有 {len(seg)} 帧）"
    first_abs = abs(seg[0])
    small = max(4.0, steady_peak * 0.02)
    if first_abs > small:
        return False, f"首帧 |x|={first_abs} 不是从 0 起（预期 ≤{small:.0f}）"
    peaks = block_peaks(seg, 48)  # 每 1ms 一块，共 10 块
    if len(peaks) < 10:
        return False, "10ms 淡入窗不足 10 块"
    for k in range(1, len(peaks)):
        if peaks[k] < peaks[k - 1] - max(4.0, peaks[k - 1] * 0.05):
            return False, f"第 {k}ms 包络回落（{peaks[k - 1]}→{peaks[k]}），不是线性升"
    if peaks[0] > steady_peak * 0.15:
        return False, f"首 1ms 峰值 {peaks[0]} 已超过稳态 15%（没有淡入）"
    if peaks[-1] < steady_peak * 0.8:
        return False, f"第 10ms 峰值 {peaks[-1]} 未接近稳态 {steady_peak:.0f}"
    return True, (f"首帧 {first_abs}，1ms 包络 {peaks[0]}→{peaks[-1]}"
                  f"（稳态 {steady_peak:.0f}）")


def fade_out_ok(samples: list[int], end: int, steady_peak: float) -> tuple[bool, str]:
    """100ms 淡出：每 10ms 块峰值单调不升 + 最后一块接近 0。"""
    start = end - FADE_OUT_FRAMES
    if start < 0 or end > len(samples):
        return False, f"淡出窗越界（start={start} end={end} len={len(samples)}）"
    seg = samples[start:end]
    peaks = block_peaks(seg, 480)  # 每 10ms 一块
    if len(peaks) < 10:
        return False, "100ms 淡出窗不足 10 块"
    for k in range(1, len(peaks)):
        if peaks[k] > peaks[k - 1] + max(4.0, peaks[k - 1] * 0.1):
            return False, f"第 {k * 10}ms 包络回升（{peaks[k - 1]}→{peaks[k]}），不是线性降"
    if peaks[-1] > steady_peak * 0.15:
        return False, f"最后 10ms 峰值 {peaks[-1]} 仍占稳态 {(peaks[-1] / steady_peak) * 100:.0f}%（未降到 0）"
    # 契约阈值：末 1ms 包络 < 5% 稳态（固件淡出步进从 65536/4800 取整改为向上取整后应≈0；
    # 旧固件取整误差 13 会让尾值停在 ~4.8%，正好落在这一档边界内，真机复测看修后是否≈0）
    last_1ms = peak_abs(seg[-48:])
    if last_1ms > steady_peak * 0.05:
        return False, (f"末 1ms 包络 {last_1ms} 仍占稳态 "
                       f"{(last_1ms / steady_peak) * 100:.1f}%（应 <5%）")
    tail = abs(seg[-1])
    return True, (f"10ms 块峰值 {peaks[0]}→{peaks[-1]}（稳态 {steady_peak:.0f}），"
                  f"末 1ms {last_1ms}，末样本 {tail}")


def limiter_ok(samples: list[int], ceiling: int = CEILING) -> tuple[bool, str]:
    """软限幅：最大值不超 ceiling+量化余量，且不得有硬削波样本。"""
    peak = peak_abs(samples)
    hard = [v for v in samples if abs(v) >= HARD_CLIP]
    if hard:
        return False, f"出现 {len(hard)} 个 |x|≥{HARD_CLIP} 的硬削波样本（峰值 {peak}）"
    limit = ceiling + CEILING_QUANT
    if peak > limit:
        return False, f"输出峰值 {peak} 超过 -1dBFS 上限 {limit}"
    return True, f"峰值 {peak} ≤ {limit}，|x|≥{HARD_CLIP} 样本 0"


def heap_range_ok(values: list[int], tol: int) -> tuple[bool, str]:
    """堆稳定性：峰谷差 ≤ tol（噪声内）。"""
    if not values:
        return False, "没有有效的 heap 读数"
    lo, hi = min(values), max(values)
    detail = f"峰谷差 {hi - lo} B（min {lo} / max {hi}，容差 {tol} B）"
    return (hi - lo) <= tol, detail


# ---------------------------------------------------------------- 串口设备
class Device:
    """最小串口封装：读行成缓冲、按前缀等回显、可同时写完整日志。"""

    def __init__(self, port: str, log_path: str = ""):
        import serial  # 延迟导入：--dry-run / --self-test 不依赖 pyserial

        # timeout 取 5ms 而不是既有脚本的 50ms：pump(duration) 的精度直接决定场景 E 的
        # 「300ms 后叠加」能否落在 341ms 抓取窗内。pyserial 的 read(n) 会等满整个 timeout
        # 才返回（即使已有数据），50ms 会在每条命令/pump 上累积出 ~100ms 的时间误差。
        self.ser = serial.Serial(port, BAUDRATE, timeout=0.005)
        self.buf = ""
        self.lines: list[tuple[float, str]] = []
        self._log = open(log_path, "w", encoding="utf-8") if log_path else None

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass
        if self._log is not None:
            try:
                self._log.close()
            except Exception:
                pass
            self._log = None

    def reset(self) -> None:
        # 与 esptool/idf.py 相同的最小复位序列：RTS→EN，DTR 保持高
        self.ser.dtr = False
        self.ser.rts = True
        time.sleep(0.15)
        self.ser.rts = False

    def _feed(self, text: str) -> None:
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = ANSI_RE.sub("", line).rstrip("\r")
            self.lines.append((time.time(), line))
            if self._log is not None:
                self._log.write(line + "\n")

    def _read_chunk(self) -> bytes:
        """读一次串口：先阻塞等 1 字节（≤5ms），再把已到达的整批取走。

        不能直接 `read(8192)`：pyserial 会等满整个 timeout 才返回，导致 pump/命令回显
        每条都多出几十毫秒，场景 E 的叠加点会漂出抓取窗。
        """
        data = self.ser.read(1)
        if not data:
            return b""
        waiting = self.ser.in_waiting
        if waiting:
            data += self.ser.read(waiting)
        return data

    def pump(self, duration: float) -> None:
        end = time.time() + duration
        while time.time() < end:
            data = self._read_chunk()
            if data:
                self._feed(data.decode("utf-8", "replace"))
            else:
                time.sleep(0.002)

    def mark(self) -> int:
        return len(self.lines)

    def lines_since(self, mark: int) -> list[tuple[float, str]]:
        return self.lines[mark:]

    def texts(self, mark: int = 0) -> list[str]:
        return [line for _, line in self.lines[mark:]]

    def log_note(self, text: str) -> None:
        """把一行非设备日志（断言明细等）写到 --log 文件（与设备日志同一个文件）。"""
        if self._log is not None:
            self._log.write(text + "\n")
            self._log.flush()

    def send(self, cmd: str) -> None:
        # 契约：一行文本 + \r\n
        self.ser.write((cmd + "\r\n").encode())
        self.ser.flush()
        if self._log is not None:
            self._log.write(">> " + cmd + "\n")
            self._log.flush()

    def console(self, cmd: str, patterns: list[re.Pattern],
                timeout: float = CONSOLE_TIMEOUT) -> list[str]:
        """发一条 console 命令，等所有 patterns 都出现（或超时），返回本次输出行。

        容忍日志乱序/互冲：只在「发出命令之后」的行里按前缀找，不依赖命令回显。
        """
        mark = self.mark()
        self.send(cmd)
        end = time.time() + timeout
        seen = [False] * len(patterns)
        while time.time() < end:
            texts = self.texts(mark)
            for i, pat in enumerate(patterns):
                if not seen[i] and any(pat.search(t) for t in texts):
                    seen[i] = True
            if all(seen):
                break
            self.pump(0.01)
        return self.texts(mark)


class EnvError(RuntimeError):
    """环境性失败：串口超时 / 回显被日志冲掉 / 探针输出不完整 → 记 WARN 并重试一次。"""


class ScenarioFail(RuntimeError):
    """确定性断言失败：已记录 FAIL 行，本场景剩余步骤中止（不重试）。"""


# ---------------------------------------------------------------- 探针封装
class Mixer:
    """`player mix ...` 探针的薄封装；缺回显时抛 EnvError（由场景重试口径处理）。"""

    def __init__(self, dev: Device):
        self.dev = dev

    @staticmethod
    def _match(lines: list[str], pattern: re.Pattern) -> re.Match | None:
        for line in lines:
            m = pattern.search(line)
            if m:
                return m
        return None

    def stop(self) -> bool:
        lines = self.dev.console("player mix stop", [RE_MIXSTOP])
        m = self._match(lines, RE_MIXSTOP)
        if m is None:
            raise EnvError("`player mix stop` 没有 MIXSTOP 回显（探针缺失或串口被日志冲掉）")
        if int(m.group(1)) != 1:
            raise EnvError("MIXSTOP ok=0（混音泵未正常退出）")
        return True

    def reset(self) -> bool:
        lines = self.dev.console("player mix reset", [RE_MIXRESET])
        m = self._match(lines, RE_MIXRESET)
        if m is None:
            raise EnvError("`player mix reset` 没有 MIXRESET 回显")
        if int(m.group(1)) != 1:
            raise EnvError("MIXRESET ok=0")
        return True

    def vol(self, value: int) -> dict:
        if not 0 <= value <= 100:
            raise ValueError(f"音量 {value} 超出 0~100")
        lines = self.dev.console(f"player mix vol {value}", [RE_MIXVOL])
        rec = parse_mixvol(lines)
        if rec is None:
            raise EnvError(f"`player mix vol {value}` 没有 MIXVOL 回显")
        return rec

    def tone(self, ch: str, freq: int, ms: int, amp: int = TONE_AMP) -> dict:
        if ch not in ("TTS", "PROMPT", "MUSIC"):
            raise ValueError(f"未知通道 {ch}")
        if not 100 <= freq <= 4000:
            raise ValueError(f"频率 {freq} 超出探针范围 100~4000Hz")
        if not 50 <= ms <= 10000:
            raise ValueError(f"时长 {ms} 超出探针范围 50~10000ms")
        if not 100 <= amp <= 20000:
            raise ValueError(f"幅度 {amp} 超出探针范围 100~20000")
        lines = self.dev.console(f"player mix tone {ch} {freq} {ms} {amp}", [RE_MIXTONE])
        rec = parse_mixtone(lines)
        if rec is None:
            raise EnvError(f"`player mix tone {ch} ...` 没有 MIXTONE 回显")
        return rec

    def cap(self, frames: int) -> int:
        if not 1 <= frames <= CAP_MAX:
            raise ValueError(f"抓取帧数 {frames} 超出 1~{CAP_MAX}")
        lines = self.dev.console(f"player mix cap {frames}", [RE_MIXCAP_ARMED])
        armed = parse_cap_armed(lines)
        if armed is None:
            raise EnvError("`player mix cap` 没有 MIXCAP armed 回显")
        return armed

    def stat(self) -> dict:
        """读 4 行状态（MIXSTAT + 3 条 MIXCH）；缺任何一条都算环境失败。"""
        lines = self.dev.console(
            "player mix stat",
            [RE_MIXSTAT, RE_MIXCH_TTS, RE_MIXCH_PROMPT, RE_MIXCH_MUSIC],
        )
        stat = parse_mixstat(lines)
        channels = parse_mixchannels(lines)
        if stat is None or len(channels) < 3:
            raise EnvError(
                "`player mix stat` 输出不完整（需要 MIXSTAT + TTS/PROMPT/MUSIC 三条 MIXCH）"
            )
        if stat["cap_len"] is None or stat["cap_pos"] is None:
            raise EnvError(
                "MIXSTAT 缺 cap_len/cap_pos：需要带抓取状态的最新 T3.3-A 固件"
                "（dump 现在按实际抓取数 cap_pos 校验，没有它无法安全取数）"
            )
        return {"stat": stat, "channels": channels, "raw": lines}

    def dump(self, off: int, count: int) -> dict:
        """取回抓取数据（单次 ≤512）；等到 MIXCAPEND（或超时）再解析。"""
        if not 1 <= count <= DUMP_CHUNK:
            raise ValueError(f"dump 条数 {count} 超出 1~{DUMP_CHUNK}")
        mark = self.dev.mark()
        self.dev.send(f"player mix dump {off} {count}")
        end = time.time() + DUMP_TIMEOUT
        while time.time() < end:
            texts = self.dev.texts(mark)
            if any(RE_MIXCAP_END.search(t) for t in texts):
                break
            self.dev.pump(0.01)
        if any(RE_MIXCAP_ERR.search(t) for t in texts):
            raise EnvError(
                f"抓取不完整：`player mix dump {off} {count}` 返回 MIXCAP err"
                "（off+count 超过实际抓到帧数 cap_pos）"
            )
        rec = parse_dump(self.dev.texts(mark))
        if rec is None:
            raise EnvError(f"`player mix dump {off} {count}` 没有有效数据（head/hex 缺失）")
        if not rec["end"]:
            raise EnvError(f"`player mix dump {off} {count}` 缺 MIXCAPEND（回显被截断）")
        if rec["off"] != off:
            raise EnvError(f"dump off 不匹配：请求 {off}，回显 {rec['off']}")
        if len(rec["samples"]) != count:
            raise EnvError(
                f"dump 样本数不匹配：请求 {count}，解析到 {len(rec['samples'])}"
                "（hex 行格式异常？固件应为每行 16 个连写的 %04x）"
            )
        return rec

    def heap(self) -> dict:
        lines = self.dev.console("sys heap", [RE_HEAP_END])
        heap = parse_heap(lines)
        if not heap.get("internal") or not heap.get("psram"):
            raise EnvError("`sys heap` 输出不完整（需要内部 RAM 与 PSRAM 两行）")
        return heap


# ---------------------------------------------------------------- 结果与场景重试

class Result:
    """PASS/FAIL 汇总：每行一条断言（带 detail），最后给机器可读汇总与退出码。"""

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        ok = bool(ok)
        self.rows.append((ok, label, detail))
        suffix = f"  {detail}" if detail else ""
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{suffix}")
        return ok

    def warn(self, label: str) -> None:
        self.rows.append((True, f"WARN {label}", ""))
        print(f"  WARN  {label}")

    def extend(self, other: "Result") -> None:
        self.rows.extend(other.rows)

    def summary(self, name: str = "MIXER_TEST") -> bool:
        failed = [row for row in self.rows if not row[0]]
        total = len(self.rows)
        passed = total - len(failed)
        print("\n===== 结果汇总 =====")
        for ok, label, _detail in self.rows:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        print(f"\nPASS {passed}/{total}")
        if failed:
            print("失败明细：")
            for _, label, detail in failed:
                print(f"  - {label}" + (f"  {detail}" if detail else ""))
        print(f"{name}: {'PASS' if not failed else 'FAIL'}")
        return not failed

    def write_log(self, dev: "Device") -> None:
        """把每条断言（含 detail）与汇总写到 --log 文件，方便现场事后定位。

        --log 里原本只有设备日志；断言明细在 stdout 一闪而过，真机跑完只剩文件，
        所以这里用 `[ASSERT]` 前缀追加到同一个日志文件末尾（设备日志与断言同文件，便于对照）。
        """
        if dev is None or getattr(dev, "_log", None) is None:
            return
        failed = [row for row in self.rows if not row[0]]
        dev.log_note("[ASSERT] ===== 断言明细 =====")
        for ok, label, detail in self.rows:
            line = f"[{'PASS' if ok else 'FAIL'}] {label}"
            if detail:
                line += f"  {detail}"
            dev.log_note("[ASSERT] " + line)
        dev.log_note(f"[ASSERT] PASS {len(self.rows) - len(failed)}/{len(self.rows)}")
        for _, label, detail in failed:
            dev.log_note("[ASSERT] FAIL " + label + (f"  {detail}" if detail else ""))


def detect_port(explicit: str | None) -> str:
    if explicit:
        return explicit
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if len(ports) == 1:
        return ports[0]
    if not ports:
        raise SystemExit("未找到 /dev/cu.usbmodem*：确认开发板已插上，或用 --port 指定")
    raise SystemExit(f"找到多个串口，请用 --port 指定：{', '.join(ports)}")


def safe_stop(dev: Device) -> None:
    """清理：停探针 + 清统计（失败不抛，finally 里用）。"""
    try:
        dev.console("player mix stop", [RE_MIXSTOP], timeout=1.0)
        dev.console("player mix reset", [RE_MIXRESET], timeout=1.0)
    except Exception:
        pass


def slate(mix: Mixer, wait: float = 0.1) -> None:
    """每个场景开始前的统一清理：stop → reset → 等 100ms（契约要求）。"""
    mix.stop()
    mix.reset()
    if wait > 0:
        mix.dev.pump(wait)


def expect_vol(res: Result, label: str, mix: Mixer, value: int) -> None:
    """场景前置音量：设备运行时音量可能是任意值（默认/用户设置），幅度断言前必须定到 100。

    `player mix vol` 不落盘；脚本结束会恢复进入时的音量（见 run()）。
    """
    rec = mix.vol(value)
    ok = rec["err"] == "ESP_OK" and rec["vol"] == value
    res.check(ok, f"{label}：前置 vol {value} 生效（幅度断言基准）",
              f"实测 vol={rec['vol']} err={rec['err']}")
    if not ok:
        raise ScenarioFail(f"前置 vol {value} 未生效（vol={rec['vol']} err={rec['err']}）")


def expect_tone(res: Result, label: str, mix: Mixer, ch: str, freq: int, ms: int,
                amp: int = TONE_AMP) -> dict:
    rec = mix.tone(ch, freq, ms, amp)
    ok = rec["err"] == "ESP_OK"
    res.check(ok, f"{label}：tone {ch} {freq}Hz {ms}ms amp={amp} → err=ESP_OK",
              f"实测 err={rec['err']}")
    if not ok:
        raise ScenarioFail(f"tone {ch} 返回 {rec['err']}（播放任务在忙或探针参数被拒）")
    return rec


def dump_range(mix: Mixer, off: int, n: int, chunk: int = DUMP_CHUNK) -> list[int]:
    """按 512 分块取回 [off, off+n) 的样本；数据不足视为环境失败。"""
    out: list[int] = []
    while len(out) < n:
        take = min(chunk, n - len(out))
        rec = mix.dump(off + len(out), take)
        got = rec["samples"]
        if not got:
            raise EnvError(f"抓取不足：off={off} 请求 {n} 帧，在 {off + len(out)} 处断流")
        out.extend(got[:take])
        if len(got) < take:
            raise EnvError(
                f"抓取不足：off={off} 请求 {n} 帧，只拿到 {off + len(out)} 帧（播放未启动或抓取提前停）"
            )
    return out


def capture_pos(res: Result, label: str, state: dict, expect_full: bool = True) -> int:
    """取"实际抓到的帧数" cap_pos（固件 MIXSTAT 末尾 cap_len/cap_pos）。

    A/B/C/E 的音调时长都 > 341ms 抓取窗，正常应 cap_pos == cap_len（取满）；D 的 300ms
    音调会提前结束，cap_pos≈14400，只允许 dump [0, cap_pos)（固件按实际抓取数校验）。
    """
    cap_len = state["stat"]["cap_len"]
    cap_pos = state["stat"]["cap_pos"]
    if expect_full:
        res.check(cap_pos == cap_len and cap_len > 0,
                  f"{label}：抓取取满（cap_pos==cap_len={cap_len}）",
                  f"cap_len={cap_len} cap_pos={cap_pos}")
    if cap_pos <= 0:
        raise EnvError(f"{label}：cap_pos={cap_pos}，播放未启动或抓取未武装")
    return cap_pos


def dump_window(mix: Mixer, cap_pos: int, off: int, n: int, chunk: int,
                what: str) -> list[int]:
    """按 cap_pos 安全取 [off, off+n)：越界即环境失败（固件会回 MIXCAP err）。"""
    if off + n > cap_pos:
        raise EnvError(f"{what}：抓取窗口 [{off},{off + n}) 超过实际抓取数 cap_pos={cap_pos}")
    return dump_range(mix, off, n, chunk)


def run_scenario(title: str, fn, result: Result, dev: Device, args: argparse.Namespace) -> None:
    """跑一个场景；环境性失败（串口/回显类）WARN + 整场景重试一次，阈值不变。"""
    print(f"\n===== 场景 {title} =====")
    for attempt in (0, 1):
        local = Result()
        try:
            fn(local, dev, args)
        except EnvError as exc:
            if attempt == 0:
                result.warn(f"场景 {title}：环境性失败（{exc}），按口径重试一次")
                safe_stop(dev)
                dev.pump(0.2)
                continue
            local.check(False, f"场景 {title}：环境性失败（{exc}；已重试）")
        except ScenarioFail as exc:
            print(f"  [ABORT] 场景 {title}：{exc}")
        result.extend(local)
        return


# ---------------------------------------------------------------- 场景 A：三通道混音 + duck

def scenario_a(res: Result, dev: Device, args: argparse.Namespace) -> None:
    label = "A 三通道混音"
    mix = Mixer(dev)
    print("  指令：tone TTS 1000 / PROMPT 440 / MUSIC 1760（各 2500ms amp 8000）→ "
          "等 0.35s 让通道淡入+duck 斜坡走完 → 再 arm cap → 稳态 FFT")
    slate(mix, 0.2)
    expect_vol(res, label, mix, 100)
    # 真机实测：三条 tone 命令各要 ~100ms 往返，PROMPT/MUSIC 分别在 ~100/~200ms 才起播，
    # 而 duck 斜坡从"通道真正 active"才开始（未 active 的通道不更新 gain）——若在 tone 之前
    # arm cap，抓取窗前段是"部分未 duck"的混合区（440/1760 幅度偏高）。故先起三路、等斜坡走完
    # 再武装，抓取窗 0..341ms 全程都是稳态。
    for ch, freq in (("TTS", 1000), ("PROMPT", 440), ("MUSIC", 1760)):
        expect_tone(res, label, mix, ch, freq, 2500, TONE_AMP)
    dev.pump(0.35)
    armed = mix.cap(args.cap_frames)
    res.check(armed == args.cap_frames,
              f"{label}：duck 斜坡走完后再 arm cap {args.cap_frames} 帧", f"armed={armed}")
    dev.pump(0.40)  # 抓满 341ms；音调还有 ~1.5s，stat 时三路仍活跃
    state = mix.stat()
    st, chs = state["stat"], state["channels"]

    res.check(st["clip"] == 0, f"{label}：clip=0（无硬削波）", f"clip={st['clip']}")
    # block_max_us 是"重置以来单块最大耗时"，偶发一次抢占就会超 10ms；它是性能观测项
    # 而非 T3.3 功能断言，故超标只记 WARN（真回归看 F 场景与体感/日志），不误判 PASS/FAIL。
    if st["block_max_us"] <= MAX_BLOCK_US:
        res.check(True, f"{label}：block_max_us ≤ {MAX_BLOCK_US}（10ms 预算）",
                  f"block_max_us={st['block_max_us']}")
    else:
        res.warn(f"{label}：block_max_us={st['block_max_us']} 超 {MAX_BLOCK_US}"
                 "（偶发抢占可忽略，持续超标需查混音耗时）")
    res.check(all(chs[ch]["active"] == 1 for ch in ("TTS", "PROMPT", "MUSIC")),
              f"{label}：三通道 active=1",
              f"TTS={chs['TTS']['active']} PROMPT={chs['PROMPT']['active']} "
              f"MUSIC={chs['MUSIC']['active']}")
    ok, detail = gain_ok(chs["TTS"]["gain_q8"], 256)
    res.check(ok, f"{label}：TTS gain_q8≈256（0dB）", detail)
    ok, detail = gain_ok(chs["PROMPT"]["gain_q8"], 128)
    res.check(ok, f"{label}：PROMPT gain_q8≈128（TTS 活跃 → duck -6dB）", detail)
    ok, detail = gain_ok(chs["MUSIC"]["gain_q8"], 64)
    res.check(ok, f"{label}：MUSIC gain_q8≈64（TTS 活跃 → duck -12dB）", detail)

    cap_pos = capture_pos(res, label, state)
    # 抓取窗从 arm 之后开始，前段已是稳态（duck 已完成），故 FFT 窗从 0 起即可；
    # --steady-start 留给 B（音量 20ms 斜坡）等"抓取窗起点含斜坡"的场景。
    samples = dump_window(mix, cap_pos, 0, args.fft_n, args.dump_chunk, label)
    res.check(len(samples) == args.fft_n, f"{label}：稳态窗取回 {args.fft_n} 帧",
              f"实际 {len(samples)} 帧")
    amps = {freq: tone_amplitude(samples, freq) for freq in (1000, 440, 1760)}
    for freq, nominal in ((1000, 8000.0), (440, 4000.0), (1760, 2000.0)):
        ok, detail = ratio_ok(amps[freq], nominal, DUCK_TOL)
        res.check(ok, f"{label}：{freq}Hz 峰 ≈{nominal:.0f}（±8%）",
                  f"{freq}Hz {detail}")
    ok, detail = ratio_ok(amps[440], amps[1000] * 0.5, DUCK_TOL)
    res.check(ok, f"{label}：幅度比 PROMPT/TTS ≈0.5（±8%）", detail)
    ok, detail = ratio_ok(amps[1760], amps[1000] * 0.25, DUCK_TOL)
    res.check(ok, f"{label}：幅度比 MUSIC/TTS ≈0.25（±8%）", detail)

    captured_peak = peak_abs(samples)
    ok, detail = ratio_ok(captured_peak, st["mix_peak"], PEAK_TOL)
    res.check(ok, f"{label}：mix_peak 与抓取峰值一致（±2%）", detail)


# ---------------------------------------------------------------- 场景 B：音量线性

def scenario_b(res: Result, dev: Device, args: argparse.Namespace) -> None:
    label = "B 音量线性"
    mix = Mixer(dev)
    print("  指令：vol 100/50/0 → cap 16384 → tone MUSIC 1000Hz 1500ms amp 8000，"
          "每档取稳态幅度")
    amps: dict[int, float] = {}
    for target in (100, 50, 0):
        print(f"  -- vol {target} --")
        slate(mix, 0.2)
        rec = mix.vol(target)
        ok = rec["err"] == "ESP_OK" and rec["vol"] == target
        res.check(ok, f"{label}：vol {target} 生效 → MIXVOL vol={target} err=ESP_OK",
                  f"实测 vol={rec['vol']} err={rec['err']}")
        if not ok:
            raise ScenarioFail(f"vol {target} 未生效（vol={rec['vol']} err={rec['err']}）")
        armed = mix.cap(args.cap_frames)
        res.check(armed == args.cap_frames, f"{label}：vol {target} 档 cap 武装 {args.cap_frames} 帧",
                  f"armed={armed}")
        expect_tone(res, label, mix, "MUSIC", 1000, 1500, TONE_AMP)
        dev.pump(0.45)
        state = mix.stat()
        res.check(state["channels"]["MUSIC"]["active"] == 1,
                  f"{label}：vol {target} 档 MUSIC active=1")
        cap_pos = capture_pos(res, label, state)

        if target == 0:
            # 音量 20ms 斜坡只在混音泵渲染时推进：从上一档（50）切到 0 时，抓取开头
            # ~20ms 仍有残余音量，之后才是全零；因此查稳态窗（100ms 后）+ mix_peak。
            samples = dump_window(mix, cap_pos, args.steady_start, args.fft_n,
                                  args.dump_chunk, label)
            max_abs = peak_abs(samples)
            res.check(max_abs == 0, f"{label}：vol 0 档稳态输出全零",
                      f"最大 |x|={max_abs}（稳态窗 {args.fft_n} 帧全查）")
            res.check(state["stat"]["mix_peak"] == 0, f"{label}：vol 0 档 mix_peak=0",
                      f"mix_peak={state['stat']['mix_peak']}")
            amps[target] = 0.0
        else:
            samples = dump_window(mix, cap_pos, args.steady_start, args.fft_n,
                                  args.dump_chunk, label)
            amp = tone_amplitude(samples, 1000)
            amps[target] = amp
            ok, detail = ratio_ok(amp, TONE_AMP * target / 100.0, VOL_TOL)
            res.check(ok, f"{label}：vol {target} 档 1000Hz 峰 ≈{TONE_AMP * target / 100:.0f}（±2%）",
                      detail)
            ok, detail = ratio_ok(state["stat"]["mix_peak"], amp, 0.05)
            res.check(ok, f"{label}：vol {target} 档 mix_peak 与 FFT 幅度一致（±5%）", detail)

    ok, detail = ratio_ok(amps[50], amps[100] * 0.5, VOL_TOL)
    res.check(ok, f"{label}：vol 50/100 幅度比 ≈0.5（±2%）", detail)
    # vol 0 是绝对零（比值无意义）：0 档必须严格全零、100 档必须有信号
    res.check(amps[0] == 0 and amps[100] > 0,
              f"{label}：vol 0 档幅度=0、vol 100 档有信号",
              f"vol0={amps[0]:.1f} vol100={amps[100]:.1f}")


# ---------------------------------------------------------------- 场景 C：软限幅

def scenario_c(res: Result, dev: Device, args: argparse.Namespace) -> None:
    label = "C 软限幅"
    mix = Mixer(dev)
    print("  指令：stop/reset → cap 16384 → tone TTS 440 / PROMPT 1000 / MUSIC 1760"
          "（各 1500ms amp 20000）→ 稳态窗峰值/限幅计数")
    slate(mix, 0.2)
    expect_vol(res, label, mix, 100)
    armed = mix.cap(args.cap_frames)
    res.check(armed == args.cap_frames, f"{label}：cap 武装 {args.cap_frames} 帧",
              f"armed={armed}")
    for ch, freq in (("TTS", 440), ("PROMPT", 1000), ("MUSIC", 1760)):
        expect_tone(res, label, mix, ch, freq, 1500, 20000)
    dev.pump(0.45)
    state = mix.stat()
    st = state["stat"]

    cap_pos = capture_pos(res, label, state)
    # 抓取窗从 arm 之后开始，前段已是稳态（duck 已完成），故 FFT 窗从 0 起即可；
    # --steady-start 留给 B（音量 20ms 斜坡）等"抓取窗起点含斜坡"的场景。
    samples = dump_window(mix, cap_pos, 0, args.fft_n, args.dump_chunk, label)
    ok, detail = limiter_ok(samples)
    res.check(ok, f"{label}：稳态窗 |x| ≤ {CEILING}（-1dBFS，+{CEILING_QUANT} 余量），"
                  f"无 |x|≥{HARD_CLIP}", detail)
    res.check(st["limiter"] > 0, f"{label}：limiter > 0（软限幅确实触发）",
              f"limiter={st['limiter']}")
    res.check(st["limiter_peak"] > CEILING,
              f"{label}：limiter_peak > {CEILING}（限幅前峰值超过 ceiling）",
              f"limiter_peak={st['limiter_peak']}")
    res.check(st["clip"] == 0, f"{label}：clip=0（没有退化成硬削波）",
              f"clip={st['clip']}")
    captured_peak = peak_abs(samples)
    ok, detail = ratio_ok(captured_peak, st["mix_peak"], PEAK_TOL)
    res.check(ok, f"{label}：mix_peak 与抓取峰值一致（±2%）", detail)


# ---------------------------------------------------------------- 场景 D：淡入淡出

def scenario_d(res: Result, dev: Device, args: argparse.Namespace) -> None:
    label = "D 淡入淡出"
    mix = Mixer(dev)
    print("  指令：cap 16384（先武装）→ tone TTS 1000Hz 300ms amp 8000 → 全段包络/阶跃检查")
    slate(mix, 0.2)
    expect_vol(res, label, mix, 100)
    armed = mix.cap(args.cap_frames)
    res.check(armed == args.cap_frames, f"{label}：cap 在 tone 之前武装 {args.cap_frames} 帧",
              f"armed={armed}")
    expect_tone(res, label, mix, "TTS", 1000, 300, TONE_AMP)
    dev.pump(0.45)
    state = mix.stat()
    st = state["stat"]
    res.check(st["clip"] == 0, f"{label}：clip=0", f"clip={st['clip']}")

    # 300ms tone 只渲染 30 个 10ms 块 = 14400 帧后泵退出，cap_pos 会比武装的 16384 小；
    # 固件 dump 按 cap_pos 校验（越界回 MIXCAP err），所以只取 [0, cap_pos)。
    tone_len = 300 * RATE // 1000
    res.check(st["cap_len"] == args.cap_frames,
              f"{label}：cap 武装 {args.cap_frames} 帧（MIXSTAT cap_len）",
              f"cap_len={st['cap_len']}")
    ok = abs(st["cap_pos"] - tone_len) <= 480
    res.check(ok, f"{label}：实际抓取 cap_pos≈{tone_len}（300ms 整段）",
              f"cap_pos={st['cap_pos']}（期望 {tone_len}±480）")
    cap_pos = capture_pos(res, label, state, expect_full=False)
    samples = dump_range(mix, 0, cap_pos, args.dump_chunk)
    first, last = find_signal_bounds(samples)
    if first is None or last is None:
        res.check(False, f"{label}：抓取里没有探针音调（cap 未在 tone 前武装？）",
                  f"抓回 {len(samples)} 帧、全零")
        raise ScenarioFail("抓取数据全零，无法验证淡入淡出")

    res.check(first <= 2, f"{label}：cap 与 tone 起点对齐（首帧即 10ms 淡入第 0 帧）",
              f"首个非零样本 index={first}")
    end = min(first + tone_len, len(samples))
    duration = end - first
    ok = abs(duration - tone_len) <= 480
    res.check(ok, f"{label}：抓取含整段 300ms（{tone_len}±480 帧）",
              f"实测 {duration} 帧（首帧 {first}，末帧 {last}）")

    steady_peak = peak_abs(samples, first + 4800, 4800)
    res.check(steady_peak > 0, f"{label}：稳态峰值可用", f"{steady_peak}")
    ok, detail = fade_in_ok(samples, first, steady_peak)
    res.check(ok, f"{label}：起始 10ms 包络从 0 线性升（无 pop）", detail)
    ok, detail = fade_out_ok(samples, end, steady_peak)
    res.check(ok, f"{label}：结束前 100ms 包络线性降到最后接近 0", detail)

    limit = slope_limit([(TONE_AMP, 1000)])
    step = max_abs_step(samples, first, duration)
    res.check(step <= limit, f"{label}：全程相邻样本 |Δ| ≤ {limit:.0f}（无单点跳变）",
              f"实测最大 |Δ|={step}")


# ---------------------------------------------------------------- 场景 E：通道启停不 pop

def scenario_e(res: Result, dev: Device, args: argparse.Namespace) -> None:
    label = "E 通道启停"
    mix = Mixer(dev)
    print("  指令：tone MUSIC 1760Hz 1500ms amp 8000 → 300ms 后 cap 武装 → "
          "tone PROMPT 440Hz 800ms amp 8000 → 用 440Hz onset 定位叠加点，再取稳态窗")
    slate(mix, 0.2)
    expect_vol(res, label, mix, 100)
    expect_tone(res, label, mix, "MUSIC", 1760, 1500, TONE_AMP)
    dev.pump(0.30)  # 契约：MUSIC 先单独播 300ms，再叠加 PROMPT
    # 叠加前武装抓取：过渡点（PROMPT 淡入 + MUSIC duck 斜坡）都在抓取窗内
    armed = mix.cap(args.cap_frames)
    res.check(armed == args.cap_frames,
              f"{label}：cap 在叠加前武装 {args.cap_frames} 帧", f"armed={armed}")
    expect_tone(res, label, mix, "PROMPT", 440, 800, TONE_AMP)
    dev.pump(0.45)
    state = mix.stat()
    res.check(state["channels"]["MUSIC"]["active"] == 1
              and state["channels"]["PROMPT"]["active"] == 1,
              f"{label}：叠加后 MUSIC/PROMPT 均 active=1",
              f"MUSIC={state['channels']['MUSIC']['active']} "
              f"PROMPT={state['channels']['PROMPT']['active']}")
    res.check(state["stat"]["clip"] == 0, f"{label}：clip=0", f"clip={state['stat']['clip']}")

    cap_pos = capture_pos(res, label, state)  # 音乐 1500ms 仍在播，抓取应取满 16384
    samples = dump_range(mix, 0, cap_pos, args.dump_chunk)
    first, last = find_signal_bounds(samples)
    if first is None or last is None:
        res.check(False, f"{label}：抓取里没有探针音调", f"抓回 {len(samples)} 帧、全零")
        raise ScenarioFail("抓取数据全零，无法验证叠加")

    # cap 与 tone PROMPT 命令之间有 ~50~100ms 往返，抓取窗开头是"MUSIC 全量"段：
    # 用 440Hz 能量定位 onset，稳态窗从 onset+10ms(淡入) +100ms(MUSIC duck 斜坡) 起。
    onset = find_tone_onset(samples, 440, ONSET_AMP_MIN, first)
    res.check(onset is not None, f"{label}：定位到 PROMPT 叠加起点（440Hz onset）",
              f"onset={onset}（首帧 {first}，cap_pos={cap_pos}）")
    if onset is None:
        raise ScenarioFail("未检测到 PROMPT 叠加（440Hz 始终未出现）")
    steady_start = onset + FADE_IN_FRAMES + args.steady_start
    avail = cap_pos - steady_start
    if avail < 4096:
        res.check(False, f"{label}：稳态窗不足（onset={onset} 后只剩 {avail} 帧）",
                  f"cap_pos={cap_pos}")
        raise ScenarioFail("叠加点太晚，抓取窗剩余不足以取稳态 FFT")
    win = min(args.fft_n, avail)
    print(f"  叠加点 onset≈{onset} 帧（{onset / RATE * 1000:.0f}ms），"
          f"稳态窗 [{steady_start},{steady_start + win}) 共 {win} 帧")

    # PROMPT 变为活跃后 MUSIC 目标增益 -12dB（voice_on 含 prompt）：稳态 1760 幅 ≈2000
    amp_1760 = tone_amplitude(samples, 1760, steady_start, win)
    amp_440 = tone_amplitude(samples, 440, steady_start, win)
    res.check(1700 <= amp_1760 <= 2300,
              f"{label}：叠加后 1760Hz 峰 ≈2000（PROMPT 活跃 → MUSIC duck -12dB）",
              f"{amp_1760:.0f}")
    res.check(7000 <= amp_440 <= 9000, f"{label}：叠加后 440Hz 峰 ≈8000（±12.5%）",
              f"{amp_440:.0f}")

    # 叠加点确实在抓取窗内：onset 起 10ms 窗内 440 分量仍明显低于稳态（淡入中）
    early_440 = tone_amplitude(samples, 440, onset, min(FADE_IN_FRAMES, cap_pos - onset))
    res.check(early_440 < amp_440 * 0.85,
              f"{label}：抓取含叠加起点（onset 处 440Hz 仍低 / 正在淡入）",
              f"onset 10ms 窗 {early_440:.0f} vs 稳态 {amp_440:.0f}")

    # 8000@440 + 2000@1760（独立正弦）→ RMS ≈ √((8000²+2000²)/2) ≈5831
    seg = samples[steady_start:steady_start + win]
    rms = math.sqrt(sum(v * v for v in seg) / len(seg))
    res.check(5200 <= rms <= 6500,
              f"{label}：叠加后 RMS ≈5831（8000 + 2000 两路独立正弦）", f"{rms:.0f}")

    limit = slope_limit([(TONE_AMP, 1760), (TONE_AMP, 440)])
    step = max_abs_step(samples, first, last - first + 1)
    res.check(step <= limit,
              f"{label}：全程相邻样本 |Δ| ≤ {limit:.0f}（含叠加点，无 pop）",
              f"实测最大 |Δ|={step}")


# ---------------------------------------------------------------- 场景 F：健康/堆稳定

def scenario_f(res: Result, dev: Device, args: argparse.Namespace) -> None:
    label = "F 健康"
    mix = Mixer(dev)
    rounds = args.heap_rounds
    print(f"  指令：{rounds} 轮 stop/reset → cap 2048 → 三通道 150ms 音调 → sys heap")
    run_mark = getattr(args, "run_mark", dev.mark())
    slate(mix, 0.2)
    expect_vol(res, label, mix, 100)
    baseline = mix.heap()
    internals = [baseline["internal"]["free"]]
    psrams = [baseline["psram"]["free"]]
    print(f"  基线：内部堆剩余 {internals[0]} B / PSRAM 剩余 {psrams[0]} B")
    clips: list[int] = []
    for i in range(rounds):
        slate(mix, 0.1)
        mix.cap(2048)
        for ch, freq in (("TTS", 1000), ("PROMPT", 440), ("MUSIC", 1760)):
            expect_tone(res, label, mix, ch, freq, 150, TONE_AMP)
        dev.pump(0.2)
        clips.append(mix.stat()["stat"]["clip"])
        heap = mix.heap()
        internals.append(heap["internal"]["free"])
        psrams.append(heap["psram"]["free"])
        print(f"  第 {i + 1}/{rounds} 轮：内部堆 {internals[-1]} B / PSRAM {psrams[-1]} B / "
              f"clip={clips[-1]}")

    ok, detail = heap_range_ok(internals, HEAP_INT_TOL)
    res.check(ok, f"{label}：{rounds} 轮内部堆峰谷差 ≤{HEAP_INT_TOL} B", detail)
    ok, detail = heap_range_ok(psrams, HEAP_PSRAM_TOL)
    res.check(ok, f"{label}：{rounds} 轮 PSRAM 峰谷差 ≤{HEAP_PSRAM_TOL} B", detail)
    res.check(all(c == 0 for c in clips), f"{label}：{rounds} 轮 clip 恒 0", f"clips={clips}")

    errs = error_lines(dev.texts(run_mark))
    res.check(not errs, f"{label}：全程日志无 E(/ERROR/panic",
              "；".join(errs[:3]) if errs else "")


# ---------------------------------------------------------------- --self-test

def _synth(components: list[tuple[float, int, float]], n: int, *,
           fade_in: int = 0, tone_len: int | None = None, rate: int = RATE) -> list[int]:
    """离线构造合成信号：components = [(幅度, 频率, 起始延时秒)]，相位从峰值开始。

    tone_len: 音调有效长度（之后为静音）；fade_in: 前段线性淡入帧数。
    """
    np = _np()
    t = np.arange(n, dtype=np.float64) / rate
    sig = np.zeros(n, dtype=np.float64)
    for amp, freq, delay in components:
        sig += amp * np.cos(2.0 * math.pi * freq * (t - delay))
    if tone_len is not None and tone_len < n:
        sig[tone_len:] = 0.0
    if fade_in > 0:
        env = np.ones(n, dtype=np.float64)
        env[:fade_in] = np.arange(fade_in, dtype=np.float64) / fade_in
        sig *= env
    return [int(v) for v in np.clip(np.round(sig), -32768, 32767)]


def _synth_tone_with_fades(amp: float, freq: int, n: int, tone_len: int,
                           fade_in: int = 480, fade_out: int = 4800) -> list[int]:
    """构造单音调：淡入 + 稳态 + 淡出 + 尾部静音（用于 D 的正例/负例）。"""
    np = _np()
    t = np.arange(tone_len, dtype=np.float64) / RATE
    sig = amp * np.cos(2.0 * math.pi * freq * t)
    env = np.ones(tone_len, dtype=np.float64)
    if fade_in:
        env[:fade_in] = np.arange(fade_in, dtype=np.float64) / fade_in
    if fade_out:
        env[tone_len - fade_out:] = np.arange(fade_out, 0, -1, dtype=np.float64) / fade_out
    sig *= env
    out = np.zeros(n, dtype=np.float64)
    out[:tone_len] = sig
    return [int(v) for v in np.clip(np.round(out), -32768, 32767)]


def run_self_test() -> bool:
    """离线自检：解析/FFT/断言逻辑（不连设备），正负例都断言避免假 PASS。"""
    print("\n===== --self-test：混音探针解析 / FFT / 断言逻辑自检（不连设备） =====")
    res = Result()

    # ---- 解析：MIXSTAT / MIXCH
    stat_lines = [
        "I (1234) DIAG: 无关日志",
        "MIXSTAT vol=42 vol_q8=107 vol_target=107 limiter=3 limiter_peak=30000 "
        "clip=0 mix_peak=12345 block_max_us=4200 cap_len=16384 cap_pos=16384",
        "MIXCH ch=TTS active=1 gain_q8=256 level_ms=1500 peak=8000",
        "MIXCH ch=PROMPT active=1 gain_q8=128 level_ms=1400 peak=4000",
        "MIXCH ch=MUSIC active=1 gain_q8=64 level_ms=1300 peak=2000",
    ]
    st = parse_mixstat(stat_lines)
    ok = st is not None and st["vol"] == 42 and st["vol_q8"] == 107 and st["limiter"] == 3
    res.check(ok, "S1 MIXSTAT 行解析（vol/vol_q8/limiter）")
    if st is not None:
        ok = (st["limiter_peak"] == 30000 and st["clip"] == 0
              and st["mix_peak"] == 12345 and st["block_max_us"] == 4200
              and st["cap_len"] == 16384 and st["cap_pos"] == 16384)
        res.check(ok, "S2 MIXSTAT 行解析（limiter_peak/clip/mix_peak/block_max_us/cap_len/cap_pos）")
    # 旧固件（无 cap 字段）也要能解析，只是 cap_len/cap_pos 为 None（由 stat() 负责报错）
    st_old = parse_mixstat([
        "MIXSTAT vol=42 vol_q8=107 vol_target=107 limiter=0 limiter_peak=0 "
        "clip=0 mix_peak=0 block_max_us=100"
    ])
    res.check(st_old is not None and st_old["cap_len"] is None and st_old["cap_pos"] is None,
              "S2b 旧固件无 cap_len/cap_pos 时兼容解析（值为 None）")
    # D 的实际抓取数：cap_pos=14400 < cap_len=16384，dump 只能读 [0, cap_pos)
    st_d = parse_mixstat([
        "MIXSTAT vol=100 vol_q8=256 vol_target=256 limiter=0 limiter_peak=0 clip=0 "
        "mix_peak=8000 block_max_us=3000 cap_len=16384 cap_pos=14400"
    ])
    res.check(st_d is not None and st_d["cap_pos"] == 14400 and
              (st_d["cap_pos"] + 512 > st_d["cap_pos"]),
              "S2c D 场景 cap_pos=14400 < cap_len=16384 可解析（只允许读 [0,cap_pos)）")
    res.check(RE_MIXCAP_ERR.search("MIXCAP err=ESP_ERR_INVALID_STATE") is not None,
              "S2d dump 越界错误行 MIXCAP err=... 可识别（视为抓取不完整）")
    chs = parse_mixchannels(stat_lines)
    ok = (len(chs) == 3 and chs["TTS"]["gain_q8"] == 256
          and chs["PROMPT"]["gain_q8"] == 128 and chs["MUSIC"]["gain_q8"] == 64)
    res.check(ok, "S3 MIXCH 三通道解析 + duck 增益 256/128/64")
    res.check(parse_mixstat(["PLAYERSTAT jobs=1 pcm=0 mp3=0"]) is None,
              "S4 非探针日志不会被解析（负例）")

    # ---- 解析：vol / tone / reset / stop / cap
    vol = parse_mixvol(["MIXVOL vol=50 err=ESP_OK"])
    res.check(vol is not None and vol["vol"] == 50 and vol["err"] == "ESP_OK",
              "S5 MIXVOL 解析（vol=50 err=ESP_OK）")
    tone = parse_mixtone(["MIXTONE ch=MUSIC freq=1760 ms=1500 amp=8000 err=ESP_OK"])
    ok = (tone is not None and tone["ch"] == "MUSIC" and tone["freq"] == 1760
          and tone["ms"] == 1500 and tone["amp"] == 8000)
    res.check(ok, "S6 MIXTONE 解析（ch/freq/ms/amp/err）")
    bad = parse_mixtone(["MIXTONE ch=TTS freq=1000 ms=300 amp=8000 err=ESP_ERR_INVALID_STATE"])
    res.check(bad is not None and bad["err"] != "ESP_OK",
              "S7 MIXTONE 错误码可识别（播放任务忙时不得当成功）")
    res.check(parse_ok(RE_MIXRESET, ["MIXRESET ok=1"]) == 1, "S8 MIXRESET ok=1 解析")
    res.check(parse_ok(RE_MIXSTOP, ["MIXSTOP ok=1"]) == 1, "S9 MIXSTOP ok=1 解析")
    res.check(parse_cap_armed(["MIXCAP armed=16384"]) == 16384,
              "S10 MIXCAP armed=16384 解析")

    # ---- 解析：dump（uint16 → int16 二补码；固件 printf("%04x") 连写、无分隔符）
    expected = [-1000, 0, 32767, -32768, 1000, -1, 31744, 258]
    dump_now = [
        "I (1) 设备日志混流",
        "MIXCAP off=0 count=8",
        "fc1800007fff800003e8ffff7c000102",
        "MIXCAPEND",
    ]
    rec = parse_dump(dump_now)
    ok = rec is not None and rec["samples"] == expected
    res.check(ok, "S11 dump 解析（固件连写 %04x）：uint16 按 int16 二补码（fc18→-1000、8000→-32768）")
    res.check(rec is not None and rec["off"] == 0 and rec["count"] == 8 and rec["end"],
              "S12 dump header/off/count/MIXCAPEND 解析")
    dump_spaced = [
        "MIXCAP off=0 count=16",
        "fc18 0000 7fff 8000 03e8 ffff 7c00 0102 03e8 fc18 7fff 8000 0000 ffff 0102 7c00",
        "MIXCAPEND",
    ]
    rec2 = parse_dump(dump_spaced)
    res.check(rec2 is not None and len(rec2["samples"]) == 16 and rec2["samples"][:8] == expected,
              "S12b 空格分隔写法也兼容（16 样本/行）")
    res.check(_dump_row_tokens("fc1800007fff8000") == ["fc18", "0000", "7fff", "8000"],
              "S12c 连写 hex 行按 4 字符切 token")
    res.check(_dump_row_tokens("I (123) MIXCAP armed=16384") is None,
              "S12d 日志行不会被当成 hex 数据（负例）")
    res.check(parse_dump(["MIXCAPEND"]) is None, "S13 缺 header/样本时返回 None（负例）")

    # ---- FFT 幅度与幅度比（场景 A 口径）
    tone_len = 16384
    a = _synth([(8000, 1000, 0.0), (4000, 440, 0.01), (2000, 1760, 0.02)],
               tone_len, fade_in=480)
    win = 8192
    amps = {f: tone_amplitude(a, f, STEADY_START, win) for f in (1000, 440, 1760)}
    ok = all(abs(amps[f] - nominal) <= 0.01 * nominal
             for f, nominal in ((1000, 8000.0), (440, 4000.0), (1760, 2000.0)))
    res.check(ok, "S14 FFT 幅度估计误差 ≤1%（1000/440/1760Hz）",
              f"实测 1000Hz={amps[1000]:.1f} 440Hz={amps[440]:.1f} 1760Hz={amps[1760]:.1f}")
    res.check(ratio_ok(amps[440], amps[1000] * 0.5, DUCK_TOL)[0]
              and ratio_ok(amps[1760], amps[1000] * 0.25, DUCK_TOL)[0],
              "S15 三通道幅度比 1:0.5:0.25 判定（正例）")
    res.check(not ratio_ok(0.25 * 1.09, 0.25, DUCK_TOL)[0],
              "S16 幅度比偏差 9% > 8% 判 FAIL（负例）")
    res.check(tone_amplitude([0] * 4096, 1000, 0, 2048) == 0.0,
              "S17 静音窗幅度为 0")

    # ---- 音量线性（场景 B 口径）
    res.check(ratio_ok(4000, 8000 * 0.5, VOL_TOL)[0], "S18 vol 50 幅度比 0.5 判定（正例）")
    res.check(not ratio_ok(4200, 8000 * 0.5, VOL_TOL)[0],
              "S19 vol 50 偏差 5% > 2% 判 FAIL（负例）")
    res.check(peak_abs([0] * 2048) == 0,
              "S20 vol 0 档抓取全零判定（绝对零，不走比值）")

    # ---- 淡入淡出（场景 D 口径）
    good = _synth_tone_with_fades(8000, 1000, 16384, 14400)
    first, last = find_signal_bounds(good)
    steady = peak_abs(good, first + 4800, 4800)
    ok_in, detail_in = fade_in_ok(good, first, steady)
    res.check(ok_in, "S21 10ms 淡入判定（合成正例）", detail_in)
    no_fade = _synth_tone_with_fades(8000, 1000, 16384, 14400, fade_in=1)
    first_nf, _ = find_signal_bounds(no_fade)
    ok_nf, _ = fade_in_ok(no_fade, first_nf, peak_abs(no_fade, first_nf + 4800, 4800))
    res.check(not ok_nf, "S22 无淡入（首块直接满幅）判 FAIL（负例）")
    ok_out, detail_out = fade_out_ok(good, last + 1, steady)
    res.check(ok_out, "S23 100ms 淡出判定（合成正例）", detail_out)
    abrupt = _synth_tone_with_fades(8000, 1000, 16384, 14400, fade_out=1)
    _, last_a = find_signal_bounds(abrupt)
    ok_ab, _ = fade_out_ok(abrupt, last_a + 1, peak_abs(abrupt, 4800, 4800))
    res.check(not ok_ab, "S24 无淡出（直接截断）判 FAIL（负例）")
    # 末 1ms 包络 < 5% 稳态：4.8%（旧固件步进取整截断的尾值）在档内，6% 判 FAIL
    end_d = last + 1  # 音调结束点（=good 的 tone_len 边界），尾段要改在 [end-48,end) 上
    tail_ok = list(good)
    for i in range(48):
        tail_ok[end_d - 48 + i] = int(384 * math.cos(2 * math.pi * i / 48))
    ok_tok, detail_tok = fade_out_ok(tail_ok, end_d, steady)
    res.check(ok_tok, "S24b 末 1ms 包络 4.8% 稳态仍在 5% 档内（边界正例）", detail_tok)
    tail_bad = list(good)
    for i in range(48):
        tail_bad[end_d - 48 + i] = int(480 * math.cos(2 * math.pi * i / 48))
    ok_tbad, _ = fade_out_ok(tail_bad, end_d, steady)
    res.check(not ok_tbad, "S24c 末 1ms 包络 6% 稳态判 FAIL（负例）")

    # ---- 斜率/pop 阈值（场景 D/E 口径）
    limit_d = slope_limit([(8000, 1000)])
    limit_e = slope_limit([(8000, 1760), (8000, 440)])
    res.check(1500 <= limit_d <= 1700, "S25 单通道 1kHz 斜率阈值 ≈1600",
              f"{limit_d:.0f}")
    res.check(3300 <= limit_e <= 3700, "S26 1760+440 合成斜率阈值 ≈3480",
              f"{limit_e:.0f}")
    res.check(max_abs_step(good, first, last - first) <= limit_d,
              "S27 正常淡入淡出无 |Δ| 超阈（正例）")
    popped = list(good)
    popped[1000] = popped[1000] + 5000
    res.check(max_abs_step(popped, first, last - first) > limit_d,
              "S28 注入单点跳变 +5000 判超阈（负例）")

    # ---- 软限幅（场景 C 口径）
    soft = [max(-CEILING, min(CEILING, v)) for v in
            _synth([(20000, 440, 0.0), (10000, 1000, 0.01), (5000, 1760, 0.02)], 8192,
                   fade_in=480)]
    ok_c, detail_c = limiter_ok(soft[STEADY_START:])
    res.check(ok_c, "S29 软限幅输出 ≤29196 且无硬削波（正例）", detail_c)
    hard = list(soft)
    hard[STEADY_START + 100] = 32767  # 必须落在被测稳态窗内，否则会被切片丢掉
    res.check(not limiter_ok(hard[STEADY_START:])[0],
              "S30 出现 |x|≥32000 硬削波判 FAIL（负例）")

    # ---- E 真实时序（场景 E 口径）：MUSIC 先播 → PROMPT 在 2400 帧后淡入，MUSIC duck -12dB
    np = _np()
    n_e = 16384
    onset_true = 2400
    idx = np.arange(n_e)
    t_e = idx / float(RATE)
    ramp = np.clip((idx - onset_true) / float(FADE_OUT_FRAMES * 0 + 4800), 0.0, 1.0)
    music_amp = 8000.0 - (8000.0 - 2000.0) * ramp
    music_amp[:onset_true] = 8000.0
    sig_e = music_amp * np.cos(2.0 * math.pi * 1760 * t_e)
    loc = idx - onset_true
    active = (loc >= 0) & (loc < int(0.8 * RATE))
    env = np.zeros(n_e, dtype=np.float64)
    env[active] = np.clip(loc[active] / float(FADE_IN_FRAMES), 0.0, 1.0)
    sig_e += 8000.0 * env * np.cos(2.0 * math.pi * 440 * t_e)
    e = [int(v) for v in np.clip(np.round(sig_e), -32768, 32767)]
    onset = find_tone_onset(e, 440, ONSET_AMP_MIN, 0)
    res.check(onset is not None and onset_true - 240 <= onset <= onset_true + 480,
              "S29b E 用 440Hz 能量定位 PROMPT onset（误差 ≤1 个检测窗）",
              f"onset={onset}（真值 {onset_true}）")
    if onset is not None:
        steady_start = onset + FADE_IN_FRAMES + STEADY_START
        win = min(8192, n_e - steady_start)
        amp440 = tone_amplitude(e, 440, steady_start, win)
        amp1760 = tone_amplitude(e, 1760, steady_start, win)
        res.check(7000 <= amp440 <= 9000 and 1700 <= amp1760 <= 2300,
                  "S29c E 稳态窗 440Hz≈8000 / 1760Hz≈2000（duck -12dB）",
                  f"440={amp440:.0f} 1760={amp1760:.0f}")
        seg = e[steady_start:steady_start + win]
        e_rms = math.sqrt(sum(v * v for v in seg) / len(seg))
        res.check(5200 <= e_rms <= 6500,
                  "S29d E 稳态窗 RMS≈5831（8000²与2000²两路独立正弦）", f"{e_rms:.0f}")
        early = tone_amplitude(e, 440, onset, FADE_IN_FRAMES)
        res.check(early < amp440 * 0.85,
                  "S29e E onset 处 440Hz 仍低（证明抓到叠加起点）",
                  f"{early:.0f} vs 稳态 {amp440:.0f}")

    # ---- 堆稳定（场景 F 口径）
    res.check(heap_range_ok([100000, 100400, 100800], HEAP_INT_TOL)[0],
              "S31 内部堆峰谷差 800B ≤2KB（正例）")
    res.check(not heap_range_ok([100000, 103000], HEAP_INT_TOL)[0],
              "S32 内部堆峰谷差 3KB >2KB 判 FAIL（负例）")
    res.check(heap_range_ok([8000000, 8003500], HEAP_PSRAM_TOL)[0],
              "S33 PSRAM 峰谷差 3.5KB ≤4KB（正例）")
    res.check(not heap_range_ok([8000000, 8004500], HEAP_PSRAM_TOL)[0],
              "S34 PSRAM 峰谷差 4.5KB >4KB 判 FAIL（负例）")

    # ---- sys heap 解析 / 错误日志过滤
    heap = parse_heap([
        "内部 RAM: 总量 393216 B, 剩余 200000 B, 最大块 120000 B, "
        "最低水位 150000 B, 峰值占用 50000 B",
        "PSRAM   : 总量 8388608 B, 剩余 8000000 B, 最大块 4000000 B, "
        "最低水位 7900000 B, 峰值占用 100000 B",
    ])
    ok = (heap.get("internal", {}).get("free") == 200000
          and heap.get("psram", {}).get("free") == 8000000)
    res.check(ok, "S35 sys heap 内部 RAM/PSRAM 解析")
    # 真机实测（T3.3 F 场景）：mixer_test 不起 mock，设备反复重连会刷这些 E 行，应全部忽略
    noise_lines = [
        "E (5738) transport_base: Failed to open a new connection: 32772",
        "E (5748) transport_ws: Error connecting to host 192.168.1.11:8765",
        "E (5760) websocket_client: read loop failed",
        "E (5770) esp-tls: connection refused",
        "E (5780) WS: WS 错误：type=0",
        "E (5790) NET: 连接断开，3s 后重连",
        "E (5800) custom_tag: Error connecting to host 10.0.0.1",  # TAG 不认识 → 行内兜底
        "E (100) wifi: 既有网络噪声（忽略）",
    ]
    real_lines = [
        "E (101) PLAYER: i2s write failed",
        "Guru Meditation Error: Core 0 panic'ed",
        "assert failed: xQueueGenericSend queue.c:123",
    ]
    errs = error_lines(noise_lines + real_lines)
    res.check(errs == real_lines,
              "S36 错误日志过滤：transport/websocket/WS 噪声全忽略，PLAYER/panic/assert 仍检出",
              f"检出 {errs}")
    res.check(len(error_lines(["I (1) PLAYER: 正常日志"])) == 0,
              "S37 正常日志不误报错误（负例）")

    # ---- 阈值常数一致性
    res.check(CEILING == 29196 and HARD_CLIP == 32000 and FADE_OUT_FRAMES == 4800,
              "S38 契约阈值常数一致（29196 / 32000 / 100ms）")

    return res.summary(name="MIXER_TEST_SELFTEST")


# ---------------------------------------------------------------- --dry-run 计划

def print_plan(args: argparse.Namespace) -> None:
    print("\n===== --dry-run：不连设备，只打印执行计划 =====")
    if args.port:
        print(f"串口：{args.port}（不会打开）")
    else:
        print("串口：自动探测唯一 /dev/cu.usbmodem*（不会打开）")
    print(f"日志：{args.log or '（不写文件）'}"
          "（若指定，设备日志末尾会追加 [ASSERT] 断言明细，含 FAIL detail）")
    print("\n启动流程：打开 115200 串口 → 等 console 就绪（player mix stat 可解析）→ "
          "`player mix stop` + `player mix reset` 清理 → 读初始音量 → 依次跑场景 → "
          "结束清理并 `player mix vol <初始>` 恢复")
    print("  注意：A/C/D/E/F 场景开头会 `player mix vol 100`（幅度断言基准），"
          "B 专门测 100/50/0；结束恢复进入时的音量。")
    print("\n场景与断言：")
    print("  A 三通道混音：tone TTS 1000/2500ms、PROMPT 440、MUSIC 1760（amp 8000）"
          "→ 等 0.35s（duck 斜坡）→ cap 16384 抓满 → FFT 窗 0..8192")
    print("    （真机实测 tone 命令 ~100ms 往返、PROMPT/MUSIC 在 ~100/~200ms 起播，"
          "故先起三路再武装）")
    print("    断言：三频幅度 ≈8000/4000/2000（±8%）、比值 1:0.5:0.25、gain_q8 256/128/64、")
    print("          mix_peak 与抓取峰值 ±2%、clip=0、block_max_us<10000")
    print("  B 音量线性：vol 100/50/0 → cap → tone MUSIC 1000/1500ms amp 8000 → 稳态幅度")
    print("    断言：幅度比 1:0.5:0（±2%）、vol 0 全零、mix_peak=0")
    print("  C 软限幅：cap → tone 三通道 440/1000/1760 amp 20000/1500ms")
    print(f"    断言：|x|≤{CEILING}+{CEILING_QUANT}、无 |x|≥{HARD_CLIP}、limiter>0、")
    print(f"          limiter_peak>{CEILING}、clip=0、mix_peak 与峰值 ±2%")
    print("  D 淡入淡出：cap（先）→ tone TTS 1000/300ms amp 8000 → dump cap_pos(14400)")
    print(f"    断言：10ms 淡入单调升、100ms 淡出线性降且末 1ms <5% 稳态、"
          f"|Δ|≤{slope_limit([(8000, 1000)]):.0f}")
    print("  E 通道启停：tone MUSIC 1760/1500ms → 300ms 后 cap 武装 → 叠加 tone PROMPT 440/800ms")
    print("    断言：用 440Hz onset 定位叠加点，稳态窗从 onset+110ms 起；440Hz≈8000、")
    print("          1760Hz≈2000（PROMPT 令 MUSIC duck -12dB）、RMS≈5831、")
    print(f"          onset 处 440Hz 仍低、|Δ|≤{slope_limit([(8000, 1760), (8000, 440)]):.0f} 无 pop")
    print(f"  F 健康：{args.heap_rounds} 轮短音调 → 内部堆峰谷差 ≤2KB、PSRAM ≤4KB、clip 恒 0、")
    print("          全程日志无真错误（E(/ERROR/panic/assert/Guru）；连接重连类噪声按 TAG 白名单")
    print("          + 行内片段忽略，含 transport_base/transport_ws/websocket_client/esp-tls 等）")
    print(f"\n抓取流程：每场景 stat 取 cap_len/cap_pos → 只 dump [0, cap_pos)；"
          f"A/B/C/E 应取满（cap_pos=cap_len={args.cap_frames}），D 的 300ms 音调 cap_pos≈14400；"
          f"dump 越界（off+count>cap_pos）固件回 MIXCAP err，脚本按抓取不完整重试一次")
    print(f"关键参数：cap_frames={args.cap_frames}、稳态窗起点={args.steady_start} 帧、"
          f"FFT={args.fft_n} 帧、dump_chunk={args.dump_chunk}")
    print("环境性失败（串口超时/回显被日志冲掉）：WARN + 整场景重试一次，不改阈值。")
    print("探针契约出处：T3.3-A 固件 `components/diag` 的 `player mix stat|reset|vol|tone|"
          "stop|cap|dump` 输出格式；本文件开头的 docstring 是逐字契约。")


def print_dry_run_rc(ok: bool) -> int:
    print(f"\nDRY_RUN_SELFTEST: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


# ---------------------------------------------------------------- 真机主流程

def wait_console_ready(dev: Device, result: Result, timeout: float) -> None:
    print("\n===== 等待 console 就绪 =====")
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = dev.console(
            "player mix stat",
            [RE_MIXSTAT, RE_MIXCH_TTS, RE_MIXCH_PROMPT, RE_MIXCH_MUSIC],
            timeout=1.0,
        )
        if parse_mixstat(lines) is not None and len(parse_mixchannels(lines)) >= 3:
            print("  console 就绪（player mix stat 可解析，T3.3 探针存在）")
            return
        if any(RE_UNKNOWN_CMD.search(t) for t in lines):
            raise SystemExit(
                "固件没有 `player mix` 探针：确认已烧录 T3.3-A 固件、且 "
                "CONFIG_DIAG_ENABLE_PLAYER_TEST 打开"
            )
        dev.pump(0.2)
    boot = dev.texts(0)
    if any(RE_BOOT_READY.search(t) for t in boot):
        raise SystemExit(
            "设备已启动但 `player mix stat` 无输出：T3.3-A 探针未就绪（或 console 未启用）"
        )
    raise SystemExit(
        f"{timeout:.0f}s 内 console 无响应：确认设备已上电/烧录、已关闭 idf.py monitor、"
        "串口正确（--port），必要时加 --reset"
    )


def run(args: argparse.Namespace) -> int:
    result = Result()
    port = detect_port(args.port)
    print(f"\n===== 串口 {port}（{BAUDRATE}，脚本独占；tts_stream_test/mp3_test 先关掉） =====")
    dev = Device(port, args.log)
    dev.log_note(f"# mixer_test 端口 {port} 波特率 {BAUDRATE}（断言明细在文件末尾 [ASSERT] 段）")
    original_vol: int | None = None
    ok = False
    try:
        if args.reset:
            print("  复位设备（--reset）…")
            dev.reset()
            dev.pump(0.3)
        wait_console_ready(dev, result, args.ready_timeout)

        mix = Mixer(dev)
        try:
            original_vol = mix.stat()["stat"]["vol"]
        except EnvError:
            original_vol = None
        # 脚本开头清理（契约要求）
        mix.stop()
        mix.reset()
        dev.pump(0.1)
        print(f"  初始音量：{original_vol if original_vol is not None else '未知'}"
              f"（player mix vol 不落盘；结束会恢复）")
        args.run_mark = dev.mark()  # F 场景用它扫全程日志错误

        run_scenario("A 三通道混音（duck 1:0.5:0.25）", scenario_a, result, dev, args)
        run_scenario("B 音量线性（100/50/0）", scenario_b, result, dev, args)
        run_scenario("C 软限幅（-1dBFS）", scenario_c, result, dev, args)
        run_scenario("D 淡入淡出（10ms/100ms）", scenario_d, result, dev, args)
        run_scenario("E 通道启停不 pop", scenario_e, result, dev, args)
        if args.skip_heap:
            print("\n===== 场景 F 健康（--skip-heap 跳过）=====")
        else:
            run_scenario("F 健康（10 轮堆/PSRAM 稳定）", scenario_f, result, dev, args)
    finally:
        try:
            safe_stop(dev)
            if original_vol is not None:
                try:
                    dev.console(f"player mix vol {original_vol}", [RE_MIXVOL], timeout=1.0)
                except Exception:
                    pass
        finally:
            try:
                if result.rows:
                    ok = result.summary()
                    result.write_log(dev)  # 断言明细（含 FAIL detail）追加到 --log
            finally:
                dev.close()

    return 0 if ok else 1


# ---------------------------------------------------------------- CLI

def main() -> int:
    parser = argparse.ArgumentParser(
        description="T3.3-B 三通道混音真机验收（混音/duck/音量线性/软限幅/淡入淡出/堆稳定）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default=None,
                        help="设备串口；缺省自动探测唯一 /dev/cu.usbmodem*")
    parser.add_argument("--log", default="", help="把完整串口日志写到该文件")
    parser.add_argument("--dry-run", action="store_true",
                        help="不连设备：打印执行计划 + 跑离线自检")
    parser.add_argument("--self-test", action="store_true",
                        help="离线自检解析/FFT/断言逻辑（不连设备）")
    parser.add_argument("--reset", action="store_true",
                        help="先复位设备再等 console 就绪（默认不复位：烧录后设备已在跑）")
    parser.add_argument("--ready-timeout", type=float, default=15.0,
                        help="等 console/探针就绪的超时（秒）")
    parser.add_argument("--heap-rounds", type=int, default=10,
                        help="F 健康场景的轮数")
    parser.add_argument("--skip-heap", action="store_true", help="跳过 F 健康场景（快速冒烟）")
    parser.add_argument("--cap-frames", type=int, default=CAP_MAX,
                        help=f"场景抓取帧数（1~{CAP_MAX}，16384=341ms）")
    parser.add_argument("--steady-start", type=int, default=STEADY_START,
                        help="稳态窗起点（帧，默认 4800=100ms，跳过淡入/duck 斜坡）")
    parser.add_argument("--fft-n", type=int, default=FFT_N,
                        help="稳态窗 FFT 帧数（默认 8192≈170ms）")
    parser.add_argument("--dump-chunk", type=int, default=DUMP_CHUNK,
                        help="单次 dump 帧数（1~512）")
    args = parser.parse_args()

    if args.self_test:
        return 0 if run_self_test() else 1
    if args.dry_run:
        print_plan(args)
        return print_dry_run_rc(run_self_test())

    if not 1 <= args.cap_frames <= CAP_MAX:
        raise SystemExit(f"--cap-frames 必须在 1~{CAP_MAX}")
    if args.steady_start < FADE_IN_FRAMES:
        raise SystemExit(f"--steady-start 必须 ≥{FADE_IN_FRAMES}（10ms 淡入）")
    if args.steady_start + args.fft_n > args.cap_frames:
        raise SystemExit("--steady-start + --fft-n 不能超过 --cap-frames")
    if not 1024 <= args.fft_n <= CAP_MAX:
        raise SystemExit("--fft-n 必须在 1024~16384")
    if not 1 <= args.dump_chunk <= DUMP_CHUNK:
        raise SystemExit(f"--dump-chunk 必须在 1~{DUMP_CHUNK}")
    if args.heap_rounds < 1:
        raise SystemExit("--heap-rounds 必须 ≥1")
    if args.ready_timeout <= 0:
        raise SystemExit("--ready-timeout 必须 >0")

    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n中断（Ctrl-C）")
        return 130


if __name__ == "__main__":
    sys.exit(main())
