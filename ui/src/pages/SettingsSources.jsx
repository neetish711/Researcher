import React, { useState } from 'react'
import { api, usePoll } from '../api.js'
import { Btn, Card, ErrorNote, Field, Input, Json, Modal, Pill, Select, useAsync } from '../lib.jsx'

const TIERS = ['primary', 'secondary', 'vendor', 'community']
const STATUS_UI = {
  connected: ['✅ connected', 'text-emerald-400'],
  quota_low: ['⚠️ quota low', 'text-amber-400'],
  exhausted: ['⛔ quota exhausted', 'text-red-400'],
  invalid: ['❌ invalid key', 'text-red-400'],
  no_key: ['○ no key', 'text-zinc-500'],
}

function QuotaMeter({ q }) {
  if (!q || q.monthly_quota == null) return <p className="text-[11px] text-zinc-600">no enforceable cap (keyless)</p>
  const pct = Math.min(100, 100 * q.used / q.monthly_quota)
  return (
    <div className="mt-1">
      <div className="h-1.5 bg-zinc-800 rounded overflow-hidden">
        <div className={`h-full ${pct > 85 ? 'bg-red-500' : pct > 60 ? 'bg-amber-500' : 'bg-emerald-600'}`}
             style={{ width: `${pct}%` }} />
      </div>
      <p className="text-[11px] text-zinc-500 mt-0.5">
        {q.used.toLocaleString()} / {q.monthly_quota.toLocaleString()} {q.unit}s used ·
        {' '}{(q.remaining ?? 0).toLocaleString()} left · resets {q.resets_on}
        {q.assumed && <span className="text-amber-500"> · assumed cap (unverified)</span>}
      </p>
    </div>
  )
}

function ProviderCard({ p, onTest }) {
  const [key, setKey] = useState('')
  const { busy, err, wrap } = useAsync()
  const [status, cls] = STATUS_UI[p.status] || STATUS_UI.no_key
  const saveKey = () => wrap(async () => {
    await api(`/research-sources/${p.id}/key`, { method: 'POST', body: { api_key: key } })
    setKey('')
  })
  return (
    <div className="border border-zinc-800 rounded-lg p-3.5 bg-zinc-950/40">
      <div className="flex items-center gap-3 flex-wrap">
        <b className="text-zinc-100">{p.name}</b>
        <span className={`text-xs font-semibold ${cls}`}>{status}</span>
        <Pill kind="pending">{p.reliability}</Pill>
        {p.allowed_endpoints && <span className="text-[10px] text-red-400 border border-red-900 rounded px-1.5 py-0.5">
          only: {p.allowed_endpoints.join(',')} — /research BLOCKED</span>}
        <a href={p.pricing_url} target="_blank" rel="noreferrer"
           className="ml-auto text-[11px] text-sky-500 hover:underline">verify limits on pricing page ↗</a>
      </div>
      <p className="text-xs text-zinc-500 mt-1">{p.role}{p.use_when && <span className="text-zinc-600"> · trigger: {p.use_when}</span>}</p>
      <QuotaMeter q={p.quota} />
      <div className="flex items-center gap-2 mt-2 flex-wrap text-xs">
        <span className="text-zinc-500">key {p.key_fingerprint
          ? <span className="font-mono text-zinc-300">{p.key_fingerprint}</span>
          : <span className="text-zinc-600">({p.key_env}{p.keyless_ok ? ' — optional' : ''})</span>}</span>
        <Input className="!w-52 !py-1" type="password" autoComplete="new-password"
               placeholder="paste key (stored server-side)" value={key} onChange={e => setKey(e.target.value)} />
        <Btn className="!py-1 !px-2 text-xs" disabled={busy || !key} onClick={saveKey}>Save key</Btn>
        <Btn className="!py-1 !px-2 text-xs" onClick={() => onTest(p)}>Test connection</Btn>
        <span className="text-[11px] text-zinc-600">rate: {p.rate_limit?.rps} rps / {p.rate_limit?.rpm} rpm</span>
      </div>
      {p.quota?.quota_verified && <p className="text-[11px] text-emerald-500 mt-1">✓ verified live: {p.quota.verified_note}</p>}
      <ErrorNote>{err}</ErrorNote>
    </div>
  )
}

