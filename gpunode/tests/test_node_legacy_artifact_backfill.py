from __future__ import annotations

import json

import pytest

from gpunode import legacy_artifact_backfill as backfill


class Response:
    def __init__(self, status: int, body: object):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body


class Connection:
    def __init__(self, response: Response):
        self.response = response
        self.headers: list[tuple[str, str]] = []
        self.sent = bytearray()
        self.request_args = None
        self.closed = False

    def request(self, *args, **kwargs):
        self.request_args = (args, kwargs)

    def putrequest(self, *args):
        self.request_args = (args, {})

    def putheader(self, name, value):
        self.headers.append((name, value))

    def endheaders(self):
        pass

    def send(self, chunk):
        self.sent.extend(chunk)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def test_preflight_accepts_only_expected_node(monkeypatch):
    connection = Connection(Response(200, {"id": "node-1", "token_hash_prefix": "abc"}))
    monkeypatch.setattr(backfill, "_connection", lambda _url: (connection, ""))

    result = backfill.preflight("http://control", "token", "node-1", "abc")

    assert result["id"] == "node-1"
    assert connection.request_args[0] == ("GET", "/api/nodes/me")
    assert connection.request_args[1]["headers"] == {"Authorization": "Bearer token"}
    assert connection.closed


def test_preflight_blocks_wrong_token_identity(monkeypatch):
    connection = Connection(Response(200, {"id": "wrong", "token_hash_prefix": "abc"}))
    monkeypatch.setattr(backfill, "_connection", lambda _url: (connection, ""))

    with pytest.raises(backfill.BackfillError, match="node identity mismatch"):
        backfill.preflight("http://control", "token", "node-1", "abc")


def test_stream_upload_sends_file_in_chunks_without_reading_whole_file(monkeypatch, tmp_path):
    source = tmp_path / "vocals.wav"
    content = b"v" * (backfill.CHUNK_BYTES + 17)
    source.write_bytes(content)
    connection = Connection(Response(200, {"ok": True, "artifact": {"key": "vocals"}}))
    monkeypatch.setattr(backfill, "_connection", lambda _url: (connection, ""))

    result = backfill.stream_upload("http://control", "token", "task-1", "grant-1", source)

    assert result["ok"] is True
    assert bytes(connection.sent) == content
    assert ("Content-Length", str(len(content))) in connection.headers
    assert ("Authorization", "Bearer token") in connection.headers
    assert "key=vocals" in connection.request_args[0][1]
