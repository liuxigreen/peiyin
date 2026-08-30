# 模式B设计定稿：无视频模式（字幕+中文配音 → 外语字幕+外语配音交付包）

> 2026-08-30。用户拍板：平台支持两种完成方式——
> **模式A**（完整）= 上传视频走全流程 → 输出配音成片视频；
> **模式B**（无视频）= 上传中文字幕SRT + 中文配音音频 → 输出【外语字幕+分句外语配音】交付包，员工本地合成。
> 模式B省掉：视频上传/存储/擦字幕/识别(ASR+OCR)/混音/烧录/编码。**翻译与配音质量两条模式完全一致**（同一条链）。

---

## 1. 模式B流程（复用现有能力标注）

```
员工上传：中文字幕SRT + 中文配音音频(整条wav/mp3)
  │
  B1 字幕解析落库        = 复用 seed-srt（含场景分组）
  B2 中文音频槽位分析     = 新增 t200 audio-probe：按SRT时间窗从整条音频切分每句参考，
  │                        得到每句 {start_ms, end_ms, ref_audio}（音频即"中文配音轨"，
  │                        时间窗=SRT时间码，与A模式的align产物同构）
  B3 五步链翻译          = 复用现有 T205-T220（M3，语种硬约束+音节校验）
  B4 外语TTS            = 复用 T310 语义：每句合成外语音频
  │                        （en=CosyVoice2需GPU节点；id/es/pt等=Confucius4在线API零GPU；
  │                         中文参考音频从B2切分的片段选取——音色克隆参考）
  B5 fit时长匹配         = 复用 T320（atempo 0.5-2.0，窗口=B2槽位）
  B6 交付包生成          = 新增 t450 package：
                           dubbing_package.zip
                           ├─ subtitles.{lang}.srt / .ass（对齐后时间码）
                           ├─ audio/{seq:04d}_{uid}.wav（分句外语配音，已fit）
                           ├─ manifest.json（每句：原文/译文/时长/速度/引擎）
                           └─ qc_report.json（音节比/时长比/失败清单）
  │
  下载交付包 → 员工本地合成
```

## 2. DAG差异（模式B = DAG子集）

| A模式任务 | B模式 |
|-----------|-------|
| T010-T060 全片阶段（probe/diarize/vad/分片） | 跳过（B2替代probe+槽位） |
| T110-T150 切片阶段（擦字幕/分离/ASR/OCR/对齐） | 跳过（字幕即台词源） |
| T205-T220 翻译五步链 | ✅ 保留 |
| T310-T340 TTS/fit/mix/encode | T310✅ T320✅ T330/T340跳过（无混音无视频编码） |
| T410-T440 缝合/烧录/QC/交付 | T450 package替代（含QC三查的音频版） |

实现方式：`instantiate-dag` 加 `mode: "B"` 参数 → build_project_dag 生成 B 子集
（不新建函数，在现有 build 里按 mode 分支，保持单一事实源）。

## 3. 上传协议

```
POST /api/projects                      {name, target_lang, mode: "B"}
POST /api/projects/{pid}/seed-srt       {srt}                     ← 中文字幕
POST /api/upload/presign                {project_id, filename: "zh_audio.wav"}
PUT  <presigned>                        ← 中文配音音频（mock模式=本地storage/）
POST /api/projects/{pid}/mode-b/run     {}  ← 触发 B2-B6 串行链（同步跑，小项目秒级；
                                            大项目走DAG异步，同一套任务行）
GET  /api/projects/{pid}/package        → 下载zip
```

## 4. 质量口径（与模式A一致）

- 音节比：100% ≤1.15（同A）
- 时长比：TTS vs 槽位 ∈ [0.8, 1.2]，超窗 atempo ≤1.3，仍超→压缩重译闭环≤2轮（同A）
- 语种校验：译文含中文>30%句→失败重试（本次新增，A/B共用）
- QC报告：随包交付 manifest + qc_report

## 5. 边界与开放问题

- 中文配音是"整条音频"时：B2 按 SRT 时间窗直接切片（时间码即真相源，无需静音检测——
  员工的配音轨本来就是按字幕时间轴录的；切片误差±100ms可接受，manifest里标注）
- 分句zip上传：二期（需要命名规范约定），一期先支持整条
- TTS 在无 GPU 时：en 走 CosyVoice2 不可用 → 降级 Confucius4(若支持) / 标记"待GPU"
  （id/es/pt 等语种 Confucius4 在线 API 即可完成全链，这正是多语种路由的战略价值）
- B模式字幕烧录：不做（用户明确跳过）；ASS 文件随包提供，员工本地播放器/剪辑软件可用
