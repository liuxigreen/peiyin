# peiyin 译配系统 · 交接文档（HANDOVER）

> 2026-08-30。交接对象：autoclaw（后续接手配音系统开发/运维的 AI Agent）。
> 读这份文档 + 仓库内 7 份设计文档，即可接手全部工作。
> 核心纪律：**实测与本档冲突 → 改档记日期，文档=现实。动手前先读文档，别凭空猜。**

---

## 一、系统是什么（30 秒版）

短剧出海译配平台：中文短剧 → 外语配音成片。
两种模式（用户拍板，同等质量）：
- **模式 A（完整）**：上传视频走全流程 → 输出配音成片视频
- **模式 B（无视频）**：上传中文字幕 SRT + 中文配音音频 → 五步链翻译 → 输出交付包（外语字幕 SRT/ASS + 分句外语配音音频 + manifest + QC 报告），员工本地合成

**当前状态**：模式 B 已上线可用（M3 真实翻译 2803 句验证通过）；模式 A 的 GPU 环节（ASR/分离/TTS/擦字幕 quality）全部未实装——这是接手后的主线工作。

---

## 二、关键位置（全部实测过）

### 本地（用户 Mac M4，开发环境）
| 项 | 位置 |
|----|------|
| 代码仓库 | `~/duanju/dubbing-system/`（git，remote=GitHub liuxigreen/peiyin，main 分支） |
| Mac 控制面 | `~/duanju/dubbing-system/controlplane`，venv 在 `.venv/`，跑 127.0.0.1:8500 |
| 前端 | `frontend/`（React18+TS+Vite+bun），`bun run build` 后 `cp -r dist/* ../controlplane/web/dist/` |
| 交付包存储 | 环境变量 `MODE_B_STORAGE`（默认 /tmp/peiyin-mode-b，云端=/tmp 同路径） |
| skill | `~/.hermes/skills/dubbing-orchestrator/`（坑点全集，必读） |

### 云端（生产，用户免费领的阿里云 ECS）
| 项 | 值 |
|----|-----|
| 实例 | `i-bp1d8pq7f99dzjc7tiwl`（杭州 cn-hangzhou，2核3.4G/40G 盘，Ubuntu 22.04，**无 GPU**，公网 47.96.168.246） |
| 管理方式 | **workbench CLI**（`~/bin/workbench`，AK 在 `~/.workbench/config.json`）——免公网免 SSH，`workbench exec/list/upload` 三板斧 |
| 代码位置 | `/opt/peiyin/`（git archive 上传解压，更新流程见 §五） |
| 服务 | systemd 双服务：`peiyin`（uvicorn 127.0.0.1:8500）+ `cloudflared`（隧道 9ad5f5f5-0d3c-4c42-93eb-2584db1816eb） |
| 域名 | **https://dubbing.mulan.dpdns.org**（Cloudflare 橙云 + Tunnel，免备案——域名没 ICP 备案，80/443 直连会被阿里云拦截，**只能走隧道**） |
| 注意 | caddy 也装了但已被 Tunnel 替代（留着无害）；Mac 的 cloudflared 管的是另一条 mulan 隧道（duanju 面板:8009），别搞混 |

### 外部服务
| 项 | 说明 |
|----|------|
| GitHub | `liuxigreen/peiyin`（公开仓库，gh CLI 已登录 liuxigreen，ADMIN） |
| 翻译 API | **edgefn 网关** `https://api.edgefn.net/v1` + 模型 `MiniMax-M3`（思考模型！max_tokens 要给足 256+；key 用户在网页 Providers 里填的，Fernet 加密存 DB，ENCRYPTION_KEY=peiyin-dev-key-change-in-prod） |
| R2 对象存储 | 已建桶+凭证（`deploy/.env.r2`，未提交 git），**尚未启用**——r2.py 双模式：不填 env 自动降级本地盘 |
| 租 GPU | compshare 4090 华北一C ¥1.94/h（未租过）；SCNET OpenAPI 有容器开关机接口（算力 Agent 干跑中，等 token） |
| 阿里云 AK | 在 `~/.workbench/config.json`（workbench 用的）；OpenAPI 签名调用样例见会话 |

