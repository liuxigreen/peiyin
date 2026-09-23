# 短剧出海译配平台

设计定稿见 DESIGN.md。云端控制面 + GPU节点自服务接入。

## 本地开发

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

## 生产部署（云端 VPS）

参见 [deploy/README.md](deploy/README.md)。当前部署为 SQLite + 本地持久目录、Caddy 身份验证和独立 HTTPS 入口；GPU 节点通过私有网络访问控制面。

## Clean checkout 验证与候选打包

以下命令只在新 clone 或临时目录运行；它们不会读取本机保留音频，也不会替换受跟踪的 `controlplane/web/dist`。
音频处理和完整测试需要系统级 `ffmpeg` **与** `ffprobe`（同一 FFmpeg 发行包通常同时提供）。安装前请确认两条命令均可从 `PATH` 调用；CI 会在每个平台显式安装该发行包。

```bash
python -m venv .venv
.venv/bin/python -m pip install -r controlplane/requirements-dev.txt
PYTHONPATH="$PWD/controlplane:$PWD/gpunode" .venv/bin/python -m pytest -p no:cacheprovider controlplane/tests gpunode/tests -q
NODE_MODE=offline PYTHONPATH="$PWD/controlplane:$PWD/gpunode" .venv/bin/python controlplane/scripts/offline_e2e.py
python scripts/release_smoke.py
cd frontend && bun install --frozen-lockfile && bun run build --outDir /tmp/peiyin-frontend-dist
```

`scripts/release_smoke.py --output-package /tmp/peiyin-candidate.tar` 可显式保留经校验的 Git archive；不传参数时只在临时位置验证。它会校验 release artifacts、published payload 的成员哈希、Python 源码和本地数据排除规则。

发布基线、artifact hash 与 ECS 漂移快照见 [`release-manifests/upgrade-seam-wave1.json`](release-manifests/upgrade-seam-wave1.json)。ECS 是已分叉的脏部署快照，必须保留且不得同步、覆盖或自动部署；任何部署由外部人工流程另行审批和执行。
