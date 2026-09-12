# 3060 受控发布通道

此通道一次安装稳定的 `release_channel.py` supervisor，以后从受信任的 HTTPS 发布源取得带 HMAC-SHA256 签名的 manifest。它不修改现有节点入口、模型、工作目录或令牌。

## 安装（管理员在 3060 上执行）

解压 `node-release-channel-bootstrap-v1.zip`，使用管理员提供的控制面 URL、manifest URL、允许主机列表和一次性 HMAC key 文件运行：

```powershell
.\gpunode\scripts\install_release_channel.ps1 -ControlPlaneUrl 'https://control.example' -ManifestUrl 'https://releases.example/node.manifest.json' -AllowedHost 'releases.example' -HmacKeyFile 'D:\secure\release-hmac.key' -EntryPointPath 'E:\peiyin-node\gpunode\entrypoint.py' -InstallRoot 'E:\peiyin-node\release-channel' -ResidentTaskName 'ExistingHiddenNodeTask'
```

密钥内容不写入配置、日志或 bootstrap；配置只保存管理员提供的受保护 key file 路径。安装器备份同一个既有隐藏 Scheduled Task 定义、停止旧实例，以 sibling staging 原子替换 supervisor 后复用原任务名；任何失败都会恢复任务定义并重启旧 resident。bootstrap 内含 `bootstrap/current.json` 与 `bootstrap/releases/0.0.0/.release-meta.json` 空 overlay，网络不可用时仍让本地既有入口运行。请在维护窗口执行；本变更没有执行真实部署、下载、计划任务或节点操作。

## 构包、签名和发布

发布者在隔离目录中只列出经审阅的 payload 文件，构包器拒绝 `entrypoint.py`、`workdir`、`models` 和名称包含 token/key/secret 的路径：

```bash
python gpunode/scripts/build_node_release.py --source-root . --output-dir out --version 1.2.3 --source-revision <commit> --package-url https://releases.example/node-release-1.2.3.zip --hmac-key-file /secure/release-hmac.key --include gpunode/node_jobs.py
```

将 zip 和 manifest 上传到 allowlist 中的 HTTPS 主机。supervisor 先验 HMAC、包 SHA-256 和逐文件 SHA-256，再安全解压到不可变的 `releases/<version>`。

## 切换、回滚与离线验证

更新前 supervisor 通过 `POST /api/nodes/me/release-state` 报告 draining，并轮询 `GET /api/nodes/me/release-switch-ready`；存在任何运行中的 PipelineTask 或 NodeJob 时不切换。就绪后使用同目录临时文件和 `os.replace` 更新 `current.json`。新隐藏子进程在健康窗口内退出会原子恢复 previous pointer，隐藏重启旧版并报告旧版 ready。

离线检查：`python -m pytest gpunode/tests/test_release_channel.py`，随后计算 bootstrap SHA-256 并与伴随 `.sha256` 比较。测试使用注入 transport、时钟、sleep 和进程工厂，不会联网或启动真实进程。
