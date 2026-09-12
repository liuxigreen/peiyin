"""Non-destructively add exact-node routing storage to ``node_jobs``."""
from __future__ import annotations

from sqlalchemy import inspect


_REQUIRED_COLUMNS = {"target_node_id"}
_REQUIRED_INDEXES = {"idx_node_jobs_target_id_claim"}


def _has_target_fk(engine) -> bool:
    return any(
        fk.get("referred_table") == "gpu_nodes"
        and fk.get("constrained_columns") == ["target_node_id"]
        for fk in inspect(engine).get_foreign_keys("node_jobs")
    )


def migrate(engine) -> None:
    """Add a nullable column and lookup index; safely repeat on both dialects."""
    inspector = inspect(engine)
    if "node_jobs" not in inspector.get_table_names():
        raise ValueError("node_jobs table does not exist")
    columns = {column["name"] for column in inspector.get_columns("node_jobs")}
    dialect = engine.dialect.name
    if "target_node_id" not in columns:
        column_type = "UUID" if dialect == "postgresql" else "VARCHAR(36)"
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"ALTER TABLE node_jobs ADD COLUMN target_node_id {column_type}"
            )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_node_jobs_target_id_claim "
            "ON node_jobs(status, target_node_id, created_at)"
        )

    # New databases obtain this relation from SQLAlchemy metadata.  On an
    # existing PostgreSQL deployment it is safe to add when the referenced
    # table exists; SQLite retains the non-destructive column/index migration.
    inspector = inspect(engine)
    if (dialect == "postgresql" and "gpu_nodes" in inspector.get_table_names()
            and not _has_target_fk(engine)):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE node_jobs ADD CONSTRAINT fk_node_jobs_target_node "
                "FOREIGN KEY (target_node_id) REFERENCES gpu_nodes(id) ON DELETE RESTRICT"
            )

    inspector = inspect(engine)
    final_columns = {column["name"] for column in inspector.get_columns("node_jobs")}
    final_indexes = {index["name"] for index in inspector.get_indexes("node_jobs")}
    if not _REQUIRED_COLUMNS <= final_columns:
        raise RuntimeError("node_jobs target_node_id column migration failed")
    if not _REQUIRED_INDEXES <= final_indexes:
        raise RuntimeError("node_jobs target node index migration failed")


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.db.session import engine

    migrate(engine)
