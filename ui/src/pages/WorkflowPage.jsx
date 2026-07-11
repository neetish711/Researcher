import React, { useEffect, useState } from 'react'
import { api, usePoll } from '../api.js'
import { Btn, Card, ErrorNote, Pill, useAsync } from '../lib.jsx'

function PromptEditor({ name }) {
  const [content, setContent] = useState(null)
  const [saved, setSaved] = useState(false)
  const { busy, err, wrap } = useAsync()
  useEffect(() => {
    setContent(null); setSaved(false)
    if (name) api(`/config/prompts/${name}`).then(d => setContent(d.content)).catch(() => setContent('(prompt not found)'))
  }, [name])
  if (content === null) return <p className="text-zinc-600 text-sm">loading prompt…</p>
  return (
    <div>
      <textarea value={content} onChange={e => { setContent(e.target.value); setSaved(false) }} rows={16}
        className="w-full bg-zinc-950 border border-zinc-700 rounded p-3 text-xs font-mono text-zinc-300 focus:outline-none focus:border-sky-600" />
      <div className="flex items-center gap-3 mt-2">
        <Btn variant="primary" disabled={busy}
          onClick={() => wrap(async () => { await api(`/config/prompts/${name}`, { method: 'PUT', body: { content } }); setSaved(true) })}>
          Save prompt</Btn>
        {saved && <span className="text-emerald-400 text-xs">saved — applies to the next agent invocation</span>}
      </div>
      <ErrorNote>{err}</ErrorNote>
    </div>
  )
}

export default function WorkflowPage() {
  const { data: flow } = usePoll('/config/flow', 15000)
  const [sel, setSel] = useState(null)
  const [promptName, setPromptName] = useState(null)
  if (!flow) return <p className="text-zinc-500 p-8">loading flow.yaml…</p>
  const steps = flow.flow
  const selected = steps.find(s => s.agent === sel)

  return (
    <div className="space-y-4">
      <Card title="Configured pipeline — rendered from config/flow.yaml (edit the YAML, this diagram follows)"
            right={<span className="text-xs text-zinc-500">human gates: {String(flow.human_gates)}</span>}>
        <div className="flex items-center gap-2 flex-wrap">
          {steps.map((s, i) => (
            <React.Fragment key={s.agent}>
              <button onClick={() => { setSel(s.agent); setPromptName(s.prompt) }}
                className={`px-5 py-3 rounded-lg border text-left transition
                  ${sel === s.agent ? 'border-sky-500 bg-sky-950/60' : 'border-zinc-700 hover:border-zinc-500 bg-zinc-950/40'}`}>
                <p className="font-semibold text-zinc-100">{s.title || s.agent}</p>
                <p className="text-[11px] text-zinc-500">role: {s.role}</p>
              </button>
              {i < steps.length - 1 && (
                <div className="text-center text-[10px] text-zinc-500 leading-tight px-1">
                  →<br />{s.gate_after !== 'none' ? <span className="text-amber-400">⏸ {s.gate_after}</span> : ''}
                </div>)}
            </React.Fragment>
          ))}
        </div>
        {/* research sub-architecture */}
        <div className="mt-4 pt-4 border-t border-zinc-800">
          <p className="text-[11px] uppercase tracking-widest text-zinc-500 mb-2">Inside the Research node
            <span className="normal-case tracking-normal ml-2 text-zinc-600">
              budget: {flow.research?.budget?.max_rounds} rounds max · {flow.research?.budget?.max_workers} workers ·
              {' '}{flow.research?.budget?.max_tool_calls_per_worker} tool calls/worker · {flow.research?.budget?.max_wall_clock} wall clock</span></p>
          <div className="flex items-center gap-1.5 flex-wrap text-xs">
            {['lead plan', '⏸ human approval', null, 'synthesis (similarity · costs · scores)',
              'coverage check ⟲ loop', 'citation verification', 'reports (HTML + PPT)'].map((label, i) =>
              label === null ? (
                <div key="workers" className="flex flex-col gap-1">
                  {Object.entries(flow.research?.categories || {}).map(([k, desc]) =>
                    <div key={k} title={desc} className="px-2 py-0.5 rounded border border-zinc-700 text-zinc-400">👷 {k}</div>)}
                </div>
              ) : (
                <React.Fragment key={label}>
                  <div className={`px-2.5 py-1.5 rounded border ${label.startsWith('⏸') ? 'border-amber-700 text-amber-300' : 'border-zinc-700 text-zinc-300'}`}>{label}</div>
                  {i < 6 && <span className="text-zinc-600">→</span>}
                </React.Fragment>
              ))}
          </div>
          <p className="text-[11px] text-zinc-600 mt-2">Coverage targets: ≥{flow.research?.coverage?.min_options_per_category} options/category,
            ≥{flow.research?.coverage?.min_findings_per_option} findings/option, similarity + costs on all. The loop repeats
            (targeting gaps) until coverage or budget. During a run this diagram goes live in the Run Console.</p>
        </div>
      </Card>

      {selected && (
        <div className="grid lg:grid-cols-2 gap-4">
          <Card title={selected.title || selected.agent}>
            <p className="text-sm text-zinc-300">{selected.purpose}</p>
            <p className="text-[11px] uppercase tracking-widest text-zinc-500 mt-4 mb-1">Inputs it needs</p>
            <ul className="text-sm text-zinc-400 list-disc pl-5">{(selected.inputs || []).map((x, i) => <li key={i}>{x}</li>)}</ul>
            <p className="text-xs text-zinc-300 mt-2 bg-zinc-950 border border-zinc-800 rounded px-3 py-2">📎 {selected.files}</p>
            <p className="text-[11px] uppercase tracking-widest text-zinc-500 mt-4 mb-1">Outputs it produces</p>
            <ul className="text-sm text-zinc-400 list-disc pl-5">{(selected.outputs || []).map((x, i) => <li key={i}>{x}</li>)}</ul>
            <p className="text-[11px] uppercase tracking-widest text-zinc-500 mt-4 mb-1">Model / role</p>
            <p className="text-sm text-zinc-400">{selected.role} — temperature {flow.roles?.[selected.role.split(' ')[0]]?.temperature ?? '—'}
              <span className="text-zinc-600"> (model chosen per run; nothing pinned)</span></p>
            <p className="text-[11px] uppercase tracking-widest text-zinc-500 mt-4 mb-1">Guardrails</p>
            <ul className="text-sm text-zinc-400 list-disc pl-5">{(selected.guardrails || []).map((x, i) => <li key={i}>{x}</li>)}</ul>
            {selected.gate_after !== 'none' && <p className="text-amber-400 text-sm mt-3">⏸ Human gate after this agent: {selected.gate_after}</p>}
          </Card>
          <Card title="Prompt (versioned, editable)" right={
            <div className="flex gap-1">
              {[selected.prompt, ...(selected.sub_prompts || [])].map(p =>
                <button key={p} onClick={() => setPromptName(p)}
                  className={`px-2 py-0.5 rounded text-xs border ${promptName === p ? 'border-sky-600 text-sky-300' : 'border-zinc-700 text-zinc-500'}`}>{p}</button>)}
            </div>}>
            <PromptEditor name={promptName || selected.prompt} />
          </Card>
        </div>
      )}
      {!selected && <p className="text-zinc-600 text-sm">Click an agent node to see its purpose, inputs, outputs, guardrails, and editable prompt.</p>}
    </div>
  )
}
