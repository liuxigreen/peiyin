# 短剧出海译配平台 · 设计定稿 V2.0（云端形态）

> 综合来源：4份原稿架构能力 + 我的云端化重设计。2026-08-27 定稿。
> **产品形态一句话**：控制面跑在云端VPS，全员任何电脑浏览器打开网页使用；本地3060和未来租的显卡都是「算力接入端」，一条命令加入算力池。

---

## 1. 我的五条核心设计决策

### 决策1：控制面上云，计算面自服务接入
- 控制面（API+数据库+网页）放云端固定地址，办公网变动、在家办公都不影响
- 任何一台带N卡的机器（今天3060，明天租的4090）装一个 `gpunode` 镜像，`join` 一条命令入池自动领任务
- 租云卡 = 实例启动脚本拉同一镜像 join，用完关机。控制面永不改动

### 决策2：去Celery化 —— Postgres队列 + REST拉取协议（我的重构）
原稿用Celery+Redis过互联网连Worker是隐患（broker暴露公网=大洞）。我的方案：
- 任务队列直接用 **Postgres `SELECT ... FOR UPDATE SKIP LOCKED`**（原子领取，经典可靠）
- GPU节点经HTTPS调控制面REST接口领任务/交产物/心跳，**Redis/DB永不出内网**
- CPU/IO类任务作为控制面进程内的asyncio执行池，省掉一类worker
- 失效lease机制：领取后10分钟无心跳→reaper线程自动回收重新入队
好处：部署组件少一半、公网攻击面只剩一个HTTPS端口、断线恢复天然成立

### 决策3：视频文件不过VPS —— 浏览器直传对象存储
控制面只签发预签名URL，母片从浏览器**直传**云对象存储（分片续传），成品从对象存储**直下**浏览器。小VPS的1核带宽不做搬运工。

### 决策4：对象存储选 Cloudflare R2（零流出费）
已有Cloudflare体系。R2免流出费对"母片反复下载+成片反复下载+GPU节点拉切片"的场景是数量级优势；S3兼容API。备选：腾讯COS/阿里OSS（同代码兼容，换endpoint即可）。**容量账**：免费档仅10GB，肯定不够；超量$0.015/GB·月(≈¥0.11)，月产20部剧稳态存量约100GB→约¥11/月，依然可忽略。真正的钱在流出费——R2永远免费（对照：OSS/COS一个月下载数TB视频的流量费是大头）。配生命周期策略：项目完成后90天清中间产物、只留成片+字幕。

### 决策5：资产中心沉淀复用
音色库/术语库/Prompt模板跨剧复用：这部戏调好的霸总音色下部戏一键绑定；prompt模板版本化管理，效果好的设为默认。系统越用越快。

---

## 2. 对4份原稿的关键取舍（定案）

