import React, { useState } from 'react'

export const STATUS_COLOR = {
  done: 'bg-emerald-600', ok: 'bg-emerald-600', complete: 'bg-emerald-600',
  running: 'bg-sky-600 animate-pulse', waiting: 'bg-amber-500', retrying: 'bg-amber-500',
  error: 'bg-red-600', paused: 'bg-red-500', rejected: 'bg-red-700', pending: 'bg-zinc-600',
}

export function Pill({ kind, children }) {
  return <span className={`inline-block px-2 py-0.5 rounded text-[11px] font-semibold text-white ${STATUS_COLOR[kind] || 'bg-zinc-600'}`}>{children ?? kind}</span>
}

export function statusKind(status = '') {
  if (status.startsWith('running')) return 'running'
  if (status.startsWith('awaiting')) return 'waiting'
  if (status.startsWith('error')) return 'error'
  if (status.startsWith('rejected')) return 'rejected'
  if (status === 'paused_budget') return 'paused'
  if (status === 'complete') return 'complete'
  return 'pending'
}

export function Card({ title, right, children, className = '' }) {
  return (
    <section className={`bg-zinc-900 border border-zinc-800 rounded-lg ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between px-4 py-2 border-b border-zinc-800">
          <h3 className="text-[11px] uppercase tracking-widest text-zinc-500 font-semibold">{title}</h3>
          <div>{right}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Btn({ children, variant = 'default', className = '', ...props }) {
  const styles = {
    default: 'bg-zinc-800 hover:bg-zinc-700 border-zinc-700 text-zinc-200',
    primary: 'bg-sky-700 hover:bg-sky-600 border-sky-600 text-white',
    approve: 'bg-emerald-700 hover:bg-emerald-600 border-emerald-600 text-white',
    danger: 'bg-red-800 hover:bg-red-700 border-red-700 text-white',
    ghost: 'bg-transparent hover:bg-zinc-800 border-transparent text-zinc-400',
  }
  return (
    <button className={`px-3 py-1.5 rounded border text-sm disabled:opacity-40 disabled:cursor-not-allowed ${styles[variant]} ${className}`} {...props}>
      {children}
    </button>
  )
}

export function Input(props) {
  return <input {...props} className={`bg-zinc-950 border border-zinc-700 rounded px-2.5 py-1.5 text-sm w-full
    focus:outline-none focus:border-sky-600 placeholder-zinc-600 ${props.className || ''}`} />
}

export function Select({ children, ...props }) {
  return <select {...props} className={`bg-zinc-950 border border-zinc-700 rounded px-2 py-1.5 text-sm w-full
    focus:outline-none focus:border-sky-600 ${props.className || ''}`}>{children}</select>
}

export function Label({ children, hint }) {
  return (
    <label className="block text-xs text-zinc-500 mb-1 mt-3 first:mt-0">
      {children}{hint && <span className="text-zinc-600 ml-2">{hint}</span>}
    </label>
  )
}

export function Field({ label, hint, children }) {
  return <div><Label hint={hint}>{label}</Label>{children}</div>
}

export function Table({ headers, rows, empty = 'none' }) {
  if (!rows?.length) return <p className="text-zinc-600 text-sm">{empty}</p>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm border-collapse">
        <thead><tr>{headers.map((h, i) =>
          <th key={i} className="text-left text-[11px] uppercase tracking-wider text-zinc-500 border-b border-zinc-800 px-2 py-1.5">{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) =>
          <tr key={i} className="border-b border-zinc-800/60 hover:bg-zinc-800/30 align-top">
            {r.map((c, j) => <td key={j} className="px-2 py-1.5">{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  )
}

export function Modal({ title, onClose, children, wide }) {
  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex items-start justify-center p-6 overflow-y-auto" onClick={onClose}>
      <div className={`bg-zinc-900 border border-zinc-700 rounded-lg w-full ${wide ? 'max-w-5xl' : 'max-w-2xl'} mt-8`}
           onClick={e => e.stopPropagation()}>
        <header className="flex justify-between items-center px-5 py-3 border-b border-zinc-800">
          <h3 className="font-semibold text-zinc-200">{title}</h3>
          <Btn variant="ghost" onClick={onClose} aria-label="close">✕</Btn>
        </header>
        <div className="p-5">{children}</div>
      </div>
    </div>
  )
}

export function Json({ value, className = '' }) {
  return <pre className={`bg-zinc-950 border border-zinc-800 rounded p-3 text-xs overflow-x-auto
    whitespace-pre-wrap break-words text-zinc-400 ${className}`}>{
    typeof value === 'string' ? value : JSON.stringify(value, null, 2)}</pre>
}

export function ErrorNote({ children }) {
  if (!children) return null
  return <p className="text-red-400 text-sm mt-2 break-words">{String(children)}</p>
}

export function useAsync() {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const wrap = async (fn) => {
    setBusy(true); setErr(null)
    try { return await fn() } catch (e) { setErr(e.message); throw e } finally { setBusy(false) }
  }
  return { busy, err, setErr, wrap }
}
