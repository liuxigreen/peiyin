# 交接文档 HANDOVER-B8（2026-09-03 凌晨）

接手前先读：`HANDOVER-0902.md`（基线+0902事故）。本文档只覆盖 0902 夜里之后的增量。

## 一、白月光完整重跑（进行中，最重要）

- 项目：`白月光` pid=`10f001e14c5c41e6bb17fdd8465d1586`，2803句/71场景，SRT来自微信文件（`_no_sub.srt`，含【第N段】标记与<b>标签，现有清洗逻辑已处理）
- 当前：自动翻译进行中（查询命令见下），最后实测 235/2803=8.4%，约23句/分钟，零429，**预计2小时翻完**
- 启动方式：后台脚本 `/tmp/byg.py`（nohup，日志 `/tmp/byg.log`），跑的是 `mode-b/run`（与网站按钮同链路）
- **翻译完成后接手 agent 必须做**（用户没音频，走纯字幕模式）：
  ```
  curl -X POST localhost:8500/api/projects/10f001e14c5c41e6bb17fdd8465d1586/mode-b/tts-batch \
    -H 'Content-Type: application/json' \
    -d '{"engine":"cosyvoice_api","engine_url":"http://127.0.0.1:50000/tts","rate":1.25}'
  ```
  节点合成 2803 句约 5-8 小时（3060）；完成后打包：
  `POST .../mode-b/package-from-clips`（body `{}`，含B7全工艺），下载：
  `/api/projects/10f001e14c5c41e6bb17fdd8465d1586/mode-b/download`
- 查进度：`sqlite3` 查 translations join utterances，或网站工作台百分比
- 若中途 service 重启，翻译脚本会断：重跑 `POST .../mode-b/run` 即续（幂等）

## 二、昨晚已完成（全部实测通过）

1. **B7交付工艺**（ECS 提交 `7a88744`，60 tests 全绿）
   - `app/audio_post.py`：句级DSP链（95Hz高通/380Hz去纸盒/2.8kHz提清晰/7.2kHz降齿音/compand压缩/alimiter 0.84）+句前40ms句尾140ms padding+说话人切换呼吸声
   - 打包自动执行，包内新增 `master_13LUFS.wav`（room-tone床+两遍loudnorm，实测-14.5LUFS/TP-1.0）；支持 body 传 `me_path` 做 M&E sidechain ducking(4.5:1)
   - `app/prosody_qc.py` + `POST .../mode-b/prosody-qc`：F0半音std<1.8或能量std<4.5=平坦→自动加强instruct重掷；耳语/哭腔跳过F0门。实测2句重掷 1.32→3.54 / 1.53→4.64
   - `POST .../mode-b/retranslate-overlimit`：超限句自动压缩重译（version+1，实测M3 17→6音节）
   - 修复：打包clips选择 `version.desc()` 被低版本覆盖→`setdefault` 保最高版本
2. **B8上传翻译直连**（ECS `8156b3d`，Mac `1e66123`+`2a15872`）
   - `POST /api/projects/{pid}/seed-translation`（body `{srt, lang}`）：双语SRT自动挑拉丁字母占比高的行，纯译文SRT按序对齐；实测100/100 matched
   - `mode-b/run` 自动跳过已有译文的场景（不烧网关配额）；llm_model='uploaded'
   - 前端 NewProject 增加翻译文件上传框，已构建部署（Mac bun build → ECS controlplane/web/dist）
3. **防爆保险丝**（0902防线）：`nodes.py` complete 端点拒收 b64/data:/超长blob（413），outputs repr>512KB 拒收；实测跑240次合成 dev.db 仅1MB
4. **节点并发池**：`gpunode/entrypoint.py` NODE_WORKERS=2 线程池（claim与合成/回传重叠）。**等用户在3060重启节点才生效**；OOM风险由用户 nvidia-smi 观察，可降1
5. **网关**：edgefn 429 已恢复（M3/Kimi实测通）。限速器仍生效：M3 9/分、其他edgefn 4/分（env TRANSLATE_RATE_M3 可调）

## 三、方案B现状说明（用户问过）

- 纯字幕模式（本次白月光）：无音频→跳过B2 audio_slots，音色走 voice_assets 6预设（性别/年龄标签+timbre描述打分）
- 完整B链：补传中文配音音频→run 时 audio_slots 按时间轴切每句中文参考→克隆原声
- 未实装：diarize 声纹聚类（哪句属于哪个角色仍靠 LLM 文本绑定）；M&E ducking 代码就绪待真实素材验证

## 四、关键操作速查

- 服务：`systemctl restart peiyin`（工作目录 /opt/peiyin/controlplane，端口8500）
- 服务env解密provider key：`eval export $(systemctl show peiyin -p Environment --value)`
- 独立脚本连DB必须带上面这行（ENCRYPTION_KEY）
- workbench exec 单条30s超时：长任务用 `systemd-run --unit=X --collect` 或 nohup 后台
- 日志：`journalctl -u peiyin --no-pager -n 50`
- 节点日志在 Windows 3060 那台（E:\peiyin-node，用户手动操作）
- 测试：`cd /opt/peiyin/controlplane && .venv/bin/python -m pytest tests -q`（60 passed）

## 五、红线（不可违反）

1. **b64/二进制禁入 JSON 列**——complete 端点已有保险丝自动拒绝；音频只走 /artifact 流式端点+文件路径
2. TTS 只用 CosyVoice 系（本地引擎 cosyvoice_api）；禁止 xTTS v2 / F5-TTS
3. git push 无凭据：ECS `upgrade/seam-wave1` 有 1a2cddf/7a88744/8156b3d 未推送，**需用户从 Mac 仓库推**（Mac 已同步 B7/B8 提交 2a15872/1e66123）
4. 3060 重启节点命令（PowerShell，E:\peiyin-node）：
   `$env:NODE_WORKERS="2"` 后 `python entrypoint.py`；看到 `[node] worker pool = 2` 生效

## 六、遗留与建议

- 竖屏审校台、P3（OCR画面文字修补/特写唇形对齐）用户明确暂不做
- Gemini 三轮顾问结论：量产租4090（$0.44/时，7-10倍速）、集级任务队列、Show Bible——见会话 `gemini.google.com/app/c0d09025350e3f1a` 与 `/app/2ae407682dfe5dd1`
- 服务器购买结论：国内 4C4G3M 够用（用户已定）
