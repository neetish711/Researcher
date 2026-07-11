import React, { useMemo, useRef, useState } from 'react'
import { api, usePoll, useEvents, KEY_RE, KEY_MSG, fmtUsd, fmtDur } from '../api.js'
import { Btn, Card, ErrorNote, Input, Json, Modal, Pill, Select, Table, statusKind, useAsync } from '../lib.jsx'

const AGENTS = ['discovery', 'mapping', 'research', 'suitability']
const GATE_OWNER = { confirm_problem: 'discovery', validate_map: 'mapping', approve_plan: 'research' }
const GATE_LABEL = {
  confirm_problem: 'Confirm the problem statement + data inventory',
  validate_map: 'Validate the current/future workflow map',
  approve_plan: 'Approve the research plan (workers only run after this)',
}
const EVENT_ICON = {
  llm_call: '🧠', search_query: '🔎', source_call: '📡', page_fetch: '🌐', doc_read: '📄',
  finding_created: '📌', citation_verified: '✅', citation_rejected: '🚫', round_complete: '🔁',
  gate_waiting: '⏸', gate_approved: '👍', gate_rejected: '👎', error: '❌', retry: '↩',
  checkpoint_saved: '💾', agent_start: '▶', agent_end: '⏹', worker_start: '👷', worker_end: '🏁',
  run_created: '✨', tool_call: '🔧',
}

/** live per-provider quota burn during a run */
function QuotaStrip({ visible }) {
  const { data } = usePoll(visible ? '/research-sources' : null, 6000)
  if (!visible || !data) return null
  const active = data.providers.filter(p => p.quota?.monthly_quota != null && (p.has_key || p.keyless_ok))
  if (!active.length) return null
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg px-4 py-2 flex flex-wrap gap-x-6 gap-y-1">
      <span className="text-[10px] uppercase tracking-widest text-zinc-500 self-center">source quota</span>
      {active.map(p => {
        const q = p.quota, pct = Math.min(100, 100 * q.used / q.monthly_quota)
        return (
          <div key={p.id} className="text-xs min-w-[130px]">
            <span className="font-mono text-zinc-300">{p.id}</span>
            <span className={`ml-1 ${p.status === 'exhausted' ? 'text-red-400' : p.status === 'quota_low' ? 'text-amber-400' : 'text-zinc-500'}`}>
              {q.used.toLocaleString()}/{q.monthly_quota.toLocaleString()} {q.unit}s</span>
            <div className="h-1 bg-zinc-800 rounded mt-0.5">
              <div className={`h-1 rounded ${pct > 85 ? 'bg-red-500' : pct > 60 ? 'bg-amber-500' : 'bg-emerald-600'}`}
                   style={{ width: `${pct}%` }} /></div>
          </div>)
      })}
      {!data.free_tier_only && <span className="text-red-400 text-xs self-center">⚠ free_tier_only OFF</span>}
    </div>
  )
}

/** end-of-run per-provider usage: calls, cache hit rate, units, findings contributed */
function SourceUsage({ runId }) {
  const { data } = usePoll(`/runs/${runId}/source-usage`, 15000)
  if (!data || !Object.keys(data.providers || {}).length) return null
  return (
    <Card title={`Source usage — ${data.total_calls} calls · cache hit rate ${(data.cache_hit_rate * 100).toFixed(0)}%`}>
      <Table headers={['provider', 'calls', 'errors', 'cache hits', 'units spent', 'findings contributed']}
        rows={Object.entries(data.providers).sort((a, b) => b[1].calls - a[1].calls).map(([p, c]) => [
          <span className="font-mono">{p}</span>, c.calls, c.errors || '—', c.cache_hits,
          c.units ? c.units.toLocaleString() : '0', c.findings || '—'])} />
      <p className="text-[11px] text-zinc-600 mt-1">Jina should carry the bulk of extraction; Firecrawl/TinyFish only on their trigger conditions.</p>
    </Card>
  )
}

function agentStates(cf, status) {
  const done = {
    discovery: !!cf.problem_statement && cf.problem_confirmed_by_human,
    mapping: (cf.future_workflow || []).length > 0 && cf.map_validated_by_human,
    research: (cf.research_rounds_done || 0) > 0 && (cf.findings || []).length > 0,
    suitability: !!cf.suitability,
  }
  const st = Object.fromEntries(AGENTS.map(a => [a, done[a] ? 'done' : 'pending']))
  if (status.startsWith('running:')) st[status.split(':')[1]] = 'running'
  if (status.startsWith('awaiting_gate:')) { const o = GATE_OWNER[status.split(':')[1]]; if (o) st[o] = 'waiting' }
  if ((status.startsWith('error') || status === 'paused_budget') && cf.next_agent && st[cf.next_agent] !== 'done')
    st[cf.next_agent] = 'error'
  return st
}

