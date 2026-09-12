"""Offline tests for the probe-only NodeJob runner and idle-loop gate."""
from __future__ import annotations

import json
import pathlib
import types
from urllib.parse import parse_qs, urlparse

import pytest

from gpunode import entrypoint
from gpunode import node_jobs


class FakeResponse:
    def __init__(self, body=None, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    def __init__(self, post_responses=None, get_responses=None):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls = []
        self.get_calls = []
        self.upload_bodies = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        response = self.post_responses.pop(0) if self.post_responses else FakeResponse({})
        content = kwargs.get("content")
        if content is not None:
            self.upload_bodies.append(b"".join(content))
        return response

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0) if self.get_responses else FakeResponse({"task": None})


def _claimed_job(**overrides):
    job = {"id": "job-1", "kind": "probe", "spec_version": "1"}
    job.update(overrides)
    return job


def _assert_bearer(call, url):
    called_url, kwargs = call
    assert called_url == url
    assert kwargs["headers"] == {"Authorization": "Bearer node-token"}


def test_probe_success_claims_and_completes_once_with_bearer(monkeypatch):
    http = FakeHTTP(
        post_responses=[
            FakeResponse({"job": _claimed_job()}),
            FakeResponse({"job": _claimed_job(status="completed")}),
        ]
    )
    probe = {
        "hostname": "offline-node",
        "os": "Linux",
        "machine": "x86_64",
        "python_version": "3.12.0",
        "workdir_disk_usage": {"total_bytes": 100, "used_bytes": 40, "free_bytes": 60},
    }
    monkeypatch.setattr(node_jobs, "collect_probe", lambda _workdir=None: probe)

    result = node_jobs.run_one(http, "https://control.example/", "node-token")

    assert result["status"] == "completed"
    _assert_bearer(http.post_calls[0], "https://control.example/api/nodes/jobs/claim")
    _assert_bearer(http.post_calls[1], "https://control.example/api/nodes/jobs/job-1/complete")
    assert http.post_calls[1][1]["json"] == {"result": probe}
    assert len(node_jobs.compact_json_bytes(probe)) <= node_jobs.RESULT_MAX_BYTES


def test_empty_queue_does_not_complete_or_execute_probe(monkeypatch):
    http = FakeHTTP(post_responses=[FakeResponse({"job": None})])
    monkeypatch.setattr(
        node_jobs,
        "collect_probe",
        lambda _workdir=None: pytest.fail("empty queue must not execute probe"),
    )

    assert node_jobs.run_one(http, "http://control.example", "node-token") is None
    assert len(http.post_calls) == 1


def test_unknown_kind_is_failed_as_non_retryable(monkeypatch):
    http = FakeHTTP(
        post_responses=[
            FakeResponse({"job": _claimed_job(kind="shell")}),
            FakeResponse({"job": _claimed_job(status="pending")}),
        ]
    )
    monkeypatch.setattr(
        node_jobs,
        "collect_probe",
        lambda _workdir=None: pytest.fail("unknown kind must not execute probe"),
    )

    result = node_jobs.run_one(http, "http://control.example", "node-token")

    assert result["status"] == "failed"
    fail_url, fail_kwargs = http.post_calls[1]
    assert fail_url.endswith("/api/nodes/jobs/job-1/fail")
    assert fail_kwargs["json"]["retryable"] is False
    assert len(fail_kwargs["json"]["error"]) <= node_jobs.ERROR_MAX_CHARS


def test_probe_result_size_is_rejected_and_failed(monkeypatch):
    http = FakeHTTP(
        post_responses=[
            FakeResponse({"job": _claimed_job()}),
            FakeResponse({"job": _claimed_job(status="pending")}),
        ]
    )
    monkeypatch.setattr(node_jobs, "collect_probe", lambda _workdir=None: {"value": "值" * 600})

    result = node_jobs.run_one(http, "http://control.example", "node-token")

    assert result["status"] == "failed"
    assert http.post_calls[1][0].endswith("/fail")
    assert http.post_calls[1][1]["json"]["retryable"] is False
    assert "1024" in http.post_calls[1][1]["json"]["error"]
    assert len(http.post_calls) == 2


