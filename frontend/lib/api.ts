import axios from 'axios'
import { getToken, removeToken } from './auth'

// Production ALWAYS routes API calls through the same-origin Vercel proxy:
// vercel.json rewrites /api/*, /auth/*, /upload/*, /files/*, /analytics/*,
// /billing/*, /admin/*, /notifications/* to the Render backend, so relative
// URLs are reliable AND avoid the page CSP + CORS entirely.
//
// FILE UPLOADS are the one exception — the file bytes NEVER go through Vercel
// or Render. The primary path is a presigned direct-to-Supabase-Storage PUT:
//   1. POST /upload/presign             → backend mints a short-lived presigned PUT URL
//   2. browser PUTs the raw file straight to Supabase Storage (XHR progress)
//   3. POST /upload/presign/finalize/id → backend verifies + builds the torrent
//      magnet/thumbnail, then persists the record
// Vercel's proxy hard-caps request bodies at ~4.5MB (FUNCTION_PAYLOAD_TOO_LARGE)
// and Render's free tier is flaky for big POSTs, so routing the upload bytes
// around both is what makes real video uploads work. middleware.ts's connect-src
// allow-lists the storage origin (https://*.supabase.co) for the PUT.
//
// A cross-origin NEXT_PUBLIC_API_URL (e.g. someone setting it to a direct
// https://torrent-cdn.onrender.com URL) breaks login in production: the browser
// either blocks the fetch via connect-src or the direct cross-origin POST fails
// at the network layer ('Could not reach the server'). So in production we
// IGNORE the env var and stay same-origin. The env override is only honored in
// local dev, where vercel.json rewrites don't exist.
const isProduction = process.env.NODE_ENV === 'production'
const API_BASE = isProduction ? '' : (process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000')

// /auth/forgot-password and /auth/reset-password have REAL Next.js pages at
// those exact paths, which SHADOW the vercel.json /auth/* rewrite (afterFiles)
// in production — a same-origin POST to them returns the page HTML instead of
// reaching the backend (the same bug class as /admin/health, fixed earlier).
// Route those two API calls through the /api/* rewrite instead: no pages live
// under /api, and the rewrite strips the prefix onto the backend's /auth/*
// routes. In local dev there is no proxy — the frontend calls the backend
// directly, where the /api prefix does not exist — so only production uses it.
const passwordResetPath = (p: string) => (isProduction ? `/api/auth${p}` : `/auth${p}`)

// Human-friendly error text for UI forms. Backend errors are JSON
// {detail: "..."}; proxy/platform failures (e.g. Vercel's plain-text 413 page)
// have no detail, and network errors have no response at all — fall back to the
// caller's message (or a status-tagged variant) instead of a bare catch-all.
export function apiErrorMessage(err: any, fallback = 'Something went wrong'): string {
  const data = err?.response?.data
  if (data && typeof data.detail === 'string' && data.detail) return data.detail
  // Platform/proxy failures (e.g. Vercel's plain-text 413 page) return text
  // bodies. Skip HTML pages and cap length — the UI renders this verbatim.
  if (typeof data === 'string' && data.trim() && !data.trim().startsWith('<')) {
    const s = data.trim()
    return s.length > 160 ? `${s.slice(0, 160)}…` : s
  }
  if (err?.response?.status) return `${fallback} (HTTP ${err.response.status})`
  if (err?.code === 'ECONNABORTED') return 'The request timed out. Please try again.'
  return fallback
}

// Chunked-upload fallback: slice size for shipping files through the same-origin
// Vercel proxy. Each chunk must stay under the proxy's ~4.5MB request-body cap
// (verified empirically: 4MB passes, 5MB is rejected with 413). MUST match the
// backend MAX_CLIENT_CHUNK_SIZE in routes/upload.py.
const CHUNK_SIZE = 4 * 1024 * 1024

// A failure is "transient" when a retry could plausibly succeed: no response at
// all (timeout / connection reset — a Render cold start or the documented
// free-tier POST quirk) or a proxy/load-balancer 502/503/504. 4xx
// (400/401/413/422) and app-level 5xx (500) are NOT transient — except 429
// WITH a Retry-After header (the global burst limiter's response shape): that
// is a "slow down and go again" signal, not a rejection, so page loads ride
// through it instead of dead-ending. A 429 WITHOUT Retry-After is a real
// lockout (login brute-force gate, 15-min message) and must surface instantly.
const isTransientUploadError = (err: any) => {
  const status = err?.response?.status
  if (status === 429 && err.response?.headers?.['retry-after']) return true
  return status === 502 || status === 503 || status === 504 || !err.response
}

// Public twin of isTransientUploadError for PAGE loads: Render's free tier
// cold-starts after ~15min idle, and the FIRST request after idle dies as a
// 502/503/504 at the Vercel proxy (or an axios timeout). Pages must treat that
// as "retry", not "you don't have access" — the admin pages previously
// rendered 'You need admin privileges…' for a cold-start 502 because only the
// 403 status was special-cased.
// 429 WITH Retry-After counts too (global burst limiter): users behind a
// shared NAT/office IP previously hit 'Rate limit exceeded' with nothing
// loading when several dashboards opened at once — retrying after the window
// clears recovers automatically. A 429 WITHOUT Retry-After (login lockout,
// register gate) is a deliberate lockout and is NOT transient.
// "No response" only counts when the error came from an actual HTTP attempt
// (axios network/timeout errors carry `request`, and abort errors carry
// `code`) — a locally-thrown Error (e.g. a malformed-payload guard) has
// neither and is NOT transient.
export const isTransientError = (err: any) => {
  const status = err?.response?.status
  if (status === 502 || status === 503 || status === 504 || status === 529) return true
  if (status === 429 && err.response?.headers?.['retry-after']) return true
  return !err.response && !!(err.request || err.code)
}

// Retry fn on transient failures. Used by page data loads (cold starts).
//
// The default schedule must survive a FULL Docker/Render cold start, not just
// a Render wake-up: the dockerized backend takes ~40-60s to boot (migration
// run + gunicorn workers), and the free-tier Render instance adds its own
// wake-up on top. The old 1s + 3s schedule gave up at ~4s — the user saw
// 'Failed to load' / 'You need admin privileges…' (via the getMe redirect)
// while the backend was still booting. Escalating pauses (2s → 5s → 10s → 10s
// → 20s → 20s) keep retrying for up to ~65s before surfacing an error; 401/
// 403 still fail IMMEDIATELY (they mean dead session / not an admin, and
// retrying those would add ~a minute of dead wait to a real denial).
// Callers that already pass a retry count keep working: retries are executed
// against the schedule index (retry N uses schedule[N]), so e.g. retries=2
// still means '2 extra attempts' with the first two pauses.
const TRANSIENT_RETRY_DELAYS_MS = [2000, 5000, 10000, 10000, 20000, 20000]

export const retryOnTransient = async (fn: () => Promise<any>, retries = TRANSIENT_RETRY_DELAYS_MS.length, delayMs?: number): Promise<any> => {
  let lastErr: any
  for (let attempt = 0; ; attempt++) {
    try {
      return await fn()
    } catch (err: any) {
      lastErr = err
      if (!isTransientError(err) || attempt >= retries) throw err
      // delayMs (legacy fixed-pause callers) wins; otherwise the escalating
      // schedule indexed by attempt number (clamped to the last entry),
      // raised to the limiter's Retry-After when the failure was a 429 so
      // the retry lands after the window actually resets.
      let delay = delayMs ?? TRANSIENT_RETRY_DELAYS_MS[Math.min(attempt, TRANSIENT_RETRY_DELAYS_MS.length - 1)]
      const ra = parseInt(err?.response?.headers?.['retry-after'] || '0', 10)
      if (!delayMs && Number.isFinite(ra) && ra > 0) delay = Math.max(delay, ra * 1000 + 250)
      await new Promise((resolve) => setTimeout(resolve, delay))
    }
  }
}

const newIdempotencyKey = () =>
  typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `up-${Date.now()}-${Math.random().toString(36).slice(2)}`

// Retries fn up to `retries` EXTRA times on transient failures. Safe for the
// chunked flow because every retried call is idempotent server-side: session
// create dedupes on the idempotency key, chunk writes are offset-based, and
// finalize dedupes on the session's key.
const withTransientRetry = async (fn: () => Promise<any>, retries = 2, delayMs = 1000) => {
  let lastErr: any
  for (let attempt = 0; ; attempt++) {
    try {
      return await fn()
    } catch (err: any) {
      lastErr = err
      if (!isTransientUploadError(err) || attempt >= retries) throw err
      await new Promise((resolve) => setTimeout(resolve, delayMs))
    }
  }
}

// Presigned direct-to-storage upload (PRIMARY path). The browser PUTs the file
// bytes straight to Supabase Storage — they never touch Render or the Vercel
// proxy. The PUT uses raw XHR, NOT the axios instance: the request interceptor
// would attach the app JWT (breaking the SigV4 signature on the presigned URL)
// and the response interceptor would redirect the page on any 401. XHR also
// gives us upload progress.
const presignUpload = async (file: File, idempotencyKey: string) =>
  withTransientRetry(() =>
    api
      .post('/upload/presign', {
        filename: file.name,
        file_size: file.size,
        content_type: file.type || undefined,
        idempotency_key: idempotencyKey,
      })
      .then((r) => r.data)
  )

const putToStorage = (
  presignedUrl: string,
  file: File,
  contentType: string,
  onProgress?: (pct: number) => void
) =>
  new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', presignedUrl)
    // ContentType is a SIGNED header on the presigned URL — the backend mints
    // the URL with this exact value, so the PUT must send it verbatim or
    // Supabase rejects the signature.
    xhr.setRequestHeader('Content-Type', contentType)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100))
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve()
      else {
        const err: any = new Error(`Storage upload failed (HTTP ${xhr.status})`)
        err.response = { status: xhr.status }
        reject(err)
      }
    }
    xhr.onerror = () => reject(new Error('Network error uploading to storage'))
    xhr.ontimeout = () => {
      const err: any = new Error('Storage upload timed out')
      err.code = 'ECONNABORTED'
      reject(err)
    }
    // 1GB on a slow uplink can outlast every other timeout in this file.
    xhr.timeout = 600000
    xhr.send(file)
  })

