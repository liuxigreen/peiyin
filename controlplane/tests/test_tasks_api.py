"""O5+O6 API级联测：instantiate→retry级联→progress聚合"""
import os, sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")

import pytest


@pytest.fixture()
def client():
    import importlib
    import app.db.session as dbsession
    importlib.reload(dbsession)
    from app.db.models import Base
    Base.metadata.create_all(dbsession.engine)
    import app.main as appmain
    importlib.reload(appmain)
    from fastapi.testclient import TestClient
    with TestClient(appmain.app) as c:
        yield c


def _seed_dag(client):
    r = client.post("/api/projects", json={"name": "O5O6"})
    pid = r.json()["id"]
    r = client.post(f"/api/projects/{pid}/instantiate-dag",
                    json={"n_segments": 2, "scenes": ["SC01"]})
    assert r.json()["ok"], r.text
    return pid


def test_instantiate_and_progress_shape(client):
    pid = _seed_dag(client)
    r = client.get(f"/api/projects/{pid}/progress")
    d = r.json()
    assert d["percent"] == 0.0
    assert set(d["phases"].keys()) >= {"pre", "translate", "tts", "stitch"}
    assert d["counts"]["pending"] == 34   # 2seg: 全片6+切片18+场景5+T213全局1+收尾4


def test_retry_cascades_downstream(client):
    pid = _seed_dag(client)
    # 模拟: S01/T120 及其下游已完成, 然后"发现"T120需要重跑
    from app.db.session import SessionLocal
    db = SessionLocal()
    from app.db.models import PipelineTask
    done_keys = []
    for i in range(60):
        from app.orchestrator import ready_scan, run_fake_executor_once
        import asyncio
        u = ready_scan(db, pid)
        e = asyncio.run(run_fake_executor_once(db, pid))
        if not u and not e:
            break
    total = db.query(PipelineTask).filter_by(project_id=pid).count()
    assert db.query(PipelineTask).filter_by(project_id=pid, status="completed").count() == total

    t120 = db.query(PipelineTask).filter_by(project_id=pid, task_key="S01/T120").first()
    tid = t120.id
    db.close()

    r = client.post(f"/api/tasks/{tid}/retry")
    d = r.json()
    assert d["ok"]
    # 波及: S01/T130,S01/T140,S01/T150,S01/T310..T340 + SC/T210,T220? (T220依赖SC01/T210,SC01/T210依赖两个T150)
    # SC01/T210依赖S01/T150+S02/T150 → 也被波及; S02链不受影响
    assert "S01/T120" in d["retried"]
    assert "S02/T120" not in d["retried"]
    # 五步链: T213全局依赖所有SC的T212 → S01/T212变化波及T213 → T214/T220
    assert any("T213" == k for k in d["retried"])
    assert any("SC01/T220" == k for k in d["retried"])
    # 被波及的下游应有缓存命中跳过（它们input_hash没变,同项目已有completed记录）
    assert len(d["cached_skipped"]) > 0
