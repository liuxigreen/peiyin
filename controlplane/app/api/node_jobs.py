"""Stage 1 NodeJob 队列 API。

NodeJob 是一条独立于 PipelineTask 的轻量节点作业通道。管理端负责提交和
查看作业，节点端通过现有 ``nodes._auth_node`` 认证后按稳定节点名领取，
并只能更新自己持有的 running 作业。节点路径为 ``/api/nodes/jobs``。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from ..db import models as m
from ..db.session import get_db
from .nodes import _auth_node


LEASE_MINUTES = 10
RESULT_MAX_BYTES = 1024
ERROR_MAX_CHARS = 500
ALLOWED_KINDS = frozenset({"probe"})
# 便于调用方和测试按语义名称导入；白名单仍只有 probe。
NODE_JOB_KINDS = ALLOWED_KINDS


def _require_admin(authorization: str = Header(default="")) -> None:
    """与 main.py 的 API_TOKEN 中间件保持一致的管理端边界。

    API_TOKEN 为空时是项目既有的本地开发模式；配置后管理端必须带
    ``Bearer <API_TOKEN>``。节点路由不使用这个依赖，而走 _auth_node。
    """
    expected = os.getenv("API_TOKEN", "")
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="unauthorized")


router = APIRouter(
    prefix="/api/node-jobs",
    tags=["node-jobs"],
    dependencies=[Depends(_require_admin)],
)
node_router = APIRouter(prefix="/api/nodes/jobs", tags=["node-jobs"])


def _validation_error(detail: str) -> None:
    raise HTTPException(status_code=422, detail=detail)


def compact_json_bytes(value: Any) -> bytes:
    """返回协议规定的紧凑 UTF-8 JSON；不可编码值直接视为非法。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=422, detail="value must be JSON serializable") from exc


def _validate_json_value(value: Any, field: str) -> Any:
    compact_json_bytes(value)
    return value


def _validate_result(value: Any) -> Any:
    encoded = compact_json_bytes(value)
    if len(encoded) > RESULT_MAX_BYTES:
        _validation_error(f"result exceeds {RESULT_MAX_BYTES} UTF-8 JSON bytes")
    return value


def _validate_error(value: Any) -> str:
    if not isinstance(value, str):
        _validation_error("error must be a string")
    if len(value) > ERROR_MAX_CHARS:
        _validation_error(f"error exceeds {ERROR_MAX_CHARS} characters")
    return value


def _create_values(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        _validation_error("request body must be an object")

    target = body.get("target_node_name")
    if not isinstance(target, str) or not target.strip():
        _validation_error("target_node_name is required")
    target = target.strip()
    if len(target) > 100:
        _validation_error("target_node_name exceeds 100 characters")

    kind = body.get("kind", "probe")
    if not isinstance(kind, str) or kind not in ALLOWED_KINDS:
        _validation_error("unsupported node job kind")

    spec_version = body.get("spec_version", "1")
    if isinstance(spec_version, bool) or spec_version is None:
        _validation_error("spec_version is required")
    spec_version = str(spec_version)
    if not spec_version or len(spec_version) > 32:
        _validation_error("spec_version must be 1..32 characters")

    params = body.get("params")
    if params is None:
        params = {}
    _validate_json_value(params, "params")

    checkpoint = body.get("checkpoint")
    if checkpoint is not None:
        _validate_json_value(checkpoint, "checkpoint")

    max_retries = body.get("max_retries", 3)
    if isinstance(max_retries, bool):
        _validation_error("max_retries must be a non-negative integer")
    try:
        max_retries = int(max_retries)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="max_retries must be a non-negative integer",
        ) from exc
    if max_retries < 0:
        _validation_error("max_retries must be a non-negative integer")

    return {
        "target_node_name": target,
        "kind": kind,
        "spec_version": spec_version,
        "params": params,
        "checkpoint": checkpoint,
        "max_retries": max_retries,
    }


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _job_payload(job: m.NodeJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "target_node_name": job.target_node_name,
        "kind": job.kind,
        "spec_version": job.spec_version,
        "params": job.params,
        "status": job.status,
        "checkpoint": job.checkpoint,
        "result": job.result,
        "claimed_by": job.claimed_by,
        "lease_until": _timestamp(job.lease_until),
        "heartbeat_at": _timestamp(job.heartbeat_at),
        "completed_at": _timestamp(job.completed_at),
        "retry_count": job.retry_count,
        "max_retries": job.max_retries,
        "error": job.error,
        "created_at": _timestamp(job.created_at),
        "updated_at": _timestamp(job.updated_at),
    }


@router.post("")
def create_job(body: dict[str, Any], db: Session = Depends(get_db)):
    values = _create_values(body)
    job = m.NodeJob(**values)
    db.add(job)
    db.commit()
    db.refresh(job)
    return _job_payload(job)


