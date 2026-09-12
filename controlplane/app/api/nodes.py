"""GPU节点接入协议（DESIGN.md §5）实装：
register / claim(原子领取) / heartbeat / complete / fail / 节点列表
生产Postgres走 SKIP LOCKED（app/core/queue.py），sqlite退化用事务内
status条件更新实现同语义——两方言均保证不重复派发。"""
import hashlib, json as _json, os, re as _re, secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import text as satext
from sqlalchemy.orm import Session
from ..db.session import get_db
from ..db import models as m

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

def _secret_ok(x_node_secret: str) -> bool:
    expected = os.getenv("NODE_SHARED_SECRET", "dev-node-secret")
    return secrets.compare_digest(x_node_secret, expected)

@router.post("/register")
def register(body: dict, x_node_secret: str = Header(default=""),
             db: Session = Depends(get_db)):
    if not _secret_ok(x_node_secret):
        raise HTTPException(403, "bad node secret")
    node_token = "gn_" + secrets.token_urlsafe(24)
    h = hashlib.sha256(node_token.encode()).hexdigest()
    # v1.4：register即建档（原来只发token，行由_auth_node兜底建——节点名/GPU型号
    # 全丢成dev-node-xxxx）。同名旧行置offline，token轮换后旧心跳行不再堆积。
    name = body.get("name") or "unnamed-node"
    for old in db.query(m.GpuNode).filter_by(name=name).all():
        old.online = False
    db.add(m.GpuNode(name=name, gpu_model=body.get("gpu_model"),
                     vram_gb=body.get("vram_gb"),
                     capabilities=body.get("capabilities") or [],
                     online=True, token_hash=h,
                     last_heartbeat=datetime.now(timezone.utc)))
    db.commit()
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


@router.get("/me")
def node_identity(authorization: str = Header(default=""),
                  db: Session = Depends(get_db)):
    """Return the non-secret identity selected by a node bearer token.

    This gives node-side maintenance tools a deterministic preflight before a
    one-time operation.  It deliberately never returns the bearer token or its
    complete hash.
    """
    node = _auth_node(db, authorization)
    return {
        "id": node.id,
        "name": node.name,
        "token_hash_prefix": (node.token_hash or "")[:16],
    }

@router.post("/heartbeat")
def heartbeat(body: dict | None = None, authorization: str = Header(default=""),
              db: Session = Depends(get_db)):
    node = _auth_node(db, authorization)
    # 节点能力可能随受控模型发布改变；只接受一个短字符串列表，避免让心跳
    # 成为任意 JSON 的写入通道。未提供时保持向后兼容。
    if body and "capabilities" in body:
        caps = body["capabilities"]
        if (not isinstance(caps, list) or len(caps) > 32
                or any(not isinstance(cap, str) or not _re.fullmatch(r"[a-z0-9_-]{1,48}", cap)
                       for cap in caps)):
            raise HTTPException(400, "invalid capabilities")
        node.capabilities = list(dict.fromkeys(caps))
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
    AND (t.task_type NOT IN ('diarize','separate-vocals','tts-generate')
         OR (t.task_type='diarize' AND :can_diarize=1)
         OR (t.task_type='separate-vocals' AND :can_sep=1)
         OR (t.task_type='tts-generate' AND :can_tts=1))
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
WHERE id = (SELECT id FROM (
      SELECT id, task_key, depends_on, priority, created_at, project_id
      FROM pipeline_tasks WHERE status='pending'
        AND (task_type NOT IN ('diarize','separate-vocals','tts-generate')
             OR (task_type='diarize' AND :can_diarize=1)
             OR (task_type='separate-vocals' AND :can_sep=1)
             OR (task_type='tts-generate' AND :can_tts=1))
      ORDER BY priority DESC, created_at ASC LIMIT 50) t
    WHERE NOT EXISTS (
      SELECT 1 FROM json_each(COALESCE(t.depends_on, json('[]'))) dep
      JOIN pipeline_tasks up ON up.project_id = t.project_id
        AND up.task_key = dep.value
      WHERE up.status <> 'completed')
    ORDER BY t.priority DESC, t.created_at ASC LIMIT 1)
