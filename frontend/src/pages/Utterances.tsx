import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { FixedSizeList } from 'react-window'
import { api } from '../api/client'
import { IcRefresh } from '../components/Icons'

type Row = {
  uid: string; speaker: string; original: string; translated: string
  ratio: number; over_limit: boolean; version: number; conf: number; conflict: boolean
}
type Filter = 'all' | 'conflict' | 'over' | 'untranslated'

export default function Utterances() {
  const { id } = useParams()
  const [rows, setRows] = useState<Row[]>([])
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState<Record<string, string>>({})
  const [filter, setFilter] = useState<Filter>('all')
  const [err, setErr] = useState('')
  const [saving, setSaving] = useState('')

  const load = useCallback(() => {
    api.get<Row[]>(`/api/projects/${id}/utterances?lang=en`)
      .then(r => { setRows(r); setErr('') })
      .catch(e => setErr(e.message))
  }, [id])
  useEffect(() => { load() }, [load])

  const shown = useMemo(() => rows.filter(r =>
    filter === 'all'
    || (filter === 'conflict' && r.conflict)
    || (filter === 'over' && (r.over_limit || r.ratio > 1.15))
    || (filter === 'untranslated' && !r.translated)), [rows, filter])

  const update = (uid: string, text: string) =>
    setEdits(m => ({ ...m, [uid]: text }))

  const save = async (uid: string) => {
    const text = edits[uid]
    if (text === undefined) return
    setSaving(uid)
    try {
      const r = await api.put<{ version: number; ratio: number; retts_triggered: boolean }>(
        `/api/projects/${id}/utterances/${uid}/translation`, { text })
      setRows(rs => rs.map(x => x.uid === uid
        ? { ...x, translated: text, version: r.version, ratio: r.ratio } : x))
      setEdits(m => { const n = { ...m }; delete n[uid]; return n })
      setSaved(m => ({ ...m, [uid]: r.retts_triggered ? '已保存 · 重配音已排' : '已保存 v' + r.version }))
      setTimeout(() => setSaved(m => { const n = { ...m }; delete n[uid]; return n }), 4000)
    } catch (e: any) { setErr('保存失败: ' + e.message) }
    setSaving('')
  }

  const saveAll = async () => {
    for (const uid of Object.keys(edits)) await save(uid)
  }
  const dirty = Object.keys(edits).length

  const Row = ({ index, style }: any) => {
    const r = shown[index]
    const val = edits[r.uid] ?? r.translated
    const edited = edits[r.uid] !== undefined && edits[r.uid] !== r.translated
    return (
      <div style={{ ...style, display: 'flex', alignItems: 'stretch' }}>
        <div className={'utline' + (r.conflict ? ' conflict' : '')} style={{ flex: 1 }}>
          <span className="dim mono">{r.uid}</span>
          <span className="dim">{r.speaker}</span>
          <span style={{ fontSize: 13 }}>{r.original}</span>
          <input value={val} onChange={e => update(r.uid, e.target.value)} />
          <span className={r.ratio > 1.15 ? 'val-hot' : r.ratio > 1.05 ? 'val-warn' : 'dim'}>
            {(r.ratio || 0).toFixed(2)}x
          </span>
          <span className={r.conflict ? 'val-warn' : 'dim'}>{(r.conf || 0).toFixed(2)}</span>
          <span className="ut-save">
            {saved[r.uid]
              ? <span className="badge completed">{saved[r.uid]}</span>
              : edited
                ? <button className="btn sm" disabled={saving === r.uid} onClick={() => save(r.uid)}>
                    {saving === r.uid ? '保存中' : '保存'}</button>
                : <span className="dim mono">v{r.version}</span>}
          </span>
        </div>
      </div>
    )
  }

  return (<>
    <div className="page-head">
      <div>
        <h1 className="page-title">台词对照表</h1>
        <div className="page-sub">
          {rows.length} 句 · 显示 {shown.length} · 音节比 &gt;1.05 预警 / &gt;1.15 超限 ·
          编辑保存 = version+1 并自动重排该句配音
        </div>
      </div>
      <div className="page-actions">
        <Link className="btn ghost" to={`/projects/${id}`}>
          ← 返回项目
        </Link>
        <button className="btn ghost" onClick={load}><IcRefresh />刷新</button>
        <button className="btn" disabled={!dirty || saving !== ''} onClick={saveAll}>
          保存全部{dirty ? `(${dirty})` : ''}
        </button>
      </div>
    </div>

    {err && <div className="alert-box">{err}</div>}

    <div className="filters">
      {([['all', '全部'], ['over', '仅超限'], ['conflict', '仅CONFLICT'], ['untranslated', '未翻译']] as [Filter, string][])
        .map(([k, label]) => (
          <button key={k} className={filter === k ? 'on' : ''} onClick={() => setFilter(k)}>{label}</button>
        ))}
    </div>

    <div className="ut-head">
      <span>UID</span><span>角色</span><span>原文</span><span>译文（可编辑）</span>
      <span>音节比</span><span>置信</span><span>版本</span>
    </div>

    {shown.length === 0
      ? <div className="empty-state"><div className="empty-icon">📝</div><p>没有符合筛选的台词</p></div>
      : <FixedSizeList height={560} itemCount={shown.length} itemSize={52} width="100%">
          {Row}
        </FixedSizeList>}
  </>)
}
