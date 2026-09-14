'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { Users, Loader2 } from 'lucide-react'
import api from '@/lib/api'
import { isUuid } from '@/lib/security'

interface FileData { id: string; filename: string; minio_url: string; magnet_link: string; file_size_mb: number }

/**
 * Stream a webtorrent 2.x File progressively via MediaSource (pieces arrive
 * live from peers), falling back to a full-file blob once downloaded.
 *
 * webtorrent 2.x REMOVED `File.renderTo` (it was dropped from the core around
 * 2.0), so the old `f.renderTo(video, { autoplay: true })` call threw
 * `TypeError: f.renderTo is not a function` the moment a P2P swarm actually
 * connected — the P2P render path had never been reachable before (no trackers
 * in the magnet meant no peers, so every embed fell back to direct). MediaSource
 * is only used for codecs Chrome accepts in a SourceBuffer (WebM/VP8/Opus);
 * anything else (e.g. non-fragmented MP4) throws on the first append and the
 * caller falls back to direct origin streaming.
 */
async function streamP2pFile(file: any, video: HTMLVideoElement): Promise<void> {
  if (typeof MediaSource !== 'undefined' && file.type && MediaSource.isTypeSupported(file.type)) {
    const ms = new MediaSource()
    video.src = URL.createObjectURL(ms)
    await new Promise<void>((resolve, reject) => {
      ms.addEventListener('sourceopen', () => resolve(), { once: true })
      ms.addEventListener('error', () => reject(new Error('MediaSource error')), { once: true })
    })
    const sb = ms.addSourceBuffer(file.type)
    const reader = file.stream().getReader()
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      if (value) {
        if (sb.updating) await new Promise((r) => sb.addEventListener('updateend', r, { once: true }))
        sb.appendBuffer(value)
        if (video.paused) video.play().catch(() => {})
      }
    }
    if (ms.readyState === 'open') ms.endOfStream()
    return
  }
  // Codec not accepted by MediaSource — play once the whole file is downloaded.
  const blob = await file.blob()
  video.src = URL.createObjectURL(blob)
  await video.play().catch(() => {})
}

