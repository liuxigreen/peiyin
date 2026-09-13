# 3060 受控发布通道

此通道一次安装稳定的 `release_channel.py` supervisor，以后从受信任的 HTTPS 发布源取得带 HMAC-SHA256 签名的 manifest。它不修改现有节点入口、模型、工作目录或令牌。

## 一次性安装（管理员在 3060 上执行）

解压 `node-release-channel-bootstrap-v1.zip` 后，只需提供既有控制面地址、本机既有节点入口和原来的隐藏常驻任务名：

```powershell
.\gpunode\scripts\install_release_channel.ps1 -ControlPlaneUrl 'https://control.example' -EntryPointPath 'E:\peiyin-node\gpunode\entrypoint.py' -InstallRoot 'E:\peiyin-node\release-channel' -ResidentTaskName 'ExistingHiddenNodeTask'
```

不再人工传入 manifest 地址、允许主机列表或 HMAC key。supervisor 复用节点既有 Bearer token，从控制面一次性 bootstrap 接口取得受限发布地址和仅在内存使用的签名密钥；密钥不会写入配置、日志或 bootstrap。

控制面只会向已明确允许的节点 ID 返回 bootstrap，接口响应禁止缓存。网络或 bootstrap 暂不可用时，supervisor 继续运行本机既有 `0.0.0` 入口并在下一轮重试，不会停止现有节点服务。

安装器备份同一个既有隐藏 Scheduled Task 定义、停止旧实例，以 sibling staging 原子替换 supervisor 后复用原任务名；任何失败都会恢复任务定义并重启旧 resident。bootstrap 内含 `bootstrap/current.json` 与 `bootstrap/releases/0.0.0/.release-meta.json` 空 overlay。请在维护窗口执行；本变更没有执行真实部署、下载、计划任务或节点操作。

## 构包、签名和发布

首包固定且仅包含以下 12 个 overlay 文件：
`gpunode/node_jobs.py`、`gpunode/legacy_artifact_backfill.py`、`gpunode/model_inventory.py`、`gpunode/stages/__init__.py`、`gpunode/stages/demo.py`、`gpunode/stages/diarize_node.py`、`gpunode/stages/engine_manager.py`、`gpunode/stages/offline.py`、`gpunode/stages/real_cpu.py`、`gpunode/stages/router.py`、`gpunode/stages/separate_node.py`、`gpunode/stages/tts_node.py`。

发布者在隔离目录中只列出经审阅的 payload 文件，构包器拒绝 `entrypoint.py`、`workdir`、`models` 和名称包含 token/key/secret 的路径：

```bash
python gpunode/scripts/build_node_release.py --source-root . --output-dir out --version 1.2.3 --source-revision <commit> --package-url https://releases.example/node-release-1.2.3.zip --hmac-key-file /secure/release-hmac.key --include gpunode/node_jobs.py
```

将 zip 和 manifest 上传到 allowlist 中的 HTTPS 主机。supervisor 先验 HMAC、包 SHA-256 和逐文件 SHA-256，再安全解压到不可变的 `releases/<version>`。

首次发布使用 `--first-payload` 代替 `--include`；两者不能组合。前置条件是本机既有 entrypoint、workdir、models、token 与依赖均已存在且受保护，控制面 release-state 端点已部署，minimum 版本尚未激活。overlay 不安装依赖或模型。本轮不产生真实 key 或发布包。

## 切换、回滚与离线验证

更新前 supervisor 通过 `POST /api/nodes/me/release-state` 报告 draining，并轮询 `GET /api/nodes/me/release-switch-ready`；存在任何运行中的 PipelineTask 或 NodeJob 时不切换。就绪后使用同目录临时文件和 `os.replace` 更新 `current.json`。新隐藏子进程在健康窗口内退出会原子恢复 previous pointer，隐藏重启旧版并报告旧版 ready。

离线检查：`python -m pytest gpunode/tests/test_release_channel.py`，随后计算 bootstrap SHA-256 并与伴随 `.sha256` 比较。测试使用注入 transport、时钟、sleep 和进程工厂，不会联网或启动真实进程。
