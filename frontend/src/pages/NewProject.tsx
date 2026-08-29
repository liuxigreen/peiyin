import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Crumbs } from '../components/Layout'

export default function NewProject() {
  const [name, setName] = useState('')
  const [lang, setLang] = useState('en')
  const [file, setFile] = useState<File | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [progress, setProgress] = useState<number | null>(null) // null=未开始
  const nav = useNavigate()

  const startUpload = () => {
    if (!file) return
    // TODO(B1): 请求 /api/upload/presign 后分片直传R2；此为UI演示进度
    let pct = 0
    setProgress(0)
    const t = setInterval(() => {
      pct = Math.min(100, pct + Math.random() * 9)
      setProgress(Math.floor(pct))
      if (pct >= 100) { clearInterval(t); setTimeout(() => nav('/'), 600) }
    }, 180)
  }

  return (<>
    <Crumbs items={['项目','新建']} />
    <h1 className="page-title">新建译配项目</h1>
    <div className="panel" style={{maxWidth: 720}}>
      <div className="field">
        <label>短剧母片（MP4，支持大文件分片上传）</label>
        <div
          className={'dropzone' + (dragOver ? ' over' : '')}
          onDragOver={e => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={e => { e.preventDefault(); setDragOver(false); setFile(e.dataTransfer.files[0]) }}
          onClick={() => document.getElementById('file-in')?.click()}
        >
          {file ? `✅ ${file.name} (${(file.size/1024/1024).toFixed(0)}MB)` : '拖拽视频到此处，或点击选择文件'}
        </div>
        <input id="file-in" type="file" accept="video/*" hidden
          onChange={e => setFile(e.target.files?.[0] ?? null)} />
      </div>

      {progress !== null && (
        <div className="field">
          <label>上传进度 {progress}%</label>
          <div className="progress"><div style={{width: `${progress}%`}} /></div>
        </div>
      )}

      <div className="field">
        <label>项目名称</label>
        <input type="text" placeholder="例：霸道总裁的替嫁新娘"
          value={name} onChange={e => setName(e.target.value)} />
      </div>
      <div className="field">
        <label>目标语种</label>
        <select value={lang} onChange={e => setLang(e.target.value)}>
          <option value="en">英语 English</option>
          <option value="es">西班牙语 Español</option>
          <option value="pt">葡萄牙语 Português</option>
          <option value="ja">日语 日本語</option>
          <option value="th">泰语 ไทย</option>
          <option value="vi">越南语 Tiếng Việt</option>
          <option value="id">印尼语 Indonesia</option>
        </select>
      </div>
      <button className="btn" disabled={!file || progress !== null} onClick={startUpload}>
        {progress === null ? '开始上传并创建项目' : progress >= 100 ? '上传完成，跳转中…' : `上传中 ${progress}%`}
      </button>
    </div>
  </>)
}
