# 升级提案清单（UPGRADE-PROPOSALS）

> 2026-09-01。分支 `upgrade/seam-wave1`，测试 32→39 全绿。
> 排序按 影响力/成本。标 ✅ 的已在本分支实现并带测试；标 📋 的为后续批次提案。
> 依赖关系：提案 8/9 依赖翻译链现有接口（独立可合入）；提案 11 依赖 G0 GPU 实装；
> 提案 12 依赖提案 5（产物回传）与 B2 槽位参考音。

---

### 提案 1（✅ 已实现）：G2 跨场景上下文取最新版本
- 现状: `translate_executor.py` 原 prev_scenes_ctx 查询硬编码 `Translation.version == 1`
- 问题: 全量重跑后最新版本是 v4+，跨场景上下文静默失效——71 场景长剧的人称/称呼衔接直接断供
- 设计: 抽出 `_prev_scene_ctx(db, project, scene_key, target)`：查前一场景全部译文行，按 utterance 取最高版本、跳占位符，尾 8 句注入 R2/R3
- 改动: `controlplane/app/translate_executor.py`
- 验证: `test_prev_scene_ctx_uses_latest_version`；生产重跑场景链后 prev_scenes_ctx 恢复非空

### 提案 2（✅ 已实现）：G1 角色卡全维注入提示词
- 现状: `build_ctx_pack` role_cards 只有 {label, role_name}；`speakers.ref_audio_pool` 里 C0 已提取的 gender/age_band/timbre 闲置（cast_agent.py 落库处 vs translate_executor 消费处）
- 问题: R2 意译拿到的"角色"只是名字串，性别/年龄/音色人设全丢——语气与称呼一致性缺锚
- 设计: role_cards 注入 gender/age_band/timbre/is_primary；`_card_str()` 生成"主角/male/young/音色:低沉威严"式角色行喂 R2
- 改动: `translate_executor.py`（`_voice_meta`/`_card_str`/build_ctx_pack/run_translate_scene）
- 验证: `test_ctx_pack_role_cards_voice_meta`

### 提案 3（✅ 已实现）：G3 句特征路由
- 现状: R1→R2 只传文本行（`_run_chunk_chain` R2 user prompt），无每句特征
- 问题: "08:30" 被意译成 "half past eight"、人名句被意译、超短句被加语气词——三类高频事故无防线
- 设计: `_line_feats()` 按行打标（纯数字/时间行→保留格式转写；术语表命中→直接输出译名；≤4字→逐字保留力度），`_feature_block()` 注入 R2（含重试路径）
- 改动: `translate_executor.py`（`_NUM_ONLY_RE`/`_line_feats`/`_feature_block`，签名透传 gloss_keys）
- 验证: `test_line_feats_routing`（纯函数级）

### 提案 4（✅ 已实现）：G5 压缩产物补过终检
- 现状: `_compress_chunk` 压缩后直接 `_write_translation` 落 version+1，R3 终检被跳过
- 问题: 压缩是最激进的改写步骤，语义漂移无人把关——恰恰最需要终检的行没有终检
- 设计: 压缩轮记录 touched 行 → `_review_compressed()` 对这些行跑 R3 增量协议（只收修改行）；修正行 version+1 带 `|review` 标记；音节比恶化且超限的修正拒收；终检被滤则保留压缩结果（不算失败）
- 改动: `translate_executor.py`（`_compress_chunk` touched 参数 + `_review_compressed` + run_translate_scene 收尾）
- 验证: `test_compression_review_writes_version`（version+1、review 标记、文本落库）

