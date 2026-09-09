# 交接文档 0908（简版·架构向）

> 目标：让接手者 10 分钟了解系统全貌。细节见 `HANDOVER-B9.md`（0904 详细版）与 `NODE-TASKS-3060.md`（节点任务清单）。实测与文档冲突 → 改文档记日期。

---

## 一、这是什么系统

peiyin：中文短剧 → 外语配音平台。输入中文剧（SRT 台词 + 原声音频），输出目标语言的整条配音素材包，用户拿去剪映自行合成成片。产品要求（用户拍板）：**全自动出包，免人工试听**，对标鬼手剪辑。

## 二、三台机器

| 机器 | 角色 |
|------|------|
| Mac（本机） | 代码仓库 `~/duanju/dubbing-system`，git 只从这里推；经 workbench 通道 exec 上 ECS |
| 阿里云 ECS | 控制面：FastAPI `controlplane/`（8500 端口）+ SQLite（`/opt/peiyin/controlplane/dev.db`，WAL）+ React 前端静态文件；对外走 Caddy/Cloudflare。Tailscale 100.77.187.54（userspace + socks5:1055，SSH 借此跳 3060） |
| Windows 3060 节点 | LENOBO / 100.67.15.30。`E:\peiyin-node\entrypoint.ps1` 拉起 CosyVoice3 引擎（127.0.0.1:50000）+ worker 轮询云端领任务（NODE_WORKERS=2） |

## 三、核心数据流（Mode B）

1. 建项目 → 上传 SRT + 原声 mp3 → 切成逐句 utterances
2. 翻译：LLM 批量翻（带音节比超窗检测，`is_over_limit`）
3. 台词→角色绑定：见下节两条路线
4. TTS 合成：节点领任务，CosyVoice 逐句合成，回传 clip（`POST /nodes/tasks/{id}/complete`）
5. 后期：`audio_post.py`（M&E ducking、呼吸声、响度 MASTER_LUFS）+ `gate_b.py` 质检闸门
6. 交付：`/mode-b/package-from-clips` → zip（逐句 wav + 字幕 srt/ass + manifest + qc_report）

## 四、台词→角色：两条路线

- **v1（已跑通）**：LLM 纯文本绑定 → 每角色配预设音色。缺陷：音色资产只有 6 条，天然撞车，用户听感"全场就 2 个人的声音"。
- **v2（0908 跑通，主推）**：demucs 人声分离 → 切句 → speechbrain ECAPA 声纹聚类（白月光聚出 10 簇）→ LLM 按台词语义把簇绑到角色 → 每角色取最清晰切片当 `ref_audio`（节点 `E:\peiyin-node\workdir\zh_refs\{uid}.wav`）→ CosyVoice zero-shot 克隆。
- pyannote 路线已废弃（HF Hub 联网死锁，BLOCKED）。

## 五、关键代码

- `controlplane/app/api/mode_b_api.py` — TTS 批量 / 试听包 / 单句 retake / clone-refs / 塌缩过滤
- `controlplane/app/audio_post.py` `render.py` `gate_b.py` `simple_clone.py`
- `gpunode/stages/diarize_node.py`（v2=speechbrain）、`separate_node.py`（SEP v1.4.1：mp3 预转 wav + ffmpeg PATH 注入）
- `frontend/src/pages/` — Projects（可交付状态卡）/ ProjectDetail（试听按钮）

## 六、当前状态（0908 14:40）

- 白月光项目 pid=`10f001e14c5c41e6bb17fdd8465d1586`：2450 句、10 角色（443 句经聚类绑定落库）、节点在线
- 翻译覆盖 **600/2450**（需继续跑完）
- v1 预设音色交付包已出（2743 句，missing=0）
- v2 克隆链各环节单测通过（SEP completed、聚类 artifact、绑定落库）；**全链路克隆试听包未出**——这是下一大步
- 已知坑：CV3 中文参考缺 instruct 会塌缩（已修：每句强制 instruct）；3060 引擎偶发挂死（`launch_engine.ps1` 拉起 + `tts-requeue` 复活）；CF 隧道大文件 502（节点改走 Tailscale 内网）；gpu_nodes 表有历史死行（活跃行 id=e20cdfad）；DB 存 UTC、journalctl 是本地时间

## 七、纪律

- base64 禁入 JSON 列（complete 端点有保险丝）；git 只从 Mac 推；交接文档本地保存，重大里程碑才入库。

## 八、待定

用户拟调整整体方案（方向：全自动、免人工试听）。接手后先与用户对齐新方案，再动代码。
