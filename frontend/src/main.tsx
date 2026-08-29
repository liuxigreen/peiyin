import React from 'react'
import ReactDOM from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import Layout from './components/Layout'
import Projects from './pages/Projects'
import NewProject from './pages/NewProject'
import ProjectDetail from './pages/ProjectDetail'
import Utterances from './pages/Utterances'
import Providers from './pages/Providers'
import Assets from './pages/Assets'
import Architecture from './pages/Architecture'
import './index.css'

const router = createBrowserRouter([
  { path: '/', element: <Layout />, children: [
    { index: true, element: <Projects /> },
    { path: 'projects/new', element: <NewProject /> },
    { path: 'projects/:id', element: <ProjectDetail /> },
    { path: 'projects/:id/text', element: <Utterances /> },
    { path: 'settings/providers', element: <Providers /> },
    { path: 'assets', element: <Assets /> },
    { path: 'architecture', element: <Architecture /> },
  ]},
])

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><RouterProvider router={router} /></React.StrictMode>,
)