/** live research sub-architecture, derived from the event stream */
function ResearchDiagram({ events, status, metrics }) {
  const last = events[events.length - 1] || {}
  const inFlight = useMemo(() => {
    const open = new Set()
    for (const e of events) {
      if (e.type === 'worker_start') open.add(e.worker)
      if (e.type === 'worker_end') open.delete(e.worker)
    }
    return open
  }, [events])
  const researchRunning = status === 'running:research'
  const phase = !researchRunning ? '' :
    inFlight.size ? 'workers' :
    (last.purpose || '').startsWith('synthesis') ? 'synthesis' :
    ['citation_verified', 'citation_rejected'].includes(last.type) ? 'citations' :
    (last.purpose || '') === 'research plan' ? 'plan' : 'workers'
  const gateWaiting = status === 'awaiting_gate:approve_plan'
  const node = (key, label, active, extra) => (
    <div className={`px-2.5 py-1.5 rounded border text-xs text-center min-w-[86px]
      ${active ? 'border-sky-500 bg-sky-950 text-sky-200 shadow-[0_0_10px_rgba(14,165,233,.3)]' : 'border-zinc-700 text-zinc-400'}`}>
      <div className="font-semibold">{label}</div>{extra && <div className="text-[10px] text-zinc-500">{extra}</div>}
    </div>)
  const arrow = <span className="text-zinc-600">→</span>
  const r = metrics?.rounds
  return (
    <div className="mt-3 pt-3 border-t border-zinc-800">
      <p className="text-[11px] uppercase tracking-widest text-zinc-500 mb-2">Research engine
        {r && <span className="ml-2 text-sky-400 normal-case tracking-normal">round {Math.min(r.done + (researchRunning ? 1 : 0), r.max)} of {r.max}</span>}
      </p>
      <div className="flex items-center gap-1.5 flex-wrap">
        {node('plan', 'lead plan', phase === 'plan')}{arrow}
        {node('gate', '⏸ approval', gateWaiting)}{arrow}
        <div className="flex flex-col gap-1">
          {['no_code', 'low_code', 'full_code', 'saas'].map(w =>
            <div key={w} className={`px-2 py-0.5 rounded border text-[11px]
              ${inFlight.has(w) ? 'border-sky-500 bg-sky-950 text-sky-200 animate-pulse' : 'border-zinc-700 text-zinc-500'}`}>
              👷 {w}{inFlight.has(w) && ' — in flight'}</div>)}
        </div>{arrow}
        {node('synth', 'synthesis', phase === 'synthesis', 'similarity · costs · scores')}{arrow}
        {node('cov', 'coverage check', false, 'loop ⟲')}{arrow}
        {node('cite', 'citations', phase === 'citations', 'verify / demote / drop')}{arrow}
        {node('rep', 'reports', false, 'HTML + PPT')}
      </div>
    </div>
  )
}

function MetricsStrip({ m, cf }) {
  if (!m) return null
  const items = [
    ['rounds', `${m.rounds.done}/${m.rounds.max}`],
    ['coverage', `${m.coverage_pct}%`],
    ['findings', `${m.findings.created} · ✅${m.findings.verified} · 🚫${m.findings.rejected}`],
    ['options', Object.entries(m.options_per_category).map(([k, v]) => `${k.replace('_', '-')}:${v}`).join(' ')],
    ['sources hit', m.sources_hit],
    ['tokens', `${(m.tokens.in / 1000).toFixed(0)}k → ${(m.tokens.out / 1000).toFixed(0)}k`],
    ['spend', `${fmtUsd(m.cost_spent_usd)}${m.cost_cap_usd ? ` / ${fmtUsd(m.cost_cap_usd)}` : ''}`],
    ['elapsed', fmtDur(m.elapsed_s)],
    ['problems', m.problems],
  ]
  const burn = m.cost_cap_usd ? Math.min(100, 100 * m.cost_spent_usd / m.cost_cap_usd) : 0
  const wall = m.wall_clock?.budget_s ? Math.min(100, 100 * (1 - m.wall_clock.left_s / m.wall_clock.budget_s)) : null
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg px-4 py-2">
      <div className="flex flex-wrap gap-x-6 gap-y-1">
        {items.map(([k, v]) => <div key={k} className="text-xs">
          <span className="text-zinc-500 uppercase tracking-wider text-[10px] mr-1.5">{k}</span>
          <span className={`font-mono ${k === 'problems' && v > 0 ? 'text-red-400' : 'text-zinc-200'}`}>{v}</span></div>)}
      </div>
      <div className="flex gap-4 mt-1.5">
        <div className="flex-1"><div className="h-1 bg-zinc-800 rounded">
          <div className="h-1 rounded bg-gradient-to-r from-emerald-600 to-red-500" style={{ width: `${burn}%` }} /></div>
          <p className="text-[10px] text-zinc-600">budget burn</p></div>
        {wall !== null && <div className="flex-1"><div className="h-1 bg-zinc-800 rounded">
          <div className="h-1 rounded bg-sky-600" style={{ width: `${wall}%` }} /></div>
          <p className="text-[10px] text-zinc-600">wall clock ({fmtDur(m.wall_clock.left_s)} left)</p></div>}
      </div>
    </div>
  )
}

