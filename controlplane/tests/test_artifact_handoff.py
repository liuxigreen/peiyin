"""分离产物到 diarize 的控制面交接与节点下载授权。"""
import importlib

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'handoff.db'}")
    monkeypatch.setenv("MODE_B_STORAGE", str(tmp_path / "storage"))
    import app.db.session as session_mod
    import app.api.nodes as nodes_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(nodes_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app), session_mod.SessionLocal


def _separate_task(client, SessionLocal):
    project = client.post("/api/projects", json={"name": "交接剧", "target_lang": "en"})
    assert project.status_code == 200, project.text
    pid = project.json()["id"]
    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        task = PipelineTask(
            project_id=pid, task_key=f"SEPARATE/{pid[:8]}",
            task_type="separate-vocals", resource="gpu", gpu_required=True,
            input_hash="source-hash", status="running",
            output_paths={"payload": {"source": "keep-me"}},
        )
        db.add(task)
        db.commit()
        return pid, task.id
    finally:
        db.close()


def _diarize_for(db, project_id):
    from app.db.models import PipelineTask
    return (db.query(PipelineTask)
            .filter_by(project_id=project_id, task_type="diarize").first())


def test_complete_then_vocals_artifact_creates_authenticated_url(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    pid, source_id = _separate_task(client, SessionLocal)
    producer = {"Authorization": "Bearer producer"}

    complete = client.post(
        f"/api/nodes/tasks/{source_id}/complete", headers=producer,
        json={"outputs": [{"key": "vocals", "path": r"E:\\node\\vocals.wav"}]},
    )
    assert complete.status_code == 200, complete.text
    db = SessionLocal()
    try:
        assert _diarize_for(db, pid) is None
        source = db.get(__import__("app.db.models", fromlist=["PipelineTask"]).PipelineTask, source_id)
        assert source.output_paths["payload"] == {"source": "keep-me"}
        assert source.output_paths["outputs"][0]["path"].startswith("E:")
    finally:
        db.close()

    uploaded = client.post(
        f"/api/nodes/tasks/{source_id}/artifact", headers=producer,
        params={"key": "vocals", "filename": "vocals.wav"}, content=b"control-plane-vocals",
    )
    assert uploaded.status_code == 200, uploaded.text
    db = SessionLocal()
    try:
        source = db.get(__import__("app.db.models", fromlist=["PipelineTask"]).PipelineTask, source_id)
        handoff = _diarize_for(db, pid)
        assert source.output_paths["payload"] == {"source": "keep-me"}
        assert source.output_paths["outputs"][0]["path"].startswith("E:")
        assert source.output_paths["artifacts"][0]["filename"] == "vocals.wav"
        url = handoff.output_paths["payload"]["zh_audio_url"]
        assert url == f"/api/nodes/tasks/{source_id}/artifacts/vocals/vocals.wav"
        assert "E:" not in url and "\\" not in url
    finally:
        db.close()

    assert client.get(url, headers={"Authorization": "Bearer foreign"}).status_code == 403
    diarize_headers = {"Authorization": "Bearer diarize-node"}
    assert client.post("/api/nodes/heartbeat", headers=diarize_headers,
                       json={"capabilities": ["diarize"]}).status_code == 200
    claimed = client.get("/api/nodes/me/claim", headers=diarize_headers)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["task"]["id"] == handoff.id
    assert claimed.json()["task"]["input_payload"]["zh_audio_url"] == url
    downloaded = client.get(url, headers={"Authorization": "Bearer diarize-node"})
    assert downloaded.status_code == 200 and downloaded.content == b"control-plane-vocals"


def test_artifact_then_complete_preserves_data_and_rejects_traversal(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    pid, source_id = _separate_task(client, SessionLocal)
    producer = {"Authorization": "Bearer producer"}

    bad = client.post(
        f"/api/nodes/tasks/{source_id}/artifact", headers=producer,
        params={"key": "../vocals", "filename": "vocals.wav"}, content=b"x",
    )
    assert bad.status_code == 400
    uploaded = client.post(
        f"/api/nodes/tasks/{source_id}/artifact", headers=producer,
        params={"key": "vocals", "filename": "vocals.wav"}, content=b"saved-first",
    )
    assert uploaded.status_code == 200, uploaded.text
    db = SessionLocal()
    try:
        source = db.get(__import__("app.db.models", fromlist=["PipelineTask"]).PipelineTask, source_id)
        assert source.output_paths["payload"] == {"source": "keep-me"}
        assert source.output_paths["artifacts"][0]["bytes"] == len(b"saved-first")
        assert _diarize_for(db, pid) is None
    finally:
        db.close()

    complete = client.post(
        f"/api/nodes/tasks/{source_id}/complete", headers=producer,
        json={"outputs": [{"key": "vocals", "path": r"E:\\node\\vocals.wav"}]},
    )
    assert complete.status_code == 200, complete.text
    db = SessionLocal()
    try:
        source = db.get(__import__("app.db.models", fromlist=["PipelineTask"]).PipelineTask, source_id)
        handoff = _diarize_for(db, pid)
        assert source.output_paths["payload"] == {"source": "keep-me"}
        assert source.output_paths["outputs"][0]["key"] == "vocals"
        assert source.output_paths["artifacts"][0]["filename"] == "vocals.wav"
        assert handoff.output_paths["payload"]["zh_audio_url"].startswith("/api/nodes/tasks/")
    finally:
        db.close()
