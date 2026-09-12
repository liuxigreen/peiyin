"""Synchronous runner for the Stage 1 node-job queue.

The resident process can execute the small probe or one narrowly validated
legacy vocals backfill.  The latter uses the token supplied by the caller and
never reads a token file or discovers files around the claimed source path.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import time
from pathlib import Path
from typing import Any

try:
    from . import legacy_artifact_backfill
except ImportError:  # pragma: no cover - keeps direct script imports working
    import legacy_artifact_backfill


CLAIM_PATH = "/api/nodes/jobs/claim"
IDENTITY_PATH = "/api/nodes/me"
COMPLETE_PATH = "/api/nodes/jobs/{job_id}/complete"
FAIL_PATH = "/api/nodes/jobs/{job_id}/fail"
HEARTBEAT_PATH = "/api/nodes/jobs/{job_id}/heartbeat"
SUPPORTED_KIND = "probe"
LEGACY_VOCALS_BACKFILL_KIND = "legacy-vocals-backfill"
SUPPORTED_SPEC_VERSION = "1"
LEGACY_VOCALS_BACKFILL_SPEC_VERSION = "1"
RESULT_MAX_BYTES = 1024
ERROR_MAX_CHARS = 500
HEARTBEAT_INTERVAL_SECONDS = 30.0
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


class NodeJobValidationError(ValueError):
    """The claimed job or the probe result does not satisfy the contract."""


def compact_json_bytes(value: Any) -> bytes:
    """Serialize a value as strict, compact UTF-8 JSON."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise NodeJobValidationError("result must be strict JSON") from exc


def _url(control: str, path: str) -> str:
    return f"{control.rstrip('/')}{path}"


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _raise_for_status(response: Any) -> None:
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
        return
    status_code = getattr(response, "status_code", 200)
    if not 200 <= status_code < 300:
        raise RuntimeError(f"HTTP {status_code}")


def _raise_control_status(response: Any, operation: str) -> None:
    """Keep protocol failures non-retryable and transport/server failures retryable."""
    status_code = getattr(response, "status_code", 200)
    if isinstance(status_code, int):
        if 400 <= status_code < 500:
            raise NodeJobValidationError(f"{operation} rejected")
        if status_code >= 500:
            raise RuntimeError(f"{operation} unavailable: HTTP {status_code}")
        if not 200 <= status_code < 300:
            raise NodeJobValidationError(f"{operation} returned HTTP {status_code}")
    _raise_for_status(response)


def _claim(http_client: Any, control: str, token: str) -> dict[str, Any] | None:
    response = http_client.post(
        _url(control, CLAIM_PATH),
        headers=_auth_headers(token),
    )
    _raise_for_status(response)
    body = response.json()
    if not isinstance(body, dict) or "job" not in body:
        raise NodeJobValidationError("claim response must contain job")
    job = body["job"]
    if job is None:
        return None
    if not isinstance(job, dict):
        raise NodeJobValidationError("claimed job must be an object")
    return job


def _job_id(job: dict[str, Any]) -> str:
    value = job.get("id")
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise NodeJobValidationError("claimed job id is required")
    return value


def _validate_job(job: dict[str, Any]) -> None:
    kind = job.get("kind")
    if kind not in {SUPPORTED_KIND, LEGACY_VOCALS_BACKFILL_KIND}:
        raise NodeJobValidationError("unsupported node job kind")
    expected_spec = (
        LEGACY_VOCALS_BACKFILL_SPEC_VERSION
        if kind == LEGACY_VOCALS_BACKFILL_KIND
        else SUPPORTED_SPEC_VERSION
    )
    if job.get("spec_version") != expected_spec:
        raise NodeJobValidationError("unsupported node job spec_version")


def _required_node_id(job: dict[str, Any], field: str) -> str:
    value = job.get(field)
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise NodeJobValidationError(f"claimed job {field} is required")
    return value


def _validate_legacy_job(job: dict[str, Any]) -> tuple[str, str, str]:
    """Validate the exact queue contract and return node/task/path values."""
    _validate_job(job)
    if job.get("kind") != LEGACY_VOCALS_BACKFILL_KIND:
        raise NodeJobValidationError("unsupported node job kind")
    target_node_id = _required_node_id(job, "target_node_id")
    _required_node_id(job, "claimed_by")

    params = job.get("params")
    expected_keys = {"source_task_id", "source_path", "key", "filename"}
    if not isinstance(params, dict) or set(params) != expected_keys:
        raise NodeJobValidationError("legacy backfill params are invalid")
    source_task_id = params.get("source_task_id")
    if not isinstance(source_task_id, str) or not _SAFE_ID.fullmatch(source_task_id):
        raise NodeJobValidationError("legacy backfill source_task_id is invalid")
    source_path = params.get("source_path")
    if (not isinstance(source_path, str) or not source_path.strip()
            or "\x00" in source_path):
        raise NodeJobValidationError("legacy backfill source_path is invalid")
    if params.get("key") != "vocals" or params.get("filename") != "vocals.wav":
        raise NodeJobValidationError("legacy backfill artifact is invalid")
    return target_node_id, source_task_id, source_path


def _response_json(response: Any, operation: str) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception as exc:
        raise NodeJobValidationError(f"{operation} response must be JSON") from exc
    if not isinstance(body, dict):
        raise NodeJobValidationError(f"{operation} response must be an object")
    return body


