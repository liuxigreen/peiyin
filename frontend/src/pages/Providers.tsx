import { useState } from 'react'
import { Crumbs } from '../components/Layout'

const seedProviders = [
  { id:'v1', name:'DeepSeek 主力', type:'custom_openai_compatible', base:'https://api.deepseek.com/v1',
    key:'sk-****8xq2', model:'deepseek-chat', is_default:true, enabled:true },
  { id:'v2', name:'Qwen 备用', type:'openai', base:'https://dashscope.aliyuncs.com/compatible-mode/v1',
    key:'sk-****4ga', model:'qwen-max', is_default:false, enabled:true },
  { id:'v3', name:'本地测试', type:'local_llm', base:'http://192.168.31.163:8000/v1',
    key:'-', model:'qwen2.5-7b', is_default:false, enabled:false },
]

export default function Providers() {
  const [list, setList] = useState(seedProviders)
  const [editing, setEditing] = useState<any | null>(null)
  const [testing, setTesting] = useState('')

  const save = () => {
    if (editing.id) setList(l => l.map(p => p.id === editing.id ? editing : p))
    else setList(l => [...l, { ...editing, id: 'v' + Date.now(), key: 'sk-****new', is_default: false }])
    setEditing(null)
  }
  const testConn = async (name: string) => {
    // TODO(B1): POST /api/providers/{id}/test 真连通性
    setTesting(name); setTimeout(() => setTesting(''), 1200)
  }

  return (<>
    <Crumbs items={['设置','翻译服务商']} />
    <h1 className="page-title">翻译服务商
      <button className="btn" onClick={() => setEditing({ name:'', type:'custom_openai_compatible', base:'', key:'', model:'' })}>＋ 新增</button>
    </h1>

    {list.map(p => (
      <div key={p.id} className="panel" style={{display:'flex', justifyContent:'space-between', alignItems:'center', maxWidth:760}}>
        <div>
          <b>{p.name}</b> {p.is_default && <span className="badge completed" style={{marginLeft:6}}>默认</span>}
          {!p.enabled && <span className="badge pending" style={{marginLeft:6}}>停用</span>}
          <div className="dim" style={{marginTop:4}}>{p.type} · {p.base} · {p.model}</div>
          <div className="dim">{p.key}</div>
        </div>
        <div style={{display:'flex', gap:8}}>
          <button className="btn sm ghost" onClick={() => testConn(p.name)}>
            {testing === p.name ? '测通中…' : '测试连通'}</button>
          <button className="btn sm ghost" onClick={() => setEditing(p)}>编辑</button>
          {!p.is_default && (
            <button className="btn sm ghost" onClick={() => setList(l => l.map(x => ({...x, is_default: x.id===p.id})))}>设默认</button>)}
        </div>
      </div>
    ))}

    {editing && (
      <div style={{position:'fixed', inset:0, background:'rgba(0,0,0,.55)', display:'flex',
        alignItems:'center', justifyContent:'center'}} onClick={() => setEditing(null)}>
        <div className="panel" style={{width:520, margin:0}} onClick={e => e.stopPropagation()}>
          <h3 style={{marginBottom:14}}>{editing.id ? '编辑服务商' : '新增服务商'}</h3>
          <div className="field"><label>名称</label>
            <input type="text" value={editing.name} onChange={e => setEditing({...editing, name:e.target.value})}/></div>
          <div className="field"><label>类型</label>
            <select value={editing.type} onChange={e => setEditing({...editing, type:e.target.value})}>
              <option value="custom_openai_compatible">OpenAI兼容（自定义）</option>
              <option value="openai">OpenAI官方</option>
              <option value="anthropic">Anthropic Claude</option>
              <option value="deepl">DeepL</option>
              <option value="local_llm">本地LLM</option>
            </select></div>
          <div className="field"><label>API Base URL</label>
            <input type="text" value={editing.base} onChange={e => setEditing({...editing, base:e.target.value})}/></div>
          <div className="field"><label>API Key（保存后加密存储，仅显示打码）</label>
            <input type="password" value={editing.key_raw || ''} placeholder="sk-..."
              onChange={e => setEditing({...editing, key_raw:e.target.value})}/></div>
          <div className="field"><label>模型名</label>
            <input type="text" value={editing.model} onChange={e => setEditing({...editing, model:e.target.value})}/></div>
          <div style={{display:'flex', gap:10, marginTop:18}}>
            <button className="btn" onClick={save}>保存并测试连通性</button>
            <button className="btn ghost" onClick={() => setEditing(null)}>取消</button>
          </div>
        </div>
      </div>
    )}
  </>)
}