def test_probe_execution_error_is_failed_and_retryable(monkeypatch):
    http = FakeHTTP(
        post_responses=[
            FakeResponse({"job": _claimed_job()}),
            FakeResponse({"job": _claimed_job(status="pending")}),
        ]
    )

    def fail_probe(_workdir=None):
        raise OSError("disk temporarily unavailable")

    monkeypatch.setattr(node_jobs, "collect_probe", fail_probe)

    result = node_jobs.run_one(http, "http://control.example", "node-token")

    assert result["status"] == "failed"
    assert http.post_calls[1][1]["json"] == {
        "error": "OSError: disk temporarily unavailable",
        "retryable": True,
    }


def _legacy_job(source_path, **overrides):
    job = {
        "id": "job-legacy-1",
        "target_node_id": "node-1",
        "kind": "legacy-vocals-backfill",
        "spec_version": "1",
        "claimed_by": "node-1",
        "params": {
            "source_task_id": "task-legacy-1",
            "source_path": str(source_path),
            "key": "vocals",
            "filename": "vocals.wav",
        },
    }
    job.update(overrides)
    return job


def _legacy_http(job, *, upload_body=None, get_body=None, extra_posts=None):
    artifact = {"key": "vocals", "filename": "vocals.wav", "bytes": 0}
    if upload_body is not None:
        artifact["bytes"] = len(upload_body)
    responses = [
        FakeResponse({"job": job}),
        FakeResponse({"ok": True}),
        FakeResponse({"ok": True, "artifact": artifact}),
    ]
    if extra_posts:
        responses.extend(extra_posts)
    responses.append(FakeResponse({"ok": True}))
    return FakeHTTP(
        post_responses=responses,
        get_responses=[FakeResponse(get_body or {"id": "node-1"})],
    )


def test_legacy_job_claims_identity_heartbeats_streams_and_completes(tmp_path):
    source = tmp_path / "vocals.wav"
    content = b"v" * (node_jobs.legacy_artifact_backfill.CHUNK_BYTES + 17)
    source.write_bytes(content)
    job = _legacy_job(source)
    http = _legacy_http(job, upload_body=content)

    result = node_jobs.run_one(http, "http://control.example", "resident-token")

    assert result["status"] == "completed"
    assert result["result"] == {
        "artifact": {"key": "vocals", "filename": "vocals.wav", "bytes": len(content)}
    }
    assert http.upload_bodies == [content]
    assert [call[0] for call in http.post_calls] == [
        "http://control.example/api/nodes/jobs/claim",
        "http://control.example/api/nodes/jobs/job-legacy-1/heartbeat",
        "http://control.example/api/nodes/tasks/task-legacy-1/artifact-backfill?job_id=job-legacy-1&key=vocals&filename=vocals.wav",
        "http://control.example/api/nodes/jobs/job-legacy-1/complete",
    ]
    assert http.get_calls[0][0] == "http://control.example/api/nodes/me"
    for _url, kwargs in [*http.post_calls, *http.get_calls]:
        assert kwargs["headers"]["Authorization"] == "Bearer resident-token"
    query = parse_qs(urlparse(http.post_calls[2][0]).query)
    assert query == {"job_id": ["job-legacy-1"], "key": ["vocals"], "filename": ["vocals.wav"]}
    assert "resident-token" not in json.dumps(result)


