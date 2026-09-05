# 交接 HANDOVER-B9b（给 Codex，0906）

> 只需读本文档即可接手。本地仓库 `~/duanju/dubbing-system`（= GitHub liuxigreen/peiyin main 分支，已全部推送）。
> 冲突时以本文档为准。核心纪律：**实测与本档冲突 → 改档记日期**。

## 一、系统是什么

中文短剧 → 外语配音的云端流水线（方案B）：

```
中文SRT ──seed──> utterances ──翻译(M3/Kimi/DeepSeek轮换,限速)──> Translation
       ──角色绑定(LLM文本绑定 + diarize声纹[可选])──> speaker_id
       ──TTS(CosyVoice3, 3060节点认领)──> tts_clips
       ──fit(atempo≤1.5) + B7工艺(DSP链/呼吸声) + 母带(-13LUFS)──> 交付包zip
```

## 二、本地文件位置（都在 `~/duanju/dubbing-system/`）

| 路径 | 内容 |
|---|---|
| `controlplane/` | FastAPI 控制面（云端 ECS 跑的） |
| `controlplane/app/api/mode_b_api.py` | **核心**：seed/翻译/绑定/tts-batch/打包/试听包/审计端点 |
| `controlplane/app/audio_post.py` | B7句级DSP+呼吸+母带混音（ffmpeg滤镜链） |
| `controlplane/app/prosody_qc.py` | 韵律质检门（F0半音std<1.8=平坦→重掷） |
| `controlplane/app/gate_b.py` | 闸门B：配角6预设映射+音色塌缩红线 |
| `controlplane/app/voice_assign.py` | 音色分配（兜底+映射） |
| `controlplane/app/translate_executor.py` | 翻译链（限速器：M3=9/min, 其他edgefn=4/min） |
| `controlplane/app/render.py` | 字幕/ASS/mux（响度统一读 `MASTER_LUFS` env，默认13） |
| `gpunode/entrypoint.py` | 3060节点主循环（claim批量+NODE_WORKERS并发池） |
| `gpunode/stages/tts_node.py` | TTS stage（代理绕过+instruct透传） |
| `gpunode/stages/diarize_node.py` | 声纹stage（zh_audio_url下载+HF镜像） |
| `frontend/` | React前端（bun build，dist 同步到 ECS `controlplane/web/dist`） |
| `NODE-TASKS-3060.md` | 3060 agent 任务板（git协作） |
| `HANDOVER-B8.md` | 上一轮交接（本地文件，不入库） |

## 三、运行环境（三台机器）

| 机器 | 角色 | 访问 |
|---|---|---|
| **Mac 本地** | 开发+git推送枢纽 | `~/duanju/dubbing-system`，bun/node 已装 |
| **ECS**（阿里云 i-bp1d8pq7f99dzjc7tiwl） | 控制面+存储 | `~/bin/workbench exec --instance-id ...` 命令行；服务 `systemctl restart peiyin`（端口8500，仅本地+Tailscale）；代码在 `/opt/peiyin` |
| **3060**（Windows, LENOBO） | TTS/声纹节点 | ECS 上 `ssh -o ProxyCommand='nc -X 5 -x 127.0.0.1:1055 %h %p' LENOBO@100.67.15.30`（Tailscale 已通）；代码 `E:\peiyin-node\peiyin-current\gpunode`；启动 `run_node.ps1`；计划任务 peiyin-node-keepalive 每分钟保活 |

网站：`https://dubbing.mulan.dpdns.org`（CF tunnel → ECS:8500）
数据：ECS `/opt/peiyin/controlplane/dev.db`（SQLite WAL）
产物：ECS `/tmp/peiyin-mode-b/`（voices/artifacts/{pid8}/package）

## 四、当前项目与状态

