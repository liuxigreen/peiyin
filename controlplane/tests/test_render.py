"""N3渲染链测试：字幕生成→配音轨预拼→混流编码→QC三查（全真ffmpeg，零mock）"""
import importlib
import os
import subprocess

import numpy as np
import soundfile as sf

from app.render import (build_dub_track, mux_video, qc_report, all_pass,
                        write_ass, write_srt, ffmpeg_bin)


def _make_wav(path, dur_s=1.0, sr=16000, freq=440):
    t = np.linspace(0, dur_s, int(dur_s * sr), endpoint=False)
    sf.write(path, (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32), sr)


def _make_video(path, dur_s=6.0, size="320x240", sr=44100):
    """ffmpeg合成测试视频：彩条+440Hz持续音（模拟原声，供混流替换）。"""
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=duration={dur_s}:size={size}:rate=15",
         "-f", "lavfi", "-i", f"sine=frequency=220:duration={dur_s}:sample_rate={sr}",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", path],
        check=True, capture_output=True)


def test_subtitles_writers(tmp_path):
    entries = [{"start_ms": 1000, "end_ms": 3000, "text": "Hello world"},
               {"start_ms": 4000, "end_ms": 6000, "text": "Second line"}]
    srt = write_srt(entries, str(tmp_path / "a.srt"))
    ass = write_ass(entries, str(tmp_path / "a.ass"))
    srt_text = open(srt, encoding="utf-8").read()
    ass_text = open(ass, encoding="utf-8").read()
    assert "00:00:01,000 --> 00:00:03,000" in srt_text
    assert "Dialogue: 0,0:00:01.00,0:00:03.00,Default" in ass_text
    assert "Hello world" in srt_text and "Second line" in ass_text


def test_dub_track_layout(tmp_path):
    """两句话音摆进6秒空白轨：时间码位置正确、非静音区间匹配。"""
    w1, w2 = str(tmp_path / "1.wav"), str(tmp_path / "2.wav")
    _make_wav(w1, 1.0, freq=440)
    _make_wav(w2, 1.0, freq=880)
    entries = [{"seq_index": 1, "start_ms": 500, "end_ms": 1500},
               {"seq_index": 2, "start_ms": 3000, "end_ms": 4000}]
    out = build_dub_track(entries, {1: w1, 2: w2}, 6000, str(tmp_path / "dub.wav"))
    data, sr = sf.read(out)
    assert len(data) == int(6 * sr)
    # 440Hz区间有能量、0-0.4s区间接近静音
    seg_on = data[int(0.6 * sr):int(0.9 * sr)]
    seg_off = data[int(0.05 * sr):int(0.3 * sr)]
    assert np.abs(seg_on).mean() > 0.05
    assert np.abs(seg_off).mean() < 0.01


def test_mux_and_qc_e2e(tmp_path):
    """全链：视频+配音轨→混流(带loudnorm+ASS)→QC三查通过。"""
    video = str(tmp_path / "v.mp4")
    _make_video(video, 6.0)
    w1, w2 = str(tmp_path / "1.wav"), str(tmp_path / "2.wav")
    _make_wav(w1, 1.2); _make_wav(w2, 1.2, freq=880)
    entries = [{"seq_index": 1, "start_ms": 500, "end_ms": 1700, "text": "Hello"},
               {"seq_index": 2, "start_ms": 3000, "end_ms": 4200, "text": "World"}]
    dub = build_dub_track(entries, {1: w1, 2: w2}, 6000, str(tmp_path / "dub.wav"))
    ass = write_ass(entries, str(tmp_path / "s.ass"), width=320, height=240,
                    band_center_y=180)
    out = mux_video(video, dub, str(tmp_path / "final.mp4"), ass_path=ass)
    assert os.path.getsize(out) > 10000
    rep = qc_report(out, expect_duration_ms=6000)
    assert rep["lufs"] is not None, rep
    assert rep["duration_pass"], rep
    assert rep["unexpected_silences"] == 0, rep
    assert all_pass(rep), rep


def test_mux_no_ass(tmp_path):
    """不带字幕的纯混流路径（ASS烧录可选）。"""
    video = str(tmp_path / "v.mp4")
    _make_video(video, 3.0)
    wav = str(tmp_path / "d.wav")
    _make_wav(wav, 3.0, freq=300)
    out = mux_video(video, wav, str(tmp_path / "f.mp4"), loudnorm=False)
    assert os.path.getsize(out) > 5000
