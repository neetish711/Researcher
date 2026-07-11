import React, { useState } from 'react'
import { api, usePoll } from '../api.js'
import { Btn, Card, ErrorNote, Field, Input, Json, Modal, Pill, Select, useAsync } from '../lib.jsx'

const TIERS = ['primary', 'secondary', 'vendor', 'community']

function SourceRow({ s, onTest }) {
  const { err, wrap } = useAsync()
  const patch = (body) => wrap(() => api(`/sources/${s.id}`, { method: 'PATCH', body }))
  const [key, setKey] = useState('')
  return (
    <div className="border border-zinc-800 rounded-lg p-3 bg-zinc-950/40">
      <div className="flex items-center gap-3 flex-wrap">
        <button onClick={() => patch({ enabled: !s.enabled })}
          className={`w-9 h-5 rounded-full relative transition ${s.enabled ? 'bg-emerald-600' : 'bg-zinc-700'}`}>
          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all ${s.enabled ? 'left-4' : 'left-0.5'}`} />
        </button>
        <b className="text-zinc-200">{s.name}</b>
        {s.builtin ? <Pill kind="pending">built-in</Pill> : <Pill kind="running">custom</Pill>}
        <span className="text-xs text-zinc-500">{s.description}</span>
        <span className="ml-auto text-[11px] text-zinc-600">free tier: {s.free_tier || '—'}</span>
      </div>
      <div className="flex items-center gap-3 mt-2 flex-wrap text-xs">
        <label className="text-zinc-500">tier
          <Select className="!w-32 !py-1 ml-1 inline-block" value={s.tier} onChange={e => patch({ tier: e.target.value })}>
            {TIERS.map(t => <option key={t}>{t}</option>)}</Select></label>
        <label className="text-zinc-500">weight
          <Input className="!w-16 !py-1 ml-1 inline-block" type="number" step="0.1" min="0" max="2" defaultValue={s.weight}
            onBlur={e => patch({ weight: Number(e.target.value) })} /></label>
        <label className="text-zinc-500">rate/min
          <Input className="!w-16 !py-1 ml-1 inline-block" type="number" defaultValue={s.rate_limit_per_min}
            onBlur={e => patch({ rate_limit_per_min: Number(e.target.value) })} /></label>
        <label className="text-zinc-500">timeout s
          <Input className="!w-16 !py-1 ml-1 inline-block" type="number" defaultValue={s.timeout_s}
            onBlur={e => patch({ timeout_s: Number(e.target.value) })} /></label>
        <label className="text-zinc-500">key {s.key_fingerprint && <span className="font-mono text-zinc-400">{s.key_fingerprint}</span>}
          <Input className="!w-32 !py-1 ml-1 inline-block" type="password" placeholder="set key…" value={key}
            onChange={e => setKey(e.target.value)}
            onBlur={() => { if (key) { patch({ api_key: key }); setKey('') } }} /></label>
        <Btn className="!py-0.5 !px-2 text-xs" onClick={() => onTest(s)}>Test source</Btn>
        {!s.builtin && <button className="text-zinc-600 hover:text-red-400"
          onClick={() => wrap(() => api(`/sources/${s.id}`, { method: 'DELETE' }))}>delete</button>}
      </div>
      <ErrorNote>{err}</ErrorNote>
    </div>
  )
}

const EMPTY_CUSTOM = {
  name: '', description: '', tier: 'secondary', weight: 1.0, free_tier: '',
  auth_type: 'none', auth_field: '', api_key: '', url: '',
  items: '', title: '', url_path: '', snippet: '', date: '',
}

export default function SettingsSources() {
  const { data: sources } = usePoll('/sources', 4000)
  const [test, setTest] = useState(null)     // {source, query, result, busy}
  const [form, setForm] = useState(EMPTY_CUSTOM)
  const { busy, err, wrap } = useAsync()

  const runTest = (source, query) => {
    setTest({ source, query, busy: true })
    api(`/sources/${source.id}/test`, { method: 'POST', body: { query } })
      .then(r => setTest({ source, query, result: r }))
      .catch(e => setTest({ source, query, result: { error: e.message, parsed: [] } }))
  }

  const addCustom = () => wrap(async () => {
    await api('/sources', {
      method: 'POST', body: {
        name: form.name, description: form.description, tier: form.tier,
        weight: Number(form.weight), free_tier: form.free_tier,
        api_key: form.api_key || undefined,
        auth: form.auth_type === 'none' ? { type: 'none' }
          : { type: form.auth_type, [form.auth_type === 'query-param' ? 'param' : 'header']: form.auth_field },
        request: { url: form.url },
        mapping: { items: form.items, title: form.title, url: form.url_path, snippet: form.snippet, date: form.date },
      }
    })
    setForm(EMPTY_CUSTOM)
  })

  return (
    <div className="grid lg:grid-cols-[1fr_420px] gap-4">
      <div className="space-y-2">
        <Card title="Registered sources — the Research Agent uses every enabled source">
          <div className="space-y-2">
            {(sources || []).map(s => <SourceRow key={s.id} s={s} onTest={(src) => runTest(src, 'workflow automation tools')} />)}
          </div>
        </Card>
      </div>

      <Card title="Add custom source — any HTTP API">
        <Field label="Name"><Input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="My internal search" /></Field>
        <Field label="Request URL" hint="must contain {query}; {n} = result count">
          <Input value={form.url} onChange={e => setForm({ ...form, url: e.target.value })}
            placeholder="https://api.example.com/search?q={query}&limit={n}" /></Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Auth type">
            <Select value={form.auth_type} onChange={e => setForm({ ...form, auth_type: e.target.value })}>
              <option value="none">none</option><option value="api-key-header">API-key header</option>
              <option value="bearer">Bearer token</option><option value="query-param">query param</option>
            </Select></Field>
          {form.auth_type !== 'none' && form.auth_type !== 'bearer' &&
            <Field label={form.auth_type === 'query-param' ? 'Param name' : 'Header name'}>
              <Input value={form.auth_field} onChange={e => setForm({ ...form, auth_field: e.target.value })}
                placeholder={form.auth_type === 'query-param' ? 'api_key' : 'X-Api-Key'} /></Field>}
        </div>
        {form.auth_type !== 'none' &&
          <Field label="API key" hint="stored in the vault, masked like provider keys">
            <Input type="password" value={form.api_key} onChange={e => setForm({ ...form, api_key: e.target.value })} /></Field>}
        <p className="text-[11px] uppercase tracking-widest text-zinc-500 mt-4 mb-1">Response mapping (dot paths)</p>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Results array"><Input value={form.items} onChange={e => setForm({ ...form, items: e.target.value })} placeholder="data.results" /></Field>
          <Field label="Title"><Input value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="name" /></Field>
          <Field label="URL"><Input value={form.url_path} onChange={e => setForm({ ...form, url_path: e.target.value })} placeholder="link" /></Field>
          <Field label="Snippet"><Input value={form.snippet} onChange={e => setForm({ ...form, snippet: e.target.value })} placeholder="description" /></Field>
          <Field label="Date (optional)"><Input value={form.date} onChange={e => setForm({ ...form, date: e.target.value })} placeholder="published_at" /></Field>
          <Field label="Tier">
            <Select value={form.tier} onChange={e => setForm({ ...form, tier: e.target.value })}>
              {TIERS.map(t => <option key={t}>{t}</option>)}</Select></Field>
        </div>
        <Field label="Free tier note"><Input value={form.free_tier} onChange={e => setForm({ ...form, free_tier: e.target.value })} placeholder="1000 req/day" /></Field>
        <Btn variant="primary" className="mt-4" disabled={busy || !form.name || !form.url} onClick={addCustom}>Register source</Btn>
        <p className="text-[11px] text-zinc-600 mt-2">Then press <b>Test source</b> to see the raw response next to the
          parsed result and fix the mapping before a real run.</p>
        <ErrorNote>{err}</ErrorNote>
      </Card>

      {test && (
        <Modal title={`Test source — ${test.source.name}`} onClose={() => setTest(null)} wide>
          <div className="flex gap-2 mb-3">
            <Input defaultValue={test.query} id="tq" placeholder="sample query" />
            <Btn variant="primary" onClick={() => runTest(test.source, document.getElementById('tq').value)}>Run</Btn>
          </div>
          {test.busy ? <p className="text-zinc-400">querying…</p> : <>
            {test.result?.error && <p className="text-red-400 text-sm mb-2">{test.result.error}</p>}
            <div className="grid md:grid-cols-2 gap-3">
              <div><p className="text-[11px] uppercase text-zinc-500 mb-1">Parsed result (what the agent sees)</p>
                <Json value={test.result?.parsed} className="max-h-96 overflow-y-auto" /></div>
              <div><p className="text-[11px] uppercase text-zinc-500 mb-1">Raw response</p>
                <Json value={test.result?.raw || '(builtin ddg — no raw JSON)'} className="max-h-96 overflow-y-auto" /></div>
            </div>
          </>}
        </Modal>
      )}
    </div>
  )
}