| 议题 | 定案 | 理由 |
|------|------|------|
| 部署形态 | **云端控制面+弹性GPU节点** | 用户拍板；原有单机方案废弃 |
| 英语TTS | CosyVoice2-0.5B | 已拍板；中文参考→英语输出链路通；4-6G显存适配3060档 |
| 中文ASR | FunASR Paraformer-zh | 准确率最高+热词+词级时间戳；⚠️长音频显存暴涨至14G+(issue#728实测)→强制≤60s块 |
| 音源分离 | Demucs htdemucs_ft 2-stem | 人声质量优先 |
| 字幕擦除 | fast=OpenCV定位+FFmpeg羽化(boxblur15:3 α0.55,实测参数)；quality=PaddleOCR+ProPainter开关 | 先通再精 |
| 任务队列 | Postgres SKIP LOCKED（替代Celery+Redis） | 公网安全+少组件 |
| 情绪提取 | V1跳过，参考音频含情绪中性约束补偿 | 二期emotion2vec |

V1锁定约束：中文→英语；角色2~10；翻译API网页可配；失败切片单独重试；预分析后不停等审批。

---

## 3. V1 验收标准

1. 任意电脑浏览器访问 `https://dubbing.opspilot.me` 登录后可用全部功能
2. 上传90min中文母片（浏览器直传进度可见）→ 自动产出英语配音1080p MP4+SRT/ASS（下载直链）
3. 3部剧实战端到端成功率>95%
4. 角色2~10个，同角色全片音色一致（首中尾抽听）
5. 失败切片一键重试，只重算该切片（缓存命中其余）
6. GPU节点断电重启→任务自动回到队列被重新领取；kill运行中进程不丢状态
7. 网页增删改翻译API+测连通
8. 译文音节比100%<1.15x
9. 成片-16LUFS±1、无>3s意外静音、无拼接爆音（QC脚本自动出报告）
10. 3060显存峰值<11G；90min片源耗时≤6h（4090预期<2.5h）

---

## 4. 云端部署拓扑

```
┌─ 云端 ────────────────────────────────────────────┐     ┌─ 对象存储 Cloudflare R2 ──┐
│ 轻量VPS（新开，2核4G，约¥60/月）                     │     │ bucket: dubbing           │
│  ├ Caddy :443  HTTPS+dubbing.opspilot.me+basicauth │     │  uploads/{pid}/source.mp4 │
│  │             （TLS自动，替代自建证书）              │◄───►│  projects/{pid}/…         │
│  ├ controlplane容器                                 │签名  │  output/{pid}/final.mp4   │
│  │   FastAPI :8500（唯一公网入口，REST only）        │URL  └───────────────────────────┘
│  │   内嵌cpu/io异步执行池 + lease-reaper线程          │                ▲
│  ├ Postgres16容器（仅内网，队列表=真理之源）          │                │ 预签名GET/PUT
│  └ Redis7容器（可选：限流/缓存，非队列）              │                │
└────────────────────────────────────────────────────┘     ┌─ GPU节点 ×N（ anywhere ）─┐
                                                            │ gpunode镜像 join:          │
┌─ 办公电脑们 ─┐                                            │  POST /nodes/register      │
│ 浏览器 ──────┼──HTTPS──► 控制面                            │  循环: long-poll领任务      │
│ 直传/直下 ◄──┼──预签名URL──► R2                            │  (模型串行加载执行)         │
└──────────────┘                                            │  心跳+产物流式传R2          │
                                                            └────────────────────────────┘
```

**为什么新开一台而不是塞进现有VPS**：43.134只有1核1.9G，已跑panel/gateway/9router四件套，再加Postgres有OOM风险；控制面独立后现有业务零风险，将来扩容互不影响。（备选：现有VPS升配2核4G省一台钱，但需停机迁移+共享故障域，列为次选。）

### 安全模型（公网三道闸）
1. Caddy basic_auth（第一层门，运营同学账号密码）
2. 应用内环境变量 `API_TOKEN`（服务间/GPU节点调用头 `Authorization: Bearer`）
3. 全链路HTTPS+CORS白名单；Redis/Postgres绑定内网不出容器网络
   密码学基元统一 cryptography 库（provider key AES-GCM 落库加密）

---

## 5. GPU节点接入协议（gpunode ↔ 控制面）

```
注册   POST /api/nodes/register  {name,gpu_model,vram_gb,capabilities[],node_token}
       → {node_id}  控制面记 last_heartbeat
领任务 GET  /api/nodes/me/claim?capabilities=gpu&models=cosyvoice2,funasr...
       → 任务{task_id,type,input_manifest[{key,presigned_get}]} 或 204空轮询(long-poll≤25s)
心跳   POST /api/tasks/{id}/heartbeat  每60s；控制面lease=10min，超时未续→回收重派
完成   POST /api/tasks/{id}/complete  {outputs:[{key,size,sha256}]}（节点先PUT完R2再报）
失败   POST /api/tasks/{id}/fail  {error,retryable}
摘除   心跳缺失15分钟→节点标记offline；配置里的vip节点转手其队列任务
```
Postgres领取原子性（控制面内部执行）：
```sql
UPDATE pipeline_tasks SET status='running',claimed_by=$node,lease_until=now()+interval'10 min'
WHERE id = (
  SELECT id FROM pipeline_tasks
  WHERE status='pending' AND ready_deps_ok
    AND ($model IS NULL OR model_name=$model)
  ORDER BY priority DESC, created_at
  FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
```

---

## 6. 数据模型（9张表，v2增补加粗）

```
projects(id,name,source_lang DEF zh,target_lang DEF en,status,source_r2_key,
         duration_ms,total_segments,total_utterances,config JSONB,…)
segments(id,project_id,seg_index,start_ms,end_ms,overlap_ms,cut_type,status)
speakers(id,project_id,label,role_name,is_primary,
         ref_audio_pool JSONB[{r2_key,dur,snr,is_primary}],utterance_count)
utterances(id,project_id,segment_id,uid,seq_index,start_ms,end_ms,
           original_text,asr_text,asr_conf,ocr_text,ocr_conf,
           merged_text,merged_conf,speaker_id FK,emotion_label DEF neutral,
           speaking_rate,char_count)
translations(id,utterance_id,target_lang,version,text,syllable_count,ratio,
             is_over_limit,llm_model,prompt_version,is_approved)
tts_clips(id,utterance_id,target_lang,translation_id,version,audio_r2_key,
          duration_ms,tts_engine,model_snapshot,prosody_rate,
          is_time_stretched,utmos_score,status)
pipeline_tasks(id,project_id,segment_id,task_type,target_lang,
               gpu_required,model_name,priority INT DEF 50,
               input_hash,output_hash,output_paths JSONB,
               status(pending/running/completed/failed/dead),
               **claimed_by FK→gpu_nodes**, **lease_until TIMESTAMPTZ**,
               **heartbeat_at**, error_message,retry_count,max_retries 3)
               INDEX(partial):status='completed'+input_hash / status='pending'+priority
**gpu_nodes(id,name,gpu_model,vram_gb,capabilities TEXT[],status,
            online BOOLEAN,last_heartbeat,node_token_hash)**
translation_providers(name,provider_type,api_base_url,api_key_encrypted,
            api_key_masked,model_name,temperature,max_tokens,system_prompt,
            prompt_template,monthly_budget,is_default,is_enabled,priority)
voice_assets / glossary_terms / prompt_templates（资产三表，同v1稿）
```

---

## 7. Pipeline（阶段设计与v1稿一致，此处列定稿参数速查）

Phase0 预分析：ffprobe探针(VFR→CFR/HDR→SDR)→16k mono音频+1fps关键帧→SileroVAD+PySceneDetect(0.3)并行→pyannote3.1(>2h分段30min+embedding cosine<0.25合并, num_speakers∈[2,10])→每说话人SNR top5参考段(情绪中性优先,3~15s)→智能分片(静音>800ms∪场景点,3~5min/片,500ms overlap)
Phase1 字幕：软字幕剥轨；硬字幕fast羽化遮挡（模糊区仅比字幕带上下多5px）；quality=PaddleOCR底部25%+ProPainter仅修有字帧+median3帧
Phase2 分离：Demucs htdemucs_ft 2-stem（soundfile读写）；Resemble Enhance可选出vocals_clean
Phase3 识别：FunASR句级切块≤60s+热词｜PaddleOCR rec 1fps底部25%连续≥3帧聚合｜合并edit_distance<0.2加权否则CONFLICT(conf .5)
Phase4 翻译：<2s场景分组批10~25句带前文3句+role_card+术语强替换；并发10-16退避30/60/90×5；chunk拆半重试+敏感词脱敏回填；音节校验zh字数/en cmudict→syllapy超1.15两轮压仍超标红
Phase5 TTS：CosyVoice2-0.5B zero_shot(en_text,zh_prompt,ref16k)；时长三级匹配(ratio≥0.9&≤1.1过/[0.8,1.2]WSOLA/外prosody_rate重生)；UTMOS<3.0换seed×3标人工
Phase6 混音：Python预拼vocal_track(防open files)>ducking语音区-10dB,SFX时刻(onset)-4dB,atk30ms rel150ms>loudnorm -16LUFS/-1dBTP>爆音±20ms插值修复
Phase7 缝合：concat -c copy帧级拼接,音频20ms equal-power crossfade>H264 CRF18+AAC192k+faststart>ASS烧录(字号帧高5~6%,置于原字幕带中心)>QC(LUFS sd<1.5/意外静音0/抽检偏移<100ms)

缓存与增量：input_hash内容寻址命中跳过；改1句译文→该句TTS+所在切片混音编码；换音色→该角色全部句。
断点：所有状态=pipeline_tasks行级事实；进程死→lease过期回收；节点失联→任务换节点续跑。

---

## 8. Web面板（6页面，React18+TS+Vite构建产物由控制面静态托管）

```
/                    项目列表（卡片:名称/状态徽标/进度条/角色数/语种/时间/下载）
/projects/new        新建项目：拖拽上传母片（浏览器→R2直传,分片+进度+断点续传）+命名+目标语种
/projects/:id        流水线总览：8阶段stepper(预分析→擦除→分离→识别→翻译→TTS→混音→缝合)
                     切片表格(状态徽标/耗时/失败原因/一级重试按钮)+角色卡片区(改名/试听/绑音色库)
/projects/:id/text   台词对照表虚拟滚动300+行：原文|ASR|OCR|译文可编辑|音节比|置信度
                     CONFLICT红底/conf<0.7黄/超限橙红；过滤切换；保存触发单句重算
/settings/providers  翻译服务商CRUD+key打码+测连通+设默认
/assets              三Tab资产中心：音色库(卡网格)/术语库(表)/Prompt模板(版本+默认)
布局：左侧深色侧栏(译配平台logo+菜单)，顶栏面包屑；暗色克制风(Stripe dashboard感)
```

---

## 9. 目录结构

```
~/duanju/dubbing-system/
├── DESIGN.md                      本文档（唯一施工依据）
├── controlplane/
│   ├── app/
│   │   ├── main.py                FastAPI入口+静态托管
│   │   ├── api/                   routes: projects/nodes/tasks/providers/assets/upload(签发)
│   │   ├── workers/celery_app.py  [已弃用路径] 改executor.py 异步执行池+reaper
│   │   ├── db/schema.sql          9张表
│   │   └── core/                  config.py auth.py crypto.py r2.py(签名)
│   ├── requirements.txt
│   └── web/dist → frontend/dist   构建同步
├── gpunode/
│   ├── entrypoint.py              register+claim循环+heartbeat线程
│   ├── models/ model_manager.py cosyvoice.py funasr.py demucs.py pyannote.py vad.py ocr.py
│   ├── stages/ subtitle.py separate.py recognize.py translate_io.py tts.py mix.py stitch.py qc.py
│   ├── Dockerfile                 nvidia/cuda12.1基础+torch+全模型权重下载层
│   └── join.sh                    docker run --gpus all … 注册示例
├── frontend/                      React18+TS+Vite（bun管理）
└── deploy/
    ├── docker-compose.yml         caddy+controlplane+postgres(+redis可选)
    ├── Dockerfile.control
    └── Caddyfile                  dubbing.opspilot.me basicauth → api:8500
```

---

## 10. 实施路线图（每步验收）

| 步骤 | 内容 | 验收 |
|------|------|------|
| F1 | 前端6页面+mock层（进行中，子任务） | bun run build零错；preview可看全部页面 |
| C1 | 开通R2桶+dubbing子域DNS+(可选)新轻量VPS | mc/rclone能列桶；域名解析生效 |
| B1 | 控制面：9表迁移+projects/providers/assets CRUD+签发URL接口 | uv测试客户端全绿；swagger可操作 |
| B2 | 节点协议：register/claim/heartbeat/complete/fail+lease回收器 | 双终端模拟：节点领到任务;杀进程任务10min回队列 |
| G0 | GPU节点镜像在3060机器起：CUDA驱动验证+torch可用 | docker run --gpus all nvidia-smi正常 |
| P0-P7 | 按§7逐阶段实现并接真流程 | 每 Phase 有独立样片单阶段验收脚本 |
| E2E | 断点续传演练+3部剧实战 | §3十条全过 |

前置清单：pyannote HF token(gated);CosyVoice2权重~5G预下;HF_ENDPOINT=https://hf-mirror.com;R2 access key;dubbing子域DNS记录;轻量VPS的SSH别名写入config

---

## 11. 成本模型（月度，V1规模）

| 项 | 方案A推荐 | 说明 |
|----|----------|------|
| 控制面VPS | 腾讯轻量2c4G ≈¥60 | 独占干净环境 |
| 对象存储 | R2 存量约¥10-15/月 | $0.015/GB·月,免费档10G不够用;零流出费是大头优势;生命周期90天清中间产物 |
| GPU | 现有3060闲置算力≈0 | 高峰期租4090 ¥2-3/h按需 |
| 翻译API | DeepSeek档 ≈¥1-3/部 | 千万token内够用 |
合计 ≈ ¥65/月 固定 + 按需弹性

## 12. 升级路线（换更好模型/租更好卡时）

1. 加卡：gpunode join 自动入池，队列自动分配；4090档batch翻倍吞吐升
2. 扩语种：TTS路由 日韩→VoxCPM2/东南亚西葡→FishSpeech/Kokoro兜底
3. 主力角色精修：GPT-SoVITS微调(+RVC消口音)，微调权重挂R2随节点缓存
4. 翻译免费档：Qwen72B-AWQ vLLM上24G节点进provider列表
5. 质量闭环：emotion2vec→instruct式TTS情绪;UTMOS全量回归周报

---

*本档为唯一施工依据。实测与本档冲突→改档记日期，文档=现实。*
