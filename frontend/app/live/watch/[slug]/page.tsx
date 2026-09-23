'use client'

import { useEffect, useState } from 'react'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import { Loader2, Radio, AlertTriangle } from 'lucide-react'
import api, { isTransientError } from '@/lib/api'
import { isSafeHlsUrl, statusColor, type LivePlaybackInfo } from '@/lib/liveClient'
import LivePlayer from '@/components/LivePlayer'

export default function LiveWatchPage() {
  const params = useParams<{ slug: string }>()
  const slug = params?.slug as string
  const [playback, setPlayback] = useState<LivePlaybackInfo | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!slug) return
    let cancelled = false
    const fetchInfo = () =>
      api
        .get(`/api/live/play/${slug}`)
        .then((r) => {
          if (!cancelled) {
            setPlayback(r.data)
            setLoading(false)
          }
        })
        .catch((err) => {
          if (cancelled) return
          if (isTransientError(err) && loading) {
            setTimeout(fetchInfo, 1500)
            return
          }
          setError(err.response?.data?.detail || 'Channel not found')
          setLoading(false)
        })
    fetchInfo()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug])

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center" style={{ backgroundColor: '#000' }}>
        <Loader2 size={22} className="animate-spin" style={{ color: '#555' }} />
      </div>
    )
  }

  if (error || !playback) {
    return (
      <div className="flex min-h-screen items-center justify-center px-6" style={{ backgroundColor: '#000' }}>
        <div className="border p-10 text-center" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <AlertTriangle size={22} className="mx-auto mb-3" style={{ color: '#ef4444' }} />
          <p className="text-sm text-white">{error || 'Channel not found'}</p>
          <Link href="/live" className="mt-4 inline-block text-xs" style={{ color: '#555' }}>
            ← Back to channels
          </Link>
        </div>
      </div>
    )
  }

  // Defensive: never hand a non-http(s) URL to the player.
  if (!isSafeHlsUrl(playback.hls_url)) {
    return (
      <div className="flex min-h-screen items-center justify-center" style={{ backgroundColor: '#000' }}>
        <p className="text-sm" style={{ color: '#ef4444' }}>Invalid playback URL</p>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-8" style={{ backgroundColor: '#000' }}>
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Radio size={14} style={{ color: statusColor(playback.status) }} />
          <h1 className="text-sm font-bold text-white">{playback.channel_name}</h1>
          <span className="font-mono text-[10px] uppercase tracking-widest" style={{ color: statusColor(playback.status) }}>
            {playback.status}
          </span>
          <span className="border px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-widest"
                style={{ borderColor: '#fbbf24', color: '#fbbf24' }}>Beta</span>
        </div>
        <Link href="/live" className="text-xs transition-colors hover:text-white" style={{ color: '#555' }}>
          Dashboard →
        </Link>
      </div>
      <LivePlayer playback={playback} />
      <p className="mt-3 font-mono text-[11px]" style={{ color: '#444' }}>
        Delivery: peer-assisted (WebTorrent) with automatic origin fallback · target latency ≈ {playback.latency_target_seconds}s ·
        swarm {playback.swarm_base}
      </p>
    </div>
  )
}
