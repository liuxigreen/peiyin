"""O5/O6: 任务重试 + 项目进度聚合 API"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..db.models import PipelineTask, Project
from ..orchestrator_cache import apply_cache_hits

router = APIRouter(prefix="/api", tags=["tasks"])


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, db: Session = Depends(get_db)):
    """复活一个失败/卡死任务及其受影响下游（下游已完成的按input_hash失效）。
    返回 {ok, retried:[keys], cached:[keys]}。"""
    t = db.get(PipelineTask, task_id)
    if not t:
        raise HTTPException(404, "task not found")
    if t.status == "running":
        raise HTTPException(409, "task is running; wait or use force")

    retried: list[str] = [t.task_key]
    t.status = "pending"
    t.retry_count = 0
    t.error_message = None
    t.claimed_by = None
    t.lease_until = None
    # 下游波及：项目内所有 depends_on 含该key 的任务 → 依次置pending并级联
    # (JSON列跨方言安全写法: 取全量后在Python侧做交集, 千级任务量无压力)
    frontier = [t.task_key]
    while frontier:
        fs = set(frontier)
        candidates = (db.query(PipelineTask)
                        .filter(PipelineTask.project_id == t.project_id,
                                PipelineTask.status != "pending")
                        .all())
        nxt = []
        for d in candidates:
            if not (fs & set(d.depends_on or [])):
                continue
            d.status = "pending"
            d.output_paths = None
            d.claimed_by = None
            d.lease_until = None
            retried.append(d.task_key)
            nxt.append(d.task_key)
        frontier = nxt

    # 复活后先过一遍缓存：没实际变化的下游直接命中跳过
    db.commit()  # 级联置pending必须先落库，避免session关闭回滚
    cached = apply_cache_hits(db, t.project_id) if retried else []
    return {"ok": True, "retried": retried,
            "cached_skipped": [k for k in cached if k in set(retried)]}


@router.get("/projects/{pid}/progress")
def project_progress(pid: str, db: Session = Depends(get_db)):
    tasks = db.query(PipelineTask).filter_by(project_id=pid).all()
    # 模式B纯翻译项目：无DAG任务但有utterances → 返回真实翻译进度
    if not tasks:
        from ..db.models import Translation, Utterance
        total = db.query(Utterance).filter_by(project_id=pid).count()
        if total:
            proj = db.get(Project, pid)
            lang = proj.target_lang if proj else "en"
            utts = db.query(Utterance).filter_by(project_id=pid).all()
            utt_ids = {u.id for u in utts}
            done = 0
            seen_utt = set()
            for t in (db.query(Translation).filter_by(target_lang=lang)
                        .order_by(Translation.version).all()):
                if t.utterance_id in utt_ids and t.text and "MISSING" not in t.text:
                    seen_utt.add(t.utterance_id)
            done = len(seen_utt)
            phases = {"translate": {"total": total, "done": done, "failed": 0}}
            return {"percent": round(done / total * 100, 1), "phases": phases,
                    "counts": {"pending": 0, "queued": 0, "running": 0,
                               "completed": done, "failed": 0, "dead": 0},
                    "recent_tasks": [], "mode": "B-translation"}
    W = {"gpu": 5, "cpu": 2, "io": 1}
    total = sum(W.get(t.resource, 1) for t in tasks) or 1
    done_w = sum(W.get(t.resource, 1) for t in tasks if t.status == "completed")
    phases: dict[str, dict] = {}
    stepper = [
        ("pre", {"T010", "T020", "T030", "T040", "T050", "T060"}),
        ("subtitle", {"T110"}), ("separate", {"T120"}),
        ("recognize", {"T130", "T140", "T150"}),
        ("translate", {"T205", "T211", "T212", "T213", "T214", "T220"}),
        ("tts", {"T310", "T320"}), ("mix", {"T330", "T340"}),
        ("stitch", {"T410", "T420", "T430", "T440"}),
    ]
    for name, keys in stepper:
        rel = [t for t in tasks
               if (t.task_key.split("/")[-1] if "/" in t.task_key else t.task_key) in keys]
        phases[name] = {
            "total": len(rel),
            "done": sum(1 for t in rel if t.status == "completed"),
            "failed": sum(1 for t in rel if t.status in ("failed", "dead")),
        }
    recent = sorted(tasks, key=lambda x: x.created_at or "")[-20:]
    return {
        "percent": round(done_w / total * 100, 1),
        "phases": phases,
        "counts": {s: sum(1 for t in tasks if t.status == s)
                   for s in ("pending", "queued", "running",
                             "completed", "failed", "dead")},
        "recent_tasks": [{"key": t.task_key, "type": t.task_type,
                          "status": t.status} for t in recent],
    }


@router.post("/projects/{pid}/instantiate-dag")
def instantiate_dag(pid: str, body: dict, db: Session = Depends(get_db)):
    """预分析产物确认后调用：按切片数/场景数实例化完整DAG。"""
    from ..orchestrator import instantiate_for_project
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404)
    n_seg = int(body.get("n_segments", 1))
    scenes = body.get("scenes") or ["SC01"]
    payload = body.get("payload") or {"n_segments": n_seg}
    n = instantiate_for_project(db, p, n_seg, scenes, payload)
    p.status = "processing"
    db.commit()
    return {"ok": True, "created": n}
