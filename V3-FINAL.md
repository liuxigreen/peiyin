# dubbing-system V3 最终方案（综合定稿）

> 2026-08-28 定稿。综合 TG 会话产出的三件套（DESIGN v2 / ORCHESTRATION v3 / PIPELINE-DETAILS）
> 与已实现代码，吸收用户新决策。**三件套继续有效，冲突处以本文为准。**
> 目标一句话：中文短剧母片 → 任意目标语种高质量配音成片，网页点一下，GPU 按小时租。

---

## 0. 用户新拍板（对 V2 的修正）

| # | 决策 | 影响 |
|---|------|------|
| 1 | **多语言是目标**，英语只是第一个语种 | TTS/音节/翻译/字幕全链按语种路由（§2） |
| 2 | GPU 租 compshare 4090（华北一C，¥1.94/h，**50G 系统盘**） | 磁盘预算重算 + 切片流式（§3） |
| 3 | 面板先跑 Mac，后期迁 VPS | 现在用 sqlite；迁移时换 PG 连接串，代码零改动（§4） |
| 4 | 对象存储后置 | r2.py 已有 mock 双模式：无凭证=本地盘，填 key 即切真桶（§4） |
| 5 | 翻译 API 全部网页配置 | providers 表+CRUD+加密已实现；executor 从 DB 读，环境里零硬编码 key |

---

## 1. 多语言架构（V3 核心新增）

### 1.1 语种路由表（TTS 按 target_lang 选引擎）

| 语种 | 首选 | 备选 | 理由 |
|------|------|------|------|
| en | CosyVoice2-0.5B zero-shot（中文 ref → 英文输出） | GPT-SoVITS 微调 | 已定案；4-6G 显存 |
| id/th/vi/ms | Confucius4-TTS | VoxCPM2 | 14 语种在线 API，跨语种无口音，3 秒克隆 |
| ja/ko | VoxCPM2 | Confucius4-TTS | 30 语种，Voice Design+情绪控制 |
| es/pt | Confucius4-TTS | FishSpeech/Kokoro | 同东南亚策略 |

- 实现形态：gpunode 内一张 `LANG_TTS_ROUTES` 注册表，任务带 target_lang 自动路由；模型权重懒加载（领到该语种任务才下载）。
- 音色一致性：同一说话人全语种共用中文参考音频（三家均支持跨语种克隆）。降级链：跨语种口音不可接受 → RVC 拉回 timbre（二期）→ GPT-SoVITS 按角色微调。
- voice_assets 表 `tts_params` JSON 已预留每语种参数位。

### 1.2 音节/时长约束按语种

- 音节估算器按语种注入：en=cmudict/syllapy；id/es/pt=元音组计数；ja=假名 mora 计数；ko=音节块计数。
- ratio 上限默认 1.15，`projects.config.syllable_limit` 可按项目/语种覆盖。
- 超限三级处理不变：LLM 压缩重写 → 规则裁剪（虚词优先删）→ 标红人工；Web 对照表 >1.05 橙、>1.15 红。

### 1.3 翻译按语种

- prompt_templates 表已按 `(target_lang, drama_genre)` 建模，五步链每步可挂不同模板。
- glossary_terms 已按 `(source_term, target_lang)` 唯一约束，术语强制替换与语种解耦。
- 场景分组批翻译（600字符/批、前文3句、role_card 注入）与目标语种无关，五步链通用。

### 1.4 识别与字幕（与目标语种无关）

- ASR/OCR 只处理源语（zh），多语言零改动。
- ASS 字幕字体按语种 fallback 链（Noto Sans SC/JP/KR…），字号规范不变（帧高 5~6%）。

---

## 2. 50G 系统盘预算（compshare 4090，替代 100G 旧账）

| 项 | 占用 |
|----|------|
| 系统 + CUDA + Torch 运行时 | ~14G |
| 模型权重（按语种懒加载，en 全套 ~8G） | ~8G |
| pip/conda/Docker 缓存 | ~10G |
| **小计** | **~32G → 剩 ~18G 工作区** |

**切片流式策略**：节点磁盘只保留当前 claim 的 ≤2 个切片工作集（单切片峰值 ~2G：视频+16k音频+Demucs双轨）。T340 编码完成 → 中间产物即刻传对象存储 → 本地删除。单卡串行下峰值驻留 ≤4G，余量充足。旧方案"全剧 30G 中间产物驻留盘"作废。

权重完整备份仍放对象存储（~¥0.9/月），实例释放后 5 分钟热恢复。

---

## 3. 部署分阶段（面板先 Mac，后迁 VPS）

| 阶段 | 控制面 | 数据库 | 存储 | 说明 |
|------|--------|--------|------|------|
| 现在 | Mac uvicorn :8500 | sqlite (dev.db) | r2.py mock=本地盘 | 全功能可测，零外部依赖 |
| 中期 | Mac 同上 | sqlite | R2 真桶（.env 填三件套） | 桶已建，直传直下立即可用 |
| 后期 | VPS docker-compose（caddy+controlplane+postgres） | Postgres | R2 | 文件全在 deploy/，rsync+up 即迁移 |

