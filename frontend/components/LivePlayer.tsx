'use client'

/**
 * LivePlayer — production browser live player.
 *
 * Flow (spec §22): playback info → join swarm → hls.js plays the HLS playlist
 * with a custom loader that prefers peer-served segments and falls back to
 * origin automatically → heartbeats + availability reports every 5s → clean
 * leave on unmount.
 *
 * hls.js is dynamically imported (kept out of the initial bundle). The custom
 * loader only touches SEGMENT requests: playlists always go straight to the
 * network (control plane stays HTTP — spec §3). All playback logic (live
 * edge, ABR, stalls, recovery) stays in hls.js.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, Radio, Users, Volume2, VolumeX, Maximize, AlertTriangle } from 'lucide-react'
import {
  liveViewerApi,
  statusColor,
  type LivePlaybackInfo,
} from '@/lib/liveClient'
import { LiveSwarm } from '@/lib/live/swarm'
import {
  LiveSegmentRegistry,
  fetchSegmentWithFallback,
  seqFromUrl,
  type LoaderContext,
} from '@/lib/live/segments'

interface Props {
  playback: LivePlaybackInfo
  onPeersChange?: (n: number) => void
  className?: string
}

const HEARTBEAT_MS = 5000

export default function LivePlayer({ playback, onPeersChange, className }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const hlsRef = useRef<any>(null)
  const swarmRef = useRef<LiveSwarm | null>(null)
  const registryRef = useRef<LiveSegmentRegistry | null>(null)
  const sessionRef = useRef<{ sessionId: string; peerId: string } | null>(null)
  const joinedRef = useRef(false)
  const countersRef = useRef({
    bytes_uploaded: 0, bytes_downloaded: 0, segments_served: 0,
    segments_failed: 0, buffering_events: 0, playback_errors: 0,
  })
  const pendingTransfersRef = useRef<{ sequence_number: number; served: boolean; bytes: number }[]>([])
  const startupRef = useRef<number>(0)

  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [errorMsg, setErrorMsg] = useState('')
  const [live, setLive] = useState(false)
  const [peers, setPeers] = useState(0)
  const [buffering, setBuffering] = useState(false)
  const [muted, setMuted] = useState(true)
  const [paused, setPaused] = useState(false)
  const [atLiveEdge, setAtLiveEdge] = useState(true)

  const sessionId = useRef<string>(
    typeof crypto !== 'undefined' && crypto.randomUUID
      ? `v-${crypto.randomUUID().slice(0, 18)}`
      : `v-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  )

  // ── telemetry loop (heartbeats + availability) ─────────────────────────────
  useEffect(() => {
    if (!playback) return
    const peerId = sessionRef.current?.peerId || sessionId.current
    const timer = setInterval(async () => {
      if (!joinedRef.current) return
      const c = countersRef.current
      const transfers = pendingTransfersRef.current.splice(0, 20)
      // P2P outcomes (segments_served/failed + bytes_p2p) are reported by the
      // RECEIVING peer in the availability report below — one measurement, one
      // counter. Sending serving-side wire bytes here as well would double-
      // count every transfer in the offload metric.
      try {
        await liveViewerApi.heartbeat(playback, {
          session_id: sessionId.current,
          peer_id: peerId,
          bytes_downloaded: c.bytes_downloaded, // origin-fetch bytes (→ bytes_origin)
          buffering_events: c.buffering_events,
          playback_errors: c.playback_errors,
          current_segment: registryRef.current ? lastObservedSegment() : null,
        })
        // reset the deltas after reporting
        countersRef.current = { bytes_uploaded: 0, bytes_downloaded: 0, segments_served: 0, segments_failed: 0, buffering_events: 0, playback_errors: 0 }
        if (registryRef.current && transfers.length) {
          await liveViewerApi.reportAvailability(playback, {
            session_id: sessionId.current,
            peer_id: peerId,
            // Real observed playlist sequences (what THIS peer actually holds
            // locally after fetching them) — the old constant [] never
            // registered availability, so the swarm had no peers to match.
            haves: (registryRef.current as any).observedSequences?.() ?? [],
            transfers,
          })
        }
      } catch { /* telemetry is best-effort */ }
    }, HEARTBEAT_MS)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playback])

  const lastObservedSegment = (): number | null => {
    // The LATEST SEGMENT SEQUENCE observed from the playlist (registry state),
    // not a playback timestamp: the backend stores this as an availability
    // have and serves it to peers as a segment number — a wall-clock position
    // here registered bogus sequence numbers into the swarm.
    const hls = hlsRef.current
    if (!hls) return null
    const details = hls.levels?.[hls.currentLevel]?.details || hls.levels?.[0]?.details
    const sn = details?.fragments?.length ? details.fragments[details.fragments.length - 1].sn : null
    if (typeof sn === 'number' && Number.isFinite(sn)) return sn
    return registryRef.current ? (registryRef.current as any).latestObserved?.() ?? null : null
  }

  // ── swarm join/leave ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!playback) return
    let cancelled = false
    const swarm = new LiveSwarm(playback, sessionId.current)
    const registry = new LiveSegmentRegistry(playback.channel_slug)
    swarmRef.current = swarm
    registryRef.current = registry
    sessionRef.current = { sessionId: sessionId.current, peerId: sessionId.current }

    ;(async () => {
      try {
        await liveViewerApi.joinSession(playback, sessionId.current)
        joinedRef.current = true
        startupRef.current = Date.now()
      } catch { /* server unreachable — hls.js still plays from origin */ }
    })()

    return () => {
      cancelled = true
      joinedRef.current = false
      if (sessionRef.current) {
        liveViewerApi.leave(playback, sessionId.current, sessionRef.current.peerId)
      }
      swarm.destroy()
      swarmRef.current = null
      registryRef.current = null
    }
  }, [playback])

  // ── hls.js wiring ───────────────────────────────────────────────────────────
  const startPlayback = useCallback(async () => {
    const video = videoRef.current
    if (!video || hlsRef.current) return
    const Hls = (await import('hls.js/dist/hls.light.min.js' as any)).default ?? (await import('hls.js'))
    if (cancelledStartRef.current) return

    const registry = registryRef.current!
    const swarm = swarmRef.current!

    const customLoader = function (this: any) {
      const origLoad = (url: string, ...rest: any[]) => {}
      return {
        load(context: LoaderContext, config: any, callbacks: any) {
          const isSegment = (context.type === 'segment' || context.type === 'fragment') && !!seqFromUrl(context.url)
          if (!isSegment) {
            // Playlists + keys + init segments: straight to network (control plane).
            const xhr = new XMLHttpRequest()
            xhr.open('GET', context.url, true)
            xhr.responseType = 'arraybuffer'
            if (context.rangeStart !== undefined && context.rangeEnd !== undefined) {
              xhr.setRequestHeader('Range', `bytes=${context.rangeStart}-${context.rangeEnd}`)
            }
            xhr.timeout = config?.timeout || 20000
            xhr.onload = () => {
              if (xhr.status >= 200 && xhr.status < 300) {
                callbacks.onSuccess({ url: context.url, data: xhr.response }, { total: xhr.response?.byteLength || 0, loaded: xhr.response?.byteLength || 0 }, context, xhr)
              } else {
                callbacks.onError({ code: xhr.status, text: xhr.statusText }, context, xhr, null)
              }
            }
            xhr.onerror = () => callbacks.onError({ code: -1, text: 'network' }, context, xhr, null)
            xhr.ontimeout = () => callbacks.onTimeout?.({ total: 0, loaded: 0 }, context, xhr)
            xhr.onprogress = (e) => callbacks.onProgress?.({ total: e.total, loaded: e.loaded }, context, null, xhr)
            xhr.send()
            return
          }
          // Segment: P2P first, origin fallback — measured, never modeled.
          const t0 = performance.now()
          fetchSegmentWithFallback(
            {
              playback,
              tryPeer: async (meta) => {
                // Join the segment's swarm via the backend-published identity.
                // The first viewer for a segment gets null here (no peers have
                // it yet) and fetches from origin — then SEEDS it below, so
                // every subsequent viewer can take the P2P path.
                const identity = await liveViewerApi.segmentIdentity(playback, meta.sequence_number)
                if (!identity) return null
                return await swarm.fetchSegment(
                  { ...meta, infohash: identity.infohash, filename: identity.filename, size: identity.size },
                  2500
                )
              },
              onSeed: (meta, data) => {
                // Serve-while-watch: the origin bytes we just fetched make us
                // the swarm's first seed. Fire-and-forget — playback already
                // has the bytes; seeding is purely for other viewers.
                void swarm.seedSegment(meta, data)
              },
              onOutcome: (seq, served, bytes) => {
                pendingTransfersRef.current.push({ sequence_number: seq, served, bytes })
                if (served && bytes > 0) swarm.noteServed(bytes)
                else if (!served) swarm.noteFailure(seq)
              },
              onOriginFetch: (bytes) => {
                countersRef.current.bytes_downloaded += bytes
                swarm.noteDownloaded(bytes)
              },
              onPeerFetch: (bytes) => {
                // P2P-delivered bytes are counted on the SERVING peer's wire
                // uploader (heartbeats read the swarm's cumulative upload
                // counter) — counting them here too would double-report.
                void bytes
              },
              getSessionPeerId: () => sessionRef.current?.peerId || sessionId.current,
            },
            context.url,
            context.rangeStart !== undefined && context.rangeEnd !== undefined
              ? { start: context.rangeStart, end: context.rangeEnd }
              : undefined
          )
            .then(({ data }) => {
              callbacks.onSuccess({ url: context.url, data }, { total: data.byteLength, loaded: data.byteLength }, context, null)
            })
            .catch((e) => {
              countersRef.current.playback_errors += 1
              callbacks.onError({ code: -2, text: String(e).slice(0, 80) }, context, null, null)
            })
          void origLoad
        },
        abort() {},
        destroy() {},
      }
    }

    const hls = new Hls({
      lowLatencyMode: true,
      liveSyncDurationCount: 3,
      backBufferLength: 15,
      maxBufferLength: 8,
      enableWorker: true,
      // Route segment fetches through the P2P pipeline.
      loader: customLoader as any,
      xhrSetup: undefined,
    })
    hlsRef.current = hls

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      setPhase('ready')
      video.play().catch(() => {})
    })
    hls.on(Hls.Events.LEVEL_LOADED, (_e: any, data: any) => {
      setLive(!!data.details?.live)
    })
    hls.on(Hls.Events.FRAG_LOADED, (_e: any, data: any) => {
      const seq = seqFromUrl(data.frag?.url || '')
      if (seq !== null) registry.observe(seq)
      swarm.prune(seq ?? 0)
    })
    hls.on(Hls.Events.ERROR, (_e: any, data: any) => {
      if (data.fatal) {
        countersRef.current.playback_errors += 1
        switch (data.type) {
          case Hls.ErrorTypes.NETWORK_ERROR:
            // Origin hiccup / playlist gap — retry, do not kill playback.
            hls.startLoad()
            break
          case Hls.ErrorTypes.MEDIA_ERROR:
            hls.recoverMediaError()
            break
          default:
            setPhase('error')
            setErrorMsg('Live stream unavailable — reconnecting…')
            setTimeout(() => { hlsRef.current?.destroy(); hlsRef.current = null; startPlayback() }, 3000)
        }
      }
    })
    video.addEventListener('waiting', () => {
      setBuffering(true)
      countersRef.current.buffering_events += 1
    })
    video.addEventListener('playing', () => setBuffering(false))
    video.addEventListener('pause', () => setPaused(true))
    video.addEventListener('play', () => setPaused(false))
    const poll = setInterval(() => {
      const v = videoRef.current
      if (!v) return
      const stats = swarmRef.current?.getStats()
      if (stats) setPeers(stats.peers)
      onPeersChange?.(peers)
      // live-edge drift guard: if the viewer falls >15s behind, snap back
      if (hlsRef.current?.liveSyncPosition && v.duration) {
        const target = hlsRef.current.liveSyncPosition
        if (Number.isFinite(target) && target - v.currentTime > 15) {
          v.currentTime = target
          setAtLiveEdge(true)
        } else {
          setAtLiveEdge(Math.abs(target - v.currentTime) < 8)
        }
      }
    }, 2000)

    hls.loadSource(playback.hls_url)
    hls.attachMedia(video)
    void poll
  }, [playback, onPeersChange, peers])

  const cancelledStartRef = useRef(false)
  useEffect(() => {
    cancelledStartRef.current = false
    startPlayback()
    return () => {
      cancelledStartRef.current = true
      if (hlsRef.current) {
        try { hlsRef.current.destroy() } catch { /* noop */ }
        hlsRef.current = null
      }
    }
  }, [startPlayback])

  const toggleFullscreen = () => {
    const el = containerRef.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else void el.requestFullscreen?.()
  }

  const toggleMute = () => {
    const v = videoRef.current
    if (!v) return
    v.muted = !v.muted
    setMuted(v.muted)
  }

  const backToLive = () => {
    const v = videoRef.current
    const hls = hlsRef.current
    if (v && hls && Number.isFinite(hls.liveSyncPosition)) {
      v.currentTime = hls.liveSyncPosition
      void v.play()
    }
  }

  if (phase === 'error' && !videoRef.current) {
    return (
      <div className={`flex aspect-video items-center justify-center border ${className || ''}`} style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
        <div className="text-center">
          <AlertTriangle size={20} className="mx-auto mb-2" style={{ color: '#ef4444' }} />
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>{errorMsg || 'Live stream unavailable'}</p>
        </div>
      </div>
    )
  }

  return (
    <div ref={containerRef} className={`relative border ${className || ''}`} style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
      <video
        ref={videoRef}
        className="aspect-video w-full"
        playsInline
        autoPlay
        muted={muted}
        controls={false}
      />
      {phase === 'loading' && (
        <div className="absolute inset-0 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.7)' }}>
          <Loader2 size={22} className="animate-spin" style={{ color: 'var(--text-dim)' }} />
        </div>
      )}
      {buffering && phase === 'ready' && (
        <div className="absolute inset-0 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.4)' }}>
          <Loader2 size={22} className="animate-spin" style={{ color: 'var(--text-soft)' }} />
        </div>
      )}
      {/* LIVE badge */}
      <div className="absolute left-3 top-3 flex items-center gap-2">
        <span className="flex items-center gap-1.5 border px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest"
              style={{ borderColor: live ? 'var(--ok-chip-border)' : 'var(--border)', backgroundColor: live ? 'var(--ok-chip-bg)' : 'var(--bg-secondary)', color: statusColor(live ? 'LIVE' : 'OFFLINE') }}>
          <Radio size={10} /> {live ? 'LIVE' : 'OFFLINE'}
        </span>
        {!atLiveEdge && (
          <button onClick={backToLive}
                  className="border px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest"
                  style={{ borderColor: 'var(--border-strong)', color: '#fbbf24', backgroundColor: 'var(--bg-secondary)' }}>
            go to live edge →
          </button>
        )}
      </div>
      {/* Custom controls: play/pause, mute, fullscreen + peer counter */}
      <div className="absolute bottom-0 left-0 right-0 flex items-center justify-between border-t px-3 py-2"
           style={{ borderColor: 'var(--border)', backgroundColor: 'rgba(0,0,0,0.85)' }}>
        <div className="flex items-center gap-3">
          <button onClick={() => { const v = videoRef.current; if (v) (paused ? v.play() : v.pause()) }}
                  className="text-sm text-[color:var(--text-primary)] transition-opacity hover:opacity-80">
            {paused ? '▶' : '❚❚'}
          </button>
          <button onClick={toggleMute} className="transition-opacity hover:opacity-80" style={{ color: 'var(--text-muted)' }} aria-label={muted ? 'Unmute' : 'Mute'}>
            {muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
          </button>
        </div>
        <div className="flex items-center gap-3 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
          <span className="flex items-center gap-1">
            <span className="h-2 w-2" style={{ backgroundColor: peers > 0 ? '#4ade80' : 'var(--border-strong)' }} />
            <Users size={11} /> {peers} {peers === 1 ? 'peer' : 'peers'}
          </span>
          <button onClick={toggleFullscreen} className="transition-opacity hover:opacity-80" aria-label="Fullscreen">
            <Maximize size={13} />
          </button>
        </div>
      </div>
    </div>
  )
}
