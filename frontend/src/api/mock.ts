// Mock数据：VITE_USE_MOCK=true时启用；后端API就绪后关闭开关即切换
export const mockProjects = [
  { id: 'p1', name: '霸道总裁的替嫁新娘', status: 'completed', progress: 100, speakers: 6, lang: 'en', created: '2026-08-25', size_mb: 2450 },
  { id: 'p2', name: '闪婚老公是豪门', status: 'tts_generating', progress: 62, speakers: 5, lang: 'en', created: '2026-08-26', size_mb: 1980 },
  { id: 'p3', name: '离婚后我成了首富千金', status: 'translating', progress: 38, speakers: 8, lang: 'en', created: '2026-08-26', size_mb: 3120 },
  { id: 'p4', name: '重生之逆袭人生', status: 'failed', progress: 15, speakers: 4, lang: 'en', created: '2026-08-27', size_mb: 850 },
  { id: 'p5', name: '千亿总裁宠妻无度', status: 'completed', progress: 100, speakers: 7, lang: 'en', created: '2026-08-20', size_mb: 2760 },
  { id: 'p6', name: '神医弃妃要翻身', status: 'mixing', progress: 81, speakers: 6, lang: 'en', created: '2026-08-24', size_mb: 2210 },
  { id: 'p7', name: '隐婚百分百', status: 'analyzed', progress: 22, speakers: 3, lang: 'en', created: '2026-08-23', size_mb: 1540 },
  { id: 'p8', name: '甜宠小娇妻马甲掉了', status: 'pre_analyzing', progress: 8, speakers: 0, lang: 'en', created: '2026-08-27', size_mb: 990 },
]

export const PHASES = ['预分析','擦字幕','人声分离','识别对齐','翻译','TTS配音','混音','缝合输出']
export const PHASE_KEYS = ['pre_analysis','subtitle','separate','recognize','translate','tts','mix','stitch']

export function mockProjectDetail(id: string) {
  const base = mockProjects.find(p => p.id === id) ?? mockProjects[0]
  const segs = Array.from({length: 12}, (_, i) => ({
    seg_id: `S${String(i+1).padStart(2,'0')}`,
    range: `${fmt(i*210)} - ${fmt((i+1)*210-3)}`,
    status: i < 7 ? 'done' : i === 7 ? 'running' : i === 9 ? 'failed' : 'pending',
    duration_s: 45 + (i*13)%60,
    error: i === 9 ? 'Demucs OOM: CUDA out of memory (11.4G peak)' : '',
  }))
  return {
    ...base,
    phase_idx: 5,
    segments: segs,
    speakers: [
      { id:'spk1', label:'SPK_01', role_name:'男主·霍云琛', utts: 412 },
      { id:'spk2', label:'SPK_02', role_name:'女主·苏念念', utts: 398 },
      { id:'spk3', label:'SPK_03', role_name:'反派·顾曼', utts: 156 },
      { id:'spk4', label:'SPK_04', role_name:'旁白', utts: 203 },
    ],
  }
}

function fmt(sec: number) {
  const m = Math.floor(sec/60), s = sec%60
  return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
}

const LN = ['How dare you treat me like this!','I never loved you.','Sign the divorce papers.','You will regret this.','Meet me at the office tomorrow.','This is my last warning.','The board votes in an hour.']
export function mockUtterances() {
  return Array.from({length: 300}, (_, i) => {
    const ratio = [0.92, 1.02, 0.88, 1.18, 0.95, 1.31][i % 6]
    return {
      uid: `S${String(Math.floor(i/25)+1).padStart(2,'0')}-U${String(i%25+1).padStart(3,'0')}`,
      speaker: ['男主','女主','反派','旁白'][i % 4],
      original: ['你竟敢这样对我！','我从来没有爱过你。','把离婚协议签了。','你会后悔的。','明天来公司见我。','这是我最后一次警告你。','董事会一小时后投票。'][i % 7],
      asr: null as string | null,
      ocr: null as string | null,
      translated: LN[i % 7],
      ratio,
      conf: [0.97, 0.64, 0.89, 0.52, 0.93][i % 5],
      conflict: i % 17 === 3,
    }
  })
}
