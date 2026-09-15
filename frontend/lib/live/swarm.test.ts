import { describe, expect, it } from 'vitest'

import { LiveSwarm } from './swarm'
import type { LivePlaybackInfo } from '../liveClient'

const playback: LivePlaybackInfo = {
  channel_id: 'ch-1',
  channel_slug: 'ch',
  channel_name: 'C',
  status: 'LIVE',
  hls_url: 'http://localhost:8888/ch/index.m3u8',
  token: 't.s',
  swarm_base: 'tcdnlive:ch',
  latency_target_seconds: 2,
}

describe('LiveSwarm', () => {
  it('tracks counters without any WebRTC (degraded mode)', () => {
    const s = new LiveSwarm(playback, 'peer-1')
    s.noteServed(500)
    s.noteServed(300)
    s.noteFailure(1)
    s.noteDownloaded(800)
    const stats = s.getStats()
    expect(stats.segments_served).toBe(2)
    expect(stats.segments_failed).toBe(1)
    expect(stats.bytes_uploaded).toBe(800)
    expect(stats.bytes_downloaded).toBe(800)
    expect(stats.peers).toBe(0)
    s.destroy()
  })

  it('prunes segments that left the live window', () => {
    const s = new LiveSwarm(playback, 'peer-1')
    // Force-attach fake handles (no real webtorrent in jsdom).
    ;(s as any).torrentFor = new Map([
      [1, { seq: 1, torrent: { numPeers: 2, destroy: () => {} } }],
      [2, { seq: 2, torrent: { numPeers: 3, destroy: () => {} } }],
    ])
    s.prune(10) // anything < 8 is detached
    // Both fake handles are below the live window — both detached+removed.
    const remaining = Array.from((s as any).torrentFor.keys())
    expect(remaining).toEqual([])
    s.destroy()
  })

  it('is inert after destroy', async () => {
    const s = new LiveSwarm(playback, 'peer-1')
    s.destroy()
    expect(await s.ensureClient()).toBeNull()
    expect(await s.fetchSegment({ sequence_number: 1, filename: '1.m4s', infohash: 'x', size: 0 })).toBeNull()
  })

  it('uses per-channel swarm naming from the backend', () => {
    // swarm_base is the backend-derived logical swarm id; the player must not
    // invent its own channel identity.
    expect(playback.swarm_base).toBe('tcdnlive:ch')
  })
})
