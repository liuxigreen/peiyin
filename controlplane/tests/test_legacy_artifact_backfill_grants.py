"""One-time ECS-issued legacy vocals backfill grant coverage."""
from __future__ import annotations

import copy
import hashlib
import importlib
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect


SCRIPTS = pathlib.Path(__file__).parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'grants.db'}")
    monkeypatch.setenv("MODE_B_STORAGE", str(tmp_path / "storage"))
    import app.db.session as session_mod
    import app.api.nodes as nodes_mod
    import app.main as main_mod

    importlib.reload(session_mod)
    importlib.reload(nodes_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app), session_mod.SessionLocal


def _node(client: TestClient, SessionLocal, token: str) -> str:
    response = client.post("/api/nodes/heartbeat", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    from app.db.models import GpuNode
    db = SessionLocal()
    try:
        return db.query(GpuNode).filter_by(
            token_hash=hashlib.sha256(token.encode()).hexdigest()).one().id
    finally:
        db.close()


def _task(client: TestClient, SessionLocal, owner_id: str):
    project_id = client.post("/api/projects", json={"name": "grant", "target_lang": "en"}).json()["id"]
    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        task = PipelineTask(project_id=project_id, task_key=f"SEPARATE/{project_id[:8]}",
                            task_type="separate-vocals", status="completed", claimed_by=owner_id,
                            lease_until=None, retry_count=3,
                            output_paths={"payload": {"source": "legacy"}})
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _lifecycle(task):
    return {"status": task.status, "claimed_by": task.claimed_by,
            "lease_until": task.lease_until, "retry_count": task.retry_count}


def _grant(SessionLocal, *, task_id: str, node_id: str, expires_at=None):
    from app.db.models import LegacyArtifactBackfillGrant
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expires_at = expires_at or now + timedelta(minutes=10)
        issued_at = min(now, expires_at - timedelta(seconds=1))
        grant = LegacyArtifactBackfillGrant(
            source_task_id=task_id, node_id=node_id, artifact_key="vocals", state="issued",
            issuer="ecs-operator", issued_at=issued_at,
            expires_at=expires_at, result="issued")
        db.add(grant)
        db.commit()
        return grant.id
    finally:
        db.close()


def test_granted_re_registered_node_uploads_once_without_source_lifecycle_change(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    old_id = _node(client, SessionLocal, "old-owner")
    current_token = "current-3060"
    current_id = _node(client, SessionLocal, current_token)
    task_id = _task(client, SessionLocal, old_id)
    grant_id = _grant(SessionLocal, task_id=task_id, node_id=current_id)
    headers = {"Authorization": f"Bearer {current_token}"}

    db = SessionLocal()
    try:
        before = _lifecycle(db.get(__import__("app.db.models", fromlist=["PipelineTask"]).PipelineTask,
                                   task_id))
    finally:
        db.close()
    first = client.post(f"/api/nodes/tasks/{task_id}/artifact-backfill", headers=headers,
                        params={"grant_id": grant_id, "key": "vocals", "filename": "vocals.wav"},
                        content=b"granted")
    assert first.status_code == 200, first.text

    from app.db.models import LegacyArtifactBackfillGrant, PipelineTask
    db = SessionLocal()
    try:
        task = db.get(PipelineTask, task_id)
        grant = db.get(LegacyArtifactBackfillGrant, grant_id)
        assert _lifecycle(task) == before
        assert task.output_paths["artifacts"][0]["key"] == "vocals"
        assert grant.state == "consumed" and grant.consumed_by_node_id == current_id
        assert grant.result == "uploaded" and grant.filename == "vocals.wav"
        assert grant.byte_count == len(b"granted") and grant.consumed_at is not None
        output_after_first = copy.deepcopy(task.output_paths)
    finally:
        db.close()

    repeated = client.post(f"/api/nodes/tasks/{task_id}/artifact-backfill", headers=headers,
                           params={"grant_id": grant_id, "key": "vocals", "filename": "vocals.wav"},
                           content=b"second")
    assert repeated.status_code == 409
    db = SessionLocal()
    try:
        assert db.get(PipelineTask, task_id).output_paths == output_after_first
    finally:
        db.close()


def test_grant_rejects_wrong_node_and_expiry_without_source_mutation(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    owner_id = _node(client, SessionLocal, "old-owner")
    target_token, other_token = "target", "other"
    target_id = _node(client, SessionLocal, target_token)
    _node(client, SessionLocal, other_token)
    task_id = _task(client, SessionLocal, owner_id)
    valid_grant = _grant(SessionLocal, task_id=task_id, node_id=target_id)
    expired_grant = _grant(SessionLocal, task_id=task_id, node_id=target_id,
                           expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))

    from app.db.models import PipelineTask
    db = SessionLocal()
    try:
        before = {**_lifecycle(db.get(PipelineTask, task_id)),
                  "output_paths": copy.deepcopy(db.get(PipelineTask, task_id).output_paths)}
    finally:
        db.close()
    responses = {}
    for label, token, grant_id in (("wrong_node", other_token, valid_grant),
                                   ("expired", target_token, expired_grant)):
        response = client.post(f"/api/nodes/tasks/{task_id}/artifact-backfill",
                               headers={"Authorization": f"Bearer {token}"},
                               params={"grant_id": grant_id, "key": "vocals", "filename": "vocals.wav"},
                               content=b"blocked")
        assert response.status_code == 409, response.text
        responses[label] = response
    mismatch = responses["wrong_node"].json()["detail"]
    assert mismatch["code"] == "backfill_grant_node_mismatch"
    assert mismatch["expected_node_id"] == target_id
    assert mismatch["authenticated_node_id"] != target_id
    assert responses["expired"].json()["detail"] == "backfill grant has expired"
    db = SessionLocal()
    try:
        task = db.get(PipelineTask, task_id)
        assert {**_lifecycle(task), "output_paths": task.output_paths} == before
    finally:
        db.close()


def test_node_identity_preflight_reports_the_authenticated_node(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    token = "preflight-token"
    node_id = _node(client, SessionLocal, token)
    response = client.get("/api/nodes/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == node_id
    assert response.json()["token_hash_prefix"] == hashlib.sha256(token.encode()).hexdigest()[:16]


def test_original_owner_needs_no_grant(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    token = "original-owner"
    owner_id = _node(client, SessionLocal, token)
    task_id = _task(client, SessionLocal, owner_id)
    response = client.post(f"/api/nodes/tasks/{task_id}/artifact-backfill",
                           headers={"Authorization": f"Bearer {token}"},
                           params={"key": "vocals", "filename": "vocals.wav"}, content=b"owner")
    assert response.status_code == 200, response.text


def test_operator_cli_issues_only_fresh_short_lived_vocals_grants(tmp_path, monkeypatch, capsys):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    owner_id = _node(client, SessionLocal, "old-owner")
    target_id = _node(client, SessionLocal, "target")
    task_id = _task(client, SessionLocal, owner_id)
    import grant_legacy_backfill as cli
    from app.db.models import LegacyArtifactBackfillGrant

    assert cli.main(["--task-id", task_id, "--node-id", target_id,
                     "--issuer", "ecs-op", "--ttl-minutes", "5"]) == 0
    grant_id = capsys.readouterr().out.strip()
    assert len(grant_id) == 32
    db = SessionLocal()
    try:
        grant = db.get(LegacyArtifactBackfillGrant, grant_id)
        assert grant.artifact_key == "vocals" and grant.state == "issued"
        assert timedelta(minutes=1) <= grant.expires_at.replace(tzinfo=timezone.utc) - grant.issued_at.replace(tzinfo=timezone.utc) <= timedelta(minutes=30)
    finally:
        db.close()
    with pytest.raises(ValueError, match="active vocals"):
        db = SessionLocal()
        try:
            cli.issue_grant(db, task_id=task_id, node_id=target_id, issuer="ecs-op", ttl_minutes=5)
        finally:
            db.close()


def test_migration_script_is_repeat_safe_on_sqlite(tmp_path):
    import migrate_legacy_backfill_grants as migration
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE pipeline_tasks (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE gpu_nodes (id VARCHAR(36) PRIMARY KEY)")
    migration.migrate(engine)
    migration.migrate(engine)
    columns = {column["name"] for column in inspect(engine).get_columns(
        "legacy_artifact_backfill_grants")}
    assert migration._REQUIRED_COLUMNS <= columns
