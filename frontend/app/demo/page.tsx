'use client'

import { useEffect, useState } from 'react'
import { Users, Zap, HardDrive, Eye, Copy, Check, ArrowRight } from 'lucide-react'
import VideoPlayer from '@/components/VideoPlayer'
import api from '@/lib/api'
import { isUuid } from '@/lib/security'

// ─── DEMO CONFIG ─────────────────────────────────────────────────────────────
// The demo video uploaded at /dashboard/upload (swap this after uploading a
// newer one — the whole page activates off this id). 'demo' = placeholder.
const DEMO_FILE_ID = 'demo'
const DEMO_EMBED = `<div data-torrent-cdn="${DEMO_FILE_ID}"></div>`

// Cost basis for the live comparison — matches the landing page's published
// figures (CloudFront ≈ $45 per 1,000 viewers at ~1GB each = $0.045/GB). The
// same $/GB applies to bytes served from a TorrentCDN origin; P2P bytes are
// free, so the entire saving IS the P2P offload.
const CLOUDFRONT_RATE_PER_GB = 0.045
const TORRENTCDN_ORIGIN_RATE_PER_GB = 0.045

interface LiveStats {
  total_views: number
  p2p_offload_percent: number
  bandwidth_saved_gb: number
  active_peers: number
  total_gb: number
  origin_gb: number
}

export default function DemoPage() {
  const [peers, setPeers] = useState(0)
  const [stats, setStats] = useState<LiveStats | null>(null)
  const [copied, setCopied] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

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
  const cloudfrontCost = totalGb * CLOUDFRONT_RATE_PER_GB
  const torrentcdnCost = originGb * TORRENTCDN_ORIGIN_RATE_PER_GB
  const money = (n: number) =>
    n >= 100
      ? `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
      : `$${n.toFixed(2)}`

  const statCards = [
    { label: 'Active peers', value: stats ? String(stats.active_peers) : '—', icon: Users },
    { label: 'Bandwidth saved', value: stats ? `${stats.bandwidth_saved_gb.toFixed(3)} GB` : '—', icon: HardDrive },
    { label: 'P2P offload', value: stats ? `${stats.p2p_offload_percent.toFixed(1)}%` : '—', icon: Zap },
    { label: 'Total views', value: stats ? String(stats.total_views) : '—', icon: Eye },
  ]

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
          <div className="mt-5 space-y-3 font-mono text-sm">
            <div className="flex items-center justify-between gap-4">
              <span style={{ color: '#555' }}>If this were CloudFront</span>
              <span className="font-bold text-white">{money(cloudfrontCost)}</span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span style={{ color: '#555' }}>Actual cost on TorrentCDN</span>
              <span className="font-bold text-white">{money(torrentcdnCost)}</span>
            </div>
            <div className="flex items-center justify-between gap-4 border-t pt-3" style={{ borderColor: '#222' }}>
              <span style={{ color: '#4ade80' }}>Saved by P2P</span>
              <span className="font-bold" style={{ color: '#4ade80' }}>{money(Math.max(0, cloudfrontCost - torrentcdnCost))}</span>
            </div>
          </div>
          <p className="mt-4 font-mono text-[11px]" style={{ color: '#444' }}>
            Based on actual bytes served{stats ? ` — ${totalGb.toFixed(3)} GB total, ${savedGb.toFixed(3)} GB via peers` : ''}. P2P delivery is free; TorrentCDN only bills what leaves your origin.
          </p>
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