### 提案 5（✅ 已实现）：G6 产物回传通道 + complete 数据丢失修复
- 现状: 节点 complete 后 output_paths 只存节点本地路径；且 `complete` 端点 `t.output_paths = body.get("outputs", {})` 收到的是**列表**，qc 钩子判定非 dict 后整行覆盖为 `{"qc": ...}`——节点完成路径的产物路径与 payload 每次被清空
- 问题: 控制面永远拿不到 wav，交付包音频断供；G6 之上还叠着一层存量数据丢失 bug
- 设计: ①complete 改为 `{"outputs": 列表}` 结构化合并 + payload 保留；②新端点 `POST /api/nodes/tasks/{id}/artifact`（raw body 流式落盘，免 multipart 依赖，80MB 上限），存 `MODE_B_STORAGE/artifacts/{task_id}/`，合并进 output_paths.artifacts；③tts-generate 任务回传自动落 tts_clips 行（utterance/translation/duration_ms/engine 齐全）；④节点 dispatch complete 成功后 `upload_artifacts()` 回传全部产物文件
- 改动: `controlplane/app/api/nodes.py`、`gpunode/entrypoint.py`
- 验证: `test_artifact_upload_and_tts_clip`（complete 保留 payload/outputs → artifact 落盘 → tts_clips 落库 → 文件存在）

### 提案 6（✅ 已实现）：G7 语气参数 + G8 音色分配最小实装
- 现状: tts-task payload 固定六键（text/lang/engine/engine_url/ref_audio/uid，节点日志实测一致）；speakers 有声线元数据但零分配逻辑
- 问题: TTS 语气全靠参考音隐式传递；"哪个角色用哪个引擎/参考音"无数据载体
- 设计: ①payload 扩展 emotion/instruct/rate，tts_node 透传引擎（CosyVoice instruct_text、Fish speed）；②新模块 `voice_assign.assign_voice` 三级策略：L1 簇参考音（diarize 产物，预留）→ L2 voice_assets 按 gender/age/timbre 标签匹配 → L3 项目级 config.tts 默认；③tts-task 支持 speaker_id|speaker 触发分配，input_hash 纳入 engine/instruct 防缓存错配
- 改动: `controlplane/app/voice_assign.py`（新）、`api/mode_b_api.py`、`gpunode/stages/tts_node.py`
- 验证: `test_tts_task_voice_assign`（标签匹配→engine/ref/rate/emotion 全链进 payload）

### 提案 7（✅ 已实现）：单句热修闭环修复
- 现状: `api/projects.py save_translation` 原地改 version=1 行；重 TTS 钩子查询 `task_type="tts_generate"`（下划线）——实际任务类型是 `tts-generate`，钩子自上线起从未触发
- 问题: 人工改译文丢历史版本；改完译文配音轨不更新（增量规则断链）
- 设计: 人工译文写 version+1（llm_model=human、is_approved、重算音节比/超限标记）；钩子类型修正并限定 project_id；completed/failed/dead 任务打回 pending 清 input_hash
- 改动: `controlplane/app/api/projects.py`
- 验证: `test_save_translation_hotfix_versions`（连续两次热修 v+1/v+2、历史版本保留、human 标记）

### 提案 8（📋 提案，下一批首选）：G4 剧情摘要层
- 现状: 跨场景连贯只有前 8 句（`_prev_scene_ctx`）+ 批内 prev3；无全剧级载体
- 问题: 71 场景的人物关系变化/伏笔/称呼演变没有结构化记忆，R2/R3 只能靠局部窗口猜
- 设计: `synopsis_agent.py` 两级 map-reduce：逐场景一句话剧情（原文批扫）→ 汇总 episode synopsis；落 `projects.config["synopsis"]`（幂等重跑）；`run_translate_scene` 注入"本集梗概 + 本场景前情一句话"到 R2/R3；`projects` 详情 API 暴露摘要供人工校对
- 改动: `controlplane/app/synopsis_agent.py`（新）、`translate_executor.py`、`api/agents.py`
- 验证: 新增 mock stub 测试（摘要注入 R2 prompt 断言）；71 场景剧全量重译对比超限率与一致性抽查
- 成本: 约 1 天（含测试）；可独立合入

