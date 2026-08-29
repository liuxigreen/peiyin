# ORCHESTRATION.md — 流水线编排完整设计（任务图/状态机/断点续传/DAG引擎规格）

> 本文是控制面的施工核心：**整个译配流程作为一张任务DAG在Postgres里定义、调度、断点续传**。
> 模型/GPU/翻译实现全部后置——本文只关心"谁在什么时候能跑、崩了怎么续"。
> 2026-08-27 定稿 v3.0。取代旧稿中一切与"流程顺序"相关的描述。

---

## 1. 顶层数据流（全景一图）

```
[浏览器]─上传母片─►R2──┐
                       ▼
              ┌──────────────────┐
              │ 项目已创建(原文档) │
              └───────┬──────────┘
                      │ orchestrator扫描: pending的PROJECT级种子任务
                      ▼
   ═══════════ 全片阶段(GLOBAL, 不切片) ═══════════
   T010 probe       探针:元数据/软硬字幕判定            [io]
   T020 audio-extract 16k mono + 1fps关键帧→R2         [gpu*
        * 关键帧也可CPU，但GPU ffmpeg更快；均可]
   T030 vad-scene   SileroVAD+PySceneDetect             [cpu]
   T040 diarize     pyannote全片→rttm                   [gpu]
   T050 speakers-build 角色聚类+参考音频池+试听样本      [cpu]
   T060 plan-chunks 智能分片→N个切片行+每片T1x种子      [cpu]
   ═══════════ 切片阶段(PER-SEGMENT, 可乱序并行) ═══════════
   对每个seg:  T110 subtitle-fast  字幕羽化遮挡          [gpu]
              T120 separate    Demucs人声/伴奏           [gpu]
              T130 asr         FunASR句级+词时间戳        [gpu]
              T140 ocr         PaddleOCR底部25%          [cpu]
              T150 align       双模合并→utterances落库    [cpu]
              (110/120先行;130依赖120;140依赖audio帧;
               150依赖130+140)
   ═══════════ 译文阶段(PER-UTTERANCE批) ═══════════
   T205 ctx-pack    剧本上下文包:术语表+前情+人设卡      [cpu]
   T211 translate-r1 直译批(带上下文包)                 [io]
   T212 translate-r2 意译批(反思R1输出,含配音约束)       [io]
   T213 translate-review 一致性终检(跨批校对/风格统一)   [io]
   T214 merge-dubtrack 台词条落库(版本化)               [cpu]
   T220 syllable-check 音节校验+两级压缩                [cpu]
   ═══════════ TTS与合成(回到切片粒度) ═══════════
   每seg:  T310 tts        批量合成该切片全部句子       [gpu]
           T320 fit-timeline 时长匹配+重试队列          [cpu]
           T330 mix        预拼人声轨+ducking           [cpu]
           T340 encode     切片成片                     [cpu]
   ═══════════ 收尾 ═══════════
   T410 stitch      concat+crossfade全片                [cpu]
   T420 subtitles   ASS/SRT生成                         [cpu]
   T430 qc          三查报告                            [cpu]
   T440 finalize    成片→R2+project.status=completed    [io]
```

资源类型仅三类：`io`(外网API) / `cpu` / `gpu`。**gpu_required=false的任务任何节点都能领**——4090节点其实也能跑cpu/io任务（空闲时顺路消化），这是后面隐藏的吞吐优化点。

---

## 2. 任务表结构升级（在现有 pipeline_tasks 上做的最小改动）

```sql
ALTER TABLE pipeline_tasks ADD COLUMN IF NOT EXISTS
  task_key VARCHAR(80);        -- 稳定键: 'T030' 或 'S03/T130' 或 'U0421/T310'
ALTER TABLE pipeline_tasks ADD COLUMN IF NOT EXISTS
  depends_on TEXT[] DEFAULT '{}';  -- task_key数组: 上游完成才ready
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_key
  ON pipeline_tasks(project_id, task_key);
```

- `task_key` 是断点续传的灵魂：重启/重跑时按 key 幂等去重，同 key 已completed则跳过
- `depends_on` 把 DAG 显式存进数据，orchestrator 无需硬编码顺序
- utterance翻译用的是批量任务key（如 `SCENE_07/T210`），单句重翻=新version+新task行

---

## 3. Orchestrator（纯Python，DB轮询驱动，无外部依赖）

```
控制面内一个 async 循环，每秒：
 ① SUBMIT:  project在analyzed等中间态但有pending可提交的GLOBAL种子 → 写入任务行
 ② READY:   status='pending' AND depends_on⊆(同项目completed集合) → 标记'queued'
 ③ 领取:    gpu节点long-poll拉取(gpu类)；内置asyncio池跑cpu类(io类)
 ④ 收割:    complete/fail回调→写outputs/触发下游②扫描
 ⑤ REAPER:  lease超时的running→回收pending(retry_count<max)或dead
 ⑥ PROGRESS: 按 task_key 聚合出Phase进度百分比→projects.progress字段(Web直接读)

停止条件：项目所有task∈{completed}→推进project.status到下一大阶段/完成
```

