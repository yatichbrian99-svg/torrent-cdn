/**
 * Live segment lifecycle + hls.js loader glue.
 *
 * Owns the client-side live-edge contract:
 *  - which segments exist (playlist observation + backend registry),
 *  - which peers hold them (availability reports to the backend),
 *  - the fetch order: healthy peer → another peer → ORIGIN (never P2P-only),
 *  - expiry: segments that leave the live window are dropped, never
 *    accumulated.
 *
 * hls.js is a peer dependency loaded dynamically; the loader hooks
 * (fLoad via xhrSetup) route segment requests through our pipeline so the
 * player transparently benefits from P2P while keeping hls.js's ABR, live
 * edge, and error-recovery behavior intact.
 */
import type { LivePlaybackInfo } from '../liveClient'
import type { SegmentMeta, SwarmStats } from './swarm'

export interface SegmentWindow {
  slug: string
  latest_sequence: number | null
  /** Sequence numbers currently inside the live window (bounded). */
  sequences: number[]
}

const MAX_TRACKED_SEGMENTS = 30 // ~1 minute at 2s segments — live only

export class LiveSegmentRegistry {
  /** seq → peers that reported having it (backend + local swarm). */
  private availability = new Map<number, Set<string>>()
  /** seq → fetch outcome counters (measured, never modeled). */
  private outcomes = new Map<number, { served: boolean; bytes: number }>()
  private slug: string

  constructor(slug: string) {
    this.slug = slug
  }

  observe(seq: number): void {
    // Playlist observation: a segment number seen in the playlist becomes tracked.
    if (!this.availability.has(seq)) this.availability.set(seq, new Set())
    if (this.availability.size > MAX_TRACKED_SEGMENTS) {
      const oldest = Math.min(...Array.from(this.availability.keys()))
      this.availability.delete(oldest)
      this.outcomes.delete(oldest)
    }
  }

  markPeerHas(seq: number, peerId: string): void {
    let set = this.availability.get(seq)
    if (!set) {
      set = new Set()
      this.availability.set(seq, set)
    }
    set.add(peerId)
  }

  markLocalHas(seq: number): void {
    this.outcomes.set(seq, { served: true, bytes: 0 })
  }

  markTransfer(seq: number, served: boolean, bytes: number): void {
    this.outcomes.set(seq, { served, bytes })
  }

  peersFor(seq: number): string[] {
    return Array.from(this.availability.get(seq) || [])
  }

  /** Expire every segment older than keepSeq (leaves the live window). */
  expireBefore(keepSeq: number): number {
    let n = 0
    for (const seq of Array.from(this.availability.keys())) {
      if (seq < keepSeq) {
        this.availability.delete(seq)
        n++
      }
    }
    for (const seq of Array.from(this.outcomes.keys())) {
      if (seq < keepSeq) this.outcomes.delete(seq)
    }
    return n
  }

  /** Local availability report payload for the backend. */
  reportPayload(sessionId: string, peerId: string): {
    session_id: string
    peer_id: string
    haves: number[]
    transfers: { sequence_number: number; served: boolean; bytes: number }[]
  } {
    const haves = Array.from(this.availability.keys()).slice(-50)
    const transfers = Array.from(this.outcomes.entries())
      .slice(-50)
      .map(([sequence_number, o]) => ({ sequence_number, served: o.served, bytes: o.bytes }))
    return { session_id: sessionId, peer_id: peerId, haves, transfers }
  }

  stats(): { tracked: number; served: number; failed: number } {
    let served = 0
    let failed = 0
    for (const o of Array.from(this.outcomes.values())) {
      if (o.served) served++
      else failed++
    }
    return { tracked: this.availability.size, served, failed }
  }
}

// hls.js loader wiring ---------------------------------------------------------
// We patch Hls.DefaultConfig.loader ONCE with a custom loader that:
//   1. lets playlist requests go straight to the network (HTTP, never P2P),
//   2. routes segment requests through LiveSegmentPipeline (P2P first, then
//      origin fetch with Range support via standard XHR).

export interface LoaderCallbacks {
  onSuccess: (response: { url: string; data: ArrayBuffer }, stats: any, context: any, networkDetails: any) => void
  onError: (error: { code: number; text: string }, context: any, networkDetails: any, stats: any) => void
  onTimeout?: (stats: any, context: any, networkDetails: any) => void
  onProgress?: (stats: any, context: any, data: any, networkDetails: any) => void
}

export interface LoaderContext {
  url: string
  rangeStart?: number
  rangeEnd?: number
  type: string // 'manifest' | 'segment' | ...
  frag?: { sn?: number | 'initSegment'; relurl?: string }
  [k: string]: any
}

/**
 * The P2P pipeline: given a segment URL (origin) and the channel slug, extract
 * the sequence number from the filename and try the swarm first.
 */
export function seqFromUrl(url: string): number | null {
  // Strip the query string first — tokens/cache-busters must not hide the
  // sequence number from the P2P registry.
  const clean = url.split('?')[0]
  const m = /(\d+)\.(m4s|ts|mp4)$/i.exec(clean)
  return m ? parseInt(m[1], 10) : null
}

export interface SegmentPipelineDeps {
  playback: LivePlaybackInfo
  tryPeer: (meta: SegmentMeta) => Promise<ArrayBuffer | null>
  onOutcome: (seq: number, served: boolean, bytes: number) => void
  onOriginFetch: (bytes: number) => void
  onPeerFetch: (bytes: number) => void
  getSessionPeerId: () => string
}

/**
 * Try P2P for a segment; fall back to origin XHR. NEVER P2P-only.
 */
export async function fetchSegmentWithFallback(
  deps: SegmentPipelineDeps,
  url: string,
  range?: { start: number; end: number }
): Promise<{ data: ArrayBuffer; via: 'p2p' | 'origin' }> {
  const seq = seqFromUrl(url)
  let failedP2P = false
  if (seq !== null) {
    try {
      const meta: SegmentMeta = {
        sequence_number: seq,
        filename: url.split('/').pop() || '',
        infohash: '',
        size: 0,
      }
      const data = await deps.tryPeer(meta)
      if (data && data.byteLength > 0) {
        deps.onOutcome(seq, true, data.byteLength)
        deps.onPeerFetch(data.byteLength)
        return { data, via: 'p2p' }
      }
      failedP2P = true
      deps.onOutcome(seq, false, 0)
    } catch {
      failedP2P = true
      deps.onOutcome(seq, false, 0)
    }
  }
  // Origin fallback: plain XHR with optional Range (CORS handled at the proxy).
  const data = await originXhr(url, range)
  deps.onOriginFetch(data.byteLength)
  // Origin served the bytes — record success ONLY for the origin path; the
  // failed P2P attempt above was already reported as failed.
  if (seq !== null && !failedP2P) deps.onOutcome(seq, true, data.byteLength)
  return { data, via: 'origin' }
}

function originXhr(url: string, range?: { start: number; end: number }): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('GET', url, true)
    xhr.responseType = 'arraybuffer'
    if (range) xhr.setRequestHeader('Range', `bytes=${range.start}-${range.end}`)
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response as ArrayBuffer)
      else reject(new Error(`origin ${xhr.status}`))
    }
    xhr.onerror = () => reject(new Error('origin network error'))
    xhr.ontimeout = () => reject(new Error('origin timeout'))
    xhr.timeout = 15000
    xhr.send()
  })
}
