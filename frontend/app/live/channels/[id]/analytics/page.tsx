'use client'

import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import { ArrowLeft, Loader2, BarChart3, AlertTriangle, Users, Wifi, Activity } from 'lucide-react'
import { isLoggedIn } from '@/lib/auth'
import {
  liveApi, formatBytes, formatUptime,
  type LiveAnalytics, type LiveChannel,
} from '@/lib/liveClient'

export default function LiveChannelAnalyticsPage() {
  const params = useParams<{ id: string }>()
  const id = params?.id as string
  const [channel, setChannel] = useState<LiveChannel | null>(null)
  const [analytics, setAnalytics] = useState<LiveAnalytics | null>(null)
  const [windowHours, setWindowHours] = useState(24)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    if (!id) return
    try {
      const [ch, a] = await Promise.all([
        liveApi.getChannel(id),
        liveApi.getAnalytics(id, windowHours),
      ])
      setChannel(ch)
      setAnalytics(a)
      setError('')
    } catch (err: any) {
      if (err.response?.status === 401 || err.response?.status === 404) setError(err.response?.data?.detail || 'Not found')
      else setError(err.response?.data?.detail || 'Failed to load analytics.')
    } finally {
      setLoading(false)
    }
  }, [id, windowHours])

  useEffect(() => {
    if (isLoggedIn()) load()
  }, [load])

  useEffect(() => {
    const t = setInterval(() => {
      liveApi.getAnalytics(id, windowHours).then(setAnalytics).catch(() => {})
    }, 10000)
    return () => clearInterval(t)
  }, [id, windowHours])

  if (loading) {
    return (
      <div className="mx-auto flex h-64 max-w-5xl items-center justify-center px-6">
        <Loader2 size={20} className="animate-spin" style={{ color: 'var(--text-muted)' }} />
      </div>
    )
  }

  if (error || !analytics) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-10">
        <Link href="/live" className="mb-6 inline-flex items-center gap-1.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          <ArrowLeft size={14} /> All channels
        </Link>
        <div className="flex items-center gap-3 border px-4 py-3" style={{ borderColor: 'var(--err-chip-border)', backgroundColor: 'var(--err-chip-bg)' }}>
          <AlertTriangle size={16} style={{ color: '#ef4444' }} />
          <p className="text-xs" style={{ color: '#fca5a5' }}>{error || 'Not found'}</p>
        </div>
      </div>
    )
  }

  const a = analytics
  const total = a.total_bytes || 1

  return (
    <div className="mx-auto max-w-5xl px-6 py-10 select-none">
      <Link href={`/live/channels/${id}`} className="mb-6 inline-flex items-center gap-1.5 text-sm transition-colors hover:text-white" style={{ color: 'var(--text-muted)' }}>
        <ArrowLeft size={14} /> {channel?.name || 'Channel'}
      </Link>

      <div className="mb-6 flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-xl font-bold tracking-[-0.02em] text-white">
          <BarChart3 size={18} style={{ color: 'var(--text-muted)' }} /> Live analytics
          <span className="border px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-widest"
                style={{ borderColor: '#fbbf24', color: '#fbbf24' }}>Beta</span>
        </h1>
        <select
          value={windowHours}
          onChange={(e) => setWindowHours(Number(e.target.value))}
          className="border px-3 py-1.5 text-xs text-white"
          style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)' }}
        >
          <option value={1}>Last hour</option>
          <option value={24}>Last 24 hours</option>
          <option value={168}>Last 7 days</option>
        </select>
      </div>

      {/* Headline: measured offload */}
      <div className="mb-6 border p-5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
        <p className="text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>
          Measured P2P offload
        </p>
        <p className="mt-2 text-4xl font-bold text-white">{a.p2p_offload_percent.toFixed(1)}%</p>
        <div className="mt-4 flex h-3 overflow-hidden" style={{ backgroundColor: 'var(--bg-secondary)' }}>
          <div style={{ width: `${a.p2p_offload_percent}%`, backgroundColor: 'var(--inverse-bg)' }} />
          <div style={{ width: `${100 - a.p2p_offload_percent}%`, backgroundColor: 'var(--danger-bg)' }} />
        </div>
        <div className="mt-2 flex items-center justify-between font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
          <span>P2P {formatBytes(a.bytes_p2p)}</span>
          <span>Origin {formatBytes(a.bytes_origin)}</span>
        </div>
        <p className="mt-3 text-[11px]" style={{ color: 'var(--text-faint)' }}>
          Measured delivery metric: bytes delivered by viewer peers ÷ total bytes delivered. No cost savings are
          implied unless origin/CDN pricing is configured.
        </p>
      </div>

      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[
          { label: 'Total bytes', value: formatBytes(a.total_bytes) },
          { label: 'Transfer success', value: `${a.transfer_success_rate.toFixed(1)}%` },
          { label: 'Transfer failure', value: `${a.transfer_failure_rate.toFixed(1)}%` },
          { label: 'Uptime', value: formatUptime(a.uptime_seconds) },
          { label: 'Buffering events', value: String(a.buffering_events) },
          { label: 'Playback errors', value: String(a.playback_errors) },
          { label: 'Avg startup', value: a.avg_startup_ms ? `${a.avg_startup_ms} ms` : '—' },
          { label: 'Peak viewers / peers', value: `${a.peak_viewers} / ${a.peak_peers}` },
        ].map((s) => (
          <div key={s.label} className="border p-4" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>{s.label}</p>
            <p className="mt-1 text-lg font-bold text-white">{s.value}</p>
          </div>
        ))}
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <div className="border p-4" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
          <p className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>
            <Users size={12} /> Viewers now
          </p>
          <p className="mt-1 text-lg font-bold text-white">{a.current.viewers}</p>
        </div>
        <div className="border p-4" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
          <p className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>
            <Wifi size={12} /> Peers now
          </p>
          <p className="mt-1 text-lg font-bold text-white">{a.current.peers}</p>
        </div>
        <div className="border p-4" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
          <p className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>
            <Activity size={12} /> Segments served / failed
          </p>
          <p className="mt-1 text-lg font-bold text-white">{a.segments_served} / {a.segments_failed}</p>
        </div>
      </div>
    </div>
  )
}
