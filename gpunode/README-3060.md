# 3060 机器接入译配系统（TTS 节点）

目标：把你的 RTX 3060 12G 变成译配系统的 TTS 算力节点，控制面（云端）派任务，3060 本地跑 CosyVoice2/Fish Speech 出英文配音。

## 一次性安装（在 3060 那台 Windows/Linux 机器上）

### 1. 前置
- NVIDIA 驱动 + CUDA 12.x（`nvidia-smi` 能看到卡）
- Python 3.10+，git

### 2. 拉代码
```bash
git clone https://github.com/liuxigreen/peiyin.git
cd peiyin/gpunode
pip install -r requirements.txt
```

### 3. 装一个 TTS 引擎（二选一，先 CosyVoice2）

**CosyVoice2（推荐，跨语种克隆最成熟）**
```bash
git clone --recursive https://github.com/FunAudioLLM/CosyVoice2-0.5B
cd CosyVoice2-0.5B
pip install -r requirements.txt
# 模型自动从 HF/ModelScope 下载（~3GB）
python webui.py --port 50000     # 起本地服务
```

**Fish Speech S1-mini（Apache2.0 商用无忧）**
```bash
pip install openaudio
# 或按官方文档起 api 服务（监听 8080）
```

### 4. 启动节点 Worker（保持引擎服务在跑）
```bash
# Linux/macOS
CONTROL_URL=https://dubbing.mulan.dpdns.org \
NODE_SHARED_SECRET=<问管理员要，或先用 dev-node-secret> \
NODE_MODE=real GPU_MODEL="RTX 3060" GPU_VRAM=12 \
python entrypoint.py

# Windows PowerShell
$env:CONTROL_URL="https://dubbing.mulan.dpdns.org"
$env:NODE_SHARED_SECRET="dev-node-secret"
$env:NODE_MODE="real"; $env:GPU_MODEL="RTX 3060"; $env:GPU_VRAM="12"
python entrypoint.py
```
看到 `[node] registered` 即接入成功，节点会开始轮询领任务。

## 测试闭环（5分钟）

1. 管理端（Web → 项目 → 白月光 → 台词对照表）确认有英文译文
2. 创建测试任务（云端或本地控制面）：
```bash
curl -X POST https://dubbing.mulan.dpdns.org/api/projects/<白月光项目id>/mode-b/tts-task \
  -H "Content-Type: application/json" \
  -d '{"uid":"SC30-1161","engine":"mock"}'
```
3. 3060 节点日志出现 `[ok] tts-generate xxxxx` → 任务完成
4. 引擎真跑：把上面 engine 换成 `cosyvoice_api`、`engine_url` 换成 `http://127.0.0.1:50000/tts`（按你引擎实际API调整）

## 排障
- `claim err` → CONTROL_URL 不可达或 NODE_SHARED_SECRET 不对
- 任务一直不领 → 控制面 pipeline_tasks 里该任务 status 必须是 pending，且 depends_on 全部 completed
- CosyVoice 显存不足 → webui 里有 fp16 开关；3060 12G 建议 fp16 + 单并发

## 补充：B方案完整安装清单（2026-09-01 定版）

| 组件 | 必要性 | 安装 |
|------|--------|------|
| CosyVoice 3 (TTS) | ✅ 必装 | 见上文第三步 |
| Demucs (人声分离) | ✅ 必装——去掉原中文人声留伴奏 + 提纯克隆参考音 | `pip install demucs`（上面 requirements 已含） |
| pyannote (说话人分离) | ⚠️ 选装（建角色音色库用） | `pip install pyannote.audio` + 去 hf.co/settings/tokens 申请 token：`huggingface-cli login` |
| FunASR (中文识别) | ❌ B方案不装 | 字幕为准；A方案(视频)阶段再装 |
| ffmpeg | ✅ 必装（Demucs 依赖 + 音频处理） | Windows: `winget install ffmpeg`；Linux: `apt install ffmpeg` |

节点支持的 task_type（控制面可派）：
- `tts-generate` — TTS合成（cosyvoice_api/fish_api/mock）
- `separate-vocals` — Demucs 人声/伴奏分离
