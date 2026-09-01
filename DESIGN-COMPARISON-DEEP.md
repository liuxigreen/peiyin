# 三份设计方案深度对照总结（逐条 vs 现有系统）

> 2026-08-30。用户要求认真精读三份方案原文后与现有系统逐条对照。
> 三份方案：①《技术与架构设计方案》（分层架构+Celery+三层切片+交错调度+pgvector）
> ②《翻译API可配置化+TTS智能路由更新》（合规TTS选型+成本追踪）
> ③《最终施工方案》（五条工程原则+定案栈+Phase0-8+三级音色+验收指标）
> 附：第一版对话记录（3060现实判断+最小闭环）。
> 结论先行：**架构层我们已实现并局部超越；三份方案里还有 18 项工程细节值得吸收（其中 5 项高价值），2 项选型冲突需裁决。**

---

## 一、三份方案各自的精华（此前吸收不足的）

### 方案① 独有价值
| # | 设计点 | 细节 |
|---|--------|------|
| 1 | **三层切片策略** | Layer1 场景切片3-8min(FFmpeg scene detect) / Layer2 对白段落1-3min(VAD静音>800ms, GPU调度单元) / Layer3 单句(编辑单元)。三层各有职责，避免硬切断句 |
| 2 | **SFX/BGM 分离混音** | 4-stem 分离后二次分类（瞬态能量=SFX vs 持续频谱=BGM）；混音时 SFX 不压限只做频段错开(>4kHz直通)，保留冲击力 |
| 3 | **Ducking 精确参数** | 侧链压缩 threshold -30dBFS / ratio 4:1 / attack 10ms / release 200ms |
| 4 | **参考音频评分函数** | score = SNR×0.4 + 情绪中性度×0.4 + 时长合适×0.2，不是只按 SNR 排序 |
| 5 | **pgvector 全局音色锁定** | speakers 表存 256-dim embedding，跨项目复用；全局 Agglomerative 聚类(cosine<0.25) |
| 6 | **CUDA 多 Stream 交错** | GPU-Compute / GPU-Memory / CPU-IO 三队列 + torch.cuda.Stream + get_optimal_concurrency(24G→2~3并发) |
| 7 | **user_edited/user_override** | utterances 表记录用户手动覆盖，重跑时保护人工编辑不被冲掉 |
| 8 | **HLS 切片热更新** | 单句修改→只重渲染该切片的 .ts 段→播放器无感刷新 |

### 方案② 独有价值
| # | 设计点 | 细节 |
|---|--------|------|
| 9 | **⚠ TTS 商用授权合规** | **xTTS v2 = CPML 商用受限；F5-TTS = CC-BY-NC 不可商用**——短剧出海是商业用途必须排除。Fish Speech v1.5(Apache 2.0, MOS 4.4-4.6, 500M~5B)、Chatterbox(MIT)、Kokoro(Apache)、MeloTTS(MIT) 合规 |
| 10 | **翻译成本追踪** | translation_usage_logs 表逐次记录 tokens/成本/延迟；月预算 80% 黄灯告警 / 100% 自动禁用切换备选 |
| 11 | **翻译故障自动降级** | TranslationRouter：健康检查失败→按 priority 自动切换下一家→全挂才报错 |
| 12 | **项目级翻译覆盖** | project_translation_overrides(project_id, target_lang)→某项目某语种指定服务商 |
| 13 | **TTS 引擎 DB 可配置** | tts_engines(显存需求/克隆能力/部署方式) + tts_language_routes(语言→主/备引擎,可拖拽调优先级) + speaker_voice_bindings(角色×语种×引擎×参考音频,含质量评分) 三张表 |
| 14 | **BaseTTSEngine 统一接口** | load/synthesize/synthesize_batch/unload + TTSWorkerManager(LRU显存管理,vram_map按引擎) |

### 方案③ 独有价值（最细）
| # | 设计点 | 细节 |
|---|--------|------|
| 15 | **三级音色管理** | Level1 主要角色(>50句)→GPT-SoVITS v2 微调(40min/角色,数据要求:SNR>30dB片段3-10分钟) / Level2 次要(10-50句)→CosyVoice2零样本 / Level3 龙套(<10句)→预置音色池20-30个 |
| 16 | **情绪工程完整链** | emotion2vec分类(label+intensity+probs) + F0轮廓/能量轮廓(下采样16-32点存JSONB) + EMOTION_INSTRUCTION_MAP多语种instruct映射 + EMOTION_RATE_MAP(愤怒1.10/悲伤0.88…) |
| 17 | **Phase 0 预览审批门控** | 预分析完成→暂停→用户确认角色名/分片→才批量处理（防浪费）。与用户"全自动"偏好冲突→做成项目级开关 |
| 18 | **验收指标表** | 端到端>95% / 字错率<0.5% / 音节100%<1.15 / UTMOS均值>3.5 / 单句修改<30s / 缓存命中修改1句重算<0.1% / 90min<2.5h |
| 19 | **混音三细节** | SFX时刻仅-4dB(非-10dB,保留冲击) / attack 30ms release 150ms(防呼吸感) / 爆音±20ms平滑插值 |
| 20 | **增量计算触发规则表** | 改1句译文→该句TTS+该切片混音+编码；换音色→该角色全部TTS+相关切片；换引擎→全量。input_hash自动检测 |
| 21 | **WhisperX 兜底** | FunASR 主 + Whisper large-v3-turbo 兜底双跑取置信度高者（噪声场景保险） |
| 22 | **Resemble Enhance 降噪** | 分离后人声增强→提高ASR准确率+情绪特征质量+TTS参考音底料 |

## 二、现有系统对照：已实现 / 已超越 / 缺口