def test_legacy_identity_mismatch_fails_before_opening_or_uploading(monkeypatch, tmp_path):
    source = tmp_path / "vocals.wav"
    source.write_bytes(b"must-not-be-read")
    job = _legacy_job(source)
    http = FakeHTTP(
        post_responses=[FakeResponse({"job": job}), FakeResponse({"ok": True})],
        get_responses=[FakeResponse({"id": "other-node"})],
    )

    def fail_open(*_args, **_kwargs):
        pytest.fail("identity mismatch must not open source")

    monkeypatch.setattr(pathlib.Path, "open", fail_open)
    result = node_jobs.run_one(http, "http://control.example", "resident-token")

    assert result["status"] == "failed"
    assert result["retryable"] is False
    assert http.upload_bodies == []
    assert [call[0] for call in http.post_calls] == [
        "http://control.example/api/nodes/jobs/claim",
        "http://control.example/api/nodes/jobs/job-legacy-1/fail",
    ]
    assert "resident-token" not in json.dumps(result)


def test_legacy_invalid_params_are_non_retryable_and_do_not_read_source(tmp_path):
    source = tmp_path / "vocals.wav"
    source.write_bytes(b"must-not-be-read")
    job = _legacy_job(source, params={
        "source_task_id": ["task-legacy-1"],
        "source_path": str(source),
        "key": "vocals",
        "filename": "vocals.wav",
    })
    http = FakeHTTP(
        post_responses=[FakeResponse({"job": job}), FakeResponse({"ok": True})]
    )

    result = node_jobs.run_one(http, "http://control.example", "resident-token")

    assert result["status"] == "failed"
    assert result["retryable"] is False
    assert http.get_calls == []
    assert http.upload_bodies == []


def test_legacy_upload_io_error_is_retryable(monkeypatch, tmp_path):
    source = tmp_path / "vocals.wav"
    source.write_bytes(b"chunk")
    job = _legacy_job(source)
    http = _legacy_http(job, upload_body=b"chunk")

    def fail_after_open(_source, **_kwargs):
        raise OSError("temporary read failure")

    monkeypatch.setattr(node_jobs.legacy_artifact_backfill, "iter_file_chunks", fail_after_open)
    result = node_jobs.run_one(http, "http://control.example", "resident-token")

    assert result["status"] == "failed"
    assert result["retryable"] is True
    fail_body = http.post_calls[-1][1]["json"]
    assert fail_body["retryable"] is True
    assert "resident-token" not in json.dumps(fail_body)


def test_legacy_heartbeat_renews_during_chunked_upload(monkeypatch, tmp_path):
    source = tmp_path / "vocals.wav"
    content = b"v" * 7
    source.write_bytes(content)
    job = _legacy_job(source)
    http = _legacy_http(
        job,
        upload_body=content,
        extra_posts=[FakeResponse({"ok": True})],
    )
    clock = iter([0.0, 31.0, 31.0])
    monkeypatch.setattr(node_jobs.time, "monotonic", lambda: next(clock))

    result = node_jobs.run_one(http, "http://control.example", "resident-token")

    assert result["status"] == "completed"
    heartbeat_urls = [
        url for url, _kwargs in http.post_calls if url.endswith("/heartbeat")
    ]
    assert len(heartbeat_urls) == 2
    assert http.upload_bodies == [content]


def test_collect_probe_uses_only_expected_stdlib_facts(monkeypatch, tmp_path):
    usage = types.SimpleNamespace(total=1000, used=400, free=600)
    monkeypatch.setattr(node_jobs.socket, "gethostname", lambda: "probe-host")
    monkeypatch.setattr(node_jobs.platform, "system", lambda: "TestOS")
    monkeypatch.setattr(node_jobs.platform, "machine", lambda: "test-machine")
    monkeypatch.setattr(node_jobs.platform, "python_version", lambda: "3.13.0")
    monkeypatch.setattr(node_jobs.shutil, "disk_usage", lambda path: (path == str(tmp_path)) and usage)

    result = node_jobs.collect_probe(tmp_path)

    assert result == {
        "hostname": "probe-host",
        "os": "TestOS",
        "machine": "test-machine",
        "python_version": "3.13.0",
        "workdir_disk_usage": {"total_bytes": 1000, "used_bytes": 400, "free_bytes": 600},
    }
    assert "token" not in json.dumps(result).lower()


class _LoopHTTP(FakeHTTP):
    pass


