import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Crumbs } from '../components/Layout'
import { api } from '../api/client'

const STATUS_ZH: Record<string, string> = {
  completed: '已完成', processing: '处理中', failed: '失败', created: '新建',
  analyzing: '预分析中', analyzed: '已预分析',
}

export default function Projects() {
  const [projects, setProjects] = useState<any[]>([])
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const load = () => {
    api.get<any[]>('/api/projects').then(setProjects).catch(e => setErr(e.message))
  }
  useEffect(() => {
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [])

  const shown = projects.filter(p => !q || p.name.toLowerCase().includes(q.toLowerCase()))

  return (<>
    <Crumbs items={['项目']} />
    <div className="hero-row">
      <h1 className="hero-title">项目</h1>
      <div className="hero-actions">
        <input className="search" placeholder="搜索项目…" value={q}
               onChange={e => setQ(e.target.value)} />
        <Link to="/projects/new" className="btn new-btn">＋ 新建</Link>
      </div>
    </div>

    {err && <div className="badge failed" style={{display:'block', padding:'8px 14px'}}>加载失败: {err}</div>}
    {!err && shown.length === 0 && (
      <div className="empty-state">
        <div className="empty-icon">🎬</div>
        <p>{q ? '没有匹配的项目' : '还没有项目'}</p>
        <Link to="/projects/new" className="btn">创建第一个项目</Link>
      </div>
    )}

    <div className="project-wall">
      {shown.map(p => (
        <Link key={p.id} to={`/projects/${p.id}`} className="project-tile">
          <span className={`dot ${p.status}`} />
          <span className="tile-name">{p.name}</span>
          <span className="tile-status">{STATUS_ZH[p.status] || p.status}</span>
          <span className="tile-arrow">→</span>
        </Link>
      ))}
    </div>
  </>)
}
