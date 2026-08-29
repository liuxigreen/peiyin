"""Cloudflare R2 (S3兼容) 预签名URL —— 浏览器直传/直下 + GPU节点产物交换
V1用boto3签名；开发期无凭证时返回占位URL保流程可测。"""
import os, hmac, hashlib, time

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY", "")
BUCKET = os.getenv("R2_BUCKET", "dubbing")

def presign_put(key: str, expires: int = 3600) -> str:
    """浏览器直传母片 / GPU节点上传产物"""
    if not R2_ACCESS_KEY:
        return f"http://localhost:9000/{BUCKET}/{key}?mock=put&exp={int(time.time())+expires}"
    import boto3
    from botocore.client import Config
    client = boto3.client(
        "s3", endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY,
        config=Config(signature_version="s3v4"), region_name="auto")  # R2仅支持SigV4
    return client.generate_presigned_url("put_object",
        Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=expires)

def presign_get(key: str, expires: int = 86400) -> str:
    """成片下载 / 节点拉切片输入"""
    if not R2_ACCESS_KEY:
        return f"http://localhost:9000/{BUCKET}/{key}?mock=get"
    import boto3
    from botocore.client import Config
    client = boto3.client(
        "s3", endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY,
        config=Config(signature_version="s3v4"), region_name="auto")  # R2仅支持SigV4
    return client.generate_presigned_url("get_object",
        Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=expires)

def object_key(project_id: str, *parts) -> str:
    return f"projects/{project_id}/" + "/".join(parts)
