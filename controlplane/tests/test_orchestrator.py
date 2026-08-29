"""O1+O2 联调测试：DAG构建→幂等落库→fake驱动全通→断言状态。
跑法: cd controlplane && .venv/bin/python -m pytest tests/test_orchestrator.py -q
"""
import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    # 文件型临时库：TestClient(多线程)与SessionLocal共享同一份
    dbfile = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{dbfile}")
    import importlib
    import app.db.session as dbsession
    importlib.reload(dbsession)
    from app.db.models import Base
    Base.metadata.create_all(dbsession.engine)
    import app.main as appmain
    importlib.reload(appmain)
    with TestClient(appmain.app) as c:
        yield c, dbsession.SessionLocal


def _make_project(client):
    r = client.post("/api/projects", json={"name": "编排联调剧", "target_lang": "en"})
    return r.json()["id"]


def test_dag_build_shape():
    from app.orchestrator_dag import build_project_dag
    rows = build_project_dag(n_segments=3, scenes=["SC01", "SC02"])
    keys = {r.task_key for r in rows}
    assert "T010" in keys and "T440" in keys
    assert "S01/T120" in keys and "S03/T310" in keys
    # 收尾依赖所有切片T340
    t410 = next(r for r in rows if r.task_key == "T410")
    assert len(t410.depends_on) == 3 and all(k.endswith("T340") for k in t410.depends_on)
    # TTS依赖所有场景的syllable-check
    s1t = next(r for r in rows if r.task_key == "S01/T310")
    assert set(s1t.depends_on) == {"SC01/T220", "SC02/T220"}


def test_idempotent_upsert(client):
    c, SessionLocal = client
    pid = _make_project(c)
    from app.orchestrator import instantiate_for_project
    db = SessionLocal()
    n1 = instantiate_for_project(db, db.get(__import__("app.db.models", fromlist=["Project"]).Project, pid),
                                 n_segments=3, scenes=["SC01"], payload={"seg": 3})
    n2 = instantiate_for_project(db, db.get(__import__("app.db.models", fromlist=["Project"]).Project, pid),
                                 n_segments=3, scenes=["SC01"], payload={"seg": 3})
    total_expected = 6 + 3 * 9 + 5 + 1 + 4   # 全片6+切片9×3+场景5行(T205,T211,T212,T214,T220)+T213全局+收尾4
    assert n1 == total_expected, f"首次应插{total_expected}, 实际{n1}"
    assert n2 == 0, f"重复实例化应为0新增, 实际{n2}"


def test_full_dag_completes_and_cascades(client):
    c, SessionLocal = client
    pid = _make_project(c)
    from app.db import models as m
    from app.orchestrator import (instantiate_for_project, drive_to_completion,
                                  progress_of)
    db = SessionLocal()
    proj = db.get(m.Project, pid)
    instantiate_for_project(db, proj, n_segments=3,
                            scenes=["SC01", "SC02"], payload={"x": 1})
    result = asyncio_run(drive_to_completion(db, pid))
    assert result["ok"], f"DAG卡死: {result}"

    tasks = db.query(m.PipelineTask).filter_by(project_id=pid).all()
    assert all(t.status == "completed" for t in tasks), \
        [t.task_key for t in tasks if t.status != "completed"]
    prog = progress_of(db, pid)
    assert prog["percent"] == 100.0


def test_blocked_dependency_stays_pending(client):
    """失败的上游→下游永不ready（不被误执行）"""
    c, SessionLocal = client
    pid = _make_project(c)
    from app.db import models as m
    from app.orchestrator import instantiate_for_project, drive_to_completion
    db = SessionLocal()
    proj = db.get(m.Project, pid)
    instantiate_for_project(db, proj, 2, ["SC01"], {})
    res = asyncio_run(drive_to_completion(db, pid, fail_keys={"S01/T110"}))
    assert not res["ok"]  # 应卡死而非全完成
    t130 = db.query(m.PipelineTask).filter_by(project_id=pid, task_key="S01/T130").first()
    assert t130.status == "pending"
    t120 = db.query(m.PipelineTask).filter_by(project_id=pid, task_key="S01/T120").first()
    assert t120.status == "pending"


def asyncio_run(coro):
    import anyio
    return anyio.run(lambda: coro) if False else __import__("asyncio").run(coro)
