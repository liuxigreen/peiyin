import { useEffect, useState } from 'react'
import { Crumbs } from '../components/Layout'
import { api } from '../api/client'

type Prov = {
  id: string
  name: string
  type: string
  base_url: string
  key_masked: string | null
  model: string | null
  is_default: boolean
  enabled: boolean
  priority: number
}

type TestResult = {
  ok: boolean
  status: number
  latency_ms?: number
  sample?: string
  model?: string
  error?: string | null
}

const EMPTY = { name: '', type: 'custom_openai_compatible', base_url: '',
                model: '', priority: 0 }

export default function Providers() {
  const [list, setList] = useState<Prov[]>([])
  const [editing, setEditing] = useState<any | null>(null)
  const [testing, setTesting] = useState<string>('')
  const [testResult, setTestResult] = useState<Record<string, TestResult>>({})
  const [err, setErr] = useState('')

  const load = async () => {
    try { setList(await api.get<Prov[]>('/api/providers')) }
    catch (e: any) { setErr('加载失败: ' + e.message) }
  }
  useEffect(() => { load() }, [])

  const save = async () => {
    if (!editing) return
    setErr('')
    const body: any = {
      name: editing.name, provider_type: editing.type,
      api_base_url: editing.base_url, model_name: editing.model,
      priority: Number(editing.priority) || 0,
    }
    if (editing.key_raw) body.api_key = editing.key_raw
    try {
      if (editing.id) await api.put(`/api/providers/${editing.id}`, body)
      else await api.post('/api/providers', body)
      setEditing(null)
      await load()
    } catch (e: any) { setErr('保存失败: ' + e.message) }
  }

  const testConn = async (p: Prov) => {
    setTesting(p.id)
    setErr('')
    try {
      const r = await api.post<TestResult>(`/api/providers/${p.id}/test`)
      setTestResult(m => ({ ...m, [p.id]: r }))
    } catch (e: any) {
      setTestResult(m => ({ ...m, [p.id]: { ok: false, status: 0, error: e.message } }))
    }
    setTesting('')
  }

  const setDefault = async (p: Prov) => {
    await api.post(`/api/providers/${p.id}/set-default`)
    await load()
  }
  const toggle = async (p: Prov) => {
    await api.put(`/api/providers/${p.id}`, { is_enabled: !p.enabled })
    await load()
  }
  const del = async (p: Prov) => {
    if (!confirm(`删除 ${p.name}？`)) return
    await api.del(`/api/providers/${p.id}`)
    await load()
  }

  const testView = (p: Prov) => {
    const r = testResult[p.id]
    if (!r) return null
    return r.ok
      ? <span className="badge completed">✓ 通 {r.latency_ms}ms{nbsp(r.sample)}</span>
      : <span className="badge failed" title={r.error || ''}>✗ {short(r.error || `HTTP ${r.status}`)}</span>
  }

  return (<>
    <Crumbs items={['设置', '翻译服务商']} />
    <h1 className="page-title">翻译服务商
      <button className="btn" onClick={() => setEditing({ ...EMPTY })}>＋ 新增</button>
    </h1>
    {err && <div className="badge failed" style={{ marginBottom: 10, display: 'block', padding: '6px 12px' }}>{err}</div>}
    <div className="dim" style={{ marginBottom: 12 }}>
      默认服务商用于全部翻译任务。key 加密存储只显示打码。MiniMax 用国际站 https://api.minimax.io/v1（国内 api.minimaxi.com）。
    </div>

    {list.map(p => (
      <div key={p.id} className="panel" style={{ marginBottom: 10, maxWidth: 860 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
          <div style={{ minWidth: 300 }}>
            <b>{p.name}</b>
            {p.is_default && <span className="badge completed" style={{ marginLeft: 6 }}>默认</span>}
            {!p.enabled && <span className="badge pending" style={{ marginLeft: 6 }}>停用</span>}
            <div className="dim" style={{ marginTop: 4 }}>{p.type} · {p.base_url} · {p.model}</div>
            <div className="dim">{p.key_masked || '（未设key）'}</div>
            {testView(p) && <div style={{ marginTop: 5 }}>{testView(p)}</div>}
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="btn sm" disabled={testing === p.id}
              onClick={() => testConn(p)}>
              {testing === p.id ? '测通中…' : '测试连通'}</button>
            {!p.is_default && <button className="btn sm ghost" onClick={() => setDefault(p)}>设默认</button>}
            <button className="btn sm ghost" onClick={() => setEditing({
              id: p.id, name: p.name, type: p.type, base_url: p.base_url,
              model: p.model || '', key_masked: p.key_masked || '', priority: p.priority,
            })}>编辑</button>
            <button className="btn sm ghost" onClick={() => toggle(p)}>{p.enabled ? '停用' : '启用'}</button>
            <button className="btn sm danger" onClick={() => del(p)}>删除</button>
          </div>
        </div>
      </div>
    ))}

    {editing && (
      <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.55)', display: 'flex',
        alignItems: 'center', justifyContent: 'center', zIndex: 50 }}
        onClick={() => setEditing(null)}>
        <div className="panel" style={{ width: 540, margin: 0 }} onClick={e => e.stopPropagation()}>
          <h3 style={{ marginBottom: 14 }}>{editing.id ? '编辑服务商' : '新增服务商'}</h3>
          <div className="field"><label>名称</label>
            <input type="text" value={editing.name} placeholder="如 MiniMax M3"
              onChange={e => setEditing({ ...editing, name: e.target.value })} /></div>
          <div className="field"><label>类型</label>
            <select value={editing.type} onChange={e => setEditing({ ...editing, type: e.target.value })}>
              <option value="custom_openai_compatible">OpenAI兼容（自定义）</option>
              <option value="openai">OpenAI官方</option>
              <option value="anthropic">Anthropic Claude</option>
              <option value="deepl">DeepL</option>
              <option value="local_llm">本地LLM</option>
            </select></div>
          <div className="field"><label>API Base URL</label>
            <input type="text" value={editing.base_url} placeholder="https://api.minimax.io/v1"
              onChange={e => setEditing({ ...editing, base_url: e.target.value })} /></div>
          <div className="field"><label>API Key{editing.key_masked ? `（已存 ${editing.key_masked}，留空不改）` : '（保存后加密存储）'}</label>
            <input type="password" value={editing.key_raw || ''} placeholder="eyJ... 或 sk-..."
              onChange={e => setEditing({ ...editing, key_raw: e.target.value })} /></div>
          <div className="field"><label>模型名</label>
            <input type="text" value={editing.model} placeholder="MiniMax-M3 / deepseek-chat"
              onChange={e => setEditing({ ...editing, model: e.target.value })} /></div>
          <div style={{ display: 'flex', gap: 10, marginTop: 18 }}>
            <button className="btn" onClick={save}>保存</button>
            <button className="btn ghost" onClick={() => setEditing(null)}>取消</button>
          </div>
        </div>
      </div>
    )}
  </>)
}

function short(s: string) { return s.length > 46 ? s.slice(0, 46) + '…' : s }
function nbsp(s?: string) {
  return s ? ` · "${s}"` : ''
}
