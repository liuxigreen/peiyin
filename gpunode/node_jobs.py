"""Probe-only runner for the Stage 1 node-job queue.

The runner deliberately keeps the probe small and synchronous.  It reports a
few host facts that are useful for node health checks and never reads
environment variables, node-token files, or other file contents.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
from typing import Any


CLAIM_PATH = "/api/nodes/jobs/claim"
COMPLETE_PATH = "/api/nodes/jobs/{job_id}/complete"
FAIL_PATH = "/api/nodes/jobs/{job_id}/fail"
SUPPORTED_KIND = "probe"
SUPPORTED_SPEC_VERSION = "1"
RESULT_MAX_BYTES = 1024
ERROR_MAX_CHARS = 500


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
    if not isinstance(value, str) or not value:
        raise NodeJobValidationError("claimed job id is required")
    return value


def _validate_job(job: dict[str, Any]) -> None:
    if job.get("kind") != SUPPORTED_KIND:
        raise NodeJobValidationError("unsupported node job kind")
    if job.get("spec_version") != SUPPORTED_SPEC_VERSION:
        raise NodeJobValidationError("unsupported node job spec_version")


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


def _error_text(exc: BaseException) -> str:
    try:
        message = str(exc)
    except Exception:
        message = "unprintable exception"
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return text[:ERROR_MAX_CHARS]


def _retryable(exc: BaseException) -> bool:
    return not isinstance(exc, NodeJobValidationError)


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
        _validate_job(job)
        result = _validated_result(collect_probe(workdir))
    except Exception as exc:
        error = _error_text(exc)
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