const presignedUpload = async (
  file: File,
  idempotencyKey: string,
  onProgress?: (pct: number) => void,
  onRetry?: () => void
) => {
  const presign = await presignUpload(file, idempotencyKey)
  // A previous attempt with this key already completed (lost response) — the
  // backend returns it as `result` and we skip the PUT entirely.
  if (presign.result) return presign.result

  try {
    await putToStorage(presign.presigned_url, file, presign.content_type, onProgress)
  } catch (err: any) {
    // The presigned URL stays valid for ~15 min — retry the PUT once on a
    // transient failure before giving up to the chunked fallback.
    if (!isTransientUploadError(err)) throw err
    if (onRetry) onRetry()
    await new Promise((resolve) => setTimeout(resolve, 2000))
    await putToStorage(presign.presigned_url, file, presign.content_type, onProgress)
  }

  // Finalize is idempotent server-side (dedup on the idempotency key), so a
  // retry after a lost response can't duplicate the record.
  return withTransientRetry(() =>
    api.post(`/upload/presign/finalize/${presign.upload_id}`, null, { timeout: 300000 }).then((r) => r.data)
  )
}

// Fallback path: slice the file into ≤4MB chunks and ship them through the
// same-origin Vercel proxy (/upload/* rewrites to the backend). Used when the
// direct path fails transiently. Returns the same UploadResponse shape.
const chunkedUpload = async (
  file: File,
  idempotencyKey: string,
  onProgress?: (pct: number) => void
) => {
  const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE))

  // 1. Create the session. If the direct attempt already stored this file (lost
  // response), the backend returns it as `result` and we skip the chunks.
  const session = await withTransientRetry(() =>
    api
      .post('/upload/session', {
        filename: file.name,
        file_size: file.size,
        total_chunks: totalChunks,
        content_type: file.type || undefined,
        idempotency_key: idempotencyKey,
      })
      .then((r) => r.data)
  )
  if (session.result) return session.result

  // 2. Stream the chunks.
  for (let i = 0; i < totalChunks; i++) {
    const blob = file.slice(i * CHUNK_SIZE, Math.min((i + 1) * CHUNK_SIZE, file.size))
    const form = new FormData()
    form.append('chunk', blob, `chunk-${i}`)
    await withTransientRetry(() =>
      api
        .post(`/upload/chunk/${session.upload_id}/${i}`, form, {
          timeout: 60000,
          onUploadProgress: (e) => {
            if (onProgress && e.total) {
              onProgress(Math.round(((i + e.loaded / e.total) / totalChunks) * 100))
            }
          },
        })
        .then((r) => r.data)
    )
    if (onProgress) onProgress(Math.round(((i + 1) / totalChunks) * 100))
  }

  // 3. Finalize (idempotent server-side, so retrying after a lost response is safe).
  return withTransientRetry(() =>
    api.post(`/upload/finalize/${session.upload_id}`, null, { timeout: 300000 }).then((r) => r.data)
  )
}

