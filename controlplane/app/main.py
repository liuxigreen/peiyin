"""控制面入口（v0.3）：DB建表 + 全部路由挂载 + Web面板静态托管"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .db.session import init_db
from .api import nodes, upload, projects, providers, assets, tasks, translate, agents, mode_b_api

app = FastAPI(title="Dubbing Platform Control Plane", version="0.3.0")

# 建表幂等且廉价：导入时执行一次，TestClient/uvicorn两种启动方式都成立
init_db()

# ── D4修复：API全局鉴权中间件 ──────────────────────────────
# API_TOKEN未配置=开发模式（本机/Mac联调跳过）；配置后所有 /api/* 必带
# Authorization: Bearer <token>。Caddy basic_auth之后的第二道闸。
import os as _os
from fastapi import Request as _Request
from fastapi.responses import JSONResponse as _JSONResponse

API_TOKEN = _os.getenv("API_TOKEN", "")


@app.middleware("http")
async def api_token_guard(request: _Request, call_next):
    if API_TOKEN and request.url.path.startswith("/api"):
        # 节点协议有自己的token体系(_auth_node)，不重复拦
        if request.url.path.startswith("/api/nodes"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {API_TOKEN}":
            return _JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.3.0"}


app.include_router(projects.router)
app.include_router(providers.router)
app.include_router(assets.router)
app.include_router(nodes.router)
app.include_router(upload.router)
app.include_router(tasks.router)
app.include_router(translate.router)
app.include_router(agents.router)
app.include_router(mode_b_api.router)

# Web面板构建产物静态托管（frontend/dist 拷贝到 controlplane/web/dist）
# 挂在最后，且只匹配非 /api /docs 路径 —— SPA fallback用自定义异常处理
_dist = Path(__file__).parent.parent / "web" / "dist"


@app.exception_handler(404)
async def spa_fallback(request, exc):
    """非API路径的404回退到index.html（react-router history模式）；API路径保持JSON 404"""
    from fastapi.responses import HTMLResponse, JSONResponse
    path = request.url.path
    if path.startswith("/api") or path in ("/docs", "/openapi.json"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    index = _dist / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>译配平台</h1><p>前端未部署：bun run build 后拷贝 dist/</p>")


if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")


# 租约收割后台线程：节点掉线后过期running任务自动回队列（0904教训：39句卡死8小时）
from .orchestrator_reaper import start_background_reaper  # noqa: E402
from .db.session import SessionLocal  # noqa: E402

start_background_reaper(SessionLocal, interval_s=60)
