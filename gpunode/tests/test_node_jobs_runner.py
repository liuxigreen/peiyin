"""Offline tests for the probe-only NodeJob runner and idle-loop gate."""
from __future__ import annotations

import json
import pathlib
import types

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

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0) if self.post_responses else FakeResponse({})

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
