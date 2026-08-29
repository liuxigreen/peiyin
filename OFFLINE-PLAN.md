# dubbing-system 离线联调方案（零外部依赖版）

> 2026-08-28。定案：**先不买 VPS/R2/GPU**。本地 Mac 全流程联调，外部件全部用「假件+接口桩」替代，
> 多Agent与流程100%设计定稿并测试通过后，外部服务一键切换接入（改环境变量，代码零改动）。

---

## 一、哪些不用买也能测（现有+本轮补）

| 外部件 | 本地替代 | 已有? | 本轮动作 |
|--------|---------|-------|---------|
| R2对象存储 | 本地目录 storage/（r2.py 已内置mock双模式） | ✅ | 补本地分片上传端点 |
| GPU实例(4090) | **Mac本地假GPU节点**（gpunode join，跑demo/轻量stage） | ✅骨架 | 补T110/T340/T420的CPU实现+假权重stage |
| 翻译API | MOCK provider（已有，词典假翻译） | ✅ | 无 |
| VPS/域名 | Mac :8500 直连（公司电脑同WiFi可访问） | ✅ | 无 |
| compshare开关机API | 算力Agent干跑模式（日志模拟开/关机） | ✅设计 | 实现Agent+干跑测试 |

## 二、多Agent名单与职责（设计定稿）

| Agent | 进程形态 | 职责 | 离线可测 |
|-------|---------|------|---------|
| 🎬 调度Agent | 控制面线程：ready_scan循环 | 依赖解锁/派发/缓存命中跳过 | ✅已有 |
| 🗡 Reaper Agent | 控制面线程：lease收割 | 节点死亡→任务回队→重派 | ✅已有 |
| 💰 算力Agent | 控制面线程：队列水位监控 | gpu积压→开机；空闲→关机；安全阀 | 🔨本轮实现（干跑） |
| ⚙️ 节点Agent×N | gpunode进程 | 注册/领任务/心跳/执行/交产物 | ✅骨架（Mac假节点演示） |
| 🧪 QC Agent | 任务完成钩子（纯函数集） | 12环节质检矩阵执行 | 🔨本轮实现 |
| 🌐 Web/API | FastAPI | 全公司入口/进度/对照表/重试 | ✅已有 |

Agent间协议：只通过Postgres任务表通信（status/claimed_by/lease/heartbeat），无内存耦合——这就是"多agent协作"在本系统的具体形态：**一个任务队列+多个自治消费者**。

## 三、流水线全环节 × 离线实现方案

| 任务 | 离线实现 | 联调验收标准 |
|------|---------|-------------|
| T010 probe | ffprobe读元数据（真实现，本地直接用） | 元数据JSON正确 |
| T030/T060 VAD/分片 | 静态分片（按时长均切3片） | 3片任务行+依赖正确 |
| T040 diarize | 假stage：固定2角色rttm | speakers表2行 |
| T110 擦字幕fast | **CPU真实现**：OpenCV白像素定位+羽化（Mac能跑，就是慢） | 擦后帧底部无字幕 |
| T120 separate | 假stage：原音复制为vocal/bg双轨 | 双文件产出 |
| T130 asr | 假stage：读SRT种子回填asr_text | utterances.asr_conf=1.0 |
| T140 ocr | 假stage：同上回填ocr_text | 合并confidence≥0.9 |
| T205-T220 翻译链 | 真实现已有（MOCK词典provider） | ✅19测试已绿 |
| T310 tts | 假stage：espeak-ng/静音wav（时长=原句×0.8） | 每句wav产出 |
| T320 fit | **CPU真实现**：读取wav实际时长，超窗→atempo加速（ffmpeg） | 全句ratio∈[0.8,1.2] |
| T330 mix | 真实现已有（render.py预拼+bg 30%） | dub.wav正确 |
| T340 encode | **CPU真实现**（render.py mux已就绪，x264软编） | 成片mp4 |
| T420 subtitles | 真实现已有（write_ass/srt） | ASS/SRT产出 |
| T430 qc | 真实现已有（QC三查）+ 🔨扩成QC Agent钩子集 | 报告JSON全pass |
| T440 finalize | 本地storage/目录交付+下载链 | 浏览器可下载成片 |

**离线全流程验收（本轮总目标）**：上传SRT测试片 → 一次点击 → 假节点+CPU池混合执行 → 浏览器下载到带假配音+真字幕+QC报告的成片MP4。全程Mac，¥0。

## 四、外部件接入设计（预留的切换开关，后期零改动）

```
R2:        R2_ACCESS_KEY 环境变量一填 → storage/变云端桶（r2.py双模式已备）
GPU:       compshare实例开机跑同镜像join → 真模型stage按capabilities自动路由
           （假stage与真stage同名注册，节点能力发现决定谁领）
翻译:      网页Providers填key → provider自动mock→live（已实现）
开关机API:  算力Agent从干跑模式切API模式（SCNET_OPENAPI_TOKEN就位）
VPS:       deploy/docker-compose.yml + DATABASE_URL（sqlalchemy双方言已备）
```
设计原则：**外部依赖全部收敛到「环境变量+capabilities注册」两个开关**，任何一天接入都是配置变更，代码冻结。

## 五、本轮实施序（确认后开工）

| 步 | 内容 | 验收 |
|----|------|------|
| O1 | QC Agent：12环节质检钩子集+质检Tab数据接口 | 造3类失败→钩子全捕获 |
| O2 | 算力Agent（干跑模式）：水位监控/开关机日志/安全阀 | 模拟积压→日志出"开机"决策 |
| O3 | 假节点+假stage包（diarize/separate/asr/tts） | Mac节点join→领gpu假任务→产物入库 |
| O4 | T110擦字幕CPU版+T320 fit真实现 | 真测试视频擦除+时长匹配 |
| O5 | **离线全流程E2E**：SRT→成片+QC报告一键出 | 浏览器下载成片，QC全绿，成本¥0 |

O1-O5全部完成后，系统的流程/多Agent/质检就100%设计落地且实测通过——届时接入R2/GPU/真API只是换配置。

---

*本档与 ARCH-V3.1.md（多Agent/GPU细节）互为补充：ARCH管「要什么」，本档管「不买外部件怎么先跑通」。*
