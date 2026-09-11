"""Issue one short-lived, node-ID-bound legacy separation vocals backfill grant.

Run locally on ECS only, after ``migrate_legacy_backfill_grants.py``:
  .venv/bin/python scripts/grant_legacy_backfill.py \
      --task-id <completed-separation-task> --node-id <current-node-id> \
      --issuer <operator-identity> --ttl-minutes 15

The command prints only the grant UUID.  Give that UUID to the target node over
the operator's existing controlled channel; it is consumed together with that
node's bearer token by the artifact-backfill endpoint.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_FRESH_FOR = timedelta(minutes=2)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def issue_grant(db, *, task_id: str, node_id: str, issuer: str,
                ttl_minutes: int, now: datetime | None = None) -> str:
    """Validate the exact historical task/current node and persist an issued grant."""
    from app.db import models as m

    now = _as_utc(now) or datetime.now(timezone.utc)
    issuer = issuer.strip()
    if not issuer or len(issuer) > 200:
        raise ValueError("issuer must be 1-200 characters")
    if not 1 <= ttl_minutes <= 30:
        raise ValueError("ttl_minutes must be between 1 and 30")

    task = db.get(m.PipelineTask, task_id)
    if task is None or task.task_type != "separate-vocals" or task.status != "completed":
        raise ValueError("task must be an exact completed separate-vocals task")
    node = db.get(m.GpuNode, node_id)
    heartbeat = _as_utc(node.last_heartbeat) if node else None
    if node is None or not node.online or heartbeat is None or now - heartbeat > _FRESH_FOR:
        raise ValueError("node must be online with a heartbeat from the last two minutes")

    active = (db.query(m.LegacyArtifactBackfillGrant)
              .filter(m.LegacyArtifactBackfillGrant.source_task_id == task.id,
                      m.LegacyArtifactBackfillGrant.artifact_key == "vocals",
                      m.LegacyArtifactBackfillGrant.state.in_(("issued", "uploading")),
                      m.LegacyArtifactBackfillGrant.expires_at > now)
              .first())
    if active is not None:
        raise ValueError("an active vocals backfill grant already exists for this task")

    grant = m.LegacyArtifactBackfillGrant(
        source_task_id=task.id,
        node_id=node.id,
        artifact_key="vocals",
        state="issued",
        issuer=issuer,
        issued_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
        result="issued",
    )
    db.add(grant)
    db.commit()
    return grant.id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--ttl-minutes", required=True, type=int)
    args = parser.parse_args(argv)

    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        grant_id = issue_grant(db, task_id=args.task_id, node_id=args.node_id,
                               issuer=args.issuer, ttl_minutes=args.ttl_minutes)
    except ValueError as error:
        db.rollback()
        parser.error(str(error))
    finally:
        db.close()
    print(grant_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
