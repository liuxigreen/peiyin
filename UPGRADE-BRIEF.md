# 升级创新任务简报（给外部升级 Agent）

> 你受托对这个短剧译配系统做**升级创新**：不是泛泛的代码评审，
> 而是逐步骤深审管线、重点攻击**步骤之间的衔接面**，提出并直接实现升级。
> 已授权你修改代码（见"授权边界"）。

## 一、阅读顺序（按序，不要跳）

1. `HANDOVER.md` — 交接文档（系统全貌/坑/实测事实）
2. `UPGRADE-BRIEF.md`（本文件）— 任务与授权
3. `V3-FINAL.md` / `ARCH-V3.1.md` / `PIPELINE-DETAILS.md` — 原始架构
4. `MODE-B-DESIGN.md` — 模式B（字幕+配音音频，当前主航道）
5. `DESIGN-COMPARISON-DEEP.md` — 与外部方案对标（22项精华/18项缺口）
6. `DESIGN-B-配音方案-v2.md` — 配音链最新设计（声纹聚类→LLM绑定→角色化TTS）
7. `REVIEW-BRIEF.md` — 另一份审核向简报（P0-P3重点清单可参考）
8. 源码（见下）

## 二、当前管线的 10 个步骤与**衔接契约**（重点审查对象）

每一步的输入/输出契约如下——你要审的就是这些契约丢了什么信息、哪里可以升级：

```
Step 1  SRT导入 (api/translate.py seed-srt)
  → utterances 落库 (uid SCxx-xxxx, start_ms, end_ms, original_text, char_count)

Step 2  C0角色提取 (cast_agent.extract_cast, 已实装)
  → speakers 落库 (label/role_name/is_primary/ref_audio_pool[gender,age_band,timbre])
  → glossary_terms 落库 (人名中英对照, 40条)
  ⚠ 衔接缺口G1: speakers 有 gender/age/timbre 但 build_ctx_pack 只传了
    role_name 字符串——gender/age/timbre 没进提示词

Step 3  翻译链 run_translate_scene (translate_executor.py v1.3)
  R1 直译   = M3 no-think（chat_template_kwargs enable_thinking=false）
  R2 本地化 = DeepSeek-V3（习语等效化，实测最强）
  R3 终检   = M3 增量协议（只回修改行）
  压缩闭环  = Qwen3-235B（超限句，≤2轮）+ 兜底链重试
  注入的上下文: glossary块 / 角色名串 / 时长预算块(duration×6syl/s×1.15)
    / prev3(前块尾3行译文) / prev_scenes_ctx(前一场景尾8句)
  失败处理: 内容审查→段级兜底链换模型重跑该块 / 网络故障→换模型
    / 格式漂移→重试一次后换模型 / 全链失败→单句隔离或二分
  ⚠ 衔接缺口G2: prev_scenes_ctx 查询硬编码 version==1——
    全量重跑后最新版本是 v4+，跨场景上下文静默失效（已知bug，待验证修复）
  ⚠ 衔接缺口G3: R1→R2 只传文本，不传"本句特征"（纯数字句/人名句/超短句
    应该走不同翻译策略）
  ⚠ 衔接缺口G4: 无全剧级剧情摘要——跨场景连贯只有"前8句"，71场景的
    长程一致性（人物关系变化/伏笔）没有载体

Step 4  压缩闭环 (_compress_chunk)
  输入: 超限句最新译文 + 时长预算
  输出: version+1 新行（llm_model 带 compress: 标记）
  ⚠ 衔接缺口G5: 压缩产物跳过 R3 终检——压缩后的语义漂移无人把关

Step 5  TTS (mode_b/tts-task → 3060节点)
  控制面建 pending 任务(payload=text/lang/engine/engine_url/ref_audio)
  → 3060节点 entrypoint 轮询 /api/nodes/me/claim 领取
  → stages/tts_node.py 调本机引擎HTTP(CosyVoice3/Fish) → wav 落节点本地
  ⚠ 衔接缺口G6: 产物回传缺失——output_paths 记的是节点本地路径，
    控制面拿不到 wav 文件（无上传通道）
  ⚠ 衔接缺口G7: payload 无 emotion/instruct 参数——TTS 语气全靠参考音隐式传递
  ⚠ 衔接缺口G8: speakers 表 gender/age/timbre 已有，但没有"音色分配"逻辑
    （哪角色用哪个引擎/参考音——DESIGN-B-配音方案-v2.md 的三级音色策略未实装）

Step 6  交付包 (mode_b.build_package)
  输出: zip(subtitles.srt/ass + manifest.json + qc_report + audio clips)
  ⚠ 衔接缺口G9: 原配音音轨的人声/伴奏分离未接入交付（Demucs stage 已在
    节点包里但无调用方）；员工本地合成缺 ducking 后的混合轨
```

## 三、创新方向提示（不限于此）

- **说话人绑定管线**（最大空白）：SRT无说话人标注。方案 v2 已设计
  "参考音→pyannote声纹聚类→LLM绑定角色→角色音色策略"，未实装——
  把它做成 cast_agent 的第二步（diarize+bind），utterances.speaker_id 落地后
  R1/R2/R3/TTS 全链都能吃到"这句话是谁说的"
- **剧情摘要层**：71场景跑一遍"摘要Agent"生成 episode synopsis + 分场景
  一句话剧情，注入 R2/R3，解决长程一致性
- **结构化中间格式**：目前步骤间靠 DB 行 + 字符串拼接传上下文；升级为
  版本化的 JSON 上下文包（budget/emotion/speaker/summary 全结构化），
  步骤间信息不再降维
- **单句热修闭环**：译文修改 → 自动触发该句重翻/TTS（input_hash 已支持，
  缺 API 串联）
- **质量回归基线**：用全量重译的 2819 句建立质量基线（超限率/救援率/模型
  分布），每次改提示词/换模型后自动 diff——防止优化把质量改坏

## 四、授权边界（必须遵守）

1. ✅ 可改：controlplane/**、gpunode/**、frontend/**、tests/**
2. ✅ 可提交：**只允许 `upgrade/*` 分支**，32 个测试全绿后 push 分支
3. ❌ 不可：直接 push main / 部署到云端（ECS tar+restart 由项目所有者执行）/
   删除或覆盖生产数据库 / 提交任何密钥（.env.r2、api key、NODE_SHARED_SECRET）
4. 本地路径 `~/duanju/dubbing-system` 是权威工作副本（含未入库的运行时上下文）；
   远程协作则 clone git 仓库。**不要把本地 .env.* 文件内容写进任何提交**
5. 测试纪律：mock 模式（不调真实 API）；改完跑
   `cd controlplane && .venv/bin/python -m pytest tests/ -q` 必须全绿；
   新功能必须带测试

## 五、产出要求

每个创新点一份提案，格式：
```
### 提案 N：<标题>
- 现状: 当前怎么做的（引用文件:行号）
- 问题: 丢了什么信息/慢在哪/质量损失在哪
- 设计: 改成什么（数据流图/接口变更）
- 改动: 文件清单
- 验证: 新增测试 + 预期指标变化（质量/速度/成本）
```
提案按 影响力/成本 排序，标记哪些可以独立合入、哪些有依赖。
改完代码 → upgrade 分支 push → 通知项目所有者 review 合并。
