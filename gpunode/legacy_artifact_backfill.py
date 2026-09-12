"""Safely stream one operator-authorized legacy vocals artifact to control plane.

This is intentionally a narrow maintenance utility, not a queue worker.  It
always reads the token used by the resident node process and verifies the
control-plane node identity before it sends any bytes.  That prevents a stale
environment token from spending minutes uploading only to receive a 409.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
from pathlib import Path
from collections.abc import Callable, Iterator
from urllib.parse import urlencode, urlparse


CHUNK_BYTES = 1024 * 1024
DEFAULT_TOKEN_FILE = Path(__file__).with_name("workdir") / "node_token.txt"


class BackfillError(RuntimeError):
    """The preflight or artifact upload did not complete safely."""


def _validate_source(source: Path) -> int:
    """Validate one exact regular-file source and return its current size."""
    try:
        if source.is_symlink() or not source.is_file():
            raise BackfillError("artifact source must be a regular file")
        return source.stat().st_size
    except BackfillError:
        raise
    except OSError as exc:
        raise BackfillError("artifact source cannot be inspected") from exc


def iter_file_chunks(
    source: Path,
    *,
    on_before_read: Callable[[], None] | None = None,
    chunk_bytes: int = CHUNK_BYTES,
) -> Iterator[bytes]:
    """Yield one exact source file in bounded chunks.

    ``on_before_read`` is called before each bounded read so a caller can
    renew a lease while a large upload is in progress.  It deliberately does
    not discover adjacent files or resolve/extend the supplied path.
    """
    _validate_source(source)
    if chunk_bytes < 1:
        raise BackfillError("chunk size must be positive")
    try:
        with source.open("rb") as artifact:
            while True:
                if on_before_read is not None:
                    on_before_read()
                chunk = artifact.read(chunk_bytes)
                if not chunk:
                    return
                yield chunk
    except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
        raise BackfillError("artifact source cannot be opened") from exc


def read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BackfillError(f"cannot read node token file: {path}") from exc
    if not token:
        raise BackfillError("node token file is empty")
    return token


def _connection(control: str) -> tuple[http.client.HTTPConnection, str]:
    parsed = urlparse(control)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BackfillError("control must be an absolute http(s) URL")
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    return connection_class(parsed.netloc, timeout=30), parsed.path.rstrip("/")


def _json_response(connection: http.client.HTTPConnection) -> tuple[int, object]:
    response = connection.getresponse()
    raw = response.read()
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = raw.decode("utf-8", errors="replace")[:500]
    return response.status, body


def preflight(control: str, token: str, expected_node_id: str,
              expected_hash_prefix: str) -> dict:
    connection, prefix = _connection(control)
    try:
        connection.request("GET", f"{prefix}/api/nodes/me",
                           headers={"Authorization": f"Bearer {token}"})
        status, body = _json_response(connection)
    finally:
        connection.close()
    if status != 200 or not isinstance(body, dict):
        raise BackfillError(f"node identity preflight failed: HTTP {status}: {body}")
    if body.get("id") != expected_node_id or body.get("token_hash_prefix") != expected_hash_prefix:
        raise BackfillError(
            "node identity mismatch: "
            f"got id={body.get('id')} hash_prefix={body.get('token_hash_prefix')}; "
            f"expected id={expected_node_id} hash_prefix={expected_hash_prefix}"
        )
    return body


def stream_upload(control: str, token: str, task_id: str, grant_id: str,
                  source: Path, *, filename: str = "vocals.wav",
                  job_id: str | None = None) -> dict:
    """Stream either a legacy grant upload or a queued job upload.

    The positional grant arguments remain for the operator CLI.  Queue
    callers may provide ``job_id`` and omit the grant; the queue runner uses
    :func:`stream_upload_job` below so it can use its resident HTTP client and
    lease callback.
    """
    size = _validate_source(source)
    if job_id is not None:
        if not job_id:
            raise BackfillError("job id is required")
        query = urlencode({"job_id": job_id, "key": "vocals", "filename": filename})
    else:
        if not grant_id:
            raise BackfillError("grant id is required")
        query = urlencode({"filename": filename, "key": "vocals", "grant_id": grant_id})
    connection, prefix = _connection(control)
    path = f"{prefix}/api/nodes/tasks/{task_id}/artifact-backfill?{query}"
    try:
        connection.putrequest("POST", path)
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(size))
        connection.endheaders()
        for chunk in iter_file_chunks(source):
            connection.send(chunk)
        status, body = _json_response(connection)
    finally:
        connection.close()
    if not 200 <= status < 300:
        raise BackfillError(f"artifact backfill failed: HTTP {status}: {body}")
    if not isinstance(body, dict):
        raise BackfillError("artifact backfill response was not JSON")
    return body


def _client_response_status(response: object, operation: str) -> None:
    """Classify a resident-client response without exposing response content."""
    status = getattr(response, "status_code", 200)
    if isinstance(status, int):
        if 400 <= status < 500:
            raise BackfillError(f"{operation} rejected: HTTP {status}")
        if status >= 500:
            raise RuntimeError(f"{operation} unavailable: HTTP {status}")
        if not 200 <= status < 300:
            raise BackfillError(f"{operation} returned HTTP {status}")
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()


def stream_upload_job(
    http_client: object,
    control: str,
    token: str,
    task_id: str,
    job_id: str,
    source: Path,
    *,
    filename: str = "vocals.wav",
    on_before_read: Callable[[], None] | None = None,
) -> dict:
    """Stream one queue-authorized artifact through the resident HTTP client."""
    size = _validate_source(source)
    if not job_id:
        raise BackfillError("job id is required")
    if filename != "vocals.wav":
        raise BackfillError("legacy queue upload requires vocals.wav")
    query = urlencode({"job_id": job_id, "key": "vocals", "filename": filename})
    url = (
        f"{control.rstrip('/')}/api/nodes/tasks/{task_id}/artifact-backfill?{query}"
    )
    response = http_client.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        },
        content=iter_file_chunks(source, on_before_read=on_before_read),
    )
    _client_response_status(response, "artifact backfill")
    try:
        body = response.json()
    except Exception as exc:
        raise BackfillError("artifact backfill response was not JSON") from exc
    if not isinstance(body, dict):
        raise BackfillError("artifact backfill response was not JSON")
    return body


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--grant-id", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-node-id", required=True)
    parser.add_argument("--expected-token-hash-prefix", required=True)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        token = read_token(args.token_file)
        # Detect a mistyped expected prefix locally too, without printing a token.
        if not hashlib.sha256(token.encode()).hexdigest().startswith(args.expected_token_hash_prefix):
            raise BackfillError("token file does not match the expected token hash prefix")
        identity = preflight(args.control, token, args.expected_node_id,
                             args.expected_token_hash_prefix)
        result = stream_upload(args.control, token, args.task_id, args.grant_id, args.source)
    except BackfillError as exc:
        print(f"[backfill] blocked: {exc}")
        return 2
    print(json.dumps({"ok": True, "node": identity, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
