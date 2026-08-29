"""O4: 内容寻址缓存。input_hash相同的completed任务→新任务行直接标completed复用。
用途：改一处参数重跑项目时，没变的环节秒级跳过。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .db.models import PipelineTask


def apply_cache_hits(db: Session, project_id: str) -> list[str]:
    """对本项目pending任务做一次缓存匹配：同task_type+input_hash已在其他(或本项目)
    项目completed且输出文件齐全 → 直接复制output_paths标记completed。
    返回命中的task_key列表。文件存在性由调用方在真实executor里校验(模拟环境跳过)。"""
    hit_keys: list[str] = []
    pendings = (db.query(PipelineTask)
                  .filter_by(project_id=project_id, status="pending")
                  .all())
    for t in pendings:
        if not t.input_hash:
            continue
        done = (db.query(PipelineTask)
                  .filter(PipelineTask.input_hash == t.input_hash,
                          PipelineTask.status == "completed",
                          PipelineTask.id != t.id)
                  .order_by(PipelineTask.completed_at.desc())
                  .first()) if hasattr(PipelineTask, "completed_at") else                (db.query(PipelineTask)
                  .filter(PipelineTask.input_hash == t.input_hash,
                          PipelineTask.status == "completed",
                          PipelineTask.id != t.id)
                  .first())
        if done and done.output_paths:
            t.status = "completed"
            t.output_paths = done.output_paths
            t.output_hash = done.output_hash
            hit_keys.append(t.task_key)
    if hit_keys:
        db.commit()
    return hit_keys
