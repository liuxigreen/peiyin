"""HTTP coverage for controlled dead-diarize canary retries."""
import importlib
import os

from fastapi.testclient import TestClient


def _client(tmp_db: str) -> TestClient:
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db}"
    import app.db.session as session_mod
    import app.main as main_mod

    importlib.reload(session_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app)


def _seed_dead_diarize(client: TestClient, *, artifact_url: str):
    project_id = client.post("/api/projects", json={"name": "retry", "target_lang": "en"}).json()["id"]
    from app.db.models import PipelineTask, Segment, Utterance
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        segment = Segment(project_id=project_id, seg_index=1, start_ms=0, end_ms=20000)
        db.add(segment)
        db.flush()
        utterances = []
        for index in range(20):
            utterances.append(Utterance(project_id=project_id, segment_id=segment.id,
                                         uid=f"U{index + 1:02d}", seq_index=index + 1,
                                         start_ms=index * 1000, end_ms=(index + 1) * 1000,
                                         original_text=f"line {index + 1}"))
        db.add_all(utterances)
        db.flush()
        task = PipelineTask(project_id=project_id, task_key="DIARIZE/retry", task_type="diarize",
                            status="dead", claimed_by="old-node", retry_count=3,
                            output_paths={"payload": {"zh_audio_url": artifact_url,
                                                       "srt_slots": [{"uid": "old"}]}})
        db.add(task)
        db.commit()
        return project_id, task.id, [utterance.uid for utterance in utterances]
    finally:
        db.close()


def _task_snapshot(task):
    return {"status": task.status, "claimed_by": task.claimed_by,
            "retry_count": task.retry_count, "output_paths": task.output_paths}


def test_retry_dead_diarize_resets_only_target_and_persists_canary(tmp_path):
    client = _client(str(tmp_path / "success.db"))
    project_id, task_id, uids = _seed_dead_diarize(
        client, artifact_url="/api/nodes/tasks/separate-1/artifacts/vocals/vocals.wav")

    response = client.post(f"/api/projects/{project_id}/mode-b/diarize/retry",
                           json={"task_id": task_id, "uids": uids[:10]})

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "task_id": task_id, "slots": 10, "idempotent": False}
    from app.db.models import PipelineTask, Project
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        task = db.get(PipelineTask, task_id)
        project = db.get(Project, project_id)
        assert task.status == "pending" and task.claimed_by is None and task.lease_until is None
        assert task.retry_count == 4
        assert [slot["uid"] for slot in task.output_paths["payload"]["srt_slots"]] == uids[:10]
        assert project.config["mode_b_run"]["canary_uids"] == uids[:10]
        assert db.query(PipelineTask).filter_by(project_id=project_id).count() == 1
    finally:
        db.close()


def test_retry_rejects_bad_selection_without_mutation(tmp_path):
    client = _client(str(tmp_path / "selection.db"))
    project_id, task_id, uids = _seed_dead_diarize(
        client, artifact_url="/api/nodes/tasks/separate-1/artifacts/vocals/vocals.wav")
    from app.db.models import PipelineTask, Project
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        before_task = _task_snapshot(db.get(PipelineTask, task_id))
        before_config = db.get(Project, project_id).config
    finally:
        db.close()

    response = client.post(f"/api/projects/{project_id}/mode-b/diarize/retry",
                           json={"task_id": task_id, "uids": uids[:9]})

    assert response.status_code == 400
    db = SessionLocal()
    try:
        assert _task_snapshot(db.get(PipelineTask, task_id)) == before_task
        assert db.get(Project, project_id).config == before_config
    finally:
        db.close()


def test_retry_rejects_legacy_voice_source_without_mutation(tmp_path):
    client = _client(str(tmp_path / "legacy.db"))
    project_id, task_id, uids = _seed_dead_diarize(
        client, artifact_url="/api/nodes/voices/zhaudio/legacy.mp3")
    from app.db.models import PipelineTask
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        before = _task_snapshot(db.get(PipelineTask, task_id))
    finally:
        db.close()

    response = client.post(f"/api/projects/{project_id}/mode-b/diarize/retry",
                           json={"task_id": task_id, "uids": uids[:10]})

    assert response.status_code == 409
    db = SessionLocal()
    try:
        assert _task_snapshot(db.get(PipelineTask, task_id)) == before
    finally:
        db.close()


def test_retry_is_idempotent_after_task_is_pending(tmp_path):
    client = _client(str(tmp_path / "idempotent.db"))
    project_id, task_id, uids = _seed_dead_diarize(
        client, artifact_url="/api/nodes/tasks/separate-1/artifacts/vocals/vocals.wav")
    body = {"task_id": task_id, "uids": uids[:10]}
    assert client.post(f"/api/projects/{project_id}/mode-b/diarize/retry", json=body).status_code == 200
    from app.db.models import PipelineTask, Project
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        before_task = _task_snapshot(db.get(PipelineTask, task_id))
        before_config = db.get(Project, project_id).config
    finally:
        db.close()

    response = client.post(f"/api/projects/{project_id}/mode-b/diarize/retry", json=body)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "task_id": task_id, "slots": 10, "idempotent": True}
    db = SessionLocal()
    try:
        assert _task_snapshot(db.get(PipelineTask, task_id)) == before_task
        assert db.get(Project, project_id).config == before_config
    finally:
        db.close()
