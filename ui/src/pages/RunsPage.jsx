import React, { useEffect, useState } from 'react'
import { api, usePoll, KEY_RE, KEY_MSG, fmtUsd, ago } from '../api.js'
import { Btn, Card, ErrorNote, Field, Input, Json, Label, Modal, Pill, Select, statusKind, useAsync } from '../lib.jsx'

const ROLES = ['lead', 'worker', 'classify', 'report']

/** provider → model picker. Models auto-load from the key; picking is one click.
    Providers WITH keys sort first and are the default — never the keyless env stub. */
function ModelPicker({ providers, value, onChange, listId }) {
  const [models, setModels] = useState([])
  const [note, setNote] = useState('')
  const [manual, setManual] = useState(false)
  const ordered = [...providers].sort((a, b) => (b.has_key ? 1 : 0) - (a.has_key ? 1 : 0))
  const provider = value.provider || ordered[0]?.name || ''

  useEffect(() => {
    let dead = false
    setModels([]); setNote('loading models from this key…')
    if (!provider) return
    api(`/providers/${provider}/models`)
      .then(d => {
        if (dead) return
        setModels(d.models); setManual(d.models.length === 0)
        setNote(d.models.length ? `${d.models.length} models — pick one` : 'no list endpoint — type a model id')
      })
      .catch(e => {
        if (dead) return
        setManual(true)
        setNote(/no api key/i.test(e.message)
          ? `provider "${provider}" has no API key — add one under Settings → Providers`
          : `no model list (${e.message.slice(0, 80)}) — type a model id`)
      })
    return () => { dead = true }
  }, [provider])

  const keyErr = value.model && KEY_RE.test(value.model) ? KEY_MSG : null
  return (
    <div className="flex gap-2 items-start">
      <div className="w-40 shrink-0">
        <Select value={provider} onChange={e => onChange({ ...value, provider: e.target.value, model: '' })}>
          {ordered.map(p => <option key={p.name} value={p.name}>
            {p.name}{p.has_key ? '' : ' (no key)'}</option>)}
        </Select>
      </div>
      <div className="flex-1">
        {!manual && models.length > 0 ? (
          <Select value={value.model || ''} onChange={e => onChange({ ...value, model: e.target.value })}>
            <option value="" disabled>— click to pick a model —</option>
            {models.map(m => <option key={m} value={m}>{m}</option>)}
          </Select>
        ) : (
          <Input list={listId} value={value.model || ''} placeholder="model id"
                 onChange={e => onChange({ ...value, model: e.target.value })}
                 className={keyErr ? 'border-red-600' : ''} />
        )}
        <p className={`text-[11px] mt-0.5 ${keyErr ? 'text-red-400' : 'text-zinc-600'}`}>
          {keyErr || note}
          {!manual && models.length > 0 &&
            <button className="ml-2 text-sky-600 hover:underline" onClick={() => setManual(true)}>type manually</button>}
        </p>
      </div>
      <div className="w-20 shrink-0">
        <Input type="number" step="0.1" min="0" max="2" placeholder="temp"
               value={value.temp ?? ''} onChange={e => onChange({ ...value, temp: e.target.value })} />
      </div>
    </div>
  )
}

const INTAKE_FIELDS = [
  ['problem', 'What is the problem? *', 'What is broken or slow, and what does “good” look like?'],
  ['context', 'Who has it & how does it work today?', 'Team, current process step by step, hand-offs'],
  ['volume', 'Volume & time', 'How often, how many items, minutes per item, error rate'],
  ['tools', 'Tools & data available', 'Systems used today; what data exists, where, how sensitive'],
  ['constraints', 'Constraints', 'Compliance/security requirements, budget appetite, deadlines, must-stay-human steps'],
  ['metric', 'Success metric', 'The number that should move, and its current baseline'],
]

