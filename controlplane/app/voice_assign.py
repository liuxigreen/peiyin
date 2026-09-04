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

    # L2：voice_assets 匹配。评分 = 标签命中×2 + 音色描述关键词命中
    # （同性别同年龄段的多个角色靠 C0 timbre 描述区分声线，如"低沉威严"vs"轻浮冷漠"）
    tags = {t for t in (meta.get("gender"), meta.get("age_band"))
            if t and t not in ("unknown", "")}
    timbre = meta.get("timbre") or ""
    if (tags or timbre) and (out["ref_audio"] is None or out["engine"] is None):
        def _kw_hits(desc: str) -> int:
            if not timbre or not desc:
                return 0
            grams = {desc[i:i + 2] for i in range(len(desc) - 1)}
            return sum(1 for g in grams if len(g) >= 2 and g in timbre)
        best: tuple[int, VoiceAsset] | None = None
        for a in db.query(VoiceAsset).all():
            atags = set(a.tags or [])
            score = 2 * len(atags & tags) + _kw_hits((a.tts_params or {}).get("desc", ""))
            if not atags & tags and not _kw_hits((a.tts_params or {}).get("desc", "")):
                continue
            if best is None or score > best[0]:
                best = (score, a)
        if best:
            a = best[1]
            out["ref_audio"] = out["ref_audio"] or a.ref_audio_r2_key
            params = a.tts_params or {}
            out["engine"] = out["engine"] or params.get("engine")
            out["rate"] = out["rate"] or params.get("rate")
    # 兜底：龙套/群众角色（性别年龄为空或无标签命中）按性别给默认音色，
    # 避免'20个人物失败'式全 NONE 单音色回退（0903白月光教训）
    if out["ref_audio"] is None:
        g = (meta.get("gender") or "") if isinstance(meta, dict) else ""
        want = "female" if g == "female" else "male"
        fb = None                        # P0修复(0905审计)：必须先初始化，
        for a in db.query(VoiceAsset).all():   # 零匹配时原代码UnboundLocalError
            if want in set(a.tags or []):
                fb = a
                break
        if fb is None:
            fb = db.query(VoiceAsset).first()
        if fb is not None:
            out["ref_audio"] = fb.ref_audio_r2_key
            params = fb.tts_params or {}
            out["engine"] = out["engine"] or params.get("engine")
            out["rate"] = out["rate"] or params.get("rate")

    # L3：项目级默认 + 主角语气
    cfg = (project.config or {}).get("tts") or {}
    out["engine"] = out["engine"] or cfg.get("engine")
    out["engine_url"] = out["engine_url"] or cfg.get("engine_url")
    if speaker.is_primary and cfg.get("instruct_primary"):
        out["instruct"] = out["instruct"] or cfg["instruct_primary"]
    return out
