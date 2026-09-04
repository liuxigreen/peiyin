# NODE-TASKS-3060 · 节点任务清单（2026-09-04 16:37 版）

> 给 3060 机器的 Agent/操作者：这是**当前待执行清单**，按顺序做。
> 完成一项勾一项，全部完成后通知云端控制者（AutoCoder）。
> 云端链接：https://github.com/liuxigreen/peiyin（分支 upgrade/seam-wave1）

---

## TASK-1 · 代码同步（2 分钟）

```powershell
# E:\peiyin-node 是节点目录。两种情况二选一：

# 情况A：E:\peiyin-node 是 git 仓库（里面有 .git）
git -C E:\peiyin-node fetch origin
git -C E:\peiyin-node checkout origin/upgrade/seam-wave1 -- gpunode/
Copy-Item -Recurse -Force E:\peiyin-node\gpunode\* E:\peiyin-node\

# 情况B：不是 git 仓库
git clone --depth 1 -b upgrade/seam-wave1 https://github.com/liuxigreen/peiyin.git E:\peiyin-fresh
Copy-Item -Recurse -Force E:\peiyin-fresh\gpunode\* E:\peiyin-node\
```

**验收**：`E:\peiyin-node\stages\diarize_node.py` 存在且包含 `run_diarize`。

- [ ] TASK-1 完成

## TASK-2 · 装声纹模型（5 分钟）

```powershell
pip install pyannote.audio==3.1 scikit-learn soundfile
```

**HuggingFace token（免费，必须）**：
1. 浏览器开 https://hf.co/settings/tokens → New token → 选 Read
2. 复制 token（hf_ 开头）
3. 写入环境变量（永久）：
```powershell
[System.Environment]::SetEnvironmentVariable("HUGGINGFACE_TOKEN", "hf_你的token", "User")
```

**验证**：
```powershell
python -c "from pyannote.audio import Model; print('pyannote OK')"
```

- [ ] TASK-2 完成

## TASK-3 · 确认 CosyVoice 3 引擎在跑

```powershell
curl http://127.0.0.1:50000/docs -v 2>&1 | findstr "200"
# 或浏览器开 http://127.0.0.1:50000/ 有界面即服务活着
```

没在跑 → 启动它（CosyVoice3 目录，之前装好的）：
```powershell
cd <CosyVoice3安装目录>
python webui.py --port 50000
# 或你之前用的启动命令
```

- [ ] TASK-3 完成（引擎端口 50000 可访问）

## TASK-4 · 重启节点 Worker（1 分钟）

```powershell
# 停掉旧 entrypoint（Ctrl+C 或关窗口），然后：
cd E:\peiyin-node
$env:NODE_WORKERS = "2"
$env:HUGGINGFACE_TOKEN = "hf_你的token"    # 同 TASK-2
$env:CONTROL_URL = "https://dubbing.mulan.dpdns.org"
python entrypoint.py
```

**验收**：输出里有
```
[node] token reused        （或 registered）
[node] worker pool = 2
```

- [ ] TASK-4 完成

## TASK-5 · 等云端派 diarize 任务（自动，约 20 分钟）

云端控制者会 POST 一个 diarize 任务。节点日志会依次出现：
```
[diarize] cut 200/2793 … cut 2793/2793      ← ffmpeg切句 ~10分钟
[diarize] embed 200/2793 … embed 2793/2793  ← 声纹提取 ~5分钟（首次下模型会慢）
[art] uploaded diarize_result.json
[ok] diarize xxxxxxxx
```

**这一步完成后立即通知云端**——后面 LLM 绑定 + 克隆重合成全是云端的事。

- [ ] TASK-5 完成（diarize_result.json 已回传）

## TASK-6 · 克隆重合成（云端派发，节点自动，约 6-8 小时）

云端会重发全剧 tts-batch（每句带角色真实参考音）。节点日志刷 `[ok] tts-generate` 即在干活。
**期间不要关机/断网**，nvidia-smi 可观察显存（ CosyVoice ~5-6G 正常）。

