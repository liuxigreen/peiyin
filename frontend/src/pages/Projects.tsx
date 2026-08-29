import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, USE_MOCK } from '../api/client'
import { mockProjects } from '../api/mock'

const STATUS_ZH: Record<string,string> = {
  completed:'已完成', tts_generating:'TTS配音中', translating:'翻译中', mixing:'混音中',
  failed:'失败', analyzed:'已预分析', pre_analyzing:'预分析中', created:'新建',
}

export default function Projects() {
  const [projects, setProjects] = useState<any[]>([])
  const nav = useNavigate()
  useEffect(() => {
    if (USE_MOCK) { setProjects(mockProjects); return }
    api.get<any[]>('/api/projects').then(setProjects)
  }, [])
  return (<>
    <h1 className="page-title">项目
      <button className="btn" onClick={() => nav('/projects/new')}>＋ 新建项目</button>
    </h1>
    <div className="card-grid">
      {projects.map(p => (
        <div key={p.id} className="card">
          <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
            <h3><Link to={`/projects/${p.id}`} style={{color:'inherit', textDecoration:'none'}}>{p.name}</Link></h3>
            <span className={`badge ${p.status}`}>{STATUS_ZH[p.status] || p.status}</span>
          </div>
          <div className="progress"><div style={{width: `${p.progress}%`}} /></div>
          <div className="dim">{p.progress}% · 角色{p.speakers}个 · 目标语种 {p.lang.toUpperCase()} · {(p.size_mb/1024).toFixed(1)}GB</div>
          <div className="dim" style={{marginTop:6}}>创建于 {p.created}</div>
          <div style={{marginTop:12, display:'flex', gap:8}}>
            <Link to={`/projects/${p.id}`}><button className="btn sm ghost">详情</button></Link>
            {p.status === 'completed' && (
              p.id === 'p1'
                ? <a href="/api/download/demo.mp4" download><button className="btn sm">下载成片</button></a>
                : <button className="btn sm" disabled>成片生成中</button>
            )}
          </div>
        </div>
      ))}
    </div>
  </>)
}
