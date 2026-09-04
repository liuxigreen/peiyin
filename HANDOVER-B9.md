# 交接文档 HANDOVER-B9（2026-09-04 晚）

> 接手前按序读：`HANDOVER-0902.md`（基线+0902事故）→ `HANDOVER-B8.md`（B7/B8波次+CV3塌缩+claim修复）→ 本文档。
> 冲突处以本文为准。核心纪律不变：实测与本档冲突 → 改档记日期。
> 当前会话：AutoCoder（auto-coder agent，OpenClaw），Mac 侧持有 workbench 通道与 git push 权限。

---

## 一、当前状态快照（30 秒版）

**白月光 B 方案 v1 交付包已产出并验证（预设音色版，2743句，missing=0）。
下一大步 = 真克隆（diarize声纹聚类→角色原声重合成），测试批卡在节点代码未更新，等 3060 执行 TASK-1~4。**

| 项 | 状态 |
|----|------|
| 分支 | `upgrade/seam-wave1`（Mac/ECS 已同步，HEAD 见 git log；origin 与云端一致） |
| 云端服务 | ✅ active（health ok）——0904 上午曾崩溃，根因见 §四事故#1，已修 |
| 白月光 | pid=`10f001e14c5c41e6bb17fdd8465d1586`（**B8 文档里的旧 pid 已作废**），2803 句/71 场景 |
| 翻译 | ✅ 完成（含 LLM 重跑版 + 上传直连版，latest 可用） |
| 台词→角色 | ✅ LLM 文本绑定 2793/2803，29 角色 |
| **TTS v1（预设音色）** | ✅ **2743 句全部完成，missing=0**（60 句【第N段】标记行正确排除），2885 clips |
| **交付包 v1** | ✅ 已产出+下载验证：`/opt/peiyin-mode-b/10f001e1/download_all.zip`（279MB=dubbing_package+zh_audio.mp3）；包内 audio/2743 wav + subtitles.en.srt/ass + manifest + qc_report |
| **音频质量体检** | ✅ 2743 句全扫：削波 0 / 尾塌缩 0 / 过轻 1 / 过短 1 / 大量静音 81（复核=短句自然留白，非缺陷）；健康率 97%；超窗 994（atempo≤1.5 已压过，残余进剪映手调） |
| 音色分布审计 | ⚠️ **实际只用 6 条预设音色**：女声A×1193（林惜音+叶娇娇+王楚熙+娇娇+刘桂芳+伯母共用）、男声A×979（楚弋+群众+路人等13角色）、男声B×435（盛君霆+阿弋）、女声B×124（楚母）、女声C×35（吴妈/夫人/盛母）。用户听感"就2个人的声音"——根因：L2 匹配按性别+年龄段，6条资产天然撞车；timbre 描述只打分未输出 instruct（instruct 仅 1 句） |
| diarize（真克隆第一步） | ⬜ **卡在节点**：TEST/TEST2 两批都 dead，错误 `zh_audio not found on node:`——根因：v1 diarize_node.py 只认节点本地路径，不认 payload.zh_audio_url。**已修**（commit ff4bc25：节点经鉴权 GET 拉 167MB zh_audio 落 workdir），**等 3060 更新代码重跑** |
| 原配音音频 | ✅ `/opt/peiyin/assets/zh_audio.mp3`（170MB，已持久化；/tmp 副本重启会丢但 /opt 是权威） |

## 二、接手第一件事：跑通 20 分钟克隆测试（用户明确要求小步验证，禁全量）

### 1. 节点侧（用户/3060 Agent，按 `NODE-TASKS-3060.md`）
- TASK-1 代码同步（**必须重新拉**：diarize 下载通道是 ff4bc25 新增）
- TASK-2 pyannote.audio==3.1 + HUGGINGFACE_TOKEN（hf.co/settings/tokens 免费建）
- TASK-4 重启 entrypoint（NODE_WORKERS=2）
- 详细命令在 `NODE-TASKS-3060.md` TASK-1~4，逐条有验收标准

### 2. 云端侧（接手 Agent）
- 节点上线后重建 diarize 任务（两个 dead 任务勿复用，output_paths 已污染）：
  参考 `/tmp/create_diarize2.py`（ECS /tmp，重启则失；逻辑=slots 限前 20 分钟 413 句 + payload.zh_audio_url=/api/nodes/voices/zhaudio_09.mp3 + registry 登记 zhaudio_09.mp3→/opt/peiyin/assets/zh_audio.mp3）
