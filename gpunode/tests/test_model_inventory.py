"""ECAPA 本地快照清单：只允许预热发布后的离线加载。"""
from __future__ import annotations

import json
import socket
import urllib.request

from gpunode import model_inventory


def _write_manifest(path, snapshot, digest):
    path.write_text(json.dumps({
        "schema_version": 1,
        "ecapa": {
            "model_id": model_inventory.ECAPA_MODEL_ID,
            "release": "test-release-v1",
            "snapshot_path": str(snapshot),
            "snapshot_sha256": digest,
        },
    }), encoding="utf-8")


def test_preflight_accepts_a_valid_local_snapshot(tmp_path):
    snapshot = tmp_path / "ecapa"
    snapshot.mkdir()
    (snapshot / "hyperparams.yaml").write_text("modules: {}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, snapshot, model_inventory.snapshot_sha256(snapshot))

    result = model_inventory.ecapa_preflight(manifest)

    assert result.ready is True
    assert result.reason is None
    assert result.snapshot_path == str(snapshot)


def test_preflight_reports_absent_or_unconfigured_snapshot(tmp_path):
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, tmp_path / "absent", "a" * 64)

    absent = model_inventory.ecapa_preflight(manifest)
    assert absent.ready is False
    assert "snapshot unavailable" in absent.reason

    _write_manifest(manifest, tmp_path / "absent", None)
    unconfigured = model_inventory.ecapa_preflight(manifest)
    assert unconfigured.ready is False
    assert unconfigured.reason == "ECAPA snapshot_sha256 is not configured"


def test_preflight_detects_a_tampered_snapshot(tmp_path):
    snapshot = tmp_path / "ecapa"
    snapshot.mkdir()
    weight = snapshot / "embedding_model.ckpt"
    weight.write_bytes(b"verified")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, snapshot, model_inventory.snapshot_sha256(snapshot))
    weight.write_bytes(b"tampered")

    result = model_inventory.ecapa_preflight(manifest)

    assert result.ready is False
    assert result.reason == "ECAPA snapshot SHA256 mismatch"


def test_preflight_never_opens_a_network_connection(tmp_path, monkeypatch):
    snapshot = tmp_path / "ecapa"
    snapshot.mkdir()
    (snapshot / "model.ckpt").write_bytes(b"local-only")
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, snapshot, model_inventory.snapshot_sha256(snapshot))

    def forbid_network(*_args, **_kwargs):
        raise AssertionError("network access is forbidden during model preflight")

    monkeypatch.setattr(socket, "create_connection", forbid_network)
    monkeypatch.setattr(urllib.request, "urlopen", forbid_network)
    assert model_inventory.ecapa_preflight(manifest).ready is True
