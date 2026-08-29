import { NavLink, Outlet } from 'react-router-dom'

export default function Layout() {
  return (<>
    <aside className="sidebar">
      <div className="logo">译配<span>平台</span></div>
      <NavLink to="/" end className={({isActive}) => 'nav-item' + (isActive ? ' active' : '')}>项目</NavLink>
      <NavLink to="/assets" className={({isActive}) => 'nav-item' + (isActive ? ' active' : '')}>资产中心</NavLink>
      <NavLink to="/architecture" className={({isActive}) => 'nav-item' + (isActive ? ' active' : '')}>架构总览</NavLink>
      <NavLink to="/settings/providers" className={({isActive}) => 'nav-item' + (isActive ? ' active' : '')}>设置</NavLink>
      <div className="foot">v0.2 · 内部系统</div>
    </aside>
    <main className="main"><Outlet /></main>
  </>)
}

export function Crumbs({ items }: { items: string[] }) {
  return <div className="crumbs">{items.map((it, i) =>
    <span key={i}>{i > 0 && ' / '}{i === items.length-1 ? <b>{it}</b> : it}</span>)}</div>
}
