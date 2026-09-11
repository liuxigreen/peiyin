"""Completed separation artifact backfill coverage."""
import copy
import hashlib
import importlib

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'backfill.db'}")
    monkeypatch.setenv("MODE_B_STORAGE", str(tmp_path / "storage"))
    import app.db.session as session_mod
    import app.api.nodes as nodes_mod
    import app.main as main_mod

    importlib.reload(session_mod)
    importlib.reload(nodes_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app), session_mod.SessionLocal


def _node_id(client: TestClient, SessionLocal, token: str) -> str:
    assert client.post("/api/nodes/heartbeat", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    from app.db.models import GpuNode
    db = SessionLocal()
    try:
        return db.query(GpuNode).filter_by(
            token_hash=hashlib.sha256(token.encode()).hexdigest()).one().id
    finally:
        db.close()


def _separation_task(client: TestClient, SessionLocal, node_id: str, *, task_type="separate-vocals",
                     status="completed"):
    project_id = client.post("/api/projects", json={"name": "backfill", "target_lang": "en"}).json()["id"]
    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        task = PipelineTask(project_id=project_id, task_key=f"SEPARATE/{project_id[:8]}",
                            task_type=task_type, status=status, claimed_by=node_id,
                            retry_count=2, output_paths={"payload": {"source": "legacy"},
                                                         "artifacts": [{"key": "accompaniment",
                                                                        "filename": "music.wav",
                                                                        "path": "legacy/music.wav",
                                                                        "bytes": 1}]})
        db.add(task)
        db.commit()
        return project_id, task.id
    finally:
        db.close()


def _snapshot(task):
    return {"status": task.status, "claimed_by": task.claimed_by,
            "lease_until": task.lease_until, "retry_count": task.retry_count,
            "output_paths": copy.deepcopy(task.output_paths)}


def test_completed_separation_backfill_preserves_task_lifecycle_and_artifacts(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    token = "owner-node-token"
    node_id = _node_id(client, SessionLocal, token)
    project_id, task_id = _separation_task(client, SessionLocal, node_id)
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post(f"/api/nodes/tasks/{task_id}/artifact-backfill", headers=headers,
                        params={"key": "vocals", "filename": "vocals.wav"}, content=b"first")
    second = client.post(f"/api/nodes/tasks/{task_id}/artifact-backfill", headers=headers,
                         params={"key": "vocals", "filename": "vocals.wav"}, content=b"second")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        task = db.get(PipelineTask, task_id)
        assert task.status == "completed" and task.claimed_by == node_id and task.retry_count == 2
        artifacts = {artifact["key"]: artifact for artifact in task.output_paths["artifacts"]}
        assert set(artifacts) == {"vocals", "accompaniment"}
        assert artifacts["vocals"]["bytes"] == len(b"second")
        assert (tmp_path / "storage" / "artifacts" / task_id / "vocals.wav").read_bytes() == b"second"
        handoffs = db.query(PipelineTask).filter_by(project_id=project_id, task_type="diarize").all()
        assert len(handoffs) == 1
    finally:
        db.close()


def test_backfill_rejects_task_type_status_key_and_node_mismatch_without_mutation(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    owner = "owner-token"
    other = "other-token"
    owner_id = _node_id(client, SessionLocal, owner)
    _node_id(client, SessionLocal, other)
    headers = {"Authorization": f"Bearer {owner}"}

    cases = [
        ("tts-generate", "completed", headers, "vocals", "vocals.wav", 409),
        ("separate-vocals", "running", headers, "vocals", "vocals.wav", 409),
        ("separate-vocals", "completed", headers, "other", "vocals.wav", 400),
        ("separate-vocals", "completed", headers, "vocals", "../vocals.wav", 400),
        ("separate-vocals", "completed", {"Authorization": f"Bearer {other}"}, "vocals", "vocals.wav", 409),
    ]
    from app.db.models import PipelineTask
    for task_type, status, request_headers, key, filename, expected_status in cases:
        _project_id, task_id = _separation_task(client, SessionLocal, owner_id,
                                                 task_type=task_type, status=status)
        db = SessionLocal()
        try:
            before = _snapshot(db.get(PipelineTask, task_id))
        finally:
            db.close()

        response = client.post(f"/api/nodes/tasks/{task_id}/artifact-backfill",
                               headers=request_headers,
                               params={"key": key, "filename": filename}, content=b"nope")

        assert response.status_code == expected_status, response.text
        db = SessionLocal()
        try:
            assert _snapshot(db.get(PipelineTask, task_id)) == before
        finally:
            db.close()
