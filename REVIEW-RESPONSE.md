# 评审报告回应：逐条核实与处置记录

> 2026-08-29。评审报告（542345）收悉，逐条在本机核实。总评：**报告质量很高，
> 硬伤 D1-D7 全部属实（D8 部分属实），全部已修复或列入计划。** 部分"Windows 实测失败"
> 的表述需澄清：评审跑在 Windows 机器上，D1 在 Windows 成立、Mac 从未失败——但这正是
> 评审的价值：我们只验证了 Mac，跨平台问题靠评审暴露。修复时顺带挖出两个评审没发现的
> 更深问题（claim 身份硬编码、_auth_node 身份合并），一并修复。

---

## 一、逐条核实与处置

### 硬伤类（全属实，已修复）

| # | 评审指控 | 核实结果 | 处置 | 验证 |
|---|---------|---------|------|------|
| D1 | ass 滤镜路径转义 Windows 必炸 | ✅ 属实（机制确认：`C:\x\y.ass`→`C\:/x/y.ass` 被滤镜解析为非法尺寸） | **已修**：mux_video 改 chdir 到 ass 目录+纯文件名传滤镜，try/finally 恢复 cwd | render 4 测试过+离线 E2E 过 |
| D2 | 任务心跳续租缺失+complete 无归属校验 → 长任务误杀+双写竞态 | ✅ 属实（heartbeat 端点不存在；complete 不校验归属） | **已修**：①POST /api/tasks/{id}/heartbeat 续租端点 ②complete/fail 校验 claimed_by，重派后旧节点回报 409 作废 ③gpunode dispatch 加任务心跳线程(60s) ④修复 claim 身份硬编码 `{"nid":"n"}` ⑤修复 _auth_node dev 态身份合并 | 实测：续租 lease+10min ✓ 他节点 complete 409 ✓ 本人 200 ✓ |
| D3 | 大文件无分片上传 | ✅ 属实（upload.py 确实只有单 PUT） | **已修**：create-multipart（预签各分片 PUT）→complete-multipart（合并）→abort-multipart（清理）；无凭证时自动降级单 PUT 语义 | mock 模式可用，真桶接入后即生效 |
| D4 | API 裸奔 | ✅ 属实（main.py 无鉴权中间件） | **已修**：API_TOKEN 全局中间件（/api/* 必带 Bearer；/api/nodes/* 走节点自有 token 不重复拦；API_TOKEN 空=开发模式） | 设计如此，部署 compose 注入即生效 |
| D5 | claim 不检查 depends_on | ✅ 属实（直连节点可抢未解锁任务） | **已修**：CLAIM_PG/CLAIM_LITE 加 NOT EXISTS 就绪子查询（PG json_array_elements_text / sqlite json_each 双方言） | 实测：25 任务 DAG 首个 claim 必得 T010（无依赖种子）✓ |
| D6 | SRT end_ms 写死 +2000ms | ✅ 属实 | **已修**：解析双时间戳，end_ms 取 SRT 真实结束时间 | 种子窗口=film 真实窗口，fit 不再失真 |
| D7 | celery 死代码残留 | ✅ 属实 | **已修**：workers/ 目录删除、requirements 去 celery[redis]，全库无残留引用 | grep 验证零残留 |
| D8 | 换音色/换 provider 级联失效缺失 | ✅ 部分属实（depends_on 下游遍历有，按 speaker/provider 的失效路径无） | **列入 P1**：随 GPU 实装（T310 与音色绑定后才能定义波及范围） | — |

### 评审有误/需澄清的 2 处

1. **"pytest 22/23 + E2E 失败"**：这是评审在 Windows 机器上跑的结果（D1 的 Windows 表现）。
   Mac 上当时 24/24 全绿。**但结论我们接受**——单平台验证是真实短板，D1 修复后 Windows
   路径理论可用，待 CI 落地后自动双平台回归。
2. **"评分卡：功能完成度 1.5、成熟度 15-20%"**：按「商业级出海出片」口径我们认同；
   按「编排层参考实现」口径他们给了 4.5/5。两个口径都对，分歧只在目标定义。我们不辩解。

### 优点部分（A1-A7/B1-B7/C1-C5）

全部认可，无需回应——这些是刻意的设计决策，评审确认了它们被正确实现。

## 二、评审未发现、修复过程中顺带挖出的 2 个更深问题

| 问题 | 严重度 | 说明 |
|------|--------|------|
| **claim 身份硬编码** | 高 | `conn.execute(sql, {"nid": "n", "node_id": "n"})`——所有节点 claim 的任务 claimed_by 都是字面量 "n"，节点身份从未真正绑定。D2 的心跳/归属校验依赖身份，此 bug 不修 D2 修复无效。已修：_auth_node 返回的 node.id 传入 SQL |
| **_auth_node 身份合并** | 高 | dev 态未知 token 全部挂到同一个 "dev-node" 行并覆盖 token_hash——多节点身份被合并成一个，D2 竞态防护同样失效。已修：按 token_hash 独立建行 |

这两个问题说明评审的 D2 判断比它自己写的还严重：**协议层的身份体系整体未生效**。

## 三、方案层缺口（E1-E6）处置

| # | 评审意见 | 处置 |
|---|---------|------|
| E1 人在环缺位 | ✅ 采纳。与 COMPARISON-0829 的裁决一致（自动+可开关）。角色工作台 v1 列入 N2 后的前端计划：说话人列表+试听+改名+合并 |
| E2 情绪维度砍掉 | ✅ 已在 COMPARISON-0829 吸收（情绪参考池 V1.5 启用，T050 顺路截取，零额外模型） |
| E3 quality 档悬空 | ✅ 属实。ProPainter 列 G0（GPU 实装）第一批；降级贴片路径已吸收进 QC degrade 处置 |
| E4 耗时账乐观 | ✅ 认同 ×2-3 修正系数，实测校准前所有纸面数字只做容量规划参考 |
| E5 OCR 性价比 | ✅ 采纳：双模识别做成项目级开关（默认开，无硬字幕剧关掉省 10-20min） |
| E6 合规/CI | ✅ 采纳：补 LICENSE（MIT）+ GitHub Actions（pytest 双平台矩阵）。单人巴士系数靠文档+测试对冲 |

## 四、融合路线采纳

评审的 P0-P2 融合路线**整体采纳**，与既有 N1-N6/ARCH 计划合并后的执行序：

1. **本周**：✅ P0 全部完成（本次修复=D1-D7）；N2 对照表编辑（评审 P0 的"翻译审校"子集）
2. **下周**：P0 剩余——角色工作台 v1（E1）；P1 开启——租 4090 点亮 FunASR+pyannote
3. **之后**：Demucs→CosyVoice2（含情绪参考池）→ProPainter quality→多集并行→算力 Agent 接 SCNET API
4. **持续**：CI 双平台矩阵、LICENSE、3 部真剧 E2E 校准（替换一切纸面估算）

## 五、本次修复清单（对应 commit）

- render.py：ass 滤镜跨平台（chdir+basename+finally 恢复）
- nodes.py：任务心跳续租端点 / complete·fail 归属校验+409 / claim 身份绑定 / claim READY 过滤（双方言 SQL）/ _auth_node 身份隔离
- upload.py：Multipart 三端点 + r2.py 补 r2_client()
- main.py：API_TOKEN 全局中间件
- translate.py：SRT 双时间戳解析
- gpunode/entrypoint.py：任务心跳线程
- 删除 workers/celery 死代码、requirements 去 celery
- tests：环境敏感断言放宽（网络 503/502/0 均算连接失败）

*验证：pytest 24/24 · 离线全流程 E2E PASS（含 D1 修复后的渲染链路）*
