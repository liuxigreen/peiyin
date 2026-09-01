"""G8音色分配（最小实装）：speaker → TTS 引擎/参考音/语气参数。
三级策略（DESIGN-B-配音方案-v2 §二b）：
  L1 主角/有簇参考音 → 克隆该簇参考音（diarize实装后 ref_audio_pool 带cluster_ref）
  L2 有 gender/age_band/timbre 标签 → voice_assets 按标签匹配预置音色
  L3 全缺 → 项目级 config.tts 默认引擎（zero-shot默认音色）
diarize未实装阶段：ref_audio_pool 只有 C0 的元数据，L1 暂由 L2 承接。
返回字段缺省为 None——调用方逐级回退到 body 显式参数与最终默认值。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .db.models import Project, Speaker, VoiceAsset


def assign_voice(db: Session, project: Project, speaker: Speaker | None) -> dict:
    out: dict = {"engine": None, "engine_url": None, "ref_audio": None,
                 "instruct": None, "rate": None}
    if speaker is None:
        return out
    pool = speaker.ref_audio_pool or []
    meta = pool[0] if (isinstance(pool, list) and pool
                       and isinstance(pool[0], dict)) else {}

    # L1：簇参考音（diarize产物）
    cluster_ref = meta.get("ref_audio") or meta.get("cluster_ref")
    if cluster_ref:
        out["ref_audio"] = cluster_ref
        out["engine"] = meta.get("engine")

    # L2：voice_assets 按标签匹配（gender/age_band/timbre 命中数最高者）
    tags = {t for t in (meta.get("gender"), meta.get("age_band"), meta.get("timbre"))
            if t and t not in ("unknown", "")}
    if tags and (out["ref_audio"] is None or out["engine"] is None):
        best: tuple[int, VoiceAsset] | None = None
        for a in db.query(VoiceAsset).all():
            score = len(set(a.tags or []) & tags)
            if score and (best is None or score > best[0]):
                best = (score, a)
        if best:
            a = best[1]
            out["ref_audio"] = out["ref_audio"] or a.ref_audio_r2_key
            params = a.tts_params or {}
            out["engine"] = out["engine"] or params.get("engine")
            out["rate"] = out["rate"] or params.get("rate")

    # L3：项目级默认 + 主角语气
    cfg = (project.config or {}).get("tts") or {}
    out["engine"] = out["engine"] or cfg.get("engine")
    out["engine_url"] = out["engine_url"] or cfg.get("engine_url")
    if speaker.is_primary and cfg.get("instruct_primary"):
        out["instruct"] = out["instruct"] or cfg["instruct_primary"]
    return out
