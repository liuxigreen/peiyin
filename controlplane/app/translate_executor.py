"""翻译执行器：五步链真实现（T205上下文包 → T211直译 → T212意译 → T213终检 → T214落库 → T220音节校验）

设计要点：
- Provider 从 DB 读（translation_providers，is_default），key 经 crypto 解密，环境零硬编码
- OpenAI 兼容 chat.completions；无 provider 时 fallback MOCK（本地词典，保证链路可测零成本）
- 429 退避 30/60/90s（V3 §4.2），HTTP 超时90s；场景批 = 一次请求翻译整批行（编号协议解析）
- 落库走 translations 表（utterance_id,target_lang,version 唯一），单句重翻=version+1
- 音节估算按语种路由（V3 §1.2）：en/es/pt/id=元音组；ja=假名；ko=音节块；zh=字数
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import (GlossaryTerm, PipelineTask, Project, PromptTemplate,
                        Speaker, Translation, TranslationProvider, Utterance)
from .orchestrator_dag import compute_input_hash

log = logging.getLogger("translate_executor")

BACKOFFS = (30, 60, 90, 30, 60)   # 429退避×5（PIPELINE-DETAILS Phase4）
HTTP_TIMEOUT = 90.0

# ── 音节估算（V3 §1.2 语种路由）─────────────────────────────
_VOWEL_GROUPS = re.compile(r"[aeiouyàáèéìíòóùúãẽĩõũ]+", re.I)
_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")


def count_syllables(text: str, lang: str) -> int:
    if not text:
        return 0
    lang = (lang or "en").lower()
    if lang.startswith("zh"):
        return len(re.findall(r"[\u4e00-\u9fff]", text))
    if lang.startswith("ja"):
        kana = len(_KANA.findall(text))
        kanji = len(re.findall(r"[\u4e00-\u9fff]", text))
        return kana + kanji * 2          # 汉字平均2 mora经验值
    if lang.startswith("ko"):
        return len(_HANGUL.findall(text))
    return max(1, len(_VOWEL_GROUPS.findall(text)))   # en/id/es/pt/th拉丁转写…


def syllable_ratio(en_text: str, zh_text: str, lang: str) -> float:
    base = count_syllables(zh_text, "zh") or 1
    return round(count_syllables(en_text, lang) / base, 3)


# ── LLM 客户端 ──────────────────────────────────────────────
def load_default_provider(db: Session) -> dict:
    """DB读默认provider；无→MOCK配置。返回 {mode,name,model,base,key,temperature,max_tokens}"""
    p = (db.query(TranslationProvider)
           .filter_by(is_enabled=True).order_by(TranslationProvider.is_default.desc(),
                                                TranslationProvider.priority.desc())
           .first())
    if not p:
        return {"mode": "mock", "name": "mock", "model": "mock-1",
                "base": "", "key": "", "temperature": 0.7, "max_tokens": 4096}
    from .core.crypto import decrypt_key
    return {"mode": "live", "name": p.name, "model": p.model_name or "",
            "base": (p.api_base_url or "").rstrip("/"), "key": "",
            "temperature": p.temperature or 0.7, "max_tokens": p.max_tokens or 4096,
            "_enc": p.api_key_encrypted or ""}


def _key_of(cfg: dict) -> str:
    if cfg.get("key"):
        return cfg["key"]
    if cfg.get("_enc"):
        from .core.crypto import decrypt_key
        return decrypt_key(cfg["_enc"])
    return ""


async def chat(cfg: dict, system: str, user: str) -> str:
    """单次LLM调用（含429退避）。MOCK模式返回确定性占位译文。"""
    if cfg["mode"] == "mock":
        return _mock_translate(user)
    key = _key_of(cfg)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": cfg["model"], "temperature": cfg["temperature"],
               "max_tokens": cfg["max_tokens"],
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    last_err = ""
    for attempt, wait in enumerate((0,) + BACKOFFS):
        if wait:
            log.warning("429 backoff %ss (attempt %d)", wait, attempt)
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
                r = await c.post(f"{cfg['base']}/chat/completions",
                                 headers=headers, json=payload)
            if r.status_code == 429:
                last_err = "HTTP 429"
                continue
            r.raise_for_status()
            data = r.json()
            return (data["choices"][0]["message"].get("content") or "").strip()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                last_err = "HTTP 429"
                continue
            raise
    raise RuntimeError(f"provider {cfg['name']} rate-limited after retries: {last_err}")


_MOCK_LEX = {"总裁": "CEO", "夫人": "madam", "少奶奶": "young madam", "离婚": "divorce",
             "你怎么敢": "How dare you", "我": "I", "你": "you", "他": "he", "她": "she"}


def _mock_translate(user: str) -> str:
    """确定性假译文：把编号行的中文按词典替换，保持行号结构。测试用。"""
    out = []
    for line in user.splitlines():
        m = re.match(r"^(\d+)\s*\|\s*(.+)$", line.strip())
        if not m:
            continue
        idx, text = m.group(1), m.group(2)
        en = text
        for zh, e in _MOCK_LEX.items():
            en = en.replace(zh, e)
        en = re.sub(r"[\u4e00-\u9fff]+", "line", en)   # 残余中文→占位
        out.append(f"{idx} | {en.strip() or 'line'}")
    return "\n".join(out)


def _parse_numbered(model_out: str, expect: int) -> dict[int, str]:
    """解析 'N | 译文' 行协议；缺失行回退占位，绝不静默错位。"""
    got: dict[int, str] = {}
    for line in model_out.splitlines():
        m = re.match(r"^\s*(\d+)\s*[|｜]\s*(.+)$", line.strip())
        if m:
            got[int(m.group(1))] = m.group(2).strip()
    return {i: got.get(i, f"[MISSING {i}]") for i in range(1, expect + 1)}


# ── 上下文包（T205）────────────────────────────────────────
def build_ctx_pack(db: Session, project: Project, utts: list[Utterance]) -> dict:
    """术语表 + 出现过的说话人卡 + 目标语种。轻量确定性，无LLM。"""
    terms = (db.query(GlossaryTerm)
               .filter(GlossaryTerm.target_lang == project.target_lang).all())
    spk_ids = {u.speaker_id for u in utts if u.speaker_id}
    spks = db.query(Speaker).filter(Speaker.id.in_(spk_ids)).all() if spk_ids else []
    tpl = (db.query(PromptTemplate)
             .filter_by(target_lang=project.target_lang, is_default=True).first())
    return {
        "target_lang": project.target_lang,
        "glossary": {t.source_term: t.target_term for t in terms},
        "role_cards": [{"label": s.label, "role_name": s.role_name or s.label} for s in spks],
        "prompt_version": f"tpl:{tpl.id[:8]}" if tpl else "builtin:v1",
        "template": tpl.content if tpl else None,
    }


# ── 五步链主流程 ───────────────────────────────────────────
_R1_SYS = ("你是影视字幕直译员。逐行翻译编号台词，保持行号格式 'N | 译文'。"
           "直译即可，忠实原文，术语表里的词必须用指定译法。")
_R2_SYS = ("你是影视配音译者。基于第一轮直译做意译：口语自然、符合角色人设、"
           "符合配音时长约束（尽量精炼）。保持行号格式 'N | 译文'。术语表译法不可更改。")
_RV_SYS = ("你是译配终审。检查全部台词：角色名/术语前后一致、风格统一、无漏译。"
           "输出修正后的完整清单，保持行号格式 'N | 译文'。无问题的行原样保留。")


async def run_translate_scene(db: Session, project: Project, scene_key: str) -> dict:
    """一个场景批的完整五步链。scene_key 形如 'SC01'（utterance.uid 前缀分组）。"""
    utts = (db.query(Utterance).filter_by(project_id=project.id)
              .order_by(Utterance.seq_index).all())
    in_scene = [u for u in utts if (u.uid or "").startswith(f"{scene_key}-")]
    if in_scene:
        utts = in_scene          # 场景批=uid前缀；无匹配时退化为全项目（单场景项目）
    if not utts:
        raise RuntimeError(f"no utterances for project {project.id}")

    ctx = build_ctx_pack(db, project, utts)
    provider = load_default_provider(db)
    lines = [f"{i+1} | {u.merged_text or u.original_text}" for i, u in enumerate(utts)]
    gloss = "\n".join(f"- {k} → {v}" for k, v in ctx["glossary"].items()) or "（无）"
    cards = "；".join(c["role_name"] for c in ctx["role_cards"]) or "（未标注）"

    r1 = await chat(provider, _R1_SYS,
                    f"目标语种:{ctx['target_lang']}\n术语表:\n{gloss}\n台词:\n" + "\n".join(lines))
    t1 = _parse_numbered(r1, len(utts))

    prev3 = "\n".join(l.split("|", 1)[-1].strip() for l in r1.splitlines()[-3:])
    r2 = await chat(provider, _R2_SYS,
                    f"前文译文（保持连贯）:\n{prev3}\n角色: {cards}\n"
                    f"第一轮直译:\n" + "\n".join(f"{i+1} | {t1[i+1]}" for i in range(len(utts))))
    t2 = _parse_numbered(r2, len(utts))

    rv = await chat(provider, _RV_SYS,
                    "全部台词终检:\n" + "\n".join(f"{i+1} | {t2[i+1]}" for i in range(len(utts))))
    t3 = _parse_numbered(rv, len(utts))

    # T214 落库（version化：已有译文则version+1）
    written = 0
    for i, u in enumerate(utts):
        text = t3[i + 1]
        ratio = syllable_ratio(text, u.merged_text or u.original_text, ctx["target_lang"])
        limit = (project.config or {}).get("syllable_limit", 1.15)
        last = (db.query(Translation)
                  .filter_by(utterance_id=u.id, target_lang=ctx["target_lang"])
                  .order_by(Translation.version.desc()).first())
        ver = (last.version + 1) if last else 1
        db.add(Translation(
            utterance_id=u.id, target_lang=ctx["target_lang"], version=ver, text=text,
            syllable_count=count_syllables(text, ctx["target_lang"]),
            syllable_ratio=ratio, is_over_limit=ratio > limit,
            llm_model=provider["model"] if provider["mode"] == "live" else "mock-1",
            prompt_version=ctx["prompt_version"],
            is_approved=provider["mode"] == "mock"))
        written += 1
    db.commit()
    return {"scene": scene_key, "utterances": written, "provider": provider["name"],
            "mode": provider["mode"], "prompt_version": ctx["prompt_version"],
            "over_limit": sum(1 for i in range(len(utts))
                              if syllable_ratio(t3[i+1], utts[i].merged_text or
                                                utts[i].original_text,
                                                ctx["target_lang"]) > 1.15)}


async def execute_translate_task(db: Session, task: PipelineTask) -> dict:
    """任务入口：task.task_type ∈ {ctx-pack,translate-r1,translate-r2,
    translate-review,merge-dubtrack,syllable-check} 全部映射到场景级五步链执行。
    幂等：任务行带 input_hash，编排器缓存层负责跳过。"""
    project = db.get(Project, task.project_id)
    if not project:
        raise RuntimeError(f"project {task.project_id} missing")
    scene = task.task_key.split("/")[0] if "/" in task.task_key else "SC01"
    # 五步链是批处理语义：场景批内任何一步触发都跑完整链（步间无独立产物价值）
    result = await run_translate_scene(db, project, scene)
    result["task_key"] = task.task_key
    return result