const api = axios.create({
  baseURL: API_BASE,
  // 45s: Render's free tier cold-starts after ~15min of inactivity (can take
  // 20-45s). The old 15s timeout made the FIRST request after idle fail with a
  // generic 'Something went wrong' even though the backend eventually responds.
  timeout: 45000,
})

api.interceptors.request.use((config) => {
  // An explicitly-set Authorization header wins (mid-login MFA enrollment
  // sends the short-lived mfa_token while no session token exists yet);
  // otherwise attach the stored session token.
  const token = getToken()
  if (token && !config.headers.Authorization) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    // A 401 from the login/register/MFA-confirm endpoints means WRONG
    // CREDENTIALS (or a wrong 2FA code), not an expired session — let the form
    // show the real error instead of nuking the page with a redirect.
    // Everywhere else (auth/me, files, billing, ...) a 401 still means the
    // session died, so clear the token and bounce home.
    const url = error.config?.url || ''
    // mfa/setup 401s mid-login mean the short-lived mfa_token expired (the
    // modal restarts enrollment at login) — NOT a dead session, so don't
    // bounce the page. (mfa/confirm is already listed below.)
    const isCredentialCheck =
      url === '/auth/login' ||
      url === '/auth/register' ||
      url === '/auth/mfa/setup' ||
      url === '/auth/mfa/confirm'
    if (error.response?.status === 401 && !isCredentialCheck) {
      removeToken()
      if (typeof window !== 'undefined') {
        window.location.href = '/'
      }
    }
    return Promise.reject(error)
  }
)

