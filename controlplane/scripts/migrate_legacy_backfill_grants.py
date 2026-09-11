"""Create the one-time legacy vocals backfill grant table on an existing ECS DB.

Run after uploading the controlplane code and before restarting the service:
  .venv/bin/python scripts/migrate_legacy_backfill_grants.py

The statements are intentionally limited to this additive table and its indexes.
They are safe to repeat.  A pre-existing incompatible table is rejected rather
than silently altered.
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS legacy_artifact_backfill_grants (
    id {id_type} PRIMARY KEY NOT NULL,
    source_task_id {task_id_type} NOT NULL REFERENCES pipeline_tasks(id) ON DELETE RESTRICT,
    node_id {node_id_type} NOT NULL REFERENCES gpu_nodes(id) ON DELETE RESTRICT,
    artifact_key VARCHAR(20) NOT NULL DEFAULT 'vocals',
    state VARCHAR(20) NOT NULL DEFAULT 'issued',
    issuer VARCHAR(200) NOT NULL,
    issued_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMP WITH TIME ZONE,
    result VARCHAR(50),
    result_detail TEXT,
    consumed_by_node_id {node_id_type} REFERENCES gpu_nodes(id) ON DELETE RESTRICT,
    consumed_at TIMESTAMP WITH TIME ZONE,
    filename VARCHAR(120),
    byte_count INTEGER,
    CONSTRAINT ck_legacy_backfill_grant_vocals CHECK (artifact_key = 'vocals'),
    CONSTRAINT ck_legacy_backfill_grant_state CHECK (state IN ('issued', 'uploading', 'consumed')),
    CONSTRAINT ck_legacy_backfill_grant_expiry CHECK (expires_at > issued_at)
)
"""
_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_legacy_backfill_grant_consume "
    "ON legacy_artifact_backfill_grants(node_id, state, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_legacy_backfill_grant_source "
    "ON legacy_artifact_backfill_grants(source_task_id, artifact_key)",
)
_REQUIRED_COLUMNS = {
    "id", "source_task_id", "node_id", "artifact_key", "state", "issuer",
    "issued_at", "expires_at", "attempt_count", "last_attempt_at", "result",
    "result_detail", "consumed_by_node_id", "consumed_at", "filename", "byte_count",
}


def migrate(engine) -> None:
    """Apply the additive DDL and fail closed if the resulting table is incomplete."""
    inspector = inspect(engine)
    task_columns = {column["name"]: column for column in inspector.get_columns("pipeline_tasks")}
    node_columns = {column["name"]: column for column in inspector.get_columns("gpu_nodes")}
    if "id" not in task_columns or "id" not in node_columns:
        raise RuntimeError("legacy backfill grant migration requires pipeline_tasks.id and gpu_nodes.id")
    task_id_type = str(task_columns["id"]["type"])
    node_id_type = str(node_columns["id"]["type"])
    ddl = _CREATE_TABLE.format(id_type=task_id_type, task_id_type=task_id_type,
                               node_id_type=node_id_type)
    with engine.begin() as connection:
        connection.exec_driver_sql(ddl)
        for statement in _CREATE_INDEXES:
            connection.exec_driver_sql(statement)
    columns = {column["name"] for column in inspect(engine).get_columns(
        "legacy_artifact_backfill_grants")}
    missing = _REQUIRED_COLUMNS - columns
    if missing:
        raise RuntimeError("legacy backfill grant migration missing columns: "
                           + ", ".join(sorted(missing)))


def main() -> int:
    from app.db.session import engine

    migrate(engine)
    print("legacy artifact backfill grants migration complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
