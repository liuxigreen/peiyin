"""翻译执行器：五步链真实现（T205上下文包 → T211直译 → T212意译 → T213终检 → T214落库 → T220音节校验）

v0.4（2026-08-31，白月光审核后重构）：
- 子批执行：场景按 batch_size（默认10，project.config 可调）分块跑三步链
- 内容审查韧性：网关以 HTTP 200 + finish_reason=content_filter + usage=0 + 拒绝文案
  拒绝含敏感词的批（生产实测：行级触发，提示词措辞无法绕过）→ ContentFilteredError；
  子批二分自动定位触发句并隔离（不落库），干净行照常翻译——绝不写 [MISSING] 占位符
- R2 意译带每行音节预算（拉丁语目标）；R3 终检改增量协议（只回修改行，防截断）
- 落库后超限压缩闭环（≤2轮，仍超限才计入 over_limit；待办#1）
设计要点：
- Provider 从 DB 读（translation_providers，is_default），key 经 crypto 解密，环境零硬编码
- OpenAI 兼容 chat.completions；无 provider 时 fallback MOCK（本地词典，保证链路可测零成本）
- 429 退避 30/60/90s（V3 §4.2），HTTP 超时90s；落库走 translations 表
  （utterance_id,target_lang,version 唯一），单句重翻=version+1
- 音节估算按语种路由（V3 §1.2）：en/es/pt/id=元音组；ja=假名；ko=音节块；zh=字数
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx
from sqlalchemy.orm import Session

from .db.models import (GlossaryTerm, PipelineTask, Project, PromptTemplate,
                        Speaker, Translation, TranslationProvider, Utterance)
from .orchestrator_dag import compute_input_hash

log = logging.getLogger("translate_executor")

BACKOFFS = (30, 60, 90, 30, 60)   # 429退避×5（PIPELINE-DETAILS Phase4）
HTTP_TIMEOUT = 90.0

# 内容审查拒绝文案特征（生产实测：'当前输入涉及敏感信息，让我们换个话题…'，usage=0）
_REFUSAL_MARKERS = ("敏感", "换个话题", "cannot assist", "I cannot help", "无法协助")
# 历史占位符签名（绝不允许再落库）
_PLACEHOLDER_RE = re.compile(
    r"^\s*\[(MISSING|Translation|UNTRANSLATED|Paragraph|Segment)")


class ContentFilteredError(RuntimeError):
    """网关内容审查拒绝。输入决定的结果——重试无意义，由上层二分隔离。"""


class FormatDriftError(RuntimeError):
    """模型输出格式漂移（重试后行协议仍解析不全）。由兜底链换模型接手。"""


class _RateLimiter:
    """全局调用节奏器：把每provider的调用间隔精确排开（用户实测M3限10次/分）。
    持锁等待=排队效果，所有并发worker共享同一节奏，从源头消灭429盲等。"""

    def __init__(self, per_min: float):
        self._interval = 60.0 / max(per_min, 0.1)
        self._lock = asyncio.Lock()
        self._next_t = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            if self._next_t > now:
                await asyncio.sleep(self._next_t - now)
                now = loop.time()
            self._next_t = max(now, self._next_t) + self._interval


_LIMITERS: dict[str, _RateLimiter] = {}


def _get_limiter(cfg: dict) -> _RateLimiter | None:
    """按 模型 的实测配额限速（env可调）：
    MiniMax-M3=9/分（用户实测10/分留余量）；edgefn其他模型=4/分（burst实测更紧）；
    chat-b-ai(b.ai)=不限（2000/分）；未知=不限。"""
    import os
    tag = f"{cfg.get('name', '')} {cfg.get('model', '')}".lower()
    key = f"{cfg.get('name', '')}:{cfg.get('model', '')}"
    if "minimax-m3" in tag:
        rate = float(os.getenv("TRANSLATE_RATE_M3", "9"))
    elif "edgefn" in tag and "minimax" not in tag:
        rate = float(os.getenv("TRANSLATE_RATE_EDGEFN_ALT", "4"))
    else:
        return None
    if key not in _LIMITERS:
        _LIMITERS[key] = _RateLimiter(rate)
    return _LIMITERS[key]


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


def _src_of(u: Utterance) -> str:
    return u.merged_text or u.original_text or ""


def _is_ph(s: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(s or ""))


def is_placeholder(text: str) -> bool:
    """占位符/拒绝残留判定（历史bug落库的垃圾行；下游取数一律视同不存在）。"""
    return _is_ph(text)


def _zh_ratio(s: str) -> float:
    if not s:
        return 0.0
    return len(re.findall(r"[\u4e00-\u9fff]", s)) / max(len(s), 1)


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


def load_fallback_providers(db: Session) -> list[dict]:
    """兜底provider链（v0.9）：启用中、非默认，按priority降序。
    edgefn同网关多模型（新key全模型可用）+ 外部网关构成段级兜底链。"""
    rows = (db.query(TranslationProvider)
              .filter_by(is_enabled=True, is_default=False)
              .order_by(TranslationProvider.priority.desc())
              .all())
    return [{"mode": "live", "name": p.name, "model": p.model_name or "",
             "base": (p.api_base_url or "").rstrip("/"), "key": "",
             "temperature": p.temperature or 0.7, "max_tokens": p.max_tokens or 4096,
             "_enc": p.api_key_encrypted or ""} for p in rows]


def load_fallback_provider(db: Session) -> dict | None:
    """兼容旧签名：兜底链第一个。"""
    chain = load_fallback_providers(db)
    return chain[0] if chain else None


def _key_of(cfg: dict) -> str:
    if cfg.get("key"):
        return cfg["key"]
    if cfg.get("_enc"):
        from .core.crypto import decrypt_key
        return decrypt_key(cfg["_enc"])
    return ""


def _is_refusal_text(content: str) -> bool:
    """拒绝文案识别：短、无行协议、含特征词。正常翻译输出（带行协议）永不误判。"""
    if not content:
        return False
    if re.search(r"^\s*\d+\s*[|｜]", content, re.M):
        return False                      # 行协议输出=正常翻译结果
    return len(content) < 120 and any(m in content for m in _REFUSAL_MARKERS)


async def chat(cfg: dict, system: str, user: str) -> str:
    """单次LLM调用（含429退避）。内容审查拒绝→ContentFilteredError（不退避重试）。"""
    if cfg["mode"] == "mock":
        return _mock_translate(user)
    key = _key_of(cfg)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": cfg["model"], "temperature": cfg["temperature"],
               "max_tokens": cfg["max_tokens"],
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    # M3思考模式开关（白山网关实测：enable_thinking=false 思考归零、token-69%、延迟减半，
    # 行协议完好）。字幕直译+术语表约束下思考增益有限，默认关闭换速度与成本。
    if "minimax" in (cfg["model"] or "").lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    last_err = ""
    for attempt, wait in enumerate((0,) + BACKOFFS):
        if wait:
            log.warning("429 backoff %ss (attempt %d)", wait, attempt)
            await asyncio.sleep(wait)
        try:
            lim = _get_limiter(cfg)
            if lim:
                await lim.acquire()
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
                r = await c.post(f"{cfg['base']}/chat/completions",
                                 headers=headers, json=payload)
            if r.status_code == 429:
                last_err = "HTTP 429"
                continue
            r.raise_for_status()
            data = r.json()
            ch = (data.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            content = (msg.get("content") or "").strip()
            usage = data.get("usage") or {}
            ctoks = usage.get("completion_tokens")
            filtered = (ch.get("finish_reason") == "content_filter"
                        or (not content and ctoks in (0, None))
                        or _is_refusal_text(content))
            if filtered:
                # 输入决定的结果：重试同样被拒。抛给上层做二分隔离。
                raise ContentFilteredError(
                    f"gateway content filter: finish_reason={ch.get('finish_reason')} "
                    f"completion_tokens={ctoks}")
            return content
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
    """解析 'N | 译文' 行协议；缺失行回退占位（仅在链内中间态使用，
    落库前会被 _PLACEHOLDER_RE 拦截转隔离，绝不写入 DB）。"""
    got: dict[int, str] = {}
    for line in model_out.splitlines():
        m = re.match(r"^\s*(\d+)\s*[|｜]\s*(.+)$", line.strip())
        if m:
            got[int(m.group(1))] = m.group(2).strip()
    return {i: got.get(i, f"[MISSING {i}]") for i in range(1, expect + 1)}


def _parse_fixes(model_out: str) -> dict[int, str]:
    """解析增量输出（终检/压缩）：只返回模型实际给出的行。"""
    got: dict[int, str] = {}
    for line in model_out.splitlines():
        m = re.match(r"^\s*(\d+)\s*[|｜]\s*(.+)$", line.strip())
        if m:
            got[int(m.group(1))] = m.group(2).strip()
    return got


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
_LANG_NAME = {"en": "English", "ja": "日本語", "ko": "한국어", "es": "Español",
              "pt": "Português", "id": "Bahasa Indonesia", "th": "ไทย",
              "vi": "Tiếng Việt", "ms": "Bahasa Melayu", "zh": "中文"}

def _lang_rule(lang: str) -> str:
    """硬约束输出语种（M3等模型会偷偷输出中文润色版——必须显式禁止）"""
    name = _LANG_NAME.get(lang, lang)
    return (f"【铁律】你的输出必须全部是{lang}({name})，一个汉字都不允许出现。"
            f"即使原文是中文，你也只能输出{name}翻译。")

_R1_SYS = ("你是影视字幕直译员。逐行翻译编号台词，保持行号格式 'N | 译文'。"
           "直译即可，忠实原文，术语表里的词必须用指定译法。")
_R2_SYS = ("你是影视配音译者。基于第一轮直译做意译：口语自然、符合角色人设、"
           "符合配音时长约束（尽量精炼）。保持行号格式 'N | 译文'。术语表译法不可更改。")
_RV_SYS = ("你是译配终审。检查台词清单：角色名/术语前后一致、风格统一、无漏译。"
           "只输出需要修改的行，格式 'N | 修正译文'；无修改的行不要输出。"
           "术语表译法不可更改。")
_COMP_SYS = ("你是配音字幕压缩员。把每行译文压缩到目标音节数内：保持原意、人名与语气，"
             "能删则删。保持行号格式 'N | 译文'。术语表译法不可更改。")

_LATIN_TARGETS = ("en", "es", "pt", "id", "th", "vi", "ms", "fr", "de", "it")


def _budget_map(utts: list[Utterance], target_lang: str,
                limit: float) -> dict[int, int] | None:
    """拉丁语目标给出每句音节预算（zh/ja/ko节奏不同，不给）。key=utterance下标。"""
    if not any(target_lang.startswith(p) for p in _LATIN_TARGETS):
        return None
    return {i: max(1, int(count_syllables(_src_of(u), "zh") * limit))
            for i, u in enumerate(utts)}


def _budget_block(chunk: list[int], budget: dict[int, int] | None) -> str:
    if not budget:
        return ""
    return ("时长预算(每行音节上限): "
            + ", ".join(f"{i}→{budget[idx]}" for i, idx in enumerate(chunk, 1)) + "\n")


async def _run_chunk_chain(provider: dict, ctx: dict, lang_rule: str,
                           utts: list[Utterance], chunk: list[int],
                           gloss: str, cards: str,
                           budget: dict[int, int] | None,
                           prev_tail: list[str],
                           r2_cfg: dict | None = None) -> tuple[dict[int, str], list[str]]:
    """一个块的多Agent协作链（v1.0）：
    R1直译=主力M3(最快) → R2本地化意译=专职模型(r2_cfg,默认DeepSeek-V3,习语地道化最强)
    → R3增量终检=M3。返回 {utterance下标: 文本} 与尾部译文。"""
    lines = [f"{i+1} | {_src_of(utts[idx])}" for i, idx in enumerate(chunk)]

    def _merge(best: dict[int, str], alt: dict[int, str]) -> int:
        """用 alt 补 best 里的缺失行，返回补上的行数。"""
        fixed = 0
        for k in best:
            if _is_ph(best[k]) and not _is_ph(alt.get(k, "")):
                best[k] = alt[k]
                fixed += 1
        return fixed

    r1 = await chat(provider, f"{_R1_SYS}\n{lang_rule}",
                    f"术语表:\n{gloss}\n台词:\n" + "\n".join(lines))
    t1 = _parse_numbered(r1, len(chunk))
    if any(_is_ph(v) for v in t1.values()):
        # M3偶发格式漂移（输出不带行协议）：重试一次，两份取并集
        log.warning("R1 parse incomplete (%d/%d) chunk=%s — retry once",
                    sum(1 for v in t1.values() if _is_ph(v)), len(chunk),
                    utts[chunk[0]].uid)
        t1b = _parse_numbered(await chat(provider, f"{_R1_SYS}\n{lang_rule}\n"
                                         "注意：只输出 'N | 译文' 格式的行，"
                                         "不要任何解释、前言或多余文本。",
                                         f"术语表:\n{gloss}\n台词:\n" + "\n".join(lines)),
                              len(chunk))
        _merge(t1, t1b)
    if any(_is_ph(v) for v in t1.values()):
        raise FormatDriftError(f"R1 incomplete after retry: chunk={utts[chunk[0]].uid}")

    prev3 = "\n".join(prev_tail[-3:]) if prev_tail else "（无）"
    r2cfg = r2_cfg or provider
    r2 = await chat(r2cfg, f"{_R2_SYS}\n{lang_rule}",
                    f"前文译文（保持连贯）:\n{prev3}\n角色: {cards}\n"
                    + _budget_block(chunk, budget)
                    + "第一轮直译:\n" + "\n".join(f"{i+1} | {t1[i+1]}" for i in range(len(chunk))))
    t2 = _parse_numbered(r2, len(chunk))
    if any(_is_ph(v) for v in t2.values()):
        log.warning("R2 parse incomplete (%d/%d) chunk=%s — retry once",
                    sum(1 for v in t2.values() if _is_ph(v)), len(chunk),
                    utts[chunk[0]].uid)
        t2b = _parse_numbered(await chat(r2cfg, f"{_R2_SYS}\n{lang_rule}\n"
                                         "注意：只输出 'N | 译文' 格式的行，"
                                         "不要任何解释、前言或多余文本。",
                                         f"前文译文（保持连贯）:\n{prev3}\n角色: {cards}\n"
                                         + _budget_block(chunk, budget)
                                         + "第一轮直译:\n" + "\n".join(f"{i+1} | {t1[i+1]}" for i in range(len(chunk)))),
                              len(chunk))
        _merge(t2, t2b)
    # R2仍缺的行回退用R1直译（有译文好过隔离；终检会再看一遍）
    for k in t2:
        if _is_ph(t2[k]) and not _is_ph(t1[k]):
            t2[k] = t1[k]
    if any(_is_ph(v) for v in t2.values()):
        raise FormatDriftError(f"R2 incomplete after retry+R1 fallback: chunk={utts[chunk[0]].uid}")

    try:
        rv = await chat(provider, f"{_RV_SYS}\n{lang_rule}",
                        "全部台词(R2译):\n"
                        + "\n".join(f"{i+1} | {t2[i+1]}" for i in range(len(chunk))))
        fixes = _parse_fixes(rv)
    except ContentFilteredError:
        fixes = {}               # 终检被滤→降级用R2结果（R2已成功，不算失败）

    texts = {}
    for i, idx in enumerate(chunk):
        fx = fixes.get(i + 1)
        texts[idx] = fx if (fx and not _PLACEHOLDER_RE.match(fx)) else t2[i + 1]
    tail = [texts[idx] for idx in chunk[-3:]]
    return texts, tail


async def _run_chunk_filtered(provider: dict, ctx: dict, lang_rule: str,
                              utts: list[Utterance], chunk: list[int],
                              gloss: str, cards: str,
                              budget: dict[int, int] | None,
                              prev_tail: list[str],
                              stats: dict,
                              fallback_chain: list[dict] | None = None,
                              r2_step_cfg: dict | None = None
                              ) -> tuple[dict[int, str], list[str]]:
    """块执行+段级兜底链（v0.9）：任一失败（审查/网络/格式漂移）→
    当场换下一个模型重跑同一块，绝不重跑整个场景。
    主provider整块被滤→二分定位触发句（干净子块主provider跑）。"""
    # 兜底模型链：主provider跑（审查/网络/漂移任一失败）→ 按序换模型跑同一块
    candidates = [provider] + [fb for fb in (fallback_chain or [])
                               if fb.get("model") != provider.get("model")]
    last_exc: Exception | None = None
    for ci, cand in enumerate(candidates):
        # 分步模型路由：首轮 R2=专职本地化模型；兜底轮=兜底模型整块接管（含R2）
        r2cfg = r2_step_cfg if ci == 0 else None
        try:
            texts, tail = await _run_chunk_chain(cand, ctx, lang_rule, utts, chunk,
                                                 gloss, cards, budget, prev_tail,
                                                 r2_cfg=r2cfg)
            if ci > 0:
                stats["fallback_used"] += 1
                log.warning("chunk=%s 主provider失败(%s) → 兜底#%d(%s)救援成功",
                            utts[chunk[0]].uid, type(last_exc).__name__ if last_exc else "?",
                            ci, cand.get("model"))
            return texts, tail
        except ContentFilteredError as e:
            last_exc = e
            stats["filtered_chunks"] += 1
            if ci == 0 and len(chunk) == 1:
                log.warning("content-filter: 主provider拒句 uid=%s → 换兜底模型",
                            utts[chunk[0]].uid)
                continue
            if ci > 0 or len(chunk) == 1:
                log.warning("content-filter: 兜底#%d也拒 chunk=%s(%d句)",
                            ci, utts[chunk[0]].uid, len(chunk))
                break
            # 主provider整块被滤→二分定位（用主provider跑干净子块）
            mid = len(chunk) // 2
            texts: dict[int, str] = {}
            tail = prev_tail
            for half in (chunk[:mid], chunk[mid:]):
                t2, tail2 = await _run_chunk_filtered(
                    provider, ctx, lang_rule, utts, half, gloss, cards,
                    budget, tail, stats, fallback_chain, r2_step_cfg)
                texts.update(t2)
                if tail2:
                    tail = tail2
            return texts, tail
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            log.warning("chunk=%s provider=%s 网络故障(%s) → 换下一兜底",
                        utts[chunk[0]].uid, cand.get("model"), type(e).__name__)
            continue
        except FormatDriftError as e:
            last_exc = e
            log.warning("chunk=%s provider=%s 格式漂移 → 换下一兜底",
                        utts[chunk[0]].uid, cand.get("model"))
            continue
    raise last_exc if last_exc else RuntimeError("all fallback candidates failed")


def _write_translation(db: Session, u: Utterance, target_lang: str, text: str,
                       ratio: float, ctx: dict, provider: dict, limit: float) -> None:
    last = (db.query(Translation)
              .filter_by(utterance_id=u.id, target_lang=target_lang)
              .order_by(Translation.version.desc()).first())
    ver = (last.version + 1) if last else 1
    db.add(Translation(
        utterance_id=u.id, target_lang=target_lang, version=ver, text=text,
        syllable_count=count_syllables(text, target_lang),
        syllable_ratio=ratio, is_over_limit=ratio > limit,
        llm_model=provider["model"] if provider["mode"] == "live" else "mock-1",
        prompt_version=ctx["prompt_version"],
        is_approved=provider["mode"] == "mock"))


async def _compress_chunk(db: Session, provider: dict, lang_rule: str,
                          utts: list[Utterance], part: list[int],
                          written: dict[int, dict], budget: dict[int, int] | None,
                          target: str, limit: float, ctx: dict) -> bool:
    """一轮压缩：把超限行压缩到预算内，写新版本（没变短不写）。
    返回 False=被内容审查拒绝（上层换下一兜底模型重试）。"""
    lines, pairs = [], []
    for i, idx in enumerate(part, 1):
        lines.append(f"{i} | {written[idx]['text']}")
        if budget:
            pairs.append(f"{i}→{budget[idx]}")
    head = ("时长预算(每行音节上限): " + ", ".join(pairs) + "\n") if pairs else ""
    try:
        out = await chat(provider, f"{_COMP_SYS}\n{lang_rule}",
                         head + "台词:\n" + "\n".join(lines))
    except ContentFilteredError:
        return False
    got = _parse_fixes(out)
    wrote = False
    for i, idx in enumerate(part, 1):
        new_text = (got.get(i) or "").strip()
        if not new_text or _PLACEHOLDER_RE.match(new_text):
            continue
        new_ratio = syllable_ratio(new_text, _src_of(utts[idx]), target)
        if new_ratio >= written[idx]["ratio"]:
            continue                             # 没变短，不值得+1版本
        _write_translation(db, utts[idx], target, new_text, new_ratio, ctx, provider, limit)
        written[idx] = {"text": new_text, "ratio": new_ratio}
        wrote = True
    return True


async def run_translate_scene(db: Session, project: Project, scene_key: str) -> dict:
    """一个场景批的完整五步链（v0.4：子批+审查二分隔离+音节预算+压缩闭环）。
    scene_key 形如 'SC01'（utterance.uid 前缀分组）。"""
    utts = (db.query(Utterance).filter_by(project_id=project.id)
              .order_by(Utterance.seq_index).all())
    in_scene = [u for u in utts if (u.uid or "").startswith(f"{scene_key}-")]
    if in_scene:
        utts = in_scene          # 场景批=uid前缀；无匹配时退化为全项目（单场景项目）
    if not utts:
        raise RuntimeError(f"no utterances for project {project.id}")

    ctx = build_ctx_pack(db, project, utts)
    provider = load_default_provider(db)
    target = ctx["target_lang"]
    target_is_zh = target.startswith("zh")
    limit = (project.config or {}).get("syllable_limit", 1.15)
    batch_size = max(1, int((project.config or {}).get("batch_size", 10)))
    gloss = "\n".join(f"- {k} → {v}" for k, v in ctx["glossary"].items()) or "（无）"
    cards = "；".join(c["role_name"] for c in ctx["role_cards"]) or "（未标注）"
    lang_rule = _lang_rule(target)
    budget = _budget_map(utts, target, limit)

    n = len(utts)
    fallback_chain = load_fallback_providers(db)
    if fallback_chain:
        log.info("fallback chain: %s", [f["model"] for f in fallback_chain])
    # R2 本地化专职模型：链里第一个 DeepSeek（实测习语地道化最强；每场景仅1次调用，9s可接受）
    r2_step_cfg = next((f for f in fallback_chain
                        if "deepseek" in (f.get("model") or "").lower()), None)
    if r2_step_cfg:
        log.info("R2 localizer agent: %s", r2_step_cfg["model"])
    stats = {"filtered_chunks": 0, "fallback_used": 0}
    finals: dict[int, str] = {}
    tail: list[str] = []
    for start in range(0, n, batch_size):
        chunk = list(range(start, min(start + batch_size, n)))
        texts, new_tail = await _run_chunk_filtered(
            provider, ctx, lang_rule, utts, chunk, gloss, cards, budget, tail,
            stats, fallback_chain, r2_step_cfg)
        finals.update(texts)
        if new_tail:
            tail = new_tail

    # 语种校验兜底（场景级，保持原阈值语义）：非中文目标时单句中文比例>30%记坏
    valid = {i: t for i, t in finals.items() if t and not _PLACEHOLDER_RE.match(t)}
    if not target_is_zh and valid:
        zh_bad = sum(1 for t in valid.values() if _zh_ratio(t) > 0.3)
        if zh_bad > n * 0.3:
            raise RuntimeError(
                f"译文语种校验失败：{zh_bad}/{n}句含中文，LLM未遵守目标语种约束")

    isolated = sorted(set(range(n)) - set(valid))
    for i in isolated:
        log.warning("line dropped (no translatable text): uid=%s", utts[i].uid)

    # T214 落库（version化）：只写真实译文，占位符/被滤句一律隔离不写
    written: dict[int, dict] = {}
    for i in sorted(valid):
        ratio = syllable_ratio(valid[i], _src_of(utts[i]), target)
        _write_translation(db, utts[i], target, valid[i], ratio, ctx, provider, limit)
        written[i] = {"text": valid[i], "ratio": ratio}
    db.commit()

    # T220 附加：超限压缩闭环（≤2轮）。
    # 压缩专职模型=Qwen3-235B（实测最精炼 avgR=1.05）；被滤/失败→按兜底链换模型重试同块
    _comp_seen: list[str] = []
    comp_candidates = []
    for c in ([next((f for f in fallback_chain
                     if "qwen" in (f.get("model") or "").lower()), provider)]
              + fallback_chain):
        mkey = c.get("model") or ""
        if mkey not in _comp_seen:
            _comp_seen.append(mkey)
            comp_candidates.append(c)
    rounds = 0
    for _ in range(2):
        over = [i for i in sorted(written) if written[i]["ratio"] > limit]
        if not over:
            break
        rounds += 1
        for start in range(0, len(over), batch_size):
            part = over[start:start + batch_size]
            for cand in comp_candidates:
                ok = await _compress_chunk(db, cand, lang_rule, utts, part, written,
                                           budget, target, limit, ctx)
                if ok:
                    break
        db.commit()

    return {"scene": scene_key, "utterances": len(written),
            "provider": provider["name"], "mode": provider["mode"],
            "prompt_version": ctx["prompt_version"],
            "over_limit": sum(1 for d in written.values() if d["ratio"] > limit),
            "isolated": [{"uid": utts[i].uid, "seq_index": utts[i].seq_index}
                         for i in isolated],
            "filtered_chunks": stats["filtered_chunks"],
            "fallback_used": stats["fallback_used"],
            "compression_rounds": rounds}


async def run_translate_project(project_id: str, scenes: list[str],
                                workers: int | None = None) -> list[dict]:
    """场景级并行翻译（v0.6.1）：workers个并发、每个worker独立DB会话；
    M3限速器全局共享（贴着9次/分跑满），429从源头消失。
    workers默认取 TRANSLATE_WORKERS env（默认8）——配额按发出请求计，
    思考时间服务端重叠，需 in-flight≈9/分×35s≈5.3 个并发才能吃满配额。
    返回逐场景结果；单场景失败不影响其他场景。"""
    import os as _os
    import time as _time
    from .db.session import SessionLocal as _SL
    workers = workers or max(1, int(_os.getenv("TRANSLATE_WORKERS", "8")))
    sem = asyncio.Semaphore(max(1, workers))

    async def _one(sc: str) -> dict:
        async with sem:
            db = _SL()
            try:
                project = db.get(Project, project_id)
                t0 = _time.time()
                info = await run_translate_scene(db, project, sc)
                info["seconds"] = round(_time.time() - t0, 1)
                log.info("scene %s done: utts=%s isolated=%s fallback=%s %.0fs",
                        sc, info.get("utterances"), len(info.get("isolated", [])),
                        info.get("fallback_used"), info.get("seconds", 0))
                return {"scene": sc, "status": "completed", **info}
            except Exception as e:                                 # noqa: BLE001
                log.exception("scene %s failed", sc)
                return {"scene": sc, "status": "failed", "error": str(e)[:300]}
            finally:
                db.close()

    return list(await asyncio.gather(*[_one(sc) for sc in scenes]))


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
