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
  { id: 'web', title: '🌐 Web面板', desc: '项目/对照表/Providers/资产 · 深色工作台UI', status: 'done', badge: '已实现', col: 1, row: 1 },
  { id: 'api', title: '🚪 API网关', desc: 'FastAPI :8500 · 30+端点 · 上传签名/翻译/节点协议', status: 'done', badge: '已实现', col: 2, row: 1 },
  { id: 'orch', title: '🎬 调度Agent', desc: '任务DAG扫描 · 依赖解锁 · 缓存命中跳过', status: 'done', badge: '已实现', col: 3, row: 1 },
  { id: 'reaper', title: '🗡 Reaper', desc: 'lease超时收割 · 节点死亡自动回队重派', status: 'done', badge: '已实现', col: 4, row: 1 },
  { id: 'power', title: '💰 算力Agent', desc: 'GPU队列水位 → 自动开关机 · 日预算安全阀（干跑）', status: 'partial', badge: '离线版', col: 5, row: 1 },
  // 第二行：任务队列 + 三类算力
  { id: 'db', title: '🗄 任务队列', desc: 'SQLite→PG 双方言 · pipeline_tasks 唯一真理之源', status: 'done', badge: '已实现', col: 2.5, row: 2 },
  { id: 'cpu', title: '💻 CPU池', desc: '翻译五步链 · 字幕生成 · 混音 · QC · 交付打包', status: 'done', badge: '已实现', col: 4, row: 2 },
  { id: 'gpu', title: '🎮 GPU节点池', desc: '3060已接入(CosyVoice3+Demucs) · 4090按需租 · 心跳摘除', status: 'done', badge: '已接入', col: 4.6, row: 2.55 },
  { id: 'io', title: '☁️ 外部API池', desc: '翻译LLM(网页填key) · Confucius4-TTS多语种', status: 'partial', badge: '可扩展', col: 3.4, row: 2.55 },
  // 第三行：处理阶段
  { id: 'upload', title: '⬆️ 上传·种子', desc: 'SRT→台词→自动起流程（upload-complete一键）', status: 'done', badge: '已实现', col: 1, row: 3 },
  { id: 'recognize', title: '🔍 识别·对齐', desc: 'diarize声纹聚类 / FunASR / OCR → 台词库', status: 'todo', badge: '待实装', col: 2, row: 3 },
  { id: 'translate', title: '🗣 翻译五步链', desc: '句特征路由→直译→意译→终检→音节校验', status: 'done', badge: '已实现', col: 3, row: 3 },
  { id: 'tts', title: '🎙 TTS配音', desc: '3060 CosyVoice3 实测中 · 按语种路由 · 产物回传', status: 'done', badge: '实测通过', col: 4, row: 3 },
  { id: 'render', title: '🎞 渲染·交付', desc: '擦字幕 · fit时长 · 混音 · ASS烧录 · 交付包', status: 'done', badge: '已实现', col: 5, row: 3 },
]

