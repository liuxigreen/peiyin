import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { PHASES, mockProjectDetail } from '../api/mock'
import { api, USE_MOCK } from '../api/client'
import { IcDownload, IcPlay } from '../components/Icons'

type Progress = {
  percent: number
  phases: Record<string, { total: number; done: number; failed: number }>
  counts: Record<string, number>
  recent_tasks?: { key: string; type: string; status: string }[]
}
const PHASE_KEYS = ['pre', 'subtitle', 'separate', 'recognize', 'translate', 'tts', 'mix', 'stitch']
const PHASE_ZH: Record<string, string> = {
  pre: '预分析', subtitle: '字幕', separate: '分离', recognize: '识别',
  translate: '翻译', tts: '配音', mix: '混音', stitch: '缝合',
}
const STATUS_ZH: Record<string, string> = {
  completed: '已完成', processing: '处理中', failed: '失败', created: '已建库',
  analyzing: '预分析中', analyzed: '已预分析',
}
const AVATAR_HUES = [222, 262, 160, 28, 200, 320, 90, 0]

function Ring({ pct }: { pct: number }) {
  const R = 30, C = 2 * Math.PI * R
  return (
    <svg width="76" height="76" viewBox="0 0 76 76" style={{ flex: 'none' }}>
      <defs><linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stopColor="#6e7bff" /><stop offset="100%" stopColor="#9c6eff" />
      </linearGradient></defs>
      <circle cx="38" cy="38" r={R} stroke="rgba(148,163,198,.15)" strokeWidth="7" fill="none" />
      <circle cx="38" cy="38" r={R} stroke="url(#ring)" strokeWidth="7" fill="none"
              strokeDasharray={C} strokeDashoffset={C * (1 - pct / 100)}
              strokeLinecap="round" transform="rotate(-90 38 38)"
              style={{ transition: 'stroke-dashoffset .5s ease' }} />
      <text x="38" y="43" textAnchor="middle" fill="#e9edf6"
            style={{ font: '700 16px ui-monospace,monospace' }}>{pct}%</text>
    </svg>
  )
}

export default function ProjectDetail() {
  const { id } = useParams()
  const [proj, setProj] = useState<any>(null)
  const [prog, setProg] = useState<Progress | null>(null)
  const [tab, setTab] = useState<'pipeline' | 'lines'>('pipeline')
  const [pkg, setPkg] = useState<{ file: string; download_url: string } | null>(null)
  const [pkgMsg, setPkgMsg] = useState('')
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
    setProg(await api.get<Progress>(`/api/projects/${id}/progress`))
  }
  async function checkPkg() {
    setPkgMsg('')
    try { setPkg(await api.get(`/api/projects/${id}/mode-b/package`)) }
    catch { setPkgMsg('交付包尚未生成（翻译/配音完成后可下载）') }
  }

  if (!proj) return <div className="dim" style={{ padding: 40 }}>加载中…</div>
  const phases = prog?.phases
  const phaseIdx = PHASE_KEYS.findIndex(k => {
    const ph = phases?.[k]
    return !ph || ph.done < ph.total
  })
  const pct = prog?.percent ?? 0
  const failedTasks = prog?.recent_tasks?.filter(t => ['failed', 'dead'].includes(t.status)) || []

  return (<>
    <div className="page-head">
      <div style={{ display: 'flex', gap: 20, alignItems: 'center' }}>
        <Ring pct={pct} />
        <div>
          <h1 className="page-title">{proj.name}
            <span className={`pill ${proj.status}`}>{STATUS_ZH[proj.status] || proj.status}</span>
          </h1>
          <div className="page-sub">
            目标语种 {String(proj.target_lang || 'en').toUpperCase()} · {proj.total_utterances || '—'} 句台词
            {prog ? ` · ${prog.counts?.completed ?? 0} 任务完成` : ''}
          </div>
        </div>
      </div>
      <div className="page-actions">
        <button className="btn ghost" onClick={checkPkg}><IcDownload />检查交付包</button>
        {pkg && <a className="btn" href={pkg.download_url}><IcDownload />下载交付包</a>}
      </div>
    </div>
    {pkgMsg && <div className="page-sub" style={{ marginBottom: 14 }}>{pkgMsg}</div>}

    {prog && (
      <div className="flow">
        <div className="flow-bar"><div style={{ width: `${pct}%` }} /></div>
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
                {phData && phData.total > 0 && <span className="fstep-n">{phData.done}/{phData.total}</span>}
              </div>
            )
          })}
        </div>
      </div>
    )}

    {failedTasks.length > 0 && (
      <div className="alert-box">
        <b>⚠ {failedTasks.length} 个失败任务</b>
        <div className="alert-list">
          {failedTasks.map(t => (
            <div key={t.key} className="alert-row">
              <code>{t.key}</code>
              <button className="btn danger sm" onClick={() => retryTask(t.key)}>重试</button>
            </div>
          ))}
        </div>
      </div>
    )}

    <div className="tabs">
      <button className={'tab' + (tab === 'pipeline' ? ' on' : '')} onClick={() => setTab('pipeline')}>流水详情</button>
      <button className={'tab' + (tab === 'lines' ? ' on' : '')} onClick={() => setTab('lines')}>
        台词对照 {proj.total_utterances ? `(${proj.total_utterances})` : ''}
      </button>
    </div>

    {tab === 'pipeline' && (
      <div className="panel" style={{ padding: 0, overflow: 'hidden' }}>
        <table className="tbl">
          <thead><tr><th>切片</th><th>时间范围</th><th>状态</th><th>耗时</th><th>失败原因</th><th></th></tr></thead>
          <tbody>
            {proj.segments.map((s: any) => (
              <tr key={s.seg_id} className={s.status === 'failed' ? 'row-bad' : ''}>
                <td className="mono">{s.seg_id}</td>
                <td className="dim">{s.range}</td>
                <td><span className={`pill ${s.status}`}>{s.status}</span></td>
                <td className="dim">{s.status === 'done' ? `${s.duration_s}s` : '—'}</td>
                <td className="val-hot" title={s.error}>{s.error ? s.error.slice(0, 46) + '…' : ''}</td>
                <td>{s.status === 'failed' && <button className="btn danger sm">重试</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )}

    {tab === 'lines' && (
      <div className="panel">
        <Link to={`/projects/${id}/text`} className="btn" style={{ display: 'inline-flex', marginBottom: 12 }}>
          打开完整台词对照表 →
        </Link>
        <p className="dim">{proj.total_utterances || 0} 句台词 · 翻译/音节比/审核状态在对照表中维护</p>
      </div>
    )}

    {proj.speakers?.length > 0 && (
      <div className="panel" style={{ marginTop: 18 }}>
        <b>角色 & 音色</b>
        <div className="cast-row">
          {proj.speakers.map((spk: any, i: number) => (
            <div key={spk.id} className="cast-card">
              <div className="cast-avatar" style={{
                background: `linear-gradient(135deg, hsl(${AVATAR_HUES[i % 8]} 70% 55%), hsl(${(AVATAR_HUES[i % 8] + 40) % 360} 70% 45%))`,
              }}>{(spk.role_name || spk.label || '?').slice(0, 1)}</div>
              <div className="cast-name">{spk.role_name || spk.label}</div>
              <div className="dim" style={{ fontSize: 12 }}>{spk.utts} 句</div>
              <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
                <button className="btn ghost sm"><IcPlay />试听</button>
                <button className="btn ghost sm">音色</button>
              </div>
            </div>
          ))}
        </div>
      </div>
    )}
  </>)
}
