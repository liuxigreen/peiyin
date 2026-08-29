import { useState } from 'react'
import { Crumbs } from '../components/Layout'

const voices = [
  { name:'霸总·低沉压迫感', tags:['男频','总裁','低音'], uses:23 },
  { name:'甜妹·元气少女', tags:['女频','女主','高音'], uses:41 },
  { name:'恶毒女配·尖刻', tags:['反派','女'], uses:17 },
  { name:'纪录片旁白·沉稳男声', tags:['旁白','中性'], uses:52 },
  { name:'御姐·冷艳', tags:['女频','配角'], uses:9 },
]
const terms = [
  ['替嫁新娘系列','苏念念','Su Niannian'],
  ['替嫁新娘系列','霍氏集团','Huo Group'],
  ['重生逆袭系列','陆言','Lu Yan'],
]

export default function Assets() {
  const [tab, setTab] = useState<'voice'|'term'|'prompt'>('voice')
  return (<>
    <Crumbs items={['资产中心']} />
    <h1 className="page-title">资产中心<span className="dim">跨剧复用：音色 / 术语 / Prompt模板</span></h1>
    <div className="tabs">
      <button className={tab==='voice'?'on':''} onClick={() => setTab('voice')}>音色库 ({voices.length})</button>
      <button className={tab==='term'?'on':''} onClick={() => setTab('term')}>术语库</button>
      <button className={tab==='prompt'?'on':''} onClick={() => setTab('prompt')}>Prompt模板</button>
    </div>

    {tab==='voice' && (
      <div className="card-grid">
        {voices.map(v => (
          <div key={v.name} className="card">
            <b>{v.name}</b>
            <div style={{margin:'8px 0'}}>{v.tags.map(t => <span key={t} className="badge pending" style={{marginRight:6}}>{t}</span>)}</div>
            <div style={{display:'flex', justifyContent:'space-between', alignItems:'center'}}>
              <button className="btn sm ghost">▶ 试听</button>
              <span className="dim">被引用{v.uses}次</span>
            </div>
          </div>
        ))}
      </div>
    )}

    {tab==='term' && (
      <table className="tbl"><thead><tr><th>剧集系列</th><th>中文术语</th><th>英文译法</th><th></th></tr></thead>
        <tbody>{terms.map(t => (
          <tr key={String(t)}><td>{t[0]}</td><td>{t[1]}</td><td>{t[2]}</td>
            <td><button className="btn sm ghost">编辑</button></td></tr>))}
        </tbody></table>
    )}

    {tab==='prompt' && (
      <table className="tbl"><thead><tr><th>模板名</th><th>语种</th><th>版本</th><th>效果分</th><th></th></tr></thead>
        <tbody>{[
          ['短剧翻译标准版','en','v3',92],
          ['打脸爽点强化版','en','v2',88],
          ['泰语本地化版','th','v1',74],
        ].map(p => (
          <tr key={String(p)}><td>{p[0] as string}</td><td>{p[1] as string}</td><td>{p[2] as string}</td>
            <td>{p[3] as number}</td>
            <td><span className="badge completed">{(p[3] as number) >= 88 ? '默认' : ''}</span>
              {' '}<button className="btn sm ghost">查看</button></td></tr>))}
        </tbody></table>
    )}
  </>)
}
