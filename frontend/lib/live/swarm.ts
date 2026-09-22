/**
 * Live P2P segment swarm — WebTorrent delivery for live HLS segments.
 *
 * ARCHITECTURE (mirrors the VOD model, adapted for live):
 *  - The BACKEND owns the infohash space: every live segment (channel, seq)
 *    maps to a deterministic single-file torrent whose infohash the backend
 *    computes the same way torrent_builder does for VOD (bencode
 *    {length, name, piece length, pieces}, SHA-1). Peers can therefore find
 *    each other through the SAME public WSS trackers VOD uses — no new
 *    discovery protocol.
 *  - The HLS playlist is NOT the P2P object. Playlists stay plain HTTP from
 *    the origin; only the .m4s segment BYTES travel over WebRTC.
 *  - Every viewer seeds segments it already buffered (serve-while-watch) and
 *    leeches segments it needs from other viewers first, falling back to the
 *    origin HTTP URL. Never P2P-only: a failed/slow transfer or an empty
 *    swarm falls back to origin inside the hls.js loader (lib/live/segments.ts).
 *
 * Live differs from VOD: segments exist for seconds. Clients fetch a
 * segment's infohash from the backend just-in-time (backend derives it from
 * the channel + sequence), seed while the segment is in the live window, and
 * drop everything when the segment expires.
 */
import type { LivePlaybackInfo } from '../liveClient'

// Trackers: MUST match the backend's torrent_builder.PUBLIC_TRACKERS (and the
// page CSP connect-src allow-list in middleware.ts) — a tracker not in both
// is silently unusable.
export const LIVE_TRACKERS = [
  'wss://tracker.webtorrent.dev',
  'wss://tracker.openwebtorrent.com',
  'wss://tracker.files.fm:7073/announce',
]

export interface SegmentMeta {
  sequence_number: number
  filename: string
  infohash: string
  size: number
}

export interface SwarmStats {
  peers: number
  bytes_uploaded: number
  bytes_downloaded: number
  segments_served: number
  segments_failed: number
}

interface TorrentHandle {
  seq: number
  torrent: any
  detachedAt?: number
}

const IDLE_DETACH_MS = 20_000 // stop seeding a segment 20s after the live window passes it

export class LiveSwarm {
  private client: any = null
  private torrentFor = new Map<number, TorrentHandle>()
  private playback: LivePlaybackInfo
  private sessionPeerId: string
  private stats: SwarmStats = { peers: 0, bytes_uploaded: 0, bytes_downloaded: 0, segments_served: 0, segments_failed: 0 }
  private destroyed = false

  constructor(playback: LivePlaybackInfo, sessionPeerId: string) {
    this.playback = playback
    this.sessionPeerId = sessionPeerId
  }

  /** Lazy webtorrent import (bundle stays out of the initial page load). */
  async ensureClient(): Promise<any | null> {
    if (this.destroyed) return null
    if (this.client) return this.client
    try {
      const WebTorrent = (await import('webtorrent')).default
      this.client = new WebTorrent()
      return this.client
    } catch {
      return null // P2P unavailable — the loader's origin fallback covers playback
    }
  }

  /**
   * Announce availability + seed a segment we already hold (fetched from
   * origin or a peer). `data` is the raw segment bytes.
   *
   * IDENTITY: create-torrent computes the infohash FROM the content — the
   * pinned `infoHash` option is not honored when seeding. Correctness
   * therefore comes from feeding the EXACT recipe the backend used:
   *   - name == backend's segment_filename (`<slug>-<seq>.m4s`)
   *   - pieceLength == torrentool's rule (262144 for >32KiB, else 32768)
   *   - bytes == the untouched origin segment
   * With those equal, the seeder's content-derived infohash is byte-identical
   * to the backend's hash, so leechers that join via the backend-published
   * identity land in THIS swarm.
   */
  async seedSegment(meta: SegmentMeta, data: ArrayBuffer): Promise<void> {
    const client = await this.ensureClient()
    if (!client || this.destroyed) return
    if (this.torrentFor.has(meta.sequence_number)) return
    try {
      const file = new File([data], meta.filename, { type: 'video/mp4' })
      const torrent = client.seed(file, {
        name: meta.filename,
        // torrentool's piece-length rule — the backend hashed with the same
        // (a mismatched piece size would fork the swarm: different info dict).
        pieceLength: meta.size > 32768 ? 262144 : 32768,
        announce: LIVE_TRACKERS,
      })
      this.torrentFor.set(meta.sequence_number, { seq: meta.sequence_number, torrent })
      const self = this
      torrent.on('wire', function wire(this: unknown, wire: any) {
        self.stats.peers = Math.max(self.stats.peers, torrent.numPeers || 0)
        wire.on('download', (n: number) => { self.stats.bytes_downloaded += n })
        wire.on('upload', (n: number) => {
          self.stats.bytes_uploaded += n
          self.stats.segments_served += 1
        })
      })
    } catch {
      // seeding is best-effort — playback never depends on it
    }
  }