- SQLAlchemy 模型两库通用；sqlite→PG 只换连接串。compose/Caddyfile 已备好。
- **红线**：迁 VPS、绑 dubbing 子域属于改动线上服务，执行前单独找用户确认。
- 安全模型不变：Caddy basicauth → API_TOKEN → HTTPS，key AES-GCM（Fernet）落库加密。

---

## 4. 配音全链路细节定稿（摘要 + V3 修正）

全参数看 PIPELINE-DETAILS.md（Phase0-7 全部有效），此处列定稿要点与修正：

1. **翻译 = 五步链**（取代旧"四步流水线"表述，方法论同源）：
   `T205 上下文包(术语表+前情3句+role_card) → T211 直译 → T212 反思意译(含配音约束) → T213 跨批一致性终检 → T214 台词落库(版本化) → T220 音节校验`
   本机 4 个 drama skill 的 prompt 作为蓝本入库（prompt_templates）。
2. **Provider 策略**：translation_providers 表 priority 排序取 enabled 的默认项；429 退避 30/60/90s×5；chunk 失败拆半重试；敏感拒答脱敏回填；换 provider = T211 缓存键含 provider_id，自动波及重翻。
3. **TTS 三要素**（参考音频 3-15s 情绪中性 SNR 最高段 / 参考文本逐字对应 / speed 因子+读两次择优）不变；时长三级火箭（直过→WSOLA→prosody_rate 重生→标红）不变；UTMOS<3.0 换 seed×3 仍低标人工不变。
4. **混音缝合**：Python 预拼防 open-files、ducking 语音区 -10dB/SFX -4dB、loudnorm -16LUFS、concat+20ms crossfade、ASS 置原字幕带中心。QC 三查（LUFS 方差<1.5 / 意外静音=0 / 抽检偏移<100ms）。
5. **多语言增补**：音节估算按语种（§1.2）；TTS 引擎按语种路由（§1.1）；其余全链语种无关。

---

## 5. 已实现状态（代码事实，全部有测试证据）

| 模块 | 状态 | 证据 |
|------|------|------|
| 编排器 O1-O6（DAG/幂等/reaper/缓存/retry/进度） | ✅ 完工 | pytest 8 passed + E2E 48 任务 100% |
| 五步翻译链入 DAG（T205-T220） | ✅ 完工 | 同上，translate 阶段 11/11 |
| 9 张表模型 + Fernet key 加密 | ✅ | models.py / crypto.py |
| providers CRUD API + 网页 | ✅ | api/providers.py + Providers.tsx |
| R2 预签名（mock/真桶双模式） | ✅ | core/r2.py |
| 前端 6 页面（React18+TS+Vite） | ✅ 已构建部署 | :8500 |
| 节点协议 register/claim/heartbeat/complete/fail | ✅ | api/nodes.py |
| **翻译真 executor** | ⬅️ **正在做** | 本次交付 |

---

## 6. 下一步（按序）

1. **翻译 executor**（本文件定稿后立即实现）：controlplane 内置 io 执行池，从 DB 读 default provider → 跑五步链 → 产物落 translations 表 + 对象存储；mock provider 单测 E2E。
2. 网页填真 key → 单场景真实 API 烟测（翻译质量人审）。
3. 音色/术语/Prompt 资产三表的前端接真（Assets 页已有壳）。
4. （GPU 最后）compshare 4090 join → T110/T120/T130/T310 逐阶段点亮，50G 盘按 §2 流式策略。
5. E2E 断点续传演练 + 3 部剧实战，按 DESIGN §3 十条验收。


---

## 7. 缺口盘点与补全（2026-08-28 第二轮：对照"上传→直接出成片"目标）

### 7.1 现状 vs 目标（诚实盘点）

| 环节 | 状态 | 缺什么 |
|------|------|--------|
| 项目/任务DAG/断点续传/重试/进度 | ✅ 已实现 | — |
| 五步翻译链（mock/live） | ✅ 已实现 | 用户网页填真 key 后烟测 |
| Provider 网页配置+加密 | ✅ 已实现 | — |
| **母片上传→自动起流程** | ⚠️ 半成品 | presign 已有；缺：上传完成回调→probe→自动 instantiate-dag 的引导任务 |
| **字幕擦除(T110)** | ❌ 未实现 | OpenCV定位+羽化（fast）=控制面CPU池可跑；ProPainter(quality)→GPU |
| **人声分离(T120)** | ❌ 未实现 | Demucs（GPU节点，权重0.4G） |
| **ASR/OCR/对齐(T130/140/150)** | ❌ 未实现 | FunASR(GPU)+PaddleOCR(CPU池)+合并 |
| **角色/音色(T040/050)** | ❌ 未实现 | pyannote diarize→参考音频池→voice_assets |
| **TTS(T310)** | ❌ 未实现 | CosyVoice2(en)/Confucius4(id等)（GPU节点） |
| **时长匹配/混音/编码(T320-340)** | ❌ 未实现 | WSOLA+预拼ducking+loudnorm（CPU池，ffmpeg） |
| **缝合/字幕烧录/QC(T410-430)** | ❌ 未实现 | concat+crossfade+ASS烧录+LUFS三查 |
| **成片交付(T440)** | ❌ 未实现 | R2直下链接+项目完成态 |
| 台词对照表Web编辑 | ✅ 后端已有 | 前端接save_translation+over_limit红橙标 |

