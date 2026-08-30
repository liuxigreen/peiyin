"""模式B（无视频）：中文字幕+中文配音音频 → 外语字幕+分句外语配音交付包。
B2槽位分析(t200) + B6交付包(t450)。设计见 MODE-B-DESIGN.md。
复用：seed-srt(落库) / translate五步链 / fit / render.write_srt+write_ass / QC。
TTS：模式B一期在云端无GPU时降级生成TTS任务清单（标注待GPU），小语种走Confucius4在线API。
"""
from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf


def _ffmpeg():
    import shutil
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def audio_slots(audio_path: str, entries: list[dict], out_dir: str) -> list[dict]:
    """B2：按SRT时间窗从整条中文配音音频切分每句参考音频。
    返回 [{uid, seq_index, start_ms, end_ms, ref_path, ref_duration_ms}]。"""
    os.makedirs(out_dir, exist_ok=True)
    data, sr = sf.read(audio_path, dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    total_ms = len(mono) / sr * 1000
    slots = []
    for i, e in enumerate(entries, 1):
        s = max(0, int(e["start_ms"] / 1000 * sr))
        en = min(len(mono), int(e["end_ms"] / 1000 * sr))
        seg = mono[s:en]
        ref = os.path.join(out_dir, f"zh_{i:04d}.wav")
        sf.write(ref, seg, sr)
        slots.append({"uid": e.get("uid", f"U{i:04d}"), "seq_index": i,
                      "start_ms": e["start_ms"], "end_ms": e["end_ms"],
                      "ref_path": ref,
                      "ref_duration_ms": int(len(seg) / sr * 1000),
                      "within_audio": e["end_ms"] <= total_ms + 500})
    return slots


def tts_clips_mock(slots: list[dict], target_lang: str, out_dir: str) -> list[dict]:
    """B4降级：无GPU/无Confucius4时生成占位音频（时长=槽位×0.85），并在manifest
    标注 engine=placeholder。真TTS接入后此函数被引擎调用替换。"""
    os.makedirs(out_dir, exist_ok=True)
    clips = []
    for s in slots:
        dur_ms = int((s["end_ms"] - s["start_ms"]) * 0.85)
        sr = 16000
        n = int(dur_ms / 1000 * sr)
        wav = (0.3 * np.sin(2 * np.pi * 300 * np.linspace(0, n / sr, n, endpoint=False))
               ).astype(np.float32)
        path = os.path.join(out_dir, f"dub_{s['seq_index']:04d}.wav")
        sf.write(path, wav, sr)
        clips.append({**s, "path": path, "final_ms": dur_ms, "speed": 1.0,
                      "engine": "placeholder"})
    return clips


def build_package(project: dict, entries: list[dict], translations: dict,
                  fitted: list[dict], out_dir: str, qc: dict) -> str:
    """B6：交付包。translations={uid: text}, fitted=[{seq_index,path,final_ms,speed}]"""
    os.makedirs(out_dir, exist_ok=True)
    from app.render import write_srt, write_ass
    sub_entries = []
    manifest = []
    for f in fitted:
        uid = f["uid"]
        text = translations.get(uid, "")
        sub_entries.append({"start_ms": f["start_ms"], "end_ms": f["start_ms"] + f["final_ms"],
                            "text": text})
        manifest.append({"uid": uid, "seq": f["seq_index"],
                         "start_ms": f["start_ms"], "final_ms": f["final_ms"],
                         "speed": f.get("speed", 1.0), "engine": f.get("engine", ""),
                         "text": text})
    srt = write_srt(sub_entries, os.path.join(out_dir, f"subtitles.{project['target_lang']}.srt"))
    ass = write_ass(sub_entries, os.path.join(out_dir, f"subtitles.{project['target_lang']}.ass"))
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"project": project["name"], "target_lang": project["target_lang"],
                   "clips": manifest}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "qc_report.json"), "w", encoding="utf-8") as f:
        json.dump(qc, f, ensure_ascii=False, indent=1)
    zip_path = os.path.join(out_dir, f"dubbing_package_{project['id'][:8]}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(srt, os.path.basename(srt))
        z.write(ass, os.path.basename(ass))
        z.write(os.path.join(out_dir, "manifest.json"), "manifest.json")
        z.write(os.path.join(out_dir, "qc_report.json"), "qc_report.json")
        for f in fitted:
            z.write(f["path"], f"audio/{f['seq_index']:04d}_{f['uid']}.wav")
    return zip_path