class _LoopThread:
    def __init__(self, target, args=(), daemon=False):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        if self.target.__name__ == "_worker":
            self.target(*self.args)


def _patch_main_common(monkeypatch, http, workers=1):
    monkeypatch.setattr(entrypoint, "register", lambda: None)
    monkeypatch.setattr(entrypoint, "heartbeat_loop", lambda: None)
    monkeypatch.setattr(entrypoint.threading, "Thread", _LoopThread)
    monkeypatch.setattr(entrypoint, "HTTP", http)
    monkeypatch.setattr(entrypoint, "POLL_IDLE", 0)
    monkeypatch.setenv("NODE_WORKERS", str(workers))


def test_advertised_capabilities_require_ready_ecapa(monkeypatch):
    monkeypatch.setenv("CAPABILITIES", "tts,asr,sep,diarize,tts")
    monkeypatch.setattr(entrypoint, "ecapa_preflight", lambda: types.SimpleNamespace(ready=False))
    assert entrypoint.advertised_capabilities() == ["tts", "asr", "sep"]

    monkeypatch.setattr(entrypoint, "ecapa_preflight", lambda: types.SimpleNamespace(ready=True))
    assert entrypoint.advertised_capabilities() == ["tts", "asr", "sep", "diarize"]


def test_pipeline_claim_has_priority_before_one_idle_node_job(monkeypatch):
    http = _LoopHTTP(
        get_responses=[
            FakeResponse({"task": {"id": "task-1", "task_type": "tts"}}),
            FakeResponse({"task": None}),
        ]
    )
    _patch_main_common(monkeypatch, http)
    dispatched = []
    node_claims = []
    monkeypatch.setattr(entrypoint, "dispatch", lambda task: dispatched.append(task))
    monkeypatch.setattr(entrypoint, "run_node_job", lambda: node_claims.append(True))

    sleeps = []

    def stop_after_idle(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(entrypoint.time, "sleep", stop_after_idle)
    with pytest.raises(SystemExit):
        entrypoint.main()

    assert dispatched == [{"id": "task-1", "task_type": "tts"}]
    assert node_claims == [True]
    assert [call[0] for call in http.get_calls] == [
        "http://localhost:8500/api/nodes/me/claim",
        "http://localhost:8500/api/nodes/me/claim",
    ]


def test_pool_full_does_not_pipeline_claim_or_node_claim(monkeypatch):
    http = _LoopHTTP()
    _patch_main_common(monkeypatch, http)
    node_claims = []
    monkeypatch.setattr(entrypoint, "run_node_job", lambda: node_claims.append(True))

    class FullSemaphore:
        def __init__(self, _workers):
            self.acquire_calls = 0

        def acquire(self, blocking=False):
            self.acquire_calls += 1
            assert blocking is False
            return False

        def release(self):
            raise AssertionError("full pool must not release a token")

    monkeypatch.setattr(entrypoint.threading, "BoundedSemaphore", FullSemaphore)
    monkeypatch.setattr(entrypoint.time, "sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(SystemExit):
        entrypoint.main()

    assert http.get_calls == []
    assert node_claims == []


def test_pipeline_token_reregister_path_does_not_claim_node_job(monkeypatch):
    http = _LoopHTTP(get_responses=[FakeResponse({}, status_code=401)])
    _patch_main_common(monkeypatch, http)
    registered = []
    node_claims = []
    def reregister_then_stop():
        registered.append(True)
        if len(registered) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(entrypoint, "register", reregister_then_stop)
    monkeypatch.setattr(entrypoint, "run_node_job", lambda: node_claims.append(True))
    monkeypatch.setattr(entrypoint, "TOKEN_FILE", str(pathlib.Path("/tmp/missing-node-token")))

    def stop_after_reregister(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(entrypoint.time, "sleep", stop_after_reregister)
    with pytest.raises(SystemExit):
        entrypoint.main()

    assert registered == [True, True]
    assert node_claims == []
    assert len(http.post_calls) == 0
