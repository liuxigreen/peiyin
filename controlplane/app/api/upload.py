"""上传签发：浏览器拿PUT签名直传R2，控制面不经手视频字节流。
D3修复：补Multipart分片协议——大文件(>500MB)分片上传，中断续传不重来。
流程：create-multipart→并发PUT parts(带partNumbers)→complete(合并)。
小文件仍走单PUT presign。"""
from fastapi import APIRouter
router = APIRouter(prefix="/api/upload", tags=["upload"])


@router.post("/presign")
async def presign(body: dict):
    from ..core.r2 import presign_put
    pid, name = body["project_id"], body.get("filename", "source.mp4")
    key = f"uploads/{pid}/{name}"
    return {"key": key, "url": presign_put(key), "method": "PUT",
            "mode": "single"}


@router.post("/create-multipart")
async def create_multipart(body: dict):
    """创建分片上传会话。body: {project_id, filename, part_count?}
    返回 upload_id + 每个分片的预签名PUT URL（浏览器并发传，失败单片重试）。"""
    from ..core.r2 import r2_client, BUCKET
    import os
    pid, name = body["project_id"], body.get("filename", "source.mp4")
    key = f"uploads/{pid}/{name}"
    part_mb = int(body.get("part_mb", 64))          # 64MB/片
    if not os.getenv("R2_ACCESS_KEY"):
        # mock模式：本地盘——返回单PUT语义（本地不挂）
        return {"key": key, "mode": "single-fallback",
                "url": f"/local-upload/{key}", "parts": []}
    client = r2_client()
    mp = client.create_multipart_upload(Bucket=BUCKET, Key=key)
    upload_id = mp["UploadId"]
    # 分片数按文件大小算（前端传file_size），无则默认32片上限
    file_size = int(body.get("file_size", 0)) or (32 * part_mb * 1024 * 1024)
    part_size = part_mb * 1024 * 1024
    n_parts = min(1000, max(1, -(-file_size // part_size)))
    from botocore.client import Config
    parts = []
    for i in range(1, n_parts + 1):
        url = client.generate_presigned_url(
            "upload_part",
            Params={"Bucket": BUCKET, "Key": key, "UploadId": upload_id,
                    "PartNumber": i},
            ExpiresIn=86400)
        parts.append({"part_number": i, "url": url})
    return {"key": key, "upload_id": upload_id, "mode": "multipart",
            "part_size": part_size, "parts": parts}


@router.post("/complete-multipart")
async def complete_multipart(body: dict):
    """合并分片。body: {key, upload_id, parts:[{part_number, etag}]}"""
    from ..core.r2 import r2_client, BUCKET
    import os
    key, upload_id = body["key"], body["upload_id"]
    if not os.getenv("R2_ACCESS_KEY"):
        return {"ok": True, "mode": "single-fallback", "key": key}
    client = r2_client()
    parts = [{"PartNumber": p["part_number"], "ETag": p["etag"]}
             for p in sorted(body["parts"], key=lambda x: x["part_number"])]
    client.complete_multipart_upload(
        Bucket=BUCKET, Key=key, UploadId=upload_id,
        MultipartUpload={"Parts": parts})
    return {"ok": True, "mode": "multipart", "key": key}


@router.post("/abort-multipart")
async def abort_multipart(body: dict):
    """放弃分片会话（清理已传分片，省存储费）。"""
    from ..core.r2 import r2_client, BUCKET
    import os
    if not os.getenv("R2_ACCESS_KEY"):
        return {"ok": True}
    client = r2_client()
    client.abort_multipart_upload(Bucket=BUCKET, Key=body["key"],
                                  UploadId=body["upload_id"])
    return {"ok": True}
