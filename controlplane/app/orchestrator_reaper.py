"""O3: lease回收器。GPU节点死亡/失联后，其running任务超时回队列。
纯DB查询实现，控制面启动时作为后台线程每60s跑一次。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update

LEASE_MINUTES = 10


def _reaper_error(current: str | None, suffix: str) -> str:
    """给 NodeJob 追加回收原因，同时满足 500 字符协议上限。"""
    return ((current or "") + suffix)[:500]


def _reap_node_jobs(db, now: datetime) -> list[str]:
    """CAS 回收 NodeJob；调用方负责提交事务。

    先读取候选再按 ``status/running + lease_until`` 条件更新，多个 reaper
    并发时只有首个更新成功的事务会递增 retry_count。
    """
    from .db.models import NodeJob

    expired = (db.query(NodeJob.id, NodeJob.retry_count, NodeJob.max_retries,
                        NodeJob.error)
                 .filter(NodeJob.status == "running",
                         NodeJob.lease_until.isnot(None),
                         NodeJob.lease_until < now)
                 .all())
    reclaimed: list[str] = []
    for job_id, retry_count, max_retries, error in expired:
        next_retry = (retry_count or 0) + 1
        retry_limit = max_retries if max_retries is not None else 3
        terminal = next_retry >= retry_limit
        status = "dead" if terminal else "pending"
        suffix = (" [lease expired beyond max retries]"
                  if terminal else " [lease expired, reclaimed]")
        result = db.execute(
            update(NodeJob)
            .where(NodeJob.id == job_id,
                   NodeJob.status == "running",
                   NodeJob.lease_until.isnot(None),
                   NodeJob.lease_until < now)
            .values(status=status,
                    claimed_by=None,
                    lease_until=None,
                    retry_count=next_retry,
                    error=_reaper_error(error, suffix),
                    updated_at=func.now())
        )
        if result.rowcount:
            reclaimed.append(job_id)
    return reclaimed


def reap_node_jobs(db, now: datetime | None = None) -> list[str]:
    """回收过期 NodeJob 并返回实际更新的 job id 列表。"""
    now = now or datetime.now(timezone.utc)
    reclaimed = _reap_node_jobs(db, now)
    if reclaimed:
        db.commit()
    return reclaimed


def reap_expired(db, now: datetime | None = None) -> list[str]:
    """把lease_until过期的running任务回收为pending（或超max_retries→dead）。
    返回被回收的task_key列表。"""
    now = now or datetime.now(timezone.utc)
    from .db.models import PipelineTask
    expired = (db.query(PipelineTask)
                 .filter(PipelineTask.status == "running",
                         PipelineTask.lease_until.isnot(None),
                         PipelineTask.lease_until < now)
                 .all())
    reclaimed: list[str] = []
    for t in expired:
        t.claimed_by = None
        t.lease_until = None
        t.retry_count = (t.retry_count or 0) + 1
        if t.retry_count >= (t.max_retries or 3):
            t.status = "dead"
            t.error_message = ((t.error_message or "") +
                               " [lease expired beyond max retries]")[:500]
        else:
            t.status = "pending"
            t.error_message = ((t.error_message or "") +
                               " [lease expired, reclaimed]")[:500]
        reclaimed.append(t.task_key)
    # NodeJob 使用独立表和相同 lease 时钟；PipelineTask 上面的逻辑保持原样。
    node_reclaimed = _reap_node_jobs(db, now)
    if reclaimed or node_reclaimed:
        db.commit()
    return reclaimed + node_reclaimed


def start_background_reaper(session_factory, interval_s: int = 60):
    """FastAPI startup时调用；daemon线程随主进程退出。"""
    import threading, time

    def _loop():
        while True:
            time.sleep(interval_s)
            db = session_factory()
            try:
                keys = reap_expired(db)
                if keys:
                    import logging
                    logging.getLogger("reaper").warning("reclaimed: %s", keys)
            except Exception as e:
                logging.getLogger("reaper").error("reap error: %s", e)
            finally:
                db.close()

    th = threading.Thread(target=_loop, daemon=True, name="lease-reaper")
    th.start()
    return th
