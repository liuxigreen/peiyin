import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Crumbs } from '../components/Layout'
import { api } from '../api/client'

type Mode = 'A' | 'B'

export default function NewProject() {
  const [mode, setMode] = useState<Mode>('B')
  const [name, setName] = useState('')
  const [lang, setLang] = useState('en')
  const [srtFile, setSrtFile] = useState<File | null>(null)
  const [srtText, setSrtText] = useState('')
  const [audioFile, setAudioFile] = useState<File | null>(null)
  const [videoFile, setVideoFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const nav = useNavigate()

  const pickSrt = async (f: File | null) => {
    setSrtFile(f)
    if (f) setSrtText(await f.text())
  }

  const create = async () => {
    setErr('')
    if (!name.trim()) { setErr('请填写项目名称'); return }
    if (mode === 'B' && !srtText.trim()) { setErr('请选择中文字幕 SRT 文件'); return }
    setBusy(true)
    try {
      const p = await api.post<{ id: string }>('/api/projects',
        { name: name.trim(), target_lang: lang, mode })
      const pid = p.id
      if (mode === 'B') {
        await api.post(`/api/projects/${pid}/seed-srt`,
          { srt: srtText, scene_size: 40 })
        if (audioFile) {
          const fd = new FormData()
          fd.append('file', audioFile)
          const up = await fetch(`/api/projects/${pid}/mode-b/upload-file`,
            { method: 'POST', body: fd })
          if (!up.ok) throw new Error(`音频上传失败 ${up.status}`)
        }
        const run = await api.post<{ ok: boolean; error?: string }>(
          `/api/projects/${pid}/mode-b/run`)
        if (!run.ok) throw new Error(run.error || '模式B流程失败')
        nav(`/projects/${pid}`)
      } else {
        // 模式A：视频上传（presign直传，当前阶段先登记）
        if (videoFile) {
          const pre = await api.post<{ url: string; key: string }>('/api/upload/presign',
            { project_id: pid, filename: videoFile.name })
          await fetch(pre.url, { method: 'PUT', body: videoFile })
          await api.post(`/api/projects/${pid}/upload-complete`,
            { r2_key: pre.key })
        }
        nav(`/projects/${pid}`)
      }
    } catch (e: any) {
      setErr(e.message || String(e))
      setBusy(false)
    }
  }

  const srtValid = srtText.includes('-->')

  return (<>
    <Crumbs items={['项目', '新建']} />
    <h1 className="page-title">新建译配项目</h1>

    {/* 模式选择 */}
    <div style={{ display: 'flex', gap: 12, marginBottom: 16, maxWidth: 720 }}>
      <div className="card" onClick={() => !busy && setMode('B')} style={{
        flex: 1, cursor: 'pointer', maxWidth: 340,
        borderColor: mode === 'B' ? 'var(--accent)' : 'var(--border)',
        borderWidth: mode === 'B' ? 2 : 1,
      }}>
        <h3>{mode === 'B' ? '🔵 ' : ''}模式B · 字幕+配音 <span className="badge completed">推荐</span></h3>
        <p className="dim" style={{ marginTop: 6 }}>
          上传中文字幕SRT + 中文配音音频 → 翻译出外语字幕+配音交付包。无视频，快，本地合成。
        </p>
      </div>
      <div className="card" onClick={() => !busy && setMode('A')} style={{
        flex: 1, cursor: 'pointer', maxWidth: 340,
        borderColor: mode === 'A' ? 'var(--accent)' : 'var(--border)',
        borderWidth: mode === 'A' ? 2 : 1,
      }}>
        <h3>{mode === 'A' ? '🔵 ' : ''}模式A · 完整流程</h3>
        <p className="dim" style={{ marginTop: 6 }}>
          上传视频母片走全流程 → 直接出配音成片视频。（GPU环节待接入，当前先登记）
        </p>
      </div>
    </div>

    <div className="panel" style={{ maxWidth: 720 }}>
      <div className="field">
        <label>项目名称</label>
        <input type="text" placeholder="例：霸道总裁的替嫁新娘"
          value={name} onChange={e => setName(e.target.value)} />
      </div>

      {mode === 'B' && (<>
        <div className="field">
          <label>中文字幕 SRT 文件 *</label>
          <div className="dropzone"
            onDragOver={e => e.preventDefault()}
            onDrop={e => { e.preventDefault(); pickSrt(e.dataTransfer.files[0]) }}
            onClick={() => document.getElementById('srt-in')?.click()}>
            {srtFile
              ? (srtValid
                ? `✅ ${srtFile.name}（解析正常）`
                : `⚠️ ${srtFile.name}（未检测到时间轴 -->，请确认是SRT格式）`)
              : '拖拽 .srt 文件到此处，或点击选择'}
          </div>
          <input id="srt-in" type="file" accept=".srt,text/plain" hidden
            onChange={e => pickSrt(e.target.files?.[0] ?? null)} />
        </div>
        <div className="field">
          <label>中文配音音频（wav/mp3，整条按字幕时间轴录制；可选，稍后也可补传）</label>
          <div className="dropzone"
            onDragOver={e => e.preventDefault()}
            onDrop={e => { e.preventDefault(); setAudioFile(e.dataTransfer.files[0]) }}
            onClick={() => document.getElementById('audio-in')?.click()}>
            {audioFile ? `✅ ${audioFile.name} (${(audioFile.size / 1024 / 1024).toFixed(1)}MB)` : '拖拽音频到此处，或点击选择'}
          </div>
          <input id="audio-in" type="file" accept="audio/*" hidden
            onChange={e => setAudioFile(e.target.files?.[0] ?? null)} />
        </div>
      </>)}

      {mode === 'A' && (
        <div className="field">
          <label>短剧母片（MP4/MOV）</label>
          <div className="dropzone"
            onDragOver={e => e.preventDefault()}
            onDrop={e => { e.preventDefault(); setVideoFile(e.dataTransfer.files[0]) }}
            onClick={() => document.getElementById('video-in')?.click()}>
            {videoFile ? `✅ ${videoFile.name} (${(videoFile.size / 1024 / 1024).toFixed(0)}MB)` : '拖拽视频到此处，或点击选择'}
          </div>
          <input id="video-in" type="file" accept="video/*" hidden
            onChange={e => setVideoFile(e.target.files?.[0] ?? null)} />
        </div>
      )}

      <div className="field">
        <label>目标语种</label>
        <select value={lang} onChange={e => setLang(e.target.value)}>
          <option value="en">英语 English</option>
          <option value="es">西班牙语 Español</option>
          <option value="pt">葡萄牙语 Português</option>
          <option value="ja">日语 日本語</option>
          <option value="ko">韩语 한국어</option>
          <option value="th">泰语 ไทย</option>
          <option value="vi">越南语 Tiếng Việt</option>
          <option value="id">印尼语 Indonesia</option>
        </select>
      </div>

      {err && <div className="badge failed" style={{ display: 'block', padding: '6px 12px', marginBottom: 10 }}>{err}</div>}

      <button className="btn" disabled={busy} onClick={create}>
        {busy ? '处理中…（翻译需要一点时间）' : mode === 'B' ? '创建并开始翻译' : '创建项目'}
      </button>
      {mode === 'B' && <div className="dim" style={{ marginTop: 8 }}>
        流程：字幕解析 → M3五步链翻译 → 音节校验 → 生成交付包（外语SRT/ASS+分句配音）→ 跳转详情页下载
      </div>}
    </div>
  </>)
}