### 7.2 CPU 问题定案（回应"租显卡应该没有CPU"）

**事实**：GPU租赁实例自带CPU——compshare/智星云4090实例配 Xeon 16核+60G内存级配置（时租费已含）。真正的约束是：**按量实例关机即停**，控制面不能依赖它常驻。

**架构定案（与DESIGN决策2一致，无需改）**：
- 控制面所在机器（现在Mac/后期VPS）的CPU池：跑 io/cpu 任务——翻译、SRT种子、字幕羽化(fast)、OCR、对齐、时长匹配、混音、编码、缝合、字幕烧录、QC。这些全部ffmpeg/Python实现，Mac M4完全够
- GPU节点CPU（实例自带16核）：只跑该节点任务的预处理/后处理（Demucs的音频IO、TTS前切片等），不为整机流程负责
- **Mac作为常驻CPU节点同时挂进节点池**：gpunode无GPU也能join（capabilities=["cpu"]），领cpu/io类任务——后续VPS同理。GPU实例只在跑片时段存在
- 由此：**任何时刻关掉GPU实例，流水线照常推进CPU/IO任务**；GPU任务等节点上线续跑（lease回收保证）

### 7.3 鬼手剪辑流程对照（对标商业闭环，确认我们没漏环节）

鬼手=擦除→翻译→配音→lipsync→合成一站式。对照结论：
1. 擦除：我们的T110对齐（fast羽化/quality ProPainter双模式）
2. 翻译：五步链已超鬼手经典档（它是单轮）
3. 配音：T310角色化+情绪参考（鬼手超真实档对应我们的voice_assets克隆）
4. **lipsync：V1明确不做**（PREVIEW: 跨语种口型本身错位，观众预期已接受；二期Wav2Lip/MuseTalk开关式加入T315槽位，DAG已可无痛插行）
5. 合成烧录：T410-430对齐
6. 成本对比：鬼手10分钟¥17-151 → 我们GPU成本≈¥1.6/部(50min片)+翻译API≈¥1-3 → **约10-50倍成本优势**，质量靠五步链+QC闭环拉齐

### 7.4 开源TTS选型定稿（多语言路由的落地权重表）

| 语种 | 引擎 | 显存 | 权重体积 | 落库到LANG_TTS_ROUTES |
|------|------|------|---------|----------------------|
| en | CosyVoice2-0.5B | 4-6G | 5G | ✅ 主力 |
| id/ms/th/vi/es/pt | Confucius4-TTS(API) | 0(在线) | 0 | ✅ 免GPU启动这些语种 |
| ja/ko | VoxCPM2(2B) | 8-10G | 4G | 二期（显存峰值预算内） |
| 旁白兜底 | Kokoro-82M | CPU可跑 | 0.3G | ✅ 零GPU兜底 |
| 精修 | GPT-SoVITS微调 | 4G+ | 按角色 | 按需 |

Confucius4在线API的战略意义：**id/es/pt等语种在租GPU之前就能全流程跑通**（TTS走API不占显存），GPU只为en/大语种租赁。

### 7.5 全自动引导链（补"上传→直接出片"的最后一环）

上传完成后自动起流程，零人工干预设计：
```
浏览器分片PUT→R2完成 → POST /api/projects/{pid}/upload-complete
  → 控制面创建T010种子任务(probe: ffprobe元数据)
  → T010完成→自动instantiate-dag(切片数/场景数由probe+VAD产出)
  → 编排器自动推进全链（翻译即跑, GPU任务等节点）
  → T440完成→project.status=completed→Web亮"可下载"
```
人工介入点仅两处（可配置跳过）：①对照表人工校对译文（可选，默认自动过）②角色名确认（可选）。

### 7.6 实施顺序（接着干，每步带验收）

| 步 | 内容 | 验收 |
|----|------|------|
| N1 | upload-complete端点+probe种子+自动instantiate | 上传SRT测试片→DAG自动出现→翻译自动完成 |
| N2 | 台词对照表前端接真（编辑/保存/红橙标/单句重跑按钮） | 浏览器改一句→version+1→重翻只影响该句 |
| N3 | CPU池实装：T410-430缝合烧录QC（纯ffmpeg，先用假音频联调） | 假音频→真成片MP4+ASS+SRT产出 |
| N4 | T110字幕羽化fast(CPU)实装 | 真视频切片→擦除后帧对比 |
| N5 | GPU阶段：租4090→join→Demucs+FunASR+CosyVoice2逐个点亮 | 第一节真配音音频 |
| N6 | E2E：真母片→自动出片+三查报告 | DESIGN §3十条验收 |

注：N3/N4在Mac CPU池即可完成——**租GPU之前，"上传→出带假配音的成片"全链已能跑**，这是G-GPU前最后的里程碑。

---

*本文档与三件套共同构成施工依据。实测冲突 → 改档记日期，文档=现实。*
