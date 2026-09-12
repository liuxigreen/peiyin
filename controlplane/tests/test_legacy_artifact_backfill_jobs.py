"""Exact-node NodeJob coverage for one legacy vocals backfill."""
from __future__ import annotations

import copy
import importlib
import pathlib
import sys

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect


sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
ADMIN = {"Authorization": "Bearer node-job-admin"}
NODE_SECRET = {"x-node-secret": "dev-node-secret"}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobs.db'}")
    monkeypatch.setenv("MODE_B_STORAGE", str(tmp_path / "storage"))
    monkeypatch.setenv("API_TOKEN", "node-job-admin")
    monkeypatch.setenv("NODE_SHARED_SECRET", "dev-node-secret")
    import app.db.session as session_mod
    import app.api.nodes as nodes_mod
    import app.api.node_jobs as jobs_mod
    import app.main as main_mod

    importlib.reload(session_mod)
    importlib.reload(nodes_mod)
    importlib.reload(jobs_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app), session_mod.SessionLocal


def _register(client: TestClient, name: str) -> str:
    response = client.post("/api/nodes/register", headers=NODE_SECRET,
                           json={"name": name, "gpu_model": "test"})
    assert response.status_code == 200, response.text
    return response.json()["node_token"]


def _node_id(SessionLocal, token: str) -> str:
    import hashlib
    from app.db.models import GpuNode
    db = SessionLocal()
    try:
        return db.query(GpuNode).filter_by(
            token_hash=hashlib.sha256(token.encode()).hexdigest()).one().id
    finally:
        db.close()


def _source(client: TestClient, SessionLocal, *, with_artifact=False):
    project_id = client.post("/api/projects", headers=ADMIN,
                             json={"name": "legacy job", "target_lang": "en"}).json()["id"]
    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        output_paths = {"outputs": [{"key": "vocals", "path": "/node/legacy/vocals.wav"}]}
        if with_artifact:
            output_paths["artifacts"] = [{"key": "vocals", "filename": "vocals.wav"}]
        task = PipelineTask(project_id=project_id, task_key=f"SEPARATE/{project_id[:8]}",
                            task_type="separate-vocals", status="completed",
                            output_paths=output_paths)
        db.add(task)
        db.commit()
        return project_id, task.id
    finally:
        db.close()


def _job(client, source_task_id, target_node_id):
    response = client.post("/api/node-jobs/legacy-vocals-backfill", headers=ADMIN,
                           json={"source_task_id": source_task_id,
                                 "target_node_id": target_node_id})
    assert response.status_code == 200, response.text
    return response.json()


def test_exact_node_claim_upload_then_complete_creates_one_handoff(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    target_token = _register(client, "same-name")
    other_token = _register(client, "same-name")
    target_id = _node_id(SessionLocal, target_token)
    project_id, task_id = _source(client, SessionLocal)
    job = _job(client, task_id, target_id)
    assert job["target_node_id"] == target_id
    assert job["kind"] == "legacy-vocals-backfill" and job["spec_version"] == "1"
    assert job["params"] == {"source_task_id": task_id, "source_path": "/node/legacy/vocals.wav",
                              "key": "vocals", "filename": "vocals.wav"}

    other = {"Authorization": f"Bearer {other_token}"}
    assert client.post("/api/nodes/jobs/claim", headers=other).json()["job"] is None
    target = {"Authorization": f"Bearer {target_token}"}
    claimed = client.post("/api/nodes/jobs/claim", headers=target).json()["job"]
    assert claimed["id"] == job["id"]

    uploaded = client.post(f"/api/nodes/tasks/{task_id}/artifact-backfill", headers=target,
                           params={"job_id": job["id"], "key": "vocals", "filename": "vocals.wav"},
                           content=b"backfilled")
    assert uploaded.status_code == 200, uploaded.text
    from app.db.models import NodeJob, PipelineTask
    db = SessionLocal()
    try:
        source = db.get(PipelineTask, task_id)
        assert source.status == "completed"
        assert source.output_paths["artifacts"][0]["key"] == "vocals"
        assert db.query(PipelineTask).filter_by(project_id=project_id, task_type="diarize").count() == 0
        assert db.get(NodeJob, job["id"]).checkpoint["artifact"]["bytes"] == len(b"backfilled")
    finally:
        db.close()

    complete = client.post(f"/api/nodes/jobs/{job['id']}/complete", headers=target, json={})
    assert complete.status_code == 200, complete.text
    db = SessionLocal()
    try:
        assert db.get(NodeJob, job["id"]).status == "completed"
        assert db.query(PipelineTask).filter_by(project_id=project_id, task_type="diarize").count() == 1
    finally:
        db.close()


def test_invalid_or_unclaimed_job_cannot_mutate_source(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    target_token = _register(client, "target")
    other_token = _register(client, "other")
    _project_id, task_id = _source(client, SessionLocal)
    job = _job(client, task_id, _node_id(SessionLocal, target_token))
    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        before = copy.deepcopy(db.get(PipelineTask, task_id).output_paths)
    finally:
        db.close()
    response = client.post(f"/api/nodes/tasks/{task_id}/artifact-backfill",
                           headers={"Authorization": f"Bearer {other_token}"},
                           params={"job_id": job["id"], "key": "vocals", "filename": "vocals.wav"},
                           content=b"blocked")
    assert response.status_code == 409
    db = SessionLocal()
    try:
        assert db.get(PipelineTask, task_id).output_paths == before
    finally:
        db.close()


def test_legacy_job_creation_rejects_non_unique_or_existing_vocals(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    token = _register(client, "target")
    target_id = _node_id(SessionLocal, token)
    _project_id, task_id = _source(client, SessionLocal, with_artifact=True)
    response = client.post("/api/node-jobs/legacy-vocals-backfill", headers=ADMIN,
                           json={"source_task_id": task_id, "target_node_id": target_id})
    assert response.status_code == 409


def test_target_node_id_migration_is_repeat_safe_and_non_destructive(tmp_path):
    import migrate_node_jobs_target_node_id as migration
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE gpu_nodes (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE node_jobs (id VARCHAR(36) PRIMARY KEY, "
            "target_node_name VARCHAR(100), status VARCHAR(20), created_at DATETIME)"
        )
    migration.migrate(engine)
    migration.migrate(engine)
    assert migration._REQUIRED_COLUMNS <= {
        column["name"] for column in inspect(engine).get_columns("node_jobs")
    }
    assert migration._REQUIRED_INDEXES <= {
        index["name"] for index in inspect(engine).get_indexes("node_jobs")
    }
    source = pathlib.Path(migration.__file__).read_text()
    assert "DROP TABLE" not in source
    assert "ALTER TABLE node_jobs RENAME" not in source
