"""简化克隆链（0906用户拍板）：不需要pyannote。
LLM文本绑定已给每句speaker → 每角色选最清晰的3句切片当参考音 → CosyVoice直接克隆。
选取标准: 时长2-8s + 能量RMS最高（=最干净最响）。
"""
import os
import subprocess
import wave


def pick_refs_per_speaker(slots: list[dict], ref_dir: str,
                          top_n: int = 3) -> dict:
    """slots=[{uid,speaker_id,wav_path}] → {speaker_id: [wav_path,...]}"""
    import struct
    by_spk: dict[str, list[dict]] = {}
    for s in slots:
        spk = s.get("speaker_id")
        p = s.get("wav_path")
        if not spk or not p or not os.path.exists(p):
            continue
        try:
            w = wave.open(p)
            dur = w.getnframes() / w.getframerate()
            w.close()
        except Exception:
            continue
        if not (1.0 <= dur <= 10.0):
            continue
        by_spk.setdefault(spk, []).append({**s, "dur": dur})

    out = {}
    for spk, items in by_spk.items():
        # RMS响度估算(粗)：文件大小/时长≈平均码率能量
        items.sort(key=lambda x: os.path.getsize(x["wav_path"]) / x["dur"],
                   reverse=True)
        out[spk] = [x["wav_path"] for x in items[:top_n]]
    return out
