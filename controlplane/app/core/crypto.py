"""API Key 加密存储：ENCRYPTION_KEY未配置时降级明文(dev前缀)，生产compose必配"""
import base64, hashlib, os

def encrypt_key(raw: str) -> tuple[str, str]:
    """返回 (encrypted, masked)"""
    key_env = os.getenv("ENCRYPTION_KEY", "")
    masked = raw[:4] + "****" + raw[-4:] if len(raw) > 8 else "****"
    if not key_env:
        return "plain:" + raw, masked
    from cryptography.fernet import Fernet
    dk = base64.urlsafe_b64encode(hashlib.sha256(key_env.encode()).digest())
    return Fernet(dk).encrypt(raw.encode()).decode(), masked

def decrypt_key(token: str) -> str:
    if token.startswith("plain:"):
        return token[6:]
    key_env = os.getenv("ENCRYPTION_KEY", "")
    assert key_env, "ENCRYPTION_KEY missing for encrypted provider key"
    from cryptography.fernet import Fernet
    dk = base64.urlsafe_b64encode(hashlib.sha256(key_env.encode()).digest())
    return Fernet(dk).decrypt(token.encode()).decode()