---

## 三、架构与代码地图

```
dubbing-system/
├── controlplane/            # FastAPI 控制面（Python 3.11, SQLAlchemy 2）
│   └── app/
│       ├── main.py          # 入口：路由挂载+API_TOKEN中间件（空=开发模式）
│       ├── api/             # projects/providers/tasks/nodes/upload/translate/agents/mode_b_api
│       ├── orchestrator*.py # DAG编排：幂等落库/READY扫描/reaper lease回收/input_hash缓存
│       ├── translate_executor.py  # 五步链（T205上下文包→T211直译→T212意译→T213终检→T214落库→T220音节）
│       ├── render.py        # SRT/ASS生成+配音轨预拼+mux(两遍loudnorm)+QC三查
│       ├── qc_agent.py      # 12环节质检钩子（挂在completed后，失败rerun/review）
│       ├── power_agent.py   # 算力Agent（GPU水位→开关机决策，干跑模式）
│       ├── mode_b.py        # 模式B：音频槽位切分+TTS降级占位+交付包zip
│       └── db/models.py     # 9张表（SQLite dev / Postgres prod 双方言）
├── frontend/src/            # 7页面：Projects/NewProject(A|B选择)/ProjectDetail/Utterances/Providers/Assets/Architecture
├── gpunode/                 # 节点协议(entrypoint.py: register/claim/heartbeat/complete/fail) + stages/
│   └── stages/offline.py    # 假stage（NODE_MODE=offline）；real_cpu.py=fit+擦字幕CPU真实现
├── deploy/                  # docker-compose/Caddyfile/env.example（VPS容器化部署用）
└── 文档7份                  # V3-FINAL(总纲)/ARCH-V3.1(多Agent+QC矩阵)/MODE-B-DESIGN/OFFLINE-PLAN/CLOUD-DEPLOY/ORCHESTRATION/PIPELINE-DETAILS + REVIEW-RESPONSE(评审修复记录)
```

**数据流**：浏览器 →(HTTPS)→ CF Tunnel → cloudflared → uvicorn:8500 → Postgres/SQLite 任务表 → 各 Agent 消费。一切状态在任务表，无内存态，随时 kill 重启。

---

## 四、已验证的事实（别重新踩坑）

1. **测试**：`cd controlplane && .venv/bin/python -m pytest tests/ -q` → 26 passed。改任何代码先跑这个。
2. **离线 E2E**：`NODE_MODE=offline .venv/bin/python scripts/offline_e2e.py` → SRT 进成片出，全绿。
3. **真实翻译已验收**：项目「白月光」2803 句 M3 全英文完成（25min，~120句/min），25% 音节超限（正常，待二次压缩闭环）。
4. **大坑（都会咬人）**：
   - SQLAlchemy JSON 列：同引用原地改 dict 再赋回 = **commit 不落库**，必须 `dict(obj.col)` copy
   - claim 身份必须传真实 node.id（曾硬编码 "n" 导致归属校验全部失效）
   - 思考模型（M3）翻译要 prompt 硬约束目标语种 + 落库前中文比例校验，否则它输出中文润色版
   - SRT 要剥内嵌 HTML 标签（`<b>` 等）
   - ffmpeg ass 滤镜路径：chdir+basename，别拼绝对路径（Windows 必炸）
5. **前端纪律**：`USE_MOCK` 默认 false（真 API）——别改回去；新页面一律接真 API 不留 mock 壳。
6. **安全**：API_TOKEN 生产必须设置（留空=无鉴权，仅限本机调试）；provider key Fernet 加密，ENCRYPTION_KEY 生产要换强随机。

---

## 五、日常操作手册