export default function VideoPlayer({ fileId, onPeers }: { fileId: string; onPeers?: (peers: number) => void }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [fileData, setFileData] = useState<FileData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [peers, setPeers] = useState(0)
  const [status, setStatus] = useState<'connecting' | 'p2p' | 'direct' | 'error'>('connecting')
  const statusRef = useRef<string>('connecting')
  const clientRef = useRef<any>(null)
  const fallbackTimer = useRef<NodeJS.Timeout | undefined>(undefined)
  const pollTimer = useRef<NodeJS.Timeout | undefined>(undefined)
  // Which source the <video> element is currently consuming: 'p2p' (MediaSource
  // blob) or 'direct' (the origin URL). Gates origin-byte billing — the
  // progress listener must only bill while the direct src is actually active.
  const activeSourceRef = useRef<'p2p' | 'direct'>('p2p')
  const originBillCleanupRef = useRef<(() => void) | null>(null)
  // Holds a final-flush callback for whatever billing path is active, so the
  // unmount cleanup never drops the last <1s of accumulated bytes (a page
  // closed mid-transfer used to silently lose pending P2P/origin bytes, which
  // distorted p2p_offload_percent).
  const finalFlushRef = useRef<(() => void) | null>(null)
  // Live peer-count callback for host pages (e.g. the /demo page's real-time
  // counter). Kept in a ref so the P2P effect's long-lived closures always
  // call the latest handler without forcing effect re-runs.
  const onPeersRef = useRef(onPeers)
  onPeersRef.current = onPeers

  useEffect(() => {
    if (fileId === 'demo') { setFileData({ id: 'demo', filename: 'Demo Video.mp4', minio_url: '', magnet_link: '', file_size_mb: 0 }); setLoading(false); return }
    // The file_id arrives from the embed URL (untrusted). Reject anything that
    // isn't a UUID before it becomes part of a request URL — blocks path
    // confusion like /embed/..%2F..%2Fadmin/stats (URL normalization would
    // otherwise redirect the fetch to a different same-origin route).
    if (!isUuid(fileId)) { setError('File not found'); setLoading(false); return }
    api.getPublicFile(fileId).then((data: FileData) => { setFileData(data); setLoading(false) }).catch(() => { setError('File not found'); setLoading(false) })
  }, [fileId])

  const fallbackToDirect = useCallback(() => {
    if (!videoRef.current || !fileData?.minio_url) return
    const video = videoRef.current
    video.src = fileData.minio_url; video.play().catch(() => {})
    setStatus('direct'); statusRef.current = 'direct'
    activeSourceRef.current = 'direct'
    // Bill origin bytes as they ACTUALLY download (progress event, throttled to
    // 1/sec), NOT the full file size at fallback time. P2P signaling + WebRTC
    // can take 5-15s before the first piece arrives, so the fallback usually
    // fires first — billing the whole file as origin then would dwarf the real
    // P2P bytes and zero out p2p_offload_percent even though P2P delivered the
    // video. With progress-based billing, origin only counts what origin really
    // served (≈0 when P2P takes over), so the offload metric stays honest.
    const totalBytes = Math.round((fileData.file_size_mb || 0) * 1024 * 1024)
    let billed = 0
    let lastBill = 0
    // Direct-path origin billing batches at the same 5s cadence as the P2P
    // path below — one POST per 5s per viewer, not per progress event.
    const DIRECT_BILL_MS = 5000
    const billOrigin = () => {
      if (activeSourceRef.current !== 'direct') return
      const now = Date.now()
      if (now - lastBill < DIRECT_BILL_MS) return
      lastBill = now
      let bufferedEnd = 0
      try {
        const b = video.buffered
        if (b && b.length) bufferedEnd = b.end(b.length - 1)
      } catch {}
      const dur = video.duration
      if (!(dur > 0) || !(bufferedEnd > 0) || totalBytes <= 0) return
      const bytes = Math.min(totalBytes, Math.round((bufferedEnd / dur) * totalBytes))
      if (bytes > billed) {
        api.trackEvent(fileData.id, 'chunk_downloaded', 0, bytes - billed).catch(() => {})
        billed = bytes
      }
    }
    video.addEventListener('progress', billOrigin)
    const cleanup = () => {
      video.removeEventListener('progress', billOrigin)
      // Final flush: bill whatever buffered progress we have at teardown.
      billOrigin()
    }
    originBillCleanupRef.current = cleanup
    finalFlushRef.current = billOrigin
  }, [fileData])

  useEffect(() => {
    if (!fileData || fileId === 'demo' || !fileData.magnet_link) return
    let mounted = true

    const init = async () => {
      try {
        const WebTorrent = (await import('webtorrent')).default
        if (!mounted) return
        const client = new WebTorrent(); clientRef.current = client
        setStatus('connecting'); statusRef.current = 'connecting'
        api.trackEvent(fileData.id, 'peer_joined').catch(() => {})

        fallbackTimer.current = setTimeout(() => {
          if (mounted && statusRef.current === 'connecting') fallbackToDirect()
        }, 5000)

        const torrent = client.add(fileData.magnet_link)
        // A connected wire means the swarm is alive — P2P is progressing (signaling +
        // WebRTC + metadata can take 5-15s before the first piece arrives), so give
        // it more time instead of letting the 5s fallback fire and bill the WHOLE
        // file as origin bytes. Only fall back when nothing has happened at all.
        torrent.on('wire', () => {
          if (!mounted) return
          setPeers(torrent.numPeers)
          onPeersRef.current?.(torrent.numPeers)
          if (statusRef.current === 'connecting') {
            clearTimeout(fallbackTimer.current)
            fallbackTimer.current = setTimeout(() => {
              if (mounted && statusRef.current === 'connecting') fallbackToDirect()
            }, 15000)
          }
        })
        // webtorrent's 'download' fires per 16KiB block received — dozens per
        // second on a fast P2P transfer. Batch chunk_downloaded analytics to
        // ONE POST per 5s (accumulated bytes). 5s (not 1s) because at 200k
        // concurrent viewers every request-class reduction multiplies out:
        // 5s batching turns 200k POSTs/min into 40k POSTs/min, comfortably
        // inside the per-IP/per-file limits while keeping the bandwidth chart
        // granularity the dashboard actually renders (the server buffers and
        // batch-inserts, so this cost is demand-side only).
        //
        // Webseed honesty: the magnet may carry a ws= webseed (origin backs up
        // thin swarms via HTTP Range fetches). Webseed bytes DO fire the
        // torrent-level 'download' event (all registered peers feed it), but
        // webseed peers NEVER fire torrent.on('wire') — only real P2P wires
        // do (torrent.js: _onWire emits 'wire'; addWebSeed registers via
        // _registerPeer directly). So the honest split is:
        //   bytes_p2p   = per-wire 'download' deltas (real peers only)
        //   bytes_origin = torrent 'download' delta − p2p delta (webseed bytes
        //                  served by origin — billed, never free)
        let pendingP2pBytes = 0   // from torrent.on('wire') download events only
        let pendingTotalBytes = 0 // from torrent.on('download') — ALL sources
        let lastAnalyticsFlush = 0
        const ANALYTICS_FLUSH_MS = 5000
        // Real P2P wires only: webseed peers never emit torrent.on('wire'), so
        // wire-level download bytes are exactly the peer-served bytes.
        torrent.on('wire', (wire: any) => {
          wire.on('download', (bytes: number) => {
            if (mounted) pendingP2pBytes += bytes
          })
        })
        const flushBytes = () => {
          const p2pDelta = pendingP2pBytes
          const totalDelta = pendingTotalBytes
          pendingP2pBytes = 0
          pendingTotalBytes = 0
          // Anything the torrent received that no real wire served came from
          // the webseed — i.e. from origin. Billed as origin bytes.
          const webseedDelta = Math.max(0, totalDelta - p2pDelta)
          if (p2pDelta > 0 || webseedDelta > 0) {
            api.trackEvent(fileData.id, 'chunk_downloaded', p2pDelta, webseedDelta).catch(() => {})
          }
        }
        torrent.on('download', (bytes: number) => {
          if (!mounted) return
          clearTimeout(fallbackTimer.current)
          setStatus('p2p'); statusRef.current = 'p2p'
          setPeers(torrent.numPeers)
          onPeersRef.current?.(torrent.numPeers)
          pendingTotalBytes += bytes
          const now = Date.now()
          if (now - lastAnalyticsFlush >= ANALYTICS_FLUSH_MS) {
            lastAnalyticsFlush = now
            flushBytes()
          }
        })
        finalFlushRef.current = flushBytes
        torrent.on('ready', () => {
          if (!mounted) return
          const f = torrent.files.find((f: any) => f.name.match(/\.(mp4|webm|ogv|mov|mkv|mp3|ogg|wav)$/i)) || torrent.files[0]
          if (!f || !videoRef.current) return
          const video = videoRef.current
          // The direct fallback may already be streaming the video (it fires at
          // 5s; signaling + WebRTC can take 5-15s). Don't yank a working stream
          // — that restarts playback for the viewer. The torrent keeps
          // downloading in the background, so bytes_p2p still accrues and the
          // offload split stays honest.
          const directAlreadyStreaming = activeSourceRef.current === 'direct' && video.readyState >= 2 && video.currentTime > 0.1
          setStatus('p2p'); statusRef.current = 'p2p'
          if (directAlreadyStreaming) return
          activeSourceRef.current = 'p2p'
          streamP2pFile(f, video).catch(() => { if (mounted) fallbackToDirect() })
        })
        pollTimer.current = setInterval(() => {
          if (!mounted) return
          const n = torrent.numPeers || peers
          setPeers(n)
          onPeersRef.current?.(n)
        }, 2000)
      } catch { if (mounted) fallbackToDirect() }
    }
    init()

    return () => {
      mounted = false
      clearTimeout(fallbackTimer.current)
      clearInterval(pollTimer.current)
      // Final flush before teardown so the last <1s of bytes aren't dropped.
      if (finalFlushRef.current) { try { finalFlushRef.current() } catch {} finalFlushRef.current = null }
      if (originBillCleanupRef.current) { originBillCleanupRef.current(); originBillCleanupRef.current = null }
      if (clientRef.current) {
        try { clientRef.current.destroy() } catch {}
        clientRef.current = null
      }
      if (fileData) api.trackEvent(fileData.id, 'peer_left').catch(() => {})
    }
  }, [fileData, fallbackToDirect, fileId])

  if (loading) return <div className="flex aspect-video items-center justify-center border" style={{ borderColor: '#222', backgroundColor: '#111' }}><Loader2 size={20} className="animate-spin" style={{ color: '#555' }} /></div>
  if (error || !fileData) return <div className="flex aspect-video items-center justify-center border" style={{ borderColor: '#222', backgroundColor: '#111' }}><p className="text-sm" style={{ color: '#555' }}>{error || 'Not found'}</p></div>

  if (fileId === 'demo') {
    return (
      <div className="border" style={{ borderColor: '#222', backgroundColor: '#111' }}>
        <div className="flex aspect-video items-center justify-center">
          <div className="text-center">
            <p className="text-sm font-medium text-white">Demo Player</p>
            <p className="mt-1 text-xs" style={{ color: '#555' }}>Upload a file to see the real P2P player</p>
          </div>
        </div>
        <div className="flex items-center justify-between border-t px-4 py-2" style={{ borderColor: '#222' }}>
          <span className="font-mono text-xs" style={{ color: '#555' }}>preview mode</span>
          <span className="flex items-center gap-1 font-mono text-xs" style={{ color: '#555' }}><Users size={12} /> 0 peers</span>
        </div>
      </div>
    )
  }

  return (
    <div className="border" style={{ borderColor: '#222', backgroundColor: '#111' }}>
      <div className="relative">
        <video ref={videoRef} className="aspect-video w-full" controls playsInline crossOrigin="anonymous" />
        {status === 'connecting' && (
          <div className="absolute inset-0 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.7)' }}>
            <p className="font-mono text-sm text-white">connecting to peers...</p>
          </div>
        )}
      </div>
      <div className="flex items-center justify-between border-t px-4 py-2" style={{ borderColor: '#222' }}>
        <span className="text-sm" style={{ color: '#555' }}>{fileData.filename}</span>
        <span className="flex items-center gap-1 font-mono text-sm" style={{ color: '#555' }}>
          <span className="h-2 w-2" style={{ backgroundColor: peers > 0 ? '#fff' : '#333' }} />
          {peers} {peers === 1 ? 'peer' : 'peers'} {status === 'p2p' ? 'connected' : status === 'direct' ? '(direct)' : ''}
        </span>
      </div>
    </div>
  )
}
