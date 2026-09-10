# 3060 节点受控发布清单

本清单用于将 Windows 3060 节点升级到仓库发布版本。它只发布经过 Git
确认的节点代码和模型；不要使用 NodeJob 下发代码、计划任务或任意命令。

## 发布目标

- 目标提交：由 Mac 端发布者提供；不得使用未提交的工作树。
- 目标能力：`tts,asr,sep,diarize`。
- 目标模型：本地、经 SHA-256 目录摘要校验的
  `speechbrain/spkrec-ecapa-voxceleb` 快照。

## 节点侧步骤

1. 在 `E:\peiyin-node` 记录当前 Git 提交、`entrypoint.py` 的本地差异和当前
   任务状态。若存在运行中的 PipelineTask，等待空闲；不要中断 TTS。
2. 取得指定 Git 提交，仅合并以下仓库文件：

   - `gpunode/node_jobs.py`
   - `gpunode/model_inventory.py`
   - `gpunode/models/manifest.json`
   - `gpunode/stages/diarize_node.py`
   - `gpunode/stages/separate_node.py`
   - `gpunode/stages/engine_manager.py`
   - `gpunode/entrypoint.py`（保留本机的隐藏启动、NO_PROXY 和引擎管家改动；只合并
     NodeJob 空闲轮询及“先上传 artifact、后 complete”的逻辑）

   不要覆盖 `entrypoint.ps1`、计划任务或本机密钥配置。
3. 下载 ECAPA 到节点本地的固定目录（不使用运行时联网下载）。下载完成后，以节点
   的 Python 计算目录摘要：

   ```powershell
   python -c "from gpunode.model_inventory import snapshot_sha256; print(snapshot_sha256(r'E:\peiyin-node\models\ecapa-voxceleb'))"
   ```

   将实际目录和上一步摘要写入 `gpunode/models/manifest.json` 的
   `snapshot_path` 与 `snapshot_sha256`。不得提交或上报模型文件、令牌或绝对路径。
4. 运行本地预检，结果必须为 `ready: true`：

   ```powershell
   python -m gpunode.model_inventory
   ```

5. 将节点启动配置中的 `CAPABILITIES` 更新为 `tts,asr,sep,diarize`，重启现有的
   隐藏常驻启动器。确认不会重新启用旧的每分钟 PowerShell 计划任务。
6. 等待一次心跳后，从 ECS 核对节点能力已经包含 `diarize`。仅随后才提交一个
   `probe` NodeJob，确认紧凑结果返回；不要以 probe 安装模型或代码。

## 发布验收与回滚

验收必须同时满足：节点心跳在线、能力包含 `diarize`、ECAPA 预检通过、引擎管家
仍为隐藏常驻、NodeJob probe 成功。任一条件不满足时，恢复发布前记录的代码和
`CAPABILITIES`，并停止在 canary 前；不得启动白月光全量任务。
