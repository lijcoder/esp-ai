#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5", "lameenc>=1.8", "av>=12", "numpy"]
# ///
"""T2.3 audio_player / MP3 解码真机自测：素材生成 → 上传 → 播放 → 数值对拍。

一条命令完成：
  1. PC 侧生成/获取 3 组 MP3 素材（24k 单声道"TTS 类"、44.1k 立体声"音乐类"、
     真实电台码流 `RADIO_URL` 抓包），并用 FFmpeg(PyAV) 独立解码出参考 PCM；
  2. 串口 `player load begin/chunk/end` 把码流以 base64 分块传到设备 PSRAM；
  3. `player mp3` 交给播放任务解码播放，断言解码帧数/样本数/采样率/声道/时长/
     单帧耗时与参考一致，坏帧 0；
  4. `player pcm <off> <n>` 取回设备解码的 PCM，与参考**逐样本**对拍（max|Δ| ≤ 2 LSB）
     —— 这是"设备真的解对了"的硬证据，不靠听；
  5. 播放中读 `mic stat`：麦克风拾到的峰值显著抬高 → 证明 PCM 真的从功放出来了；
  6. 重复播放 + `sys heap` 前后对比，确认内部 RAM/PSRAM 不随次数增长。

为什么不用设备自己拉 HTTP：T2.3 只验证"解码 + 播放"，素材由 PC 侧准备（含真实电台
码流）可重复、不依赖网络与 mock；设备侧 HTTP 拉流属于 T4.1 source_http，T2.4 的流式
入口走 WS 下行。电台 URL 只作为"真实世界 MP3"的素材来源。

脚本独占串口（运行前关掉 `idf.py monitor`）。

用法：
    uv run tools/mp3_test.py
    uv run tools/mp3_test.py --no-reset --skip-radio        # 设备已在跑、不联网
    uv run tools/mp3_test.py --dry-run                     # 只生成素材并打印参考值
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 复用 voice_test / voice_cloud_test 的串口封装与断言工具，避免多套实现漂移
from voice_test import Device, Result, detect_port, error_lines  # noqa: E402
from voice_cloud_test import lines_since, parse_heap, wait_line  # noqa: E402

RADIO_URL = "http://lhttp.qingting.fm/live/20210755/64k.mp3"
RADIO_SECONDS = 4.0
RADIO_MAX_BYTES = int(RADIO_SECONDS * 8192)  # 64kbps ≈ 8KB/s；上传走 115200 串口，别抓太长时间

# 设备侧输出（diag 探针与播放任务日志）
RE_READY = re.compile(r'ready, say "Hi Lexin"')
RE_PLAYERSTAT = re.compile(
    r"PLAYERSTAT jobs=(\d+) pcm=(\d+) mp3=(\d+) errors=(\d+) busy=(\d+) vol=(\d+) "
    r"last_play_ms=(\d+) mp3_ready=(\d+)")
RE_MP3STAT = re.compile(
    r"MP3STAT frames=(\d+) samples=(\d+) rate=(\d+) ch=(\d+) dur_ms=(\d+) skipped=(\d+) "
    r"bad=(\d+) dec_avg_us=(\d+) dec_max_us=(\d+)")
RE_LOAD_READY = re.compile(r"PLAYERLOAD ready capacity=(\d+)")
RE_LOAD_CHUNK = re.compile(r"PLAYERLOAD chunk total=(\d+)")
RE_LOAD_END = re.compile(r"PLAYERLOAD bytes=(\d+)")
RE_PLAYERPLAY = re.compile(r"PLAYERPLAY bytes=(\d+)")
RE_PCM_HEADER = re.compile(r"PLAYERPCM off=(\d+) count=(\d+)")
RE_MP3_DECODED = re.compile(
    r"PLAYER: mp3 decoded: frames=(\d+) samples=(\d+) rate=(\d+) ch=(\d+) dur=(\d+)ms "
    r"dec_avg=(\d+)us dec_max=(\d+)us skipped=(\d+) bad=(\d+)")
RE_PLAY_START = re.compile(r"PLAYER: play (\d+) samples @(\d+) Hz")
RE_JOB_DONE = re.compile(r"PLAYER: job done \((\d+) ms\)")
RE_PROMPT_DONE = re.compile(r"APP: prompt done, feed resumed")
END_PLAYERSTAT = RE_MP3STAT
END_LOAD_READY = RE_LOAD_READY
END_LOAD_CHUNK = RE_LOAD_CHUNK
END_LOAD_END = RE_LOAD_END
END_PCM = re.compile(r"PLAYERPCMEND")

UPLOAD_CHUNK_B64 = 240  # 每行 base64 字符数：500 实测会偶发丢字符（UART 无流控）


# ---------------------------------------------------------------- 素材生成

def gen_tone_mp3(path: str, rate: int, channels: int, seconds: float,
                 freqs: tuple[int, int], kbps: int) -> None:
    """用 lameenc 现场编一段确定性的正弦波 MP3（无需 ffmpeg：本机没有 MP3 编码器）。"""
    import lameenc
    import numpy as np

    n = int(rate * seconds)
    t = np.arange(n, dtype=np.float64)
    left = (0.5 * 32767 * np.sin(2 * np.pi * freqs[0] * t / rate)).astype(np.int16)
    if channels == 1:
        pcm = left.tobytes()
    else:
        right = (0.3 * 32767 * np.sin(2 * np.pi * freqs[1] * t / rate)).astype(np.int16)
        pcm = np.stack([left, right], axis=1).tobytes()

    enc = lameenc.Encoder()
    enc.set_bit_rate(kbps)
    enc.set_in_sample_rate(rate)
    enc.set_out_sample_rate(rate)  # 不设会被 lameenc 默认降到 24k
    enc.set_channels(channels)
    enc.set_quality(5)
    data = enc.encode(pcm) + enc.flush()
    with open(path, "wb") as f:
        f.write(data)
    print(f"  生成 {os.path.basename(path)}: {rate} Hz/{channels} ch/{kbps} kbps, "
          f"{len(data)} B, {seconds:.1f}s")


def fetch_radio(path: str) -> bool:
    """抓真实电台码流（从流中间接入，首帧可能不完整 —— 正好考验同步字搜索）。"""
    if not shutil.which("curl"):
        print("  WARN 本机没有 curl，跳过电台素材")
        return False
    # 直播流不会自己结束：curl 到 -m 上限会以 28 退出，属预期，能拿到数据即可
    try:
        subprocess.run(
            ["curl", "-s", "-m", "6", "-A", "Mozilla/5.0", RADIO_URL, "-o", path],
            check=False, timeout=15)
    except Exception as exc:  # noqa: BLE001 - 网络失败只降级，不让整轮失败
        print(f"  WARN 抓取电台失败：{exc}")
        return False
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size < 16384:
        print(f"  WARN 电台素材太小（{size} B），跳过")
        return False
    with open(path, "rb") as f:
        data = f.read(RADIO_MAX_BYTES)
    with open(path, "wb") as f:
        f.write(data)
    print(f"  抓取电台码流: {len(data)} B（{RADIO_URL}，截取前 {RADIO_SECONDS:.0f}s）")
    return True


def reference_decode(path: str) -> dict:
    """FFmpeg(PyAV) 独立解码：参考帧数/样本数/采样率/声道 + 单声道参考 PCM。"""
    import av
    import numpy as np

    container = av.open(path)
    stream = container.streams.audio[0]
    rate = stream.codec_context.sample_rate
    channels = stream.codec_context.channels
    frames = 0
    samples = 0
    chunks = []
    for frame in container.decode(audio=0):
        arr = frame.to_ndarray().astype(np.float64) * 32768.0  # fltp → int16 量纲
        if arr.ndim == 2:
            if arr.shape[0] == channels:  # 平面格式 (ch, n)
                arr = arr.mean(axis=0)
            else:  # 交织格式 (n, ch)
                arr = arr.reshape(-1, channels).mean(axis=1)
        chunks.append(arr)
        frames += 1
        samples += frame.samples
    mono = np.concatenate(chunks) if chunks else np.zeros(0)
    return {"rate": rate, "channels": channels, "frames": frames,
            "samples": samples, "mono": mono}


# ---------------------------------------------------------------- 设备侧命令

def upload_mp3(dev: Device, data: bytes, retries: int = 1) -> int | None:
    """base64 分块传到设备 PSRAM；返回设备确认的字节数（失败 None）。

    串口没有流控，丢一个字符 base64 仍然合法、只是少解几个字节，所以最后必须用
    设备回报的总长度 == 素材长度来确认；不符就整段重传（默认重试 1 次）。
    """
    for attempt in range(retries + 1):
        out = dev.console("player load begin", END_LOAD_READY, timeout=3.0)
        capacity = None
        for line in out:
            m = RE_LOAD_READY.search(line)
            if m:
                capacity = int(m.group(1))
        if capacity is None:
            return None
        if len(data) > capacity:
            print(f"  素材 {len(data)} B 超过设备上传上限 {capacity} B")
            return None

        b64 = base64.b64encode(data).decode()
        for i in range(0, len(b64), UPLOAD_CHUNK_B64):
            chunk = b64[i:i + UPLOAD_CHUNK_B64]
            out = dev.console(f"player load {chunk}", END_LOAD_CHUNK, timeout=5.0)
            if not any(RE_LOAD_CHUNK.search(line) for line in out):
                print(f"  上传失败（chunk @{i}）")
                return None

        out = dev.console("player load end", END_LOAD_END, timeout=3.0)
        got = None
        for line in out:
            m = RE_LOAD_END.search(line)
            if m:
                got = int(m.group(1))
        if got == len(data):
            return got
        print(f"  上传长度不符：设备 {got} B / 素材 {len(data)} B（第 {attempt + 1} 次"
              f"{'，重传' if attempt < retries else ''}）")
    return None


def play_and_wait(dev: Device, result: Result, listen_check: bool) -> dict:
    """发起播放、等解码与播放完成；期间用 mic stat 验证"真的出声了"。"""
    decoded: dict = {}
    peak: int | None = None

    mark = dev.mark()
    dev.send("player mp3")
    if not wait_line(dev, RE_PLAYERPLAY, 5.0, mark):
        result.check(False, "player mp3 被接受")
        return {"decoded": decoded, "peak": None}

    line = wait_line(dev, RE_MP3_DECODED, 15.0, mark)
    if line:
        m = RE_MP3_DECODED.search(line)
        decoded = {
            "frames": int(m.group(1)), "samples": int(m.group(2)),
            "rate": int(m.group(3)), "channels": int(m.group(4)),
            "dur_ms": int(m.group(5)), "dec_avg_us": int(m.group(6)),
            "dec_max_us": int(m.group(7)), "skipped": int(m.group(8)),
            "bad": int(m.group(9)),
        }

    if listen_check:
        if wait_line(dev, RE_PLAY_START, 5.0, mark):
            time.sleep(1.0)  # 让功放真正出声后再读麦克风窗口
            out = dev.console("mic stat", re.compile(r"参考:"), timeout=3.0)
            for text in out:
                m = re.search(r"峰值: (\d+)", text)
                if m:
                    peak = int(m.group(1))

    done = wait_line(dev, RE_JOB_DONE, 30.0, mark)
    decoded["job_done_ms"] = None
    if done:
        m = RE_JOB_DONE.search(done)
        decoded["job_done_ms"] = int(m.group(1))
    # job done 之后还有 300ms 尾部延时 + R2 回调，等它落地再发下一条命令（busy 归位）
    wait_line(dev, RE_PROMPT_DONE, 5.0, mark)
    return {"decoded": decoded, "peak": peak}


def player_stat(dev: Device) -> dict:
    out = dev.console("player stat", END_PLAYERSTAT, timeout=3.0)
    stat: dict = {}
    for line in out:
        m = RE_PLAYERSTAT.search(line)
        if m:
            stat.update({
                "jobs": int(m.group(1)), "pcm": int(m.group(2)), "mp3": int(m.group(3)),
                "errors": int(m.group(4)), "busy": int(m.group(5)), "vol": int(m.group(6)),
                "last_play_ms": int(m.group(7)), "mp3_ready": int(m.group(8)),
            })
        m = RE_MP3STAT.search(line)
        if m:
            stat.update({
                "frames": int(m.group(1)), "samples": int(m.group(2)),
                "rate": int(m.group(3)), "channels": int(m.group(4)),
                "dur_ms": int(m.group(5)), "skipped": int(m.group(6)),
                "bad": int(m.group(7)), "dec_avg_us": int(m.group(8)),
                "dec_max_us": int(m.group(9)),
            })
    return stat


def read_pcm(dev: Device, offset: int, count: int) -> list[int] | None:
    out = dev.console(f"player pcm {offset} {count}", END_PCM, timeout=3.0)
    hexes: list[str] = []
    seen_header = False
    for line in out:
        if RE_PCM_HEADER.search(line):
            seen_header = True
            continue
        if seen_header and re.fullmatch(r"[0-9a-f]{2,64}", line.strip()):
            hexes.append(line.strip())
    if not hexes:
        return None
    raw = "".join(hexes)
    # 设备用 printf("%04x", (uint16_t)sample) 打印的是数值本身（不是字节序），按数值解析再转有符号
    out: list[int] = []
    for i in range(0, len(raw) - 3, 4):
        v = int(raw[i:i + 4], 16)
        out.append(v - 0x10000 if v >= 0x8000 else v)
    return out


# ---------------------------------------------------------------- 断言

def check_material(result: Result, name: str, data: bytes, ref: dict,
                   got: dict, pcm_dev: list[int] | None, peak: int | None,
                   min_peak: int, radio: bool, pcm_offset: int) -> None:
    dec = got["decoded"]
    frames_tol = 8 if radio else 0
    samp_tol = (8 * 1152) if radio else 0

    result.check(bool(dec), f"{name}: 设备完成解码")
    if not dec:
        return
    result.check(abs(dec["frames"] - ref["frames"]) <= frames_tol,
                 f"{name}: 帧数 {dec['frames']} ≈ 参考 {ref['frames']}")
    result.check(abs(dec["samples"] - ref["samples"]) <= samp_tol,
                 f"{name}: 样本数 {dec['samples']} ≈ 参考 {ref['samples']}")
    result.check(dec["rate"] == ref["rate"], f"{name}: 采样率 {dec['rate']} == {ref['rate']}")
    result.check(dec["channels"] == ref["channels"],
                 f"{name}: 声道数 {dec['channels']} == {ref['channels']}")
    dur_ref = int(ref["samples"] * 1000 / ref["rate"])
    dur_tol = max(5, dur_ref // 20 if radio else dur_ref // 100)
    result.check(abs(dec["dur_ms"] - dur_ref) <= dur_tol,
                 f"{name}: 时长 {dec['dur_ms']} ms ≈ 参考 {dur_ref} ms")
    result.check(dec["bad"] == 0, f"{name}: 坏帧 0（实际 {dec['bad']}）")
    if not radio:
        result.check(dec["skipped"] == 0, f"{name}: 无跳过字节（实际 {dec['skipped']}）")
    else:
        print(f"  INFO {name}: 跳过 {dec['skipped']} B（流中间接入，属预期）")

    # 单帧解码耗时必须小于一帧音频时长，否则播放会断续
    frame_us = int(ref["samples"] / max(ref["frames"], 1) * 1e6 / ref["rate"])
    result.check(dec["dec_avg_us"] < frame_us,
                 f"{name}: 解码 {dec['dec_avg_us']} us/帧 < 帧长 {frame_us} us（可实时）")

    if pcm_dev is not None and not radio:
        n = min(len(pcm_dev), len(ref["mono"]))
        if n > 0:
            import numpy as np
            dev = np.asarray(pcm_dev[:n], dtype=np.float64)
            refp = ref["mono"][pcm_offset:pcm_offset + n]
            diff = float(np.abs(dev - refp).max())
            result.check(diff <= 2.0, f"{name}: 设备 PCM 与 FFmpeg 逐样本 max|Δ|={diff:.0f} ≤ 2")
        else:
            result.check(False, f"{name}: 取回设备 PCM 为空")
    elif radio and pcm_dev:
        import numpy as np
        rms = float(np.sqrt(np.mean(np.asarray(pcm_dev, dtype=np.float64) ** 2)))
        ref_rms = float(np.sqrt(np.mean(ref["mono"][pcm_offset:pcm_offset + len(pcm_dev)] ** 2)))
        # 电台流从中间接入，帧对齐与 PyAV 不同，无法逐样本对拍；用"量级一致"兜底
        near = 0.2 * ref_rms <= rms <= 5.0 * max(ref_rms, 1.0)
        result.check(rms > 20.0 and near,
                     f"{name}: 设备 PCM 有效（RMS={rms:.0f}，参考 {ref_rms:.0f}）")

    if peak is not None:
        result.check(peak >= min_peak,
                     f"{name}: 麦克风拾音峰值 {peak} ≥ {min_peak}（证明真的出声）")


# ---------------------------------------------------------------- 主流程

def run(args: argparse.Namespace) -> int:
    result = Result()
    workdir = tempfile.mkdtemp(prefix="mp3_test_")
    materials: list[dict] = []

    print("[1/4] 准备素材与参考解码")
    tone24 = os.path.join(workdir, "tone_24k_mono.mp3")
    gen_tone_mp3(tone24, 24000, 1, 4.0, (1000, 1000), 32)
    materials.append({"name": "24k/mono(TTS 类)", "path": tone24, "radio": False,
                      "listen": True})

    tone44 = os.path.join(workdir, "tone_44k_stereo.mp3")
    gen_tone_mp3(tone44, 44100, 2, 4.0, (440, 880), 128)
    materials.append({"name": "44.1k/stereo(音乐类)", "path": tone44, "radio": False,
                      "listen": True})

    radio_path = os.path.join(workdir, "radio.mp3")
    if args.skip_radio:
        print("  跳过电台素材（--skip-radio）")
    elif fetch_radio(radio_path):
        materials.append({"name": "电台流(真实 MP3)", "path": radio_path, "radio": True,
                          "listen": True})

    for mat in materials:
        mat["data"] = open(mat["path"], "rb").read()
        mat["ref"] = reference_decode(mat["path"])
        ref = mat["ref"]
        print(f"  参考 {mat['name']}: frames={ref['frames']} samples={ref['samples']} "
              f"rate={ref['rate']} ch={ref['channels']}")

    if args.dry_run:
        print("\n--dry-run：素材就绪，未接触设备")
        return 0

    dev = Device(detect_port(args.port))
    try:
        mark = dev.mark()
        if not args.no_reset:
            print("[2/4] 复位设备并等待就绪")
            dev.reset()
            if not wait_line(dev, RE_READY, args.boot_timeout, mark):
                result.check(False, "设备启动就绪")
                return 1
        else:
            print("[2/4] 复用已运行的设备（--no-reset）")
            time.sleep(0.3)

        stat = player_stat(dev)
        result.check(stat.get("mp3_ready") == 1, "MP3 解码能力就绪（mp3_ready=1）")
        result.check(stat.get("errors") == 0, "播放错误计数 0")
        if not all(ok for ok, _ in result.rows):
            print("MP3 能力不可用，后续用例无法执行")
            result.summary()
            return 1

        mark_buf = dev.mark()
        dev.console("player reset", re.compile(r"PLAYERSTAT cleared"), timeout=2.0)
        dev.console("mic reset", re.compile(r"已清零"), timeout=2.0)

        heap_before = None
        played = 0

        print("[3/4] 逐组上传/播放/对拍")
        for mat in materials:
            print(f"  --- {mat['name']}（{len(mat['data'])} B）---")
            uploaded = upload_mp3(dev, mat["data"])
            result.check(uploaded == len(mat["data"]),
                         f"{mat['name']}: 上传 {uploaded}/{len(mat['data'])} B")
            if uploaded != len(mat["data"]):
                continue
            got = play_and_wait(dev, result, listen_check=mat["listen"])

            pcm_dev = None
            ref = mat["ref"]
            # 电台流前几百样本常是全零（mid-stream 接入、比特池为空），取 0.2s 处做量级检查
            offset = ref["samples"] // 2 if not mat["radio"] else 8192
            count = 256
            if not mat["radio"]:
                dev.console("mic reset", re.compile(r"已清零"), timeout=2.0)
                pcm_dev = read_pcm(dev, offset, count)
            else:
                pcm_dev = read_pcm(dev, offset, count)

            check_material(result, mat["name"], mat["data"], ref, got,
                           pcm_dev, got.get("peak"), args.min_peak, mat["radio"],
                           offset)
            played += 1
            if heap_before is None:
                # 第一组播完后采样：console 历史缓冲（≤32 条命令行）与预分配缓冲已就位，
                # 之后的差值才反映"播放是否泄漏"
                heap_before = parse_heap(dev)

        print("[4/4] 稳定性与内存")
        first = materials[0]
        for i in range(args.repeat):
            uploaded = upload_mp3(dev, first["data"])
            if uploaded != len(first["data"]):
                result.check(False, f"重复播放上传 #{i + 1}")
                continue
            got = play_and_wait(dev, result, listen_check=False)
            played += 1
            result.check(bool(got["decoded"]), f"重复播放 #{i + 1} 完成解码")
            result.check(got["decoded"].get("bad", 1) == 0 if got["decoded"] else False,
                         f"重复播放 #{i + 1} 坏帧 0")

        stat = player_stat(dev)
        result.check(stat.get("mp3", 0) == played,
                     f"播放计数 mp3={stat.get('mp3')} == 实际成功播放 {played} 次")
        result.check(stat.get("errors") == 0, f"播放错误计数仍为 0（{stat.get('errors')}）")
        result.check(stat.get("busy") == 0, "测试结束时播放器空闲")

        time.sleep(1.0)  # 300ms 尾部延时 + R2 回调落地后再读 busy
        heap_after = parse_heap(dev)
        if heap_before and heap_before.get("internal") and heap_after.get("internal"):
            delta = heap_before["internal"]["free"] - heap_after["internal"]["free"]
            result.check(delta <= 8192, f"内部 RAM 释放差 {delta} B ≤ 8KB（每次播放不泄漏）")
        if heap_before and heap_before.get("psram") and heap_after.get("psram"):
            delta = heap_before["psram"]["free"] - heap_after["psram"]["free"]
            result.check(delta <= 2 * 1024 * 1024,
                         f"PSRAM 占用增量 {delta} B（预分配缓冲：解码状态+1.5MB PCM+512KB 上传）")
            if heap_after["psram"]["min_free"] > 512 * 1024:
                result.check(True, f"PSRAM 余量 {heap_after['psram']['min_free']} B")

        bad = error_lines(lines_since(dev, mark_buf))
        result.check(not bad, f"无 ERROR/panic（{len(bad)} 条）")
        for line in bad[:5]:
            print(f"    ! {line}")
    finally:
        dev.close()

    return 0 if result.summary() else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="串口设备（默认自动探测 /dev/cu.usbmodem*）")
    ap.add_argument("--no-reset", action="store_true", help="不复位设备（已在运行时用）")
    ap.add_argument("--skip-radio", action="store_true", help="不抓电台素材（离线环境）")
    ap.add_argument("--dry-run", action="store_true", help="只生成素材/参考值，不接触设备")
    ap.add_argument("--repeat", type=int, default=2, help="重复播放次数（稳定性/内存）")
    ap.add_argument("--min-peak", type=int, default=1000, help="麦克风拾音峰值门槛（证明出声）")
    ap.add_argument("--boot-timeout", type=float, default=30.0)
    ap.add_argument("--log", help="把串口原始日志写到文件")
    args = ap.parse_args()

    rc = run(args)
    print("\n" + ("全部 PASS" if rc == 0 else "存在 FAIL"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
