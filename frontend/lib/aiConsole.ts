/**
 * Client for the TCDN-AI developer console (`/api/ai/v1/console/*`).
 *
 * Why a separate module instead of `lib/api.ts`: everything here must stay off
 * the critical path of the landing page, dashboard and upload flow. The flag
 * probe below is the only piece that ever runs on a non-AI page, and it is one
 * memoised request per tab.
 *
 * Gating: the backend mounts the AI routes only when `ENABLE_TCDN_AI` is on, so
 * `/api/ai/v1/console/status` answers 404 on a flag-off deployment. That 404 IS
 * the feature flag — there is deliberately no always-on probe endpoint, because
 * one would give the flag-off build an AI route it is not supposed to have.
 *
 * Secrets: the API key shown after creation lives in React state only. Nothing
 * here ever writes a key to localStorage, sessionStorage, a cookie, a URL or a
 * log — see `docs/tcdn-ai/FRONTEND_PLAN.md` §6.
 */
import api from './api'

/** Prefix kept through both dev (direct backend) and the production proxy. */
export const AI_CONSOLE_BASE = '/api/ai/v1/console'

export interface AiBalance {
  micro: number
  credits_usd: number
  held_micro: number
  available_micro: number
  kes_per_usd: number
  approx_kes: number
}

export interface AiSummary {
  account_id: string
  balance: AiBalance
  usage: { jobs: number; spend_micro: number; last_job_at: number | null }
  keys: { active: number; total: number }
  models: { count: number }
  checklist: {
    account_ready: boolean
    has_key: boolean
    has_job: boolean
    has_credit: boolean
  }
  status: {
    feature_on: boolean
    environment: string
    commit: string | null
    database: string
    redis: string
    workers: {
      registered: number | null
      healthy: number | null
      heartbeat_timeout_seconds?: number
      newest_heartbeat_age_seconds?: number | null
      models_served?: string[]
    }
    server_time: number
  }
}

export interface AiRegion {
  region: string
  registered: number
  healthy: number
  newest_heartbeat_age_seconds: number | null
}

export interface AiStatus {
  service: string
  commit: string | null
  environment: string
  feature_flag: { name: string; on: boolean; routes_mounted: boolean }
  statement: string
  database: { status: string; latency_ms: number; detail?: string }
  redis: { status: string; latency_ms: number; detail?: string }
  workers: {
    registered: number | null
    healthy: number | null
    heartbeat_timeout_seconds: number
    newest_heartbeat_age_seconds: number | null
    oldest_heartbeat_age_seconds: number | null
    models_served: string[]
  }
  regions: AiRegion[]
  server_time: number
}

export interface AiKey {
  id: string
  prefix: string
  scopes: string[]
  status: string
  created_at: number
  last_used_at: number | null
  revoked_at: number | null
}

export interface AiModel {
  model_id: string
  name: string
  format: string
  size_bytes: number
  required_vram_gb: number
  vram_tier: 'small' | 'medium' | 'large'
  license_note: string
  allowlisted: boolean
  standard: {
    input_micro_per_mtok: number
    output_micro_per_mtok: number
    available: boolean
  }
  confidential: {
    input_micro_per_mtok: number
    output_micro_per_mtok: number
    available: boolean
    reason: string
  }
  launch_pricing_until: string
}

export interface AiJob {
  job_id: string
  model: string
  status: string
  tier: string
  confidentiality: string
  input_tokens: number
  output_tokens: number
  total_micro: number
  hold_micro: number
  created_at: number
  settled_at: number | null
  region: string
  gpu_model: string | null
  dispute_reason: string | null
}

export interface AiJobDetail {
  job: AiJob
  verification: { state: string; label: string; detail?: string | null }
  settlement: { compute_micro: number; seeder_micro: number; platform_micro: number }
}

export interface AiPack {
  id: string
  label: string
  credits_usd: number
  credit_micro: number
  kes: number
  kes_minor: number
  currency: string
}

export interface AiCheckout {
  mode: 'test' | 'simulated'
  reference: string
  authorization_url: string
  pack_id: string
  credit_micro: number
  kes: number
}

/** GET that resolves to null on 404 — used only by the feature probe. */
let probe: Promise<boolean> | null = null
let probedAt = 0
const PROBE_TTL_MS = 60_000

/**
 * True when this deployment serves the AI surface.
 *
 * Fails closed: a 404 (flag off), a 5xx, a timeout or a proxy error all mean
 * "not available", because a broken probe must never render a dashboard that
 * cannot work.
 */
