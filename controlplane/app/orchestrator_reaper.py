"""O3: lease回收器。GPU节点死亡/失联后，其running任务超时回队列。
纯DB查询实现，控制面启动时作为后台线程每60s跑一次。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

LEASE_MINUTES = 10


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
    if reclaimed:
        db.commit()
    return reclaimed


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