def _node_identity(http_client: Any, control: str, token: str) -> str:
    response = http_client.get(
        _url(control, IDENTITY_PATH),
        headers=_auth_headers(token),
    )
    _raise_control_status(response, "node identity")
    body = _response_json(response, "node identity")
    node_id = body.get("id")
    if not isinstance(node_id, str) or not _SAFE_ID.fullmatch(node_id):
        raise NodeJobValidationError("node identity id is invalid")
    return node_id


def _heartbeat(http_client: Any, control: str, token: str, job_id: str) -> None:
    response = http_client.post(
        _url(control, HEARTBEAT_PATH.format(job_id=job_id)),
        headers=_auth_headers(token),
    )
    _raise_control_status(response, "job heartbeat")


def _legacy_result(upload_body: dict[str, Any]) -> dict[str, Any]:
    artifact = upload_body.get("artifact")
    if not isinstance(artifact, dict):
        raise NodeJobValidationError("artifact backfill response is missing artifact")
    if artifact.get("key") != "vocals" or artifact.get("filename") != "vocals.wav":
        raise NodeJobValidationError("artifact backfill response has invalid artifact")
    byte_count = artifact.get("bytes")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise NodeJobValidationError("artifact backfill response has invalid byte count")
    # Keep the durable job result deliberately small and free of paths, hashes,
    # grants, or any bearer material returned by an unexpected proxy.
    return {
        "artifact": {
            "key": "vocals",
            "filename": "vocals.wav",
            "bytes": byte_count,
        }
    }


def _run_legacy_upload(
    http_client: Any,
    control: str,
    token: str,
    job_id: str,
    job: dict[str, Any],
) -> dict[str, Any]:
    target_node_id, source_task_id, source_path = _validate_legacy_job(job)
    claimed_by = job["claimed_by"]
    identity_id = _node_identity(http_client, control, token)
    if identity_id != target_node_id or identity_id != claimed_by:
        raise NodeJobValidationError("node identity does not own legacy backfill")

    # Send one lease renewal before opening the source.  Subsequent renewals
    # happen before bounded reads, no more than 30 seconds apart.
    _heartbeat(http_client, control, token, job_id)
    last_heartbeat = [time.monotonic()]

    def renew_if_due() -> None:
        now = time.monotonic()
        if now - last_heartbeat[0] >= HEARTBEAT_INTERVAL_SECONDS:
            _heartbeat(http_client, control, token, job_id)
            last_heartbeat[0] = now

    upload_body = legacy_artifact_backfill.stream_upload_job(
        http_client,
        control,
        token,
        source_task_id,
        job_id,
        Path(source_path),
        filename="vocals.wav",
        on_before_read=renew_if_due,
    )
    return _legacy_result(upload_body)


def collect_probe(workdir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Collect the small, non-sensitive probe payload using the stdlib only."""
    path = os.getcwd() if workdir is None else os.fspath(workdir)
    usage = shutil.disk_usage(path)
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "workdir_disk_usage": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        },
    }


def _validated_result(result: Any) -> Any:
    encoded = compact_json_bytes(result)
    if len(encoded) > RESULT_MAX_BYTES:
        raise NodeJobValidationError(
            f"probe result exceeds {RESULT_MAX_BYTES} UTF-8 JSON bytes"
        )
    return result


def _error_text(exc: BaseException, *sensitive_values: str) -> str:
    try:
        message = str(exc)
    except Exception:
        message = "unprintable exception"
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    for value in sensitive_values:
        if value:
            text = text.replace(value, "<redacted>")
    text = re.sub(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])",
                  "<redacted>", text)
    return text[:ERROR_MAX_CHARS]


def _retryable(exc: BaseException) -> bool:
    return not isinstance(exc, (NodeJobValidationError, legacy_artifact_backfill.BackfillError))


def run_one(
    http_client: Any,
    control: str,
    token: str,
    *,
    workdir: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    """Claim and synchronously finish at most one node job.

    A successful run returns a compact status record.  An empty queue returns
    ``None``.  Validation and probe execution failures are reported through the
    job fail endpoint with an explicit retryable flag.
    """
    job = _claim(http_client, control, token)
    if job is None:
        return None

    job_id = _job_id(job)
    headers = _auth_headers(token)
    try:
        if job.get("kind") == LEGACY_VOCALS_BACKFILL_KIND:
            result = _run_legacy_upload(http_client, control, token, job_id, job)
        else:
            _validate_job(job)
            result = _validated_result(collect_probe(workdir))
    except Exception as exc:
        error = _error_text(
            exc,
            token,
            str(job.get("params", {}).get("source_path", ""))
            if isinstance(job.get("params"), dict) else "",
        )
        retryable = _retryable(exc)
        response = http_client.post(
            _url(control, FAIL_PATH.format(job_id=job_id)),
            headers=headers,
            json={"error": error, "retryable": retryable},
        )
        _raise_for_status(response)
        return {
            "job_id": job_id,
            "status": "failed",
            "error": error,
            "retryable": retryable,
        }

    response = http_client.post(
        _url(control, COMPLETE_PATH.format(job_id=job_id)),
        headers=headers,
        json={"result": result},
    )
    _raise_for_status(response)
    return {"job_id": job_id, "status": "completed", "result": result}


def run_node_job(
    http_client: Any,
    control: str,
    token: str,
    *,
    workdir: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    """Descriptive entrypoint for the one-job synchronous runner."""
    return run_one(http_client, control, token, workdir=workdir)


# Keep a concise alias for callers that prefer the polling operation name.
run_once = run_node_job