- 节点日志预期：`[diarize] zh_audio downloaded: 162MB` → `cut 413` → `embed 413` → `[art] uploaded diarize_result.json`
- 拿到 artifact 后：读 clusters（预期 5-10 簇，最大簇≈林惜音），LLM 簇→角色绑定（一次调用，带每簇抽样台词）
- **试听对比包**：挑 5 主角各 2 句，原声切片 vs CosyVoice 克隆版并列 zip，经 `/mode-b/file/{name}` 给用户下载
- **用户耳朵验收后**才允许全量重合成（2793 句 × 6-8h），禁跳步

### 3. 全量重合成后的交付（v2 交付包，用户点名要"标准音轨文件"）
- v1 包是逐句 wav；v2 必须加 `dub_track_full.wav`（整条配音音轨：按窗口落位、重叠避让、片头对齐）
- 拼轨逻辑：按 manifest 的 start_ms/end_ms 把逐句 wav 混入 2h 静音底（B7 audio_post.py 的 master_mix 基础上扩展）
- 用户工作流 = 剪映导入三件套（视频 + dub_track_full.wav + subtitles.en.srt）自行调整——**系统不做最终成片合成，出素材包即可**

## 三、这波（0902夜~0904）落地的能力与修复（全部实测）

### 云端新端点（60 测试全绿，但见 §四事故#3 的测试环境差异）
| 端点 | 用途 |
|------|------|
| `POST /projects/{pid}/mode-b/tts-batch` | 批量单句 TTS（input_hash 幂等含 voice_id/rate/emotion；uids/scene/limit 精选） |
| `POST /projects/{pid}/mode-b/package-from-clips` | 真交付包（fit atempo≤1.5 联合上限防 chipmunk；markers 排除；B7 工艺 post/me_path） |
| `POST /projects/{pid}/mode-b/tts-requeue` | 死任务复活（默认跳过 dead，include_dead=true 才包含） |
| `POST /projects/{pid}/bind-speakers` | LLM 台词→角色绑定（幂等） |
| `GET /projects/{pid}/mode-b/file/{name}` | 工作文件下载（白名单；**大文件 >190MB 会被 Cloudflare 掐断，需分段/断点续传**） |
| `POST /nodes/tasks/{id}/artifact` | 节点产物回传（raw body） |
| `GET /nodes/voices/registry.json` + `GET /nodes/voices/zhaudio/{name}` | 整条音频下发通道（diarize 用，registry=名字→云端路径映射） |
| `POST /projects/{pid}/seed-translation` | 双语 SRT 直连翻译库（B8，实测 100/100 matched） |
| `POST /projects/{pid}/mode-b/diarize` | 声纹聚类任务创建（slots+音频URL 下发） |

### 修复的存量 bug（均带回归测试或实测）
1. **main.py 裸 import**（0904 崩溃根因）：`from orchestrator_reaper import`/`from db.session import` 缺相对点号 → 服务崩循环、域名 1033/530。已修两处 + Mac 侧合并时再次踩到（B8 提交覆盖回旧版）→ 6b43cc0 再次修复。**教训：main.py 的 import 在 ECS 和 Mac 两边都验证过才算修**
2. **B7 打包签名被覆盖**：merge 时 mode_b.py 被 B8 侧旧版覆盖丢 `post/me_path` 参数 → package-from-clips 必崩。6b43cc0 恢复 7a88744 版本
3. complete 数据丢失 / tts_clips 挂占位行 / 标记行全链过滤 / task_key 同秒碰撞 / 缓存键吞参数 / register 不建档 / G1-G5 翻译链衔接——见 HANDOVER-0902 §三
4. **claim 性能**：16.6s→0.3s（窗口化+idx_claim(status,priority,created_at)+WAL，eaa3ee1）
5. **节点并发**：NODE_WORKERS=2 线程池 + artifact 回传 + keep-alive + token 持久化

### 音色与质量
- `voice_assign.py` 三级分配（L1簇参考音→L2资产标签×2+timbre关键词→L3性别兜底）
- CV3 塌缩修复（引擎<0.25s检测+回退中性instruct）；voice_assets 6条已裁静音
- fit_clip：tts_rate×atempo≤1.5 联合上限；over_window 如实标记（994句）
- 音频体检脚本：`/tmp/qc_full.py`（zip 全扫：削波/响度/静音/塌缩/超窗分类统计）

## 四、事故记录（两起，都有根因）

1. **0904 上午域名 1033/530 全断**：非"批量压垮机器"（该假设被推翻）——是升级 Agent 部署 reaper 代码时 main.py 裸 import → 崩循环。workbench 通道（云助手）恰好也挂着导致误判整机失联；实际实例 Running，只是服务死。用户在控制台重启机器后恢复。**教训：先验证云助手/隧道/服务三层的独立状态，别把服务死判成机器死**
2. **diarize 首派失败×2**：节点 v1 代码只认本地 zh_audio 路径。ff4bc25 已补下载通道。**教训：下发 payload 前先跑通节点代码版本核对**