export async function aiFeatureEnabled(force = false): Promise<boolean> {
  const now = Date.now()
  if (!force && probe && now - probedAt < PROBE_TTL_MS) return probe
  probedAt = now
  probe = api
    .get(`${AI_CONSOLE_BASE}/status`, { timeout: 8000 })
    .then((response) => Boolean(response.data?.feature_flag?.on ?? true))
    .catch(() => false)
  return probe
}

/** Test-only seam so unit tests do not share a module-level cache. */
export function resetAiFeatureProbe() {
  probe = null
  probedAt = 0
}

export const aiConsole = {
  status: () => api.get(`${AI_CONSOLE_BASE}/status`).then((r) => r.data as AiStatus),
  summary: () => api.get(`${AI_CONSOLE_BASE}/summary`).then((r) => r.data as AiSummary),
  models: () =>
    api
      .get(`${AI_CONSOLE_BASE}/models`)
      .then((r) => r.data as { models: AiModel[]; tiers: { id: string; label: string; available: boolean; reason?: string }[] }),
  keys: () => api.get(`${AI_CONSOLE_BASE}/keys`).then((r) => r.data as { keys: AiKey[] }),
  createKey: (scopes: string[]) =>
    api
      .post(`${AI_CONSOLE_BASE}/keys`, { scopes })
      .then((r) => r.data as { api_key: string; prefix: string; scopes: string[]; notice: string }),
  revokeKey: (id: string) =>
    api.post(`${AI_CONSOLE_BASE}/keys/${encodeURIComponent(id)}/revoke`).then((r) => r.data),
  usage: (params: { limit?: number; model?: string; status?: string }) =>
    api
      .get(`${AI_CONSOLE_BASE}/usage`, { params })
      .then((r) => r.data as { jobs: AiJob[]; total_micro: number; note: string }),
  job: (id: string) =>
    api
      .get(`${AI_CONSOLE_BASE}/jobs/${encodeURIComponent(id)}`)
      .then((r) => r.data as AiJobDetail),
  packs: () =>
    api
      .get(`${AI_CONSOLE_BASE}/billing/packs`)
      .then((r) => r.data as { packs: AiPack[]; currency: { code: string; kes_per_usd: number }; mode: string; provider: string; note: string }),
  checkout: (packId: string) =>
    api.post(`${AI_CONSOLE_BASE}/billing/checkout`, { pack_id: packId }).then((r) => r.data as AiCheckout),
  checkoutStatus: (reference: string) =>
    api
      .get(`${AI_CONSOLE_BASE}/billing/checkout/${encodeURIComponent(reference)}`)
      .then((r) => r.data as { reference: string; status: 'pending' | 'paid'; credit_micro: number; balance_micro?: number }),
  simulateCheckout: (reference: string) =>
    api
      .post(`${AI_CONSOLE_BASE}/billing/checkout/${encodeURIComponent(reference)}/simulate`)
      .then((r) => r.data as { status: 'paid'; credit_micro: number; balance_micro: number }),
}

/** One minimal chat completion, used by "test my key". Never stores the key. */
export async function testApiKey(key: string, model: string) {
  const response = await fetch('/api/ai/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${key}`,
    },
    body: JSON.stringify({
      model,
      messages: [{ role: 'user', content: 'ping' }],
      max_tokens: 8,
      stream: false,
    }),
  })
  const text = await response.text()
  return { status: response.status, ok: response.ok, body: text.slice(0, 2000) }
}

// ── formatting ─────────────────────────────────────────────────────────────

export function usd(micro: number, digits = 4): string {
  return `$${(micro / 1_000_000).toFixed(digits)}`
}

export function kes(micro: number, perUsd: number): string {
  const value = (micro / 1_000_000) * perUsd
  return `≈ KES ${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
}

export function perMtok(micro: number): string {
  // Prices are stored per million tokens in micro-credits (1 credit = $1).
  return `$${(micro / 1_000_000).toFixed(2)} / Mtok`
}

export function timestamp(unix: number | null): string {
  if (!unix) return '—'
  return new Date(unix * 1000).toISOString().replace('T', ' ').slice(0, 16) + ' UTC'
}

export function relativeAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return 'never'
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  return `${Math.round(seconds / 3600)}h ago`
}

export function bytes(value: number): string {
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GB`
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(0)} MB`
  return `${value} B`
}