function EventRow({ e, onOpen }) {
  const err = e.status === 'error' || e.type === 'error'
  return (
    <button onClick={() => onOpen(e)}
      className={`w-full text-left grid grid-cols-[52px_20px_92px_1fr_auto] gap-2 px-2 py-1 rounded text-xs font-mono
        hover:bg-zinc-800/60 ${err ? 'bg-red-950/40 text-red-300' : e.type === 'retry' ? 'bg-amber-950/30 text-amber-200' : 'text-zinc-400'}`}>
      <span className="text-zinc-600">#{e.seq}</span>
      <span>{EVENT_ICON[e.type] || '·'}</span>
      <span className="truncate text-zinc-500">{e.agent}{e.worker ? `/${e.worker}` : ''}</span>
      <span className="truncate">
        <b className="text-zinc-300">{e.type}</b>{' '}
        {e.purpose || e.query || e.url || e.claim || e.gate || e.model || e.error?.slice(0, 90) || ''}
      </span>
      <span className="text-zinc-600">
        {e.duration_ms ? `${e.duration_ms}ms ` : ''}{e.tokens_out ? `${e.tokens_in}/${e.tokens_out}tk ` : ''}
        {e.cost_usd ? fmtUsd(e.cost_usd) : ''}</span>
    </button>
  )
}

function EventDetail({ e, onClose, onRetry }) {
  return (
    <Modal title={`#${e.seq} ${e.type} — ${e.agent}${e.worker ? '/' + e.worker : ''}`} onClose={onClose} wide>
      <div className="flex flex-wrap gap-4 text-xs mb-3">
        {e.model && <span>model <b className="text-zinc-200 font-mono">{e.model}</b></span>}
        {e.provider && <span>provider <b className="text-zinc-200">{e.provider}</b></span>}
        {e.temperature !== undefined && <span>temp <b className="text-zinc-200">{e.temperature}</b></span>}
        {e.duration_ms !== undefined && <span>duration <b className="text-zinc-200">{e.duration_ms}ms</b></span>}
        {e.tokens_in !== undefined && <span>tokens <b className="text-zinc-200">{e.tokens_in}→{e.tokens_out}</b></span>}
        {e.cost_usd !== undefined && <span>cost <b className="text-zinc-200">{fmtUsd(e.cost_usd)}</b></span>}
        <Pill kind={e.status === 'ok' ? 'ok' : e.status === 'retrying' ? 'retrying' : 'error'}>{e.status}</Pill>
      </div>
      {e.error && <>
        <p className="text-red-400 text-sm mb-1 break-words">{e.error}</p>
        {e.error_class && <p className="text-xs text-zinc-400 mb-2">class <b className="text-amber-400">{e.error_class}</b>
          — {e.suggested_fix} {onRetry && <Btn className="ml-2 !py-0.5 !px-2 text-xs" onClick={onRetry}>Retry step</Btn>}</p>}
      </>}
      {e.system && <><p className="text-[11px] uppercase text-zinc-500 mt-3 mb-1">System prompt (key redacted)</p>
        <Json value={e.system} className="max-h-40 overflow-y-auto" /></>}
      {e.messages && <><p className="text-[11px] uppercase text-zinc-500 mt-3 mb-1">Request messages</p>
        <Json value={e.messages} className="max-h-56 overflow-y-auto" /></>}
      {e.response && <><p className="text-[11px] uppercase text-zinc-500 mt-3 mb-1">Raw response</p>
        <Json value={e.response} className="max-h-56 overflow-y-auto" /></>}
      {e.needs_approval && <><p className="text-[11px] uppercase text-zinc-500 mt-3 mb-1">Needs approval</p>
        <Json value={e.needs_approval} className="max-h-56 overflow-y-auto" /></>}
      {!e.system && !e.messages && !e.response && !e.needs_approval &&
        <Json value={e} className="max-h-72 overflow-y-auto" />}
    </Modal>
  )
}

