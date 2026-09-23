'use client'

import { useCallback, useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import Link from 'next/link'
import {
  ArrowLeft, Radio, Loader2, Copy, Check, RefreshCw, Trash2, Play,
  BarChart3, Users, Wifi, Activity, AlertTriangle, ExternalLink, Eye,
} from 'lucide-react'
import { isLoggedIn } from '@/lib/auth'
import {
  liveApi, statusColor, formatUptime, formatBitrate, formatBytes,
  isLocalRtmpUrl,
  type LiveChannel, type LiveStatus, type LiveIngestDiagnostics,
} from '@/lib/liveClient'

interface IngestInfo {
  rtmp_url: string
  publish_path: string
  hls_url: string
}

export default function LiveChannelDetailPage() {
  const router = useRouter()
  const params = useParams<{ id: string }>()
  const id = params?.id as string

  const [channel, setChannel] = useState<LiveChannel | null>(null)
  const [status, setStatus] = useState<LiveStatus | null>(null)
  const [diag, setDiag] = useState<LiveIngestDiagnostics | null>(null)
  const [ingest, setIngest] = useState<IngestInfo | null>(null)
  // One-time OBS setup secrets. The plaintext key is returned ONLY by channel
  // creation (handed over via ?key= and immediately stripped from the URL) and
  // by Reveal/Rotate — it is stored hashed server-side and can never be
  // re-fetched, exactly like the tcdn_ API keys.
  const [setup, setSetup] = useState<{ key: string; server?: string; path?: string } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [copied, setCopied] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [confirmRotate, setConfirmRotate] = useState(false)

  const load = useCallback(async () => {
    try {
      const [ch, st] = await Promise.all([liveApi.getChannel(id), liveApi.getStatus(id)])
      setChannel(ch)
      setStatus(st)
      // Playback info carries the canonical URLs (config-driven, no secrets).
      const pb = await liveApi.getPlaybackInfo(id)
      setIngest({
        // Config-driven — the backend derives these from MEDIAMTX_RTMP_URL /
        // MEDIAMTX_HLS_URL; the client never invents hosts.
        rtmp_url: pb.rtmp_server || (process.env.NEXT_PUBLIC_RTMP_INGEST_URL || 'rtmp://localhost:1935') + '/live',
        publish_path: pb.publish_path || `live/${ch.slug}`,
        hls_url: pb.hls_url,
      })
      setError('')
    } catch (err: any) {
      if (err.response?.status === 401) router.push('/')
      else if (err.response?.status === 404) router.push('/live')
      else setError(err.response?.data?.detail || 'Failed to load channel.')
    } finally {
      setLoading(false)
    }
  }, [id, router])

  useEffect(() => {
    if (isLoggedIn() && id) load()
  }, [id, load])

  // One-time key handoff from channel creation: /live/channels/{id}?key=...
  // Move it into state and scrub it from the address bar immediately so it is
  // never left in history, referrers, or a shared link.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const sp = new URLSearchParams(window.location.search)
    const k = sp.get('key')
    if (!k) return
    setSetup({ key: k })
    sp.delete('key')
    const qs = sp.toString()
    window.history.replaceState(null, '', window.location.pathname + (qs ? `?${qs}` : ''))
  }, [id])

  useEffect(() => {
    if (!id) return
    const tick = () => {
      liveApi.getStatus(id).then(setStatus).catch(() => {})
      // Diagnostics poll piggybacks on the status poll (5s) — the backend
      // caches its HLS probe 10s so this stays cheap.
      liveApi.getIngestDiagnostics(id).then(setDiag).catch(() => {})
    }
    tick()
    const t = setInterval(tick, 5000)
    return () => clearInterval(t)
  }, [id])

  const copy = async (label: string, value: string) => {
    await navigator.clipboard.writeText(value)
    setCopied(label)
    setTimeout(() => setCopied(null), 2000)
  }

  // Reveal = generate a fresh key and show it. Two-step confirm because this
  // instantly invalidates any key already configured in OBS.
  const handleReveal = async () => {
    if (!confirmRotate) {
      setConfirmRotate(true)
      setTimeout(() => setConfirmRotate(false), 5000)
      return
    }
    setBusy('rotate')
    try {
      const res = await liveApi.rotateKey(id)
      setSetup({ key: res.stream_key, server: res.rtmp_server || undefined, path: res.publish_path || undefined })
      setConfirmRotate(false)
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to rotate key.')
    } finally {
      setBusy(null)
    }
  }

  const handleToggleEnable = async () => {
    if (!channel) return
    setBusy('enable')
    try {
      if (channel.enabled) await liveApi.disableChannel(id)
      else await liveApi.enableChannel(id)
      await load()
    } finally {
      setBusy(null)
    }
  }

  const handleDelete = async () => {
    if (!confirmDelete) {
      setConfirmDelete(true)
      setTimeout(() => setConfirmDelete(false), 5000)
      return
    }
    setBusy('delete')
    try {
      await liveApi.deleteChannel(id)
      router.push('/live')
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to delete channel.')
      setBusy(null)
      setConfirmDelete(false)
    }
  }

  if (loading) {
    return (
      <div className="mx-auto flex h-64 max-w-5xl items-center justify-center px-6">
        <Loader2 size={20} className="animate-spin" style={{ color: '#555' }} />
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-5xl px-6 py-10 select-none">
      <Link href="/live" className="mb-6 inline-flex items-center gap-1.5 text-sm transition-colors hover:text-white" style={{ color: '#555' }}>
        <ArrowLeft size={14} /> All channels
      </Link>

      {error && (
        <div className="mb-6 flex items-center gap-3 border px-4 py-3" style={{ borderColor: '#331111', backgroundColor: '#1a0d0d' }}>
          <AlertTriangle size={16} style={{ color: '#ef4444' }} />
          <p className="flex-1 text-xs" style={{ color: '#fca5a5' }}>{error}</p>
        </div>
      )}

      <div className="mb-8 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-3 text-xl font-bold tracking-[-0.02em] text-white">
            <span className="flex items-center gap-1.5 border px-2 py-1 font-mono text-[10px] uppercase tracking-widest"
                  style={{ borderColor: statusColor(status?.status || 'OFFLINE'), color: statusColor(status?.status || 'OFFLINE') }}>
              <span className="h-2 w-2" style={{ backgroundColor: statusColor(status?.status || 'OFFLINE') }} />
              {status?.status || channel?.status || '…'}
            </span>
            {channel?.name}
            <span className="border px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-widest"
                  style={{ borderColor: '#fbbf24', color: '#fbbf24' }}>Beta</span>
          </h1>
          <p className="mt-1 font-mono text-xs" style={{ color: '#555' }}>/{channel?.slug}</p>
        </div>
        <div className="flex items-center gap-2">
          <Link
            href={`/live/channels/${id}/analytics`}
            className="flex items-center gap-2 border px-4 py-2 text-sm transition-opacity hover:opacity-80"
            style={{ borderColor: '#222', color: '#fff' }}
          >
            <BarChart3 size={14} /> Analytics
          </Link>
          <Link
            href={`/live/watch/${channel?.slug || ''}`}
            className="flex items-center gap-2 border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80"
            style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}
          >
            <Play size={14} /> Open player
          </Link>
        </div>
      </div>

      {/* WHY the channel is in its current stage — never a bare STARTING/ERROR. */}
      {(diag?.status_reason || status?.status_reason) && (
        <div className="mb-6 flex items-center gap-3 border px-4 py-3"
             style={{
               borderColor: status?.status === 'ERROR' ? '#331111' : status?.status === 'LIVE' ? '#113311' : '#332a11',
               backgroundColor: status?.status === 'ERROR' ? '#1a0d0d' : status?.status === 'LIVE' ? '#0d1a0d' : '#1a170d',
             }}>
          {status?.status === 'ERROR' && <AlertTriangle size={16} style={{ color: '#ef4444' }} />}
          {status?.status === 'LIVE' && <Check size={16} style={{ color: '#4ade80' }} />}
          <p className="flex-1 text-xs" style={{ color: '#ddd' }}>
            {diag?.status_reason || status?.status_reason}
          </p>
        </div>
      )}

      {/* Live stats row */}
      <div className="mb-8 grid gap-4 sm:grid-cols-3 lg:grid-cols-6">
        {[
          { label: 'Viewers', value: String(status?.current_viewers ?? 0), icon: <Users size={12} /> },
          { label: 'Peers', value: String(status?.current_peers ?? 0), icon: <Wifi size={12} /> },
          { label: 'Bitrate', value: formatBitrate(status?.bitrate_kbps ?? 0), icon: <Activity size={12} /> },
          { label: 'Uptime', value: formatUptime(status?.uptime_seconds ?? 0), icon: <Radio size={12} /> },
          { label: 'Segment', value: status?.latest_segment_sequence != null ? `#${status.latest_segment_sequence}` : '—', icon: <Activity size={12} /> },
          { label: 'Health', value: channel?.enabled ? 'enabled' : 'disabled', icon: <Check size={12} /> },
        ].map((s) => (
          <div key={s.label} className="border p-4" style={{ borderColor: '#222', backgroundColor: '#111' }}>
            <p className="flex items-center gap-1.5 text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>
              {s.icon} {s.label}
            </p>
            <p className="mt-1 text-lg font-bold text-white">{s.value}</p>
          </div>
        ))}
      </div>

      {/* Ingest diagnostics — where the RTMP → MediaMTX → HLS chain stands. */}
      <div className="mb-8 border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
        <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>
          Ingest diagnostics
        </p>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {([
            ['Publisher', diag ? (diag.publisher_connected ? `Connected${diag.publisher_uptime_seconds ? ` · ${formatUptime(diag.publisher_uptime_seconds)}` : ''}` : 'Disconnected') : '—',
              diag?.publisher_connected ? '#4ade80' : '#555'],
            ['Auth', diag ? diag.auth_status : '—', diag?.auth_status === 'accepted' ? '#4ade80' : diag?.auth_status === 'denied' ? '#ef4444' : '#555'],
            ['HLS playlist', diag ? (diag.hls_playlist_available == null ? 'unknown' : diag.hls_playlist_available ? 'Available' : 'Not available') : '—',
              diag?.hls_playlist_available ? '#4ade80' : '#555'],
            ['Reconnects', diag ? String(diag.reconnect_count) : '—', '#ddd'],
          ] as [string, string, string][]).map(([label, value, color]) => (
            <div key={label} className="border p-3" style={{ borderColor: '#222', backgroundColor: '#0c0c0c' }}>
              <p className="text-[10px] font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>{label}</p>
              <p className="mt-1 text-sm font-bold" style={{ color }}>{value}</p>
            </div>
          ))}
          {diag?.ingest_bitrate_kbps ? (
            <div className="border p-3" style={{ borderColor: '#222', backgroundColor: '#0c0c0c' }}>
              <p className="text-[10px] font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Ingest bitrate</p>
              <p className="mt-1 text-sm font-bold text-white">{formatBitrate(diag.ingest_bitrate_kbps)}</p>
            </div>
          ) : null}
          {diag?.resolution || diag?.codec || diag?.fps ? (
            <div className="border p-3" style={{ borderColor: '#222', backgroundColor: '#0c0c0c' }}>
              <p className="text-[10px] font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Stream</p>
              <p className="mt-1 text-sm font-bold text-white">
                {[diag?.resolution, diag?.codec, diag?.fps ? `${Math.round(diag.fps)} fps` : null].filter(Boolean).join(' · ')}
              </p>
            </div>
          ) : null}
        </div>
      </div>

      {/* Ingest configuration */}
      <div className="mb-8 border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
        <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>
          OBS / Encoder settings
        </p>
        <p className="mt-1 text-sm" style={{ color: '#555' }}>
          In OBS → Settings → Stream, choose Custom…, and paste:
        </p>
        {(() => {
          const rtmpUrl = setup?.server || ingest?.rtmp_url || ''
          if (rtmpUrl && isLocalRtmpUrl(rtmpUrl)) {
            return (
              <div className="mt-3 flex items-center gap-3 border px-4 py-3" style={{ borderColor: '#332a11', backgroundColor: '#1a170d' }}>
                <AlertTriangle size={16} style={{ color: '#fbbf24' }} />
                <p className="flex-1 text-xs" style={{ color: '#fbbf24' }}>
                  This is a <strong>LOCAL DEVELOPMENT RTMP URL</strong> — it only works for an encoder on this same machine.
                  An external broadcaster (OBS/Wirecast on another device/network) needs the PUBLIC RTMP URL:
                  set <code className="font-mono">MEDIAMTX_RTMP_URL</code> to the public host (e.g. <code className="font-mono">rtmp://live.yourdomain.com:1935</code>)
                  and open TCP port 1935.
                </p>
              </div>
            )
          }
          return null
        })()}
        <div className="mt-4 space-y-3">
          <FieldRow label="Server" value={setup?.server || ingest?.rtmp_url || ''} copied={copied === 'server'} onCopy={() => copy('server', setup?.server || ingest?.rtmp_url || '')} />
          {/* OBS 'Stream key' field: <slug>?pass=<key> — MediaMTX forwards the
              stream-name query (user/pass) to the auth hook, and the slug
              routes the publish to the right channel. */}
          {setup ? (
            <FieldRow label="Stream key (OBS)" value={`${channel?.slug ?? ''}?pass=${setup.key}`} copied={copied === 'key'} onCopy={() => copy('key', `${channel?.slug ?? ''}?pass=${setup.key}`)} />
          ) : (
            <div className="flex items-center gap-2">
              <p className="w-32 shrink-0 text-xs uppercase tracking-widest" style={{ color: '#555' }}>Stream key (OBS)</p>
              <code className="flex-1 border px-3 py-2 font-mono text-xs" style={{ backgroundColor: '#0c0c0c', borderColor: '#333', color: '#555' }}>
                •••••••••••••••••••• hidden — stored hashed, shown once per key
              </code>
              <button
                onClick={handleReveal}
                disabled={busy === 'rotate'}
                className="flex items-center gap-2 border px-4 py-2 text-xs font-bold transition-opacity hover:opacity-80 disabled:opacity-50"
                style={{ borderColor: confirmRotate ? '#ef4444' : '#fff', backgroundColor: confirmRotate ? '#7f1d1d' : 'transparent', color: confirmRotate ? '#fff' : '#fff' }}
              >
                {busy === 'rotate' ? <Loader2 size={12} className="animate-spin" /> : <Eye size={12} />}
                {confirmRotate ? 'Click again — old key stops working' : 'Reveal stream key'}
              </button>
            </div>
          )}
        </div>
        {setup && (
          <p className="mt-3 text-xs" style={{ color: '#fbbf24' }}>
            Shown only for this visit — paste the full Stream key (it carries the channel) into OBS now.
            Stored hashed server-side; regenerate anytime with Reveal.
          </p>
        )}
        <div className="mt-5 flex flex-wrap items-center gap-3 border-t pt-4" style={{ borderColor: '#222' }}>
          {!setup && (
            <button onClick={handleReveal} disabled={busy === 'rotate'}
                    className="border px-4 py-2 text-xs font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
                    style={{ borderColor: confirmRotate ? '#ef4444' : '#553333', color: '#ef4444' }}>
              {busy === 'rotate' ? <Loader2 size={12} className="animate-spin" /> : confirmRotate ? 'Click again — old key stops working' : 'Rotate stream key'}
            </button>
          )}
          <button onClick={handleToggleEnable} disabled={busy === 'enable'}
                  className="border px-4 py-2 text-xs transition-opacity hover:opacity-80 disabled:opacity-50"
                  style={{ borderColor: '#333', color: channel?.enabled ? '#fbbf24' : '#4ade80' }}>
            {channel?.enabled ? 'Disable channel' : 'Enable channel'}
          </button>
          <button onClick={handleDelete} disabled={busy === 'delete'}
                  className="border px-4 py-2 text-xs transition-opacity hover:opacity-80 disabled:opacity-50"
                  style={{ borderColor: confirmDelete ? '#ef4444' : '#553333', backgroundColor: confirmDelete ? '#7f1d1d' : 'transparent', color: '#ef4444' }}>
            {busy === 'delete' ? 'Deleting…' : confirmDelete ? 'Click again to confirm' : (<><Trash2 size={11} className="inline" /> Delete</>)}
          </button>
          <a href={ingest?.hls_url || '#'} target="_blank" rel="noopener noreferrer"
             className="ml-auto flex items-center gap-1 text-xs transition-colors hover:text-white" style={{ color: '#555' }}>
            HLS URL <ExternalLink size={11} />
          </a>
        </div>
      </div>

      {/* Stream key prefix (safe) */}
      {channel?.stream_key_prefix && !setup && (
        <p className="mb-8 text-xs" style={{ color: '#555' }}>
          Current key: <code className="font-mono text-white">{channel.stream_key_prefix}…</code>
          {' '}(use Reveal to generate a fresh one — the old key stops working immediately)
        </p>
      )}
    </div>
  )
}

function FieldRow({ label, value, copied, onCopy, secret }: {
  label: string
  value: string
  copied: boolean
  onCopy: () => void
  secret?: boolean
}) {
  return (
    <div className="flex items-center gap-2">
      <p className="w-32 shrink-0 text-xs uppercase tracking-widest" style={{ color: '#555' }}>{label}</p>
      <code className="flex-1 truncate border px-3 py-2 font-mono text-xs text-white"
            style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}>
        {value}
      </code>
      <button onClick={onCopy} disabled={secret}
              className="border px-3 py-2 text-xs transition-opacity hover:opacity-80 disabled:opacity-40"
              style={{ borderColor: '#333', color: '#555' }}>
        {copied ? <><Check size={12} className="inline" style={{ color: '#4ade80' }} /> Copied</> : <><Copy size={12} className="inline" /> Copy</>}
      </button>
    </div>
  )
}
