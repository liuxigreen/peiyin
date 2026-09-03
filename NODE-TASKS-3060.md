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
