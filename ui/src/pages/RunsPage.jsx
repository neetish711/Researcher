import React, { useEffect, useState } from 'react'
import { api, usePoll, KEY_RE, KEY_MSG, fmtUsd, ago } from '../api.js'
import { Btn, Card, ErrorNote, Field, Input, Json, Label, Modal, Pill, Select, statusKind, useAsync } from '../lib.jsx'

const ROLES = ['lead', 'worker', 'classify', 'report']

/** provider → searchable model picker, populated from the key's list-models call */
function ModelPicker({ providers, value, onChange, listId }) {
  const [models, setModels] = useState([])
  const [note, setNote] = useState('')
  const provider = value.provider || providers[0]?.name || ''

  useEffect(() => {
    let dead = false
    setModels([]); setNote('')
    if (!provider) return
    api(`/providers/${provider}/models`)
      .then(d => { if (!dead) { setModels(d.models); setNote(`${d.models.length} models from this key`) } })
      .catch(e => { if (!dead) setNote(`no model list (${e.message.slice(0, 80)}) — type a model id`) })
    return () => { dead = true }
  }, [provider])

  const keyErr = value.model && KEY_RE.test(value.model) ? KEY_MSG : null
  return (
    <div className="flex gap-2 items-start">
      <div className="w-40 shrink-0">
        <Select value={provider} onChange={e => onChange({ ...value, provider: e.target.value, model: '' })}>
          {providers.map(p => <option key={p.name} value={p.name}>{p.name}</option>)}
        </Select>
      </div>
      <div className="flex-1">
        <Input list={listId} value={value.model || ''} placeholder={models.length ? 'search models…' : 'model id'}
               onChange={e => onChange({ ...value, model: e.target.value })}
               className={keyErr ? 'border-red-600' : ''} />
        <datalist id={listId}>{models.map(m => <option key={m} value={m} />)}</datalist>
        <p className={`text-[11px] mt-0.5 ${keyErr ? 'text-red-400' : 'text-zinc-600'}`}>{keyErr || note}</p>
      </div>
      <div className="w-20 shrink-0">
        <Input type="number" step="0.1" min="0" max="2" placeholder="temp"
               value={value.temp ?? ''} onChange={e => onChange({ ...value, temp: e.target.value })} />
      </div>
    </div>
  )
}