function RetryModal({ runId, providers, onClose, onDone }) {
  const [model, setModel] = useState('')
  const [provider, setProvider] = useState('')
  const { busy, err, wrap } = useAsync()
  const go = () => wrap(async () => {
    if (model && KEY_RE.test(model)) throw new Error(KEY_MSG)
    await api(`/runs/${runId}/retry`, { method: 'POST', body: { model: model || null, provider: provider || null } })
    onDone(); onClose()
  })
  return (
    <Modal title="Retry this step" onClose={onClose}>
      <p className="text-sm text-zinc-400 mb-3">The model is a per-call parameter — swap it and re-run just the failed step, or leave blank to retry as-is.</p>
      <div className="grid grid-cols-2 gap-3">
        <div><p className="text-xs text-zinc-500 mb-1">Provider (optional)</p>
          <Select value={provider} onChange={e => setProvider(e.target.value)}>
            <option value="">(keep current)</option>
            {providers.map(p => <option key={p.name} value={p.name}>{p.name}</option>)}</Select></div>
        <div><p className="text-xs text-zinc-500 mb-1">Different model (optional)</p>
          <Input value={model} onChange={e => setModel(e.target.value)} placeholder="model id" /></div>
      </div>
      <div className="mt-4 flex gap-2"><Btn variant="primary" disabled={busy} onClick={go}>Retry</Btn></div>
      <ErrorNote>{err}</ErrorNote>
    </Modal>
  )
}

function Uploads({ runId }) {
  const { data, error } = usePoll(`/runs/${runId}/uploads`, 5000)
  const [sens, setSens] = useState('internal')
  const { err, setErr, wrap } = useAsync()
  const inputRef = useRef()
  const send = (files) => wrap(async () => {
    for (const f of files) {
      const fd = new FormData(); fd.append('file', f); fd.append('sensitivity', sens)
      await api(`/runs/${runId}/uploads`, { method: 'POST', body: fd })
    }
  })
  return (
    <div>
      <div onDragOver={e => e.preventDefault()}
           onDrop={e => { e.preventDefault(); send([...e.dataTransfer.files]) }}
           onClick={() => inputRef.current.click()}
           className="border-2 border-dashed border-zinc-700 hover:border-sky-700 rounded-lg p-4 text-center cursor-pointer">
        <p className="text-sm text-zinc-400">Drag internal docs here (or click) — .txt .md .csv .json, ≤5 MB</p>
        <p className="text-[11px] text-zinc-600">They join research round 1 as high-reliability internal:// sources.</p>
        <input ref={inputRef} type="file" multiple hidden onChange={e => send([...e.target.files])} />
      </div>
      <div className="flex items-center gap-2 mt-2 text-xs">
        <span className="text-zinc-500">sensitivity for new files:</span>
        <Select value={sens} onChange={e => setSens(e.target.value)} className="!w-40">
          {['public', 'internal', 'confidential', 'restricted'].map(s => <option key={s}>{s}</option>)}</Select>
      </div>
      <ErrorNote>{err || error}</ErrorNote>
      <div className="mt-2 space-y-1">
        {(data?.files || []).map(f => (
          <div key={f.name} className="flex items-center gap-3 text-sm px-2 py-1 rounded bg-zinc-950/60 border border-zinc-800">
            <span className="font-mono text-zinc-300">{f.name}</span>
            <Pill kind={f.status === 'parsed' ? 'ok' : f.status === 'stored' ? 'pending' : 'error'}>{f.status}</Pill>
            <span className="text-zinc-600 text-xs">{f.sensitivity} · {(f.size / 1024).toFixed(1)} KB</span>
            {f.error && <span className="text-red-400 text-xs">{f.error}</span>}
            <button className="ml-auto text-zinc-600 hover:text-red-400"
              onClick={() => wrap(() => api(`/runs/${runId}/uploads/${f.name}`, { method: 'DELETE' }))}>✕</button>
          </div>))}
      </div>
    </div>
  )
}

