'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import {
  Activity, Database, Server, HardDrive, ShieldCheck, ShieldAlert,
  Shield, AlertTriangle, RefreshCw, Clock, CheckCircle2, XCircle,
  MinusCircle, Loader2, ArrowLeft, Eye,
} from 'lucide-react'
import { isLoggedIn } from '@/lib/auth'
import api, { isTransientError, retryOnTransient } from '@/lib/api'

interface HealthPayload {
  generated_at: number
  uptime_seconds: number
  checks: Record<string, { status: string; detail: string }>
  waf: { status: string; detail: string; headers?: Record<string, string> }
  posture: { check: string; status: string; detail: string }[]
  events: { ts: number; type: string; detail: string; ip: string }[]
}

type Status = 'pass' | 'warn' | 'ok' | 'absent' | 'error' | 'unknown'

const statusColor: Record<Status, { text: string; bg: string; border: string }> = {
  pass: { text: '#4ade80', bg: 'rgba(74,222,128,0.08)', border: 'rgba(74,222,128,0.25)' },
  ok: { text: '#4ade80', bg: 'rgba(74,222,128,0.08)', border: 'rgba(74,222,128,0.25)' },
  warn: { text: '#fbbf24', bg: 'rgba(251,191,36,0.08)', border: 'rgba(251,191,36,0.25)' },
  absent: { text: '#94a3b8', bg: 'rgba(148,163,184,0.08)', border: 'rgba(148,163,184,0.25)' },
  error: { text: '#ff6b6b', bg: 'rgba(255,107,107,0.08)', border: 'rgba(255,107,107,0.25)' },
  unknown: { text: '#94a3b8', bg: 'rgba(148,163,184,0.08)', border: 'rgba(148,163,184,0.25)' },
}

const statusLabel: Record<Status, string> = {
  pass: 'PASS', ok: 'OK', warn: 'WARN', absent: 'ABSENT', error: 'ERROR', unknown: 'UNKNOWN',
}

const eventLabels: Record<string, string> = {
  login_failure: 'Login failure',
  mfa_failure: 'MFA failure',
  mfa_enabled: 'MFA enabled',
  mfa_disabled: 'MFA disabled',
  password_reset: 'Password reset',
  register_throttled: 'Register throttled',
  admin_toggle: 'Admin toggled',
  admin_plan_change: 'Plan changed',
  admin_delete: 'User deleted',
  admin_verify: 'User verified',
}

const eventColor: Record<string, string> = {
  login_failure: '#ff6b6b',
  mfa_failure: '#ff6b6b',
  register_throttled: '#fbbf24',
  admin_delete: '#ff6b6b',
  admin_toggle: '#a78bfa',
  admin_plan_change: '#a78bfa',
  admin_verify: '#60a5fa',
  password_reset: '#60a5fa',
  mfa_enabled: '#4ade80',
  mfa_disabled: '#4ade80',
}

