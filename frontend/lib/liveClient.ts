/**
 * Live streaming API client.
 *
 * Lives in lib/ so the coverage gate (vitest.config.ts counts every file under
 * lib/ + components/) applies — hence the pure helpers below are exported and
 * unit-tested. Playback-token-bearing viewer calls go through the same axios
 * instance (Bearer header = playback token for viewer endpoints, session JWT
 * for owner endpoints).
 */
import api from './api'
import { isSafeHttpsUrl } from './security'

// ── Pure helpers (unit-tested; no network) ───────────────────────────────────

export interface LiveChannel {
  id: string
  tenant_id: string
  owner_id: string
  name: string
  slug: string
  description: string | null
  status: 'OFFLINE' | 'STARTING' | 'LIVE' | 'ERROR'
  enabled: boolean
  stream_key_prefix: string | null
  // Present ONLY on the creation response — the one-time plaintext setup
  // secrets (key stored hashed server-side; URLs are config-derived).
  stream_key?: string | null
  rtmp_server?: string | null
  publish_path?: string | null
  created_at: string
  updated_at: string
  last_seen_at: string | null
  current_viewers: number
  current_peers: number
  bitrate_kbps: number
  uptime_seconds: number
  latest_segment_sequence: number | null
}

export interface LivePlaybackInfo {
  channel_id: string
  channel_slug: string
  channel_name: string
  status: string
  hls_url: string
  token: string
  swarm_base: string
  latency_target_seconds: number
  // Paste-ready OBS ingest (no secrets — config-derived hosts).
  rtmp_server?: string | null
  publish_path?: string | null
}

export interface LiveStatus {
  channel_id: string
  slug: string
  status: string
  enabled: boolean
  current_viewers: number
  current_peers: number
  bitrate_kbps: number
  uptime_seconds: number
  latest_segment_sequence: number | null
  latency_target_seconds: number
  last_seen_at: string | null
  // Human-readable WHY for the current stage ("Publisher connected — waiting
  // for MediaMTX to confirm publisher and produce HLS segments..."). Optional
  // for backward compatibility with older backends.
  status_reason?: string | null
}

/** Owner-facing ingest diagnostics (GET /api/live/channels/{id}/ingest). */
export interface LiveIngestDiagnostics {
  channel_id: string
  slug: string
  status: string
  status_reason: string
  rtmp_listener_reachable: boolean
  auth_status: 'accepted' | 'denied' | 'unknown'
  publish_attempts: number
  publish_denied: number
  reconnect_count: number
  publisher_connected: boolean
  publisher_uptime_seconds: number
  mediamtx_path: string
  mediamtx_path_active: boolean
  hls_playlist_available: boolean | null
  latest_segment_sequence: number | null
  last_segment_age_seconds: number | null
  ingest_bitrate_kbps: number
  codec: string | null
  resolution: string | null
  fps: number | null
  last_publish_outcome: string | null
}

export interface LiveAnalytics {
  channel_id: string
  window_hours: number
  bytes_p2p: number
  bytes_origin: number
  total_bytes: number
  p2p_offload_percent: number
  segments_served: number
  segments_failed: number
  transfer_success_rate: number
  transfer_failure_rate: number
  buffering_events: number
  playback_errors: number
  peak_viewers: number
  peak_peers: number
  avg_startup_ms: number
  uptime_seconds: number
  sessions: number
  current: { viewers: number; peers: number }
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  const digits = v >= 100 ? 0 : v >= 10 ? 1 : 2
  return `${v.toFixed(digits)} ${units[i]}`
}