export default function RunConsole({ runId }) {
  const { data: run, error: runErr } = usePoll(`/runs/${runId}`, 3000)
  const { data: metrics } = usePoll(`/runs/${runId}/metrics`, 5000)
  const { data: filesD } = usePoll(`/runs/${runId}/files`, 8000)
  const { data: inputsD } = usePoll(`/runs/${runId}/inputs`, 10000)
  const { data: providers } = usePoll('/providers', 60000)
  const events = useEvents(runId)
  const [tab, setTab] = useState('events')
  const [detail, setDetail] = useState(null)
  const [retry, setRetry] = useState(false)
  const [filter, setFilter] = useState({ agent: '', type: '', status: '', q: '' })
  const [snapshotA, setSnapshotA] = useState('')
  const { err: actErr, setErr, wrap } = useAsync()

  if (runErr) return <Card title="Run"><p className="text-red-400">{runErr}</p></Card>
  if (!run) return <p className="text-zinc-500 p-8">loading run…</p>
  const cf = run.casefile, sm = run.summary
  const states = agentStates(cf, sm.status)
  const gate = sm.awaiting_gate

  const problems = events.filter(e => e.type === 'error' || e.status === 'error')
  const shown = events.filter(e =>
    (!filter.agent || e.agent === filter.agent) &&
    (!filter.type || e.type === filter.type) &&
    (!filter.status || (filter.status === 'error' ? (e.status === 'error' || e.type === 'error') : e.status === filter.status)) &&
    (!filter.q || JSON.stringify(e).toLowerCase().includes(filter.q.toLowerCase())))
  const types = [...new Set(events.map(e => e.type))].sort()

  const approve = () => wrap(() => api(`/runs/${runId}/approve`, { method: 'POST' }))
  const rejectGate = () => {
    const reason = prompt('Why are you rejecting? (recorded in the event log)') || ''
    return wrap(() => api(`/runs/${runId}/reject`, { method: 'POST', body: { reason } }))
  }
  const resume = () => wrap(() => api(`/runs/${runId}/resume`, { method: 'POST' }))

  const rejectedFindings = events.filter(e => e.type === 'citation_rejected')
  const allOptions = Object.entries(cf.tool_landscape || {}).flatMap(([cat, os]) => os.map(o => ({ ...o, cat })))

  return (
    <div className="space-y-3">
      {/* header + flow */}
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-lg font-bold text-zinc-100 font-mono">{runId}</h1>
        <Pill kind={statusKind(sm.status)}>{sm.status.slice(0, 60)}</Pill>
        <span className="text-xs text-zinc-500">{fmtUsd(sm.cost_spent_usd)} · {sm.llm_calls} calls</span>
        {(sm.status.startsWith('error') || sm.status === 'paused_budget') && <>
          <Btn variant="primary" onClick={resume}>Retry / resume</Btn>
          <Btn onClick={() => setRetry(true)}>Retry with different model…</Btn></>}
      </div>

      <Card>
        <div className="flex items-center gap-2 flex-wrap">
          {AGENTS.map((a, i) => (
            <React.Fragment key={a}>
              <div className={`px-4 py-2 rounded-lg border text-center
                ${states[a] === 'running' ? 'border-sky-500 bg-sky-950' :
                  states[a] === 'waiting' ? 'border-amber-500 bg-amber-950/40' :
                  states[a] === 'error' ? 'border-red-600 bg-red-950/40' :
                  states[a] === 'done' ? 'border-emerald-700' : 'border-zinc-700'}`}>
                <div className="text-sm font-semibold text-zinc-200 capitalize">{a}</div>
                <Pill kind={states[a]} />
              </div>
              {i < AGENTS.length - 1 && <div className="text-zinc-600 text-center text-[10px] leading-tight">
                →<br />{['confirm', 'validate', 'approve'][i]}</div>}
            </React.Fragment>
          ))}
        </div>
        <ResearchDiagram events={events} status={sm.status} metrics={metrics} />
      </Card>

      <MetricsStrip m={metrics} cf={cf} />
      <QuotaStrip visible={sm.status.startsWith('running') || sm.status.startsWith('awaiting')} />

      {gate && (
        <div className="border border-amber-600 bg-amber-950/30 rounded-lg p-4">
          <p className="font-semibold text-amber-300">⏸ Waiting for you: {GATE_LABEL[gate] || gate}</p>
          <Json value={run.gate_payload} className="max-h-64 overflow-y-auto my-2" />
          <div className="flex gap-2">
            <Btn variant="approve" onClick={approve}>Approve & continue</Btn>
            <Btn variant="danger" onClick={rejectGate}>Reject</Btn>
            <Btn onClick={() => setRetry(true)}>Change model first…</Btn>
          </div>
        </div>
      )}
      {sm.status.startsWith('error') && (
        <div className="border border-red-700 bg-red-950/30 rounded-lg p-4 text-sm">
          <p className="text-red-300 break-words">{sm.status}</p>
          {sm.suggested_fix && <p className="text-zinc-400 mt-1">class <b className="text-amber-400">{sm.error_class}</b> — {sm.suggested_fix}</p>}
        </div>
      )}
      <ErrorNote>{actErr}</ErrorNote>

      {/* tabs */}
      <div className="flex gap-1 border-b border-zinc-800">
        {[['events', `Events (${events.length})`], ['problems', `Problems (${problems.length})`],
          ['results', `Results (${sm.findings})`], ['files', 'Files & Inputs'], ['casefile', 'CaseFile']].map(([k, label]) => (
          <button key={k} onClick={() => setTab(k)}
            className={`px-4 py-2 text-sm rounded-t ${tab === k ? 'bg-zinc-900 text-zinc-100 border border-zinc-800 border-b-zinc-900' : 'text-zinc-500 hover:text-zinc-300'}
              ${k === 'problems' && problems.length ? 'text-red-400' : ''}`}>{label}</button>
        ))}
      </div>

      {tab === 'events' && (
        <Card title="Live event stream" right={
          <div className="flex gap-2">
            <Select className="!w-28 !py-1 text-xs" value={filter.agent} onChange={e => setFilter({ ...filter, agent: e.target.value })}>
              <option value="">all agents</option>{['server', 'human', ...AGENTS].map(a => <option key={a}>{a}</option>)}</Select>
            <Select className="!w-36 !py-1 text-xs" value={filter.type} onChange={e => setFilter({ ...filter, type: e.target.value })}>
              <option value="">all types</option>{types.map(t => <option key={t}>{t}</option>)}</Select>
            <Select className="!w-24 !py-1 text-xs" value={filter.status} onChange={e => setFilter({ ...filter, status: e.target.value })}>
              <option value="">any status</option><option value="ok">ok</option><option value="error">error</option><option value="retrying">retrying</option></Select>
            <Input className="!w-44 !py-1 text-xs" placeholder="search…" value={filter.q} onChange={e => setFilter({ ...filter, q: e.target.value })} />
          </div>}>
          <div className="max-h-[520px] overflow-y-auto space-y-px">
            {shown.length === 0 && <p className="text-zinc-600 text-sm">no events match</p>}
            {shown.map(e => <React.Fragment key={e.seq}>
              {e.type === 'round_complete' && <div className="text-center text-[11px] text-sky-500 py-1 border-t border-b border-zinc-800 my-1">
                ── round {e.round}/{e.of} complete · {e.options} options · {e.findings} findings · {e.gaps} gaps · {fmtUsd(e.spent_usd)} ──</div>}
              <EventRow e={e} onOpen={setDetail} />
            </React.Fragment>)}
          </div>
        </Card>
      )}

      {tab === 'problems' && (
        <Card title="Problems — failures surface here first">
          {problems.length === 0 && <p className="text-emerald-500 text-sm">No failures recorded for this run.</p>}
          <div className="space-y-2">
            {problems.slice().reverse().map(e => (
              <div key={e.seq} className="border border-red-900 bg-red-950/30 rounded p-3 text-sm">
                <div className="flex items-center gap-3 flex-wrap">
                  <b className="text-red-300">{e.error_class || 'error'}</b>
                  <span className="text-zinc-500 text-xs">#{e.seq} · {e.agent}{e.worker ? '/' + e.worker : ''} · {e.type}</span>
                  {e.recovered !== undefined && <Pill kind={e.recovered ? 'ok' : 'error'}>{e.recovered ? 'recovered' : 'not recovered'}</Pill>}
                  <Btn className="!py-0.5 !px-2 text-xs ml-auto" onClick={() => setDetail(e)}>full detail</Btn>
                  <Btn className="!py-0.5 !px-2 text-xs" variant="primary" onClick={() => setRetry(true)}>retry step</Btn>
                </div>
                <p className="text-red-200/80 mt-1 break-words">{e.error}</p>
                {e.suggested_fix && <p className="text-zinc-400 text-xs mt-1">fix: {e.suggested_fix}</p>}
                {e.impact && <p className="text-zinc-500 text-xs">impact: {e.impact}</p>}
                {e.type === 'source_call' && <SourceCallRetry runId={runId} e={e} />}
              </div>))}
          </div>
        </Card>
      )}

      {tab === 'results' && (
        <div className="space-y-3">
          {cf.suitability && <Card title="Verdict">
            <p className="text-2xl font-bold text-emerald-400">{cf.suitability.verdict}</p>
            <p className="text-xs text-zinc-400 mt-1">{Object.entries(cf.suitability.scores || {}).map(([k, v]) => `${k} ${v}/10`).join(' · ')}</p>
            <p className="text-sm text-zinc-300 mt-2 whitespace-pre-wrap">{cf.suitability.rationale}</p>
            {cf.suitability.better_path && <p className="text-sm text-amber-300 mt-1">Better path: {cf.suitability.better_path}</p>}
          </Card>}
          <Card title={`Tool landscape (${allOptions.length} options)`}>
            <Table headers={['option', 'category', 'similarity', 'matched/missing', 'build $ est', 'monthly $ est', 'fit', 'exists?', 'sources']}
              rows={allOptions.sort((a, b) => (b.similarity?.index || 0) - (a.similarity?.index || 0)).map(o => [
                <span><a className="text-sky-400 hover:underline" href={o.url} target="_blank" rel="noreferrer">{o.name}</a>
                  {o.vendor_only && <span className="block text-amber-400 text-[10px]">⚑ vendor, unverified</span>}
                  {o.community_only && <span className="block text-red-400 text-[10px]">⚑ anecdote-only evidence</span>}</span>,
                o.cat, `${o.similarity?.index ?? 0}/100`,
                `${o.similarity?.matched?.length || 0}/${o.similarity?.missing?.length || 0}`,
                `${Math.round(o.costs?.build_cost_usd_low || 0)}–${Math.round(o.costs?.build_cost_usd_high || 0)}`,
                (o.costs?.monthly_operation_usd || 0).toFixed(0),
                o.scores?.capability_fit ?? '—',
                o.similarity?.existing_solution ? <b className="text-red-400">yes</b> : 'no',
                o.finding_ids?.length || 0])} />
          </Card>
          <Card title={`Findings (${(cf.findings || []).length}) — every claim links to its source`}>
            <Table headers={['id', 'kind', 'claim', 'source', 'verified']}
              rows={(cf.findings || []).map(f => [
                <span className="font-mono">{f.id}</span>,
                <Pill kind={f.kind === 'fact' ? 'ok' : f.kind === 'estimate' ? 'waiting' : 'error'}>{f.kind}{f.vendor_claim ? ' ⚑' : ''}</Pill>,
                <span className="text-zinc-300">{f.claim}</span>,
                <a className="text-sky-400 hover:underline break-all" href={f.source.url} target="_blank" rel="noreferrer">{f.source.title || f.source.url}</a>,
                f.source.verified ? '✅' : '—'])} />
          </Card>
          <SourceUsage runId={runId} />
          <Card title={`Rejected by citation check (${rejectedFindings.length})`}>
            <Table headers={['finding', 'url', 'action', 'reason']} empty="nothing rejected"
              rows={rejectedFindings.map(e => [
                <span className="font-mono">{e.finding_id}</span>,
                <span className="break-all text-zinc-500">{e.url}</span>, e.action, e.reason])} />
          </Card>
          {filesD?.files?.find(f => f.name === 'report.html' && f.available) && (
            <Card title="Detailed report" right={<a className="text-sky-400 text-xs hover:underline" href={`/runs/${runId}/report`} target="_blank" rel="noreferrer">open in tab ↗</a>}>
              <iframe title="report" src={`/runs/${runId}/report`} className="w-full h-[600px] bg-white rounded" />
            </Card>)}
        </div>
      )}

      {tab === 'files' && (
        <div className="grid lg:grid-cols-2 gap-3">
          <Card title="Stage inputs — what each agent needs">
            {(inputsD?.stages || []).map(s => (
              <div key={s.agent} className="flex items-start gap-3 py-2 border-b border-zinc-800/60 last:border-0">
                <Pill kind={s.satisfied ? 'ok' : 'pending'}>{s.satisfied ? 'ready' : 'not yet'}</Pill>
                <div>
                  <p className="text-sm font-semibold text-zinc-200 capitalize">{s.agent}</p>
                  <p className="text-xs text-zinc-500">needs: {s.needs.join(', ')}</p>
                  <p className="text-xs text-zinc-400 mt-0.5">{s.files}{s.staged_files ? ` (${s.staged_files} staged)` : ''}</p>
                </div>
              </div>))}
          </Card>
          <Card title="Downloads">
            {(filesD?.files || []).map(f => (
              <div key={f.name} className="flex items-center gap-3 py-1.5 text-sm border-b border-zinc-800/60 last:border-0">
                <span className="font-mono text-zinc-300">{f.name}</span>
                {f.available ? <>
                  <span className="text-zinc-600 text-xs">{(f.size / 1024).toFixed(1)} KB</span>
                  <a className="ml-auto text-sky-400 text-xs hover:underline" href={f.url} download>download</a>
                </> : <span className="ml-auto text-zinc-600 text-xs">not generated yet</span>}
              </div>))}
            <p className="text-[11px] text-zinc-600 mt-2">Serverless note: run state is ephemeral — download as soon as ready.</p>
          </Card>
          <Card title="Staged internal documents (research grounding)" className="lg:col-span-2">
            <Uploads runId={runId} />
          </Card>
        </div>
      )}

      {tab === 'casefile' && (
        <Card title="CaseFile inspector — the single source of truth" right={
          <div className="flex items-center gap-2 text-xs">
            <span className="text-zinc-500">what did each agent add?</span>
            <Select className="!w-44 !py-1" value={snapshotA} onChange={e => setSnapshotA(e.target.value)}>
              <option value="">full casefile</option>
              {AGENTS.map(a => <option key={a} value={a}>after {a} (snapshot)</option>)}
            </Select>
          </div>}>
          <SnapshotView runId={runId} agent={snapshotA} casefile={cf} />
        </Card>
      )}

      {detail && <EventDetail e={detail} onClose={() => setDetail(null)}
        onRetry={(sm.status.startsWith('error') || sm.status === 'paused_budget') ? () => { setDetail(null); setRetry(true) } : null} />}
      {retry && <RetryModal runId={runId} providers={providers || []} onClose={() => setRetry(false)} onDone={() => setErr(null)} />}
    </div>
  )
}

