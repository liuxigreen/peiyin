import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { FixedSizeList } from 'react-window'
import { Crumbs } from '../components/Layout'
import { mockUtterances } from '../api/mock'

type Filter = 'all' | 'conflict' | 'over'

export default function Utterances() {
  const { id } = useParams()
  const [rows, setRows] = useState(() => mockUtterances())
  const [filter, setFilter] = useState<Filter>('all')

  const shown = useMemo(() => rows.filter(r =>
    filter === 'all' || (filter === 'conflict' ? r.conflict : r.ratio > 1.15)), [rows, filter])

  const update = (uid: string, text: string) =>
    setRows(rs => rs.map(r => r.uid === uid ? { ...r, translated: text } : r))

  const Row = ({ index, style }: any) => {
    const r = shown[index]
    return (
      <div style={{...style, display:'grid', gridTemplateColumns:'110px 70px 1fr 1fr 90px 80px',
        gap:10, alignItems:'center', padding:'4px 10px',
        borderBottom:'1px solid rgba(42,50,66,.5)',
        background: r.conflict ? 'rgba(244,84,79,.07)' : undefined}}>
        <span className="dim">{r.uid}</span>
        <span className="dim">{r.speaker}</span>
        <span>{r.original}</span>
        <input value={r.translated} onChange={e => update(r.uid, e.target.value)} />
        <span className={r.ratio > 1.15 ? 'val-hot' : r.ratio > 1.05 ? 'val-warn' : 'dim'}>
          {r.ratio.toFixed(2)}x
        </span>
        <span className={r.conf < 0.7 ? 'val-warn' : 'dim'}>{r.conf.toFixed(2)}</span>
      </div>
    )
  }

  return (<>
    <Crumbs items={['项目', String(id), '台词对照']} />
    <h1 className="page-title">台词对照表
      <span className="dim">{shown.length} / {rows.length} 句 · CONFLICT红底 &lt;0.7黄标 超限红数字</span>
    </h1>
    <div className="filters">
      {([['all','全部'],['conflict','仅CONFLICT'],['over','仅超限']] as [Filter,string][]).map(([k, label]) => (
        <button key={k} className={filter===k?'on':''} onClick={() => setFilter(k)}>{label}</button>
      ))}
    </div>
    <div style={{display:'grid', gridTemplateColumns:'110px 70px 1fr 1fr 90px 80px', gap:10,
      padding:'0 10px 6px', color:'var(--text-dim)', fontSize:12}}>
      <span>UID</span><span>角色</span><span>原文</span><span>译文（可编辑）</span><span>音节比</span><span>置信度</span>
    </div>
    <FixedSizeList height={560} itemCount={shown.length} itemSize={42} width="100%">
      {Row}
    </FixedSizeList>
  </>)
}