关键点：**orchestrator是纯函数式的**——它只看任务表现状决定动作，不保存内存态。进程随时kill随时起，行为不变。

## 4. 断点续传矩阵（每一层怎么恢复）

| 故障 | 恢复机制 | 用户感知 |
|------|---------|---------|
| GPU实例关机/被回收 | 任务running超lease→reaper回收→pending；节点重新join后继续 | 无感，只是慢了 |
| 网络中断传输中 | R2分片上传天然续传；节点claim超时自动放弃 | 无感 |
| 单切片TTS失败 | 只重跑该seg的T310（其他切片任务不动） | 点一下重试 |
| 单句译文修改 | 该utt所在SCENE批translation版本+1→只重算该句T320-T340链 | <30s出新音频 |
| 换音色 | 该speaker全部utt关联的T310置pending | 自动波及正确范围 |
| 换翻译Provider | T210全局版本+1（缓存键含provider_id） | 全部重翻或选段重翻 |
| 控制面进程崩溃 | 重启即可：一切状态在PG里 | 秒级自愈 |
| 中途改擦字幕模式fast→quality | T110的input_hash变化→该key及下游连锁失效 | 明细页提示影响面 |

**内容寻址缓存**: input_hash = sha256(task_type ‖ 关键输入内容hash ‖ 参数hash ‖ 模型标识)。上游输出变了hash才变。completed任务命中即跳过——这让「部分重跑」成为免费操作。

## 5. 切片内并行度（为什么总时长≈2.4h而不是串行8h）

单卡串行理论：12+8+18+15+55+25=133min GPU连续占用。
实际编排让 **CPU/io与GPU重叠**：

```
GPU忙时线:  [probe][diarize][sep×30]────[asr×30]────[tts×30]───
CPU线:            [vad][plan]  [ocr×30][align×30]  [fit×30][mix×30][enc×30]
IO线:                    (TTS分离一完成就送翻译)═════[translate]═══
                                              ▲翻译不占GPU,提前跑完排队
```
- gpu worker串行取每seg的{T110,T120,T130,T310}(模型相近相邻减少切换)
- cpu/io池并发执行无冲突任务
- 全局节流：同一project同时running的gpu任务≤1(3060档)/≤2(24G+且显存<6G任务)

## 6. 进度模型（Web进度条的数据来源）

```
projects.progress = completed_weighted / total_weighted
权重: gpu任务5 / cpu任务2 / io任务1  （反映真实耗时占比）
前端展示两层: Phase stepper(7大阶段布尔) + 当日流水日志(最近20条task事件)
WebSocket暂不做 — 前端10s轮询 progress接口,足够流畅
```

## 7. 与现有代码的衔接（migration清单）

| 已有 | 动作 |
|------|------|
| pipeline_tasks表 | ALTER加task_key/depends_on/唯一索引 |
| nodes.py claim SQL | ORDER BY加priority保留；过滤depends_on满足(satisfies = NOT EXISTS未完成依赖) |
| upload.py presign | 保留;补Part协议(Etag列表/CompleteMultipart)供>500MB分片 |
| seed_demo.py | 补一条完整demo项目种子(含全部T0xx任务行便于联调UI) |
| 新增 | controlplane/app/orchestrator.py(约300行) + api/tasks.py(retry按钮端点) |

## 8. 实施切分（每个PR独立可测）

| PR | 内容 | 自测标准 |
|----|------|---------|
| O1 | 表迁移+task_key幂等写入工具函数 | pytest: 同key重复insert=更新非新增 |
| O2 | orchestrator骨架(SUBMIT/READY/收割循环)+fake executor | python -m app.orchestrator模拟30任务DAG全通 |
| O3 | reaper线程+ lease回收 | 杀executor模拟节点死亡→10min任务回队 |
| O4 | 缓存命中(input_hash)跳过逻辑 | 改参数=重跑,不改=跳过 |
| O5 | retry API+Web重试按钮接真 | POST /api/tasks/{id}/retry 只复活该任务及受影响下游 |
| O6 | 进度聚合+前端stepper接真数据 | demo项目跑半程UI进度条正确变化 |
| G-GPU | 此后才租4090实装stages/* | 第一节真音频产出 |

## 9. 开放问题（实现时决策,不阻塞O1-O6）

- 全片diarize >2h内存风险：预分段30min+跨段embedding合并代码写成try-双路径,首部90min剧实测选路径
- ASR热词表大小上限: 先按glossary全文注入,效果评估后裁剪
- 多语种扩展点: target_lang已是列,stage实现按lang路由即可,O1-O6无需改动

---

*本文件为流水线编排唯一施工依据。DESIGN.md负责架构与部署,本文负责任务图与断点续传。PIPELINE-DETAILS.md负责各环节内的质量参数。三件套构成完整落地设计。*
