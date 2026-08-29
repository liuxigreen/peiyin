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
    # 开发态：未知token按hash独立建行（身份隔离——同一行被多token覆盖会让
    # claimed_by归属校验失效，评审D2的竞态防护依赖节点身份唯一）
    node = db.query(m.GpuNode).filter_by(token_hash=h).first()
    if not node:
        node = m.GpuNode(name=f"dev-node-{h[:8]}", token_hash=h, online=True)
        db.add(node); db.commit()
    elif not node.online:
        node.online = True; db.commit()
    return node

@router.post("/heartbeat")
def heartbeat(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    node = _auth_node(db, authorization)
    node.last_heartbeat = datetime.now(timezone.utc)
    node.online = True
    db.commit()
    return {"ok": True}


@router.post("/tasks/{task_id}/heartbeat")
def task_heartbeat(task_id: str, authorization: str = Header(default=""),
                   db: Session = Depends(get_db)):
    """D2修复：任务级心跳续租——长任务(ProPainter ~25min)每60s续10min lease，
    防reaper误杀。只续自己claim的任务（lease_until再延10min）。"""
    node = _auth_node(db, authorization)
    t = db.get(m.PipelineTask, task_id)
    if not t:
        raise HTTPException(404)
    if t.claimed_by != node.id:
        raise HTTPException(409, "task not claimed by this node")
    if t.status != "running":
        raise HTTPException(409, f"task is {t.status}, not running")
    t.lease_until = datetime.now(timezone.utc) + timedelta(minutes=10)
    t.heartbeat_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "lease_until": str(t.lease_until)}

# D5修复：claim只发READY任务（depends_on全部completed）——评审指出原SQL
# 直连节点可抢到未解锁任务，与ORCHESTRATION §3语义不符。
# JSON列跨方言不能join，用EXISTS子查询+JSON每行提取（千级任务量无压力）。
CLAIM_PG = """UPDATE pipeline_tasks SET status='running', claimed_by=:node_id,
    lease_until=NOW() + INTERVAL '10 minutes', heartbeat_at=NOW()
WHERE id = (SELECT t.id FROM pipeline_tasks t WHERE t.status='pending'
    AND NOT EXISTS (
      SELECT 1 FROM pipeline_tasks d, json_array_elements_text(
        COALESCE(t.depends_on, '[]'::json)) AS dep(key)
      JOIN pipeline_tasks up ON up.project_id = d.project_id
        AND up.task_key = dep.key
      WHERE d.id = t.id AND up.status <> 'completed')
    ORDER BY t.priority DESC, t.created_at ASC FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *"""

CLAIM_LITE = """UPDATE pipeline_tasks SET status='running', claimed_by=:nid,
    lease_until=datetime('now','+10 minutes'), heartbeat_at=datetime('now')
WHERE id = (SELECT t.id FROM pipeline_tasks t WHERE t.status='pending'
    AND NOT EXISTS (
      SELECT 1 FROM json_each(COALESCE(t.depends_on, json('[]'))) dep
      JOIN pipeline_tasks up ON up.project_id = t.project_id
        AND up.task_key = dep.value
      WHERE up.status <> 'completed')
    ORDER BY t.priority DESC, t.created_at ASC LIMIT 1)
RETURNING *"""

@router.get("/me/claim")
def claim(capabilities: str = "", model: str | None = None,
          authorization: str = Header(default=""), db: Session = Depends(get_db)):
    from ..db.session import engine
    node = _auth_node(db, authorization)   # 评审补充发现：claimed_by原硬编码"n"，
    is_pg = engine.url.get_backend_name().startswith("postgres")   # 节点身份从未绑定
    sql = CLAIM_PG if is_pg else CLAIM_LITE
    with engine.begin() as conn:
        row = conn.execute(satext(sql),
                           {"nid": node.id, "node_id": node.id}).mappings().first()
    if not row:
        return {"task": None}   # 204语义：空轮询long-poll在C1后启用
    task = dict(row)
    task.pop("lease_until", None); task.pop("created_at", None)
    return {"task": task}

@router.post("/tasks/{task_id}/complete")
def complete(task_id: str, body: dict, authorization: str = Header(default=""),
             db: Session = Depends(get_db)):
    node = _auth_node(db, authorization)
    t = db.get(m.PipelineTask, task_id)
    if not t: raise HTTPException(404)
    # D2修复：归属校验——任务已被reaper重派给别的节点时，旧节点的回报作废
    # （防双写竞态：lease过期重派后原节点跑完又标completed）
    if t.claimed_by and t.claimed_by != node.id:
        raise HTTPException(409, "task was reassigned; stale result discarded")
    t.status = "completed"
    t.output_paths = body.get("outputs", {})
    t.output_hash = body.get("output_hash")
    t.lease_until = None
    db.commit()
    from ..qc_agent import run_qc_hook
    qc = run_qc_hook(t, db)          # 节点完成路径同样过QC Agent
    return {"ok": True, "qc_pass": qc["pass"], "qc_action": qc["action"]}

@router.post("/tasks/{task_id}/fail")
def fail(task_id: str, body: dict, authorization: str = Header(default=""),
         db: Session = Depends(get_db)):
    node = _auth_node(db, authorization)
    t = db.get(m.PipelineTask, task_id)
    if not t: raise HTTPException(404)
    if t.claimed_by and t.claimed_by != node.id:
        raise HTTPException(409, "task was reassigned; stale fail discarded")
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