### 提案 9（📋 提案）：结构化上下文包 v2
- 现状: 步骤间靠 DB 行 + 字符串拼接（cards 字符串、prev3 字符串、budget "i→n" 串）
- 问题: 每次拼接都是信息降维，下游无法按需取用；prompt 版本迭代无法追溯是哪块上下文起了作用
- 设计: `CtxPack` dataclass（version/budget/speaker/summary/features/glossary 全结构化）+ `serialize()` 渲染各步骤 prompt 段；落库 translations.prompt_version 升级为 ctx_hash，QC 可按 ctx_hash 分组对比质量
- 改动: `translate_executor.py` 主体重构、`db/models.py`（prompt_version 长度）
- 验证: 现有 39 测试回归 + ctx_hash 幂等测试
- 成本: 2-3 天；依赖提案 8 先落摘要字段（建议同批）

### 提案 10（📋 提案）：质量回归基线
- 现状: 全量重译的 2819 句结果散在 translations 表；改提示词/换模型前后无自动 diff
- 问题: 每次优化都在赌——超限率/救援率/模型分布缺少前后对照，优化可能悄悄改坏质量
- 设计: `scripts/quality_baseline.py`：按项目聚合（超限率/isolated 数/fallback_used/压缩轮数/模型分布/音节比分布）落 JSON 基线；`--diff` 模式输出两份基线差异超阈值项；翻译 runner 每次全量跑完自动快照
- 改动: `controlplane/scripts/quality_baseline.py`（新）+ 测试
- 验证: 基线生成 + 人造差异 diff 命中
- 成本: 半天；可独立合入

### 提案 11（📋 提案，G0 后第一批）：说话人绑定管线（最大空白）
- 现状: utterances.speaker_id 全空；DESIGN-B-配音方案-v2 的"声纹聚类→LLM绑定"未实装
- 问题: 每句"是谁说的"缺数据，角色化 TTS 无法启动，多角色剧音色随机
- 设计: cast_agent 第二步 `diarize_and_bind(db, project)`：B2 槽位参考音 → 节点 pyannote stage（嵌入+Agglomerative 聚类 cosine<0.25）→ 簇抽样台词 + C0 角色档案喂 LLM 绑定 → speakers.ref_audio_pool 写 cluster_ref、utterances.speaker_id 落地；Web 角色页可改绑 + 单句重跑
- 改动: `cast_agent.py`、`gpunode/stages/diarize_node.py`（新）、`api/agents.py`、前端角色页
- 验证: mock 聚类 stub 下绑定正确率断言；白月光 2803 句实测抽检
- 成本: 3-4 天（依赖 3060 装 pyannote）；与提案 6 的 L1 簇参考音直接衔接

### 提案 12（📋 提案）：G9 Demucs 分离接入交付
- 现状: `gpunode/stages/separate_node.py` 已注册但无控制面调用方；`mode_b.build_package` 无伴奏轨/混合轨
- 问题: 交付包只有分句干配音，员工本地合成缺 ducking 后的底轨——合成质量靠员工手艺
- 设计: mode-b run 增加"分离"可选步骤（有 zh 音频时）：节点 separate-vocals stage → 伴奏回传（复用提案 5 artifact 通道）→ render 预拼 ducking 混合轨（-10dB 侧链）→ 交付包加 backing/ 与 mixed/ 目录
- 改动: `mode_b.py`、`api/mode_b_api.py`、`render.py`
- 验证: 假音频 E2E 出包结构断言
- 成本: 1-2 天；依赖提案 5（已合入本分支）+ B2 槽位

---

## 合入顺序建议
1. 本分支整体 review 合入（提案 1-7 相互独立，任一出问题可单独 revert）
2. 下一批：提案 10（半天，先立基线）→ 提案 8 → 提案 9（同批，摘要字段一次到位）
3. GPU 实装后：提案 11 → 提案 12
