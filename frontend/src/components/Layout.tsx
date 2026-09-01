import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { IcFolder, IcMic, IcSliders, IcCpu } from './Icons'

function CrumbFromPath() {
  const loc = useLocation()
  const seg = loc.pathname.split('/').filter(Boolean)
  const name: Record<string, string> = {
    projects: '项目', new: '新建', text: '台词对照',
    assets: '资产中心', architecture: '架构总览',
    settings: '设置', providers: '翻译服务商',
  }
  const items = seg.map(s => (/^[0-9a-f]{8,}$/.test(s) ? '详情' : name[s] || s))
  if (!items.length) return <b>工作台</b>
  return (<>
    {items.map((it, i) => i === items.length - 1
      ? <b key={i}>{it}</b>
      : <span key={i} style={{ opacity: .7 }}>{it} <span style={{ opacity: .5 }}>/</span></span>)}
  </>)
}

export default function Layout() {
  const cls = ({ isActive }: { isActive: boolean }) => 'nav-item' + (isActive ? ' active' : '')
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="logo">
          <div className="logo-mark">配</div>
          <div>译配<span className="accent">工场</span></div>
        </div>
        <div className="nav-section">制作</div>
        <NavLink to="/" end className={cls}><IcFolder />项目工作台</NavLink>
        <NavLink to="/assets" className={cls}><IcMic />资产中心</NavLink>
        <div className="nav-section">系统</div>
        <NavLink to="/architecture" className={cls}><IcCpu />架构总览</NavLink>
        <NavLink to="/settings/providers" className={cls}><IcSliders />翻译服务商</NavLink>
        <div className="foot">peiyin v0.3 · 内部系统<br />短剧出海 AI 译配</div>
      </aside>
      <div className="main-wrap">
        <header className="topbar">
          <div className="crumbs"><CrumbFromPath /></div>
          <div className="spacer" />
          <span className="env-chip"><span className="dot" />PROD · 云端在线</span>
        </header>
        <main className="main"><Outlet /></main>
      </div>
    </div>
  )
}

export function Crumbs({ items }: { items: string[] }) {
  return <div className="crumbs">{items.map((it, i) =>
    <span key={i}>{i > 0 && ' / '}{i === items.length - 1 ? <b>{it}</b> : it}</span>)}</div>
}
