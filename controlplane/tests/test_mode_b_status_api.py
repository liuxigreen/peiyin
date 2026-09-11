"""HTTP coverage for the read-only Mode B status endpoint."""
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


def test_mode_b_status_returns_404_for_missing_project(tmp_path):
    response = _client(str(tmp_path / "missing.db")).get("/api/projects/no-such-project/mode-b/status")

    assert response.status_code == 404


def test_mode_b_status_returns_persisted_reconciler_state(tmp_path):
    client = _client(str(tmp_path / "status.db"))
    project_id = client.post("/api/projects", json={
        "name": "status", "target_lang": "en"}).json()["id"]
    srt = """1
00:00:00,000 --> 00:00:01,000
状态测试
"""
    seeded = client.post(f"/api/projects/{project_id}/seed-srt", json={"srt": srt})
    assert seeded.status_code == 200

    response = client.get(f"/api/projects/{project_id}/mode-b/status")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"state", "phase", "next_action", "blocked_reason", "selected_uids", "counts"}
    assert body["state"] == "blocked"
    assert body["phase"] == "separate-vocals"
    assert body["next_action"] == "create_separation"
    assert body["selected_uids"] and body["counts"]["selected"] == 1
