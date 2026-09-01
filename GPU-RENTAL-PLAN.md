# GPU 租用方案（GPU-RENTAL-PLAN）

> 2026-09-01 定稿（备案用）。**本文是既有 GPU 设计的收拢备案，不是新设计**——
> 开关机逻辑见 ARCH-V3.1 §三（算力Agent+¥50日安全阀）、任务/资源模型见
> ORCHESTRATION.md（io/cpu/gpu 三类 · gpu_required=false 任何节点可领 ·
> G-GPU 里程碑）、干跑→API 切换见 OFFLINE-PLAN.md §四（SCNET_OPENAPI_TOKEN）、
> 盘预算与 TTS 选型见 V3-FINAL §2/§7.4、租卡假设见 COMPARISON-0829.md。
> 本文补充：3060 实测基线、4090 接入操作序、混合池调度与风险对策。

## 〇、实测基线（2026-09-01 白月光实战）

| 项 | 实测值 |
|----|--------|
| 3060 12G · CosyVoice3 单并发 | 合成延迟中位 2.9s / p90 4.0s |
| 节点请求开销（新建连接/次，走代理+CF隧道） | ~9s/句 → keep-alive 复用后 ~2s/句 |
| 端到端吞吐 | 3.1 句/分 → **7.4 句/分**（entrypoint 持久连接后实测） |
| 全剧 2803 句预计 | ~6.3h（3060）· 4090 batch2 预计 ~2h |
| 产物回传 | artifact 端点逐句落盘+落 tts_clips，实测 245+ 文件零丢失 |

## 一、租用选型

| 平台 | 规格 | 价格 | 适用 |
|------|------|------|------|
| **compshare 4090（华北一C）** | RTX 4090 24G + Xeon 16核/60G内存 + 50G盘 | **¥1.94/h** | 主力：en 全剧 TTS / 模式A 全链 |
| SCNET 容器 | 按量 | 待询价 | 备选；OpenAPI 有开关机接口（power_agent 对接目标） |
| 自有 3060 | 12G | 电费 | 日常验证/小批量（当前已接入） |

选型裁决（与 V3-FINAL/DESIGN-COMPARISON-DEEP 一致）：4090 24G 支持
CosyVoice2 batch≥2 显存余量 + NVENC 编码 + Demucs，单卡串行 90min 剧 ≈ 2.2h ≈ **¥4.3/部**。

## 二、接入流程（4090 到活）

```
1. 开机（手动或 power_agent 自动）→ 拿 SSH
2. 一次性环境（~30min）：
   git clone https://github.com/liuxigreen/peiyin.git && cd peiyin/gpunode
   pip install -r requirements.txt
   # CosyVoice3 服务（fp16 + 单并发起步）
   # Demucs（separate-vocals stage 用）
   nvidia-smi 确认驱动/CUDA
3. join 进池：
   CONTROL_URL=https://dubbing.mulan.dpdns.org \
   NODE_SHARED_SECRET=<生产secret> \
   NODE_MODE=real GPU_MODEL="RTX 4090" GPU_VRAM=24 \
   python entrypoint.py        # systemd 常驻或 tmux
4. 控制面 gpu_nodes 出现新行 → claim 自动领任务 → 完成
```

节点代码同步纪律：entrypoint.py 已带 **token 持久化**（workdir/node_token.txt，
重启不换 token 不堆行）与 **artifact 回传**（/api/nodes/tasks/{id}/artifact）。
更新节点 = 覆盖 entrypoint.py + stages/tts_node.py 两个文件后重启。

## 三、自动开关机（算力 Agent，power_agent.py 已干跑）

```
每 60s：
  gpu队列 pending≥2 持续5min 且 无在线GPU节点 → 开机（OpenAPI/一键脚本）
  gpu队列空 且 全部completed 持续10min → 通知 → 10min后关机
安全阀：单日GPU预算上限（默认¥50，超了只通知不开机）
配置位：projects.config.gpu_auto_on/off + settings.daily_gpu_budget
```

上线前置：compshare 控制台确认有无 API 密钥入口；有→接 OpenAPI，
没有→降级「网页按钮 + TG 通知」人工 10 秒开机。

## 四、50G 盘预算与流式策略（4090）

| 项 | 占用 |
|----|------|
| 系统 + CUDA + Torch | ~14G |
| 模型权重（CosyVoice3 ~3G / Demucs 0.4G / pyannote 0.2G） | ~4G |
| pip/conda 缓存 | ~10G |
| **工作区（流式，只留当前 ≤2 切片）** | 峰值 ≤4G |

中间产物即传即删（R2 备份权重 ~¥0.9/月），单卡串行无磁盘风险。

## 五、成本模型（实测校准）

| 项 | 3060（自有） | 4090（租用） |
|----|-------------|-------------|
| TTS 2800句 | ~2.5-4h（电费） | ~1.5-2h（batch 2 可再半）≈ ¥3-4 |
| 模式A 全链（擦字幕/分离/ASR/TTS/混码烧） | — | ≈2.2h ≈ ¥4.3 |
| 翻译 API | ¥1-3/部（M3+DeepSeek） | 同 |
| **单部剧总成本** | ≈电费 | **≈ ¥5-8** |

对比鬼手剪辑 ¥17-151/部：10-30 倍成本优势，质量靠五步链+QC 拉齐。

## 六、风险与对策

| 风险 | 对策 |
|------|------|
| 代理/隧道 SSL 抖动（3060 实测有） | entrypoint 已换 keep-alive 持久连接；claim/complete 失败自动重试；reaper 收割 stale lease |
| 实例抢占/释放 | 权重备份 R2，5 分钟热恢复；任务表 SQLite/PG 全持久，重启即续 |
| 显存 OOM | fp16 + 单并发起步；dispatch 的 fail(retryable) 链路已通 |
| 节点离线任务堆积 | reaper 10min lease 回收自动重派；tts-requeue 一键补传缺产物任务 |
| 成本失控 | 日预算安全阀（¥50 默认）+ 用完即关 + 每部剧成本复盘 |

## 七、3060 → 4090 混合池调度（现状即支持）

控制面 claim 无 GPU 型号过滤——3060 与 4090 可同时在线分摊队列；
`PREFERRED_MODEL` env 预留按模型路由。建议：租用期把 3060 一起挂着跑
小语种/兜底，4090 跑 en 主力，用完各自关机。
