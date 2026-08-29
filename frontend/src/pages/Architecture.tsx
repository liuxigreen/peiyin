import { Crumbs } from '../components/Layout'

type NodeDef = {
  id: string
  title: string
  desc: string
  status: 'done' | 'partial' | 'todo'
  badge: string
  col: number
  row: number
}

// 节点状态：done=已实现已测 ✅  partial=骨架/离线版 🔶  todo=待GPU/外部接入 ⬜
const NODES: NodeDef[] = [
  // 第一行：入口与控制面
  { id: 'web', title: '🌐 Web面板', desc: '项目/对照表/Providers/资产 6页面（React18+TS）', status: 'done', badge: '已实现', col: 1, row: 1 },
  { id: 'api', title: '🚪 API网关', desc: 'FastAPI :8500 · 20+端点 · 上传签名/翻译/节点协议', status: 'done', badge: '已实现', col: 2, row: 1 },
  { id: 'orch', title: '🎬 调度Agent', desc: '任务DAG扫描 · 依赖解锁 · 缓存命中跳过', status: 'done', badge: '已实现', col: 3, row: 1 },
  { id: 'reaper', title: '🗡 Reaper', desc: 'lease超时收割 · 节点死亡自动回队重派', status: 'done', badge: '已实现', col: 4, row: 1 },
  { id: 'power', title: '💰 算力Agent', desc: 'GPU队列水位 → 自动开机/关机 · 日预算安全阀（现为干跑模式）', status: 'partial', badge: '离线版', col: 5, row: 1 },
  // 第二行：任务队列 + 三类算力
  { id: 'db', title: '🗄 任务队列', desc: 'SQLite→PG 双方言 · pipeline_tasks = 唯一真理之源', status: 'done', badge: '已实现', col: 2.5, row: 2 },
  { id: 'cpu', title: '💻 CPU池(VPS/Mac)', desc: '翻译五步链 · 字幕生成 · 混音 · QC · 编码', status: 'done', badge: '已实现', col: 4, row: 2 },
  { id: 'gpu', title: '🎮 GPU节点池', desc: 'compshare 4090 按租 · join即入池 · 心跳摘除', status: 'partial', badge: '协议就绪', col: 4.6, row: 2.55 },
  { id: 'io', title: '☁️ 外部API池', desc: '翻译LLM(网页填key) · Confucius4-TTS多语种', status: 'partial', badge: 'mock可测', col: 3.4, row: 2.55 },
  // 第三行：处理阶段
  { id: 'upload', title: '⬆️ 上传·种子', desc: 'SRT→台词→自动起流程（upload-complete一键）', status: 'done', badge: '已实现', col: 1, row: 3 },
  { id: 'recognize', title: '🔍 识别·对齐', desc: 'diarize角色 / FunASR / OCR → 台词库', status: 'todo', badge: '待GPU', col: 2, row: 3 },
  { id: 'translate', title: '🗣 翻译五步链', desc: '上下文包→直译→意译→跨批终检→音节校验', status: 'done', badge: '已实现', col: 3, row: 3 },
  { id: 'tts', title: '🎙 TTS配音', desc: 'CosyVoice2(en) / Confucius4(id·es·pt) 按语种路由', status: 'todo', badge: '待GPU', col: 4, row: 3 },
  { id: 'render', title: '🎞 渲染·交付', desc: '擦字幕 · fit时长 · 混音 · ASS烧录 · loudnorm', status: 'done', badge: '已实现', col: 5, row: 3 },
]

const STATUS_STYLE: Record<NodeDef['status'], { border: string; badge: string }> = {
  done: { border: 'rgba(52,199,123,.5)', badge: 'var(--ok)' },
  partial: { border: 'rgba(245,165,36,.5)', badge: 'var(--warn)' },
  todo: { border: 'rgba(139,147,167,.4)', badge: 'var(--text-dim)' },
}

const FLOWS: { from: string; to: string; label?: string }[] = [
  { from: 'web', to: 'api' },
  { from: 'api', to: 'orch' }, { from: 'api', to: 'reaper' }, { from: 'api', to: 'power' },
  { from: 'orch', to: 'db' }, { from: 'reaper', to: 'db' },
  { from: 'db', to: 'cpu', label: 'cpu/io任务' },
  { from: 'db', to: 'gpu', label: 'gpu任务' },
  { from: 'db', to: 'io', label: 'io任务' },
  { from: 'cpu', to: 'render' },
  { from: 'upload', to: 'recognize' }, { from: 'recognize', to: 'translate' },
  { from: 'translate', to: 'tts' }, { from: 'tts', to: 'render' },
]

