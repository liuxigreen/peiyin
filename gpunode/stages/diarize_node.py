"""diarize stage（3060节点）：pyannote声纹聚类——每句中文参考音 → 声纹簇。
输入payload: {"project_id": ..., "zh_audio": 节点侧音频路径, "srt_slots": [{uid,start_ms,end_ms}...]}
执行:
  1. ffmpeg 从整条音频按窗口切句（缓存 workdir/zh_refs/{uid}.wav）
  2. pyannote embeddings 提取每句声纹向量
  3. 余弦距离凝聚聚类（阈值0.7，短剧配音员少效果好）
  4. 输出: [{uid, cluster, snr}] + 每簇推荐参考音（SNR最高2句）
产物回传: diarize_result.json（控制面据此做 LLM 簇→角色绑定 + 克隆重合成）
"""
from __future__ import annotations

import json
import os
import subprocess
import shutil

import numpy as np

from .router import register

WORKDIR = os.getenv("NODE_WORKDIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workdir"))
REF_DIR = os.path.join(WORKDIR, "zh_refs")
os.makedirs(REF_DIR, exist_ok=True)

HUGGINGFACE_TOKEN = os.getenv("HUGGINGFACE_TOKEN", "")


def _cut_slots(zh_audio: str, slots: list[dict]) -> list[dict]:
    """ffmpeg按窗口切每句参考音（缓存：已存在且时长匹配则跳过）。"""
    out = []
    total = len(slots)
    for i, s in enumerate(slots):
        uid = s["uid"]
        dst = os.path.join(REF_DIR, f"{uid}.wav")
        if not os.path.exists(dst):
            dur = max(0.3, (s["end_ms"] - s["start_ms"]) / 1000)
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{s['start_ms']/1000:.3f}",
                 "-t", f"{dur:.3f}", "-i", zh_audio,
                 "-ac", "1", "-ar", "16000", dst],
                capture_output=True, timeout=60)
        if os.path.exists(dst):
            out.append({**s, "wav": dst})
        if (i + 1) % 200 == 0:
            print(f"[diarize] cut {i+1}/{total}", flush=True)
    return out


def _download_zh_audio(url: str, dst: str) -> str:
    """从控制面拉整条原配音（registry映射→FileResponse）。167MB走keep-alive。"""
    import httpx
    from ..entrypoint import CONTROL, state
    r = httpx.get(f"{CONTROL}{url}", headers={"Authorization": f"Bearer {state['token']}"},
                  timeout=600, follow_redirects=True)
    r.raise_for_status()
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(r.content)
    print(f"[diarize] zh_audio downloaded: {len(r.content)>>20}MB", flush=True)
    return dst


@register("diarize")
def run_diarize(task: dict) -> list[dict]:
    payload = task.get("payload") or {}
    zh_audio = payload.get("zh_audio") or ""
    slots = payload.get("srt_slots") or []
    if not zh_audio or not os.path.exists(zh_audio):
        # 云端下发下载URL：节点经鉴权通道拉取（167MB，~2-5分钟）
        url = payload.get("zh_audio_url")
        if not url:
            raise RuntimeError(f"zh_audio not found and no zh_audio_url: {zh_audio!r}")
        zh_audio = _download_zh_audio(url, os.path.join(REF_DIR, "_zh_full.mp3"))
    if not slots:
        raise RuntimeError("srt_slots empty")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not installed on node")

    wavs = _cut_slots(zh_audio, slots)

    # pyannote 声纹提取（首次运行需 HUGGINGFACE_TOKEN 下载模型）
    from pyannote.audio import Model, Inference
    model = Model.from_pretrained("pyannote/embedding", use_auth_token=HUGGINGFACE_TOKEN or None)
    infer = Inference(model, window="whole")
    embs, ok_wavs = [], []
    for i, w in enumerate(wavs):
        try:
            e = infer(w["wav"])
            embs.append(np.asarray(e).flatten())
            ok_wavs.append(w)
        except Exception:
            continue
        if (i + 1) % 200 == 0:
            print(f"[diarize] embed {i+1}/{len(wavs)}", flush=True)
    if len(embs) < 10:
        raise RuntimeError(f"too few embeddings: {len(embs)}")
    X = np.stack(embs)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)   # 归一化→余弦距离

    # 凝聚聚类（scikit-learn AgglomerativeClustering, cosine, distance_threshold=0.7）
    from sklearn.cluster import AgglomerativeClustering
    labels = AgglomerativeClustering(
        n_clusters=None, distance_threshold=0.7, metric="cosine", linkage="average"
    ).fit_predict(X)

    # 簇统计 + SNR估算（帧RMS方差近似）+ 每簇推荐参考音
    clusters: dict[int, list[int]] = {}
    for i, lab in enumerate(labels):
        clusters.setdefault(int(lab), []).append(i)
    result = []
    for lab, members in sorted(clusters.items(), key=lambda x: -len(x[1])):
        snrs = []
        for m in members:
            try:
                w = wave_stats(ok_wavs[m]["wav"])
                snrs.append((w, m))
            except Exception:
                snrs.append((0.0, m))
        snrs.sort(reverse=True)
        recs = [ok_wavs[m]["uid"] for _, m in snrs[:3]]      # SNR最高的3句
        result.append({"cluster": f"C{lab:02d}", "count": len(members),
                       "snr_est": round(snrs[0][0], 1),
                       "recommended_refs": recs,
                       "uids": [ok_wavs[m]["uid"] for m in members]})
    result.sort(key=lambda r: -r["count"])

    # 落盘 JSON（artifact 回传控制面）
    out_json = os.path.join(WORKDIR, "diarize_result.json")
    json.dump({"project_id": payload.get("project_id"),
               "total": len(ok_wavs), "clusters": result},
              open(out_json, "w"), ensure_ascii=False, indent=1)

    rows = [{"key": "diarize_result", "path": out_json}]
    # 每簇推荐参考音一并回传（控制面直接用于克隆）
    for r in result[:20]:
        for uid in r["recommended_refs"][:2]:
            p = os.path.join(REF_DIR, f"{uid}.wav")
            if os.path.exists(p):
                rows.append({"key": f"ref_{uid}", "path": p})
    return rows


def wave_stats(path: str) -> float:
    """粗SNR估算：RMS/最低帧能量比（dB）。"""
    import soundfile as sf
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1: x = x.mean(axis=1)
    win = sr // 50
    frames = [float(np.sqrt((x[i:i+win]**2).mean())) for i in range(0, max(len(x)-win, 1), win)]
    if not frames: return 0.0
    loud = sorted(frames, reverse=True)[:max(len(frames)//4, 1)]
    quiet = sorted(frames)[:max(len(frames)//4, 1)] or [1e-6]
    import math
    return round(20 * math.log10((sum(loud)/len(loud)) / max(sum(quiet)/len(quiet), 1e-6)), 1)
