"""Non-destructively add persisted node release state."""
from __future__ import annotations

from sqlalchemy import inspect


_REQUIRED_COLUMNS = {
    "release_version", "release_digest", "release_ready", "release_draining", "release_reported_at",
}
_REQUIRED_INDEXES = {"idx_gpu_nodes_release_state"}


def migrate(engine) -> None:
    """Add nullable/defaulted fields and an index; safe to execute repeatedly."""
    inspector = inspect(engine)
    if "gpu_nodes" not in inspector.get_table_names():
        raise ValueError("gpu_nodes table does not exist")
    columns = {column["name"] for column in inspector.get_columns("gpu_nodes")}
    dialect = engine.dialect.name
    timestamp = "TIMESTAMPTZ" if dialect == "postgresql" else "DATETIME"
    additions = {
        "release_version": "VARCHAR(32)",
        "release_digest": "VARCHAR(64)",
        "release_ready": "BOOLEAN NOT NULL DEFAULT FALSE",
        "release_draining": "BOOLEAN NOT NULL DEFAULT FALSE",
        "release_reported_at": timestamp,
    }
    for name, definition in additions.items():
        if name not in columns:
            with engine.begin() as connection:
                connection.exec_driver_sql(f"ALTER TABLE gpu_nodes ADD COLUMN {name} {definition}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_gpu_nodes_release_state "
            "ON gpu_nodes(release_ready, release_draining)"
        )
    inspector = inspect(engine)
    final_columns = {column["name"] for column in inspector.get_columns("gpu_nodes")}
    final_indexes = {index["name"] for index in inspector.get_indexes("gpu_nodes")}
    if not _REQUIRED_COLUMNS <= final_columns:
        raise RuntimeError("gpu node release state column migration failed")
    if not _REQUIRED_INDEXES <= final_indexes:
        raise RuntimeError("gpu node release state index migration failed")

