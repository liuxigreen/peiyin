"""任务队列核心（DESIGN.md §5）：Postgres SKIP LOCKED 原子领取 + lease回收
这是整个平台的调度命根子。B2步在真Postgres上验收：
  1. 两个并发claim不拿到同一条
  2. claim后lease_until写入
  3. reaper把超时running任务回收为pending
"""
import hashlib, json
from datetime import datetime, timedelta

CLAIM_SQL = """
UPDATE pipeline_tasks SET
    status = 'running',
    claimed_by = %(node_id)s,
    lease_until = NOW() + INTERVAL '10 minutes',
    heartbeat_at = NOW()
WHERE id = (
    SELECT id FROM pipeline_tasks
    WHERE status = 'pending'
      AND (%(model)s IS NULL OR model_name = %(model)s)
    ORDER BY priority DESC, created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
"""

REAP_SQL = """
UPDATE pipeline_tasks SET
    status = 'pending',
    claimed_by = NULL,
    lease_until = NULL,
    retry_count = retry_count + 1,
    error_message = COALESCE(error_message, '') || ' [lease expired, reclaimed]'
WHERE status = 'running' AND lease_until < NOW()
RETURNING id;
"""


def complete_ok(task: dict) -> bool:
    """输入未变 → 输出可复用（内容寻址缓存）"""
    expected = task_input_hash(task["task_type"], task.get("payload") or {})
    return task.get("input_hash") == expected


def task_input_hash(task_type: str, payload: dict) -> str:
    return hashlib.sha256(
        (task_type + "|" + json.dumps(payload, sort_keys=True, ensure_ascii=False)).encode()
    ).hexdigest()


async def claim_task(pool, node_id: str, model: str | None) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(CLAIM_SQL, {"node_id": node_id, "model": model})
        return dict(row) if row else None


async def reap_expired(pool) -> list[str]:
    """reaper线程每60s调用：超时running→重新入队"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(REAP_SQL)
        return [r["id"] for r in rows]
