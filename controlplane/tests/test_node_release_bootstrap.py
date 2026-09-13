"""Offline coverage for the node-authenticated release bootstrap endpoint."""
from __future__ import annotations

import base64
import importlib
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient


sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
NODE_SECRET = {"x-node-secret": "dev-node-secret"}
MANIFEST_URL = "https://ReLeAsE.Example.COM/releases/manifest.json"
TEST_KEY = b"release-bootstrap-test-key-32-byte!"


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'bootstrap.db'}")
    monkeypatch.setenv("NODE_SHARED_SECRET", "dev-node-secret")
    for name in ("NODE_RELEASE_BOOTSTRAP_NODE_IDS", "NODE_RELEASE_MANIFEST_URL",
                 "NODE_RELEASE_HMAC_KEY_B64"):
        monkeypatch.delenv(name, raising=False)
    import app.db.session as session_mod
    import app.api.nodes as nodes_mod
    import app.main as main_mod
    importlib.reload(session_mod)
    importlib.reload(nodes_mod)
    importlib.reload(main_mod)
    session_mod.init_db()
    return TestClient(main_mod.app), session_mod.SessionLocal


def _node(client: TestClient, name: str) -> dict[str, str]:
    result = client.post("/api/nodes/register", headers=NODE_SECRET, json={"name": name})
    assert result.status_code == 200, result.text
    return {"Authorization": f"Bearer {result.json()['node_token']}"}


def _node_id(client: TestClient, headers: dict[str, str]) -> str:
    result = client.get("/api/nodes/me", headers=headers)
    assert result.status_code == 200, result.text
    return result.json()["id"]


def _configure(monkeypatch, node_ids: str, *, url: str = MANIFEST_URL,
               key_b64: str | None = None) -> str:
    key_b64 = key_b64 if key_b64 is not None else base64.b64encode(TEST_KEY).decode("ascii")
    monkeypatch.setenv("NODE_RELEASE_BOOTSTRAP_NODE_IDS", node_ids)
    monkeypatch.setenv("NODE_RELEASE_MANIFEST_URL", url)
    monkeypatch.setenv("NODE_RELEASE_HMAC_KEY_B64", key_b64)
    return key_b64


def test_bootstrap_requires_authenticated_exact_allowlisted_node(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    allowed = _node(client, "allowed")
    other = _node(client, "other")
    _configure(monkeypatch, _node_id(client, allowed))

    assert client.get("/api/nodes/me/release-bootstrap").status_code == 401
    assert client.get("/api/nodes/me/release-bootstrap",
                      headers={"Authorization": "Bearer unknown-token"}).status_code == 403
    denied = client.get("/api/nodes/me/release-bootstrap", headers=other)
    assert denied.status_code == 403 and denied.json() == {"detail": "forbidden"}


def test_bootstrap_returns_only_expected_no_store_payload_without_state_writes(tmp_path, monkeypatch):
    client, SessionLocal = _client(tmp_path, monkeypatch)
    headers = _node(client, "allowed")
    node_id = _node_id(client, headers)
    configured_key = _configure(monkeypatch, node_id)

    from app.db.models import GpuNode, NodeJob, PipelineTask
    db = SessionLocal()
    try:
        node = db.get(GpuNode, node_id)
        original_release_state = (node.release_version, node.release_digest, node.release_ready,
                                  node.release_draining, node.release_reported_at)
        assert db.query(PipelineTask).count() == db.query(NodeJob).count() == 0
    finally:
        db.close()

    result = client.get("/api/nodes/me/release-bootstrap", headers=headers)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json() == {
        "schema_version": 1,
        "manifest_url": MANIFEST_URL,
        "allowed_hosts": ["release.example.com"],
        "hmac_key_b64": configured_key,
    }
    assert base64.b64decode(result.json()["hmac_key_b64"], validate=True) == TEST_KEY

    db = SessionLocal()
    try:
        node = db.get(GpuNode, node_id)
        assert (node.release_version, node.release_digest, node.release_ready,
                node.release_draining, node.release_reported_at) == original_release_state
        assert db.query(PipelineTask).count() == db.query(NodeJob).count() == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("node_ids", "url", "key_b64"),
    [
        ("", MANIFEST_URL, None),
        ("{node_id},", MANIFEST_URL, None),
        ("{node_id}", "http://release.example.com/manifest.json", None),
        ("{node_id}", "https://user@release.example.com/manifest.json", None),
        ("{node_id}", "https://release.example.com/manifest.json#fragment", None),
        ("{node_id}", MANIFEST_URL, "not base64!"),
        ("{node_id}", MANIFEST_URL, base64.b64encode(b"too-short").decode("ascii")),
        ("{node_id}", MANIFEST_URL, base64.b64encode(b"x" * 65).decode("ascii")),
    ],
)
def test_bootstrap_misconfiguration_fails_closed_without_leaking_values(
    tmp_path, monkeypatch, node_ids, url, key_b64
):
    client, _ = _client(tmp_path, monkeypatch)
    headers = _node(client, "allowed")
    node_id = _node_id(client, headers)
    raw_ids = node_ids.format(node_id=node_id)
    configured_key = _configure(monkeypatch, raw_ids, url=url, key_b64=key_b64)

    result = client.get("/api/nodes/me/release-bootstrap", headers=headers)
    assert result.status_code == 503 and result.json() == {"detail": "release bootstrap unavailable"}
    if raw_ids:
        assert raw_ids not in result.text
    assert url not in result.text
    assert configured_key not in result.text
    assert "NODE_RELEASE_" not in result.text
