import React, { useEffect, useState } from 'react'
import { api, getOperator, setOperator, setToken } from './api.js'
import RunsPage from './pages/RunsPage.jsx'
import RunConsole from './pages/RunConsole.jsx'
import WorkflowPage from './pages/WorkflowPage.jsx'
import SettingsProviders from './pages/SettingsProviders.jsx'
import SettingsSources from './pages/SettingsSources.jsx'

function Login({ onDone }) {
  const [token, setTok] = useState('')
  const [name, setName] = useState(getOperator())
  const [err, setErr] = useState('')
  const go = async () => {
    setToken(token.trim()); setOperator(name.trim())
    try { await api('/api'); onDone() } catch { setErr('token rejected — check CONSOLE_TOKEN') }
  }
  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-8 w-96">
        <h1 className="font-bold text-zinc-100 mb-1">O2S<span className="text-sky-500">▸</span>console</h1>
        <p className="text-xs text-zinc-500 mb-4">This console is protected. Enter the access token
          (the CONSOLE_TOKEN the operator configured) and your name — approvals are recorded against it.</p>
        <input type="password" autoComplete="current-password" placeholder="console token" value={token}
          onChange={e => setTok(e.target.value)} onKeyDown={e => e.key === 'Enter' && go()}
          className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-2 text-sm mb-2 focus:outline-none focus:border-sky-600" />
        <input placeholder="your name (shown on approvals)" value={name}
          onChange={e => setName(e.target.value)} onKeyDown={e => e.key === 'Enter' && go()}
          className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-2 text-sm mb-3 focus:outline-none focus:border-sky-600" />
        <button onClick={go} className="w-full bg-sky-700 hover:bg-sky-600 rounded py-2 text-sm text-white">Enter</button>
        {err && <p className="text-red-400 text-xs mt-2">{err}</p>}
      </div>
    </div>
  )
}

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
  const [auth, setAuth] = useState('checking')   // checking | ok | required
  useEffect(() => {
    api('/api').then(() => setAuth('ok')).catch(e =>
      setAuth(String(e.message).includes('unauthorized') ? 'required' : 'ok'))
    const fn = () => setAuth('required')
    window.addEventListener('console-auth-required', fn)
    return () => window.removeEventListener('console-auth-required', fn)
  }, [])
  const active = (href) => route.startsWith(href.slice(1)) && (href !== '#/runs' || !route.startsWith('/runs/'))

  if (auth === 'checking') return <p className="p-10 text-zinc-500">connecting…</p>
  if (auth === 'required') return <Login onDone={() => setAuth('ok')} />

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
