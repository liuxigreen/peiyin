# 短剧出海译配平台

设计定稿见 DESIGN.md。云端控制面 + GPU节点自服务接入。

## 本地开发跑起来（Mac即可）

```bash
cd controlplane
uv run --with fastapi --with httpx --with pydantic-settings --with 'sqlalchemy>=2.0'   uvicorn app.main:app --port 8500
# 浏览器 http://localhost:8500  （前端已构建并托管在 controlplane/web/dist）

# 填充演示数据（可选）
uv run ... python scripts/seed_demo.py   # 同上依赖
```

## GPU节点接入（有N卡的机器）

```bash
cd gpunode && CONTROL_URL=http://<控制机>:8500 NODE_SHARED_SECRET=<secret> ./join.sh
```

## 生产部署（云端VPS）

```bash
cd deploy && cp env.example .env  # 填好密钥
docker compose up -d              # Caddy(443) + controlplane + postgres
```

## 前端改动后重新发布

```bash
cd frontend && bun install && bun run build
rm -rf ../controlplane/web/dist && cp -r dist ../controlplane/web/dist
```