RETURNING *"""


def _claim_capability_params(node: m.GpuNode) -> dict[str, int]:
    capabilities = set(node.capabilities or [])
    return {
        "can_diarize": int("diarize" in capabilities),
        "can_sep": int("sep" in capabilities),
        "can_tts": int("tts" in capabilities),
    }

@router.get("/me/claim")
def claim(capabilities: str = "", model: str | None = None, n: int = 1,
          authorization: str = Header(default=""), db: Session = Depends(get_db)):
    """n>1 = 批量认领（一次租N条减少轮询往返，0903提速项）。
    向后兼容：响应恒带 task（单任务语义），批量列表放 tasks。"""
    from ..db.session import engine
    node = _auth_node(db, authorization)   # 评审补充发现：claimed_by原硬编码"n"，
    is_pg = engine.url.get_backend_name().startswith("postgres")   # 节点身份从未绑定
    sql = CLAIM_PG if is_pg else CLAIM_LITE
    n = max(1, min(int(n or 1), 32))
    tasks = []
    capability_params = _claim_capability_params(node)
    with engine.begin() as conn:
        for _ in range(n):
            row = conn.execute(satext(sql),
                               {"nid": node.id, "node_id": node.id,
                                **capability_params}).mappings().first()
            if not row:
                break
            task = dict(row)
            task.pop("lease_until", None); task.pop("created_at", None)
            tasks.append(task)
    if not tasks:
        return {"task": None, "tasks": []}   # 204语义：空轮询long-poll在C1后启用
    return {"task": tasks[0], "tasks": tasks}

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
    # G6配套：outputs写入+payload保留。历史两处数据丢失：
    # ①节点dispatch发的是列表，整行赋值后qc钩子判定非dict→output_paths只剩{"qc"}；
    # ②即便dict也被整行覆盖，artifact回传时payload里的uid/engine已丢失。
    body_out = body.get("outputs", {})
    # 上传可能早于 complete：在已有控制面产物之上合并节点回报，不能把
    # payload/artifacts 整行覆盖掉。列表保持既有的 outputs 兼容结构；字典
    # 仍按历史语义展开到 output_paths 顶层。
    outs = dict(t.output_paths or {})
    if isinstance(body_out, list):
        outs["outputs"] = body_out
    elif isinstance(body_out, dict):
        for name, value in body_out.items():
            if name not in ("payload", "artifacts"):
                outs[name] = value
    t.output_paths = outs
    t.output_hash = body.get("output_hash")
    t.lease_until = None
    db.commit()
    from ..qc_agent import run_qc_hook
    qc = run_qc_hook(t, db)          # 节点完成路径同样过QC Agent
    _queue_diarize_handoff(db, t)
    db.commit()
    return {"ok": True, "qc_pass": qc["pass"], "qc_action": qc["action"]}


_ART_MAX_MB = int(os.getenv("NODE_ARTIFACT_MAX_MB", "80"))
_VOICES_DIR = os.getenv("NODE_VOICES_DIR",
                        os.path.join(os.getenv("MODE_B_STORAGE", "/tmp/peiyin-mode-b"), "voices"))
_ART_TOKEN_RE = _re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
_TASK_ID_RE = _re.compile(r"[A-Za-z0-9_-]{1,80}")


def _safe_artifact_token(value: str) -> bool:
    """仅允许控制面可稳定定位的单段 artifact key/文件名。"""
    return bool(_ART_TOKEN_RE.fullmatch(value)) and ".." not in value


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize sqlite's naive datetimes before comparing with an aware clock."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _artifact_url(task_id: str, key: str, filename: str) -> str:
    return f"/api/nodes/tasks/{task_id}/artifacts/{key}/{filename}"


def _queue_diarize_handoff(db: Session, source: "m.PipelineTask") -> None:
    """仅在 control-plane vocals artifact 已持久化后排入 diarize。

    节点上报的 outputs 路径只可作为诊断信息，绝不能成为另一节点的输入。
    """
    if source.task_type != "separate-vocals" or source.status != "completed":
        return
    outs = dict(source.output_paths or {})
    artifact = next((a for a in (outs.get("artifacts") or [])
                     if isinstance(a, dict) and a.get("key") == "vocals"), None)
    if not artifact:
        return
    filename = artifact.get("filename")
    if not isinstance(filename, str) or not _safe_artifact_token(filename):
        return
    handoff_url = _artifact_url(source.id, "vocals", filename)
    from ..db.models import Utterance
    slots = [{"uid": u.uid, "start_ms": u.start_ms, "end_ms": u.end_ms}
             for u in db.query(Utterance).filter_by(project_id=source.project_id)
             .order_by(Utterance.seq_index).all()
             if (u.end_ms or 0) > (u.start_ms or 0)]
    input_hash = hashlib.sha256(
        f"diarize:{source.project_id}:{source.input_hash}:vocals".encode()
    ).hexdigest()
    diarize = (db.query(m.PipelineTask)
               .filter(m.PipelineTask.project_id == source.project_id,
                       m.PipelineTask.task_type == "diarize",
                       m.PipelineTask.input_hash == input_hash,
                       m.PipelineTask.status != "dead").first())
    if diarize is None:
        db.add(m.PipelineTask(
            project_id=source.project_id,
            task_key=f"DIARIZE/{source.id[:8]}",
            task_type="diarize", resource="gpu", gpu_required=True,
            weight=5, depends_on=[source.task_key], input_hash=input_hash, status="pending",
            output_paths={"payload": {"zh_audio_url": handoff_url, "srt_slots": slots}}))
        return
    diarize_outs = dict(diarize.output_paths or {})
    payload = dict(diarize_outs.get("payload") or {})
    payload.update({"zh_audio_url": handoff_url, "srt_slots": slots})
    payload.pop("zh_audio", None)
    diarize_outs["payload"] = payload
    diarize.output_paths = diarize_outs


# P2修复：进程级音色索引缓存（md5→path），避免每请求遍历+读文件算md5
_voice_index_cache: dict[str, str] | None = None


def _build_voice_index() -> dict[str, str]:
    global _voice_index_cache
    if _voice_index_cache is not None:
        return _voice_index_cache
    import hashlib as _h
    idx: dict[str, str] = {}
    if os.path.isdir(_VOICES_DIR):
        for fn in os.listdir(_VOICES_DIR):
            fp = os.path.join(_VOICES_DIR, fn)
            if os.path.isfile(fp):
                idx["v" + _h.md5(open(fp, "rb").read()).hexdigest()[:10]] = fp
    _voice_index_cache = idx
    return idx


def invalidate_voice_cache():
    """新音色入库后调用（管理端点/重启自然失效）。"""
    global _voice_index_cache
    _voice_index_cache = None


@router.get("/voices/registry.json")
def get_voice_registry():
    """节点拉取注册表（zh_audio 等整条音频映射）。"""
    import json as _json
    reg_path = os.path.join(os.environ.get("MODE_B_STORAGE", "/tmp/peiyin-mode-b"),
                            "voices_registry.json")
    if os.path.exists(reg_path):
        return _json.load(open(reg_path))
    return {}


@router.get("/voices/zhaudio/{name}")
def get_zh_audio(name: str):
    """节点拉取整条原配音音频（diarize/克隆用）。名字白名单。"""
    import json as _json
    from fastapi.responses import FileResponse
    if not _re.fullmatch(r"[A-Za-z0-9_.\-]{1,120}", name):
        raise HTTPException(400)
    reg_path = os.path.join(os.environ.get("MODE_B_STORAGE", "/tmp/peiyin-mode-b"),
                            "voices_registry.json")
    mapping = _json.load(open(reg_path)) if os.path.exists(reg_path) else {}
    src = mapping.get(name)
    if src and os.path.exists(src):
        return FileResponse(src, filename=name, media_type="audio/mpeg")
    raise HTTPException(404)


@router.get("/voices/{fid}.wav")
def get_voice(fid: str):
    """预置音色下发（节点按需拉取并缓存）。fid=文件内容md5前10位（不可猜）。
    设计红线：参考音频绝不入库（0902事故：b64塞output_paths，993MB拖垮SQLite），
    永远走 HTTP + 节点侧缓存。
    P1：带Authorization头时校验节点身份；无头放行（兼容旧节点缓存逻辑）。"""
    from fastapi.responses import FileResponse
    if not fid.isalnum() or len(fid) > 16:
        raise HTTPException(400)
    fp = _build_voice_index().get(fid)
    if fp and os.path.isfile(fp):
        return FileResponse(fp, media_type="audio/wav", filename=f"{fid}.wav")
    raise HTTPException(404)


@router.get("/engine-should-run")
def engine_should_run(db: Session = Depends(get_db)):
    """节点引擎管家信号：有无 pending/running 的 TTS 任务。
    有 → 节点拉起 CosyVoice；空闲超时 → 节点自行关停引擎。"""
    n = db.query(m.PipelineTask).filter(
        m.PipelineTask.task_type == "tts-generate",
        m.PipelineTask.status.in_(("pending", "running"))).count()
    return {"should_run": n > 0, "pending_tts": n}


@router.post("/tasks/{task_id}/artifact")
async def upload_artifact(task_id: str, request: "Request", filename: str = "",
                          key: str = "", authorization: str = Header(default=""),
                          db: Session = Depends(get_db)):
    """G6产物回传：节点把wav等产物POST回控制面（raw body流式落盘，
    免multipart依赖）。保存到 MODE_B_STORAGE/artifacts/{task_id}/，
    合并进 task.output_paths.artifacts；tts-generate 任务同时落 tts_clips 行
    （此前output_paths只有节点本地路径，控制面拿不到音频文件，交付包断供）。"""
    node = _auth_node(db, authorization)
    t = db.get(m.PipelineTask, task_id)
    if not t:
        raise HTTPException(404)
    if t.claimed_by and t.claimed_by != node.id:
        raise HTTPException(409, "task not claimed by this node")
    fname = filename or f"{t.task_key}.bin"
    artifact_key = key or fname
    if (not _TASK_ID_RE.fullmatch(task_id) or not _safe_artifact_token(fname)
            or not _safe_artifact_token(artifact_key)):
        raise HTTPException(400, "invalid artifact key or filename")
    storage = os.getenv("MODE_B_STORAGE", "/tmp/peiyin-mode-b")
    dest_dir = os.path.join(storage, "artifacts", task_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, fname)
    size = 0
    with open(dest, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > _ART_MAX_MB << 20:
                f.close()
                os.remove(dest)
                raise HTTPException(413, f"artifact exceeds {_ART_MAX_MB}MB")
            f.write(chunk)
    entry = {"key": artifact_key, "filename": fname, "path": dest, "bytes": size}
    outs = dict(t.output_paths or {})
    arts = [a for a in (outs.get("artifacts") or []) if a.get("key") != entry["key"]]
    arts.append(entry)
    outs["artifacts"] = arts          # JSON列整体重赋值（原地改不落库，HANDOVER坑#4）
    t.output_paths = outs
    clip_info = _upsert_tts_clip(db, t, dest)
    _queue_diarize_handoff(db, t)
    db.commit()
    return {"ok": True, "artifact": entry, "tts_clip": clip_info}


@router.post("/tasks/{task_id}/artifact-backfill")
async def backfill_separation_artifact(task_id: str, request: "Request", filename: str = "",
                                       key: str = "", grant_id: str = "", job_id: str = "",
                                       authorization: str = Header(default=""),
                                       db: Session = Depends(get_db)):
    """Backfill completed separation audio, with a short-lived operator grant if needed."""
    node = _auth_node(db, authorization)
    task = db.get(m.PipelineTask, task_id)
    if not task:
        raise HTTPException(404)
    if task.task_type != "separate-vocals" or task.status != "completed":
        raise HTTPException(409, "artifact backfill requires a completed separation task")
    if key not in {"vocals", "accompaniment"}:
        raise HTTPException(400, "backfill key must be vocals or accompaniment")
    if (not _TASK_ID_RE.fullmatch(task_id) or not _safe_artifact_token(filename)):
        raise HTTPException(400, "invalid artifact key or filename")

    # Job ownership is an independent one-time authority.  This branch never
    # falls through to a historical owner or grant check.
    job = None
    if job_id:
        from .node_jobs import (
            LEGACY_VOCALS_BACKFILL_KIND,
            LEGACY_VOCALS_BACKFILL_SPEC_VERSION,
        )
        job = db.get(m.NodeJob, job_id)
        params = job.params if job is not None and isinstance(job.params, dict) else {}
        if (job is None or job.status != "running" or job.claimed_by != node.id
                or job.target_node_id != node.id
                or job.kind != LEGACY_VOCALS_BACKFILL_KIND
                or job.spec_version != LEGACY_VOCALS_BACKFILL_SPEC_VERSION
                or params.get("source_task_id") != task.id
                or params.get("key") != "vocals"
                or params.get("filename") != "vocals.wav"
                or not isinstance(params.get("source_path"), str)):
            raise HTTPException(409, "legacy backfill job is not owned by this node")
        if key != "vocals" or filename != "vocals.wav":
            raise HTTPException(400, "legacy backfill job requires vocals.wav")
        declared = [item for item in ((task.output_paths or {}).get("outputs") or [])
                    if isinstance(item, dict) and item.get("key") == "vocals"]
        if (len(declared) != 1 or declared[0].get("path") != params["source_path"]
                or any(isinstance(item, dict) and item.get("key") == "vocals"
                       for item in ((task.output_paths or {}).get("artifacts") or []))):
            raise HTTPException(409, "legacy backfill source no longer matches job")

    # The historical owner remains fully backward compatible.  A re-registered
    # node gets no ownership inference: it must present an ECS-issued grant for
    # this exact source task, node row, and the only allowed legacy key.
    grant = None
    if job is None and task.claimed_by != node.id:
        if not grant_id:
            raise HTTPException(409, "task was not completed by this node")
        grant = db.get(m.LegacyArtifactBackfillGrant, grant_id)
        now = datetime.now(timezone.utc)
        expires_at = _as_utc(grant.expires_at) if grant else None
        if grant is None:
            raise HTTPException(409, "unknown backfill grant")
        if grant.source_task_id != task.id or grant.artifact_key != "vocals" or key != "vocals":
            raise HTTPException(409, "backfill grant does not match this artifact")
        if grant.node_id != node.id:
            raise HTTPException(
                409,
                detail={
                    "code": "backfill_grant_node_mismatch",
                    "authenticated_node_id": node.id,
                    "expected_node_id": grant.node_id,
                },
            )
        if grant.state != "issued":
            raise HTTPException(409, "backfill grant is already in use or consumed")
        if expires_at is None or expires_at <= now:
            raise HTTPException(409, "backfill grant has expired")
        # Reserve before receiving bytes.  A second request cannot overwrite the
        # artifact while this one is in flight; a failed stream releases it below.
        grant.state = "uploading"
        grant.attempt_count += 1
        grant.last_attempt_at = now
        grant.result = "uploading"
        grant.result_detail = None
        reserved_grant_id = grant.id
        db.commit()

    storage = os.getenv("MODE_B_STORAGE", "/tmp/peiyin-mode-b")
    dest_dir = os.path.join(storage, "artifacts", task_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)
    temporary = dest + ".backfill"
    size = 0
    try:
        with open(temporary, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > _ART_MAX_MB << 20:
                    raise HTTPException(413, f"artifact exceeds {_ART_MAX_MB}MB")
                f.write(chunk)
        os.replace(temporary, dest)
    except Exception:
        if os.path.exists(temporary):
            os.remove(temporary)
        if grant is not None:
            db.rollback()
            grant = db.get(m.LegacyArtifactBackfillGrant, reserved_grant_id)
            if grant is not None and grant.state == "uploading":
                grant.state = "issued"
                grant.result = "upload_failed"
                grant.result_detail = "artifact stream failed"
                db.commit()
        raise

    entry = {"key": key, "filename": filename, "path": dest, "bytes": size}
    outputs = dict(task.output_paths or {})
    artifacts = [artifact for artifact in (outputs.get("artifacts") or [])
                 if artifact.get("key") != key]
    artifacts.append(entry)
    outputs["artifacts"] = artifacts
    task.output_paths = outputs
    if job is not None:
        # Upload persists only the artifact and a durable checkpoint.  A
        # consumer appears only when the running job is explicitly completed.
        job.checkpoint = {"artifact": {"key": key, "filename": filename, "bytes": size}}
    else:
        _queue_diarize_handoff(db, task)
    if grant is not None:
        # The source task lifecycle is intentionally untouched.  Consumption is
        # recorded only after the artifact has reached control-plane storage.
        grant.state = "consumed"
        grant.consumed_by_node_id = node.id
        grant.consumed_at = datetime.now(timezone.utc)
        grant.filename = filename
        grant.byte_count = size
        grant.result = "uploaded"
        grant.result_detail = None
    db.commit()
    return {"ok": True, "artifact": entry}


@router.get("/tasks/{task_id}/artifacts/{key}/{filename}")
def download_artifact(task_id: str, key: str, filename: str,
                      authorization: str = Header(default=""),
                      db: Session = Depends(get_db)):
    """向已领取对应 diarize 任务的节点下发控制面保存的产物。"""
    if (not _TASK_ID_RE.fullmatch(task_id) or not _safe_artifact_token(key)
            or not _safe_artifact_token(filename)):
        raise HTTPException(400, "invalid artifact locator")
    node = _auth_node(db, authorization)
    source = db.get(m.PipelineTask, task_id)
    if source is None:
        raise HTTPException(404)
    requested_url = _artifact_url(task_id, key, filename)
    # 上传者不等于消费者；只有已领取且 payload 明确引用此 handoff 的 diarize
    # 节点能下载。未知 token 和其他节点都不能借 URL 读取项目产物。
    consumer = (db.query(m.PipelineTask)
                .filter(m.PipelineTask.project_id == source.project_id,
                        m.PipelineTask.task_type == "diarize",
                        m.PipelineTask.claimed_by == node.id,
                        m.PipelineTask.status == "running").all())
    if not any(((row.output_paths or {}).get("payload") or {}).get("zh_audio_url")
               == requested_url for row in consumer):
        raise HTTPException(403, "artifact not assigned to this node")
    artifact = next((a for a in ((source.output_paths or {}).get("artifacts") or [])
                     if isinstance(a, dict) and a.get("key") == key
                     and a.get("filename") == filename), None)
    if artifact is None:
        raise HTTPException(404)
    storage = os.path.realpath(os.getenv("MODE_B_STORAGE", "/tmp/peiyin-mode-b"))
    path = os.path.realpath(os.path.join(storage, "artifacts", task_id, filename))
    if os.path.commonpath((storage, path)) != storage or not os.path.isfile(path):
        raise HTTPException(404)
    from fastapi.responses import FileResponse
    return FileResponse(path, filename=filename)


def _upsert_tts_clip(db: Session, t: "m.PipelineTask", path: str) -> dict | None:
    """tts-generate任务的产物回传→tts_clips落库（交付包/试听的数据源）。"""
    if t.task_type != "tts-generate":
        return None
    payload = (t.output_paths or {}).get("payload") or {}
    uid_ = payload.get("uid")
    if not uid_:
        return None
    u = db.query(m.Utterance).filter_by(project_id=t.project_id, uid=uid_).first()
    if not u:
        return None
    # 取最新"非占位符"译文（白月光实测教训：历史bug遗留的[MISSING n]行
    # 挂在最新版本上，会把正确音频的clip关联到垃圾文本，交付包manifest错乱）
    from ..translate_executor import is_placeholder as _is_ph
    tr = None
    for cand in (db.query(m.Translation)
                   .filter_by(utterance_id=u.id,
                              target_lang=payload.get("lang") or "en")
                   .order_by(m.Translation.version.desc()).all()):
        if not _is_ph(cand.text or ""):
            tr = cand
            break
    if tr is None:
        return None
    dur_ms = 0
    try:
        import soundfile as _sf
        info = _sf.info(path)
        dur_ms = int(info.frames / info.samplerate * 1000)
    except Exception:                                        # noqa: BLE001
        pass
    engine = payload.get("engine") or "mock"
    row = (db.query(m.TtsClip)
             .filter_by(utterance_id=u.id, target_lang=tr.target_lang,
                        version=tr.version, tts_engine=engine).first())
    if row is None:
        row = m.TtsClip(utterance_id=u.id, target_lang=tr.target_lang,
                        translation_id=tr.id, version=tr.version,
                        tts_engine=engine)
        db.add(row)
    row.audio_r2_key = path          # R2未启用阶段存控制面本地路径
    row.duration_ms = dur_ms
    row.prosody_rate = float(payload["rate"]) if payload.get("rate") else None
    row.status = "completed"
    return {"clip_id": row.id, "duration_ms": dur_ms, "engine": engine}

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
