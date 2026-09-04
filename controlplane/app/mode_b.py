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
    slots = []
    # 流式切片：seek逐句读（每句~2MB），整条读入2小时音频=3.2GB→OOM(0903事故)
    with sf.SoundFile(audio_path) as f:
        sr = f.samplerate
        total_frames = len(f)
        total_ms = total_frames / sr * 1000
        for i, e in enumerate(entries, 1):
            s = max(0, int(e["start_ms"] / 1000 * sr))
            en = min(total_frames, int(e["end_ms"] / 1000 * sr))
            f.seek(s)
            seg = f.read(frames=max(en - s, 0), dtype="float32", always_2d=True)
            mono = seg.mean(axis=1) if seg.size else np.zeros(0, dtype="float32")
            ref = os.path.join(out_dir, f"zh_{i:04d}.wav")
            sf.write(ref, mono, sr)
            slots.append({"uid": e.get("uid", f"U{i:04d}"), "seq_index": i,
                          "start_ms": e["start_ms"], "end_ms": e["end_ms"],
                          "ref_path": ref,
                          "ref_duration_ms": int(len(mono) / sr * 1000),
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


def fit_clip(src_path: str, dst_dir: str, uid: str,
             window_ms: int, max_stretch: float = 1.3,
             tts_rate: float = 1.0, max_total_speed: float = 1.5) -> dict:
    """T320时长匹配：atempo 补偿到窗口内。
    防听不清保护：合成语速(tts_rate)×atempo 的联合加速上限 max_total_speed（默认1.5）——
    达到上限仍超窗→部分补偿并标记 over_window（进qc人工/压缩重译），
    绝不无上限加速把配音变成 chipmunk。返回 {path, final_ms, speed, tts_rate, over_window}。"""
    try:
        info = sf.info(src_path)
        final_ms = int(info.frames / info.samplerate * 1000)
    except Exception as e:                                   # noqa: BLE001
        # 坏文件（0字节/截断）：如实标记，不让单句炸掉整包
        return {"path": src_path, "final_ms": 0, "speed": 1.0,
                "tts_rate": tts_rate, "over_window": True, "corrupt": str(e)[:80]}
    out = {"path": src_path, "final_ms": final_ms, "speed": 1.0,
           "tts_rate": tts_rate, "over_window": False}
    if window_ms <= 0:
        return out
    ratio = final_ms / window_ms
    if ratio <= 1.15:
        return out
    headroom = max_total_speed / max(tts_rate, 0.1)   # 剩余允许的atempo空间
    cap = min(max_stretch, headroom)
    if cap <= 1.0:
        out["over_window"] = True
        return out
    try:
        speed = round(min(ratio + 0.02, cap), 3)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, f"fit_{uid}.wav")
        subprocess.run([_ffmpeg(), "-y", "-i", src_path, "-filter:a",
                        f"atempo={speed}", dst],
                       capture_output=True, timeout=120, check=True)
        info2 = sf.info(dst)
        final2 = int(info2.frames / info2.samplerate * 1000)
        out.update(path=dst, final_ms=final2, speed=speed)
        if final2 > window_ms * 1.15:            # 到顶仍超窗：如实标记
            out["over_window"] = True
    except Exception:                                        # noqa: BLE001
        out["over_window"] = True
    return out


def build_package_from_clips(project: dict, rows: list[dict], out_dir: str,
                             post: bool = True, me_path: str | None = None) -> str:
    """B6真TTS交付包（区别于mock的build_package）：
    rows=[{uid,seq,start_ms,end_ms,text,audio_path,final_ms,speed,engine,
           over_window,over_limit}]。
    字幕时间码=原slot窗口（字幕跟源时间轴走）；音频按 uid 命名；
    无产物句进qc missing清单。返回zip路径。"""
    os.makedirs(out_dir, exist_ok=True)
    from app.render import write_srt, write_ass
    sub_entries, manifest, missing, over_win, over_limit = [], [], [], [], []
    for r in rows:
        if not r.get("audio_path"):
            missing.append({"uid": r["uid"], "seq": r["seq"]})
            continue
        if r.get("over_window"):
            over_win.append(r["uid"])
        if r.get("over_limit"):
            over_limit.append(r["uid"])
        sub_entries.append({"start_ms": r["start_ms"], "end_ms": r["end_ms"],
                            "text": r["text"]})
        manifest.append({"uid": r["uid"], "seq": r["seq"],
                         "start_ms": r["start_ms"],
                         "window_ms": (r["end_ms"] or 0) - (r["start_ms"] or 0),
                         "final_ms": r.get("final_ms"),
                         "speed": r.get("speed", 1.0),
                         "tts_rate": r.get("tts_rate", 1.0),
                         "total_speed": round(r.get("speed", 1.0) * r.get("tts_rate", 1.0), 3),
                         "engine": r.get("engine", ""),
                         "over_window": bool(r.get("over_window")),
                         "text": r["text"]})
    qc = {"clips": len(manifest), "missing": missing, "over_window": over_win,
          "over_limit": over_limit}
    srt = write_srt(sub_entries,
                    os.path.join(out_dir, f"subtitles.{project['target_lang']}.srt"))
    ass = write_ass(sub_entries,
                    os.path.join(out_dir, f"subtitles.{project['target_lang']}.ass"))
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"project": project["name"], "target_lang": project["target_lang"],
                   "clips": manifest}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "qc_report.json"), "w", encoding="utf-8") as f:
        json.dump(qc, f, ensure_ascii=False, indent=1)
    # B7句级后处理：手机扬声器EQ+压缩限幅+边界padding(+说话人切换呼吸声)
    post_errors, master_path = [], None
    if post:
        from app.audio_post import condition_line, make_breath, master_mix
        aud_dir = os.path.join(out_dir, "audio")
        os.makedirs(aud_dir, exist_ok=True)
        breath = make_breath(os.path.join(out_dir, "breath.wav"))
        kept = [r for r in rows if r.get("audio_path")]
        for r in kept:
            try:
                cond = condition_line(
                    r["audio_path"],
                    os.path.join(aud_dir, f"{r['seq']:04d}_{r['uid']}.wav"),
                    breath=breath if r.get("breath") else None)
                r["audio_path"] = cond
            except Exception as e:                               # noqa: BLE001
                post_errors.append({"uid": r["uid"], "err": str(e)[:120]})
        if kept:
            total_ms = max((r.get("end_ms") or 0) for r in kept) + 2000
            master_path = os.path.join(out_dir, "master_13LUFS.wav")
            try:
                mix_info = master_mix(
                    [{"path": r["audio_path"], "start_ms": r["start_ms"] or 0}
                     for r in kept],
                    master_path, total_ms, me_path=me_path, work_dir=out_dir)
                qc["master_mix"] = {"dur_ms": mix_info["dur_ms"],
                                    "loudnorm": "I=-13 TP=-1 LRA=6"}
            except Exception as e:                               # noqa: BLE001
                post_errors.append({"uid": "MIX", "err": str(e)[:160]})
                master_path = None
    qc["post_errors"] = post_errors
    with open(os.path.join(out_dir, "qc_report.json"), "w", encoding="utf-8") as f:
        json.dump(qc, f, ensure_ascii=False, indent=1)
    zip_path = os.path.join(out_dir, f"dubbing_package_{project['id'][:8]}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(srt, os.path.basename(srt))
        z.write(ass, os.path.basename(ass))
        z.write(os.path.join(out_dir, "manifest.json"), "manifest.json")
        z.write(os.path.join(out_dir, "qc_report.json"), "qc_report.json")
        if master_path and os.path.exists(master_path):
            z.write(master_path, "master_13LUFS.wav")
        for r in rows:
            if r.get("audio_path"):
                z.write(r["audio_path"], f"audio/{r['seq']:04d}_{r['uid']}.wav")
    return zip_path


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
