# 审核任务简介（给外部审核 Agent）

> 你受托对「短剧出海 AI 译配系统」（代号 peiyin）做全面技术审核。
> 本文件是入口：先按"阅读顺序"建立全貌，再按"审核范围"逐项审查，
> 最后按"产出要求"输出报告。不要跳过阅读顺序——文档之间有严格的时序与因果关系。

## 一、阅读顺序（必须按序）

1. `HANDOVER.md` — 项目交接文档：系统是什么、关键位置、已验证事实、坑、待办
2. `V3-FINAL.md` → `ARCH-V3.1.md` → `ORCHESTRATION.md` → `PIPELINE-DETAILS.md` — 总纲→架构→编排→管线细节
3. `MODE-B-DESIGN.md` — 模式B（字幕+配音音频，当前主航道）
4. `DESIGN-COMPARISON-DEEP.md` + `COMPARISON-0829.md` — 与外部方案的对标裁决（22项精华/18项缺口/5项选型）
5. `DESIGN-B-配音方案-v2.md` — 配音链设计：声纹聚类→LLM绑定角色→角色化TTS（speaker归属的补白设计）
6. `gpunode/README-3060.md` — GPU节点接入协议与3060安装定版
7. `REVIEW-RESPONSE.md` — 上一轮评审的修复记录（D1-D7，可对照验证修复质量）
8. 源码（重点文件见下）

## 二、源码结构（controlplane/ 为主战场）

```
controlplane/app/
├── translate_executor.py   # 翻译执行器 v1.3：多Agent分步(M3直译→DeepSeek本地化→M3终检)
│                           #   +段级兜底链+每模型限速器+格式漂移重试+内容审查识别+压缩闭环
├── cast_agent.py           # C0角色提取Agent：LLM扫全剧台词→角色档案+中英人名术语表
├── orchestrator*.py        # 纯DB驱动的DAG编排：幂等实例化/READY扫描/lease回收/input_hash缓存
├── qc_agent.py             # 12环节质检钩子（挂completed后，失败rerun/review）
├── mode_b.py + api/mode_b_api.py  # 模式B：槽位切分→翻译→TTS→交付包zip
├── render.py               # SRT/ASS生成+配音轨预拼+mux(两遍loudnorm)+QC三查
├── power_agent.py          # 算力Agent（GPU水位→开关机决策，干跑模式）
├── db/models.py            # 9张表（SQLite dev / Postgres prod 双方言）
└── api/                    # projects/providers/tasks/nodes/upload/translate/agents/mode_b_api
gpunode/                    # GPU节点协议：entrypoint(register/claim/heartbeat/complete/fail)
│                             + stages(tts-generate/separate-vocals/offline/real_cpu) + README-3060
frontend/src/               # React19+TS+Vite：7页面（Projects/ProjectDetail/NewProject/
                              Utterances/Providers/Assets/Architecture）
```

## 三、审核范围（按优先级）

### P0 翻译执行器（translate_executor.py，~700行，近期大改）
- 多Agent分步链的**并发安全**：8 workers 场景级并行，每 worker 独立 Session，SQLite busy_timeout=30s 够不够
- 段级兜底链 `_run_chunk_filtered`：失败类型（审查/网络/格式漂移）分流逻辑是否有死角
- 限速器 `_RateLimiter`：持锁 sleep 的排队语义在高压下的正确性
- 落库路径：绝不允许占位符落库；llm_model 标记的真实性
- 内容审查识别 `_is_refusal_text`：误判面（ja/ko 目标语误伤可能）

### P1 编排与数据一致性
- orchestrator 的 READY 扫描与 claim 的 SKIP LOCKED 语义（SQLite 侧 json_each 依赖检查）
- 翻译 latest-version 取数路径（跳占位符行）在 api/projects / qc_agent / mode_b_api 三处的一致性
- 单句热修（version+1）与缓存 input_hash 的交互

### P2 安全与部署
- 生产 API_TOKEN 为空=无鉴权（已知，待修复）——评估除鉴权外的纵深防御
- SQLite 当生产库 + /tmp 交付包存储的重启风险
- provider key Fernet 加密链路的密钥管理

### P3 测试与工程
- 32 个测试的覆盖缺口（并发/重试/兜底链的组合态）
- CI（三平台 + 离线E2E）的有效性

## 四、运行态状态（不在 git 里，审核须知）

以下内容**不在仓库**，审不到是正常的，不要臆断：
- 云端生产数据库（ECS /opt/peiyin/controlplane/dev.db，含白月光项目 2819 句译文、5个翻译provider的加密key、38角色speakers、40条术语表）
- 云端 systemd 服务状态（peiyin/cloudflared）、域名 dubbing.mulan.dpdns.org（Cloudflare Tunnel）
- workbench CLI 云端管理通道（AK 在用户 Mac 本地）
- 3060 GPU 节点（另一台机器，按 README-3060 接入中，TTS引擎 CosyVoice 3 + Demucs）
- 生产实测数据（71场景全量重译 70min、284句压缩攻坚 285→83、审查救援3次实战）

如需运行态证据，向项目所有者索取，不要猜测。

## 五、产出要求

输出一份审核报告，包含：
1. 按上述 P0-P3 的发现（每条：严重度 P0/P1/P2 + 证据 + 建议修复）
2. 架构级评价：与 HANDOVER 里"外部方案对比"（DESIGN-COMPARISON-DEEP.md）的交叉验证
3. 明确区分：代码审查结论（可直接验证）vs 运行态推测（无法验证的，标注）
4. 不要泛泛而谈"建议加测试"——指出具体缺哪个测试、测什么路径