export default {
  // Raw axios passthroughs — used by newer clients (e.g. live) that need
  // custom per-call Authorization headers (playback tokens) without touching
  // the app session token. Standard verb names on the shared instance so
  // interceptors (Bearer fallback, 401 handling) keep applying.
  post: (url: string, data?: unknown, config?: object) => api.post(url, data, config),
  get: (url: string, config?: object) => api.get(url, config),
  patch: (url: string, data?: unknown, config?: object) => api.patch(url, data, config),
  put: (url: string, data?: unknown, config?: object) => api.put(url, data, config),
  delete: (url: string, config?: object) => api.delete(url, config),

  register: (email: string, password: string, termsAccepted: boolean = false, refCode?: string) =>
    api.post('/auth/register', { email, password, terms_accepted: termsAccepted, ref_code: refCode || undefined }).then((r) => r.data),

  login: async (email: string, password: string) => {
    // Render's free tier cold-starts after ~15min idle — the first request after
    // idle can hit a transient 502/503/504 at the Vercel proxy while the
    // instance wakes. Retry ONCE after a short pause so users don't have to
    // click Login twice. Login is safe to retry (idempotent: only mints a
    // token). Non-5xx errors (wrong password 401, lockout 429) pass through.
    try {
      return (await api.post('/auth/login', { email, password })).data
    } catch (err: any) {
      const status = err.response?.status
    // Retry once on a cold start: either a transient 502/503/504 from the
    // proxy, a bare 500 (gunicorn's --timeout kill surfaces as 500, and the
    // backend's JSON 500 handler returns no useful per-request detail), OR no
    // response at all (the 45s timeout fires while Render boots). Login is
    // idempotent (only mints a token) so retrying is safe — it can't hit the
    // 401/429 lockout paths, which pass through untouched.
    const isTransient = status === 500 || status === 502 || status === 503 || status === 504 || !err.response
      if (!isTransient) throw err
      await new Promise((resolve) => setTimeout(resolve, 2000))
      return (await api.post('/auth/login', { email, password })).data
    }
  },

  getMe: () => api.get('/auth/me').then((r) => r.data),

  uploadFile: (
    file: File,
    opts: { onProgress?: (pct: number) => void; onRetry?: () => void } = {}
  ) => {
    const { onProgress, onRetry } = opts
    // Idempotency key — stable across the presigned attempt AND the chunked
    // fallback, so whichever path lands first, a retry can never create a
    // duplicate (the backend dedupes on it).
    const idempotencyKey = newIdempotencyKey()
    return presignedUpload(file, idempotencyKey, onProgress, onRetry).catch((err: any) => {
      // Definitive rejections (unsupported type, over quota, magic-byte
      // failure, size mismatch) pass through — the chunked fallback would hit
      // the same wall. Everything else (storage-bucket CORS not configured yet,
      // storage/network hiccup, backend cold start) falls back to chunked
      // uploads through the reliable same-origin Vercel proxy (each chunk stays
      // under the proxy's ~4.5MB body cap).
      const status = err?.response?.status
      if (status === 400 || status === 402 || status === 413 || status === 422) throw err
      if (onRetry) onRetry()
      return chunkedUpload(file, idempotencyKey, onProgress)
    })
  },

  listFiles: () => api.get('/files').then((r) => r.data),

  getFile: (file_id: string) => api.get(`/files/${file_id}`).then((r) => r.data),

  deleteFile: (file_id: string) => api.delete(`/files/${file_id}`).then((r) => r.data),

  getPublicFile: (file_id: string) => api.get(`/files/public/${file_id}`).then((r) => r.data),

  // Import-by-URL: the server downloads a copy (SSRF-guarded), runs the same
  // validation/storage pipeline as every upload path, and content-hash dedup
  // applies — identical bytes store once regardless of source. `filename` is
  // optional; when omitted it is derived from the URL path or Content-Type.
  importUrl: (url: string, filename?: string) =>
    api.post('/upload/import', { url, filename: filename || undefined }).then((r) => r.data),

  trackEvent: (file_id: string, event_type: string, bytes_p2p?: number, bytes_origin?: number) =>
    api.post('/analytics/event', { file_id, event_type, bytes_p2p: bytes_p2p || 0, bytes_origin: bytes_origin || 0 }).then((r) => r.data),

  getAnalytics: (file_id: string) => api.get(`/analytics/${file_id}`).then((r) => r.data),

  // Public aggregate stats (views, offload %, bandwidth saved, active peers)
  // for the public demo page — the backend endpoint is unauthenticated.
  getPublicAnalytics: (file_id: string) => api.get(`/analytics/public/${file_id}`).then((r) => r.data),

  getBillingStatus: () => api.get('/billing/status').then((r) => r.data),

  getApiKey: () => api.get('/auth/api-key').then((r) => r.data),

  // Generate + revoke retry once on transient failures (Render cold start: a
  // 502/503/504 from the proxy or no response while the instance wakes) — the
  // same condition the login flow auto-retries. Both operations are safe to
  // retry: create is an atomic rotate (one key per user, so a duplicate can't
  // appear) and revoke is an idempotent DELETE.
  createApiKey: () =>
    withTransientRetry(() => api.post('/auth/api-key').then((r) => r.data)),

  revokeApiKey: () =>
    withTransientRetry(() => api.delete('/auth/api-key').then((r) => r.data)),

  // Two-factor authentication (TOTP)
  mfaStatus: () => api.get('/auth/mfa').then((r) => r.data),

  // Begin MFA enrollment. Pass an mfa_token (from a pending login) to enroll
  // mid-login — mandatory setup — before any session token exists; with no
  // argument the stored session token authenticates (dashboard flow).
  mfaSetup: (mfaToken?: string) =>
    api.post('/auth/mfa/setup', null, mfaToken ? { headers: { Authorization: `Bearer ${mfaToken}` } } : undefined).then((r) => r.data),

  mfaEnable: (code: string) =>
    api.post('/auth/mfa/enable', { code }).then((r) => r.data),

  mfaDisable: (code: string) =>
    api.post('/auth/mfa/disable', { code }).then((r) => r.data),

  // Second factor for a pending MFA login (password already verified).
  mfaConfirm: (mfaToken: string, code: string) =>
    api.post('/auth/mfa/confirm', { mfa_token: mfaToken, code }).then((r) => r.data),

  verifyEmail: (token: string) =>
    api.post('/auth/verify-email', { token }).then((r) => r.data),

  resendVerification: () =>
    api.post('/auth/resend-verification').then((r) => r.data),

  getVerificationStatus: () =>
    api.get('/auth/verification-status').then((r) => r.data),

  forgotPassword: (email: string) =>
    api.post(passwordResetPath('/forgot-password'), { email }).then((r) => r.data),

  resetPassword: (token: string, password: string) =>
    api.post(passwordResetPath('/reset-password'), { token, password }).then((r) => r.data),

  deleteAccount: () => api.delete('/auth/me').then((r) => r.data),

  createCheckout: async (plan: string) => {
    try {
      const res = await api.post('/billing/create-checkout', { plan })
      return res.data
    } catch {
      throw new Error('Checkout failed. Please try again.')
    }
  },

  getNotifications: () => api.get('/notifications').then((r) => r.data),

  markNotificationRead: (id: string) => api.post(`/notifications/mark-read/${id}`).then((r) => r.data),

  getReferral: () => api.get('/auth/referral').then((r) => r.data),

  getBandwidthHistory: () => api.get('/analytics/bandwidth-history').then((r) => r.data),

  getAdminStats: () => api.get('/admin/stats').then((r) => r.data),

  getAdminUsers: (page: number) => api.get(`/admin/users?page=${page}&limit=20`).then((r) => r.data),

  updateUserPlan: (userId: string, plan: string) =>
    api.post(`/admin/users/${userId}/plan`, { plan }).then((r) => r.data),

  toggleAdmin: (userId: string) => api.post(`/admin/users/${userId}/toggle-admin`).then((r) => r.data),

  verifyUser: (userId: string) => api.post(`/admin/users/${userId}/verify`).then((r) => r.data),

  deleteUser: (userId: string) => api.delete(`/admin/users/${userId}`).then((r) => r.data),

  // NOTE: the API lives at /admin/health/status, NOT /admin/health — the
  // Next.js page at /admin/health SHADOWS the vercel.json /admin/* rewrite, so
  // a same-origin GET /admin/health returns the page's HTML instead of JSON
  // (the page then crashed on Object.values(html).checks). /admin/health/status
  // has no page, so the rewrite forwards it to the backend untouched.
  getAdminHealth: () => api.get('/admin/health/status').then((r) => r.data),

  // Paystack Transfers — admin payouts to M-PESA. All admin-only. Amounts the
  // backend stores/receives in KES cents; the UI works in whole KES.
  getPayoutBalance: () => api.get('/billing/payouts/balance').then((r) => r.data),

  listPayoutRecipients: () => api.get('/billing/payouts/recipients').then((r) => r.data),

  createPayoutRecipient: (data: { name: string; account_number: string; description?: string }) =>
    api.post('/billing/payouts/recipients', data).then((r) => r.data),

  deactivatePayoutRecipient: (recipientCode: string) =>
    api.delete(`/billing/payouts/recipients/${recipientCode}`).then((r) => r.data),

  listPayouts: () => api.get('/billing/payouts').then((r) => r.data),

  initiatePayout: (data: { recipient_code: string; amount: number; reason?: string; idempotency_key?: string }) =>
    api.post('/billing/payouts', data).then((r) => r.data),

  fetchPayout: (transferCode: string) =>
    api.get(`/billing/payouts/${transferCode}`).then((r) => r.data),
}

