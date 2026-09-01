import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { IcFilm, IcMic } from '../components/Icons'

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
        await api.post(`/api/projects/${pid}/seed-srt`, { srt: srtText, scene_size: 40 })
        if (audioFile) {
          const fd = new FormData()
          fd.append('file', audioFile)
          const up = await fetch(`/api/projects/${pid}/mode-b/upload-file`, { method: 'POST', body: fd })
          if (!up.ok) throw new Error(`音频上传失败 ${up.status}`)
        }
        const run = await api.post<{ ok: boolean; error?: string }>(`/api/projects/${pid}/mode-b/run`)
        if (!run.ok) throw new Error(run.error || '模式B流程失败')
        nav(`/projects/${pid}`)
      } else {
        if (videoFile) {
          const pre = await api.post<{ url: string; key: string }>('/api/upload/presign',
            { project_id: pid, filename: videoFile.name })
          await fetch(pre.url, { method: 'PUT', body: videoFile })
          await api.post(`/api/projects/${pid}/upload-complete`, { r2_key: pre.key })
        }
        nav(`/projects/${pid}`)
      }
    } catch (e: any) {
      setErr(e.message || String(e))
      setBusy(false)
    }
  }

  const srtValid = srtText.includes('-->')
  const step = mode === 'B' ? (srtFile ? 2 : 1) : (videoFile ? 2 : 1)

  return (<>
    <div className="page-head">
      <div>
        <h1 className="page-title">新建译配项目</h1>
        <div className="page-sub">三步：选模式 → 传素材 → 创建并启动流水线</div>
      </div>
    </div>

    <div className="wizard-steps">
      {['① 选择模式', '② 上传素材', '③ 启动流水线'].map((s, i) => (
        <div key={s} className={'wstep' + (step > i + 1 ? ' done' : step === i + 1 ? ' on' : '')}>{s}</div>
      ))}
    </div>

    <div className="mode-cards">
      <div className={'card' + (mode === 'B' ? ' sel' : '')} onClick={() => !busy && setMode('B')}>
        <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <IcMic />模式B · 字幕+配音 <span className="badge completed">推荐</span>
        </h3>
        <p className="dim" style={{ marginTop: 6, fontSize: 12.5 }}>
          上传中文字幕 SRT + 中文配音音频 → 翻译出外语字幕 + 分句外语配音交付包。无需视频，交付快。
        </p>
      </div>
      <div className={'card' + (mode === 'A' ? ' sel' : '')} onClick={() => !busy && setMode('A')}>
        <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <IcFilm />模式A · 完整流程
        </h3>
        <p className="dim" style={{ marginTop: 6, fontSize: 12.5 }}>
          上传视频母片走全流程 → 直接出配音成片视频。GPU 环节接入中，当前先登记素材。
        </p>
      </div>
    </div>

    <div className="panel" style={{ maxWidth: 780 }}>
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
              ? (srtValid ? `✅ ${srtFile.name}（时间轴解析正常）`
                          : `⚠️ ${srtFile.name}（未检测到时间轴 -->，请确认 SRT 格式）`)
              : '拖拽 .srt 文件到此处，或点击选择'}
          </div>
          <input id="srt-in" type="file" accept=".srt,text/plain" hidden
                 onChange={e => pickSrt(e.target.files?.[0] ?? null)} />
        </div>
        <div className="field">
          <label>中文配音音频（wav/mp3，按字幕时间轴整条录制；可选，稍后可补传）</label>
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

      {err && <div className="alert-box">{err}</div>}

      <button className="btn" disabled={busy} onClick={create} style={{ minWidth: 200 }}>
        {busy ? '处理中…（翻译需要一点时间）' : mode === 'B' ? '创建并开始翻译' : '创建项目'}
      </button>
      {mode === 'B' && (
        <div className="dim" style={{ marginTop: 10, fontSize: 12 }}>
          流程：字幕解析 → 五步链翻译 → 音节校验 → 交付包（外语 SRT/ASS + 分句配音）→ 详情页下载
        </div>
      )}
    </div>
  </>)
}