  /**
   * Try to fetch a segment's bytes from the P2P swarm first.
   * Resolves with ArrayBuffer when a peer served it, or null when the swarm
   * can't (no peers / timeout) — the caller then falls back to origin.
   */
  async fetchSegment(meta: SegmentMeta, timeoutMs = 4000): Promise<ArrayBuffer | null> {
    const client = await this.ensureClient()
    if (!client || this.destroyed) return null
    // Already seeding/leeching this segment — read what the swarm has.
    const existing = this.torrentFor.get(meta.sequence_number)
    if (existing?.torrent) {
      const f = existing.torrent.files?.[0]
      if (f && existing.torrent.progress === 1) {
        try {
          return await withTimeout(f.arrayBuffer(), timeoutMs)
        } catch {
          this.stats.segments_failed += 1
          return null
        }
      }
      if (f) {
        // Torrent added but incomplete — wait for it within the budget.
        try {
          await withTimeout(
            new Promise<void>((resolve, reject) => {
              if (existing.torrent.progress === 1) return resolve()
              existing.torrent.on('done', resolve)
              existing.torrent.on('error', reject)
            }),
            timeoutMs
          )
          return await f.arrayBuffer()
        } catch {
          this.stats.segments_failed += 1
          return null
        }
      }
    }
    // Join the swarm by the backend-published identity — the ONLY way to
    // find peers before having the bytes (content-derived identity cannot be
    // computed pre-download). Without meta.infohash there is no swarm to join:
    // return null → the loader falls back to origin.
    if (!meta.infohash) return null
    try {
      const torrent = await withTimeout(
        new Promise<any>((resolve, reject) => {
          const t = client.add({ infoHash: meta.infohash, announce: LIVE_TRACKERS, name: meta.filename })
          const onReady = () => resolve(t)
          const onErr = (e: unknown) => reject(e)
          t.once('ready', onReady)
          t.once('error', onErr)
        }),
        timeoutMs
      )
      const wireStats = (wire: any) => {
        wire.on('download', (n: number) => { this.stats.bytes_downloaded += n })
        wire.on('upload', (n: number) => { this.stats.bytes_uploaded += n })
      }
      torrent.on('wire', wireStats)
      const file = torrent.files?.[0]
      if (!file) {
        torrent.destroy({ destroyStore: true })
        this.stats.segments_failed += 1
        return null
      }
      // Metadata received — the download itself shares the remaining budget.
      const data = (await withTimeout(file.arrayBuffer(), timeoutMs)) as ArrayBuffer
      this.torrentFor.set(meta.sequence_number, { seq: meta.sequence_number, torrent })
      // Keep the torrent alive: we are now a seed for late joiners.
      return data
    } catch {
      // Swarm didn't form in time — tear down the partial torrent so the
      // client stops announcing, and let the loader fall back to origin.
      try {
        const t = this.torrentFor.get(meta.sequence_number)?.torrent
        if (!t) {
          // client.add may still be pending inside the client; remove it.
          const stray = client.torrents?.find((x: any) => x.infoHash === meta.infohash)
          if (stray) stray.destroy({ destroyStore: true })
        }
      } catch { /* already gone */ }
      this.stats.segments_failed += 1
      return null
    }
  }

  /** Register a failed P2P fetch so the backend scrubs the dead peer. */
  noteFailure(seq: number): void {
    this.stats.segments_failed += 1
  }

  noteServed(bytes: number): void {
    this.stats.segments_served += 1
    this.stats.bytes_uploaded += bytes
  }

  noteDownloaded(bytes: number): void {
    this.stats.bytes_downloaded += bytes
  }

  getStats(): SwarmStats {
    // Live peer count from every active torrent
    let peers = 0
    for (const t of Array.from(this.torrentFor.values())) {
      if (!t.detachedAt) peers = Math.max(peers, t.torrent?.numPeers || 0)
    }
    this.stats.peers = peers
    return { ...this.stats }
  }

  /** Stop tracking segments that left the live window (release memory). */
  prune(currentSeq: number): void {
    for (const [seq, handle] of Array.from(this.torrentFor.entries())) {
      if (seq < currentSeq - 2 && !handle.detachedAt) {
        handle.detachedAt = Date.now()
        try {
          handle.torrent.destroy({ destroyStore: true })
        } catch {
          /* already gone */
        }
        this.torrentFor.delete(seq)
      }
    }
  }

  destroy(): void {
    this.destroyed = true
    for (const h of Array.from(this.torrentFor.values())) {
      try {
        h.torrent.destroy({ destroyStore: true })
      } catch { /* noop */ }
    }
    this.torrentFor.clear()
    if (this.client) {
      try {
        this.client.destroy()
      } catch { /* noop */ }
      this.client = null
    }
  }
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms)
    p.then(
      (v) => { clearTimeout(timer); resolve(v) },
      (e) => { clearTimeout(timer); reject(e) }
    )
  })
}
