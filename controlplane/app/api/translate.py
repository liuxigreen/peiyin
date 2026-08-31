"""翻译链路 API：SRT种子导入 + 翻译运行器（驱动DAG中translate任务到真executor）"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..db.models import (PipelineTask, Project, Segment, Translation,
                         TranslationProvider, Utterance)
from ..db.session import get_db
from ..translate_executor import (execute_translate_task,
                                  load_default_provider,
                                  run_translate_scene)

router = APIRouter(prefix="/api", tags=["translate"])


_SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")


def _parse_srt(text: str) -> list[dict]:
    """极简SRT解析：返回 [{start_ms,end_ms,text}]"""
    entries: list[dict] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for b in blocks:
        lines = [l for l in b.splitlines() if l.strip()]
        if not lines:
            continue
        tmatch = None
        for i, l in enumerate(lines):
            if "-->" in l:
                tmatch = (i, l)
                break
        if tmatch is None:
            continue
        i, tline = tmatch
        m = _SRT_TIME.search(tline)
        if not m:
            continue
        h, mi, s, ms, h2, mi2, s2, ms2 = (int(x) for x in m.groups())
        body = " ".join(lines[i + 1:]).strip()
        # 剥离SRT内嵌HTML标签（<b>/<i>/<font>等——白月光实测发现大量<b>残留）
        body = re.sub(r"</?[a-zA-Z][^>]*>", "", body).strip()
        if body:
            # 评审D6修复：end_ms读SRT真实结束时间（原+2000写死使fit窗口失真）
            entries.append({"start_ms": ((h * 60 + mi) * 60 + s) * 1000 + ms,
                            "end_ms": ((h2 * 60 + mi2) * 60 + s2) * 1000 + ms2,
                            "text": body})
    return entries


def _seed_utterances(db: Session, project: Project, srt_text: str,
                     scene_size: int = 40) -> tuple[int, list[str]]:
    """SRT文本→utterances落库（共享逻辑：seed-srt端点与upload-complete引导链共用）。
    返回 (句数, 场景列表)。"""
    entries = _parse_srt(srt_text)
    if not entries:
        raise HTTPException(400, "no subtitle entries parsed from srt")
    seg = Segment(project_id=project.id, seg_index=1, start_ms=entries[0]["start_ms"],
                  end_ms=entries[-1]["end_ms"], status="completed")
    db.add(seg)
    db.flush()
    scene_of = lambda i: f"SC{i // scene_size + 1:02d}"
    scenes: list[str] = []
    for i, e in enumerate(entries):
        u = Utterance(project_id=project.id, segment_id=seg.id,
                      uid=f"{scene_of(i)}-{i+1:04d}",
                      seq_index=i + 1, start_ms=e["start_ms"], end_ms=e["end_ms"],
                      original_text=e["text"], merged_text=e["text"],
                      merged_confidence=1.0, char_count=len(e["text"]))
        db.add(u)
        sc = scene_of(i)
        if sc not in scenes:
            scenes.append(sc)
    db.commit()
    return len(entries), scenes


@router.post("/projects/{pid}/seed-srt")
def seed_srt(pid: str, body: dict, db: Session = Depends(get_db)):
    """从SRT文本导入台词为utterances（识别阶段的替代种子，联调翻译链用）。
    body: {srt: "...", scene_size?: 每场景句数(默认40)}"""
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404, "project not found")
    n, scenes = _seed_utterances(db, p, body.get("srt", ""),
                                 int(body.get("scene_size", 40)))
    return {"ok": True, "utterances": n, "scenes": scenes}


@router.get("/projects/{pid}/translate-plan")
def translate_plan(pid: str, db: Session = Depends(get_db)):
    """预览翻译执行计划：场景分组、句数、当前provider、是否mock。"""
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404, "project not found")
    utts = db.query(Utterance).filter_by(project_id=pid).order_by(Utterance.seq_index).all()
    groups: dict[str, int] = {}
    for u in utts:
        sc = (u.uid or "").split("-")[0] or "SC01"
        groups[sc] = groups.get(sc, 0) + 1
    prov = load_default_provider(db)
    return {"project": p.name, "target_lang": p.target_lang,
            "provider": {"name": prov["name"], "mode": prov["mode"], "model": prov["model"]},
            "scenes": groups, "total_utterances": len(utts)}


@router.post("/projects/{pid}/run-translate")
async def run_translate(pid: str, simulate_upstream: bool = False,
                        db: Session = Depends(get_db)):
    """跑通翻译链：对项目内queued/待处理的translate任务逐场景执行真executor。
    无DAG任务时按utterances场景分组直接执行（轻量路径）。
    simulate_upstream=true：GPU前置任务（T1xx等）未跑时自动fake完成——
    仅用于无GPU阶段的链路联调，生产一律false。"""
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404, "project not found")
    if not db.query(Utterance).filter_by(project_id=pid).first():
        raise HTTPException(400, "no utterances; POST seed-srt first")

    tasks = (db.query(PipelineTask)
               .filter(PipelineTask.project_id == pid,
                       PipelineTask.task_type.in_(
                           ("ctx-pack", "translate-r1", "translate-r2",
                            "translate-review", "merge-dubtrack", "syllable-check")),
                       PipelineTask.status.in_(("queued", "running", "pending")))
               .all())
    results = []
    FAMILY = ("ctx-pack", "translate-r1", "translate-r2",
              "translate-review", "merge-dubtrack", "syllable-check")
    if tasks:
        from ..orchestrator import ready_scan, run_fake_executor_once
        # simulate_upstream：无GPU联调模式下，fake完成翻译链的所有上游依赖
        if simulate_upstream:
            for _ in range(60):
                ready_scan(db, pid)
                done = await run_fake_executor_once(db, pid)
                if not done:
                    break
            # fake executor会把翻译族任务也一并完成——复位，翻译链必须真跑。
            # 注意：必须逐对象赋值——bulk update绕过identity map，
            # 且SessionLocal是expire_on_commit=False，后续查询会读到脏缓存
            fam_rows = (db.query(PipelineTask)
                          .filter(PipelineTask.project_id == pid,
                                  PipelineTask.task_type.in_(FAMILY)).all())
            for t in fam_rows:
                t.status = "pending"
                t.output_paths = None
                t.claimed_by = None
                t.lease_until = None
                t.error_message = None
            db.commit()
        # DAG路径：场景分组推进。链是批处理语义——场景内首个ready任务触发整链，
        # 完成后该场景全部family任务一并标记completed（避免6次重复LLM调用）。
        done_scenes: set[str] = set()
        for _ in range(len(tasks) + 2):
            ready_scan(db, pid)
            ready = (db.query(PipelineTask)
                       .filter(PipelineTask.project_id == pid,
                               PipelineTask.status == "queued",
                               PipelineTask.task_type.in_(FAMILY))
                       .all())
            if not ready:
                break
            by_scene: dict[str, list] = {}
            for t in ready:
                sc = t.task_key.split("/")[0] if "/" in t.task_key else "__global__"
                by_scene.setdefault(sc, []).append(t)
            for sc, ts in by_scene.items():
                if sc == "__global__":
                    # T213跨批终检已在各场景链内执行过——直接标记完成
                    for t in ts:
                        t.status = "completed"
                        t.output_paths = {"scene": "review", "mode": "in-chain"}
                    db.commit()
                    results.append({"task": "T213", "status": "completed",
                                    "scene": "review", "mode": "in-chain"})
                    continue
                if sc in done_scenes:
                    # 同场景下游登记任务（T214/T220）已在链内完成——登记后放行
                    for t in ts:
                        t.status = "completed"
                        t.output_paths = {"scene": sc, "mode": "in-chain"}
                    db.commit()
                    continue
                t0 = ts[0]
                t0.status = "running"
                db.commit()
                entry: dict = {"task": t0.task_key, "scene": sc}
                try:
                    info = await run_translate_scene(db, p, sc)
                    for t in ts:
                        t.status = "completed"
                        # JSON列大坑：必须整体新dict赋值，不能同引用原地改
                        t.output_paths = {"scene": sc, "utterances": info["utterances"],
                                          "mode": info["mode"],
                                          "isolated": info.get("isolated", []),
                                          "filtered_chunks": info.get("filtered_chunks", 0)}
                    db.commit()
                    from ..qc_agent import run_qc_hook
                    run_qc_hook(t0, db)   # QC Agent：场景批代表行跑质检钩子
                    entry.update({"status": "completed", "utterances": info["utterances"],
                                  "mode": info["mode"],
                                  "isolated": info.get("isolated", []),
                                  "filtered_chunks": info.get("filtered_chunks", 0),
                                  "compression_rounds": info.get("compression_rounds", 0)})
                    done_scenes.add(sc)
                except Exception as e:                     # noqa: BLE001
                    msg = str(e)[:500]
                    t0.status = "failed"
                    t0.error_message = msg
                    entry.update({"status": "failed", "error": msg[:200]})
                db.commit()
                results.append(entry)
    else:
        # 轻量路径：无DAG，按场景分组直接跑
        from ..db.models import PipelineTask as PT
        from ..qc_agent import run_qc_hook
        import datetime as _dt
        utts = db.query(Utterance).filter_by(project_id=pid).all()
        scenes = sorted({(u.uid or "SC01").split("-")[0] for u in utts})
        for sc in scenes:
            try:
                info = await run_translate_scene(db, p, sc)
                # 造影子任务行承载QC结果（轻量路径无DAG行；幂等：同key复用）
                shadow = (db.query(PT)
                            .filter_by(project_id=pid, task_key=f"{sc}/translate")
                            .first())
                if shadow is None:
                    shadow = PT(project_id=pid, task_key=f"{sc}/translate",
                                task_type="translate-r1", resource="io",
                                gpu_required=False, status="completed")
                    db.add(shadow)
                shadow.status = "completed"
                shadow.output_paths = {"scene": sc, "utterances": info["utterances"],
                                       "mode": info["mode"],
                                       "isolated": info.get("isolated", []),
                                       "filtered_chunks": info.get("filtered_chunks", 0)}
                db.commit()
                qc = run_qc_hook(shadow, db)
                results.append({"task": f"{sc}/translate", "status": "completed",
                                "qc": qc["pass"], **info})
            except Exception as e:                         # noqa: BLE001
                results.append({"task": f"{sc}/translate", "status": "failed",
                                "error": str(e)[:200]})
    ok = sum(1 for r in results if r["status"] == "completed")
    return {"ok": ok > 0 and ok == len(results), "ran": len(results),
            "completed": ok, "results": results}


@router.post("/projects/{pid}/upload-complete")
async def upload_complete(pid: str, body: dict, db: Session = Depends(get_db)):
    """N1引导链：母片/SRT上传完成后的自动启动入口（V3 §7.5）。
    body: {r2_key?: 母片key, srt?: 字幕文本, scene_size?: 40}
    当前阶段（识别未接GPU）：srt存在→种子+自动instantiate+自动开翻；
    仅母片→记r2_key并建T010 probe种子（GPU阶段N5接probe真实现）。
    返回 {ok, mode: full|queued|probe-seeded, scenes, dag_created}"""
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404, "project not found")
    if body.get("r2_key"):
        p.source_r2_key = body["r2_key"]
        db.commit()
    srt = body.get("srt")
    if not srt:
        return {"ok": True, "mode": "probe-seeded",
                "note": "母片已登记，识别链接GPU后自动出台词(N5)"}
    n, scenes = _seed_utterances(db, p, srt, int(body.get("scene_size", 40)))
    from ..orchestrator import instantiate_for_project
    payload = {"n_segments": 1, "scenes": scenes, "seed": "srt-upload"}
    created = instantiate_for_project(db, p, 1, scenes, payload)
    p.status = "processing"
    db.commit()
    # 自动开翻：DAG路径run-translate，无GPU上游→simulate_upstream=true（当前阶段语义）
    result = await run_translate(pid=pid, simulate_upstream=True, db=db)
    return {"ok": result["ok"], "mode": "full", "utterances": n, "scenes": scenes,
            "dag_created": created, "translate": result}


def _latest_translations(db, utt_id: str, lang: str):
    return (db.query(Translation)
              .filter_by(utterance_id=utt_id, target_lang=lang)
              .order_by(Translation.version.desc()).first())
