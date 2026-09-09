"""Offline contract tests for pipeline artifact upload and completion order."""
from __future__ import annotations

import sys
import types

from gpunode import entrypoint


class FakeResponse:
    def __init__(self, error=None):
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def _install_router(monkeypatch, outputs):
    stages = types.ModuleType("stages")
    router = types.ModuleType("stages.router")
    router.run_task = lambda task: outputs
    monkeypatch.setitem(sys.modules, "stages", stages)
    monkeypatch.setitem(sys.modules, "stages.router", router)


def _setup_dispatch(monkeypatch, http, outputs):
    _install_router(monkeypatch, outputs)
    monkeypatch.setattr(entrypoint, "HTTP", http)
    monkeypatch.setattr(entrypoint.threading, "Thread", NoopThread)
    monkeypatch.setattr(entrypoint, "CONTROL", "https://control.example")
    monkeypatch.setitem(entrypoint.state, "token", "node-token")


def _task():
    return {"id": "task-1", "task_type": "tts"}


def test_existing_output_uploads_before_task_complete(monkeypatch, tmp_path):
    output = tmp_path / "line.wav"
    output.write_bytes(b"audio")
    http = FakeHTTP([FakeResponse(), FakeResponse()])
    _setup_dispatch(monkeypatch, http, [{"path": str(output), "key": "line-1"}])

    entrypoint.dispatch(_task())

    assert [url for url, _ in http.calls] == [
        "https://control.example/api/nodes/tasks/task-1/artifact",
        "https://control.example/api/nodes/tasks/task-1/complete",
    ]
    artifact_kwargs = http.calls[0][1]
    assert artifact_kwargs["content"] == b"audio"
    assert artifact_kwargs["params"] == {"filename": "line.wav", "key": "line-1"}
    assert http.calls[1][1]["json"] == {
        "outputs": [{"path": str(output), "key": "line-1"}]
    }


def test_upload_failure_skips_complete_and_fails_retryably(monkeypatch, tmp_path):
    output = tmp_path / "line.wav"
    output.write_bytes(b"audio")
    http = FakeHTTP([FakeResponse(RuntimeError("artifact rejected")), FakeResponse()])
    _setup_dispatch(monkeypatch, http, [{"path": str(output)}])

    entrypoint.dispatch(_task())

    assert [url for url, _ in http.calls] == [
        "https://control.example/api/nodes/tasks/task-1/artifact",
        "https://control.example/api/nodes/tasks/task-1/fail",
    ]
    assert http.calls[1][1]["json"]["retryable"] is True
    assert "artifact upload failed" in http.calls[1][1]["json"]["error"]
    assert output.read_bytes() == b"audio"


def test_no_output_completes_without_artifact_upload(monkeypatch):
    http = FakeHTTP([FakeResponse()])
    _setup_dispatch(monkeypatch, http, [])

    entrypoint.dispatch(_task())

    assert [url for url, _ in http.calls] == [
        "https://control.example/api/nodes/tasks/task-1/complete"
    ]
    assert http.calls[0][1]["json"] == {"outputs": []}
