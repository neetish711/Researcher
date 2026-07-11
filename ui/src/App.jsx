import React, { useEffect, useState } from 'react'
import RunsPage from './pages/RunsPage.jsx'
import RunConsole from './pages/RunConsole.jsx'
import WorkflowPage from './pages/WorkflowPage.jsx'
import SettingsProviders from './pages/SettingsProviders.jsx'
import SettingsSources from './pages/SettingsSources.jsx'

// tiny hash router: #/runs, #/runs/<id>, #/workflow, #/settings/providers, #/settings/sources
function useRoute() {
  const [hash, setHash] = useState(window.location.hash || '#/runs')
  useEffect(() => {
    const fn = () => setHash(window.location.hash || '#/runs')
    window.addEventListener('hashchange', fn)
    return () => window.removeEventListener('hashchange', fn)
  }, [])
  return hash.replace(/^#/, '')
}

const NAV = [
  ['#/runs', 'Runs'],
  ['#/workflow', 'Workflow'],
  ['#/settings/providers', 'Providers'],
  ['#/settings/sources', 'Research Sources'],
]

export default function App() {
  const route = useRoute()
  const active = (href) => route.startsWith(href.slice(1)) && (href !== '#/runs' || !route.startsWith('/runs/'))

  let page
  const runMatch = route.match(/^\/runs\/([A-Za-z0-9_-]+)/)
  if (runMatch) page = <RunConsole runId={runMatch[1]} />
  else if (route.startsWith('/workflow')) page = <WorkflowPage />
  else if (route.startsWith('/settings/sources')) page = <SettingsSources />
  else if (route.startsWith('/settings')) page = <SettingsProviders />
  else page = <RunsPage />

  return (
    <div className="min-h-screen">
      <nav className="sticky top-0 z-40 flex items-center gap-1 px-4 h-12 bg-zinc-900/95 backdrop-blur border-b border-zinc-800">
        <span className="font-bold text-zinc-100 mr-4 tracking-tight">O2S<span className="text-sky-500">▸</span>console</span>
        {NAV.map(([href, label]) => (
          <a key={href} href={href}
             className={`px-3 py-1.5 rounded text-sm ${active(href) ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'}`}>
            {label}</a>
        ))}
        <div className="ml-auto flex items-center gap-3 text-xs text-zinc-500">
          {runMatch && <span className="font-mono">run {runMatch[1]}</span>}
          <a href="/docs" target="_blank" className="hover:text-zinc-300">API</a>
        </div>
      </nav>
      <main className="p-4 max-w-[1500px] mx-auto">{page}</main>
    </div>
  )
}