### ✅ 已实现且验证（无需动）
- 任务DAG编排+断点续传+input_hash缓存+retry级联（方案③第六章的我们版，且去Celery化更简）
- 五步翻译链+语种硬约束+语种校验兜底（M3 2803句验证）
- 翻译Provider网页配置+加密+测试连通（=方案②1.1-1.4 的已实现版）
- QC Agent 12环节钩子 / 算力Agent自动开关机(干跑) / 渲染链(两遍loudnorm+ASS) / 模式B交付包 / 云端Tunnel部署 / R2双模式 / 评审修复7项

### 🔶 已超越三份方案的决策（保持，不改）
1. **PG SKIP LOCKED 替代 Celery+Redis**（三份全是 Celery——公网场景是反模式，评审也确认我们优）
2. **PG 队列+REST 拉取**替代 broker 暴露
3. **算力 Agent 自动开关机**（三份方案都没有，只有"云Worker弹性"一句话）
4. **模式 B 无视频交付链**（三份方案全部默认有视频全流程）
5. **FunASR 为主**（方案①用WhisperX、方案③定案FunASR与我们一致；方案③的 Whisper 兜底双跑可吸收）

### ❌ 缺口 = 接手后的实现清单（按价值排序）

**高价值（直接影响成片质量）**
| # | 缺口 | 来源 | 实现要点 |
|---|------|------|---------|
| G1 | 三级音色管理 | 方案③6.2 | speakers表加level字段+voice_pool表；GPT-SoVITS微调管线(G0后) |
| G2 | 情绪工程链 | 方案③Phase4 | emotion2vec阶段+utterances加contours字段+instruct映射表（参考音频情绪中性选择已吸收，此为完整版） |
| G3 | TTS商用授权核查 | 方案② | 我们已选 CosyVoice2(开源可用)+Confucius4(Apache) ✅合规；**注意绝不引入xTTS v2/F5-TTS**；Fish Speech(Apache) 作为 en/es/pt 备选引擎入库 |
| G4 | 混音三细节 | 方案①A3/③Phase7 | SFX时刻-4dB、attack/release参数、爆音插值——render.py 小改 |
| G5 | 音节比25%超限二次压缩闭环 | 已知待办 | 白月光703句,按方案③5.3两轮修复(LLM压缩→规则裁剪) |

**中价值（体验与成本）**
| # | 缺口 | 来源 |
|---|------|------|
| G6 | 翻译成本追踪+预算告警 | 方案②1.5（usage_logs+80%/100%阈值） |
| G7 | 翻译故障自动降级链 | 方案②1.4（健康检查→priority切换） |
| G8 | TTS引擎DB可配置三表 | 方案②2.5（替代硬编码LANG_TTS_ROUTES） |
| G9 | 参考音频评分函数 | 方案①B3（SNR×0.4+中性×0.4+时长×0.2） |
| G10 | user_edited保护 | 方案①（对照表人工编辑后重跑不冲掉） |
| G11 | Whisper兜底双跑 | 方案③Phase3 |

**低价值（二期/规模化再考虑）**
| # | 缺口 | 说明 |
|---|------|------|
| G12 | Web时间轴精修台(Canvas波形+HLS热更新) | 大工程,先有对照表编辑够用 |
| G13 | CUDA多Stream交错调度 | 单卡并发2-3先靠资源模型节流 |
| G14 | 三层切片 | 当前SRT种子/VAD分片够用,GPU阶段再精化 |
| G15 | pgvector跨项目音色库 | 资产中心二期 |

## 三、选型冲突裁决

| 冲突 | 方案①/② | 方案③/本系统 | 裁决 |
|------|---------|-------------|------|
| TTS英语主力 | ①xTTS v2(已废) ②Fish Speech | ③CosyVoice2 | **维持 CosyVoice2 主力**（与方案③一致，6-10G显存更省，instruct情绪控制）；**Fish Speech 入库为 en/es/pt 备选引擎**（Apache合规、MOS更高但显存8-12G） |
| ASR | ①WhisperX | ③FunASR+Whisper兜底 | **FunASR 主**（中文>98%+热词），Whisper large-v3-turbo 兜底双跑列入G11 |
| 字幕擦除 | ①STTN/LAMA | ③ProPainter | **ProPainter**（quality档，与现有T110 quality槽位一致） |
| 任务队列 | ①②Celery+Redis | 我们PG SKIP LOCKED | **保持PG**（公网安全，评审确认） |
| 音源分离 | ①UVR5 4-stem | ③Demucs 2-stem | **Demucs 2-stem**（人声质量反高于4-stem，定案不变） |

## 四、给 autoclaw 的行动项（合并进 HANDOVER 待办）

在 HANDOVER.md §六 待办路线中插入/更新：
- G5 音节二次压缩（原第1项，优先级不变）
- G4 混音三细节（render.py 小改，归入 N3 收尾）
- G3 TTS 引擎入库登记：CosyVoice2 主 + Fish Speech 备 + Confucius4 小语种 + Kokoro 旁白（全部合规许可）；**红线：不引入 xTTS v2 / F5-TTS**
- G1+G2 三级音色+情绪工程（GPU 实装 G0 后的第一批，替代原"情绪参考池 V1.5"的简单版）
- G6~G11 按序排入 P2

## 五、一句话总结

三份方案的**骨架**（DAG/切片/缓存/调度）我们在没有它们的情况下独立推导并实现了一遍，且队列架构与部署形态更优；它们的**血肉**（三级音色/情绪工程/混音参数/成本追踪/合规TTS矩阵/验收指标）是我们下一阶段（GPU 实装后）的施工图。两套模式（A完整/B字幕配音）的运行框架不受影响——这些细节全部落在模式 A 的 GPU 环节与两种模式共用的翻译/TTS 质量链上。
