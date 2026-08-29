"""N3 渲染链：字幕生成(T420) + 配音轨合成(T330) + 混流编码 + QC(T430)。
纯 ffmpeg/Python，控制面 CPU 池执行（V3 §7.2 定案）。
- ffmpeg 二进制来自 imageio_ffmpeg 静态构建（requirements 已加，零系统依赖）
- ASS 置原字幕带中心（video-dubbing-pipeline skill 教训：字号帧高5-6%、MarginV=高-带中心Y）
- 配音轨 Python 预拼（numpy 混音，防 ffmpeg open-files 教训）
- QC 三查：LUFS(ebur128)、意外静音>3s、时长一致
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf


def ffmpeg_bin() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-400:]}")
    return proc


# ── T420 字幕生成 ───────────────────────────────────────────
def _ts(ms: int, ass: bool = False) -> str:
    h, rem = divmod(max(0, ms), 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    if ass:
        return f"{h}:{m:02d}:{s:02d}.{ms2 // 10:02d}"
    return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"


def write_srt(entries: list[dict], path: str) -> str:
    """entries: [{start_ms,end_ms,text}]"""
    with open(path, "w", encoding="utf-8") as f:
        for i, e in enumerate(entries, 1):
            f.write(f"{i}\n{_ts(e['start_ms'])} --> {_ts(e['end_ms'])}\n{e['text']}\n\n")
    return path


def write_ass(entries: list[dict], path: str, width: int = 1080, height: int = 1920,
              band_center_y: int | None = None) -> str:
    """ASS 置原字幕带中心（默认高度78%处≈短剧字幕位），字号=帧高6%。"""
    fs = max(36, int(height * 0.06))
    margin_v = height - (band_center_y or int(height * 0.78)) + fs // 2
    header = (
        "[Script Info]\nTitle: Dubbed Subtitles\nScriptType: v4.00+\n"
        f"WrapStyle: 0\nPlayResX: {width}\nPlayResY: {height}\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{fs},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,"
        f"100,100,0,0,1,4,0,2,20,20,{margin_v},1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        for e in entries:
            text = e["text"].replace("\n", "\\N")
            f.write(f"Dialogue: 0,{_ts(e['start_ms'], True)},{_ts(e['end_ms'], True)},"
                    f"Default,,0,0,0,,{text}\n")
    return path


# ── T330 配音轨合成（Python预拼）────────────────────────────
def build_dub_track(entries: list[dict], audio_paths: dict[int, str],
                    total_ms: int, out_path: str, bg_path: str | None = None,
                    bg_gain: float = 0.30, sr: int = 44100) -> str:
    """把每句TTS音频按时间码摆进空白轨：audio_paths={seq_index: wav路径}。
    bg_path 提供时混入背景音（Demucs伴奏）。输出 wav。"""
    n = int(round(total_ms / 1000 * sr))
    track = np.zeros(n, dtype=np.float32)
    for e in entries:
        wav = audio_paths.get(e["seq_index"])
        if not wav or not os.path.exists(wav):
            continue
        data, file_sr = sf.read(wav, dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
        if file_sr != sr:
            # 线性插值重采样（句级短音频，质量足够且零依赖）
            t_old = np.linspace(0.0, 1.0, len(mono), endpoint=False)
            t_new = np.linspace(0.0, 1.0, int(len(mono) * sr / file_sr), endpoint=False)
            mono = np.interp(t_new, t_old, mono).astype(np.float32)
        start = int(e["start_ms"] / 1000 * sr)
        end = min(start + len(mono), n)
        if end > start:
            track[start:end] += mono[: end - start]
    if bg_path and os.path.exists(bg_path):
        bg, bsr = sf.read(bg_path, dtype="float32", always_2d=True)
        bg = bg.mean(axis=1)
        if bsr != sr:
            t_old = np.linspace(0.0, 1.0, len(bg), endpoint=False)
            t_new = np.linspace(0.0, 1.0, int(len(bg) * sr / bsr), endpoint=False)
            bg = np.interp(t_new, t_old, bg).astype(np.float32)
        m = min(len(track), len(bg))
        track[:m] += bg[:m] * bg_gain
    peak = float(np.max(np.abs(track))) if len(track) else 0.0
    if peak > 0.99:
        track *= 0.99 / peak
    sf.write(out_path, track, sr, subtype="PCM_16")
    return out_path


# ── T340/合成 混流编码 ──────────────────────────────────────
def loudnorm_measure(dub_audio: str) -> dict:
    """两遍loudnorm第一遍：测量 input_i/input_tp/input_lra/input_thresh。"""
    ff = ffmpeg_bin()
    proc = subprocess.run(
        [ff, "-hide_banner", "-nostats", "-i", dub_audio,
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
         "-f", "null", "-"], capture_output=True, text=True, timeout=600)
    import json as _json
    txt = proc.stderr
    start = txt.rfind("{")
    end = txt.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"loudnorm measure failed: {txt[-300:]}")
    return _json.loads(txt[start:end + 1])


def mux_video(video: str, dub_audio: str, out_path: str, ass_path: str | None = None,
              loudnorm: bool = True, vcodec: str = "libx264", crf: int = 20) -> str:
    """成片混流：配音轨替换原音（loudnorm两遍法精确到-16LUFS），可选ASS烧录。
    音频比视频长时以视频为准截断（-shortest）。"""
    af = None
    if loudnorm:
        m = loudnorm_measure(dub_audio)
        try:
            af = ("loudnorm=I=-16:TP=-1.5:LRA=11"
                  f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                  f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                  f":offset={m.get('target_offset', '0')}:linear=true")
        except KeyError:
            af = "loudnorm=I=-16:TP=-1.5:LRA=11"   # 测量缺字段→退单遍动态
    args = ["-i", video, "-i", dub_audio]
    vf = []
    if ass_path:
        ass_esc = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        vf.append(f"ass={ass_esc}")
    if vf:
        args += ["-vf", ",".join(vf)]
    args += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", vcodec, "-crf", str(crf),
             "-preset", "fast", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", "-shortest"]
    if af:
        args += ["-af", af]
    args.append(out_path)
    _run(args, timeout=1800)
    return out_path


# ── T430 QC 三查 ────────────────────────────────────────────
def qc_report(media_path: str, expect_duration_ms: int | None = None) -> dict:
    """三查：①LUFS(ebur128 integrated) ②意外静音>3s计数 ③时长与预期差。"""
    ff = ffmpeg_bin()
    # ①响度
    proc = subprocess.run(
        [ff, "-hide_banner", "-nostats", "-i", media_path,
         "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800)
    lufs = None
    for line in proc.stderr.splitlines():
        if "I:" in line and "LUFS" in line:
            try:
                lufs = float(line.split("I:")[1].split("LUFS")[0].strip())
            except ValueError:
                pass
    # ②静音检测（ silencedetect 噪声门-40dB / 最短3s ）
    proc2 = subprocess.run(
        [ff, "-hide_banner", "-nostats", "-i", media_path,
         "-af", "silencedetect=noise=-40dB:d=3", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800)
    silences = [l for l in proc2.stderr.splitlines() if "silence_start" in l]
    # ③时长
    proc3 = subprocess.run(
        [ff, "-hide_banner", "-i", media_path, "-f", "null", "-"],
        capture_output=True, text=True, timeout=600)
    dur_ms = None
    for line in proc3.stderr.splitlines():
        if "time=" in line:
            try:
                t = line.split("time=")[1].split(" ")[0]
                hh, mm, ss = t.split(":")
                dur_ms = int((int(hh) * 3600 + int(mm) * 60 + float(ss)) * 1000)
            except Exception:
                pass
    drift = None
    if expect_duration_ms and dur_ms:
        drift = round(abs(dur_ms - expect_duration_ms) / 1000, 2)
    return {
        "lufs": lufs,
        "lufs_pass": lufs is not None and abs(lufs + 16) <= 1.5,
        "unexpected_silences": len(silences),
        "silence_pass": len(silences) == 0,
        "duration_ms": dur_ms,
        "duration_drift_s": drift,
        "duration_pass": drift is None or drift < 2.0,
    }


def all_pass(report: dict) -> bool:
    return bool(report.get("lufs_pass") and report.get("silence_pass")
                and report.get("duration_pass"))
