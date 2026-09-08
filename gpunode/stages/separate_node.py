"""人声分离 stage（3060 节点）：Demucs 2-stem (vocals/accompaniment)。
任务payload: {"audio_path": 节点侧音频路径, "out_dir?": 输出目录}
产物: [{key: vocals|accompaniment, path}]
v1.4.1 修复（0908）：
- WinError 2 根因：demucs 解码 mp3 输入时内部调 ffmpeg 子进程，但节点任务进程
  PATH 没有 ffmpeg → 先预转 wav（绝对路径 ffmpeg），demucs 只读 wav（走 soundfile 无依赖）
- PATH 注入 demucs 子进程 env 双保险
"""
from __future__ import annotations

import os
import sys
import subprocess
import shutil

from .router import register

WORKDIR = os.getenv("NODE_WORKDIR", os.path.join(os.path.dirname(__file__), "..", "workdir"))
OUTDIR = os.path.join(WORKDIR, "separated")
os.makedirs(OUTDIR, exist_ok=True)

# 候选 ffmpeg 路径（按序探测）：venv 同级 → winget Links（节点实测路径）→ PATH
_FFMPEG_CANDIDATES = [
    lambda: os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"),
    lambda: os.path.join(os.path.dirname(sys.executable), "ffmpeg"),
    lambda: os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    lambda: r"C:\Users\LENOBO\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe",
    lambda: shutil.which("ffmpeg"),
]


def _find_ffmpeg() -> str | None:
    for probe in _FFMPEG_CANDIDATES:
        try:
            p = probe()
            if p and os.path.exists(p):
                return p
        except Exception:
            continue
    return None


@register("separate-vocals")
def run_separate(task: dict) -> list[dict]:
    payload = task.get("payload") or {}
    audio = payload.get("audio_path") or ""
    if not audio or not os.path.exists(audio):
        raise RuntimeError(f"audio_path not found on node: {audio}")
    model = payload.get("model", "htdemucs")
    out_dir = payload.get("out_dir") or os.path.join(OUTDIR, os.path.basename(audio).rsplit(".", 1)[0])
    os.makedirs(out_dir, exist_ok=True)

    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found on node (checked venv, winget Links, PATH)")
    # 给子进程注入 PATH（demucs 内部对非 wav 的 ffmpeg 调用、以及 Windows DLL 搜索都受益）
    child_env = dict(os.environ)
    child_env["PATH"] = os.path.dirname(ffmpeg) + os.pathsep + child_env.get("PATH", "")

    # mp3/其它格式 → 先转 wav：demucs 读 wav 走 soundfile，不再触发内部 ffmpeg 子进程
    src = audio
    if not audio.lower().endswith(".wav"):
        wav_src = os.path.join(out_dir, "_src.wav")
        r = subprocess.run(
            [ffmpeg, "-y", "-i", audio, "-ar", "44100", "-ac", "2", wav_src],
            capture_output=True, text=True, timeout=1800, env=child_env)
        if r.returncode != 0 or not os.path.exists(wav_src):
            raise RuntimeError(f"ffmpeg convert failed: {r.stderr[-300:]}")
        src = wav_src

    exe = shutil.which("demucs") or os.path.join(os.path.dirname(sys.executable), "demucs.exe")
    if not os.path.exists(exe):
        raise RuntimeError(f"demucs not found: {exe}")
    cmd = [exe, "-n", model, "--two-stems", "vocals", "-o", out_dir, src]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200, env=child_env)
    if proc.returncode != 0:
        raise RuntimeError(f"demucs failed: {proc.stderr[-400:]}")

    # demucs 输出结构: {out_dir}/{model}/{audio_basename}/vocals.wav & no_vocals.wav
    base = os.path.basename(src).rsplit(".", 1)[0]
    stem_dir = os.path.join(out_dir, model, base)
    # 兼容输入被改名的情况（_src.wav → 用源文件名目录）
    if not os.path.isdir(stem_dir) and src != audio:
        stem_dir = os.path.join(out_dir, model, os.path.basename(audio).rsplit(".", 1)[0])
    vocals = os.path.join(stem_dir, "vocals.wav")
    accomp = os.path.join(stem_dir, "no_vocals.wav")
    missing = [p for p in (vocals, accomp) if not os.path.exists(p)]
    if missing:
        # 递归找一次真实输出
        found = []
        for root, _, files in os.walk(os.path.join(out_dir, model)):
            for f in files:
                if f in ("vocals.wav", "no_vocals.wav"):
                    found.append(os.path.join(root, f))
        if len(found) >= 2:
            vocals = next(p for p in found if p.endswith("vocals.wav"))
            accomp = next(p for p in found if p.endswith("no_vocals.wav"))
        else:
            raise RuntimeError(f"demucs output missing: {missing}; walked={found}")
    return [{"key": "vocals", "path": vocals},
            {"key": "accompaniment", "path": accomp}]