export default function SettingsSources() {
  const { data, error } = usePoll('/research-sources', 5000)
  const { data: custom } = usePoll('/sources', 10000)
  const [test, setTest] = useState(null)
  const [customTest, setCustomTest] = useState(null)
  const [form, setForm] = useState(null)
  const { busy, err, wrap } = useAsync()

  const toggleFreeTier = () => wrap(async () => {
    const next = !data.free_tier_only
    if (!next && !confirm('Disabling this permits billable calls. Brave-style overage billing has no spend cap.\n\nDisable free_tier_only?')) return
    await api('/research-sources/config', { method: 'PATCH', body: { free_tier_only: next } })
  })

  const runTest = (p) => {
    setTest({ p, busy: true })
    api(`/research-sources/${p.id}/test`, { method: 'POST' })
      .then(r => setTest({ p, result: r }))
      .catch(e => setTest({ p, result: { ok: false, detail: e.message } }))
  }
  const runCustomTest = (s, query) => {
    setCustomTest({ s, busy: true })
    api(`/sources/${s.id}/test`, { method: 'POST', body: { query } })
      .then(r => setCustomTest({ s, result: r }))
      .catch(e => setCustomTest({ s, result: { error: e.message, parsed: [] } }))
  }

  if (error) return <Card title="Research sources"><p className="text-red-400">{error}</p></Card>
  if (!data) return <p className="text-zinc-500 p-8">loading…</p>

  return (
    <div className="space-y-4">
      <div className={`rounded-lg border p-4 flex items-center gap-4 flex-wrap
        ${data.free_tier_only ? 'border-emerald-800 bg-emerald-950/20' : 'border-red-700 bg-red-950/30'}`}>
        <button onClick={toggleFreeTier}
          className={`w-11 h-6 rounded-full relative transition ${data.free_tier_only ? 'bg-emerald-600' : 'bg-red-600'}`}>
          <span className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-all ${data.free_tier_only ? 'left-5' : 'left-0.5'}`} />
        </button>
        <div>
          <p className="font-semibold text-zinc-100">free_tier_only: {String(data.free_tier_only)}</p>
          <p className="text-xs text-zinc-400">
            {data.free_tier_only
              ? 'Hard block ON — a call that would exceed any free tier is refused pre-flight, never attempted.'
              : '⚠ Billable calls are PERMITTED. Brave-style overage billing has no spend cap.'}
          </p>
        </div>
        <ErrorNote>{err}</ErrorNote>
      </div>

      <Card title="Keyed providers — enter keys once; the UI only ever sees fingerprints">
        <div className="grid xl:grid-cols-2 gap-3">
          {data.providers.map(p => <ProviderCard key={p.id} p={p} onTest={runTest} />)}
        </div>
      </Card>

      <Card title="Keyless primaries — always on, zero quota risk, your best citations">
        <div className="flex flex-wrap gap-2">
          {data.keyless.map(k => (
            <span key={k.id} className="px-2.5 py-1 rounded border border-zinc-700 text-xs text-zinc-300">
              {k.name} <span className="text-zinc-600">· {k.reliability}{k.mailto ? ' · polite pool' : ''}</span>
            </span>))}
        </div>
        <p className="text-[11px] text-zinc-600 mt-2">Weights: {Object.entries(data.weights).map(([k, v]) => `${k} ${v}`).join(' · ')}.
          Community sources (HN/Reddit) are weighted 0.4 and can never be the sole basis for a recommendation.</p>
      </Card>

      <Card title="Custom sources — register any HTTP API" right={
        <Btn className="!py-1 text-xs" onClick={() => setForm({ name: '', url: '', auth_type: 'none', auth_field: '', api_key: '', items: '', title: '', url_path: '', snippet: '', date: '', tier: 'secondary', free_tier: '' })}>+ add custom source</Btn>}>
        {(custom || []).filter(s => !s.builtin).length === 0 && <p className="text-zinc-600 text-sm">none registered</p>}
        <div className="space-y-1.5">
          {(custom || []).filter(s => !s.builtin).map(s => (
            <div key={s.id} className="flex items-center gap-3 text-sm px-3 py-2 rounded border border-zinc-800">
              <b className="text-zinc-200">{s.name}</b>
              <Pill kind={s.enabled ? 'ok' : 'pending'}>{s.enabled ? 'on' : 'off'}</Pill>
              <span className="text-xs text-zinc-500">{s.tier} · weight {s.weight}</span>
              {s.key_fingerprint && <span className="font-mono text-xs text-zinc-400">{s.key_fingerprint}</span>}
              <Btn className="!py-0.5 !px-2 text-xs ml-auto" onClick={() => runCustomTest(s, 'workflow automation tools')}>Test source</Btn>
              <button className="text-zinc-600 hover:text-red-400 text-xs"
                onClick={() => api(`/sources/${s.id}`, { method: 'DELETE' })}>delete</button>
            </div>))}
        </div>
      </Card>

      {test && (
        <Modal title={`Test connection — ${test.p.name}`} onClose={() => setTest(null)}>
          {test.busy ? <p className="text-zinc-400">one cheap live call…</p> : <>
            <p className={test.result.ok ? 'text-emerald-400' : 'text-red-400'}>
              {test.result.ok ? '✅' : '❌'} {test.result.detail}</p>
            {test.result.quota && <div className="mt-3"><QuotaMeter q={test.result.quota} /></div>}
            {test.result.note && <p className="text-xs text-amber-400 mt-3">{test.result.note}</p>}
            {test.result.pricing_url && <a className="text-xs text-sky-500 hover:underline" target="_blank"
              rel="noreferrer" href={test.result.pricing_url}>open the live pricing page ↗</a>}
          </>}
        </Modal>
      )}

      {customTest && (
        <Modal title={`Test source — ${customTest.s.name}`} onClose={() => setCustomTest(null)} wide>
          <div className="flex gap-2 mb-3">
            <Input defaultValue="workflow automation tools" id="ctq" />
            <Btn variant="primary" onClick={() => runCustomTest(customTest.s, document.getElementById('ctq').value)}>Run</Btn>
          </div>
          {customTest.busy ? <p className="text-zinc-400">querying…</p> : <>
            {customTest.result?.error && <p className="text-red-400 text-sm mb-2">{customTest.result.error}</p>}
            <div className="grid md:grid-cols-2 gap-3">
              <div><p className="text-[11px] uppercase text-zinc-500 mb-1">Parsed (what the agent sees)</p>
                <Json value={customTest.result?.parsed} className="max-h-96 overflow-y-auto" /></div>
              <div><p className="text-[11px] uppercase text-zinc-500 mb-1">Raw response</p>
                <Json value={customTest.result?.raw} className="max-h-96 overflow-y-auto" /></div>
            </div></>}
        </Modal>
      )}

      {form && (
        <Modal title="Register custom source" onClose={() => setForm(null)}>
          <Field label="Name"><Input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="Request URL" hint="{query} required; {n} = result count">
            <Input value={form.url} onChange={e => setForm({ ...form, url: e.target.value })}
                   placeholder="https://api.example.com/search?q={query}&limit={n}" /></Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Auth"><Select value={form.auth_type} onChange={e => setForm({ ...form, auth_type: e.target.value })}>
              <option value="none">none</option><option value="api-key-header">API-key header</option>
              <option value="bearer">Bearer</option><option value="query-param">query param</option></Select></Field>
            {form.auth_type !== 'none' && form.auth_type !== 'bearer' &&
              <Field label={form.auth_type === 'query-param' ? 'Param' : 'Header'}>
                <Input value={form.auth_field} onChange={e => setForm({ ...form, auth_field: e.target.value })} /></Field>}
          </div>
          {form.auth_type !== 'none' && <Field label="API key" hint="vaulted + masked">
            <Input type="password" value={form.api_key} onChange={e => setForm({ ...form, api_key: e.target.value })} /></Field>}
          <div className="grid grid-cols-2 gap-3">
            <Field label="Results array path"><Input value={form.items} onChange={e => setForm({ ...form, items: e.target.value })} placeholder="data.results" /></Field>
            <Field label="Title path"><Input value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="name" /></Field>
            <Field label="URL path"><Input value={form.url_path} onChange={e => setForm({ ...form, url_path: e.target.value })} placeholder="link" /></Field>
            <Field label="Snippet path"><Input value={form.snippet} onChange={e => setForm({ ...form, snippet: e.target.value })} placeholder="desc" /></Field>
            <Field label="Date path"><Input value={form.date} onChange={e => setForm({ ...form, date: e.target.value })} /></Field>
            <Field label="Tier"><Select value={form.tier} onChange={e => setForm({ ...form, tier: e.target.value })}>
              {TIERS.map(t => <option key={t}>{t}</option>)}</Select></Field>
          </div>
          <Field label="Free-tier note"><Input value={form.free_tier} onChange={e => setForm({ ...form, free_tier: e.target.value })} /></Field>
          <Btn variant="primary" className="mt-4" disabled={busy || !form.name || !form.url}
            onClick={() => wrap(async () => {
              await api('/sources', { method: 'POST', body: {
                name: form.name, tier: form.tier, free_tier: form.free_tier,
                api_key: form.api_key || undefined,
                auth: form.auth_type === 'none' ? { type: 'none' }
                  : { type: form.auth_type, [form.auth_type === 'query-param' ? 'param' : 'header']: form.auth_field },
                request: { url: form.url },
                mapping: { items: form.items, title: form.title, url: form.url_path, snippet: form.snippet, date: form.date },
              } })
              setForm(null)
            })}>Register</Btn>
          <ErrorNote>{err}</ErrorNote>
        </Modal>
      )}
    </div>
  )
}
