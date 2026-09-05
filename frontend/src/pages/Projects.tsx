import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { IcPlus, IcFilm } from '../components/Icons'

const STATUS_ZH: Record<string, string> = {
  completed: '已完成', processing: '处理中', failed: '失败', created: '待翻译',
  analyzing: '预分析中', analyzed: '已预分析',
}

type Prog = { percent: number } | undefined

export default function Projects() {
  const [projects, setProjects] = useState<any[]>([])
  const [prog, setProg] = useState<Record<string, Prog>>({})
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const ps = await api.get<any[]>('/api/projects')
        if (!alive) return
        setProjects(ps); setErr('')
        // 每个项目拉一次进度（项目量小，串行 pooled）
        const entries = await Promise.all(ps.map(async p => {
          try {
            const g = await api.get<{ percent: number }>(`/api/projects/${p.id}/progress`)
            return [p.id, g] as const
          } catch { return [p.id, undefined] as const }
        }))
        if (alive) setProg(Object.fromEntries(entries))
      } catch (e: any) { if (alive) setErr(e.message) }
    }
    load()
    const t = setInterval(load, 10000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  // 可交付状态聚合（0906审计P1）：生成≠可交付，制作人员一眼看到下一步
  const [dstat, setDstat] = useState<Record<string, any>>({})
  useEffect(() => {
    let alive = true
    const loadD = async () => {
      const entries = await Promise.all(projects.map(async p => {
        try {
          const d = await api.get<any>(`/api/projects/${p.id}/deliverable-status`)
          return [p.id, d] as const
        } catch { return [p.id, undefined] as const }
      }))
      if (alive) setDstat(Object.fromEntries(entries))
    }
    if (projects.length) loadD()
    const t = setInterval(loadD, 10000)
    return () => { alive = false; clearInterval(t) }
  }, [projects])

  const shown = projects.filter(p => !q || p.name.toLowerCase().includes(q.toLowerCase()))
  const kpi = {
    total: projects.length,
    running: projects.filter(p => ['processing', 'analyzing'].includes(p.status)).length,
    done: projects.filter(p => p.status === 'completed').length,
    failed: projects.filter(p => p.status === 'failed').length,
  }

  return (<>
    <div className="page-head">
      <div>
        <h1 className="hero-title">项目工作台</h1>
        <div className="page-sub">短剧出海译配 · 模式B（字幕+配音）主航道</div>
      </div>
      <div className="page-actions">
        <div style={{ position: 'relative' }}>
          <input className="search" placeholder="搜索项目…" value={q}
                 onChange={e => setQ(e.target.value)} />
        </div>
        <Link to="/projects/new" className="btn"><IcPlus />新建项目</Link>
      </div>
    </div>

    {err && <div className="alert-box">加载失败：{err}</div>}

    <div className="kpi-row">
      <div className="kpi"><div className="kpi-label"><IcFilm />项目总数</div><div className="kpi-value acc">{kpi.total}</div></div>
      <div className="kpi"><div className="kpi-label">进行中</div><div className="kpi-value">{kpi.running}</div></div>
      <div className="kpi"><div className="kpi-label">已完成</div><div className="kpi-value ok">{kpi.done}</div></div>
      <div className="kpi"><div className="kpi-label">失败</div><div className="kpi-value bad">{kpi.failed}</div></div>
    </div>

    {!err && shown.length === 0 && (
      <div className="empty-state">
        <div className="empty-icon">🎬</div>
        <p>{q ? '没有匹配的项目' : '还没有项目——上传第一部分字幕，开始译配'}</p>
        <Link to="/projects/new" className="btn">创建第一个项目</Link>
      </div>
    )}

    <div className="project-wall">
      {shown.map(p => {
        const pct = prog[p.id]?.percent ?? 0
        const d = dstat[p.id]
        return (
          <Link key={p.id} to={`/projects/${p.id}`} className="project-tile">
            <div className="tile-top">
              <span className="tile-name">{p.name}</span>
              <span className={`dot ${p.status}`} />
            </div>
            {d && (
              <div style={{ fontSize: 12.5, margin: '6px 0' }}>
                {d.deliverable
                  ? <span className="badge completed">✅ 可交付</span>
                  : <span className="badge">{d.next_action}</span>}
              </div>
            )}
            <div className="tile-meta">
              <span>{STATUS_ZH[p.status] || p.status}</span>
              <span>→ {String(p.target_lang || 'en').toUpperCase()}</span>
              {d && <span style={{ fontFamily: 'var(--mono)' }}>配音 {d.generated}/{d.total}</span>}
              <span style={{ marginLeft: 'auto', fontFamily: 'var(--mono)' }}>{pct}%</span>
            </div>
            <div className="tile-bar"><div style={{ width: `${pct}%` }} /></div>
            {d && (d.missing > 0 || d.over_limit > 0 || d.over_slot > 0) && (
              <div style={{ fontSize: 11.5, color: 'var(--warn, #e8b339)', marginTop: 4 }}>
                {d.missing > 0 && <span>缺音频 {d.missing}　</span>}
                {d.over_limit > 0 && <span>超长 {d.over_limit}　</span>}
                {d.over_slot > 0 && <span>超窗 {d.over_slot}</span>}
              </div>
            )}
          </Link>
        )
      })}
    </div>
  </>)
}
