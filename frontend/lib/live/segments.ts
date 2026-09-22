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

  /** Highest sequence observed from any playlist (the client's live edge). */
  latestObserved(): number | null {
    let latest: number | null = null
    for (const seq of Array.from(this.availability.keys())) {
      if (latest === null || seq > latest) latest = seq
    }
    return latest
  }

  /** Sequences this client has actually fetched (locally held → shareable). */
  observedSequences(): number[] {
    // Only segments with a recorded fetch outcome were truly downloaded by
    // this peer — claiming un-fetched playlist entries would offer peers
    // data we don't have (a transfer failure every time).
    const held = Array.from(this.outcomes.entries())
      .filter(([, o]) => o.served)
      .map(([seq]) => seq)
    return held.sort((a, b) => a - b).slice(-50)
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
    const haves = this.observedSequences()
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
  // LL-HLS parts (MediaMTX: `<hash>_video1_part<k>.mp4`) are PARTIAL bytes —
  // they are control-plane data like playlists: straight to origin, never a
  // swarm object, never an availability `have`.
  if (/_part\d+\.(?:m4s|ts|mp4)$/i.test(clean)) return null
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
  /** Called after an origin fetch with the segment bytes, so the caller can
   * seed them into the swarm (serve-while-watch). Best-effort. */
  onSeed?: (meta: SegmentMeta, data: ArrayBuffer) => void
}

/**
 * Deterministic live-segment swarm identity — the same (slug, seq, bytes)
 * must map to the SAME infohash on every viewer, or peers can never find
 * each other.
 *
 * Mirrors the VOD swarm's identity model: a single-file torrent whose info
 * dict is bencoded in torrentool's key order (length, name, piece length,
 * pieces) and SHA-1'd. Segment identity is CONTENT-derived — piece hashes
 * come from the origin bytes every viewer downloads anyway — so a peer can
 * only join the correct swarm by holding the correct bytes: a malicious peer
 * serving rehashed garbage simply isn't discoverable in the real swarm.
 * (Live keeps the VOD piece-length rule: ≤32KiB → 32768, else 262144; a 2s
 * segment is typically ~250–500KB → 262144.)
 */
export async function liveSegmentInfohash(slug: string, sequence: number, data: ArrayBuffer): Promise<string> {
  const bytes = new Uint8Array(data)
  const pieceLength = bytes.byteLength > 32768 ? 262144 : 32768
  const pieceDigests: Uint8Array[] = []
  for (let pos = 0; pos < bytes.byteLength; pos += pieceLength) {
    pieceDigests.push(await sha1Digest(bytes.subarray(pos, Math.min(pos + pieceLength, bytes.byteLength))))
  }
  const piecesB = concatBytes(...pieceDigests)
  const name = `${slug}-${sequence}.m4s`
  const nameB = utf8Bytes(name)
  const info = concatBytes(
    utf8Bytes(`d6:lengthi${bytes.byteLength}e4:name${nameB.length}:`),
    nameB,
    utf8Bytes(`12:piece lengthi${pieceLength}e6:pieces${piecesB.length}:`),
    piecesB,
    utf8Bytes('e')
  )
  return hex(await sha1Digest(info))
}

function utf8Bytes(s: string): Uint8Array {
  return new TextEncoder().encode(s)
}

function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((n, p) => n + p.length, 0)
  const out = new Uint8Array(total)
  let off = 0
  for (const p of parts) {
    out.set(p, off)
    off += p.length
  }
  return out
}

/** SHA-1 via WebCrypto (available in all browsers and Node ≥15). */
async function sha1Digest(data: Uint8Array): Promise<Uint8Array> {
  const d = await crypto.subtle.digest('SHA-1', data as unknown as ArrayBuffer)
  return new Uint8Array(d)
}

function hex(bytes: Uint8Array): string {
  let s = ''
  for (let i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2, '0')
  return s
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
  // Serve-while-watch: offer the freshly fetched bytes to the swarm. Pinned
  // infohash identity (slug+seq+content) means every viewer computing it from
  // the SAME origin bytes joins ONE swarm per segment. Best-effort and
  // fire-and-forget — playback never waits on seeding, and a seeding failure
  // cannot affect the already-complete origin fetch.
  if (seq !== null && data.byteLength > 0 && deps.onSeed) {
    try {
      const infohash = await liveSegmentInfohash(deps.playback.channel_slug, seq, data)
      const meta: SegmentMeta = {
        sequence_number: seq,
        filename: `${deps.playback.channel_slug}-${seq}.m4s`,
        infohash,
        size: data.byteLength,
      }
      deps.onSeed(meta, data)
    } catch {
      /* identity computation is best-effort */
    }
  }
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