/** one-click retry of a failed source call, optionally forcing a different provider */
function SourceCallRetry({ runId, e }) {
  const [provider, setProvider] = useState('')
  const [result, setResult] = useState(null)
  const { busy, err, wrap } = useAsync()
  const providers = e.endpoint === 'read'
    ? ['jina', 'firecrawl', 'builtin']
    : ['tavily', 'zenserp', 'wikipedia', 'github', 'openalex', 'ddg_web', 'algolia_hn']
  const go = () => wrap(async () => {
    const body = { worker: e.worker || 'low_code', force_provider: provider || null }
    if (e.endpoint === 'read') body.url = e.url; else body.query = e.query || e.url
    setResult(await api(`/runs/${runId}/source-retry`, { method: 'POST', body }))
  })
  return (
    <div className="flex items-center gap-2 mt-2 text-xs">
      <Select className="!w-40 !py-1" value={provider} onChange={ev => setProvider(ev.target.value)}>
        <option value="">same chain</option>
        {providers.filter(p => p !== e.provider).map(p => <option key={p} value={p}>force {p}</option>)}
      </Select>
      <Btn className="!py-0.5 !px-2 text-xs" disabled={busy} onClick={go}>Retry this call</Btn>
      {result && <span className={result.ok ? 'text-emerald-400' : 'text-red-400'}>
        {result.ok ? `✓ ok${result.chars ? ` (${result.chars} chars, cached for the agent)` : ` (${result.results?.length ?? 0} results)`}` : '✗ still failing'}</span>}
      <ErrorNote>{err}</ErrorNote>
    </div>
  )
}

