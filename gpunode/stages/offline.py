"""O3: 离线联调假stage包——Mac节点（无GPU）领"gpu类"任务的轻量实现。
设计：与未来真stage同名注册（@register同task_type），由环境变量
NODE_MODE=offline|real 决定加载哪套。真stage(G0后)替换本文件加载即可。
产物路径约定：本地工作区 workdir/（真节点=对象存储key，接口一致）。
"""
from __future__ import annotations

import os
import time

import numpy as np
import soundfile as sf

from .router import register

WORKDIR = os.getenv("NODE_WORKDIR", os.path.join(os.path.dirname(__file__), "..", "workdir"))


def _wdir(task_id: str) -> str:
    d = os.path.join(WORKDIR, task_id[:8])
    os.makedirs(d, exist_ok=True)
    return d


def _ffmpeg():
    """gpunode独立于controlplane——ffmpeg从PATH或imageio_ffmpeg取。"""
    import shutil
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def subprocess_run(args):
    import subprocess
    return subprocess.run(args, capture_output=True, text=True)


@register("probe")
def probe(task):
    """T010真实现（本地就能跑）：ffprobe式读母片时长。无母片→默认。"""
    import json as _json
    src = (task.get("inputs") or {}).get("source_path", "")
    meta = {"duration_ms": 60000, "width": 1080, "height": 1920, "fps": 30,
            "has_audio": True, "mode": "offline-default"}
    if src and os.path.exists(src):
        proc = subprocess_run([_ffmpeg(), "-hide_banner", "-i", src])
        for line in proc.stderr.splitlines():
            if "Duration:" in line:
                t = line.split("Duration:")[1].split(",")[0].strip()
                hh, mm, ss = t.split(":")
                meta["duration_ms"] = int((int(hh) * 3600 + int(mm) * 60 + float(ss)) * 1000)
                break
    out = _wdir(task["id"])
    path = os.path.join(out, "probe.json")
    with open(path, "w") as f:
        _json.dump(meta, f, ensure_ascii=False)
    return [{"key": f"out/{task['id'][:8]}/probe.json", "path": path}]


@register("diarize")
def diarize(task):
    """T040假实现：固定2角色rttm（真=pyannote）。"""
    out = _wdir(task["id"])
    path = os.path.join(out, "diar.rttm")
    dur_ms = int((task.get("inputs") or {}).get("duration_ms", 60000))
    seg = max(dur_ms // 4, 2000)
    with open(path, "w") as f:
        t = 0
        i = 0
        while t < dur_ms:
            spk = "SPK_A" if i % 2 == 0 else "SPK_B"
            f.write(f"SPEAKER demo 1 {t/1000:.2f} {seg/1000:.2f} <NA> <NA> {spk} <NA>\n")
            t += seg
            i += 1
    return [{"key": f"out/{task['id'][:8]}/diar.rttm", "path": path,
             "speakers": ["SPK_A", "SPK_B"]}]


@register("separate")
def separate(task):
    """T120假实现：音频复制为vocal/bg双轨（真=Demucs）。"""
    out = _wdir(task["id"])
    src = (task.get("inputs") or {}).get("audio_path", "")
    sr = 44100
    if src and os.path.exists(src):
        data, sr = sf.read(src, dtype="float32", always_2d=True)
    else:
        data = np.zeros((sr * 10, 1), dtype=np.float32)   # 静音10s占位
    mono = data.mean(axis=1)
    v, b = os.path.join(out, "vocal.wav"), os.path.join(out, "bg.wav")
    sf.write(v, mono, sr)
    sf.write(b, mono * 0.5, sr)
    return [{"key": f"out/{task['id'][:8]}/vocal.wav", "path": v},
            {"key": f"out/{task['id'][:8]}/bg.wav", "path": b}]


@register("asr")
def asr(task):
    """T130假实现：读SRT种子回填（真=FunASR）。输入带utterances时原样输出。"""
    import json as _json
    out = _wdir(task["id"])
    utts = (task.get("inputs") or {}).get("utterances", [])
    result = [{"uid": u["uid"], "text": u.get("text", ""), "conf": 0.98}
              for u in utts]
    path = os.path.join(out, "asr.json")
    with open(path, "w") as f:
        _json.dump(result, f, ensure_ascii=False)
    return [{"key": f"out/{task['id'][:8]}/asr.json", "path": path}]


@register("tts")
def tts(task):
    """T310假实现：每句合成时长的正弦波wav（真=CosyVoice2/Confucius4）。
    inputs.utterances=[{uid,text,start_ms,end_ms}] → 每句一个wav。"""
    out = _wdir(task["id"])
    sr = 16000
    clips = []
    for i, u in enumerate((task.get("inputs") or {}).get("utterances", []), 1):
        dur_ms = max(u.get("end_ms", u.get("start_ms", 0) + 1000)
                     - u.get("start_ms", 0), 300)
        n = int(dur_ms / 1000 * sr * 0.8)          # 假TTS说快一点(0.8x)→测fit
        t = np.linspace(0, n / sr, n, endpoint=False)
        freq = 220 + (i % 4) * 60
        wav = (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        path = os.path.join(out, f"utt_{u.get('uid', i)}.wav")
        sf.write(path, wav, sr)
        clips.append({"uid": u.get("uid", str(i)), "path": path,
                      "expect_ms": dur_ms})
    return [{"key": f"out/{task['id'][:8]}/tts_clips.json", "path": "",
             "clips": clips}]


@register("subtitle-fast")
def subtitle_fast(task):
    """T110占位（离线）：真CPU实现O4接（OpenCV+羽化）。返回输入路径原样。"""
    src = (task.get("inputs") or {}).get("video_path", "")
    return [{"key": f"out/{task['id'][:8]}/wiped.mp4", "path": src}]