export function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—'
  const s = Math.floor(seconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s % 60}s`
  return `${s}s`
}

export function formatBitrate(kbps: number): string {
  if (!Number.isFinite(kbps) || kbps <= 0) return '—'
  return kbps >= 1000 ? `${(kbps / 1000).toFixed(1)} Mbps` : `${Math.round(kbps)} kbps`
}

export function statusColor(status: string): string {
  switch (status) {
    case 'LIVE':
      return '#4ade80'
    case 'STARTING':
      return '#fbbf24'
    case 'ERROR':
      return '#ef4444'
    default:
      return '#555'
  }
}

/** True when an HLS URL is safe to hand to the player (https or localhost http). */
export function isSafeHlsUrl(url: string): boolean {
  try {
    const u = new URL(url)
    if (u.protocol === 'https:') return true
    if (u.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(u.hostname)) return true
    return false
  } catch {
    return false
  }
}

/**
 * True when an RTMP ingest URL points at localhost — i.e. it can only work
 * for an encoder on the SAME machine. External broadcasters (OBS/Wirecast on
 * another device) need the public RTMP URL; showing localhost to them is the
 * "stuck on localhost" failure. Used to badge the OBS setup card.
 */
export function isLocalRtmpUrl(url: string): boolean {
  try {
    const u = new URL(url.replace(/^rtmp:/, 'http:'))
    return ['localhost', '127.0.0.1', '[::1]', '::1'].includes(u.hostname)
  } catch {
    return false
  }
}

export { isSafeHttpsUrl }

// ── Owner (dashboard) endpoints — session JWT via the shared axios client ────

export const liveApi = {
  createChannel: (name: string, description?: string) =>
    api.post('/api/live/channels', { name, description: description || undefined }).then((r) => r.data),

  listChannels: (): Promise<LiveChannel[]> =>
    api.get('/api/live/channels').then((r) => r.data),

  getChannel: (id: string): Promise<LiveChannel> =>
    api.get(`/api/live/channels/${id}`).then((r) => r.data),

  updateChannel: (id: string, data: { name?: string; description?: string }) =>
    api.patch(`/api/live/channels/${id}`, data).then((r) => r.data),

  deleteChannel: (id: string) =>
    api.delete(`/api/live/channels/${id}`).then((r) => r.data),

  enableChannel: (id: string) =>
    api.post(`/api/live/channels/${id}/enable`).then((r) => r.data),

  disableChannel: (id: string) =>
    api.post(`/api/live/channels/${id}/disable`).then((r) => r.data),

  // Returns the plaintext stream key exactly once — never store or log it.
  rotateKey: (id: string): Promise<{ channel_id: string; stream_key: string; stream_key_prefix: string; rtmp_server?: string | null; publish_path?: string | null }> =>
    api.post(`/api/live/channels/${id}/rotate-key`).then((r) => r.data),

  getStatus: (id: string): Promise<LiveStatus> =>
    api.get(`/api/live/channels/${id}/status`).then((r) => r.data),

  // Ingest diagnostics: where the RTMP → MediaMTX → HLS chain stands right now.
  getIngestDiagnostics: (id: string): Promise<LiveIngestDiagnostics> =>
    api.get(`/api/live/channels/${id}/ingest`).then((r) => r.data),

  getAnalytics: (id: string, hours = 24): Promise<LiveAnalytics> =>
    api.get(`/api/live/channels/${id}/analytics?hours=${hours}`).then((r) => r.data),

  getPlaybackInfo: (id: string): Promise<LivePlaybackInfo> =>
    api.get(`/api/live/channels/${id}/playback`).then((r) => r.data),
}

// ── Viewer endpoints — Bearer = short-lived playback token ───────────────────

export const liveViewerApi = {
  joinSession: (playback: LivePlaybackInfo, sessionId: string, userAgent?: string) =>
    api
      .post(
        '/api/live/session',
        {
          channel_slug: playback.channel_slug,
          session_id: sessionId,
          user_agent: userAgent || (typeof navigator !== 'undefined' ? navigator.userAgent : undefined),
        },
        { headers: { Authorization: `Bearer ${playback.token}` } }
      )
      .then((r) => r.data),

  heartbeat: (
    playback: LivePlaybackInfo,
    data: {
      session_id: string
      peer_id?: string
      bytes_uploaded?: number
      bytes_downloaded?: number
      segments_served?: number
      segments_failed?: number
      buffering_events?: number
      playback_errors?: number
      current_segment?: number | null
      current_position?: number | null
    }
  ) =>
    api
      .post('/api/live/heartbeat', { channel_slug: playback.channel_slug, ...data }, { headers: { Authorization: `Bearer ${playback.token}` } })
      .then((r) => r.data),

  reportAvailability: (
    playback: LivePlaybackInfo,
    data: {
      session_id: string
      peer_id: string
      haves: number[]
      transfers: { sequence_number: number; served: boolean; bytes?: number }[]
    }
  ) =>
    api
      .post(
        '/api/live/segment/availability',
        { channel_slug: playback.channel_slug, ...data },
        { headers: { Authorization: `Bearer ${playback.token}` } }
      )
      .then((r) => r.data),

  leave: (playback: LivePlaybackInfo, sessionId: string, peerId?: string) =>
    api
      .post(
        '/api/live/leave',
        { channel_slug: playback.channel_slug, session_id: sessionId, peer_id: peerId },
        { headers: { Authorization: `Bearer ${playback.token}` } }
      )
      .then((r) => r.data)
      .catch(() => {}), // leaving must never block unmount
}
