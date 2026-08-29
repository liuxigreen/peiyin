"""控制面入口（v0.3）：DB建表 + 全部路由挂载 + Web面板静态托管"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .db.session import init_db
from .api import nodes, upload, projects, providers, assets, tasks, translate, agents

app = FastAPI(title="Dubbing Platform Control Plane", version="0.3.0")

# 建表幂等且廉价：导入时执行一次，TestClient/uvicorn两种启动方式都成立
init_db()


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