export default function Architecture() {
  const gridW = 1100, gridH = 560
  const colX = (c: number) => 40 + (c - 1) * (gridW / 5.4)
  const rowY = (r: number) => 46 + (r - 1) * 165
  const nodeW = 196, nodeH = 108
  const pos = (n: NodeDef) => ({ x: colX(n.col), y: rowY(n.row) })
  const byId = Object.fromEntries(NODES.map(n => [n.id, n]))
  const center = (n: NodeDef) => { const p = pos(n); return { x: p.x + nodeW / 2, y: p.y + nodeH / 2 } }

  return (
    <>
      <Crumbs items={['系统架构']} />
      <h1 className="page-title">系统整体架构
        <span style={{ display: 'flex', gap: 14, fontSize: 12.5, fontWeight: 400, marginLeft: 8 }}>
          <span style={{ color: 'var(--ok)' }}>✅ 已实现已测</span>
          <span style={{ color: 'var(--warn)' }}>🔶 骨架/离线版</span>
          <span style={{ color: 'var(--text-dim)' }}>⬜ 待GPU/外部接入</span>
        </span>
      </h1>

      <div style={{ overflowX: 'auto' }}>
        <svg viewBox={`0 0 ${gridW + 80} ${gridH + 30}`} style={{ minWidth: 980, width: '100%', maxWidth: 1240 }}>
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--text-dim)" />
            </marker>
          </defs>

          {/* 连线 */}
          {FLOWS.map((f, i) => {
            const a = center(byId[f.from]), b = center(byId[f.to])
            const mx = (a.x + b.x) / 2
            const straight = Math.abs(a.x - b.x) < 10 || Math.abs(a.y - b.y) < 10
            const d = straight
              ? `M ${a.x} ${a.y} L ${b.x} ${b.y}`
              : `M ${a.x} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}`
            return (
              <g key={i}>
                <path d={d} fill="none" stroke="var(--border)" strokeWidth="1.6"
                  markerEnd="url(#arrow)" opacity={0.9} />
                {f.label && (
                  <text x={mx} y={(a.y + b.y) / 2 - 6} textAnchor="middle"
                    fill="var(--text-dim)" fontSize="11">{f.label}</text>
                )}
              </g>
            )
          })}

          {/* 节点 */}
          {NODES.map(n => {
            const p = pos(n)
            const st = STATUS_STYLE[n.status]
            return (
              <g key={n.id}>
                <rect x={p.x} y={p.y} width={nodeW} height={nodeH} rx={12}
                  fill="var(--bg2)" stroke={st.border} strokeWidth={1.4} />
                <text x={p.x + 14} y={p.y + 26} fontSize="14.5" fontWeight="700" fill="var(--text)">
                  {n.title}
                </text>
                <rect x={p.x + nodeW - 62} y={p.y + 12} width={50} height={18} rx={9}
                  fill="transparent" stroke={st.badge} strokeWidth={1} opacity={0.75} />
                <text x={p.x + nodeW - 37} y={p.y + 25} fontSize="10.5" textAnchor="middle"
                  fill={st.badge}>{n.badge}</text>
                {n.desc.split(' · ').map((line, li) => (
                  <text key={li} x={p.x + 14} y={p.y + 48 + li * 17} fontSize="11.5"
                    fill="var(--text-dim)">{line}</text>
                ))}
              </g>
            )
          })}
        </svg>
      </div>

      {/* 当前进度 */}
      <div className="card" style={{ marginTop: 18 }}>
        <h3>📍 当前进度（下一步做什么）</h3>
        <table className="tbl" style={{ marginTop: 10 }}>
          <thead><tr><th>阶段</th><th>状态</th><th>说明</th></tr></thead>
          <tbody>
            <tr><td><b>已完成的里程碑</b></td><td><span className="badge completed">✅ 全部验证通过</span></td>
              <td>编排器DAG+断点续传 · 五步翻译链 · 一键引导 · 渲染(擦字幕/fit/混音/烧字幕/loudnorm) · QC Agent · 算力Agent(干跑) · 23测试全绿 + 离线全流程E2E PASS</td></tr>
            <tr><td><b>当前所在步骤</b></td><td><span className="badge running">🔶 离线联调收尾</span></td>
              <td>零外部依赖全流程已跑通（SRT→成片+QC报告，¥0）。补N2对照表前端编辑，即为「租卡前完整版」</td></tr>
            <tr><td>接入真实翻译API</td><td><span className="badge pending">等你</span></td>
              <td>设置→Providers 网页填 key → 点「测试连通」→ 自动从mock切真实翻译</td></tr>
            <tr><td>GPU实装（真配音）</td><td><span className="badge pending">待租卡</span></td>
              <td>租4090→join入池→CosyVoice2/FunASR/Demucs逐个点亮，预计¥4.3/部</td></tr>
            <tr><td>云端部署（全公司可用）</td><td><span className="badge pending">待拍板</span></td>
              <td>Mac已能跑；要24h在线再上VPS（CLOUD-DEPLOY.md 一条命令迁移）</td></tr>
          </tbody>
        </table>
      </div>

      <div className="dim" style={{ marginTop: 14 }}>
        设计文档：V3-FINAL.md（总纲）· ARCH-V3.1.md（多Agent/GPU细节/字幕规范）· OFFLINE-PLAN.md（离线联调）· CLOUD-DEPLOY.md（云端迁移）
      </div>
    </>
  )
}