@router.get("")
def list_jobs(
    status: str | None = None,
    target_node_name: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    if limit < 1 or limit > 500:
        _validation_error("limit must be between 1 and 500")
    query = db.query(m.NodeJob)
    if status:
        query = query.filter(m.NodeJob.status == status)
    if target_node_name:
        query = query.filter(m.NodeJob.target_node_name == target_node_name)
    rows = query.order_by(m.NodeJob.created_at.asc(), m.NodeJob.id.asc()).limit(limit).all()
    return [_job_payload(job) for job in rows]


@router.get("/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(m.NodeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="node job not found")
    return _job_payload(job)


_CLAIM_PG = """
UPDATE node_jobs
SET status = 'running',
    claimed_by = :node_id,
    lease_until = NOW() + INTERVAL '10 minutes',
    heartbeat_at = NOW(),
    updated_at = NOW()
WHERE id = (
    SELECT id
    FROM node_jobs
    WHERE status = 'pending'
      AND target_node_name = :target_node_name
    ORDER BY created_at ASC, id ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id
"""

_CLAIM_SQLITE = """
UPDATE node_jobs
SET status = 'running',
    claimed_by = :node_id,
    lease_until = datetime('now', '+10 minutes'),
    heartbeat_at = datetime('now'),
    updated_at = datetime('now')
WHERE id = (
    SELECT id
    FROM node_jobs
    WHERE status = 'pending'
      AND target_node_name = :target_node_name
    ORDER BY created_at ASC, id ASC
    LIMIT 1
)
  AND status = 'pending'
  AND target_node_name = :target_node_name
RETURNING id
"""

# Public aliases make the atomic strategy easy to inspect without coupling
# callers to the private spelling used above.
CLAIM_PG = _CLAIM_PG
CLAIM_SQLITE = _CLAIM_SQLITE


def _claim_for_node(db: Session, node: m.GpuNode) -> m.NodeJob | None:
    dialect = db.get_bind().dialect.name
    statement = _CLAIM_PG if dialect == "postgresql" else _CLAIM_SQLITE
    row = db.execute(
        sa_text(statement),
        {"node_id": node.id, "target_node_name": node.name},
    ).mappings().first()
    db.commit()
    if row is None:
        return None
    return db.get(m.NodeJob, row["id"])


def _claim_response(db: Session, authorization: str) -> dict[str, Any]:
    node = _auth_node(db, authorization)
    job = _claim_for_node(db, node)
    return {"job": _job_payload(job) if job is not None else None}


@node_router.post("/claim")
def claim_job(
    body: dict[str, Any] | None = None,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    # body 预留给后续批量/能力过滤协议；Stage 1 的路由只信任认证节点名。
    return _claim_response(db, authorization)


@node_router.get("/claim")
def claim_job_get(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    return _claim_response(db, authorization)


def _owned_job(db: Session, job_id: str, authorization: str) -> tuple[m.NodeJob, m.GpuNode]:
    node = _auth_node(db, authorization)
    job = db.get(m.NodeJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="node job not found")
    if job.status != "running" or job.claimed_by != node.id:
        raise HTTPException(status_code=409, detail="node job is not owned by this node")
    return job, node


def _apply_checkpoint(job: m.NodeJob, body: dict[str, Any]) -> None:
    if "checkpoint" in body:
        _validate_json_value(body["checkpoint"], "checkpoint")
        job.checkpoint = body["checkpoint"]


@node_router.post("/{job_id}/heartbeat")
def heartbeat_job(
    job_id: str,
    body: dict[str, Any] | None = None,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    job, _node = _owned_job(db, job_id, authorization)
    body = body or {}
    _apply_checkpoint(job, body)
    now = datetime.now(timezone.utc)
    job.heartbeat_at = now
    job.lease_until = now + timedelta(minutes=LEASE_MINUTES)
    db.commit()
    db.refresh(job)
    return {"ok": True, "job": _job_payload(job)}


@node_router.post("/{job_id}/complete")
def complete_job(
    job_id: str,
    body: dict[str, Any] | None = None,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    job, _node = _owned_job(db, job_id, authorization)
    body = body or {}
    if "result" in body:
        _validate_result(body["result"])
        job.result = body["result"]
    _apply_checkpoint(job, body)
    job.status = "completed"
    job.lease_until = None
    job.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return {"ok": True, "job": _job_payload(job)}


@node_router.post("/{job_id}/fail")
def fail_job(
    job_id: str,
    body: dict[str, Any] | None = None,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    job, _node = _owned_job(db, job_id, authorization)
    body = body or {}
    error = _validate_error(body.get("error", ""))
    _apply_checkpoint(job, body)
    retryable = body.get("retryable", True)
    if not isinstance(retryable, bool):
        _validation_error("retryable must be a boolean")
    job.error = error
    if retryable and (job.retry_count or 0) < (job.max_retries or 0):
        job.retry_count = (job.retry_count or 0) + 1
        job.status = "pending"
        will_retry = True
    else:
        job.status = "dead"
        will_retry = False
    job.claimed_by = None
    job.lease_until = None
    db.commit()
    db.refresh(job)
    return {"ok": True, "will_retry": will_retry, "job": _job_payload(job)}
