import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { IcPlus } from '../components/Icons'

type Prov = {
  id: string; name: string; type: string; base_url: string
  key_masked: string | null; model: string | null
  is_default: boolean; enabled: boolean; priority: number
}
type TestResult = {
  ok: boolean; status: number; latency_ms?: number
  sample?: string; model?: string; error?: string | null
}
const EMPTY = { name: '', type: 'custom_openai_compatible', base_url: '', model: '', priority: 0 }

export default function Providers() {
  const [list, setList] = useState<Prov[]>([])
  const [editing, setEditing] = useState<any | null>(null)
  const [testing, setTesting] = useState('')
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
      setEditing(null); await load()
    } catch (e: any) { setErr('保存失败: ' + e.message) }
  }
  const testConn = async (p: Prov) => {
    setTesting(p.id); setErr('')
    try {
      const r = await api.post<TestResult>(`/api/providers/${p.id}/test`)
      setTestResult(m => ({ ...m, [p.id]: r }))
    } catch (e: any) {
      setTestResult(m => ({ ...m, [p.id]: { ok: false, status: 0, error: e.message } }))
    }
    setTesting('')
  }
  const setDefault = async (p: Prov) => { await api.post(`/api/providers/${p.id}/set-default`); await load() }
  const toggle = async (p: Prov) => { await api.put(`/api/providers/${p.id}`, { is_enabled: !p.enabled }); await load() }
  const del = async (p: Prov) => {
    if (!confirm(`删除 ${p.name}？`)) return
    await api.del(`/api/providers/${p.id}`); await load()
  }
  const testView = (p: Prov) => {
    const r = testResult[p.id]
    if (!r) return null
    return r.ok
      ? <span className="badge completed">✓ 通 {r.latency_ms}ms{r.sample ? ` · "${r.sample}"` : ''}</span>
      : <span className="badge failed" title={r.error || ''}>✗ {short(r.error || `HTTP ${r.status}`)}</span>
  }

  return (<>
    <div className="page-head">
      <div>
        <h1 className="page-title">翻译服务商</h1>
        <div className="page-sub">
          默认服务商承接全部翻译任务 · key 加密存储只显示打码 · MiniMax 国际站 https://api.minimax.io/v1
        </div>
      </div>
      <button className="btn" onClick={() => setEditing({ ...EMPTY })}><IcPlus />新增服务商</button>
    </div>

    {err && <div className="alert-box">{err}</div>}

    {list.length === 0 && !err && (
      <div className="empty-state"><div className="empty-icon">🔌</div>
        <p>还没有配置翻译服务商——新增一个 OpenAI 兼容网关即可开始</p></div>
    )}

    <div style={{ display: 'grid', gap: 12 }}>
      {list.map(p => (
        <div key={p.id} className="panel" style={{ padding: '16px 20px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
            <div style={{ minWidth: 300 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <b style={{ fontSize: 15 }}>{p.name}</b>
                {p.is_default && <span className="badge completed">默认</span>}
                {!p.enabled && <span className="badge pending">停用</span>}
              </div>
              <div className="dim" style={{ marginTop: 4, fontSize: 12.5 }}>
                {p.type} · <span className="mono">{p.base_url}</span> · <span className="mono">{p.model}</span>
              </div>
              <div className="dim" style={{ fontSize: 12.5 }}>{p.key_masked || '（未设key）'}</div>
              {testView(p) && <div style={{ marginTop: 6 }}>{testView(p)}</div>}
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button className="btn sm" disabled={testing === p.id} onClick={() => testConn(p)}>
                {testing === p.id ? '测通中…' : '测试连通'}
              </button>
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
    </div>

    {editing && (
      <div className="modal-mask" onClick={() => setEditing(null)}>
        <div className="modal" onClick={e => e.stopPropagation()}>
          <h3>{editing.id ? '编辑服务商' : '新增服务商'}</h3>
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
          <div className="field">
            <label>API Key{editing.key_masked ? `（已存 ${editing.key_masked}，留空不改）` : '（保存后加密存储）'}</label>
            <input type="password" value={editing.key_raw || ''} placeholder="eyJ... 或 sk-..."
                   onChange={e => setEditing({ ...editing, key_raw: e.target.value })} /></div>
          <div className="field"><label>模型名</label>
            <input type="text" value={editing.model} placeholder="MiniMax-M3 / deepseek-chat"
                   onChange={e => setEditing({ ...editing, model: e.target.value })} /></div>
          <div className="modal-actions">
            <button className="btn ghost" onClick={() => setEditing(null)}>取消</button>
            <button className="btn" onClick={save}>保存</button>
          </div>
        </div>
      </div>
    )}
  </>)
}

function short(s: string) { return s.length > 46 ? s.slice(0, 46) + '…' : s }
