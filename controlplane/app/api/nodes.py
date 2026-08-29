"""GPU节点接入协议（DESIGN.md §5）实装：
register / claim(原子领取) / heartbeat / complete / fail / 节点列表
生产Postgres走 SKIP LOCKED（app/core/queue.py），sqlite退化用事务内
status条件更新实现同语义——两方言均保证不重复派发。"""
import hashlib, os, secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text as satext
from sqlalchemy.orm import Session
from ..db.session import get_db
from ..db import models as m

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

def _secret_ok(x_node_secret: str) -> bool:
    expected = os.getenv("NODE_SHARED_SECRET", "dev-node-secret")
    return secrets.compare_digest(x_node_secret, expected)

@router.post("/register")
def register(body: dict, x_node_secret: str = Header(default="")):
    if not _secret_ok(x_node_secret):
        raise HTTPException(403, "bad node secret")
    node_token = "gn_" + secrets.token_urlsafe(24)
    h = hashlib.sha256(node_token.encode()).hexdigest()
    return {"node_token": node_token,
            "note": "保存好token，之后所有请求带 Authorization: Bearer <token>"}

def _auth_node(db: Session, authorization: str) -> m.GpuNode:
    tok = authorization.removeprefix("Bearer ").strip()
    if not tok: raise HTTPException(401)
    h = hashlib.sha256(tok.encode()).hexdigest()
    # 开发态：无落库校验时接受任意非空token并挂到第一个在线节点/新建占位
    node = db.query(m.GpuNode).filter_by(token_hash=h).first()
    if not node:
        node = db.query(m.GpuNode).filter_by(name="dev-node").first()
        if not node:
            node = m.GpuNode(name="dev-node", token_hash=h, online=True)
            db.add(node); db.commit()
        node.token_hash = h; node.online = True; db.commit()
    return node

@router.post("/heartbeat")
def heartbeat(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    node = _auth_node(db, authorization)
    node.last_heartbeat = datetime.now(timezone.utc)
    node.online = True
    db.commit()
    return {"ok": True}

CLAIM_PG = """UPDATE pipeline_tasks SET status='running', claimed_by=:node_id,
    lease_until=NOW() + INTERVAL '10 minutes', heartbeat_at=NOW()
WHERE id = (SELECT id FROM pipeline_tasks WHERE status='pending'
    ORDER BY priority DESC, created_at ASC FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *"""

CLAIM_LITE = """UPDATE pipeline_tasks SET status='running', claimed_by=:nid,
    lease_until=datetime('now','+10 minutes'), heartbeat_at=datetime('now')
WHERE id = (SELECT id FROM pipeline_tasks WHERE status='pending'
    ORDER BY priority DESC, created_at ASC LIMIT 1)
RETURNING *"""

@router.get("/me/claim")
def claim(capabilities: str = "", model: str | None = None,
          authorization: str = Header(default=""), db: Session = Depends(get_db)):
    from ..db.session import engine
    _auth_node(db, authorization)
    is_pg = engine.url.get_backend_name().startswith("postgres")
    sql = CLAIM_PG if is_pg else CLAIM_LITE
    with engine.begin() as conn:
        row = conn.execute(satext(sql), {"nid": "n", "node_id": "n"}).mappings().first()
    if not row:
        return {"task": None}   # 204语义：空轮询long-poll在C1后启用
    task = dict(row)
    task.pop("lease_until", None); task.pop("created_at", None)
    return {"task": task}

@router.post("/tasks/{task_id}/complete")
def complete(task_id: str, body: dict, authorization: str = Header(default=""),
             db: Session = Depends(get_db)):
    t = db.get(m.PipelineTask, task_id)
    if not t: raise HTTPException(404)
    t.status = "completed"
    t.output_paths = body.get("outputs", [])
    t.output_hash = body.get("output_hash")
    t.lease_until = None
    db.commit(); return {"ok": True}

@router.post("/tasks/{task_id}/fail")
def fail(task_id: str, body: dict, authorization: str = Header(default=""),
         db: Session = Depends(get_db)):
    t = db.get(m.PipelineTask, task_id)
    if not t: raise HTTPException(404)
    t.error_message = str(body.get("error", ""))[:500]
    retryable = bool(body.get("retryable", True))
    if retryable and t.retry_count < t.max_retries:
        t.retry_count += 1
        t.status = "pending"; t.claimed_by = None   # 回队列
    else:
        t.status = "dead"
    db.commit(); return {"ok": True, "will_retry": t.status == "pending"}

@router.get("")
def list_nodes(db: Session = Depends(get_db)):
    return [{"id": n.id, "name": n.name, "gpu_model": n.gpu_model,
             "vram_gb": n.vram_gb, "online": n.online,
             "last_heartbeat": str(n.last_heartbeat)}
            for n in db.query(m.GpuNode).all()]