- [ ] TASK-6 完成（云端通知）

## 排障速查

| 症状 | 解法 |
|------|------|
| pyannote 报 token/401 | HUGGINGFACE_TOKEN 没生效：重开 PowerShell 窗口再 echo $env:HUGGINGFACE_TOKEN 验证 |
| 模型下载卡住 | `$env:HF_ENDPOINT = "https://hf-mirror.com"` 后重试 |
| 簇数异常（=1 或 >50） | 不用管，云端会调阈值重跑 |
| ffmpeg not found | 把 ffmpeg.exe 放进 PATH（winget install ffmpeg 或手动） |
| 引擎 500/超时 | 看 CosyVoice 窗口报错；显存不足就重启引擎服务 |
| 节点不出现在云端节点表 | CONTROL_URL 拼错或 NODE_SHARED_SECRET 不对（dev-node-secret） |

---

# 附录：DIARIZE 接入任务（2026-09-04 新增，与上表并行）

# NODE-TASKS-3060（3060 节点专属任务板）

本文件只给 3060 节点 agent。云端/控制面代码由云端 agent 负责，双方通过 git 协作：
做完一项在这打勾提交，云端 agent 会拉取验证。

## 环境事实

- 节点：`E:\peiyin-node`（Windows），本地 entrypoint.py 已是含补丁①~⑤的超集（并发池在位），**勿用云端旧版覆盖**
- 引擎：CosyVoice3 本地 HTTP（127.0.0.1:50000）
- 已修复：系统代理 502 劫持（NO_PROXY）、塌缩检测+cross_lingual→instruct2 回退

## 待办（按优先级）

- [ ] **T1 引擎 ref 缓存**：按 voice_id 缓存参考音频前端提取的 prompt 条件（同一角色几百句复用），省 1-2s/句
- [ ] **T2 模式顺序调换**：默认走 instruct2（有文本条件，稳），干净 refs 再试 cross_lingual——坏 refs 场景省一半 GPU
- [ ] **T3 fp16**：引擎打开 fp16（README 已建议），预期 1.5-2x
- [ ] **T4 批量认领接入**：云端 claim 端点已支持 `GET /api/nodes/me/claim?n=8`（响应含 `tasks` 数组，向后兼容 `task`）。节点主循环改为：带 n=workers 请求 → 对 `tasks` 逐个线程池分发。注意：云端仓库的 `gpunode/entrypoint.py` 有另一条血统的未提交改动，**以本机文件为准合并，勿直接 checkout 覆盖**
- [ ] **T5 验证塌缩回退日志**：跑一批新任务确认 collapse→instruct2 重推→产出≥0.4s

## 协作约定

- 改完在本文件打勾 + commit + push，云端 agent 拉取后在 HANDOVER-B8.md 记录验证结果
- 塌缩垃圾云端会重筛重排队（duration<250ms / 文件<15KB），节点侧不用管
- 云端 git 提交参考：`ef2c47e`（claim 批量）、`777d41b`（tts_node 绕代理——本机已用 NO_PROXY 等效处理，合并时取其代码级写法）
## 测试批等待中（0905）

- [ ] **TASK-7 拉取最新代码**：`git pull origin main`（77d8a1b）——`stages/diarize_node.py` 已含 zh_audio_url 下载通道（此前"attempted relative import"是本机旧版文件，云端已验证 import 结构 OK）
- [ ] **TASK-8 重启节点**（`NODE_MODE=real`），等 DIARIZE/1788539135 任务（前20分钟413句）自动认领
- [ ] **TASK-9 确认**：日志应出现 `[diarize] fetching audio` → 167MB 下载 → `[diarize] embed N/413` → 产物回传
- 首次跑 pyannote 需要 `HUGGINGFACE_TOKEN` 环境变量（下载 embedding 模型）
