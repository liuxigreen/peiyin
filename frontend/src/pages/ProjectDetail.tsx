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
const PHASE_ZH: Record<string,string> = {
  pre: '预分析', subtitle: '字幕', separate: '分离', recognize: '识别',
  translate: '翻译', tts: '配音', mix: '混音', stitch: '缝合',
}
const STATUS_ZH: Record<string,string> = {
  completed:'已完成', processing:'处理中', failed:'失败', created:'新建',
  analyzing:'预分析中', analyzed:'已预分析',
}

export default function ProjectDetail() {
  const { id } = useParams()
  const [proj, setProj] = useState<any>(null)
  const [prog, setProg] = useState<Progress | null>(null)
  const [tab, setTab] = useState<'pipeline' | 'lines'>('pipeline')
  useEffect(() => {
    if (USE_MOCK) { setProj(mockProjectDetail(id || 'p2')); return }
    api.get<any>(`/api/projects/${id}`).then(setProj).catch(() => {})
    const load = () => api.get<Progress>(`/api/projects/${id}/progress`).then(setProg).catch(() => {})
    load()
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [id])

  async function retryTask(taskId: string) {
    await api.post(`/api/tasks/${taskId}/retry`)
    const p = await api.get<Progress>(`/api/projects/${id}/progress`)
    setProg(p)
  }

  if (!proj) return <div className="dim" style={{padding: 40}}>加载中…</div>
  const phases = prog?.phases
  const phaseIdx = PHASE_KEYS.findIndex(k => {
    const ph = phases?.[k]
    return !ph || ph.done < ph.total
  })
  const pct = prog?.percent ?? 0

  return (<>
    <Crumbs items={['项目', proj.name]} />
    <div className="hero-row">
      <h1 className="hero-title">{proj.name}</h1>
      <span className={`pill ${proj.status}`}>{STATUS_ZH[proj.status] || proj.status}</span>
      {prog && <span className="pct-big">{pct}%</span>}
    </div>

    {/* 进度条 + 阶段链 */}
    {prog && (
      <div className="flow">
        <div className="flow-bar"><div style={{width: `${pct}%`}} /></div>
        <div className="flow-steps">
          {PHASES.map((ph, i) => {
            const phData = phases?.[PHASE_KEYS[i]]
            const done = phData && phData.total > 0 && phData.done >= phData.total
            const failed = phData && phData.failed > 0
            const active = i === phaseIdx
            return (
              <div key={ph} className={'fstep' + (done ? ' done' : failed ? ' failed' : active ? ' active' : '')}>
                <span className="fstep-dot" />
                <span>{PHASE_ZH[ph] || ph}</span>
                {phData && phData.total > 0 && (
                  <span className="fstep-n">{phData.done}/{phData.total}</span>
                )}
              </div>
            )
          })}
        </div>
      </div>
    )}

    {/* 失败任务速修 */}
    {prog && prog.recent_tasks?.filter(t => t.status === 'failed' || t.status === 'dead').length! > 0 && (
      <div className="alert-box">
        <b>⚠ {prog.recent_tasks!.filter(t => t.status==='failed'||t.status==='dead').length} 个失败任务</b>
        <div className="alert-list">
          {prog.recent_tasks!.filter(t => t.status==='failed'||t.status==='dead').map(t => (
            <div key={t.key} className="alert-row">
              <code>{t.key}</code>
              <button className="btn danger sm" onClick={() => retryTask(t.key)}>重试</button>
            </div>
          ))}
        </div>
      </div>
    )}

    {/* Tab 切换：流水 / 台词 */}
    <div className="tabs">
      <button className={'tab' + (tab==='pipeline' ? ' on' : '')} onClick={() => setTab('pipeline')}>流水详情</button>
      <button className={'tab' + (tab==='lines' ? ' on' : '')} onClick={() => setTab('lines')}>
        台词对照表 <span className="dim">{proj.total_utterances ? `(${proj.total_utterances})` : ''}</span>
      </button>
    </div>

    {tab === 'pipeline' && (
      <div className="panel">
        <table className="tbl">
          <thead><tr><th>切片</th><th>时间范围</th><th>状态</th><th>耗时</th><th>失败原因</th><th></th></tr></thead>
          <tbody>
            {proj.segments.map((s:any) => (
              <tr key={s.seg_id} className={s.status==='failed' ? 'row-bad' : ''}>
                <td className="mono">{s.seg_id}</td>
                <td className="dim">{s.range}</td>
                <td><span className={`pill ${s.status}`}>{s.status}</span></td>
                <td className="dim">{s.status==='done' ? `${s.duration_s}s` : '—'}</td>
                <td className="val-hot" title={s.error}>{s.error ? s.error.slice(0, 46) + '…' : ''}</td>
                <td>{s.status==='failed' && <button className="btn danger sm">重试</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )}

    {tab === 'lines' && (
      <div className="panel">
        <Link to={`/projects/${id}/text`} className="btn ghost sm" style={{marginBottom: 12, display: 'inline-block'}}>
          打开完整对照表 →
        </Link>
        <p className="dim">{proj.total_utterances || 0} 句台词 · 翻译/音节比/审核状态在对照表中查看</p>
      </div>
    )}

    {/* 角色卡 */}
    {proj.speakers?.length > 0 && (
      <div className="panel">
        <b>角色 & 音色</b>
        <div className="cast-row">
          {proj.speakers.map((spk:any) => (
            <div key={spk.id} className="cast-card">
              <div className="cast-name">{spk.role_name || spk.label}</div>
              <div className="dim">{spk.utts} 句</div>
              <div style={{display:'flex', gap:6, marginTop:8}}>
                <button className="btn ghost sm">▶ 试听</button>
                <button className="btn ghost sm">音色</button>
              </div>
            </div>
          ))}
        </div>
      </div>
    )}
  </>)
}
