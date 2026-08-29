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
    """真实连通性测试：OpenAI兼容 /v1/models 探测；DeepL查usage。"""
    from ..core.crypto import decrypt_key
    p = db.get(m.TranslationProvider, provid)
    try:
        key = decrypt_key(p.api_key_encrypted) if p.api_key_encrypted else ""
        base = (p.api_base_url or "").rstrip("/")
        if p.provider_type == "deepl":
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"https://api-free.deepl.com/v2/usage",
                                headers={"Authorization": f"DeepL-Auth-Key {key}"})
        else:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{base}/models",
                                headers={"Authorization": f"Bearer {key}"})
        return {"ok": r.status_code == 200, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
