import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Crumbs } from '../components/Layout'
import { PHASES, mockProjectDetail } from '../api/mock'
import { api, USE_MOCK } from '../api/client'

type Progress = {
  percent: number
  phases: Record<string, { total: number; done: number; failed: number }>
  counts: Record<string, number>
  recent_tasks?: { key: string; type: string; status: string }[]
}
const PHASE_KEYS = ['pre','subtitle','separate','recognize','translate','tts','mix','stitch']

export default function ProjectDetail() {
  const { id } = useParams()
  const [proj, setProj] = useState<any>(null)
  const [prog, setProg] = useState<Progress | null>(null)
  useEffect(() => {
    if (USE_MOCK) { setProj(mockProjectDetail(id || 'p2')); return }
    api.get<any>(`/api/projects/${id}`).then(setProj).catch(() => {})
    const load = () => api.get<Progress>(`/api/projects/${id}/progress`).then(setProg).catch(() => {})
    load()
    const t = setInterval(load, 3000)   // 编排器每秒推进, 前端3s轮询
    return () => clearInterval(t)
  }, [id])

  async function retryTask(taskId: string) {
    await api.post(`/api/tasks/${taskId}/retry`)
    const p = await api.get<Progress>(`/api/projects/${id}/progress`)
    setProg(p)
  }

  if (!proj) return null
  const phases = prog?.phases
  const phaseIdx = PHASE_KEYS.findIndex(k => {
    const ph = phases?.[k]
    return !ph || ph.done < ph.total      // 第一个未全完成的阶段=当前阶段
  })
  const doneCount = proj.segments.filter((s:any) => s.status==='done').length
  return (<>
    <Crumbs items={['项目', proj.name]} />
    <h1 className="page-title">{proj.name}
      <span className={`badge ${proj.status}`}>{proj.status}</span>
      <span className="dim">{doneCount}/{proj.segments.length} 切片</span>
    </h1>

    <div className="stepper">
      {PHASES.map((ph, i) => {
        const phData = phases?.[PHASE_KEYS[i]]
        const st = !phData ? '' : phData.done >= phData.total && phData.total > 0 ? ' done'
                 : phData.failed > 0 ? ' failed' : i === phaseIdx ? ' current' : ''
        return (
          <div key={ph} className={'step' + (st || (i < (phaseIdx<0?99:phaseIdx) ? ' done' : i === phaseIdx ? ' current' : ''))}>
            {ph}
            {phData && phData.total > 0 && (
              <span className="dim" style={{marginLeft:4, fontSize:11}}>
                {phData.done}/{phData.total}{phData.failed > 0 ? ` ⚠${phData.failed}` : ''}
              </span>
            )}
          </div>
        )
      })}
    </div>
    {prog && (
      <div className="panel" style={{marginBottom:14}}>
        <b>流水进度 {prog.percent}%</b>
        <span className="dim" style={{marginLeft:12}}>
          完成{prog.counts.completed} · 运行中{prog.counts.running} · 排队{prog.counts.queued}
          {prog.counts.failed > 0 ? ` · 失败${prog.counts.failed}` : ''}
          {prog.counts.dead > 0 ? ` · 卡死${prog.counts.dead}` : ''}
        </span>
        {prog.recent_tasks?.filter(t => t.status === 'failed' || t.status === 'dead').length! > 0 && (
          <table className="tbl" style={{marginTop:10}}>
            <thead><tr><th>失败任务</th><th>状态</th><th></th></tr></thead>
            <tbody>
              {prog.recent_tasks!.filter(t => t.status === 'failed' || t.status === 'dead')
                .map(t => (
                <tr key={t.key}>
                  <td>{t.key}</td>
                  <td><span className={`badge ${t.status}`}>{t.status}</span></td>
                  <td><button className="btn danger sm" onClick={() => retryTask(t.key)}>重试</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    )}

    <div className="panel">
      <div style={{display:'flex', justifyContent:'space-between', marginBottom:10}}>
        <b>切片流水 ({proj.segments.length})</b>
        <Link to={`/projects/${id}/text`} className="dim">台词对照表 →</Link>
      </div>
      <table className="tbl">
        <thead><tr><th>切片</th><th>时间范围</th><th>状态</th><th>耗时</th><th>失败原因</th><th></th></tr></thead>
        <tbody>
          {proj.segments.map((s:any) => (
            <tr key={s.seg_id} style={s.status==='failed' ? {background:'rgba(244,84,79,.06)'} : undefined}>
              <td>{s.seg_id}</td>
              <td className="dim">{s.range}</td>
              <td><span className={`badge ${s.status}`}>{s.status}</span></td>
              <td className="dim">{s.status==='done' ? `${s.duration_s}s` : '-'}</td>
              <td className="val-hot" title={s.error}>{s.error ? s.error.slice(0, 42) + '…' : ''}</td>
              <td>{s.status==='failed' && <button className="btn danger sm">重试</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>

    <div className="panel">
      <b>角色 & 音色绑定</b>
      <div className="spk-cards" style={{marginTop:12}}>
        {proj.speakers.map((spk:any) => (
          <div key={spk.id} className="card">
            <div className="dim">{spk.label} · {spk.utts}句</div>
            <input type="text" defaultValue={spk.role_name} style={{margin:'8px 0',
              background:'var(--bg3)', border:'1px solid var(--border)', borderRadius:6,
              color:'var(--text)', padding:'5px 8px', width:'100%'}} />
            <div style={{display:'flex', gap:8}}>
              <button className="btn sm ghost">▶ 试听参考音频</button>
              <button className="btn sm ghost">绑音色库</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  </>)
}
