import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Crumbs } from '../components/Layout'
import { api } from '../api/client'

const STATUS_ZH: Record<string,string> = {
  completed:'已完成', processing:'处理中', failed:'失败', created:'新建',
  analyzing:'预分析中', analyzed:'已预分析',
}
const MODE_ZH: Record<string,string> = { B: '字幕+配音', A: '完整流程' }

export default function Projects() {
  const [projects, setProjects] = useState<any[]>([])
  const [err, setErr] = useState('')
  const nav = useNavigate()
  const load = () => {
    api.get<any[]>('/api/projects').then(setProjects).catch(e => setErr(e.message))
  }
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t) }, [])

  return (<>
    <Crumbs items={['项目']} />
    <h1 className="page-title">项目
      <button className="btn" onClick={() => nav('/projects/new')}>＋ 新建项目</button>
    </h1>
    {err && <div className="badge failed" style={{display:'block', padding:'6px 12px', marginBottom:10}}>加载失败: {err}</div>}
    {!err && projects.length === 0 && <div className="dim">还没有项目——点右上角「＋ 新建项目」开始</div>}
    <div className="card-grid">
      {projects.map(p => (
        <div key={p.id} className="card">
          <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
            <h3><Link to={`/projects/${p.id}`} style={{color:'inherit', textDecoration:'none'}}>{p.name}</Link></h3>
            <span className={`badge ${p.status}`}>{STATUS_ZH[p.status] || p.status}</span>
          </div>
          <div className="dim" style={{marginTop:6}}>
            目标语种 {(p.target_lang||'en').toUpperCase()} · {p.total_segments||0}片段 · {MODE_ZH[(p.config?.mode)] || ''}
          </div>
          <div className="dim" style={{marginTop:6}}>创建于 {String(p.created_at||'').slice(0,16)}</div>
          <div style={{marginTop:12}}>
            <Link to={`/projects/${p.id}`}><button className="btn sm ghost">详情</button></Link>
          </div>
        </div>
      ))}
    </div>
  </>)
}
