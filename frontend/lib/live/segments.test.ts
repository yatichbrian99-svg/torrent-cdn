import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

import {
  LiveSegmentRegistry,
  fetchSegmentWithFallback,
  seqFromUrl,
} from './segments'

describe('seqFromUrl', () => {
  it('extracts the sequence number from segment URLs', () => {
    expect(seqFromUrl('https://x/hls/ch/segment001.m4s')).toBe(1)
    expect(seqFromUrl('https://x/hls/ch/segment001.m4s?token=a')).toBe(1)
    expect(seqFromUrl('http://localhost:8888/ch/12345.ts')).toBe(12345)
    expect(seqFromUrl('https://x/hls/ch/index.m3u8')).toBeNull()
    expect(seqFromUrl('https://x/hls/ch/init.mp4')).toBeNull()
  })
})

describe('LiveSegmentRegistry', () => {
  it('observes playlist segments and bounds the live window', () => {
    const r = new LiveSegmentRegistry('ch')
    for (let i = 0; i < 40; i++) r.observe(i)
    // MAX_TRACKED_SEGMENTS = 30 — the oldest are expired automatically.
    expect(r.stats().tracked).toBeLessThanOrEqual(30)
    expect(r.peersFor(0)).toEqual([]) // oldest dropped
    expect(r.peersFor(39)).toEqual([])
  })

  it('tracks peer availability per segment', () => {
    const r = new LiveSegmentRegistry('ch')
    r.observe(5)
    r.markPeerHas(5, 'peerA')
    r.markPeerHas(5, 'peerB')
    r.markPeerHas(6, 'peerA')
    expect(r.peersFor(5)).toEqual(['peerA', 'peerB'])
    expect(r.peersFor(6)).toEqual(['peerA'])
    expect(r.peersFor(99)).toEqual([])
  })

  it('expires segments that leave the live window', () => {
    const r = new LiveSegmentRegistry('ch')
    for (let i = 0; i < 10; i++) {
      r.observe(i)
      r.markPeerHas(i, 'peerA')
    }
    const n = r.expireBefore(7)
    expect(n).toBe(7)
    expect(r.peersFor(3)).toEqual([])
    expect(r.peersFor(8)).toEqual(['peerA'])
  })

  it('builds the backend report payload with haves + transfers', () => {
    const r = new LiveSegmentRegistry('ch')
    for (let i = 0; i < 5; i++) r.observe(i)
    r.markTransfer(3, true, 250_000)
    r.markTransfer(4, false, 0)
    const payload = r.reportPayload('sess-1', 'peer-1')
    expect(payload.session_id).toBe('sess-1')
    expect(payload.peer_id).toBe('peer-1')
    expect(payload.haves).toEqual([0, 1, 2, 3, 4])
    expect(payload.transfers).toContainEqual({ sequence_number: 3, served: true, bytes: 250_000 })
    expect(payload.transfers).toContainEqual({ sequence_number: 4, served: false, bytes: 0 })
  })

  it('counts measured outcomes (served vs failed)', () => {
    const r = new LiveSegmentRegistry('ch')
    r.markTransfer(1, true, 100)
    r.markTransfer(2, false, 0)
    r.markTransfer(3, true, 200)
    expect(r.stats()).toEqual({ tracked: 0, served: 2, failed: 1 })
  })
})

describe('fetchSegmentWithFallback', () => {
  const deps = () => ({
    playback: {
      channel_id: 'ch-1',
      channel_slug: 'ch',
      channel_name: 'C',
      status: 'LIVE',
      hls_url: 'http://localhost:8888/ch/index.m3u8',
      token: 't.s',
      swarm_base: 'tcdnlive:ch',
      latency_target_seconds: 2,
    },
    tryPeer: vi.fn(),
    onOutcome: vi.fn(),
    onOriginFetch: vi.fn(),
    onPeerFetch: vi.fn(),
    getSessionPeerId: () => 'peer-1',
  })

  beforeEach(() => {
    vi.stubGlobal('XMLHttpRequest', class {
      open() {}
      setRequestHeader() {}
      send() {
        setTimeout(() => {
          this.onerror?.()
        }, 0)
      }
      onerror: (() => void) | null = null
      ontimeout: (() => void) | null = null
      status = 0
      response = new ArrayBuffer(0)
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('uses the peer when the swarm serves bytes', async () => {
    const d = deps()
    d.tryPeer.mockResolvedValue(new ArrayBuffer(1000))
    const { via } = await fetchSegmentWithFallback(d, 'https://x/ch/segment7.m4s')
    expect(via).toBe('p2p')
    expect(d.onPeerFetch).toHaveBeenCalledWith(1000)
    expect(d.onOutcome).toHaveBeenCalledWith(7, true, 1000)
    expect(d.onOriginFetch).not.toHaveBeenCalled()
  })

  it('falls back to origin when the peer returns null', async () => {
    const d = deps()
    d.tryPeer.mockResolvedValue(null)
    // XHR stub resolves successfully this time
    vi.stubGlobal('XMLHttpRequest', class {
      open() {}
      setRequestHeader() {}
      send() {
        setTimeout(() => {
          this.status = 200
          this.response = new ArrayBuffer(500)
          this.onload?.()
        }, 0)
      }
      onload: (() => void) | null = null
      status = 0
      response = new ArrayBuffer(0)
    })
    const { via } = await fetchSegmentWithFallback(d, 'https://x/ch/segment7.m4s')
    expect(via).toBe('origin')
    expect(d.onOriginFetch).toHaveBeenCalledWith(500)
    expect(d.onOutcome).toHaveBeenCalledWith(7, false, 0)
  })

  it('counts an origin-served transfer as served (not failed P2P)', async () => {
    const d = deps()
    d.tryPeer.mockRejectedValue(new Error('swarm dead'))
    vi.stubGlobal('XMLHttpRequest', class {
      open() {}
      setRequestHeader() {}
      send() {
        setTimeout(() => {
          this.status = 200
          this.response = new ArrayBuffer(300)
          this.onload?.()
        }, 0)
      }
      onload: (() => void) | null = null
      status = 0
      response = new ArrayBuffer(0)
    })
    await fetchSegmentWithFallback(d, 'https://x/ch/segment9.m4s')
    // P2P failed → exactly ONE outcome (failed); the origin delivery is
    // accounted via onOriginFetch, NOT a second "served" outcome (which
    // would corrupt the peer success-rate metrics).
    expect(d.onOutcome).toHaveBeenCalledTimes(1)
    expect(d.onOutcome).toHaveBeenCalledWith(9, false, 0)
    expect(d.onOriginFetch).toHaveBeenCalledWith(300)
  })

  it('sends playlists (non-segment URLs) straight to origin — never P2P', async () => {
    const d = deps()
    vi.stubGlobal('XMLHttpRequest', class {
      open() {}
      setRequestHeader() {}
      send() {
        setTimeout(() => {
          this.status = 200
          this.response = new ArrayBuffer(50)
          this.onload?.()
        }, 0)
      }
      onload: (() => void) | null = null
      status = 0
      response = new ArrayBuffer(0)
    })
    const { via } = await fetchSegmentWithFallback(d, 'https://x/ch/index.m3u8')
    expect(via).toBe('origin')
    expect(d.tryPeer).not.toHaveBeenCalled()
  })
})
