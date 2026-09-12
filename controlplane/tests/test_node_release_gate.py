"""Offline release reporting, drain switching, and activated claim gate coverage."""
from __future__ import annotations

import importlib
import pathlib
import sys

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect


sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
NODE_SECRET = {"x-node-secret": "dev-node-secret"}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'release.db'}")
    monkeypatch.setenv("NODE_SHARED_SECRET", "dev-node-secret")
    monkeypatch.delenv("NODE_MIN_RELEASE_DIARIZE", raising=False)
    monkeypatch.delenv("NODE_MIN_RELEASE_SEP", raising=False)
    monkeypatch.delenv("NODE_MIN_RELEASE_TTS", raising=False)
    import app.db.session as session_mod
    import app.api.nodes as nodes_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(nodes_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app), session_mod.SessionLocal


def _node(client: TestClient, capabilities=("tts",)):
    response = client.post("/api/nodes/register", headers=NODE_SECRET,
                           json={"name": "release-node", "capabilities": list(capabilities)})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def _state(client, headers, *, version="1.10.0", ready=True, draining=False,
           digest="a" * 64):
    return client.post("/api/nodes/me/release-state", headers=headers,
                       json={"version": version, "digest": digest,
                             "ready": ready, "draining": draining})


def _task(client, SessionLocal, task_type: str):
    project_id = client.post("/api/projects", json={"name": f"release-{task_type}",
                                                      "target_lang": "en"}).json()["id"]
    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        task = PipelineTask(project_id=project_id, task_key=f"TASK/{task_type}",
                            task_type=task_type, resource="gpu", gpu_required=True,
                            status="pending")
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def test_release_state_is_authenticated_validated_and_persisted(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    assert client.post("/api/nodes/me/release-state", json={}).status_code == 401
    headers = _node(client)
    assert _state(client, headers, version="1.0", ready=True).status_code == 422
    assert _state(client, headers, version="garbage", ready=False, digest="x").status_code == 422
    assert _state(client, headers, version="1.10.0", ready=False, digest="").status_code == 422
    unknown = _state(client, headers, version="", ready=False, digest="")
    assert unknown.status_code == 200 and unknown.json()["version"] is None
    assert _state(client, headers, ready=True, draining=True).status_code == 422
    saved = _state(client, headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["ready"] is True and saved.json()["digest"] == "a" * 64
    from app.db.models import GpuNode
    db = SessionLocal()
    try:
        node = db.query(GpuNode).one()
        assert node.release_version == "1.10.0" and node.release_reported_at is not None
    finally:
        db.close()


def test_release_switch_ready_counts_only_authenticated_running_ownership(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    headers = _node(client)
    assert _state(client, headers, ready=False, draining=True).status_code == 200
    from app.db.models import GpuNode, NodeJob, PipelineTask
    db = SessionLocal()
    try:
        node = db.query(GpuNode).one()
        project_id = client.post("/api/projects", json={"name": "switch", "target_lang": "en"}).json()["id"]
        task = PipelineTask(project_id=project_id, task_key="TASK/switch", task_type="tts-generate",
                            status="running", claimed_by=node.id)
        job = NodeJob(target_node_name=node.name, kind="probe", status="running", claimed_by=node.id)
        db.add_all([task, job])
        db.commit()
        response = client.get("/api/nodes/me/release-switch-ready", headers=headers)
        assert response.json() == {"ready_to_switch": False, "running_pipeline_tasks": 1,
                                   "running_node_jobs": 1}
        task.status = "completed"
        db.commit()
        assert client.get("/api/nodes/me/release-switch-ready", headers=headers).json()["running_pipeline_tasks"] == 0
        job.status = "completed"
        db.commit()
        assert client.get("/api/nodes/me/release-switch-ready", headers=headers).json()["ready_to_switch"] is True
    finally:
        db.close()


def test_unconfigured_minimum_keeps_capability_only_claim_compatibility(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    headers = _node(client, ("tts",))
    task_id = _task(client, SessionLocal, "tts-generate")
    claimed = client.get("/api/nodes/me/claim", headers=headers)
    assert claimed.status_code == 200 and claimed.json()["task"]["id"] == task_id


def test_activated_gate_rejects_not_ready_draining_old_and_invalid_versions(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    headers = _node(client, ("diarize", "sep", "tts"))
    monkeypatch.setenv("NODE_MIN_RELEASE_DIARIZE", "1.9.0")
    monkeypatch.setenv("NODE_MIN_RELEASE_SEP", "1.9.0")
    monkeypatch.setenv("NODE_MIN_RELEASE_TTS", "1.9.0")
    task_ids = {_task(client, SessionLocal, task_type)
                for task_type in ("diarize", "separate-vocals", "tts-generate")}
    assert client.get("/api/nodes/me/claim", headers=headers).json()["task"] is None
    assert _state(client, headers, version="1.10.0", ready=False, draining=True).status_code == 200
    assert client.get("/api/nodes/me/claim", headers=headers).json()["task"] is None
    assert _state(client, headers, version="1.8.9", ready=True).status_code == 200
    assert client.get("/api/nodes/me/claim", headers=headers).json()["task"] is None
    assert _state(client, headers, version="1.10.0", ready=True).status_code == 200
    claimed = client.get("/api/nodes/me/claim", headers=headers, params={"n": 3}).json()["tasks"]
    assert {task["id"] for task in claimed} == task_ids  # 1.10.0 compares above 1.9.0 by integer segment.

    # Missing fourth segments are zero for comparisons: 1.2.3 equals 1.2.3.0.
    monkeypatch.setenv("NODE_MIN_RELEASE_TTS", "1.2.3.0")
    equal_id = _task(client, SessionLocal, "tts-generate")
    assert _state(client, headers, version="1.2.3", ready=True).status_code == 200
    assert client.get("/api/nodes/me/claim", headers=headers).json()["task"]["id"] == equal_id

    # A nonempty malformed configuration fails closed for its capability.
    monkeypatch.setenv("NODE_MIN_RELEASE_TTS", "release-1.10")
    blocked_id = _task(client, SessionLocal, "tts-generate")
    assert client.get("/api/nodes/me/claim", headers=headers).json()["task"] is None
    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        assert db.get(PipelineTask, blocked_id).status == "pending"
    finally:
        db.close()


def test_release_state_migration_is_repeat_safe_and_non_destructive(tmp_path):
    import migrate_gpu_node_release_state as migration
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE gpu_nodes (id VARCHAR(36) PRIMARY KEY, name VARCHAR(100))")
    migration.migrate(engine)
    migration.migrate(engine)
    assert migration._REQUIRED_COLUMNS <= {
        column["name"] for column in inspect(engine).get_columns("gpu_nodes")
    }
    assert migration._REQUIRED_INDEXES <= {
        index["name"] for index in inspect(engine).get_indexes("gpu_nodes")
    }
    source = pathlib.Path(migration.__file__).read_text()
    assert "DROP TABLE" not in source and " RENAME " not in source
