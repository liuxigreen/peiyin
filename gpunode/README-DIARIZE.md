# 3060 节点更新：声纹聚类（diarize）接入指南

> 目的：让配音从"6条预设音色"升级为"**每个角色自己的原片声线克隆**"。
> 白月光实测：现在 2743 句里 1193 句共用一个女声——多角色剧听感全串。克隆链路是解。

## 一、要装什么（全新命令，复制即用）

```powershell
# Windows PowerShell（管理员）在 E:\peiyin-node 目录下：

# 1. 节点代码更新（含新 diarize stage）
git -C E:\peiyin-node pull
# 或者节点目录不是 git 的话：重新下载整个 gpunode 覆盖
# git clone https://github.com/liuxigreen/peiyin.git 并复制 gpunode/ 到 E:\peiyin-node

# 2. pyannote（声纹模型）
pip install pyannote.audio==3.1
# 首次跑会自动下载 pyannote/embedding 模型（~100MB）

# 3. HuggingFace token（pyannote 模型下载需要，免费）
# 去 https://hf.co/settings/tokens 建一个 read token
$env:HUGGINGFACE_TOKEN = "hf_xxxxxxxxxxxxxxxx"

# 4. sklearn（聚类用，requirements 里可能有）
pip install scikit-learn

# 5. 重启节点（保持 NODE_WORKERS=2）
$env:NODE_WORKERS = "2"
python entrypoint.py
# 看到 [node] worker pool = 2 即成功
```

**可选但强烈建议**：CosyVoice 3 服务保持运行（端口 50000）——diarize 完成后马上要跑克隆重合成。

## 二、跑起来之后的流程（云端我这边操作，你不用动）

```
1. 我在云端 POST /projects/{pid}/mode-b/diarize
   → 任务下发：整条 zh_audio.mp3 下载链接 + 2793 句的窗口清单
2. 3060 节点自动领取 → ffmpeg 切出 2793 句中文参考音（~10分钟）
   → pyannote 声纹提取（~5分钟） → 聚类（~1分钟）
3. 产物 diarize_result.json 回传云端：每句→簇号 + 每簇推荐参考音
4. 我在云端做 LLM 簇→角色绑定（一次调用）
5. 重发全剧 tts-batch（带每簇真实参考音）
   → CosyVoice3 cross_lingual：中文原声 → 英文台词，角色音色=原片演员
```

## 三、验证标准

- diarize_result.json 里簇数应该在 8-20 之间（白月光主配角+龙套）
- 最大簇 = 女主（林惜音 ~780 句），第二簇 = 男主（楚弋 ~500 句）
- 每簇推荐参考音 SNR > 10dB（太低的参考音克隆会糊）

## 四、排障

| 症状 | 解法 |
|------|------|
| pyannote 报 HF token 错误 | $env:HUGGINGFACE_TOKEN 没设置，或 token 无 read 权限 |
| ffmpeg 切句很慢 | 正常，2793 句约 8-12 分钟（每句 ~0.2s） |
| 簇数=1 或 =100 | 距离阈值 0.7 不适配这个剧 → 告诉我，我调 0.6/0.8 |
| 显存不够 | diarize 用完即释；跑之前确认 CosyVoice 服务在但空闲 |
