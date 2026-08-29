"""上传签发：浏览器拿PUT签名直传R2，控制面不经手视频字节流"""
from fastapi import APIRouter
router = APIRouter(prefix="/api/upload", tags=["upload"])

@router.post("/presign")
async def presign(body: dict):
    from ..core.r2 import presign_put, object_key
    pid, name = body["project_id"], body.get("filename", "source.mp4")
    key = f"uploads/{pid}/{name}"
    return {"key": key, "url": presign_put(key), "method": "PUT"}
