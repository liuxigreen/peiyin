# 交接文档 · 0902 波（给接手 Agent）

> 2026-09-02 早。前一棒：upgrade agent（wave1-4 升级 + 多角色音色链路 + 白月光实战联测）。
> 本文是 HANDOVER.md（0900 基线版）的增量补充，冲突处以本文为准。
> 核心纪律不变：实测与本档冲突 → 改档记日期。动手前先读本文 §四/§六。

---

## 一、当前状态快照（30 秒版）

**多角色音色链路已全线打通并部署云端，只差节点最后一次文件更新。**
更新完成后一条命令发全剧批量（2793 句 × 角色音色 × 1.25速），~6h 跑完，出全量交付包。

| 项 | 状态 |
|----|------|
| 分支 | `upgrade/seam-wave1`（52 tests 全绿，与 origin 同步，已部署云端） |
| 云端代码 | = 分支 HEAD（health ok，版本 0.3.0） |
| 白月光项目 | id=`133d1fea8be24d9c940c8421abfe6cc7`，2803 句，71 场景 |
| 台词→角色绑定 | ✅ 2793/2803（LLM文本绑定），角色已归并 38→29 |
| 预置音色 | ✅ 6 条 voice_assets（edge-tts 生成 wav 在 ECS `/tmp/peiyin-mode-b/voices/`） |
| 主角音色分配 | ✅ 实测正确（见 §五分配表） |
| TTS 队列 | 空（单音色旧批次已 dead 止损，1012 句旧产物保留做对照） |
| **节点更新** | ⬜ **待办#1：用户更新 `gpunode/stages/tts_node.py` 后重启 entrypoint** |

---

## 二、接手第一件事：把全剧多角色配音跑起来

### 1. 请用户更新节点（唯一阻塞项，30 秒）
```powershell
cd E:\peiyin-node
Invoke-WebRequest "https://cdn.jsdelivr.net/gh/liuxigreen/peiyin@upgrade/seam-wave1/gpunode/stages/tts_node.py" -OutFile stages\tts_node.py
# Ctrl+C 停 entrypoint → 原命令重跑
```
该版本新增：参考音 base64 落地（`workdir\refs\{voice_id}.wav` 缓存，同音色任务复用本地文件）。
节点现状：rtx3060-e（Windows，E:\peiyin-node），已带 artifact 回传 + keep-alive + token 持久化。

### 2. 发全剧批量（节点更新确认后）
```bash
curl -X POST https://dubbing.mulan.dpdns.org/api/projects/133d1fea8be24d9c940c8421abfe6cc7/mode-b/tts-batch \
  -H 'Content-Type: application/json' \
  -d '{"engine":"cosyvoice_api","engine_url":"http://127.0.0.1:50000/tts","rate":1.25}'
```
**不要传 speaker 参数**——payload 构建自动读每句的 speaker_id→角色音色（rate 1.25 已实测验证：11/12 超窗句改善）。
预计 2791 句新建，~6h 跑完（吞吐 2.7-3 句/分，瓶颈是节点代理 SSL 抖动，见 §六）。

### 3. 监控（workbench exec 有 30s 限制，长任务用 systemd-run）
```bash
~/bin/workbench exec --instance-id i-bp1d8pq7f99dzjc7tiwl --command \
  "/opt/peiyin/controlplane/.venv/bin/python /tmp/dub_now2.py"
# 脚本输出：批量各状态计数 / clips 数 / artifact 数 / 节点心跳
```

### 4. 跑完出交付包 + 试听包
```bash
# 打包（>30s，必须 systemd-run）
systemd-run --unit=peiyin-pkg --collect bash -c 'curl -s -X POST \
  http://127.0.0.1:8500/api/projects/133d1fea8be24d9c940c8421abfe6cc7/mode-b/package-from-clips \
  -H "Content-Type: application/json" -d "{}" > /tmp/pkg.json 2>&1'
# 25s 后 cat /tmp/pkg.json → 下载 /api/projects/{pid}/mode-b/download
# 多角色试听包：挑各主角的 uid 建 tts-batch({"uids":[...]}) → 完成后产物在
# MODE_B_STORAGE/artifacts/{task_id}/ → zip 后放 MODE_B_STORAGE/133d1fea/xxx.zip
# → 经 /api/projects/{pid}/mode-b/file/xxx.zip 下载（文件名白名单端点）
```