export default function RunsPage() {
  const { data: runs } = usePoll('/runs', 4000)
  const { data: providers } = usePoll('/providers', 30000)
  const { data: rsources } = usePoll('/research-sources', 30000)
  const { data: flow } = usePoll('/config/flow', 60000)
  const { busy, err, setErr, wrap } = useAsync()

  const [title, setTitle] = useState('')
  const [intake, setIntake] = useState({})
  const [budget, setBudget] = useState('')
  const [roleCfg, setRoleCfg] = useState(() => {
    // a model clicked on the Providers page pre-fills the run form
    try {
      const pref = JSON.parse(localStorage.getItem('preferred_model') || 'null')
      return pref ? { lead: { provider: pref.provider, model: pref.model } } : {}
    } catch { return {} }
  })                                               // {role: {provider, model, temp}}
  const [perRole, setPerRole] = useState(false)
  const [srcSel, setSrcSel] = useState(null)       // null = all registered
  const [dry, setDry] = useState(null)
  const [forecast, setForecast] = useState(null)

  const provList = (providers || [])
  const defaults = flow?.roles || {}
  // the run's selectable source universe = quota-guarded providers + keyless + custom
  const allSources = rsources ? [
    ...rsources.providers.map(p => ({ id: p.id, name: p.name, status: p.status })),
    ...rsources.keyless.map(k => ({ id: k.id, name: k.name, status: 'connected' })),
    ...rsources.custom.filter(c => c.enabled).map(c => ({ id: c.id, name: c.name, status: 'connected' })),
  ] : []

  const composedProblem = () => {
    const parts = []
    if (intake.problem) parts.push(intake.problem)
    for (const [key, label] of [['context', 'Who has it / current process'], ['volume', 'Volume & time'],
                                ['tools', 'Tools & data available'], ['constraints', 'Constraints'],
                                ['metric', 'Success metric & baseline']])
      if (intake[key]) parts.push(`${label}: ${intake[key]}`)
    return parts.join('\n\n')
  }

  const buildBody = () => {
    const problem = composedProblem()
    if (!problem.trim()) throw new Error('describe the problem first')
    const lead = roleCfg.lead || {}
    if (!lead.model && !perRole) throw new Error('pick a model (populated from your provider key)')
    const usable = provList.filter(p => p.has_key)
    const body = { problem, title: title || intake.problem?.slice(0, 80) || '',
                   budget: budget || null,
                   provider: lead.provider || usable[0]?.name || provList[0]?.name || null }
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
    <div className="grid lg:grid-cols-[460px_1fr] gap-4">
      <Card title="New idea">
        {!provList.some(p => p.has_key) && (
          <div className="border border-amber-600 bg-amber-950/40 rounded-lg p-3 mb-4 text-sm">
            <p className="font-bold text-amber-300">⚠ Nothing can run yet — no LLM API key is configured.</p>
            <ol className="list-decimal pl-5 text-zinc-300 mt-1 space-y-0.5">
              <li><a className="text-sky-400 underline" href="#/settings/providers">Add a provider key</a> (Anthropic,
                OpenAI, or any compatible endpoint) and press <b>Test connection</b>.</li>
              <li>Come back here — the model dropdown fills from your key.</li>
              <li>Describe your idea and press Start run.</li>
            </ol>
            <p className="text-[11px] text-zinc-500 mt-1.5">Hosting on Vercel? Also set <code>CRED_SECRET</code> (and
              optionally <code>LLM_API_KEY</code>) in the project env — the serverless disk is wiped on recycle,
              so vault-only keys can vanish without it.</p>
          </div>)}
        <Field label="Idea title"><Input value={title} onChange={e => setTitle(e.target.value)}
          placeholder="e.g. Automate invoice triage" /></Field>
        {INTAKE_FIELDS.map(([key, label, hint]) => (
          <Field key={key} label={label} hint={hint}>
            <textarea value={intake[key] || ''} onChange={e => setIntake({ ...intake, [key]: e.target.value })}
              rows={key === 'problem' ? 3 : 2}
              className="bg-zinc-950 border border-zinc-700 rounded px-2.5 py-1.5 text-sm w-full focus:outline-none focus:border-sky-600" />
          </Field>))}
        <p className="text-[11px] text-zinc-600 mt-1">Anything you leave blank, the discovery agent will
          ask about — you answer its questions right in the run view.</p>

        <div className="flex items-center justify-between mt-3">
          <Label>Model <span className="text-zinc-600">(required — from your key; nothing is pinned)</span></Label>
          <button className="text-xs text-sky-500 hover:underline" onClick={() => setPerRole(!perRole)}>
            {perRole ? 'simple: one model' : 'per-role models'}</button>
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

        <details className="mt-3">
          <summary className="text-xs text-sky-500 cursor-pointer">Advanced: sources, budget</summary>
          <Field label="Research budget" hint="wall clock, e.g. 30m / 4h (caps also in research.yaml)">
            <Input value={budget} onChange={e => setBudget(e.target.value)} placeholder="4h" />
          </Field>
          <Label>Research sources for this run <span className="text-zinc-600">(quota-guarded; router picks per worker)</span></Label>
          <div className="flex flex-wrap gap-1.5">
            {allSources.map(s => {
              const on = !srcSel || srcSel.includes(s.id)
              return <button key={s.id} onClick={() => {
                const cur = srcSel || allSources.map(x => x.id)
                const next = on ? cur.filter(x => x !== s.id) : [...cur, s.id]
                setSrcSel(next.length === allSources.length ? null : next)
              }} title={s.status}
                className={`px-2 py-0.5 rounded text-xs border ${on ? 'border-sky-700 bg-sky-950 text-sky-300' : 'border-zinc-700 text-zinc-500'}
                  ${s.status === 'no_key' ? 'opacity-50' : ''}`}>
                {s.name}{s.status === 'no_key' ? ' (no key)' : ''}</button>
            })}
          </div>
        </details>

        <div className="flex gap-2 mt-4">
          <Btn variant="primary" disabled={busy || !(intake.problem || '').trim()} onClick={start}>Start run</Btn>
          <Btn disabled={busy || !(intake.problem || '').trim()} onClick={dryRun}>Dry run / explain plan</Btn>
          <Btn disabled={busy} onClick={() => wrap(async () => setForecast(await api('/forecast', { method: 'POST' })))}>
            Quota forecast</Btn>
        </div>
        <ErrorNote>{err}</ErrorNote>
      </Card>

      <Card title={`Idea portfolio (${runs?.length ?? '…'})`}>
        {(runs || []).length === 0 && <p className="text-zinc-600 text-sm">No ideas yet. On serverless hosting run
          state is ephemeral — download files as soon as they're ready.</p>}
        <div className="space-y-1.5">
          {(runs || []).map(r => (
            <a key={r.run_id} href={`#/runs/${r.run_id}`}
               className="flex items-center gap-3 px-3 py-2 rounded border border-zinc-800 hover:border-zinc-600 bg-zinc-950/50">
              <span className="text-zinc-100 font-semibold truncate max-w-[220px]">{r.title || r.run_id}</span>
              <Pill kind={statusKind(r.status)}>{r.status.slice(0, 40)}</Pill>
              {r.awaiting_gate && <span className="text-amber-400 text-xs">
                {r.open_questions_count ? `${r.open_questions_count} questions for you` : 'needs approval'}</span>}
              <span className="text-zinc-500 text-xs truncate flex-1">{r.problem}</span>
              {r.decision && <span className="text-emerald-400 text-xs font-bold uppercase">{r.decision}
                {r.recommended_option ? ` · ${r.recommended_option}` : ''}</span>}
              {!r.decision && r.verdict && <span className="text-emerald-500 text-xs">{r.verdict}</span>}
              <span className="text-zinc-500 text-xs">{r.findings}f · {fmtUsd(r.cost_spent_usd)}</span>
              <span className="text-zinc-600 text-xs w-16 text-right">{ago(r.updated_at)}</span>
            </a>
          ))}
        </div>
      </Card>

      {forecast && (
        <Modal title="Pre-run quota forecast — estimated source calls vs remaining free tier" onClose={() => setForecast(null)} wide>
          <table className="w-full text-sm">
            <thead><tr className="text-[11px] uppercase text-zinc-500 text-left">
              <th className="px-2 py-1">provider</th><th>estimated (upper bound)</th><th>remaining</th>
              <th>monthly cap</th><th>resets</th><th></th></tr></thead>
            <tbody>{forecast.providers.map(r => (
              <tr key={r.provider} className={`border-t border-zinc-800 ${r.would_exceed ? 'bg-red-950/40' : ''}`}>
                <td className="px-2 py-1.5 font-mono">{r.provider}</td>
                <td>{r.estimated_units.toLocaleString()} {r.unit}s</td>
                <td>{r.remaining == null ? '∞' : r.remaining.toLocaleString()}</td>
                <td>{r.monthly_quota == null ? '—' : r.monthly_quota.toLocaleString()}{r.assumed ? ' (assumed)' : ''}</td>
                <td className="text-zinc-500">{r.resets_on}</td>
                <td>{r.would_exceed && <b className="text-red-400">⚑ would exceed free tier</b>}</td>
              </tr>))}</tbody>
          </table>
          {forecast.would_exceed.length > 0
            ? <p className="text-amber-300 text-sm mt-3">⚠ {forecast.suggestion}</p>
            : <p className="text-emerald-400 text-sm mt-3">✓ fits inside every free tier{forecast.free_tier_only ? ' (and free_tier_only would refuse any breach pre-flight anyway)' : ''}</p>}
          <p className="text-[11px] text-zinc-600 mt-1">{forecast.note}</p>
        </Modal>
      )}

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