const STATUS_STYLE: Record<NodeDef['status'], { border: string; badge: string }> = {
  done: { border: 'rgba(61,214,140,.45)', badge: '#3dd68c' },
  partial: { border: 'rgba(255,178,36,.45)', badge: '#ffb224' },
  todo: { border: 'rgba(148,163,198,.35)', badge: '#98a2b8' },
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
      <div className="page-head">
        <div>
          <h1 className="page-title">系统整体架构</h1>
          <div className="page-sub" style={{ display: 'flex', gap: 14 }}>
            <span style={{ color: 'var(--ok)' }}>✅ 已实现已测</span>
            <span style={{ color: 'var(--warn)' }}>🔶 骨架/离线版</span>
            <span className="dim">⬜ 待接入</span>
          </div>
        </div>
      </div>

      <div className="panel" style={{ overflowX: 'auto' }}>
        <svg viewBox={`0 0 ${gridW + 80} ${gridH + 30}`} style={{ minWidth: 980, width: '100%' }}>
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#5d6678" />
            </marker>
          </defs>

          {FLOWS.map((f, i) => {
            const a = center(byId[f.from]), b = center(byId[f.to])
            const mx = (a.x + b.x) / 2
            const straight = Math.abs(a.x - b.x) < 10 || Math.abs(a.y - b.y) < 10
            const d = straight
              ? `M ${a.x} ${a.y} L ${b.x} ${b.y}`
              : `M ${a.x} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x} ${b.y}`
            return (
              <g key={i}>
                <path d={d} fill="none" stroke="rgba(148,163,198,.25)" strokeWidth="1.6"
                      markerEnd="url(#arrow)" opacity={0.9} />
                {f.label && (
                  <text x={mx} y={(a.y + b.y) / 2 - 6} textAnchor="middle"
                        fill="#98a2b8" fontSize="11">{f.label}</text>
                )}
              </g>
            )
          })}

          {NODES.map(n => {
            const p = pos(n)
            const st = STATUS_STYLE[n.status]
            return (
              <g key={n.id}>
                <rect x={p.x} y={p.y} width={nodeW} height={nodeH} rx={12}
                      fill="#121722" stroke={st.border} strokeWidth={1.4} />
                <text x={p.x + 14} y={p.y + 26} fontSize="14.5" fontWeight="700" fill="#e9edf6">
                  {n.title}
                </text>
                <rect x={p.x + nodeW - 62} y={p.y + 12} width={50} height={18} rx={9}
                      fill="transparent" stroke={st.badge} strokeWidth={1} opacity={0.75} />
                <text x={p.x + nodeW - 37} y={p.y + 25} fontSize="10.5" textAnchor="middle"
                      fill={st.badge}>{n.badge}</text>
                {n.desc.split(' · ').map((line, li) => (
                  <text key={li} x={p.x + 14} y={p.y + 48 + li * 17} fontSize="11.5"
                        fill="#98a2b8">{line}</text>
                ))}
              </g>
            )
          })}
        </svg>
      </div>

      <div className="panel" style={{ marginTop: 18 }}>
        <b>📍 当前进度</b>
        <table className="tbl" style={{ marginTop: 10 }}>
          <thead><tr><th>阶段</th><th>状态</th><th>说明</th></tr></thead>
          <tbody>
            <tr><td><b>控制面 + 翻译链</b></td><td><span className="badge completed">✅ 全量验证</span></td>
              <td>DAG编排+断点续传 · 五步翻译链（句特征路由/压缩终检） · 44测试全绿 · 白月光2803句真实翻译</td></tr>
            <tr><td><b>3060节点 · TTS</b></td><td><span className="badge running">🔶 实测中</span></td>
              <td>CosyVoice3 真配音 · 批量任务+产物回传+交付包全链路已通 · 平均 3.1s/句</td></tr>
            <tr><td>说话人绑定</td><td><span className="badge pending">待实装</span></td>
              <td>pyannote 声纹聚类 → LLM绑定角色 → 角色化音色（DESIGN-B-配音方案-v2）</td></tr>
            <tr><td>租用GPU扩容</td><td><span className="badge pending">方案已备</span></td>
              <td>compshare 4090 ¥1.94/h · join即入池 · 自动开关机 · 见 GPU-RENTAL-PLAN.md</td></tr>
            <tr><td>模式A全流程</td><td><span className="badge pending">待GPU</span></td>
              <td>擦字幕/分离/ASR/混音/烧录 逐阶段点亮，预计 ¥4.3/部</td></tr>
          </tbody>
        </table>
      </div>

      <div className="dim" style={{ marginTop: 14, fontSize: 12.5 }}>
        设计文档：V3-FINAL.md（总纲）· ARCH-V3.1.md（多Agent/GPU细节）· MODE-B-DESIGN.md（模式B）· GPU-RENTAL-PLAN.md（租卡方案）· UPGRADE-PROPOSALS.md（升级提案）
      </div>
    </>
  )
}
