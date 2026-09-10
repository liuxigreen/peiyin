"""Stage 1 NodeJob 队列：管理鉴权、节点归属、原子领取和 lease 回收。"""
from __future__ import annotations

import concurrent.futures
import importlib
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

ADMIN = {"Authorization": "Bearer node-job-admin"}
NODE_SECRET = {"x-node-secret": "dev-node-secret"}


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    dbfile = tmp_path / "node-jobs.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("API_TOKEN", "node-job-admin")
    monkeypatch.setenv("NODE_SHARED_SECRET", "dev-node-secret")

    import app.db.session as dbsession
    importlib.reload(dbsession)
    dbsession.init_db()
    import app.main as appmain
    importlib.reload(appmain)
    with TestClient(appmain.app) as client:
        yield client, dbsession.SessionLocal


def _register(client: TestClient, name: str, capabilities=None) -> str:
    response = client.post(
        "/api/nodes/register",
        headers=NODE_SECRET,
        json={"name": name, "gpu_model": "test", "capabilities": capabilities or ["probe"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["node_token"]


def _submit(client: TestClient, target: str = "node-a", **extra):
    body = {"target_node_name": target, "kind": "probe", "params": {"x": 1}}
    body.update(extra)
    response = client.post("/api/node-jobs", headers=ADMIN, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_heartbeat_updates_validated_node_capabilities(app_client):
    client, SessionLocal = app_client
    token = _register(client, "capability-node")
    headers = {"Authorization": f"Bearer {token}"}

    updated = client.post("/api/nodes/heartbeat", headers=headers,
                          json={"capabilities": ["tts", "asr", "sep", "diarize", "tts"]})
    assert updated.status_code == 200, updated.text
    invalid = client.post("/api/nodes/heartbeat", headers=headers,
                          json={"capabilities": ["diarize", "not valid"]})
    assert invalid.status_code == 400

    db = SessionLocal()
    try:
        from app.db.models import GpuNode
        node = db.query(GpuNode).filter_by(token_hash=__import__("hashlib").sha256(token.encode()).hexdigest()).one()
        assert node.capabilities == ["tts", "asr", "sep", "diarize"]
    finally:
        db.close()


def test_pipeline_claim_requires_node_capability(app_client):
    client, SessionLocal = app_client
    token = _register(client, "capability-node", ["tts"])
    pid = client.post("/api/projects", headers=ADMIN,
                      json={"name": "claim gate", "target_lang": "en"}).json()["id"]
    db = SessionLocal()
    try:
        from app.db.models import PipelineTask
        db.add_all([
            PipelineTask(project_id=pid, task_key="DIARIZE/cap", task_type="diarize",
                         resource="gpu", gpu_required=True, status="pending"),
            PipelineTask(project_id=pid, task_key="TTS/cap", task_type="tts-generate",
                         resource="gpu", gpu_required=True, status="pending"),
        ])
        db.commit()
    finally:
        db.close()

    claimed = client.get("/api/nodes/me/claim", headers={"Authorization": f"Bearer {token}"})
    assert claimed.status_code == 200
    assert claimed.json()["task"]["task_type"] == "tts-generate"


def test_management_auth_and_submit(app_client):
    client, _SessionLocal = app_client
    body = {"target_node_name": "node-a", "kind": "probe", "params": {"x": 1}}
    assert client.post("/api/node-jobs", json=body).status_code == 401
    assert client.get("/api/node-jobs").status_code == 401

    created = client.post("/api/node-jobs", headers=ADMIN, json=body)
    assert created.status_code == 200, created.text
    job = created.json()
    assert job["status"] == "pending"
    assert job["target_node_name"] == "node-a"
    assert job["kind"] == "probe"
    assert job["spec_version"] == "1"
    assert job["created_at"] and job["updated_at"]
    listed = client.get("/api/node-jobs", headers=ADMIN).json()
    assert any(row["id"] == job["id"] for row in listed)


def test_node_auth_kind_and_target_name_filter(app_client):
    client, _SessionLocal = app_client
    assert client.post(
        "/api/node-jobs",
        headers=ADMIN,
        json={"target_node_name": "node-a", "kind": "shell"},
    ).status_code == 422

    node_a = _register(client, "node-a")
    node_b = _register(client, "node-b")
    job = _submit(client, target="node-a")

    assert client.post("/api/nodes/jobs/claim").status_code == 401
    wrong = client.post("/api/nodes/jobs/claim", headers={"Authorization": f"Bearer {node_b}"})
    assert wrong.status_code == 200 and wrong.json()["job"] is None, wrong.text
    claimed = client.post("/api/nodes/jobs/claim", headers={"Authorization": f"Bearer {node_a}"})
    assert claimed.status_code == 200
    assert claimed.json()["job"]["id"] == job["id"]


def test_same_name_reregistration_claims_pending_job(app_client):
    client, SessionLocal = app_client
    _old_token = _register(client, "stable-node")
    new_token = _register(client, "stable-node")
    job = _submit(client, target="stable-node")

    response = client.post(
        "/api/nodes/jobs/claim",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert response.status_code == 200 and response.json()["job"]["id"] == job["id"]
    db = SessionLocal()
    try:
        from app.db.models import GpuNode, NodeJob

        assert db.query(GpuNode).filter_by(name="stable-node").count() == 2
        claimed = db.get(NodeJob, job["id"])
        assert claimed.status == "running" and claimed.claimed_by
    finally:
        db.close()


def test_concurrent_claim_is_unique(app_client):
    client, SessionLocal = app_client
    token_a = _register(client, "race-node")
    token_b = _register(client, "race-node")
    job = _submit(client, target="race-node")

    def claim(token: str):
        return client.post(
            "/api/nodes/jobs/claim",
            headers={"Authorization": f"Bearer {token}"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(claim, (token_a, token_b)))
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    claimed = [response.json()["job"] for response in responses if response.json()["job"]]
    assert len(claimed) == 1, [response.json() for response in responses]
    assert claimed[0]["id"] == job["id"]
    db = SessionLocal()
    try:
        from app.db.models import NodeJob

        assert db.get(NodeJob, job["id"]).status == "running"
    finally:
        db.close()


def test_owner_heartbeat_complete_and_fail_and_limits(app_client):
    client, SessionLocal = app_client
    owner = _register(client, "owner-node")
    other = _register(client, "other-node")
    headers = {"Authorization": f"Bearer {owner}"}
    other_headers = {"Authorization": f"Bearer {other}"}

    job = _submit(client, target="owner-node")
    job_id = job["id"]
    assert client.post(f"/api/nodes/jobs/{job_id}/heartbeat", headers=other_headers).status_code == 409
    assert client.post(f"/api/nodes/jobs/{job_id}/complete", headers=other_headers,
                       json={"result": {"ok": True}}).status_code == 409
    assert client.post(f"/api/nodes/jobs/{job_id}/fail", headers=other_headers,
                       json={"error": "bad"}).status_code == 409

    claimed = client.post("/api/nodes/jobs/claim", headers=headers).json()["job"]
    assert claimed["completed_at"] is None
    before = claimed["lease_until"]
    beat = client.post(
        f"/api/nodes/jobs/{job_id}/heartbeat",
        headers=headers,
        json={"checkpoint": {"step": 2}},
    )
    assert beat.status_code == 200
    assert beat.json()["job"]["checkpoint"] == {"step": 2}
    assert beat.json()["job"]["lease_until"] >= before

    # JSON 字节边界按紧凑、非 ASCII 转义的 UTF-8 计算：引号占两字节。
    accepted = "x" * 1022
    complete = client.post(
        f"/api/nodes/jobs/{job_id}/complete",
        headers=headers,
        json={"result": accepted},
    )
    assert complete.status_code == 200, complete.text
    completed = complete.json()["job"]
    assert completed["status"] == "completed" and completed["completed_at"]
    assert completed["lease_until"] is None
    assert client.post(f"/api/nodes/jobs/{job_id}/heartbeat", headers=headers).status_code == 409

    too_large = _submit(client, target="owner-node")
    too_large_id = too_large["id"]
    assert client.post("/api/nodes/jobs/claim", headers=headers).json()["job"]["id"] == too_large_id
    rejected_result = client.post(
        f"/api/nodes/jobs/{too_large_id}/complete",
        headers=headers,
        json={"result": "x" * 1023},
    )
    assert rejected_result.status_code == 422
    db = SessionLocal()
    try:
        from app.db.models import NodeJob

        assert db.get(NodeJob, too_large_id).status == "running"
    finally:
        db.close()

    error_boundary = _submit(client, target="owner-node", max_retries=1)
    error_id = error_boundary["id"]
    assert client.post("/api/nodes/jobs/claim", headers=headers).json()["job"]["id"] == error_id
    accepted_error = client.post(
        f"/api/nodes/jobs/{error_id}/fail",
        headers=headers,
        json={"error": "e" * 500},
    )
    assert accepted_error.status_code == 200
    assert accepted_error.json()["job"]["status"] == "pending"

    # 消耗该作业的最后一次重试，随后队列才会轮到新作业。
    assert client.post("/api/nodes/jobs/claim", headers=headers).json()["job"]["id"] == error_id
    dead = client.post(
        f"/api/nodes/jobs/{error_id}/fail",
        headers=headers,
        json={"error": "terminal"},
    )
    assert dead.status_code == 200 and dead.json()["job"]["status"] == "dead"

    next_job = _submit(client, target="owner-node", max_retries=1)
    assert client.post("/api/nodes/jobs/claim", headers=headers).json()["job"]["id"] == next_job["id"]
    rejected_error = client.post(
        f"/api/nodes/jobs/{next_job['id']}/fail",
        headers=headers,
        json={"error": "e" * 501},
    )
    assert rejected_error.status_code == 422


def test_reaper_retries_to_dead_and_preserves_checkpoint(app_client):
    client, SessionLocal = app_client
    token = _register(client, "reaper-node")
    headers = {"Authorization": f"Bearer {token}"}
    created = _submit(
        client,
        target="reaper-node",
        checkpoint={"offset": 7, "opaque": ["keep", 1]},
        max_retries=2,
    )
    job_id = created["id"]
    assert client.post("/api/nodes/jobs/claim", headers=headers).json()["job"]["id"] == job_id

    from app.db.models import NodeJob
    from app.orchestrator_reaper import reap_expired

    db = SessionLocal()
    try:
        job = db.get(NodeJob, job_id)
        job.lease_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        reclaimed = reap_expired(db, datetime.now(timezone.utc))
        assert job_id in reclaimed
        db.refresh(job)
        assert job.status == "pending" and job.retry_count == 1
        assert job.claimed_by is None and job.lease_until is None
        assert job.checkpoint == {"offset": 7, "opaque": ["keep", 1]}

        job.status = "running"
        job.claimed_by = "old-owner"
        job.lease_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        reap_expired(db, datetime.now(timezone.utc))
        db.refresh(job)
        assert job.status == "dead" and job.retry_count == 2
        assert job.claimed_by is None and job.lease_until is None
        assert job.checkpoint == {"offset": 7, "opaque": ["keep", 1]}
        assert job.completed_at is None
        assert len(job.error or "") <= 500
    finally:
        db.close()
