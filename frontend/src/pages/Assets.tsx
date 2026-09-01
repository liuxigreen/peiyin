import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { IcPlay, IcPlus } from '../components/Icons'

type Voice = { id: string; name: string; tags: string[]; use_count: number }
type Term = { series: string; source: string; target_lang: string; target: string }
type Prompt = { id: string; name: string; lang: string; version: number; is_default: boolean; score: number }

const AVATAR_HUES = [222, 262, 160, 28, 200, 320, 90, 0]

export default function Assets() {
  const [tab, setTab] = useState<'voice' | 'term' | 'prompt'>('voice')
  const [series, setSeries] = useState('全部')
  const [voices, setVoices] = useState<Voice[]>([])
  const [terms, setTerms] = useState<Term[]>([])
  const [prompts, setPrompts] = useState<Prompt[]>([])
  const [err, setErr] = useState('')
  const [adding, setAdding] = useState<'' | 'voice' | 'term'>('')
  const [form, setForm] = useState<any>({})

  useEffect(() => {
    const load = async () => {
      try {
        const [v, g, p] = await Promise.all([
          api.get<Voice[]>('/api/assets/voices'),
          api.get<Term[]>('/api/assets/glossary'),
          api.get<Prompt[]>('/api/assets/prompts'),
        ])
        setVoices(v); setTerms(g); setPrompts(p); setErr('')
      } catch (e: any) { setErr(e.message) }
    }
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [])

  const saveVoice = async () => {
    await api.post('/api/assets/voices', {
      name: form.name, tags: (form.tags || '').split(/[,，\s]+/).filter(Boolean),
      ref_audio_r2_key: form.ref || '',
    })
    setAdding(''); setForm({})
    setVoices(await api.get<Voice[]>('/api/assets/voices'))
  }
  const saveTerm = async () => {
    await api.post('/api/assets/glossary', {
      series: form.series, source_term: form.source,
      target_term: form.target, target_lang: form.lang || 'en',
    })
    setAdding(''); setForm({})
    setTerms(await api.get<Term[]>('/api/assets/glossary'))
  }

  return (<>
    <div className="page-head">
      <div>
        <h1 className="page-title">资产中心</h1>
        <div className="page-sub">跨剧复用：音色 / 术语 / Prompt模板</div>
      </div>
      <div className="page-actions">
        {tab === 'voice' && <button className="btn" onClick={() => setAdding('voice')}><IcPlus />新增音色</button>}
        {tab === 'term' && <button className="btn" onClick={() => setAdding('term')}><IcPlus />新增术语</button>}
      </div>
    </div>

    {err && <div className="alert-box">{err}</div>}

    <div className="tabs">
      <button className={'tab' + (tab === 'voice' ? ' on' : '')} onClick={() => setTab('voice')}>音色库 ({voices.length})</button>
      <button className={'tab' + (tab === 'term' ? ' on' : '')} onClick={() => setTab('term')}>术语库 ({terms.length})</button>
      <button className={'tab' + (tab === 'prompt' ? ' on' : '')} onClick={() => setTab('prompt')}>Prompt模板 ({prompts.length})</button>
    </div>

    {tab === 'voice' && (<div className="page-sub" style={{ marginBottom: 12 }}>
      音色库全局共享、跨剧复用（设计如此）：预置音色按 性别/年龄段/音色 标签，
      角色通过「角色→音色分配」按标签自动匹配；主角优先克隆本剧声纹簇（diarize实装后生效）。
    </div>) && (voices.length === 0
      ? <Empty text="音色库为空——新增预置音色，龙套角色按性别/年龄自动匹配" />
      : <div className="card-grid">
          {voices.map((v, i) => (
            <div key={v.id} className="card">
              <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                <div className="cast-avatar" style={{
                  width: 32, height: 32, margin: 0,
                  background: `linear-gradient(135deg, hsl(${AVATAR_HUES[i % 8]} 70% 55%), hsl(${(AVATAR_HUES[i % 8] + 40) % 360} 70% 45%))`,
                }}>{(v.name || '?').slice(0, 1)}</div>
                <b>{v.name}</b>
              </div>
              <div style={{ margin: '10px 0', display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {(v.tags || []).map(t => <span key={t} className="badge info">{t}</span>)}
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <button className="btn ghost sm"><IcPlay />试听</button>
                <span className="dim" style={{ fontSize: 12 }}>被引用 {v.use_count ?? 0} 次</span>
              </div>
            </div>
          ))}
        </div>)}

    {tab === 'term' && (<>
      <div className="page-sub" style={{ marginBottom: 12 }}>
        术语按「剧集系列」隔离存储（剧与剧之间互不干扰）；C0角色提取自动写入人名对照，同系列同术语只保留一条译法。
      </div>
      {(() => {
        const all = Array.from(new Set(terms.map(t => t.series || '（未分类）')))
        const shown = series === '全部' ? terms : terms.filter(t => (t.series || '（未分类）') === series)
        return (<>
          <div className="filters">
            {['全部', ...all].map(se => (
              <button key={se} className={series === se ? 'on' : ''} onClick={() => setSeries(se)}>{se}</button>
            ))}
          </div>
          {shown.length === 0
            ? <Empty text="该剧集还没有术语——C0角色提取后自动生成" />
            : <div className="panel" style={{ padding: 0, overflow: 'hidden' }}>
                <table className="tbl">
                  <thead><tr><th>剧集系列</th><th>中文术语</th><th>语种</th><th>译法</th></tr></thead>
                  <tbody>{shown.map(t => (
                    <tr key={t.series + t.source + t.target_lang}>
                      <td>{t.series || '—'}</td><td>{t.source}</td>
                      <td className="mono dim">{t.target_lang}</td><td>{t.target}</td>
                    </tr>))}
                  </tbody></table>
              </div>}
        </>)
      })()}
    </>)}

    {tab === 'prompt' && (<div className="page-sub" style={{ marginBottom: 12 }}>
      Prompt模板 = 翻译五步链的提示词覆盖入口（按语种/剧型各存一套，替代内置的 R1直译/R2意译/R3终检提示词）。
      当前链路跑内置提示词，此表留作按剧调优与 A/B 效果分对比。
    </div>) && (prompts.length === 0
      ? <Empty text="暂无自定义模板——当前使用链路内置提示词（效果已实测）" />
      : <div className="panel" style={{ padding: 0, overflow: 'hidden' }}>
          <table className="tbl">
            <thead><tr><th>模板名</th><th>语种</th><th>版本</th><th>效果分</th><th></th></tr></thead>
            <tbody>{prompts.map(p => (
              <tr key={p.id}>
                <td>{p.name}</td><td className="mono dim">{p.lang}</td>
                <td className="mono">v{p.version}</td>
                <td>{p.score ?? '—'}</td>
                <td>{p.is_default && <span className="badge completed">默认</span>}</td>
              </tr>))}
            </tbody></table>
        </div>)}

    {adding && (
      <div className="modal-mask" onClick={() => setAdding('')}>
        <div className="modal" onClick={e => e.stopPropagation()}>
          <h3>{adding === 'voice' ? '新增音色' : '新增术语'}</h3>
          {adding === 'voice' ? (<>
            <div className="field"><label>名称</label>
              <input value={form.name || ''} placeholder="如 霸总·低沉压迫感"
                     onChange={e => setForm({ ...form, name: e.target.value })} /></div>
            <div className="field"><label>标签（逗号分隔：male/young/低沉…）</label>
              <input value={form.tags || ''} placeholder="male, young, 低沉威严"
                     onChange={e => setForm({ ...form, tags: e.target.value })} /></div>
            <div className="field"><label>参考音频路径/R2 key（可选）</label>
              <input value={form.ref || ''} placeholder="assets/voices/bazong.wav"
                     onChange={e => setForm({ ...form, ref: e.target.value })} /></div>
            <div className="modal-actions">
              <button className="btn ghost" onClick={() => setAdding('')}>取消</button>
              <button className="btn" onClick={saveVoice}>保存</button>
            </div>
          </>) : (<>
            <div className="field"><label>剧集系列</label>
              <input value={form.series || ''} placeholder="如 替嫁新娘系列"
                     onChange={e => setForm({ ...form, series: e.target.value })} /></div>
            <div className="field"><label>中文术语</label>
              <input value={form.source || ''} placeholder="如 霍氏集团"
                     onChange={e => setForm({ ...form, source: e.target.value })} /></div>
            <div className="field"><label>译法</label>
              <input value={form.target || ''} placeholder="如 Huo Group"
                     onChange={e => setForm({ ...form, target: e.target.value })} /></div>
            <div className="field"><label>语种</label>
              <select value={form.lang || 'en'} onChange={e => setForm({ ...form, lang: e.target.value })}>
                {['en', 'es', 'pt', 'ja', 'ko', 'th', 'vi', 'id'].map(l =>
                  <option key={l} value={l}>{l}</option>)}
              </select></div>
            <div className="modal-actions">
              <button className="btn ghost" onClick={() => setAdding('')}>取消</button>
              <button className="btn" onClick={saveTerm}>保存</button>
            </div>
          </>)}
        </div>
      </div>
    )}
  </>)
}

function Empty({ text }: { text: string }) {
  return <div className="empty-state"><div className="empty-icon">🗂</div><p>{text}</p></div>
}