function SnapshotView({ runId, agent, casefile }) {
  const { data } = usePoll(agent ? `/runs/${runId}/snapshots/${agent}` : null, 60000, [agent])
  const shown = agent ? data : casefile
  const prevAgent = agent ? AGENTS[AGENTS.indexOf(agent) - 1] : null
  const { data: prev } = usePoll(prevAgent ? `/runs/${runId}/snapshots/${prevAgent}` : null, 60000, [prevAgent])
  if (!shown) return <p className="text-zinc-600 text-sm">no snapshot yet{agent ? ` — ${agent} hasn't completed` : ''}</p>
  const diff = agent && prev ? diffSummary(prev, shown) : agent ? diffSummary({}, shown) : null
  return (
    <div>
      {diff && <div className="mb-3 text-xs bg-zinc-950 border border-zinc-800 rounded p-3">
        <p className="text-zinc-500 uppercase text-[10px] tracking-widest mb-1">changes vs {prevAgent || 'empty casefile'}</p>
        {diff.length === 0 ? <p className="text-zinc-600">no field changes</p> :
          diff.map(d => <p key={d.key}><b className="text-emerald-400">{d.key}</b> <span className="text-zinc-400">{d.note}</span></p>)}
      </div>}
      <Json value={shown} className="max-h-[560px] overflow-y-auto" />
    </div>
  )
}

function diffSummary(a, b) {
  const out = []
  for (const key of Object.keys(b)) {
    const av = a?.[key], bv = b[key]
    if (JSON.stringify(av) === JSON.stringify(bv)) continue
    if (Array.isArray(bv)) out.push({ key, note: `${(av || []).length} → ${bv.length} items` })
    else if (typeof bv === 'object' && bv !== null) out.push({ key, note: 'updated' })
    else out.push({ key, note: `${JSON.stringify(av)} → ${JSON.stringify(bv)?.slice(0, 80)}` })
  }
  return out.filter(d => !['updated_at', 'llm_calls', 'cost_spent_usd', 'status', 'next_agent'].includes(d.key))
}
