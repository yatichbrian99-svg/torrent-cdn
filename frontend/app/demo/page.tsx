'use client'

import { useEffect, useRef, useState } from 'react'
import { Users, Zap, HardDrive, Eye, Copy, Check, ArrowRight } from 'lucide-react'
import VideoPlayer from '@/components/VideoPlayer'
import api from '@/lib/api'
import { isUuid } from '@/lib/security'
import { startSeeding, SeederHandle } from '@/lib/seeder'

// ─── DEMO CONFIG ─────────────────────────────────────────────────────────────
// The demo video uploaded at /dashboard/upload (swap this after uploading a
// newer one — the whole page activates off this id).
const DEMO_FILE_ID = '4a130574-822d-43cb-a54c-f7db8204033d'
const DEMO_EMBED = `<div data-torrent-cdn="${DEMO_FILE_ID}"></div>`

// Cost basis for the live comparison — matches the landing page's published
// figures (CloudFront ≈ $45 per 1,000 viewers at ~1GB each = $0.045/GB). The
// same $/GB applies to bytes served from a TorrentCDN origin; P2P bytes are
// free, so the entire saving IS the P2P offload.
const CLOUDFRONT_RATE_PER_GB = 0.045
const TORRENTCDN_ORIGIN_RATE_PER_GB = 0.045
// Projected-cost audience: the live actuals on a tiny demo file round to $0.00,
// so alongside them we show what the SAME real per-view bytes would cost at a
// meaningful scale (1,000 viewers — the landing page's headline figure).
const PROJECTED_VIEWS = 1000

interface LiveStats {
  total_views: number
  p2p_offload_percent: number
  bandwidth_saved_gb: number
  active_peers: number
  total_gb: number
  origin_gb: number
}

interface PublicFile {
  id: string
  filename: string
  minio_url: string
  magnet_link: string
  file_size_mb: number
}

