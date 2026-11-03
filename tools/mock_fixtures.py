"""mock_fixtures —— 自测与错误注入共用的真实 MP3 素材（内嵌 base64，零外部依赖）。"""

from __future__ import annotations

import base64
import os
import tempfile

from mock_core import Mp3Frame, parse_mp3_frames

# 自测用的真实 MP3：lameenc 24k/单声道/32kbps，取自 tools/今天天气如何.wav 前 0.48s
# （22 帧 × 24ms = 528ms / 2112 B）。嵌入脚本是为了 `--self-test` 不依赖 lameenc/av：
# 帧数/字节/帧头/节奏断言直接跑在真实码流上，而不是脚本自己造的"假 MP3"。
SELFTEST_MP3_FRAMES = 22
SELFTEST_MP3_BYTES = 2112
SELFTEST_MP3_B64 = (
    "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"
    "qqqqqqqqqqqqqqqq"
)


def embedded_real_mp3_bytes() -> bytes:
    """内嵌的真实 MP3 字节（lameenc 24k/单声道/32kbps；`--self-test` 与 T3.6 报错注入共用）。"""
    return base64.b64decode(SELFTEST_MP3_B64)


def embedded_real_mp3_frames() -> list[Mp3Frame]:
    """内嵌真实 MP3 切帧结果：--error-stage after-tts-start 在没给 --tts-mp3 时用它下发。"""
    return parse_mp3_frames(embedded_real_mp3_bytes(), source="<内嵌真实 MP3>")


def write_selftest_mp3() -> str:
    directory = tempfile.mkdtemp(prefix="ws_mock_selftest_")
    path = os.path.join(directory, "selftest_24k_mono.mp3")
    with open(path, "wb") as handle:
        handle.write(embedded_real_mp3_bytes())
    return path
