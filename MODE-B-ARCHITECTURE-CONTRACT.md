# Mode B 架构与流程契约

本文以运行中的控制面和 3060 节点为准，取代将 `PipelineTask`、节点本地路径和人工步骤混用的旧流程描述。白月光恢复执行前，以下契约必须成立。

## 1. 边界与唯一真相

| 边界 | 唯一真相 | 禁止事项 |
| --- | --- | --- |
| ECS 控制面 | `PipelineTask` 的状态、依赖、输入指纹和产物描述 | 用节点日志或节点本地文件判断阶段完成 |
| GPU 节点 | 领取、执行、心跳和上传产物 | 将 `E:\\...`、`/tmp/...` 等节点本地路径作为下游输入契约 |
| 产物存储 | 控制面本地 artifact 目录；启用 R2 后改为 R2 key | 将二进制或 base64 写入数据库 JSON |
| NodeJob | 节点健康探针 | 用 NodeJob 下发代码、计划任务或任意 shell 命令 |

任务完成只表示计算结束；只有必需产物已写入控制面持有的存储，才允许解锁下游任务。

## 2. 统一产物描述

每个可跨阶段消费的产物必须保存为以下语义字段：

```json
{
  "key": "vocals",
  "producer_task_id": "...",
  "storage": "controlplane-local",
  "href": "/api/nodes/tasks/.../artifact/vocals",
  "bytes": 1234,
  "sha256": "...",
  "mime_type": "audio/wav"
}
```

`href` 是节点使用 Bearer node token 下载的控制面相对地址。节点可缓存下载结果，但缓存路径不进入数据库，也不传给其他节点。R2 启用后仅将 `storage` 和 `href` 的实现替换为预签名对象地址，调用方不变。

## 3. Mode B 的唯一生产链

```
upload / seed-srt
  → separate-vocals
  → diarize
  → bind-speakers
  → translate + syllable check
  → tts-batch
  → fit + QC
  → package
```

`separate-vocals` 的 `vocals` artifact 已持久化后，控制面才创建 `diarize`；其 payload 只能引用该 artifact 的 `href`。`diarize` 完成后写入聚类 JSON 与推荐参考音；`bind-speakers` 将角色绑定到簇。真实 TTS 只接受已绑定的参考音或显式的备用音色，不能生成占位音频。

每阶段以 `pending → running → completed | dead` 运行。`completed` 后仍等待产物上传的任务不推进下游；上传到齐后由控制面幂等 reconcile。重试只重跑自身和依赖它的下游，不能覆盖已经成功的无关阶段。

## 4. 节点发布与模型准备

节点启动时必须上报：节点版本、已安装 stage、模型版本、模型缓存校验值和可用显存。控制面只把任务派给满足能力的节点。

模型采用显式版本锁和预热检查。`speechbrain/spkrec-ecapa-voxceleb` 必须在执行 diarize 前完成下载和校验；模型不存在时节点报告 `model_unavailable`，任务不应消耗三次运行重试。模型包的安装/升级走受控发布流程，不走 NodeJob。

节点代码发布使用不可变版本包和 manifest：下载、校验、切换、重启、probe。3060 的本机补丁必须回收为仓库提交，禁止长期存在“实机超集代码”。

## 5. 实施门槛

白月光不得恢复执行，直到以下门槛全部通过：

1. 分离 artifact 可由另一个节点经鉴权 URL 下载。
2. 分离完成与 artifact 上传的先后顺序都不会丢失 `payload`、`outputs` 或 `artifacts`。
3. diarize 的任务 payload 不含节点本地绝对路径。
4. 3060 的 ECAPA 模型预热和离线加载检查通过。
5. 20 分钟小样走完分离、聚类、角色绑定和至少一批真实克隆 TTS，并生成 QC 结果。

SQLite 可以继续支撑当前单个控制面和单个 3060；接入第二个长期 GPU 节点或需要多进程控制面时，再迁移 Postgres。生产管理 API 的认证单独纳入上线门槛，不能靠节点 token 代替。
