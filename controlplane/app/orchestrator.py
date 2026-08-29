"""O1/O2: 编排器——幂等实例化DAG + READY扫描 + fake/真实执行收割。
纯DB驱动无内存态：kill即停、重启续跑。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db.models import PipelineTask, Project
from .orchestrator_dag import build_project_dag, TaskRow

log = logging.getLogger("orchestrator")


# ── O1: 幂等写入 ────────────────────────────────────────────
def upsert_tasks(db, project_id: str, rows: list[TaskRow], payload: dict | None = None) -> int:
    """按(project_id,task_key)幂等写任务行：已存在则跳过。返回新增数。"""
    from .orchestrator_dag import compute_input_hash
    inserted = 0
    for r in rows:
        exists = db.query(PipelineTask.id).filter_by(
            project_id=project_id, task_key=r.task_key).first()
        if exists:
            continue
        t = PipelineTask(
            project_id=project_id, task_key=r.task_key, task_type=r.task_type,
            resource=r.resource, weight=r.weight, depends_on=list(r.depends_on),
            gpu_required=(r.resource == "gpu"), model_name=r.model_name,
            input_hash=compute_input_hash(r.task_type, payload or {}),
            status="pending",
        )
        db.add(t)
        inserted += 1
    if inserted:
        db.commit()
    return inserted


def instantiate_for_project(db, project: Project, n_segments: int,
                            scenes: list[str], payload: dict) -> int:
    rows = build_project_dag(n_segments, scenes)
    n = upsert_tasks(db, project.id, rows, payload)
    # 种子资源类型字段也写进通用列 resource? → 复用 task_type 语义即可
    return n


# ── O2: READY扫描 + 推进 ────────────────────────────────────
def ready_scan(db, project_id: str) -> list[PipelineTask]:
    """把依赖全部completed的pending任务标记为queued。返回刚解锁的列表。"""
    tasks = db.query(PipelineTask).filter_by(project_id=project_id).all()
    done = {t.task_key for t in tasks if t.status == "completed"}
    unlocked: list[PipelineTask] = []
    for t in tasks:
        if t.status != "pending":
            continue
        deps = t.depends_on or []
        if all(d in done for d in deps):
            t.status = "queued"
            unlocked.append(t)
    if unlocked:
        db.commit()
    return unlocked


def advance_project_status(db, project_id: str):
    """全链completed→项目完成; 有running/queued→generating; 否则维持。"""
    tasks = db.query(PipelineTask).filter_by(project_id=project_id).all()
    p = db.get(Project, project_id)
    if not p:
        return
    keys = [t.status for t in tasks]
    if tasks and all(s == "completed" for s in keys):
        p.status = "completed"
    elif any(s in ("queued", "running") for s in keys):
        p.status = "processing"
    p.updated_at = datetime.now(timezone.utc)
    db.commit()


# ── 进度聚合(O6数据源) ──────────────────────────────────────
WEIGHT_BY_RESOURCE = {"gpu": 5, "cpu": 2, "io": 1}

def progress_of(db, project_id: str) -> dict:
    tasks = db.query(PipelineTask).filter_by(project_id=project_id).all()
    total = sum(WEIGHT_BY_RESOURCE.get(t.resource, 1) * (t.weight or 50) / 50
                for t in tasks)
    done = sum(WEIGHT_BY_RESOURCE.get(t.resource, 1) * (t.weight or 50) / 50
               for t in tasks if t.status == "completed")
    pct = round(done / total * 100, 1) if total else 0.0
    by_phase = {}
    for t in tasks:
        phase = t.task_key.split("/")[-1][:3] if "/" in t.task_key else t.task_key[:3]
        d = by_phase.setdefault(phase, {"total": 0, "done": 0})
        d["total"] += 1
        d["done"] += (t.status == "completed")
    return {"percent": pct, "phases": by_phase}


# ── 模拟执行器（O2联调）：queued任务跑一拍变completed ────────
async def run_fake_executor_once(db, project_id: str, fail_keys: set[str] | None = None):
    fail_keys = fail_keys or set()
    queued = db.query(PipelineTask).filter_by(
        project_id=project_id, status="queued").all()
    done_keys = []
    for t in queued:
        if t.task_key in fail_keys:
            t.status = "failed"
            t.error_message = "fake executor failure"
        else:
            t.status = "completed"
            # 模拟产物（真实executor写入R2 key列表）
            t.output_paths = [{"key": f"mock/{t.task_key}.out"}]
        done_keys.append(t.task_key)
    if done_keys:
        db.commit()
    return done_keys


async def drive_to_completion(db, project_id: str, max_iters: int = 200,
                              fail_keys: set[str] | None = None):
    """测试驱动器：循环 扫描READY→fake执行→直到无变化或达上限。"""
    for i in range(max_iters):
        unlocked = ready_scan(db, project_id)
        executed = await run_fake_executor_once(db, project_id, fail_keys)
        p = db.get(Project, project_id)
        if p:
            advance_project_status(db, project_id)
            db.refresh(p)
            if p.status == "completed":
                return {"iters": i + 1, "ok": True}
        if not unlocked and not executed:
            return {"iters": i + 1, "ok": False, "stuck": True}
    return {"iters": max_iters, "ok": False, "timeout": True}