export default function RunsPage() {
  const { data: runs } = usePoll('/runs', 4000)
  const { data: providers } = usePoll('/providers', 30000)
  const { data: sources } = usePoll('/sources', 30000)
  const { data: flow } = usePoll('/config/flow', 60000)
  const { busy, err, setErr, wrap } = useAsync()

  const [problem, setProblem] = useState('')
  const [budget, setBudget] = useState('')
  const [roleCfg, setRoleCfg] = useState({})       // {role: {provider, model, temp}}
  const [perRole, setPerRole] = useState(false)
  const [srcSel, setSrcSel] = useState(null)       // null = all enabled
  const [dry, setDry] = useState(null)

  const provList = (providers || [])
  const enabledSources = (sources || []).filter(s => s.enabled)
  const defaults = flow?.roles || {}

  const buildBody = () => {
    const lead = roleCfg.lead || {}
    if (!lead.model && !perRole) throw new Error('pick a model (populated from your provider key)')
    const body = { problem, budget: budget || null, provider: lead.provider || provList[0]?.name || null }
    if (perRole) {
      body.models = {}; body.temperatures = {}
      for (const r of ROLES) {
        const c = roleCfg[r] || {}
        if (c.model) body.models[r] = c.model
        if (c.temp !== undefined && c.temp !== '') body.temperatures[r] = Number(c.temp)
      }
      if (!Object.keys(body.models).length) throw new Error('set at least one role model')
    } else {
      body.model = lead.model
      if (lead.temp) body.temperatures = Object.fromEntries(ROLES.map(r => [r, Number(lead.temp)]))
    }
    if (srcSel) body.sources = srcSel
    for (const m of [body.model, ...Object.values(body.models || {})])
      if (m && KEY_RE.test(m)) throw new Error(KEY_MSG)
    return body
  }

  const start = () => wrap(async () => {
    const d = await api('/runs', { method: 'POST', body: buildBody() })
    window.location.hash = `#/runs/${d.run_id}`
  })

  const dryRun = () => wrap(async () => {
    const b = buildBody()
    setDry({ loading: true })
    try {
      setDry(await api('/dryrun', { method: 'POST', body: { problem: b.problem, provider: b.provider, model: b.model, models: b.models } }))
    } catch (e) { setDry(null); throw e }
  })

  const rolesToShow = perRole ? ROLES : ['lead']
  return (
    <div className="grid lg:grid-cols-[440px_1fr] gap-4">
      <Card title="New run">
        {provList.length === 0 && (
          <p className="text-amber-400 text-sm mb-3">No providers configured — add an API key under
            <a className="underline ml-1" href="#/settings/providers">Settings → Providers</a> first.</p>)}
        <Field label="Business problem"
               hint="the interview is skipped in server mode — include process, volumes, tools, data">
          <textarea value={problem} onChange={e => setProblem(e.target.value)} rows={6}
            className="bg-zinc-950 border border-zinc-700 rounded px-2.5 py-1.5 text-sm w-full focus:outline-none focus:border-sky-600"
            placeholder="What is broken, for whom, current workflow, volumes, error tolerance…" />
        </Field>

        <div className="flex items-center justify-between mt-3">
          <Label>Models <span className="text-zinc-600">(from your validated keys — nothing is pinned)</span></Label>
          <button className="text-xs text-sky-500 hover:underline" onClick={() => setPerRole(!perRole)}>
            {perRole ? 'simple: one model' : 'advanced: per-role models'}</button>
        </div>
        <div className="space-y-2">
          {rolesToShow.map(role => (
            <div key={role}>
              {perRole && <p className="text-[11px] text-zinc-500 mb-0.5">{role}
                <span className="text-zinc-700 ml-2">default temp {defaults[role]?.temperature ?? '—'}</span></p>}
              <ModelPicker providers={provList} listId={`models-${role}`}
                           value={roleCfg[role] || {}} onChange={v => setRoleCfg({ ...roleCfg, [role]: v })} />
            </div>
          ))}
        </div>

        <Field label="Research budget" hint="wall clock, e.g. 30m / 4h (caps also in research.yaml)">
          <Input value={budget} onChange={e => setBudget(e.target.value)} placeholder="4h" />
        </Field>

        <Label>Research sources for this run
          <span className="text-zinc-600 ml-1">({enabledSources.length} enabled globally)</span></Label>
        <div className="flex flex-wrap gap-1.5">
          {enabledSources.map(s => {
            const on = !srcSel || srcSel.includes(s.id)
            return <button key={s.id} onClick={() => {
              const cur = srcSel || enabledSources.map(x => x.id)
              const next = on ? cur.filter(x => x !== s.id) : [...cur, s.id]
              setSrcSel(next.length === enabledSources.length ? null : next)
            }} className={`px-2 py-0.5 rounded text-xs border ${on ? 'border-sky-700 bg-sky-950 text-sky-300' : 'border-zinc-700 text-zinc-500'}`}>
              {s.name}</button>
          })}
        </div>

        <div className="flex gap-2 mt-4">
          <Btn variant="primary" disabled={busy || !problem.trim()} onClick={start}>Start run</Btn>
          <Btn disabled={busy || !problem.trim()} onClick={dryRun}>Dry run / explain plan</Btn>
        </div>
        <ErrorNote>{err}</ErrorNote>
      </Card>

      <Card title={`Runs (${runs?.length ?? '…'})`}>
        {(runs || []).length === 0 && <p className="text-zinc-600 text-sm">No runs yet. On serverless hosting run
          state is ephemeral — download files as soon as they're ready.</p>}
        <div className="space-y-1.5">
          {(runs || []).map(r => (
            <a key={r.run_id} href={`#/runs/${r.run_id}`}
               className="flex items-center gap-3 px-3 py-2 rounded border border-zinc-800 hover:border-zinc-600 bg-zinc-950/50">
              <span className="font-mono text-zinc-200">{r.run_id}</span>
              <Pill kind={statusKind(r.status)}>{r.status.slice(0, 40)}</Pill>
              {r.awaiting_gate && <span className="text-amber-400 text-xs">needs approval</span>}
              <span className="text-zinc-500 text-xs truncate flex-1">{r.problem}</span>
              {r.verdict && <span className="text-emerald-400 text-xs font-semibold">{r.verdict}</span>}
              <span className="text-zinc-500 text-xs">{r.findings}f · {r.options}o · {fmtUsd(r.cost_spent_usd)}</span>
              <span className="text-zinc-600 text-xs w-16 text-right">{ago(r.updated_at)}</span>
            </a>
          ))}
        </div>
      </Card>

      {dry && (
        <Modal title="Dry run — what this flow WOULD do (nothing executed)" onClose={() => setDry(null)} wide>
          {dry.loading ? <p className="text-zinc-400">Planning… (this makes 2 LLM calls, no workers run)</p> : <>
            <div className="grid md:grid-cols-3 gap-4 mb-4">
              <Card title="Agents in order">
                <ol className="text-sm space-y-1">{dry.flow.map((s, i) =>
                  <li key={i}>{i + 1}. <b>{s.agent}</b>{s.gate_after !== 'none' &&
                    <span className="text-amber-400 text-xs ml-2">⏸ gate: {s.gate_after}</span>}</li>)}</ol>
              </Card>
              <Card title="Cost estimate (upper bound)">
                <p className="text-2xl font-bold text-zinc-100">{fmtUsd(dry.estimate.cost_usd_upper_bound)}</p>
                <p className="text-xs text-zinc-500">≤ {dry.estimate.llm_calls_upper_bound} LLM calls ·
                  {dry.estimate.max_rounds} rounds × {dry.estimate.workers} workers</p>
                <ul className="text-[11px] text-zinc-600 mt-2 list-disc pl-4">
                  {dry.estimate.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
                <p className="text-[11px] text-zinc-500 mt-2">this dry run cost {fmtUsd(dry.dry_run_cost_usd)}</p>
              </Card>
              <Card title="Example worker queries">
                <ul className="text-xs space-y-1 text-zinc-400">
                  {Object.values(dry.example_queries)[0]?.slice(0, 8).map((q, i) => <li key={i}>• {q}</li>)}</ul>
              </Card>
            </div>
            <Card title="Generated research plan"><Json value={dry.research_plan} className="max-h-72 overflow-y-auto" /></Card>
          </>}
        </Modal>
      )}
    </div>
  )
}
