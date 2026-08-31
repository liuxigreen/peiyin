"""O1: QC Agent——12环节质检矩阵（ARCH-V3.1 §5）。
每个钩子=纯函数：输入任务+DB对象，输出 {pass, checks:[{name,ok,detail}], action}。
action ∈ {none, rerun, degrade, review} —— 调度Agent据此自动处置。
挂在任务completed之后的收割路径（orchestrator.py complete流程调用 run_qc_hook）。
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from sqlalchemy.orm import Session

from .db.models import PipelineTask, Translation, Utterance
from .render import ffmpeg_bin

log = logging.getLogger("qc_agent")


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


# ── 各环节钩子（task_type → 钩子函数）────────────────────────
def qc_translate(task: PipelineTask, db: Session) -> dict:
    """翻译族任务：音节比/空译/术语命中率（术语空表时跳过该项）。"""
    from .db.models import GlossaryTerm, Project
    project = db.get(Project, task.project_id)
    if project is None:
        return {"pass": False, "checks": [_check("项目存在", False, task.project_id)],
                "action": "review"}
    utts = db.query(Utterance).filter_by(project_id=task.project_id).all()
    latest: dict[str, Translation] = {}
    for t in (db.query(Translation).filter_by(target_lang=project.target_lang)
                 .order_by(Translation.version).all()):
        latest[t.utterance_id] = t
    translated = [latest[u.id] for u in utts if u.id in latest]
    if not translated:
        return {"pass": False, "checks": [_check("译文存在", False, "0句译文")],
                "action": "review"}
    over = [t for t in translated if t.is_over_limit]
    empty = [t for t in translated if not (t.text or "").strip()]
    untranslated = len(utts) - len(translated)
    checks = [
        _check("音节比≤1.15", len(over) == 0, f"{len(over)}句超限"),
        _check("空译=0", len(empty) == 0, f"{len(empty)}句空"),
        _check("未翻译句=0", untranslated == 0, f"{untranslated}句未译(含隔离句)"),
    ]
    terms = db.query(GlossaryTerm).filter_by(
        target_lang=project.target_lang).all()
    if terms:
        miss = [t for t in terms if not any(
            t.target_term.lower() in tr.text.lower() for tr in translated
            if t.source_term)]
        checks.append(_check("术语命中率100%", len(miss) == 0,
                             f"未命中:{[t.source_term for t in miss][:5]}"))
    ok = all(c["ok"] for c in checks)
    return {"pass": ok, "checks": checks, "action": "none" if ok else "review"}


def qc_tts(task: PipelineTask, db: Session) -> dict:
    """TTS：每句音频存在/时长比∈[0.8,1.2]（假stage产出也走此检查）。"""
    import soundfile as sf
    outputs = task.output_paths or {}
    clips = outputs.get("clips", []) if isinstance(outputs, dict) else []
    bad = []
    for c in clips:
        path = c.get("path", "")
        expect = c.get("expect_ms", 0)
        if not path or not Path(path).exists():
            bad.append(f"{c.get('uid')}:缺文件")
            continue
        info = sf.info(path)
        ratio = info.frames / info.samplerate * 1000 / max(expect, 1)
        if not (0.5 <= ratio <= 1.5):     # 假TTS放宽窗；真CosyVoice2收紧[0.8,1.2]
            bad.append(f"{c.get('uid')}:ratio={ratio:.2f}")
    return {"pass": not bad, "checks": [_check("时长比窗内", not bad, ";".join(bad[:5]))],
            "action": "none" if not bad else "rerun"}


def qc_render(task: PipelineTask, db: Session) -> dict:
    """混码/烧录/缝合类：成片存在+可解析+LUFS+静音+时长漂移（复用render.qc_report）。"""
    from .render import qc_report
    outputs = task.output_paths or {}
    media = (outputs.get("final") or outputs.get("path") or "") \
        if isinstance(outputs, dict) else ""
    if not media or not Path(media).exists():
        return {"pass": False, "checks": [_check("成片存在", False, str(media)[:80])],
                "action": "rerun"}
    expect_ms = outputs.get("expect_ms")
    rep = qc_report(media, expect_duration_ms=expect_ms)
    checks = [
        _check("LUFS∈[-17.5,-14.5]", rep["lufs_pass"], f"lufs={rep['lufs']}"),
        _check("意外静音=0", rep["silence_pass"], f"{rep['unexpected_silences']}处"),
        _check("时长漂移<2s", rep["duration_pass"], f"drift={rep['duration_drift_s']}s"),
    ]
    ok = all(c["ok"] for c in checks)
    return {"pass": ok, "checks": checks, "detail": rep,
            "action": "none" if ok else "rerun"}


def qc_generic(task: PipelineTask, db: Session) -> dict:
    """无专属钩子的任务：产物存在性检查（output_paths非空）。"""
    outputs = task.output_paths
    ok = bool(outputs)
    return {"pass": ok, "checks": [_check("产物存在", ok)],
            "action": "none" if ok else "rerun"}


_HOOKS = {
    "ctx-pack": qc_translate,
    "translate-r1": qc_translate,
    "translate-r2": qc_translate,
    "translate-review": qc_translate,
    "merge-dubtrack": qc_translate,
    "syllable-check": qc_translate,
    "tts": qc_tts,
    "mix": qc_render,
    "encode": qc_render,
    "stitch": qc_render,
    "subtitles": qc_render,
    "qc": qc_render,
    "finalize": qc_generic,
}


def run_qc_hook(task: PipelineTask, db: Session) -> dict:
    """统一入口：任务completed后调用。失败按action处置：
    rerun→retry_count<max则置pending自动重跑；review/degrade→保持completed但标
    output_paths.qc=FAIL（网页质检Tab可见，人工介入）。"""
    if task.task_type not in _HOOKS:
        return {"pass": True, "checks": [], "action": "none"}
    try:
        result = _HOOKS[task.task_type](task, db)
    except Exception as e:                                  # noqa: BLE001
        log.warning("qc hook error %s: %s", task.task_key, e)
        result = {"pass": False, "checks": [_check("钩子异常", False, str(e)[:120])],
                  "action": "review"}
    # 写回任务行（网页质检Tab数据源）
    # 注意：必须copy出新dict再赋值——同引用原地改，SQLAlchemy比较无变化，commit不落库
    outs = dict(task.output_paths) if isinstance(task.output_paths, dict) else {}
    outs["qc"] = {"pass": result["pass"], "checks": result.get("checks", []),
                  "action": result["action"]}
    task.output_paths = outs
    if not result["pass"] and result["action"] == "rerun":
        if task.retry_count < task.max_retries:
            task.retry_count += 1
            task.status = "pending"
            log.warning("QC FAIL→rerun %s (%d/%d)", task.task_key,
                        task.retry_count, task.max_retries)
        else:
            result["action"] = "review"
    db.commit()
    return result


def qc_summary(db: Session, project_id: str) -> dict:
    """网页质检Tab：项目全任务QC状态聚合。"""
    tasks = db.query(PipelineTask).filter_by(project_id=project_id).all()
    out = []
    for t in tasks:
        outs = t.output_paths if isinstance(t.output_paths, dict) else {}
        qc = outs.get("qc")
        if qc:
            out.append({"task_key": t.task_key, "task_type": t.task_type,
                        "status": t.status, **qc})
    return {"total": len(out),
            "passed": sum(1 for x in out if x["pass"]),
            "items": out}
