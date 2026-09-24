# 单机控制面部署

这套 Compose 启动网站入口和控制面，数据放在 `deploy/data/`。服务器无需 GPU；GPU 节点另行接入。网站入口只监听本机 `127.0.0.1:8080`，可由 Cloudflare Tunnel 或其他 HTTPS 反向代理发布。控制面 API 只监听本机 `127.0.0.1:8500`；给 GPU 节点使用时须通过 Tailscale 等私有网络转发，不要把 8500 端口开放到公网。

1. 安装 Docker Engine 与 Compose，复制仓库，进入 `deploy/`。
2. 复制 `env.example` 为 `.env`；生成各不相同的随机 `ENCRYPTION_KEY` 和 `NODE_SHARED_SECRET`，设置 `DUBBING_BASIC_PASSWORD_HASH`。不要提交 `.env`。在国内网络可设置示例中的构建镜像源。
3. 运行 `docker compose config --quiet && docker compose up -d --build`。
4. 检查 `docker compose ps`、`curl http://127.0.0.1:8500/health`，再通过 HTTPS 入口验证网站和 `/api/projects`。入口使用 Basic Auth，用户名为 `ops`。

`deploy/data/` 保存 SQLite 数据库与 Mode B 上传文件。备份时至少保留整个目录以及 `.env`；恢复时先停止容器，再还原目录和同一份密钥。不要把数据目录打进发布包。

Mode A 的母片直传和分片上传使用 Cloudflare R2。在 `.env` 设置 `R2_ACCOUNT_ID`、`R2_ACCESS_KEY`、`R2_SECRET_KEY`、`R2_BUCKET` 后重新创建控制面容器；浏览器直传还要求 R2 bucket 对网站域名配置允许 PUT/GET 的 CORS。密钥只放服务器 `.env`，不要放进前端或 Git。

新的空数据库接入时，节点的 `CONTROL_URL` 指向新控制面私网地址，`NODE_SHARED_SECRET` 与服务器一致。现有节点会先尝试复用保存的 token；当前控制面会为有效格式的未知 token 建立新节点行。若没有 token 或验证失败，节点会使用共享密钥重新注册。Windows 节点修改配置后重启常驻进程，并以新控制面心跳和能力列表为验收依据。后续租用 GPU 按同一方式接入。

当前网站的 Mode B 可上传字幕与音频并创建分离任务；完整生产还依赖真实 GPU 引擎及后续人工/自动流程。Mode A 上传依赖额外的对象存储配置。离线冒烟测试使用模拟 TTS，不能替代真人声线质量验收。
