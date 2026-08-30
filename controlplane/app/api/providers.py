"""翻译服务商 CRUD + 测连通（key加密落库）"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import httpx
from ..db.session import get_db
from ..db import models as m
from ..core.crypto import encrypt_key

router = APIRouter(prefix="/api/providers", tags=["providers"])

def _mask_view(p: m.TranslationProvider) -> dict:
    return {"id": p.id, "name": p.name, "type": p.provider_type,
            "base_url": p.api_base_url, "key_masked": p.api_key_masked,
            "model": p.model_name, "is_default": p.is_default,
            "enabled": p.is_enabled, "priority": p.priority}

@router.get("")
def list_providers(db: Session = Depends(get_db)):
    return [_mask_view(p) for p in db.query(m.TranslationProvider).all()]

@router.post("")
async def create_provider(body: dict, db: Session = Depends(get_db)):
    raw = body.pop("api_key", "")
    enc, masked = encrypt_key(raw) if raw else ("", None)
    p = m.TranslationProvider(**{k: v for k, v in body.items()
                                 if k in dir(m.TranslationProvider)},
                              api_key_encrypted=enc, api_key_masked=masked)
    db.add(p); db.commit()
    return _mask_view(p)

@router.put("/{provid}")
async def update_provider(provid: str, body: dict, db: Session = Depends(get_db)):
    p = db.get(m.TranslationProvider, provid)
    if not p: raise HTTPException(404)
    raw = body.pop("api_key", None)
    if raw:
        p.api_key_encrypted, p.api_key_masked = encrypt_key(raw)
    for k, v in body.items():
        if hasattr(p, k): setattr(p, k, v)
    db.commit()
    return _mask_view(p)

@router.delete("/{provid}")
def delete_provider(provid: str, db: Session = Depends(get_db)):
    db.delete(db.get(m.TranslationProvider, provid)); db.commit()
    return {"ok": True}

@router.post("/{provid}/set-default")
def set_default(provid: str, db: Session = Depends(get_db)):
    db.query(m.TranslationProvider).update({"is_default": False})
    p = db.get(m.TranslationProvider, provid); p.is_default = True
    db.commit(); return {"ok": True}

@router.post("/{provid}/test")
async def test_provider(provid: str, db: Session = Depends(get_db)):
    """真实连通性测试：发一条最小chat请求（验证key+模型名+端点三者）。
    MiniMax等平台无/models路由，chat探测是唯一可靠的验证方式。"""
    import time
    from ..core.crypto import decrypt_key
    p = db.get(m.TranslationProvider, provid)
    if not p:
        raise HTTPException(404, "provider not found")
    key = decrypt_key(p.api_key_encrypted) if p.api_key_encrypted else ""
    base = (p.api_base_url or "").rstrip("/")
    t0 = time.time()
    try:
        if p.provider_type == "deepl":
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"https://api-free.deepl.com/v2/usage",
                                headers={"Authorization": f"DeepL-Auth-Key {key}"})
            ok = r.status_code == 200
            return {"ok": ok, "status": r.status_code,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": None if ok else f"HTTP {r.status_code}"}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {key}",
                                      "Content-Type": "application/json"},
                             # max_tokens=256：思考模型(M3/o1类)会把小预算全吃进reasoning
                             json={"model": p.model_name or "gpt-3.5-turbo",
                                   "messages": [{"role": "user",
                                                 "content": "Reply with exactly: PONG"}],
                                   "max_tokens": 256, "temperature": 0})
        latency = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            try:
                msg = r.json()["choices"][0]["message"]
                text = (msg.get("content") or "").strip()
            except Exception:                               # noqa: BLE001
                text = ""
            # 思考模型可能把回答放进reasoning_content——有reasoning=模型活着
            reasoning = ""
            try:
                reasoning = (r.json()["choices"][0]["message"]
                             .get("reasoning_content") or "")
            except Exception:                               # noqa: BLE001
                pass
            alive = bool(text) or bool(reasoning)
            return {"ok": alive, "status": 200, "latency_ms": latency,
                    "model": p.model_name,
                    "sample": (text or f"[思考模型] {reasoning[:40]}")[:60],
                    "error": None if alive else "200但无回复内容，检查模型名"}
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "") or str(r.json())[:150]
        except Exception:                                   # noqa: BLE001
            detail = r.text[:150]
        return {"ok": False, "status": r.status_code, "latency_ms": latency,
                "error": f"HTTP {r.status_code}: {detail}"}
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "status": 0,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": str(e)[:200]}
