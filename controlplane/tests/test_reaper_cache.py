"""O3+O4测试：lease回收 + 缓存命中跳过"""
import os, sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest


@pytest.fixture()
def db_session():
    from app.db.session import init_db, SessionLocal
    init_db()
    db = SessionLocal()
    yield db
    db.close()


def _mk_project(db):
    from app.db.models import Project
    p = Project(name="t"); db.add(p); db.commit()
    return p


def _mk_task(db, project_id, key, status="running", lease_min=None, retry=0,
             max_retries=3, input_hash=None, outputs=None):
    from app.db.models import PipelineTask
    t = PipelineTask(project_id=project_id, task_key=key, task_type=key.split("/")[-1],
                     resource="gpu", gpu_required=True, weight=50, depends_on=[],
                     status=status, retry_count=retry, max_retries=max_retries,
                     lease_until=datetime.now(timezone.utc) + timedelta(minutes=lease_min)
                     if lease_min is not None else None,
                     input_hash=input_hash, output_paths=outputs)
    db.add(t); db.commit()
    return t


def test_reaper_reclaims_expired(db_session):
    db = db_session
    p = _mk_project(db)
    dead_node_task = _mk_task(db, p.id, "S01/T120", status="running", lease_min=-5)   # 超时
    alive_task = _mk_task(db, p.id, "S02/T120", status="running", lease_min=10)      # 未超时
    maxed = _mk_task(db, p.id, "S01/T130", status="running", lease_min=-5,
                     retry=2, max_retries=2)  # 已耗尽重试次数

    from app.orchestrator_reaper import reap_expired
    reclaimed = reap_expired(db)

    assert set(reclaimed) == {"S01/T120", "S01/T130"}
    db.refresh(dead_node_task); db.refresh(alive_task); db.refresh(maxed)
    assert dead_node_task.status == "pending"       # 回队列等待重派
    assert dead_node_task.retry_count == 1
    assert dead_node_task.claimed_by is None
    assert alive_task.status == "running"           # 不受影响
    assert maxed.status == "dead"                   # 超过重试上限


@pytest.mark.parametrize(
    ("max_retries", "expected_states"),
    [
        (0, [("dead", 0)]),
        (1, [("pending", 1), ("dead", 1)]),
        (2, [("pending", 1), ("pending", 2), ("dead", 2)]),
    ],
    ids=["zero", "one", "two"],
)
def test_pipeline_reaper_retry_budget_matches_explicit_fail(
    db_session, max_retries, expected_states
):
    """Lease expiry follows nodes.fail's retry_count < max_retries contract."""
    db = db_session
    project = _mk_project(db)
    task = _mk_task(
        db, project.id, f"S01/reaper-{max_retries}", lease_min=-1,
        max_retries=max_retries,
    )

    from app.orchestrator_reaper import reap_expired

    for step, (expected_status, expected_retry) in enumerate(expected_states):
        now = datetime.now(timezone.utc)
        if step:
            task.status = "running"
            task.claimed_by = f"owner-{step}"
            task.lease_until = now - timedelta(minutes=1)
            db.commit()

        reclaimed = reap_expired(db, now)
        db.refresh(task)
        assert reclaimed == [task.task_key]
        assert (task.status, task.retry_count) == (expected_status, expected_retry)
        assert task.claimed_by is None and task.lease_until is None


def test_reaper_iteration_logs_errors_and_allows_later_iteration():
    from app import orchestrator_reaper

    class FakeDB:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    databases = [FakeDB(), FakeDB()]
    all_databases = list(databases)
    session_factory = lambda: databases.pop(0)
    logger = Mock()

    original_reap = orchestrator_reaper.reap_expired
    try:
        orchestrator_reaper.reap_expired = Mock(
            side_effect=[RuntimeError("first reap failed"), ["later-task"]]
        )
        orchestrator_reaper._run_reaper_iteration(session_factory, logger)
        orchestrator_reaper._run_reaper_iteration(session_factory, logger)
    finally:
        orchestrator_reaper.reap_expired = original_reap

    logger.error.assert_called_once()
    assert str(logger.error.call_args.args[1]) == "first reap failed"
    logger.warning.assert_called_once_with("reclaimed: %s", ["later-task"])
    assert all(db.closed for db in all_databases)
    assert not databases


def test_cache_hit_skips_completed_work(db_session):
    db = db_session
    p1 = _mk_project(db); p2 = _mk_project(db)
    # 项目1已跑完某TTS任务并产生outputs
    done1 = _mk_task(db, p1.id, "S01/T310", status="completed",
                     input_hash="abc123", outputs=[{"key": "out/a.wav"}])
    # 项目2有同hash的pending任务
    pend2 = _mk_task(db, p2.id, "S01/T310", status="pending", input_hash="abc123")
    # 不同hash的不命中
    pend3 = _mk_task(db, p2.id, "S02/T310", status="pending", input_hash="zzz999")

    from app.orchestrator_cache import apply_cache_hits
    hits = apply_cache_hits(db, p2.id)

    assert hits == ["S01/T310"]
    db.refresh(pend2); db.refresh(pend3); db.refresh(done1)
    assert pend2.status == "completed"
    assert pend2.output_paths == [{"key": "out/a.wav"}]   # 复用产物
    assert pend3.status == "pending"                       # hash不同不命中
