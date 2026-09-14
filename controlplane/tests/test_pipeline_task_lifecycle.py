"""PipelineTask owner/state and artifact replacement boundaries."""
import asyncio
import copy
import hashlib
import importlib
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'lifecycle.db'}")
    monkeypatch.setenv("MODE_B_STORAGE", str(tmp_path / "storage"))
    import app.db.session as session_mod
    import app.api.nodes as nodes_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(nodes_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app), session_mod.SessionLocal, nodes_mod


def _node_id(client, SessionLocal, token):
    assert client.post("/api/nodes/heartbeat", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    from app.db.models import GpuNode
    db = SessionLocal()
    try:
        return db.query(GpuNode).filter_by(
            token_hash=hashlib.sha256(token.encode()).hexdigest()).one().id
    finally:
        db.close()


def _task(client, SessionLocal, *, status="running", claimed_by=None, task_type="custom",
          output_paths=None):
    project_id = client.post("/api/projects", json={"name": "lifecycle", "target_lang": "en"}).json()["id"]
    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        task = PipelineTask(project_id=project_id, task_key=f"TASK/{project_id[:8]}",
                            task_type=task_type, status=status, claimed_by=claimed_by,
                            output_paths=output_paths or {"payload": {"stable": True}})
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _snapshot(SessionLocal, task_id):
    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        task = db.get(PipelineTask, task_id)
        return {
            "status": task.status,
            "claimed_by": task.claimed_by,
            "lease_until": task.lease_until,
            "retry_count": task.retry_count,
            "error_message": task.error_message,
            "output_hash": task.output_hash,
            "output_paths": copy.deepcopy(task.output_paths),
        }
    finally:
        db.close()


def _old_artifact(tmp_path, SessionLocal, task_id, *, key="sound", filename="sound.wav"):
    from app.db.models import PipelineTask
    dest_dir = tmp_path / "storage" / "artifacts" / task_id
    dest_dir.mkdir(parents=True)
    dest = dest_dir / filename
    dest.write_bytes(b"old-bytes")
    db = SessionLocal()
    try:
        task = db.get(PipelineTask, task_id)
        outputs = dict(task.output_paths or {})
        outputs["artifacts"] = [{"key": key, "filename": filename,
                                  "path": str(dest), "bytes": len(b"old-bytes")}]
        task.output_paths = outputs
        db.commit()
    finally:
        db.close()
    return dest


def test_complete_and_fail_enforce_current_running_owner(tmp_path, monkeypatch):
    client, SessionLocal, _nodes = _client(tmp_path, monkeypatch)
    owner, other = "owner", "other"
    owner_id = _node_id(client, SessionLocal, owner)
    _node_id(client, SessionLocal, other)
    owner_headers = {"Authorization": f"Bearer {owner}"}
    other_headers = {"Authorization": f"Bearer {other}"}

    rejected = [
        ("unowned", "running", None, owner_headers),
        ("wrong_owner", "running", owner_id, other_headers),
        ("pending", "pending", owner_id, owner_headers),
        ("dead", "dead", owner_id, owner_headers),
        ("failed", "failed", owner_id, owner_headers),
    ]
    for _label, status, claimed_by, headers in rejected:
        task_id = _task(client, SessionLocal, status=status, claimed_by=claimed_by)
        before = _snapshot(SessionLocal, task_id)
        assert client.post(f"/api/nodes/tasks/{task_id}/complete", headers=headers,
                           json={"outputs": {"changed": True}}).status_code == 409
        assert _snapshot(SessionLocal, task_id) == before
        assert client.post(f"/api/nodes/tasks/{task_id}/fail", headers=headers,
                           json={"error": "changed"}).status_code == 409
        assert _snapshot(SessionLocal, task_id) == before

    completed = _task(client, SessionLocal, status="completed", claimed_by=owner_id,
                      output_paths={"payload": {"keep": True},
                                    "qc": {"pass": False, "action": "review"}})
    before = _snapshot(SessionLocal, completed)
    replay = client.post(f"/api/nodes/tasks/{completed}/complete", headers=owner_headers,
                         json={"outputs": {"changed": True}, "output_hash": "new"})
    assert replay.status_code == 200 and replay.json() == {
        "ok": True, "qc_pass": False, "qc_action": "review"}
    assert _snapshot(SessionLocal, completed) == before

    for output_paths in (None, []):
        legacy = _task(client, SessionLocal, status="completed", claimed_by=owner_id,
                       output_paths=output_paths)
        before = _snapshot(SessionLocal, legacy)
        replay = client.post(f"/api/nodes/tasks/{legacy}/complete", headers=owner_headers,
                             json={"outputs": {"changed": True}})
        assert replay.status_code == 200 and replay.json() == {
            "ok": True, "qc_pass": True, "qc_action": "none"}
        assert _snapshot(SessionLocal, legacy) == before

    happy = _task(client, SessionLocal, claimed_by=owner_id)
    response = client.post(f"/api/nodes/tasks/{happy}/complete", headers=owner_headers,
                           json={"outputs": {"result": "ok"}})
    assert response.status_code == 200, response.text
    assert _snapshot(SessionLocal, happy)["status"] == "completed"

    for retryable in ("true", 1, 0, {}, [], None):
        task_id = _task(client, SessionLocal, claimed_by=owner_id)
        before = _snapshot(SessionLocal, task_id)
        response = client.post(f"/api/nodes/tasks/{task_id}/fail", headers=owner_headers,
                               json={"retryable": retryable, "error": "do not write"})
        assert response.status_code == 422, response.text
        assert _snapshot(SessionLocal, task_id) == before

    retry = _task(client, SessionLocal, claimed_by=owner_id)
    assert client.post(f"/api/nodes/tasks/{retry}/fail", headers=owner_headers,
                       json={"error": "retry"}).json()["will_retry"] is True
    assert _snapshot(SessionLocal, retry)["status"] == "pending"


def test_complete_and_fail_cas_reject_a_reassignment_after_read(tmp_path, monkeypatch):
    client, SessionLocal, nodes_mod = _client(tmp_path, monkeypatch)
    owner, other = "owner", "other"
    owner_id = _node_id(client, SessionLocal, owner)
    other_id = _node_id(client, SessionLocal, other)

    def force_reassignment_when_pipeline_cas_runs(db, task_id):
        query = db.query

        def hooked_query(entity, *args, **kwargs):
            if entity is __import__("app.db.models", fromlist=["PipelineTask"]).PipelineTask:
                other_db = SessionLocal()
                try:
                    task = other_db.get(entity, task_id)
                    task.claimed_by = other_id
                    other_db.commit()
                finally:
                    other_db.close()
            return query(entity, *args, **kwargs)

        monkeypatch.setattr(db, "query", hooked_query)

    for endpoint, body in ((nodes_mod.complete, {"outputs": {"stale": True}}),
                           (nodes_mod.fail, {"error": "stale"})):
        task_id = _task(client, SessionLocal, claimed_by=owner_id)
        before = _snapshot(SessionLocal, task_id)
        db = SessionLocal()
        try:
            force_reassignment_when_pipeline_cas_runs(db, task_id)
            with pytest.raises(HTTPException) as raised:
                endpoint(task_id, body, authorization=f"Bearer {owner}", db=db)
            assert raised.value.status_code == 409
        finally:
            db.close()
        after = _snapshot(SessionLocal, task_id)
        assert after["claimed_by"] == other_id
        assert after["status"] == before["status"] == "running"
        assert after["output_paths"] == before["output_paths"]


def test_artifact_owner_state_and_atomic_replacement(tmp_path, monkeypatch):
    client, SessionLocal, nodes_mod = _client(tmp_path, monkeypatch)
    owner, other = "owner", "other"
    owner_id = _node_id(client, SessionLocal, owner)
    other_id = _node_id(client, SessionLocal, other)
    owner_headers = {"Authorization": f"Bearer {owner}"}
    other_headers = {"Authorization": f"Bearer {other}"}

    for status, claimed_by, headers in (
        ("running", None, owner_headers), ("running", owner_id, other_headers),
        ("pending", owner_id, owner_headers), ("dead", owner_id, owner_headers),
    ):
        task_id = _task(client, SessionLocal, status=status, claimed_by=claimed_by)
        before = _snapshot(SessionLocal, task_id)
        response = client.post(f"/api/nodes/tasks/{task_id}/artifact", headers=headers,
                               params={"key": "sound", "filename": "sound.wav"}, content=b"blocked")
        assert response.status_code == 409, response.text
        assert _snapshot(SessionLocal, task_id) == before

    task_id = _task(client, SessionLocal, claimed_by=owner_id)
    dest = _old_artifact(tmp_path, SessionLocal, task_id)
    before = _snapshot(SessionLocal, task_id)
    monkeypatch.setattr(nodes_mod, "_ART_MAX_MB", 0)
    response = client.post(f"/api/nodes/tasks/{task_id}/artifact", headers=owner_headers,
                           params={"key": "sound", "filename": "sound.wav"}, content=b"too-large")
    assert response.status_code == 413, response.text
    assert dest.read_bytes() == b"old-bytes" and _snapshot(SessionLocal, task_id) == before
    assert not list(dest.parent.glob(".*.upload"))

    class BrokenRequest:
        async def stream(self):
            yield b"partial"
            raise RuntimeError("stream broke")

    monkeypatch.setattr(nodes_mod, "_ART_MAX_MB", 80)
    db = SessionLocal()
    try:
        with pytest.raises(RuntimeError, match="stream broke"):
            asyncio.run(nodes_mod.upload_artifact(
                task_id, BrokenRequest(), filename="sound.wav", key="sound",
                authorization=f"Bearer {owner}", db=db))
    finally:
        db.close()
    assert dest.read_bytes() == b"old-bytes" and _snapshot(SessionLocal, task_id) == before
    assert not list(dest.parent.glob(".*.upload"))

    class ReassignedMidStream:
        async def stream(self):
            yield b"partial"
            other_db = SessionLocal()
            try:
                from app.db.models import PipelineTask
                task = other_db.get(PipelineTask, task_id)
                task.claimed_by = other_id
                other_db.commit()
            finally:
                other_db.close()
            yield b"more"

    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(nodes_mod.upload_artifact(
                task_id, ReassignedMidStream(), filename="sound.wav", key="sound",
                authorization=f"Bearer {owner}", db=db))
        assert raised.value.status_code == 409
    finally:
        db.close()
    after = _snapshot(SessionLocal, task_id)
    assert dest.read_bytes() == b"old-bytes"
    assert after["claimed_by"] == other_id and after["status"] == "running"
    assert after["output_paths"] == before["output_paths"]
    assert not list(dest.parent.glob(".*.upload"))

    db = SessionLocal()
    try:
        from app.db.models import PipelineTask
        task = db.get(PipelineTask, task_id)
        task.claimed_by = owner_id
        db.commit()
    finally:
        db.close()

    success = client.post(f"/api/nodes/tasks/{task_id}/artifact", headers=owner_headers,
                          params={"key": "sound", "filename": "sound.wav"}, content=b"fresh")
    assert success.status_code == 200, success.text
    assert dest.read_bytes() == b"fresh"
    assert _snapshot(SessionLocal, task_id)["output_paths"]["artifacts"] == [success.json()["artifact"]]

    completed = _task(client, SessionLocal, status="completed", claimed_by=owner_id)
    assert client.post(f"/api/nodes/tasks/{completed}/artifact", headers=owner_headers,
                       params={"key": "late", "filename": "late.wav"}, content=b"late").status_code == 200


def test_backfill_uses_unique_temp_and_preserves_old_artifact_on_stream_failure(tmp_path, monkeypatch):
    client, SessionLocal, nodes_mod = _client(tmp_path, monkeypatch)
    token = "legacy-owner"
    owner_id = _node_id(client, SessionLocal, token)
    headers = {"Authorization": f"Bearer {token}"}
    task_id = _task(client, SessionLocal, status="completed", claimed_by=owner_id,
                    task_type="separate-vocals")
    dest = _old_artifact(tmp_path, SessionLocal, task_id, key="vocals", filename="vocals.wav")
    before = _snapshot(SessionLocal, task_id)

    class BrokenRequest:
        async def stream(self):
            yield b"partial"
            raise RuntimeError("backfill stream broke")

    db = SessionLocal()
    try:
        with pytest.raises(RuntimeError, match="backfill stream broke"):
            asyncio.run(nodes_mod.backfill_separation_artifact(
                task_id, BrokenRequest(), filename="vocals.wav", key="vocals",
                authorization=f"Bearer {token}", db=db))
    finally:
        db.close()
    assert dest.read_bytes() == b"old-bytes" and _snapshot(SessionLocal, task_id) == before
    assert not list(dest.parent.glob(".*.backfill"))

    seen = []
    replace = nodes_mod.os.replace

    def capture_replace(source, target):
        seen.append(source)
        return replace(source, target)

    monkeypatch.setattr(nodes_mod.os, "replace", capture_replace)
    for content in (b"first", b"second"):
        response = client.post(f"/api/nodes/tasks/{task_id}/artifact-backfill", headers=headers,
                               params={"key": "vocals", "filename": "vocals.wav"}, content=content)
        assert response.status_code == 200, response.text
    assert len(seen) == 2 and seen[0] != seen[1] and all(path.endswith(".backfill") for path in seen)
    assert dest.read_bytes() == b"second"