---

## 三、这波落地的能力（全在 upgrade/seam-wave1 分支）

### 控制面新端点（均有测试，52 全绿）
| 端点 | 用途 | 关键语义 |
|------|------|---------|
| `POST /projects/{pid}/mode-b/tts-batch` | 批量单句TTS | input_hash=uid+译文版本+engine+instruct+ref+voice_id+rate+emotion 幂等；`uids`精选重跑；`scene`/`limit`；**自动跳过标记行/占位行/无译文行** |
| `POST /projects/{pid}/mode-b/package-from-clips` | 真TTS交付包 | 从 tts_clips 构建；fit_clip **联合语速上限 tts_rate×atempo≤1.5**（超窗如实标记防chipmunk）；manifest 带 total_speed；字幕=源slot时间码 |
| `POST /projects/{pid}/mode-b/tts-requeue` | 补传 | **默认跳过 dead**（冻结队列保护），include_dead=true 才包含 |
| `POST /projects/{pid}/bind-speakers` | LLM台词绑定 | cast_agent.bind_speakers，幂等（已绑定跳过，force覆盖） |
| `GET  /projects/{pid}/mode-b/file/{name}` | 工作文件下载 | 文件名白名单防穿越（A/B试听包等） |
| `POST /nodes/tasks/{id}/artifact` | 节点产物回传 | raw-body 免multipart；tts-generate 自动落 tts_clips |
| `POST /projects/{pid}/mode-b/tts-task` | 单句测试 | payload 现含 ref_audio_b64/voice_id/instruct |

### 修复的存量 bug（都已带回归测试）
1. **complete 数据丢失**：outputs 列表整行赋值→qc钩子覆盖成 `{"qc"}` → 改结构化合并+payload保留
2. **tts_clips 挂错版本**：取最新译文行没跳占位符 → manifest 出现 `[MISSING 12]` 配正确音频 → 跳占位符取数
3. **场景标记行**：`【第 N 段】` 60 条被翻译+配音+进SRT → `is_marker()` 全链过滤（tts-batch/package）
4. **HTML 标签**：译文 `<b>` 残留进引擎/字幕 → TTS payload 与 manifest 双侧剥除
5. **task_key 同秒碰撞** → 随机后缀
6. **缓存键吞参数** → rate/emotion/voice_id 全进 input_hash
7. **register 不建档** → register 即建 GpuNode 行+同名旧行置 offline
8. **save_translation**：原地改v1 → version+1+llm_model=human；钩子类型 `tts_generate`→`tts-generate`
9. **G1/G2/G3/G5 翻译链衔接**：角色卡全维注入 / 跨场景上下文取最新版本 / 句特征路由 / 压缩产物补终检

### 翻译链 & 音色引擎
- `voice_assign.py`：三级分配（簇参考音→voice_assets 标签×2+**timbre关键词**评分→项目默认）
- 节点 `entrypoint.py`：token持久化（workdir/node_token.txt）+ artifact回传 + **httpx keep-alive**
- 节点 `stages/tts_node.py`：instruct/rate 透传 + ref_audio_b64 落地缓存（**待用户更新**）

---

## 四、实测事实（白月光，别重新踩）

- **绑定结果**：林惜音 720 / 楚弋 630 / 盛君霆 388 / 王楚熙 206 / 叶娇娇 165 / 楚母 137 …共 29 角色
- **主角音色分配（已实测正确）**：

| 角色 | 音色资产 | edge-tts 声线 |
|------|---------|--------------|
| 林惜音(female/young) | va_female_young | zh-CN-XiaoyiNeural 甜美活泼 |
| 楚弋(male/young) | va_male_young | zh-CN-YunjianNeural 热血青年 |
| 盛君霆(male/young) | va_male_mature | zh-CN-YunyangNeural 低沉威严（timbre关键词区分） |
| 王楚熙(female/young) | va_female_sharp | zh-CN-XiaoniNeural 尖锐冷艳 |
| 楚母(female/middle) | va_female_warm | zh-CN-XiaoxiaoNeural 温暖知性 |
| 群众/unknown | 引擎默认音色 | — |

