"""B7音频后处理+韵律质检单测。"""
import math
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audio_post import (condition_line, ffprobe_ms, make_breath,  # noqa: E402
                            master_mix, make_room_tone)
from app.prosody_qc import (boosted_instruct, st_std_from_f0,  # noqa: E402
                            FLAT_ST_STD)


def _tone(path: str, ms: int = 800, freq: int = 220) -> str:
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={ms/1000:.3f}",
                    "-ar", "48000", path], capture_output=True, check=True)
    return path


def test_breath_generation(tmp_path):
    b = make_breath(str(tmp_path / "breath.wav"), ms=150)
    d = ffprobe_ms(b)
    assert 120 <= d <= 200


def test_condition_line_pads_duration(tmp_path):
    src = _tone(str(tmp_path / "src.wav"), ms=800)
    dst = str(tmp_path / "cond.wav")
    condition_line(src, dst, pre_ms=40, post_ms=140)
    d = ffprobe_ms(dst)
    assert 900 <= d <= 1100          # 800 + 40 + 140 ±编解码余量


def test_condition_line_with_breath(tmp_path):
    src = _tone(str(tmp_path / "src.wav"), ms=800)
    b = make_breath(str(tmp_path / "breath.wav"))
    dst = str(tmp_path / "cond_b.wav")
    condition_line(src, dst, breath=b)
    assert ffprobe_ms(dst) >= ffprobe_ms(src) + 150


def test_master_mix_duration_and_loudnorm(tmp_path):
    a = _tone(str(tmp_path / "a.wav"), ms=1000)
    b = _tone(str(tmp_path / "b.wav"), ms=1000, freq=330)
    bed = make_room_tone(str(tmp_path / "room.wav"), 4.0)
    out = str(tmp_path / "mix.wav")
    info = master_mix([{"path": a, "start_ms": 0},
                       {"path": b, "start_ms": 2000}],
                      out, total_ms=4000, work_dir=str(tmp_path))
    assert info["dur_ms"] >= 4000
    assert os.path.getsize(out) > 1000


def test_st_std_flat_vs_lively():
    flat = [220.0] * 50
    assert st_std_from_f0(flat) < 0.1
    lively = [220 * 2 ** (s / 12) for s in
              [0, 3, -2, 4, -3, 5, -1, 2, -4, 3, 1, -2] * 5]
    assert st_std_from_f0(lively) > FLAT_ST_STD


def test_boosted_instruct():
    assert "语调起伏" in boosted_instruct(None, None)
    out = boosted_instruct("用愤怒的语气说这句话", "暴怒")
    assert out.startswith("用愤怒的语气说这句话")
    assert "暴怒" in out