## 五、红线（不可违反）

1. 参考音频绝不入库（b64/data:/超长 blob 出库走 /artifact 流式端点；complete 端点有保险丝 413）
2. TTS 只用 CosyVoice 系本地引擎；禁 xTTS v2/F5-TTS（授权）
3. **用户明确要求：小步验证，禁不验收就全量跑**（0904 16:14 用户："不能一次全量跑完"）——每步出试听包给用户耳朵验收
4. main.py 的 import 修改必须 ECS+Mac 双侧验证
5. entrypoint.py 有另一 agent 的 staging 血统未提交改动——**云端勿覆盖 gpunode/entrypoint.py**（0903 约定继续有效）
6. git push 只能从 Mac（ECS 无凭据）；ECS 提交用 bundle 迁移：`git bundle create /tmp/x.bundle HEAD` → workbench download → Mac `git fetch bundle HEAD:refs/heads/ecs-xxx` → merge
7. 云端 API_TOKEN 为空（P0 遗留）：隧道是唯一闸门；配强密码需用户拍板

## 六、待办（按优先级）

| # | 项 | 说明 |
|---|-----|------|
| 1 | **diarize 测试批跑通** | 节点更新代码（ff4bc25）→ 重建任务 → 出簇结果 → LLM 绑定 → 试听对比包 → 用户验收 |
| 2 | 全剧克隆重合成（v1→v2） | 用户验收后发；~6-8h 节点时间 |
| 3 | **dub_track_full.wav 整条音轨** | v2 交付包必含（用户点名）；落位逻辑扩展 audio_post.master_mix |
| 4 | 网站三件套修复 | 进度条接 tts-batch 数据 / 角色卡真实试听（点开能听，当前是摆设）/ A/B 方案步骤动态区分（用户 0904 明确不满） |
| 5 | 超窗 994 句消化 | retranslate-overlimit 端点已就绪（B7），跑一轮压缩重译 |
| 6 | diarize 声纹全量 | 20 分钟测试通过后去掉 start_ms<1200000 限制跑全剧 2793 句 |
| 7 | M&E ducking 验证 | 代码就绪（B7），需真实原声素材进剪映后反馈 |
| 8 | 音色文件持久化 | /tmp/peiyin-mode-b/voices → R2（r2.py 双模式现成） |
| 9 | 服务器 | 用户已定购国内 4C4G3M 新机；迁移时 SQLite→Postgres + R2 一起做 |
| 10 | API_TOKEN | P0，等用户拍板方案 |

## 七、快速自检（接手第一遍）

```bash
cd ~/duanju/dubbing-system && git checkout upgrade/seam-wave1 && git pull
cd controlplane && .venv/bin/python -m pytest tests/ -q   # 60 passed（Mac 本地 5 个 b7 测试需 ffmpeg，缺席则 55+5fail 属环境差异）
curl -s https://dubbing.mulan.dpdns.org/health             # {"status":"ok"}
~/bin/workbench exec --instance-id i-bp1d8pq7f99dzjc7tiwl --command "systemctl is-active peiyin cloudflared"
# 节点心跳
~/bin/workbench exec --instance-id i-bp1d8pq7f99dzjc7tiwl --command "cd /opt/peiyin/controlplane && /opt/peiyin/controlplane/.venv/bin/python -c \"import sqlite3; c=sqlite3.connect('dev.db'); print(list(c.execute('SELECT name,last_heartbeat FROM gpu_nodes ORDER BY last_heartbeat DESC LIMIT 2')))\""
# 交付包就位
~/bin/workbench exec --instance-id i-bp1d8pq7f99dzjc7tiwl --command "ls -la /opt/peiyin-mode-b/10f001e1/download_all.zip"
```

全绿后从 §二 开始（diarize 测试批）。

## 八、多 Agent 协作纪要（现状）

- **Mac 侧 AutoCoder**（本文作者）：git push 权限、云端 workbench、与用户直连、总控与验收
- **ECS 侧升级 Agent**：B7/B8/claim 修复的作者；commit 在 ECS 本地（经 bundle 迁移合并）；有未提交工作态（mode_b_api run 跳过已翻译逻辑/tasks.py 翻译进度端点，已 stash，与 B8 提交重叠）
- **3060 侧 Agent**：NODE-TASKS-3060.md 执行者；0903 夜修过代理劫持/引擎塌缩回退；**entrypoint.py 有其未提交改动，勿覆盖**
- 协作风险：三方同时改 gpunode/ 会互踩（已有先例）——**改动前先看 NODE-TASKS-3060.md 与 HANDOVER 最新版是否有人在做同一件事**
