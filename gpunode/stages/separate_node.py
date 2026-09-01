"""人声分离 stage（3060 节点）：Demucs 2-stem (vocals/accompaniment)。
任务payload: {"audio_path": 节点侧音频路径, "out_dir?": 输出目录}
产物: [{key: vocals|accompaniment, path}]"""
from __future__ import annotations

import os
import subprocess
import shutil

from .router import register

WORKDIR = os.getenv("NODE_WORKDIR", os.path.join(os.path.dirname(__file__), "..", "workdir"))
OUTDIR = os.path.join(WORKDIR, "separated")
os.makedirs(OUTDIR, exist_ok=True)


@register("separate-vocals")
def run_separate(task: dict) -> list[dict]:
    payload = task.get("payload") or {}
    audio = payload.get("audio_path") or ""
    if not audio or not os.path.exists(audio):
        raise RuntimeError(f"audio_path not found on node: {audio}")
    model = payload.get("model", "htdemucs")
    out_dir = payload.get("out_dir") or os.path.join(OUTDIR, os.path.basename(audio).rsplit(".", 1)[0])
    os.makedirs(out_dir, exist_ok=True)

    # demucs CLI：-n htdemucs 默认输出 vocals.wav + no_vocals.wav
    cmd = [shutil.which("demucs") or "demucs", "-n", model,
           "--two-stems", "vocals", "-o", out_dir, audio]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"demucs failed: {proc.stderr[-300:]}")

    # demucs 输出结构: {out_dir}/{model}/{audio_basename}/vocals.wav & no_vocals.wav
    base = os.path.basename(audio).rsplit(".", 1)[0]
    stem_dir = os.path.join(out_dir, model, base)
    vocals = os.path.join(stem_dir, "vocals.wav")
    accomp = os.path.join(stem_dir, "no_vocals.wav")
    missing = [p for p in (vocals, accomp) if not os.path.exists(p)]
    if missing:
        raise RuntimeError(f"demucs output missing: {missing}")

    return [{"key": "vocals", "path": vocals},
            {"key": "accompaniment", "path": accomp},
            {"key": "model", "path": model}]
