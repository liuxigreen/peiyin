# 3060 节点 Tailscale + SSH 配置清单（执行人：3060 侧 agent）

目标：让 ECS（阿里云 control plane）能 SSH 直连这台 Windows，实现云端直接运维节点。
完成后把 3 项信息填到本文档末尾，commit + push。

## 步骤（管理员 PowerShell）

### 1. 安装并登录 Tailscale
```powershell
winget install tailscale.tailscale
tailscale up
```
- 会弹出浏览器登录（Google/GitHub 账号）。**登录这一步可能需要代理**——一次性，登录完日常通信不走国外
- 若无代理可用：下载安装包 https://tailscale.com/install.sh 不可用时，直接下 MSI：https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi

### 2. 记录本机 Tailscale IP
```powershell
tailscale ip -4
# 输出形如 100.x.y.z，记下来
```

### 3. 开启 OpenSSH 服务端
```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic
# 防火墙放行（Tailscale 网段）
New-NetFirewallRule -DisplayName "SSH-Tailscale" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 -RemoteAddress 100.64.0.0/10
```

### 4. 确认 SSH 可登录的账号
- 使用当前 Windows 管理员账号的用户名+密码即可（或之后由云端配公钥免密）
- 微软账号登录的机器：SSH 用户名用邮箱前缀或 `MicrosoftAccount\邮箱`

### 5. 回填信息（commit 前）
```
TAILSCALE_IP=100.x.y.z
SSH_USER=<windows用户名>
SSH_PORT=22
（密码建议不要写在 git 里；云端配公钥后可删除本行占位）
```

## 云端侧（ECS agent 做，3060 不用管）
- ECS 安装 tailscale 并加入同一账号（auth key 或重新登录）
- ssh 密钥分发 + 免密验证
- 验证：`ssh <user>@<100.x.y.z> "nvidia-smi"`

## 安全边界
- SSH 仅监听 Tailscale 网段（100.64.0.0/10），公网不可达
- Tailscale 设备列表里只保留必要机器，多余设备及时移除