export default function AdminHealthPage() {
  const router = useRouter()
  const [data, setData] = useState<HealthPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)

  const fetchHealth = useCallback(async (silent = false) => {
    if (!silent) setRefreshing(true)
    try {
      // Retry transient failures (Render cold start: 502/503/504 or timeout) —
      // the silent auto-refresh already re-tries every 60s, but the initial
      // load shouldn't depend on that interval.
      const d = await retryOnTransient(() => api.getAdminHealth())
      // Defensive shape check: the same-origin proxy could return an HTML page
      // (e.g. a route shadow like /admin/health) or a proxy error body — never
      // feed a non-payload into the renderer (Object.values(html) would crash).
      if (!d || typeof d !== 'object' || !d.checks || typeof d.checks !== 'object') {
        throw new Error('Malformed health payload')
      }
      setData(d)
      setLastRefresh(new Date())
      setError('')
    } catch (err: any) {
      // Only a real 403 means "not an admin" — a cold-start 502 previously
      // rendered the same 'You need admin privileges…' denial on an admin
      // account. Transient failures get their own honest message.
      if (err?.response?.status === 403) setError('Admin access required')
      else if (isTransientError(err)) setError('Still connecting to the server — please try again in a moment.')
      else setError('Failed to load health data')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    if (!isLoggedIn()) { router.push('/'); return }
    // A transient getMe() failure (cold start) must NOT bounce an admin home.
    // ~1min of retries covers a full Docker/Render boot before giving up.
    retryOnTransient(() => api.getMe(), 5, 8000)
      .then((me) => { if (!me?.is_admin) { router.push('/'); return } fetchHealth() })
      .catch(() => { router.push('/') })
  }, [router, fetchHealth])

  // Auto-refresh every 60s while the page is open.
  useEffect(() => {
    if (!data) return
    const t = setInterval(() => fetchHealth(true), 60000)
    return () => clearInterval(t)
  }, [data, fetchHealth])

  if (loading) return (
    <div className="flex min-h-[70vh] items-center justify-center select-none">
      <div className="flex flex-col items-center gap-4">
        <Loader2 size={24} className="animate-spin" style={{ color: 'var(--text-muted)' }} />
        <p className="text-xs tracking-widest uppercase" style={{ color: 'var(--text-faint)' }}>Checking systems</p>
      </div>
    </div>
  )

  if (error && !data) return (
    <div className="mx-auto max-w-4xl px-6 py-24 text-center select-none">
      <div className="inline-flex items-center justify-center w-12 h-12 rounded-full mb-4" style={{ backgroundColor: 'rgba(255,107,107,0.1)' }}>
        <ShieldAlert size={20} style={{ color: '#ff6b6b' }} />
      </div>
      <p className="text-sm mb-1" style={{ color: '#ff6b6b' }}>{error}</p>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>You need admin privileges to access this page.</p>
    </div>
  )

  if (!data) return null

  const infraItems: { key: string; label: string; icon: any }[] = [
    { key: 'database', label: 'Database', icon: Database },
    { key: 'redis', label: 'Redis', icon: Server },
    { key: 'storage', label: 'Object storage', icon: HardDrive },
  ]

  const warnCount = [
    ...Object.values(data.checks).map((c) => c.status),
    data.waf.status,
    ...data.posture.map((p) => p.status),
  ].filter((s) => s === 'warn' || s === 'error' || s === 'unknown').length

  const fmtTime = (epoch: number) =>
    new Date(epoch * 1000).toLocaleTimeString('en-US', { hour12: false })

  const fmtAgo = (epoch: number) => {
    const s = Math.floor(Date.now() / 1000 - epoch)
    if (s < 60) return `${s}s ago`
    if (s < 3600) return `${Math.floor(s / 60)}m ago`
    return `${Math.floor(s / 3600)}h ago`
  }

  return (
    <div className="mx-auto max-w-7xl px-4 sm:px-6 py-8 sm:py-12 select-none">
      {/* Header */}
      <div className="mb-8">
        <div className="flex items-center gap-3 mb-1">
          <button onClick={() => router.push('/admin')} className="p-1.5 border transition-colors hover:border-white/20" style={{ borderColor: 'var(--border)' }}>
            <ArrowLeft size={14} style={{ color: 'var(--text-muted)' }} />
          </button>
          <div className="flex items-center justify-center w-8 h-8 rounded-lg" style={{ backgroundColor: 'rgba(74,222,128,0.1)' }}>
            <Activity size={16} style={{ color: '#4ade80' }} />
          </div>
          <h1 className="text-lg sm:text-xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">Security & Health</h1>
          <span className="ml-1 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest rounded"
            style={{ backgroundColor: 'rgba(74,222,128,0.12)', color: '#4ade80' }}>
            {warnCount === 0 ? 'All systems nominal' : `${warnCount} attention${warnCount === 1 ? '' : 's'}`}
          </span>
        </div>
        <div className="flex items-center gap-4 mt-2">
          <p className="text-xs flex items-center gap-1.5" style={{ color: 'var(--text-muted)' }}>
            <Clock size={11} /> Uptime {Math.floor(data.uptime_seconds / 86400)}d {(Math.floor(data.uptime_seconds / 3600)) % 24}h
          </p>
          {lastRefresh && (
            <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
              Last check {lastRefresh.toLocaleTimeString('en-US', { hour12: false })}
            </p>
          )}
          <button
            onClick={() => fetchHealth()}
            disabled={refreshing}
            className="flex items-center gap-1.5 border px-3 py-1.5 text-[10px] uppercase tracking-widest transition-colors hover:border-white/20 disabled:opacity-40 ml-auto"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)', fontFamily: 'monospace' }}
          >
            <RefreshCw size={11} className={refreshing ? 'animate-spin' : ''} /> Re-check
          </button>
        </div>
      </div>

      {/* Live infra checks */}
      <p className="text-xs font-bold uppercase tracking-widest mb-3" style={{ color: 'var(--text-muted)' }}>Live infrastructure</p>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-8">
        {infraItems.map(({ key, label, icon: Icon }) => {
          const c = data.checks[key]
          const st = (c?.status || 'unknown') as Status
          const color = statusColor[st]
          return (
            <div key={key} className="border p-4 transition-colors duration-200 hover:border-opacity-40" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-secondary)' }}>
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2.5">
                  <div className="flex items-center justify-center w-7 h-7 rounded-lg" style={{ backgroundColor: color.bg }}>
                    <Icon size={13} style={{ color: color.text }} />
                  </div>
                  <span className="text-xs font-medium text-[color:var(--text-primary)]">{label}</span>
                </div>
                <span className="text-[10px] font-bold tracking-widest px-2 py-0.5 rounded" style={{ color: color.text, backgroundColor: color.bg }}>
                  {statusLabel[st]}
                </span>
              </div>
              <p className="text-[11px] leading-relaxed truncate" style={{ color: 'var(--text-dim)' }} title={c?.detail}>
                {c?.detail || 'no detail'}
              </p>
            </div>
          )
        })}
      </div>

      {/* WAF */}
      <p className="text-xs font-bold uppercase tracking-widest mb-3" style={{ color: 'var(--text-muted)' }}>Edge protection (WAF)</p>
      <div className="border mb-8 p-4 sm:p-5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-secondary)' }}>
        <div className="flex items-center gap-3">
          <div className="flex items-center justify-center w-9 h-9 rounded-lg" style={{ backgroundColor: statusColor[data.waf.status as Status]?.bg || statusColor.unknown.bg }}>
            <Shield size={16} style={{ color: statusColor[data.waf.status as Status]?.text || statusColor.unknown.text }} />
          </div>
          <div className="min-w-0">
            <p className="text-sm font-medium text-[color:var(--text-primary)] capitalize">{data.waf.status.replace('_', ' ')}</p>
            <p className="text-[11px] leading-relaxed" style={{ color: 'var(--text-dim)' }}>{data.waf.detail}</p>
          </div>
          <span className="ml-auto text-[10px] font-bold tracking-widest px-2 py-0.5 rounded flex-shrink-0"
            style={(() => { const c = statusColor[data.waf.status as Status] || statusColor.unknown; return { color: c.text, backgroundColor: c.bg } })()}>
            {statusLabel[(data.waf.status as Status) || 'unknown']}
          </span>
        </div>
        {data.waf.headers && Object.keys(data.waf.headers).length > 0 && (
          <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--divider)' }}>
            <div className="flex flex-wrap gap-2">
              {Object.entries(data.waf.headers).map(([k, v]) => (
                <span key={k} className="px-2 py-1 text-[10px]" style={{ backgroundColor: 'var(--bg-raised)', color: 'var(--text-dim)' }}>
                  <span style={{ color: 'var(--text-soft)' }}>{k}:</span> {v.slice(0, 60)}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Security posture */}
      <p className="text-xs font-bold uppercase tracking-widest mb-3" style={{ color: 'var(--text-muted)' }}>Security posture</p>
      <div className="border mb-8" style={{ borderColor: 'var(--border)' }}>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b" style={{ borderColor: 'var(--border)' }}>
              <th className="px-4 sm:px-5 py-3 text-left text-xs font-medium" style={{ color: 'var(--text-muted)' }}>Check</th>
              <th className="px-4 py-3 text-left text-xs font-medium" style={{ color: 'var(--text-muted)' }}>Status</th>
              <th className="hidden sm:table-cell px-4 sm:px-5 py-3 text-left text-xs font-medium" style={{ color: 'var(--text-muted)' }}>Detail</th>
            </tr>
          </thead>
          <tbody>
            {data.posture.map((p) => {
              const st = p.status as Status
              const c = statusColor[st] || statusColor.unknown
              return (
                <tr key={p.check} className="border-b" style={{ borderColor: 'var(--divider)' }}>
                  <td className="px-4 sm:px-5 py-3">
                    <span className="flex items-center gap-2 text-xs text-[color:var(--text-primary)]">
                      {st === 'pass' || st === 'ok'
                        ? <CheckCircle2 size={12} style={{ color: c.text }} />
                        : st === 'warn'
                          ? <AlertTriangle size={12} style={{ color: c.text }} />
                          : <XCircle size={12} style={{ color: c.text }} />}
                      {p.check}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-[10px] font-bold tracking-widest px-2 py-0.5 rounded" style={{ color: c.text, backgroundColor: c.bg }}>
                      {statusLabel[st]}
                    </span>
                  </td>
                  <td className="hidden sm:table-cell px-4 sm:px-5 py-3">
                    <span className="text-[11px]" style={{ color: 'var(--text-dim)' }}>{p.detail}</span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Security events feed */}
      <div className="flex items-center justify-between mb-3">
        <p className="text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
          <span className="flex items-center gap-2">
            <Eye size={12} /> Recent security events
            <span className="font-normal" style={{ color: 'var(--text-faint)' }}>(last 50 · in-memory)</span>
          </span>
        </p>
      </div>
      <div className="border" style={{ borderColor: 'var(--border)' }}>
        {data.events.length === 0 ? (
          <div className="px-5 py-12 text-center">
            <div className="flex flex-col items-center gap-2">
              <ShieldCheck size={18} style={{ color: 'var(--border-strong)' }} />
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>No security events recorded since the last restart</p>
            </div>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b" style={{ borderColor: 'var(--border)' }}>
                  <th className="px-4 sm:px-5 py-3 text-left text-xs font-medium" style={{ color: 'var(--text-muted)' }}>Time</th>
                  <th className="px-4 py-3 text-left text-xs font-medium" style={{ color: 'var(--text-muted)' }}>Event</th>
                  <th className="hidden md:table-cell px-4 py-3 text-left text-xs font-medium" style={{ color: 'var(--text-muted)' }}>Detail</th>
                  <th className="px-4 sm:px-5 py-3 text-left text-xs font-medium" style={{ color: 'var(--text-muted)' }}>IP</th>
                </tr>
              </thead>
              <tbody>
                {data.events.map((e, i) => (
                  <tr key={i} className="border-b transition-colors hover:bg-[color:var(--hover-overlay)]" style={{ borderColor: 'var(--divider)' }}>
                    <td className="px-4 sm:px-5 py-2.5 whitespace-nowrap">
                      <span className="text-[11px] font-mono" style={{ color: 'var(--text-dim)' }} title={fmtTime(e.ts)}>
                        {fmtAgo(e.ts)}
                      </span>
                    </td>
                    <td className="px-4 py-2.5">
                      <span className="flex items-center gap-2 text-[11px]" style={{ color: eventColor[e.type] || 'var(--text-soft)' }}>
                        <MinusCircle size={10} />
                        {eventLabels[e.type] || e.type}
                      </span>
                    </td>
                    <td className="hidden md:table-cell px-4 py-2.5">
                      <span className="text-[11px] font-mono" style={{ color: 'var(--text-dim)' }}>{e.detail || '—'}</span>
                    </td>
                    <td className="px-4 sm:px-5 py-2.5">
                      <span className="text-[11px] font-mono" style={{ color: e.ip ? 'var(--text-soft)' : 'var(--text-faint)' }}>{e.ip || '—'}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
