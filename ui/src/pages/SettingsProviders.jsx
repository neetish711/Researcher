import React, { useState } from 'react'
import { api, usePoll } from '../api.js'
import { Btn, Card, ErrorNote, Field, Input, Select, Table, useAsync } from '../lib.jsx'

export default function SettingsProviders() {
  const { data: providers } = usePoll('/providers', 5000)
  const [form, setForm] = useState({ name: '', type: 'anthropic', base_url: '', api_key: '' })
  const [tests, setTests] = useState({})   // name -> {ok, detail, model_count, busy}
  const { busy, err, wrap } = useAsync()

  const save = () => wrap(async () => {
    await api('/providers', { method: 'POST', body: { ...form, api_key: form.api_key || null } })
    setForm({ name: '', type: 'anthropic', base_url: '', api_key: '' })
  })
  const test = (name) => {
    setTests(t => ({ ...t, [name]: { busy: true } }))
    api(`/providers/${name}/test`, { method: 'POST' })
      .then(r => setTests(t => ({ ...t, [name]: r })))
      .catch(e => setTests(t => ({ ...t, [name]: { ok: false, detail: e.message } })))
  }
  const del = (name) => wrap(() => api(`/providers/${name}`, { method: 'DELETE' }))

  const vault = (providers || []).filter(p => !p.type.startsWith('env'))
  const env = (providers || []).filter(p => p.type.startsWith('env'))

  return (
    <div className="grid lg:grid-cols-[420px_1fr] gap-4">
      <Card title="Add / update provider connection">
        <p className="text-xs text-zinc-500 mb-3">Keys are stored <b className="text-zinc-300">server-side only</b>, encrypted at
          rest. The UI only ever sees a fingerprint. Model dropdowns everywhere are populated from each key's
          list-models endpoint.</p>
        <Field label="Display name"><Input value={form.name} placeholder="my-anthropic"
          onChange={e => setForm({ ...form, name: e.target.value })} /></Field>
        <Field label="Provider type">
          <Select value={form.type} onChange={e => setForm({ ...form, type: e.target.value })}>
            <option value="anthropic">Anthropic</option>
            <option value="openai">OpenAI</option>
            <option value="openai-compatible">Any OpenAI-compatible endpoint</option>
          </Select></Field>
        {form.type === 'openai-compatible' && (
          <Field label="Base URL"><Input value={form.base_url} placeholder="https://openrouter.ai/api/v1"
            onChange={e => setForm({ ...form, base_url: e.target.value })} /></Field>)}
        <Field label="API key" hint="masked; never echoed back after save — leave blank on update to keep">
          <Input type="password" autoComplete="new-password" value={form.api_key} placeholder="••••••••"
            onChange={e => setForm({ ...form, api_key: e.target.value })} /></Field>
        <Btn variant="primary" className="mt-4" disabled={busy || !form.name} onClick={save}>Save provider</Btn>
        <ErrorNote>{err}</ErrorNote>
      </Card>

      <div className="space-y-4">
        <Card title="Provider connections (vault)">
          <Table headers={['name', 'type', 'base url', 'key', 'connection', '']}
            empty="none yet — add your first key on the left"
            rows={vault.map(p => {
              const t = tests[p.name]
              return [
                <b className="text-zinc-200">{p.name}</b>, p.type,
                <span className="text-zinc-500 text-xs">{p.base_url}</span>,
                <span className="font-mono text-zinc-400">{p.key_fingerprint}</span>,
                <span className="text-xs">
                  <Btn className="!py-0.5 !px-2 text-xs mr-2" disabled={t?.busy} onClick={() => test(p.name)}>
                    {t?.busy ? 'testing…' : 'Test connection'}</Btn>
                  {t && !t.busy && (t.ok
                    ? <span className="text-emerald-400">✅ {t.detail}</span>
                    : <span className="text-red-400">❌ {t.detail?.slice(0, 120)}</span>)}
                </span>,
                <button className="text-zinc-600 hover:text-red-400" onClick={() => del(p.name)}>delete</button>,
              ]
            })} />
        </Card>
        <Card title="Environment providers (config/llm.yaml — read-only here)">
          <Table headers={['name', 'base url', 'key source']} empty="none"
            rows={env.map(p => [p.name, <span className="text-zinc-500 text-xs">{p.base_url}</span>,
              <span className="font-mono text-zinc-500 text-xs">{p.key_fingerprint}</span>])} />
          <p className="text-[11px] text-zinc-600 mt-2">These read their key from an environment variable on the
            server. A vault connection with the same name takes precedence.</p>
        </Card>
      </div>
    </div>
  )
}
