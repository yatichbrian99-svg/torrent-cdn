import { describe, expect, it, vi, beforeEach } from 'vitest'

const post = vi.fn()
const get = vi.fn()
const patch = vi.fn()
const del = vi.fn()

vi.mock('./api', () => ({
  default: {
    post: (...a: unknown[]) => post(...a),
    get: (...a: unknown[]) => get(...a),
    patch: (...a: unknown[]) => patch(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}))

import {
  liveApi,
  liveViewerApi,
  formatBytes,
  formatUptime,
  formatBitrate,
  statusColor,
  isSafeHlsUrl,
} from './liveClient'

beforeEach(() => {
  post.mockReset().mockResolvedValue({ data: {} })
  get.mockReset().mockResolvedValue({ data: {} })
  patch.mockReset().mockResolvedValue({ data: {} })
  del.mockReset().mockResolvedValue({ data: {} })
})

describe('formatBytes', () => {
  it('formats human-readable sizes', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(2048)).toBe('2.00 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.00 MB')
    expect(formatBytes(3.5 * 1024 * 1024 * 1024)).toBe('3.50 GB')
    expect(formatBytes(1234 * 1024 * 1024 * 1024)).toContain('TB')
  })
})

describe('formatUptime / formatBitrate', () => {
  it('formats uptime', () => {
    expect(formatUptime(0)).toBe('—')
    expect(formatUptime(45)).toBe('45s')
    expect(formatUptime(125)).toBe('2m 5s')
    expect(formatUptime(7200 + 600)).toBe('2h 10m')
  })
  it('formats bitrate', () => {
    expect(formatBitrate(0)).toBe('—')
    expect(formatBitrate(850)).toBe('850 kbps')
    expect(formatBitrate(2500)).toBe('2.5 Mbps')
  })
})

describe('statusColor', () => {
  it('maps statuses to the design palette', () => {
    expect(statusColor('LIVE')).toBe('#4ade80')
    expect(statusColor('STARTING')).toBe('#fbbf24')
    expect(statusColor('ERROR')).toBe('#ef4444')
    expect(statusColor('OFFLINE')).toBe('#555')
    expect(statusColor('nonsense')).toBe('#555')
  })
})

describe('isSafeHlsUrl', () => {
  it('accepts https and localhost http', () => {
    expect(isSafeHlsUrl('https://stream.example.com/hls/ch/index.m3u8')).toBe(true)
    expect(isSafeHlsUrl('http://localhost:8888/ch/index.m3u8')).toBe(true)
  })
  it('rejects javascript:, data:, and remote http', () => {
    expect(isSafeHlsUrl('javascript:alert(1)')).toBe(false)
    expect(isSafeHlsUrl('data:text/plain,x')).toBe(false)
    expect(isSafeHlsUrl('http://evil.example.com/x.m3u8')).toBe(false)
    expect(isSafeHlsUrl('not a url')).toBe(false)
  })
})

describe('liveApi', () => {
  it('creates channels', async () => {
    await liveApi.createChannel('My Channel', 'desc')
    expect(post).toHaveBeenCalledWith('/api/live/channels', { name: 'My Channel', description: 'desc' })
  })
  it('lists channels', async () => {
    await liveApi.listChannels()
    expect(get).toHaveBeenCalledWith('/api/live/channels')
  })
  it('rotates keys', async () => {
    await liveApi.rotateKey('ch-1')
    expect(post).toHaveBeenCalledWith('/api/live/channels/ch-1/rotate-key')
  })
  it('requests status + analytics with window', async () => {
    await liveApi.getAnalytics('ch-1', 168)
    expect(get).toHaveBeenCalledWith('/api/live/channels/ch-1/analytics?hours=168')
  })
})

describe('liveViewerApi', () => {
  const playback = {
    channel_id: 'ch-1',
    channel_slug: 'my-channel',
    channel_name: 'My Channel',
    status: 'LIVE',
    hls_url: 'http://localhost:8888/my-channel/index.m3u8',
    token: 'tok.ensig',
    swarm_base: 'tcdnlive:my-channel',
    latency_target_seconds: 2,
  }

  it('sends the playback token as Bearer on join', async () => {
    await liveViewerApi.joinSession(playback, 'viewer-12345678')
    expect(post).toHaveBeenCalledWith(
      '/api/live/session',
      expect.objectContaining({ channel_slug: 'my-channel', session_id: 'viewer-12345678' }),
      expect.objectContaining({ headers: { Authorization: 'Bearer tok.ensig' } })
    )
  })

  it('sends heartbeats with counters under the token', async () => {
    await liveViewerApi.heartbeat(playback, {
      session_id: 'viewer-12345678',
      bytes_uploaded: 1000,
      current_segment: 7,
    })
    expect(post).toHaveBeenCalledWith(
      '/api/live/heartbeat',
      expect.objectContaining({ channel_slug: 'my-channel', bytes_uploaded: 1000, current_segment: 7 }),
      expect.objectContaining({ headers: { Authorization: 'Bearer tok.ensig' } })
    )
  })

  it('reports availability with haves + transfers', async () => {
    await liveViewerApi.reportAvailability(playback, {
      session_id: 'viewer-12345678',
      peer_id: 'peer-1',
      haves: [5, 6],
      transfers: [{ sequence_number: 5, served: true, bytes: 250000 }],
    })
    expect(post).toHaveBeenCalledWith(
      '/api/live/segment/availability',
      expect.objectContaining({ haves: [5, 6], transfers: [{ sequence_number: 5, served: true, bytes: 250000 }] }),
      expect.any(Object)
    )
  })

  it('never throws on leave failures', async () => {
    post.mockRejectedValueOnce(new Error('gone'))
    await expect(liveViewerApi.leave(playback, 'viewer-12345678')).resolves.toBeUndefined()
  })
})
