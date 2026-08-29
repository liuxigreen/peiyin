"""集中配置：全部环境变量注入，无默认密码"""
import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = os.getenv("DATABASE_URL", "postgresql://dubbing:dubbing@localhost:5432/dubbing")
    api_token: str = os.getenv("API_TOKEN", "dev-token")   # 节点/服务间Bearer
    node_shared_secret: str = os.getenv("NODE_SHARED_SECRET", "dev-node-secret")  # 节点注册
    r2_account_id: str = os.getenv("R2_ACCOUNT_ID", "")
    r2_bucket: str = os.getenv("R2_BUCKET", "dubbing")

settings = Settings()
