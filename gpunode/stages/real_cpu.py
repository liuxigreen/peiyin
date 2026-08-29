"""O4: T110字幕擦除(fast=CPU) + T320 fit-timeline 真实现。
擦字幕：OpenCV白像素定位(视频底部45%区域)→框内中值模糊+羽化边缘混合→写回。
       （quality=ProPainter路线二期，接口一致 TASK subtitle-quality）
fit：读取TTS实际时长 vs 原时间窗，ratio>1.0→ffmpeg atempo加速(0.5-2.0)；
     加速后新时长回填输出manifest（字幕跟配音走原则）。
"""
from __future__ import annotations

import os
import subprocess

import numpy as np


def _ffmpeg():
    import shutil
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args, timeout=1800):
    assert all(a is not None for a in args), f"ffmpeg args含None: {args[:3]}"
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-300:]}")
    return proc


def wipe_subtitles(video_path: str, out_path: str, band_top_ratio: float = 0.55,
                   band_bottom_ratio: float = 0.95, white_thresh: int = 215,
                   blur_sigma: int = 31) -> str:
    """擦除字幕带：检测带内白像素行范围→该区域强模糊（帧级处理，速度换简洁）。
    OpenCV缺失时降级为整带boxblur（ffmpeg滤镜，零依赖）。"""
    try:
        import cv2
    except ImportError:
        return _wipe_ffmpeg_only(video_path, out_path, band_top_ratio,
                                 band_bottom_ratio, blur_sigma)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video_path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    y0, y1 = int(h * band_top_ratio), int(h * band_bottom_ratio)
    tmp = out_path + ".tmp.mp4"
    vw = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        band = frame[y0:y1]
        gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
        rows = np.where((gray > white_thresh).sum(axis=1) > w * 0.02)[0]
        if len(rows):
            top, bot = max(y0 + rows.min() - 6, y0), min(y0 + rows.max() + 6, y1)
            region = frame[top:bot]
            frame[top:bot] = cv2.GaussianBlur(region, (0, 0), blur_sigma)
        vw.write(frame)
    cap.release()
    vw.release()
    # mp4v容器转h264+aac
    _run([_ffmpeg(), "-i", tmp, "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "copy", "-y", out_path])
    os.remove(tmp)
    return out_path


def _wipe_ffmpeg_only(video_path: str, out_path: str, top_r: float, bot_r: float,
                      sigma: int) -> str:
    """无OpenCV降级：ffmpeg boxblur整个字幕带（每帧都处理，离线可接受）。"""
    w, h = _probe_size(video_path)
    y0, y1 = int(h * top_r), int(h * bot_r)
    bh = y1 - y0
    _run([_ffmpeg(), "-i", video_path,
          "-filter_complex",
          (f"[0:v]split[a][b];[b]crop=w={w}:h={bh}:x=0:y={y0},"
           f"boxblur=15:3[blur];[a][blur]overlay=x=0:y={y0}"),
          "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "copy",
          "-y", out_path])
    return out_path


def _probe_size(video_path: str):
    """用ffmpeg读分辨率（避免强依赖cv2）。"""
    proc = subprocess.run([_ffmpeg(), "-hide_banner", "-i", video_path],
                          capture_output=True, text=True)
    import re
    for line in proc.stderr.splitlines():
        if "Video:" in line:
            m = re.search(r"(\d{3,5})x(\d{3,5})", line)
            if m:
                return int(m.group(1)), int(m.group(2))
    return 1080, 1920


def fit_timeline(clips: list[dict], out_dir: str) -> list[dict]:
    """T320：clips=[{uid,path,start_ms,end_ms,expect_ms}]→每句检查实际时长，
    超窗0.95-1.05外→atempo变速（0.5-2.0区间），输出[{...,final_ms,speed}]。"""
    os.makedirs(out_dir, exist_ok=True)
    import soundfile as sf
    out = []
    for c in clips:
        path = c["path"]
        info = sf.info(path)
        cur_ms = info.frames / info.samplerate * 1000
        window = c.get("end_ms", 0) - c.get("start_ms", 0)
        speed = 1.0
        final_path = path
        if window > 0:
            ratio = cur_ms / window
            if ratio > 1.05 or ratio < 0.95:
                speed = min(2.0, max(0.5, ratio))     # 加速向（TTS偏长的主场景）
                final_path = os.path.join(out_dir, f"fit_{c['uid']}.wav")
                _run([_ffmpeg(), "-i", path, "-filter:a",
                      f"atempo={speed:.4f}", "-y", final_path])
                info2 = sf.info(final_path)
                cur_ms = info2.frames / info2.samplerate * 1000
        out.append({**c, "path": final_path, "final_ms": int(cur_ms),
                    "speed": round(speed, 3)})
    return out
