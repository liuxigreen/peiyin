"""C0 角色提取Agent（v1.1）：LLM扫全剧台词 → 角色档案 + 中英人名术语表。
服务两头：翻译一致性（glossary/speaker卡） + 配音声线分配（gender/age/timbre）。
幂等：同项目重跑→更新utterance_count与字段，不重复建行（按label唯一）。
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter

from sqlalchemy.orm import Session

from .db.models import GlossaryTerm, Project, Speaker, Utterance
from .translate_executor import (_get_limiter, count_syllables,
                                 load_default_provider, load_fallback_providers)
from . import translate_executor as _te

log = logging.getLogger("cast_agent")

_CAST_SYS = (
    "你是影视剧本顾问。分析短剧台词，提取全部出场角色，输出JSON（不要任何解释）：\n"
    '{"characters":[{"label":"中文称呼(如 阿弋/盛母/司机甲)","role_name":"英文译名(如 A-Yi)",'
    ',"is_primary":true/false(主角/配角),"gender":"male/female/unknown",'
    '"age_band":"child/teen/young/middle/elder/unknown",'
    '"timbre":"音色建议,如 少年清亮/低沉威严/干练女声/沙哑老者",'
    '"en_variants":["英文里可能的拼写变体"]}]}\n'
    "规则：只提有台词或被台词明确指涉的角色；群演/无名角色合并为 群众/路人甲；"
    "label 用剧中中文称呼；英文译名用拼音或意译，全剧唯一。")

_CAST_USER = (
    "全剧台词（编号）：\n{lines}\n\n请提取角色档案JSON。注意："
    "全文共{n}句，可能提及未出场的关键人物（如 盛父），也要列出。")


def _chunk_lines(utts: list[Utterance], size: int = 300) -> list[list[str]]:
    out = []
    for i in range(0, len(utts), size):
        out.append([f"{j} | {(u.merged_text or u.original_text or '')[:80]}"
                    for j, u in enumerate(utts[i:i + size], i + 1)])
    return out


def _merge_cast(buckets: list[list[dict]]) -> list[dict]:
    """多块结果合并：label 归一化聚合，en_variants 并集，is_primary 任一为真即真。"""
    by_label: dict[str, dict] = {}
    for b in buckets:
        for c in b:
            label = (c.get("label") or "").strip()
            if not label:
                continue
            if label not in by_label:
                by_label[label] = {**c, "en_variants": list(c.get("en_variants") or [])}
            else:
                d = by_label[label]
                if c.get("is_primary"):
                    d["is_primary"] = True
                if c.get("role_name") and not d.get("role_name"):
                    d["role_name"] = c["role_name"]
                for g in ("gender", "age_band", "timbre"):
                    if c.get(g) and (not d.get(g) or d[g] in ("unknown", "")):
                        d[g] = c[g]
                for v in (c.get("en_variants") or []):
                    if v and v not in d["en_variants"]:
                        d["en_variants"].append(v)
    return list(by_label.values())


def _pick(obj: dict, *keys, default=""):
    for k in keys:
        v = (obj.get(k) or "").strip()
        if v and v.lower() != "unknown":
            return v
    return default


async def extract_cast(db: Session, project: Project) -> dict:
    """扫全剧台词→角色档案→写 speakers + glossary_terms。幂等可重跑。"""
    utts = (db.query(Utterance).filter_by(project_id=project.id)
              .order_by(Utterance.seq_index).all())
    if not utts:
        raise RuntimeError("no utterances to analyze")

    provider = load_default_provider(db)
    fallbacks = load_fallback_providers(db)
    candidates = [provider] + fallbacks

    # 台词量大时分块多次调用合并（每次一个角色提取请求）
    chunks = _chunk_lines(utts, 300)
    buckets: list[list[dict]] = []
    for ci, chunk in enumerate(chunks):
        user = _CAST_USER.format(lines="\n".join(chunk), n=len(chunk))
        got = None
        for cand in candidates:                       # 兜底链同样适用于本Agent
            try:
                raw = await _te.chat(cand, _CAST_SYS, user)
                m = re.search(r"\{[\s\S]*\}", raw)
                data = json.loads(m.group(0)) if m else {}
                chars = data.get("characters") or []
                if chars:
                    got = chars
                    break
            except Exception as e:                    # noqa: BLE001
                log.warning("cast chunk %d provider %s failed: %s", ci, cand["model"], e)
        if got:
            buckets.append(got)
    if not buckets:
        raise RuntimeError("cast extraction failed on all providers")

    characters = _merge_cast(buckets)
    counts = Counter((u.merged_text or u.original_text or "") for u in utts)

    # ---- 写 speakers（label唯一，幂等）----
    spk_by_label: dict[str, Speaker] = {}
    for s in db.query(Speaker).filter_by(project_id=project.id).all():
        spk_by_label.setdefault(s.label, s)
    created = 0
    for c in characters:
        label = c["label"]
        spk = spk_by_label.get(label)
        if spk is None:
            spk = Speaker(project_id=project.id, label=label)
            db.add(spk)
            created += 1
        spk.role_name = _pick(c, "role_name", default=label)
        spk.is_primary = bool(c.get("is_primary"))
        # 音色/性别/年龄段进 ref_audio_pool 元数据（配音Agent按此分配声线）
        spk.ref_audio_pool = [{"gender": _pick(c, "gender", default="unknown"),
                               "age_band": _pick(c, "age_band", default="unknown"),
                               "timbre": _pick(c, "timbre", default="")}]
    # utterance_count：按台词归属统计（无说话人标注阶段，用提及计数近似）
    for label, spk in ((c["label"], spk_by_label.get(c["label"])) for c in characters):
        if spk is None:
            continue
        cnt = sum(1 for u in utts if label in (u.merged_text or u.original_text or ""))
        spk.utterance_count = cnt
    db.commit()

    # ---- 写 glossary_terms：人名对照（target_lang 唯一键，幂等）----
    g_created = 0
    for c in characters:
        en = _pick(c, "role_name")
        if not en or c["label"] == en:
            continue
        row = (db.query(GlossaryTerm)
                 .filter_by(series_name=project.name, source_term=c["label"],
                            target_lang=project.target_lang).first())
        variants = " | ".join((c.get("en_variants") or [])[:4])
        if row is None:
            db.add(GlossaryTerm(series_name=project.name, source_term=c["label"],
                                target_lang=project.target_lang, target_term=en,
                                note=f"{_pick(c,'gender')}/{_pick(c,'age_band')} {variants}".strip()))
            g_created += 1
        else:
            row.target_term = en
            row.note = f"{_pick(c,'gender')}/{_pick(c,'age_band')} {variants}".strip()
    db.commit()

    return {"characters": len(characters), "speakers_created": created,
            "glossary_created": g_created,
            "cast": [{"label": c["label"], "en": _pick(c, "role_name"),
                      "gender": _pick(c, "gender"), "age": _pick(c, "age_band"),
                      "primary": bool(c.get("is_primary"))} for c in characters]}
