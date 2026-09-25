'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import { Radio, Loader2, Plus, Users, Wifi, Activity, AlertTriangle } from 'lucide-react'
import { isLoggedIn } from '@/lib/auth'
import { liveApi, statusColor, formatUptime, formatBitrate, type LiveChannel } from '@/lib/liveClient'

export default function LiveIndexPage() {
  const router = useRouter()
  const [channels, setChannels] = useState<LiveChannel[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')

  useEffect(() => {
    if (!isLoggedIn()) router.push('/')
  }, [router])

  const load = useCallback(() => {
    setLoading(true)
    liveApi
      .listChannels()
      .then((data) => {
        setChannels(data)
        setError('')
      })
      .catch((err) => {
        if (err.response?.status === 401) router.push('/')
        else setError(err.response?.data?.detail || 'Failed to load channels.')
      })
      .finally(() => setLoading(false))
  }, [router])

  useEffect(() => {
    if (isLoggedIn()) load()
  }, [load])

  // Light polling keeps status/viewers fresh without a websocket.
  useEffect(() => {
    if (!channels.length) return
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [channels.length, load])

  const handleCreate = async () => {
    if (!newName.trim()) return
    setCreating(true)
    try {
      const ch = await liveApi.createChannel(newName.trim(), newDesc.trim() || undefined)
      setShowCreate(false)
      setNewName('')
      setNewDesc('')
      // One-time stream-key handoff: the plaintext key is returned ONLY here;
      // the detail page consumes ?key= and immediately scrubs it from the URL.
      if (ch.stream_key) {
        router.push(`/live/channels/${ch.id}?key=${encodeURIComponent(ch.stream_key)}`)
      } else {
        router.push(`/live/channels/${ch.id}`)
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to create channel.')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="mx-auto max-w-5xl px-6 py-10 select-none">
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">
            <Radio size={18} style={{ color: 'var(--text-muted)' }} /> Live Streaming
            <span className="border px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-widest"
                  style={{ borderColor: 'var(--warn)', color: 'var(--warn)' }}>Beta</span>
          </h1>
          <p className="mt-1 text-sm" style={{ color: 'var(--text-muted)' }}>
            P2P-assisted live channels — publish from OBS, deliver over WebTorrent, fall back to origin automatically.
          </p>
        </div>
        <button
          onClick={() => setShowCreate((s) => !s)}
          className="flex items-center gap-2 border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80"
          style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
        >
          <Plus size={14} /> New channel
        </button>
      </div>

      {showCreate && (
        <div className="mb-8 border p-5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
          <p className="text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>
            Create a live channel
          </p>
          <div className="mt-3 grid gap-3">
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Channel name (e.g. Sunday Service)"
              maxLength={120}
              className="border px-3 py-2 text-sm text-[color:var(--text-primary)]"
              style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)' }}
            />
            <input
              value={newDesc}
              onChange={(e) => setNewDesc(e.target.value)}
              placeholder="Description (optional)"
              maxLength={500}
              className="border px-3 py-2 text-sm text-[color:var(--text-primary)]"
              style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)' }}
            />
            <div className="flex items-center gap-3">
              <button
                onClick={handleCreate}
                disabled={creating || !newName.trim()}
                className="border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
                style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
              >
                {creating ? <Loader2 size={12} className="animate-spin" /> : 'Create channel'}
              </button>
              <button onClick={() => setShowCreate(false)} className="text-xs" style={{ color: 'var(--text-muted)' }}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="mb-6 flex items-center gap-3 border px-4 py-3" style={{ borderColor: 'var(--err-chip-border)', backgroundColor: 'var(--err-chip-bg)' }}>
          <AlertTriangle size={16} style={{ color: 'var(--err)' }} />
          <p className="flex-1 text-xs" style={{ color: 'var(--err-soft)' }}>{error}</p>
          <button onClick={load} className="border px-3 py-1 text-xs" style={{ borderColor: 'var(--danger-bg)', color: 'var(--text-primary)' }}>
            Refresh
          </button>
        </div>
      )}

      {loading ? (
        <div className="flex h-40 items-center justify-center border" style={{ borderColor: 'var(--border)' }}>
          <Loader2 size={20} className="animate-spin" style={{ color: 'var(--text-muted)' }} />
        </div>
      ) : !error && channels.length === 0 ? (
        <div className="border p-16 text-center" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
          <Radio size={24} className="mx-auto mb-3" style={{ color: 'var(--text-muted)' }} />
          <p className="text-sm text-[color:var(--text-primary)]">No live channels yet.</p>
          <p className="mt-1 text-xs" style={{ color: 'var(--text-muted)' }}>
            Create one, copy the RTMP settings into OBS, and go live.
          </p>
          <button
            onClick={() => setShowCreate(true)}
            className="mt-4 inline-block border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80"
            style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
          >
            Create your first channel &rarr;
          </button>
        </div>
      ) : (
        <div>
          <div
            className="flex items-center gap-4 border-b px-4 py-2 text-xs font-bold uppercase tracking-widest"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)', fontFamily: 'monospace' }}
          >
            <div className="flex-1">Channel</div>
            <div className="w-20">Status</div>
            <div className="w-16 text-right">Viewers</div>
            <div className="w-16 text-right">Peers</div>
            <div className="w-24 text-right">Bitrate</div>
            <div className="w-20 text-right">Uptime</div>
          </div>
          {channels.map((ch) => (
            <Link
              key={ch.id}
              href={`/live/channels/${ch.id}`}
              className="flex items-center gap-4 border-b px-4 py-3 text-sm transition-colors hover:bg-[color:var(--hover-overlay)]"
              style={{ borderColor: 'var(--border)' }}
            >
              <div className="flex-1">
                <p className="font-medium text-[color:var(--text-primary)]">{ch.name}</p>
                <p className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>/{ch.slug}</p>
              </div>
              <div className="w-20">
                <span className="flex items-center gap-1.5 font-mono text-xs" style={{ color: statusColor(ch.status) }}>
                  <span className="h-2 w-2" style={{ backgroundColor: statusColor(ch.status) }} />
                  {ch.status}
                </span>
              </div>
              <div className="w-16 text-right font-mono text-xs" style={{ color: 'var(--text-muted)' }}>{ch.current_viewers}</div>
              <div className="w-16 text-right font-mono text-xs" style={{ color: 'var(--text-muted)' }}>{ch.current_peers}</div>
              <div className="w-24 text-right font-mono text-xs" style={{ color: 'var(--text-muted)' }}>{formatBitrate(ch.bitrate_kbps)}</div>
              <div className="w-20 text-right font-mono text-xs" style={{ color: 'var(--text-muted)' }}>{formatUptime(ch.uptime_seconds)}</div>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