- **吞吐**：keep-alive 后 2.7-3 句/分（合成延迟中位 3.0s 健康；开销全在节点代理 SSL 抖动，每句 3 次请求：claim/complete/artifact）
- **超窗**：rate=1.25 后 ~40%（107/259）；默认音色语速慢于 6音节/秒 预算是根因之一；**>1.3 的属结构性**（源字幕槽位 0.7s 级别），建议压缩重译消化
- **rate=1.25 实验**：11/12 改善，1 句引擎随机性变差（CosyVoice 温度）
- **edge-tts 注意**：ECS 直连可用；`Communicate(text, voice)` 第二参数必须拆包（踩过两次）；zh-CN-XiaomoNeural 从 ECS 不可用（No audio received）

---

## 五、运维手册（本波新增部分）

```bash
# 云端部署（与 HANDOVER §五 相同）
git archive HEAD | gzip > /tmp/peiyin.tar.gz
~/bin/workbench upload /tmp/peiyin.tar.gz /tmp/ --instance-id i-bp1d8pq7f99dzjc7tiwl --force
~/bin/workbench exec --instance-id i-bp1d8pq7f99dzjc7tiwl \
  --command "cd /opt/peiyin && tar xzf /tmp/peiyin.tar.gz && systemctl restart peiyin && curl -s http://127.0.0.1:8500/health"

# 独立脚本跑 LLM/DB 操作必须注入服务环境（否则 ENCRYPTION_KEY missing 解不了 provider key）
eval export $(systemctl show peiyin -p Environment --value)

# ECS 上现成的工具脚本（/tmp 下，重启即失，可从 git 历史恢复）
#   dub_now2.py 批量状态总览 | dub_rate.py 延迟分布 | make_ab.py A/B试听包
#   gen_voices*.py + reg_voices2.py 音色生成注册 | merge_aliases.py 角色归并
```

- **音色管理**：voice_assets 表（tags JSON + tts_params.desc 关键词）；生成脚本模式：edge-tts mp3 → ffmpeg 22.05k mono wav → 入库（`/tmp/peiyin-mode-b/voices/*.wav`，注意 /tmp 重启即失，**建议搬进 R2 或 /opt 持久化**）
- **改音色/加音色**：生成 wav → voice_assets 加行（tags=[gender,age_band] + tts_params.desc）→ 直接发 batch（voice_id 变化 → input_hash 变化 → 自动重新合成对应角色）

---

## 六、已知问题与下一步（按优先级）

| # | 项 | 说明 |
|---|-----|------|
| 1 | 节点更新 + 全剧批量 | 见 §二，用户的 3060 在手 |
| 2 | 吞吐优化 | 节点代理 SSL 抖动吃掉 ~15s/句。方案：claim 协议批量返回（一次领 N 句，节点本地排队）可省 2/3 往返；或用户换稳定网络出口 |
| 3 | 压缩重译通道 | 超窗 >1.3 的句子（结构性短槽位）需要"只跑压缩闭环"的入口端点（run_translate_scene 压缩段已具备，缺独立触发） |
| 4 | diarize 声纹聚类 | DESIGN-B-配音方案-v2 Step1：3060 装 pyannote → 每句声纹→簇 → 替换 LLM文本绑定的结果 + 每角色真克隆参考音（从原配音轨提纯）。bind_speakers 的产出可作聚类初始化 |
| 5 | 音色文件持久化 | /tmp/peiyin-mode-b/voices/ 重启即失 → 搬 R2（r2.py 双模式现成） |
| 6 | 云端 API_TOKEN 为空 | 隧道是唯一闸门（P2 已知项），主人拍板后补 |
| 7 | 网站台词表编辑 | 已接真API（version+1+重TTS钩子验证过），可加行内试听按钮 |

---

## 七、快速自检（接手第一遍）

```bash
cd ~/duanju/dubbing-system && git checkout upgrade/seam-wave1 && git pull
cd controlplane && .venv/bin/python -m pytest tests/ -q        # 52 passed
curl -s https://dubbing.mulan.dpdns.org/health                  # {"status":"ok"}
~/bin/workbench exec --instance-id i-bp1d8pq7f99dzjc7tiwl \
  --command "/opt/peiyin/controlplane/.venv/bin/python /tmp/dub_now2.py"   # 节点心跳新鲜
curl -s https://dubbing.mulan.dpdns.org/api/projects | head -c 200        # 只剩白月光
```

全绿后从 §二 第 1 步开始干活。