**更新云端部署**（改完代码后）：
```bash
cd ~/duanju/dubbing-system
git add -A && git commit -m "..." && git push        # 1. 提交推送
git archive HEAD | gzip > /tmp/peiyin.tar.gz          # 2. 打包
~/bin/workbench upload /tmp/peiyin.tar.gz /tmp/ --instance-id i-bp1d8pq7f99dzjc7tiwl --force
~/bin/workbench exec --instance-id i-bp1d8pq7f99dzjc7tiwl \
  --command "cd /opt/peiyin && tar xzf /tmp/peiyin.tar.gz && systemctl restart peiyin && sleep 3 && systemctl is-active peiyin"
curl -s https://dubbing.mulan.dpdns.org/health        # 3. 验证
```
注意：改动 requirements 后云端要重装依赖（workbench exec 里 `.venv/bin/pip install ...`，用国内源快）；改前端必须先 `bun run build` 并同步 dist 再打包。

**云端排障**：
```bash
~/bin/workbench exec --instance-id i-bp1d8pq7f99dzjc7tiwl --command "systemctl is-active peiyin cloudflared"
~/bin/workbench exec --instance-id i-bp1d8pq7f99dzjc7tiwl --command "journalctl -u peiyin -n 20 --output=cat"
```

**长任务跑法**：workbench exec 30s 就超时——长任务用 `systemd-run --unit=任务名 --collect -p Environment=... 命令`，查进度查 `journalctl -u 任务名` 或轮询 DB/API。**别用 nohup+&**（workbench 会等）。

**本地跑**：`cd controlplane && .venv/bin/python -m uvicorn app.main:app --port 8500`；Mac 上 8500 被占用先 `lsof -ti:8500 | xargs kill`。

---

## 六、待办路线（接手后按序干）

| # | 任务 | 说明 | 算力 |
|---|------|------|------|
| 1 | 音节超限二次压缩闭环 | 703 句 >1.15 自动回 T212 压缩重译（≤2轮），用户已验收翻译质量基线 | CPU |
| 2 | 模式 B 详情页补 UI | 交付包下载按钮 + 音频补传入口 + 进度百分比（列表 10s 轮询已有） | CPU |
| 3 | 模式 B 出真配音 | 接 Confucius4 在线 API（id/es/pt 等小语种免 GPU 即可全链）；en 等租 GPU | 在线API/GPU |
| 4 | GPU 实装（G0） | 租 compshare 4090 → gpunode join → FunASR+pyannote → Demucs → CosyVoice2 逐个点亮；按 ARCH-V3.1 显存预算+防OOM三闸 | GPU |
| 5 | R2 启用 | .env 填 R2_* 三件套（deploy/.env.r2 有值）→ 直传直下+Multipart 生效 | — |
| 6 | 模式 A 完整链 | 依赖 4；识别对齐→TTS→混音→缝合全流程 | GPU |
| 7 | Postgres 迁移 | 生产换 DATABASE_URL（SQLAlchemy 已双方言），alembic 对齐 schema | — |
| 8 | 算力 Agent 接真 SCNET API | 干跑→实跑自动开关机 | — |

**用户偏好提醒**：全自动优先（少问人在环）；质量抓翻译和配音（B 模式存在的意义）；成本敏感（GPU 按需租用完关机）；用户会核对价格和数据；红线操作（花钱/改线上/删数据）先确认。

---

## 七、快速自检（接手第一天跑一遍）

```bash
cd ~/duanju/dubbing-system && git pull && git log --oneline -3   # 代码最新
cd controlplane && .venv/bin/python -m pytest tests/ -q          # 26 passed
curl -s https://dubbing.mulan.dpdns.org/health                   # {"status":"ok"}
~/bin/workbench exec --instance-id i-bp1d8pq7f99dzjc7tiwl \
  --command "systemctl is-active peiyin cloudflared"              # active active
```
全绿 = 交接完成，可以开始干活。有疑问先读仓库 7 份文档 + `~/.hermes/skills/dubbing-orchestrator/SKILL.md`。
