"""diarize stage v2（3060节点）：speechbrain ECAPA 声纹 + 凝聚聚类。
替代 pyannote（HF 门控模型在节点下载不通）。ECAPA 公开模型，走 hf-mirror。
输入payload: {"zh_audio": 节点侧音频路径, "srt_slots": [{uid,start_ms,end_ms}...]}
输出: diarize_result.json [{cluster, count, snr_est, recommended_refs, uids}]
"""
from __future__ import annotations

import json
import os
import sys
import subprocess
import shutil
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

from ..model_inventory import ecapa_preflight
from .router import register

WORKDIR = os.getenv("NODE_WORKDIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workdir"))
REF_DIR = os.path.join(WORKDIR, "zh_refs")
CONTROL = os.getenv("CONTROL_URL", "http://localhost:8500").rstrip("/")
MAX_ZH_AUDIO_BYTES = int(os.getenv("NODE_ZH_AUDIO_MAX_BYTES", str(2 << 30)))

_HF_CANDIDATES = [
    lambda: os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"),
    lambda: shutil.which("ffmpeg"),
]


TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "workdir", "node_token.txt")


def _node_token() -> str:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as token_file:
            return token_file.read().strip()
    except OSError:
        return ""


def _download_zh_audio(local_path: str, audio_url: str) -> str:
    """从控制面下载原配音到节点本地缓存。"""
    if not audio_url:
        raise RuntimeError(f"zh_audio not found on node: {local_path}; zh_audio_url missing")
    parsed = urllib.parse.urlparse(audio_url)
    if parsed.scheme or parsed.netloc:
        raise RuntimeError(f"zh_audio_url must be control-relative: {audio_url}")
    token = _node_token()
    if not token:
        raise RuntimeError("node token missing; cannot download zh_audio_url")

    if not local_path:
        name = os.path.basename(parsed.path) or "zh_audio.mp3"
        local_path = os.path.join(WORKDIR, name)
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    tmp_path = local_path + ".part"
    request = urllib.request.Request(
        CONTROL.rstrip("/") + "/" + audio_url.lstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status >= 400:
                raise RuntimeError(f"zh_audio download HTTP {status}: {audio_url}")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_ZH_AUDIO_BYTES:
                raise RuntimeError(f"zh_audio download exceeds {MAX_ZH_AUDIO_BYTES} bytes")
            written = 0
            with open(tmp_path, "wb") as audio_file:
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_ZH_AUDIO_BYTES:
                        raise RuntimeError(f"zh_audio download exceeds {MAX_ZH_AUDIO_BYTES} bytes")
                    audio_file.write(chunk)
        if not written:
            raise RuntimeError(f"zh_audio download was empty: {audio_url}")
        os.replace(tmp_path, local_path)
        return local_path
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"zh_audio download HTTP {exc.code}: {audio_url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"zh_audio download failed: {exc.reason}") from exc
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _exe(name: str) -> str:
    cand = os.path.join(os.path.dirname(sys.executable), name + ".exe")
    if os.path.exists(cand):
        return cand
    return shutil.which(name) or name


def _cut_slots(zh_audio: str, slots: list[dict]) -> list[dict]:
    out = []
    os.makedirs(REF_DIR, exist_ok=True)
    ff = _exe("ffmpeg")
    total = len(slots)
    for i, s in enumerate(slots):
        uid = s["uid"]
        dst = os.path.join(REF_DIR, f"{uid}.wav")
        if not os.path.exists(dst) or os.path.getsize(dst) < 1000:
            dur = max(0.3, (s["end_ms"] - s["start_ms"]) / 1000)
            subprocess.run(
                [ff, "-y", "-ss", f"{s['start_ms']/1000:.3f}",
                 "-t", f"{dur:.3f}", "-i", zh_audio,
                 "-ac", "1", "-ar", "16000", dst],
                capture_output=True, timeout=60)
        if os.path.exists(dst) and os.path.getsize(dst) > 1000:
            out.append({**s, "wav": dst})
        if (i + 1) % 150 == 0:
            print(f"[diarize] cut {i+1}/{total}", flush=True)
    return out


def _snr(path: str) -> float:
    import soundfile as sf
    import math
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1: x = x.mean(axis=1)
    win = sr // 50
    frames = [float(np.sqrt((x[i:i+win]**2).mean())) for i in range(0, max(len(x)-win, 1), win)]
    if not frames: return 0.0
    loud = sorted(frames, reverse=True)[:max(len(frames)//4, 1)]
    quiet = sorted(frames)[:max(len(frames)//4, 1)] or [1e-6]
    return round(20 * math.log10((sum(loud)/len(loud)) / max(sum(quiet)/len(quiet), 1e-6)), 1)


@register("diarize")
def run_diarize(task: dict) -> list[dict]:
    payload = task.get("payload")
    if payload is None or payload == {}:
        output_paths = task.get("output_paths")
        output_payload = (
            output_paths.get("payload")
            if isinstance(output_paths, dict) else None
        )
        payload = output_payload if isinstance(output_payload, dict) else {}
    zh_audio = payload.get("zh_audio") or ""
    slots = payload.get("srt_slots") or []
    if not os.path.isfile(zh_audio):
        zh_audio = _download_zh_audio(zh_audio, payload.get("zh_audio_url") or "")
    if not slots:
        raise RuntimeError("srt_slots empty")

    wavs = _cut_slots(zh_audio, slots)
    print(f"[diarize] cut done: {len(wavs)}/{len(slots)}", flush=True)
    if len(wavs) < 10:
        raise RuntimeError(f"too few valid refs: candidates={len(slots)}, valid={len(wavs)}")

    model = ecapa_preflight()
    if not model.ready:
        raise RuntimeError(f"model_unavailable: {model.reason}")

    # 仅允许已由受控发布预热并校验的本地快照；此 stage 从不下载模型。
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from speechbrain.inference.speaker import EncoderClassifier
    enc = EncoderClassifier.from_hparams(
        source=model.snapshot_path,
        savedir=model.snapshot_path,
        run_opts={"device": "cuda" if os.environ.get("NODE_GPU") != "0" else "cpu"})

    import soundfile as sf
    import torch
    decode_errors = (sf.LibsndfileError, sf.SoundFileError, sf.SoundFileRuntimeError)
    embs, ok_wavs, skipped = [], [], []
    for i, w in enumerate(wavs):
        try:
            wav_t, _ = sf.read(w["wav"], dtype="float32")
        except decode_errors as exc:
            reason = f"{type(exc).__name__}: {exc}"
            skipped.append(reason)
            print(f"[diarize] skip decode {w['wav']}: {reason}", flush=True)
            continue
        if wav_t.ndim > 1: wav_t = wav_t.mean(axis=1)
        t = torch.from_numpy(wav_t).unsqueeze(0)
        with torch.no_grad():
            e = enc.encode_batch(t)
        embs.append(e.squeeze().cpu().numpy())
        ok_wavs.append(w)
        if (i + 1) % 150 == 0:
            print(f"[diarize] embed {i+1}/{len(wavs)}", flush=True)
    if len(embs) < 10:
        reasons = "; ".join(dict.fromkeys(skipped)) or "no decoded embeddings"
        raise RuntimeError(
            f"too few embeddings: candidates={len(wavs)}, embeddings={len(embs)}, "
            f"skipped={len(skipped)}, reasons={reasons}"
        )
    X = np.stack(embs)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)

    from sklearn.cluster import AgglomerativeClustering
    labels = AgglomerativeClustering(
        n_clusters=None, distance_threshold=0.75, metric="cosine", linkage="average"
    ).fit_predict(X)

    clusters: dict[int, list[int]] = {}
    for i, lab in enumerate(labels):
        clusters.setdefault(int(lab), []).append(i)
    result = []
    for lab, members in sorted(clusters.items(), key=lambda x: -len(x[1])):
        snrs = sorted(((_snr(ok_wavs[m]["wav"]), m) for m in members), reverse=True)
        recs = [ok_wavs[m]["uid"] for _, m in snrs[:3]]
        result.append({"cluster": f"C{lab:02d}", "count": len(members),
                       "snr_est": snrs[0][0],
                       "recommended_refs": recs,
                       "uids": [ok_wavs[m]["uid"] for m in members]})
    result.sort(key=lambda r: -r["count"])

    out_json = os.path.join(WORKDIR, "diarize_result.json")
    json.dump({"project_id": payload.get("project_id"),
               "total": len(ok_wavs), "clusters": result},
              open(out_json, "w"), ensure_ascii=False, indent=1)

    rows = [{"key": "diarize_result", "path": out_json}]
    for r in result[:20]:
        for uid in r["recommended_refs"][:2]:
            p = os.path.join(REF_DIR, f"{uid}.wav")
            if os.path.exists(p):
                rows.append({"key": f"ref_{uid}", "path": p})
    return rows