- **白月光**：pid=`10f001e14c5c41e6bb17fdd8465d1586`，2803句/71场
  - 翻译 ✅ 全部完成；角色 41 个（LLM绑定 2710，93群杂归"路人甲"）
  - TTS v1 ✅ 2803 句全部完成（**但用的是6预设音色+旧工艺，v1包有音色撞车问题**）
  - v1 交付包已出（279MB），QC：健康率97%、塌缩0
  - **待办**：diarize 测试批（前20分钟413句）在节点上跑了很久未完成（疑似逐句embed循环不落盘）→ **建议放弃等它，直接用预设音色出试听包给用户验收，通过后再全量v2**
- **情绪剧V2**：pid=`cc8b0097feef49d2ba5042c5a6039541`，100句全程验证过（B7工艺+情绪+母带全通），可当参照系

## 五、已修复的坑（别再踩）

1. **b64禁入JSON列**（0902事故）：complete端点有保险丝自动拒收>64KB/blob
2. **audio_slots 曾全量读音频→OOM**：已改流式切片
3. **progress轮询全量ORM加载→OOM**：已改轻列查询；uvicorn 2 workers + keepalive 25
4. **情绪串台**：tts-batch 每句独立 `line_body`（勿改回共享body）
5. **M&E ducking 输入索引**：动态算（不能写死[2:a]）
6. **响度统一**：master/mux/QC 全读 `MASTER_LUFS` env（默认13），改一处要三处同步
7. **claim 性能**：窗口化LIMIT 50+idx_claim+WAL（16.6s→0.3s）
8. **CF隧道大文件不稳**：>100MB 传输偶发502/reset——节点拉大文件走 Tailscale 内网 `http://100.77.187.54:8500`（节点 run_node.ps1 的 CONTROL_URL 已切）
9. **节点双进程互踩**：peiyin-node-keepalive 计划任务+runtime python 会双开 entrypoint，杀一个留 venv-sep 的
10. **diarize_node**：token路径=`stages/../workdir/node_token.txt`；HF 下载必须走镜像（`HF_ENDPOINT=https://hf-mirror.com`，已写进 run_node.ps1）

## 六、测试命令速查

```bash
# ECS上跑测试（60+3个测试全绿才算完）
cd /opt/peiyin/controlplane && .venv/bin/python -m pytest tests -q

# 出试听包（每音色2句+主角全覆盖）
curl -X POST localhost:8500/api/projects/<pid>/mode-b/audition-pack \
  -H 'Content-Type: application/json' -d '{"per_voice":2}'

# 重建TTS批次（幂等）
curl -X POST localhost:8500/api/projects/<pid>/mode-b/tts-batch \
  -H 'Content-Type: application/json' \
  -d '{"engine":"cosyvoice_api","engine_url":"http://127.0.0.1:50000/tts","rate":1.25}'

# 打包（自动含B7工艺+母带）
curl -X POST localhost:8500/api/projects/<pid>/mode-b/package-from-clips \
  -H 'Content-Type: application/json' -d '{}'
```

## 七、协作纪律

- **改完必须双侧验证**：Mac 编译/测试过 + ECS 重启服务后真实请求过，才算完成
- **git 推送只能从 Mac**（ECS 无凭据）；改 ECS 文件后同步回 Mac 仓库提交
- **gpunode/ 目录云端勿覆盖**（3060 侧有本地补丁超集）；节点变更走 NODE-TASKS-3060.md
- **workbench exec 单条 30s 超时**：长任务用 `systemd-run --unit=X --collect` 或 nohup
- **服务 env**：独立脚本连 DB 需要 `eval export $(systemctl show peiyin -p Environment --value)`

## 八、当前挂起（Codex 可直接开工）

1. **出预设音色试听包**给用户验收（见第六节命令，白月光 pid 在上）
2. 用户验收后 → 全量 v2 重合成（6-8h，3060 自动认领）
3. v2 包补 `dub_track_full.wav` 整条音轨（用户点名要）
4. 网站三件套：进度条真实化、角色卡试听、A/B区分
5. diarize 修复方向：embed结果落盘+batch推理（413句一次喂GPU）而非逐句
