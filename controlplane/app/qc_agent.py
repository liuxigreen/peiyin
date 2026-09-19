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

from .db.models import PipelineTask, Translation, TtsClip, Utterance
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
    from .translate_executor import is_placeholder
    latest: dict[str, Translation] = {}
    for t in (db.query(Translation).filter_by(target_lang=project.target_lang)
                 .order_by(Translation.version).all()):
        if is_placeholder(t.text or ""):
            continue                     # 占位行视同不存在：隔离句=未翻译，QC拦住
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
    """TTS：只接受已落盘、已关联 utterance 的真实音频。

    ``complete`` 可以早于 artifact 上传，因此在 clip 尚未落库时必须明确
    失败且保持 completed，允许节点继续上传；上传成功后由节点端点再次调用
    本钩子，以音频实际时长与所属 utterance 的时窗比较。
    """
    import soundfile as sf
    outputs = task.output_paths or {}
    payload = outputs.get("payload") if isinstance(outputs, dict) else None
    uid_ = payload.get("uid") if isinstance(payload, dict) else None
    utterance = (db.query(Utterance)
                 .filter_by(project_id=task.project_id, uid=uid_).first()) if uid_ else None
    window_ms = ((utterance.end_ms or 0) - (utterance.start_ms or 0)) if utterance else 0
    artifacts = outputs.get("artifacts") or [] if isinstance(outputs, dict) else []
    artifact_paths = {
        item.get("path") for item in artifacts if isinstance(item, dict)
        and isinstance(item.get("path"), str)
    }
    clips = (db.query(TtsClip)
             .filter_by(utterance_id=utterance.id, status="completed").all()) if utterance else []
    clip = next((row for row in clips if row.audio_r2_key in artifact_paths), None)

    checks = [
        _check("utterance时窗有效", window_ms > 0,
               f"uid={uid_ or '-'}, window_ms={window_ms}"),
        _check("真实artifact与TtsClip已落盘", clip is not None,
               f"artifacts={len(artifact_paths)}, clips={len(clips)}"),
    ]
    actual_ms = 0
    if clip is not None:
        path = clip.audio_r2_key
        try:
            info = sf.info(path)
            actual_ms = int(info.frames / info.samplerate * 1000)
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("音频可解析", False, str(exc)[:120]))
        else:
            checks.append(_check("音频可解析", actual_ms > 0,
                                 f"path={path}, actual_ms={actual_ms}"))
    else:
        checks.append(_check("音频可解析", False, "尚无已关联的真实artifact"))
    ratio = actual_ms / window_ms if window_ms > 0 else 0
    checks.append(_check("实际时长比∈[0.8,1.2]", 0.8 <= ratio <= 1.2,
                         f"actual_ms={actual_ms}, window_ms={window_ms}, ratio={ratio:.3f}"))
    ok = all(check["ok"] for check in checks)
    # 失败时不重入队：节点完成后仍须能够回传或替换控制面 artifact。
    return {"pass": ok, "checks": checks, "action": "none" if ok else "review"}


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
    "tts-generate": qc_tts,
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
