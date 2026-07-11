import { useEffect, useRef, useState } from 'react'

export async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : { 'content-type': 'application/json' },
    ...opts,
    body: opts.body instanceof FormData ? opts.body
        : opts.body ? JSON.stringify(opts.body) : undefined,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const j = await res.json()
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch { /* keep statusText */ }
    throw new Error(detail)
  }
  return res.json()
}

/** Poll a GET endpoint; pause when the tab is hidden. */
export function usePoll(path, ms = 3000, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    let timer
    const tick = async () => {
      if (document.visibilityState === 'visible' && path) {
        try { const d = await api(path); if (alive.current) { setData(d); setError(null) } }
        catch (e) { if (alive.current) setError(e.message) }
      }
      timer = setTimeout(tick, ms)
    }
    tick()
    return () => { alive.current = false; clearTimeout(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ms, ...deps])
  return { data, error }
}

/** Live event feed: SSE with automatic fallback to since-polling.
    Survives refresh — always replays from seq 0 on mount. */
export function useEvents(runId) {
  const [events, setEvents] = useState([])
  const seq = useRef(0)
  useEffect(() => {
    if (!runId) return
    seq.current = 0
    setEvents([])
    let stop = false, es = null, timer = null

    const append = (batch) => {
      if (!batch.length) return
      seq.current = Math.max(seq.current, ...batch.map(e => e.seq || 0))
      setEvents(prev => [...prev, ...batch])
    }
    const poll = async () => {
      while (!stop) {
        try {
          const d = await api(`/runs/${runId}/events?since=${seq.current}`)
          append(d.events)
        } catch { /* transient */ }
        await new Promise(r => { timer = setTimeout(r, 1500) })
      }
    }
    // try SSE first; on error fall back to polling (serverless-safe)
    try {
      es = new EventSource(`/runs/${runId}/events/stream?since=0`)
      es.onmessage = (m) => { try { append([JSON.parse(m.data)]) } catch { /* skip */ } }
      es.onerror = () => { es.close(); es = null; poll() }
    } catch { poll() }

    return () => { stop = true; if (es) es.close(); clearTimeout(timer) }
  }, [runId])
  return events
}

export const KEY_RE = /^(sk-|sk_|key-|api[-_]?key|ghp_|xoxb-)/i
export const KEY_MSG = 'That looks like an API key, not a model id — add keys under Settings → Providers.'
export const fmtUsd = (v) => `$${Number(v || 0).toFixed(2)}`
export const fmtDur = (s) => {
  s = Math.max(0, Math.round(s || 0))
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
  return h ? `${h}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`
}
export const ago = (iso) => {
  if (!iso) return ''
  const d = (Date.now() - new Date(iso).getTime()) / 1000
  return d < 90 ? `${Math.round(d)}s ago` : d < 5400 ? `${Math.round(d / 60)}m ago` : `${Math.round(d / 3600)}h ago`
}
