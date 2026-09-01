"""TTS stage（3060/任意GPU节点）：任务 payload → 合成音频 → 本地路径。
两种模式：
- engine=cosyvoice_api / fish_api：调用本机已启动的引擎HTTP服务（推荐，engines自管理显存）
- engine=mock：测试协议闭环（生成占位wav）
任务payload约定（控制面下发）:
  {"task_type": "tts-generate", "payload": {"text": "...", "lang": "en",
   "ref_audio": "path/on/node", "engine": "cosyvoice_api",
   "engine_url": "http://127.0.0.1:50000/tts", "out_path": "/workdir/out/xxx.wav"}}
产物返回 [{key, path}]，控制面按需取回。
"""
from __future__ import annotations

import os
import time
import urllib.request
import urllib.error
import json
import wave
import math

import numpy as np
import soundfile as sf

from .router import register

WORKDIR = os.getenv("NODE_WORKDIR", os.path.join(os.path.dirname(__file__), "..", "workdir"))
OUTDIR = os.path.join(WORKDIR, "tts_out")
os.makedirs(OUTDIR, exist_ok=True)

TTS_TIMEOUT = int(os.getenv("TTS_TIMEOUT", "300"))
REFDIR = os.path.join(WORKDIR, "refs")
os.makedirs(REFDIR, exist_ok=True)


def _resolve_ref(payload: dict) -> str:
    """参考音解析三级：节点本地路径 → payload内嵌base64（云端生成的预置音色，
    首次落地 workdir/refs/{voice_id}.wav 后续复用，不再重复传输） → 无参考音。"""
    ref = payload.get("ref_audio") or ""
    if ref and os.path.exists(ref):
        return ref
    b64 = payload.get("ref_audio_b64")
    if b64:
        import base64
        vid = payload.get("voice_id") or "voice"
        safe = "".join(c for c in vid if c.isalnum() or c in "-_")[:60] or "voice"
        path = os.path.join(REFDIR, f"{safe}.wav")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
        return path
    return ""


def _write_placeholder(path: str, seconds: float = 2.0, sr: int = 16000):
    n = int(seconds * sr)
    t = np.linspace(0, seconds, n, endpoint=False)
    wav = (0.25 * np.sin(2 * np.pi * 320 * t)).astype("float32")
    sf.write(path, wav, sr)


@register("tts-generate")
def run_tts(task: dict) -> list[dict]:
    payload = task.get("payload") or {}
    engine = (payload.get("engine") or "mock").lower()
    text = payload.get("text") or ""
    lang = payload.get("lang") or "en"
    ref = _resolve_ref(payload)
    out_path = payload.get("out_path") or os.path.join(
        OUTDIR, f"tts_{int(time.time()*1000)}.wav")

    if engine in ("cosyvoice_api", "fish_api", "api"):
        return [_tts_via_http(task, payload, engine, text, lang, ref, out_path)]
    # mock / 未知引擎：协议闭环占位
    _write_placeholder(out_path, min(2.0, max(0.6, len(text) * 0.06)))
    return [{"key": task.get("task_key", "tts"), "path": out_path, "engine": "mock"}]


def _tts_via_http(task: dict, payload: dict, engine: str, text: str,
                  lang: str, ref: str, out_path: str) -> dict:
    """调用节点本机的 TTS HTTP 服务。
    CosyVoice2 自带 webui/openapi 兼容：POST {text, prompt_audio/...} → wav
    Fish Speech S1-mini 自带 openaudio api：POST /v1/tts
    这里做统一适配：按 engine 选字段名，响应统一落 wav。
    """
    url = payload.get("engine_url") or os.getenv(
        "TTS_ENGINE_URL", "http://127.0.0.1:50000/tts")
    body = {"text": text, "lang": lang}
    # G7：语气/情绪参数透传（payload此前只有text/lang/ref_audio，
    # TTS语气全靠参考音隐式传递）。instruct→CosyVoice instruct_text；rate→speed。
    if payload.get("instruct"):
        body["instruct_text"] = payload["instruct"]
        body["instruct"] = payload["instruct"]
    if payload.get("rate"):
        body["speed"] = payload["rate"]
        body["rate"] = payload["rate"]
    if ref and os.path.exists(ref):
        # 引擎一般要 multipart/或 base64；两种都试，先走最简 JSON+ref_path（本机同机部署路径可见）
        body["ref_audio_path"] = ref
        body["prompt_audio"] = ref
    if engine == "fish_api":
        req_body = {"text": text, "reference_audio": ref,
                    "reference_audio_path": ref, "format": "wav"}
        if payload.get("rate"):
            req_body["speed"] = payload["rate"]
    else:
        req_body = body

    data = json.dumps(req_body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=TTS_TIMEOUT) as resp:
        raw = resp.read()
    dt = time.time() - t0
    ctype = resp.headers.get("Content-Type", "")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    if "json" in ctype:
        d = json.loads(raw)
        audio_url = d.get("url") or d.get("audio_url") or d.get("audio")
        if audio_url and audio_url.startswith("http"):
            urllib.request.urlretrieve(audio_url, out_path)
        elif d.get("audio_base64"):
            import base64
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(d["audio_base64"]))
        else:
            raise RuntimeError(f"tts api json response missing audio: {list(d)[:6]}")
    else:
        with open(out_path, "wb") as f:
            f.write(raw)

    # 校验可读 & 记录时长
    info = sf.info(out_path)
    return {"key": task.get("task_key", "tts"), "path": out_path,
            "engine": engine, "duration_ms": int(info.frames / info.samplerate * 1000),
            "latency_s": round(dt, 1)}