export default function DemoPage() {
  const [peers, setPeers] = useState(0)
  const [stats, setStats] = useState<LiveStats | null>(null)
  const [copied, setCopied] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  // Self-seed status: this tab fetches the demo file from origin once and seeds
  // it into the WebTorrent swarm, so the page's player (and any other viewer)
  // has a real peer to pull pieces from over WebRTC.
  const [seeding, setSeeding] = useState(false)
  const [seedPeers, setSeedPeers] = useState(0)
  const seederRef = useRef<SeederHandle | null>(null)

  // The stats API needs a real file id — the 'demo' placeholder skips polling.
  const isLive = isUuid(DEMO_FILE_ID)

  // Live stats — poll the public analytics endpoint every 5 seconds.
  useEffect(() => {
    if (!isLive) { setStats(null); return }
    let mounted = true
    const poll = () => {
      api
        .getPublicAnalytics(DEMO_FILE_ID)
        .then((data: LiveStats) => {
          if (!mounted) return
          setStats(data)
          setLastUpdated(new Date())
        })
        .catch(() => { /* 404 until the demo file exists — keep showing "—" */ })
    }
    poll()
    const timer = setInterval(poll, 5000)
    return () => { mounted = false; clearInterval(timer) }
  }, [isLive])

  // Self-seed: fetch the demo file from origin (R2 CORS is open) and seed it
  // back into the swarm via lib/seeder.ts — the SAME recipe the upload
  // auto-seed uses (magnet dn name + torrentool piece-length + magnet trackers),
  // so the infohash matches the backend-built torrent and the page's own
  // player connects to it over WebRTC. Without any seed, a solo viewer has
  // nobody to peer with and offload stays 0% even though the player works.
  // Best-effort: if the fetch or swarm never forms, direct streaming still
  // plays the video.
  useEffect(() => {
    if (!isLive) return
    let cancelled = false
    let poll: ReturnType<typeof setInterval> | undefined
    const stopSeeding = () => {
      if (seederRef.current) { try { seederRef.current.destroy() } catch {} seederRef.current = null }
      setSeeding(false); setSeedPeers(0)
    }
    const init = async () => {
      try {
        const file = await api.getPublicFile(DEMO_FILE_ID) as PublicFile
        if (cancelled) return
        const resp = await fetch(file.minio_url)
        if (!resp.ok) return
        const blob = await resp.blob()
        if (cancelled) return
        const f = new File([blob], file.filename || 'demo.mp4', { type: blob.type || 'video/mp4' })
        const seeder = await startSeeding(f, file.magnet_link)
        if (cancelled || !seeder) return
        seederRef.current = seeder
        setSeeding(true)
        setSeedPeers(seeder.peers())
        poll = setInterval(() => setSeedPeers(seeder.peers()), 2000)
      } catch { /* best-effort — direct streaming still works without a seed */ }
    }
    init()
    return () => { cancelled = true; if (poll) clearInterval(poll); stopSeeding() }
  }, [isLive])

  const handleCopy = async () => {
    await navigator.clipboard.writeText(DEMO_EMBED)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  // Live cost comparison from ACTUAL bytes served: CloudFront would bill all
  // of it; TorrentCDN bills only the origin bytes — P2P delivery is free.
  const totalGb = stats?.total_gb ?? 0
  const savedGb = stats?.bandwidth_saved_gb ?? 0
  const originGb = stats?.origin_gb ?? 0
  const views = Math.max(stats?.total_views ?? 0, 1) // avoid div-by-zero in projection
  const cloudfrontCost = totalGb * CLOUDFRONT_RATE_PER_GB
  const torrentcdnCost = originGb * TORRENTCDN_ORIGIN_RATE_PER_GB

  // Projected at 1,000 views: reuse the REAL average bytes-per-view recorded
  // so far, so the projection is grounded in this file's actual traffic.
  const perViewGb = totalGb / views
  const perViewOriginGb = originGb / views
  const projectedGb = perViewGb * PROJECTED_VIEWS
  const projectedOriginGb = perViewOriginGb * PROJECTED_VIEWS
  const projectedCloudfront = projectedGb * CLOUDFRONT_RATE_PER_GB
  const projectedTorrentcdn = projectedOriginGb * TORRENTCDN_ORIGIN_RATE_PER_GB

  // Money formatting: keep sub-cent actuals visible (they're real — a 1MB demo
  // file genuinely costs fractions of a cent), so nothing silently reads $0.00.
  const money = (n: number) => {
    if (n >= 100) return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    if (n >= 0.01) return `$${n.toFixed(2)}`
    if (n > 0) return `$${n.toFixed(4)}`
    return '$0.00'
  }

  const statCards = [
    { label: 'Active peers', value: stats ? String(stats.active_peers) : '—', icon: Users },
    { label: 'Bandwidth saved', value: stats ? `${stats.bandwidth_saved_gb.toFixed(3)} GB` : '—', icon: HardDrive },
    { label: 'P2P offload', value: stats ? `${stats.p2p_offload_percent.toFixed(1)}%` : '—', icon: Zap },
    { label: 'Total views', value: stats ? String(stats.total_views) : '—', icon: Eye },
  ]

  const costRow = (label: string, value: string, color?: string) => (
    <div className="flex items-center justify-between gap-4">
      <span style={{ color: '#555' }}>{label}</span>
      <span className="font-bold" style={{ color: color || '#fff' }}>{value}</span>
    </div>
  )

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      {/* Header */}
      <div className="mb-10">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1.5 border px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-[0.2em]" style={{ borderColor: '#333', color: '#4ade80' }}>
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping opacity-75" style={{ backgroundColor: '#4ade80' }} />
              <span className="relative inline-flex h-1.5 w-1.5" style={{ backgroundColor: '#4ade80' }} />
            </span>
            Live
          </span>
          {!isLive && (
            <span className="border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.2em]" style={{ borderColor: '#553333', color: '#ef4444' }}>
              Demo file pending
            </span>
          )}
        </div>
        <h1 className="text-3xl font-bold tracking-[-0.02em] text-white sm:text-4xl">Live P2P Demo</h1>
        <p className="mt-2 text-sm" style={{ color: '#555' }}>
          Watch how viewers become peers in real time.
        </p>
      </div>

      {/* Video player */}
      <section className="mb-10">
        <VideoPlayer fileId={DEMO_FILE_ID} onPeers={setPeers} />
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <p className="flex items-center gap-2 font-mono text-xs" style={{ color: '#888' }}>
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping opacity-60" style={{ backgroundColor: peers > 0 ? '#4ade80' : '#333' }} />
              <span className="relative inline-flex h-2 w-2" style={{ backgroundColor: peers > 0 ? '#4ade80' : '#333' }} />
            </span>
            {peers} {peers === 1 ? 'peer' : 'peers'} connected right now
          </p>
          <p className="font-mono text-xs" style={{ color: '#888' }}>
            P2P offload:{' '}
            <span className="font-bold text-white">{stats ? `${stats.p2p_offload_percent.toFixed(1)}%` : '—'}</span>
          </p>
        </div>
        {/* Self-seed indicator — this tab is a peer in the swarm */}
        {isLive && seeding && (
          <div className="mt-3 flex items-center gap-2 border-t pt-3" style={{ borderColor: '#222' }}>
            <span className="h-2 w-2 animate-pulse rounded-full" style={{ backgroundColor: seedPeers > 0 ? '#4ade80' : '#444' }} />
            <span className="font-mono text-[11px]" style={{ color: '#666' }}>
              {seedPeers > 0
                ? `your browser is a peer — seeding to ${seedPeers} ${seedPeers === 1 ? 'viewer' : 'viewers'}`
                : 'this tab is seeding the video to the swarm…'}
            </span>
          </div>
        )}
        {!isLive && (
          <p className="mt-2 font-mono text-[11px]" style={{ color: '#666' }}>
            &gt; Player goes live once a demo video is uploaded — replace <span className="text-white">DEMO_FILE_ID</span> at the top of <span className="text-white">frontend/app/demo/page.tsx</span>.
          </p>
        )}
      </section>

      {/* Live stats */}
      <section className="mb-10">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Live stats</h2>
          <p className="font-mono text-[10px]" style={{ color: '#444' }}>
            {isLive ? (lastUpdated ? `updated ${lastUpdated.toLocaleTimeString()}` : 'connecting…') : 'waiting for demo file'}
          </p>
        </div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {statCards.map((s) => (
            <div key={s.label} className="border p-4" style={{ borderColor: '#222', backgroundColor: '#111' }}>
              <p className="flex items-center gap-2 text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>
                <s.icon size={12} /> {s.label}
              </p>
              <p className="mt-2 font-mono text-2xl font-bold text-white">{s.value}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Cost comparison */}
      <section className="mb-10">
        <div className="border p-6" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <h2 className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Live cost comparison</h2>

          {/* Real actuals so far */}
          <div className="mt-5 space-y-3 font-mono text-sm">
            {costRow('If this were CloudFront', stats ? money(cloudfrontCost) : '—')}
            {costRow('Actual cost on TorrentCDN', stats ? money(torrentcdnCost) : '—')}
            {costRow(
              'Saved by P2P',
              stats ? money(Math.max(0, cloudfrontCost - torrentcdnCost)) : '—',
              '#4ade80'
            )}
          </div>
          <p className="mt-3 font-mono text-[11px]" style={{ color: '#444' }}>
            Real bytes served so far{stats ? ` — ${totalGb.toFixed(4)} GB total, ${savedGb.toFixed(4)} GB via peers` : ''}. P2P delivery is free; TorrentCDN only bills what leaves your origin.
          </p>

          {/* Projection at scale — same real per-view bytes, 1,000 viewers */}
          <div className="mt-5 border-t pt-4" style={{ borderColor: '#222' }}>
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#888', fontFamily: 'monospace' }}>
              At {PROJECTED_VIEWS.toLocaleString()} views — from your real per-view bytes
            </p>
            <div className="mt-3 space-y-3 font-mono text-sm">
              {costRow('If this were CloudFront', stats ? money(projectedCloudfront) : '—')}
              {costRow('Actual cost on TorrentCDN', stats ? money(projectedTorrentcdn) : '—')}
              {costRow(
                'Saved by P2P',
                stats ? money(Math.max(0, projectedCloudfront - projectedTorrentcdn)) : '—',
                '#4ade80'
              )}
            </div>
            <p className="mt-3 font-mono text-[11px]" style={{ color: '#444' }}>
              Projection reuses the observed average bytes/view{stats ? ` (${perViewGb.toFixed(4)} GB/view, ${stats.p2p_offload_percent.toFixed(1)}% P2P)` : ''} — the numbers scale linearly with traffic.
            </p>
          </div>
        </div>
      </section>

      {/* Embed code */}
      <section className="mb-10">
        <h2 className="mb-3 text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>One-line embed</h2>
        <div className="border p-4" style={{ backgroundColor: '#0c0c0c', borderColor: '#222' }}>
          <div className="mb-3 flex items-center justify-between">
            <div className="terminal-dots"><span /><span /><span /></div>
            <button
              onClick={handleCopy}
              className="flex items-center gap-1.5 text-xs transition-colors hover:text-white"
              style={{ color: '#555', fontFamily: 'monospace' }}
            >
              {copied ? <Check size={12} style={{ color: '#4ade80' }} /> : <Copy size={12} />}
              {copied ? 'copied' : 'copy'}
            </button>
          </div>
          <pre className="overflow-x-auto font-mono text-sm text-white"><code>{DEMO_EMBED}</code></pre>
        </div>
      </section>

      {/* CTA */}
      <section className="border p-10 text-center" style={{ borderColor: '#222', backgroundColor: '#111' }}>
        <h2 className="text-xl font-bold tracking-[-0.02em] text-white">Want this for your platform?</h2>
        <p className="mx-auto mt-2 max-w-md text-sm" style={{ color: '#555' }}>
          Upload a video, paste one line of code, and your viewers start sharing bandwidth immediately.
        </p>
        <a
          href="https://www.torrentcdn.store/dashboard/upload"
          className="mt-6 inline-flex items-center gap-2 border px-6 py-3 text-sm font-medium transition-opacity hover:opacity-80"
          style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}
        >
          Start uploading <ArrowRight size={14} />
        </a>
      </section>
    </div>
  )
}
