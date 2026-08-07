/**
 * Shared helpers for the Playwright E2E suite (frontend/e2e).
 *
 * The suite targets the PRODUCTION site (override with E2E_BASE_URL) and drives
 * a real headless Chromium against the live UI. Every run creates a throwaway
 * account + test file and deletes them in cleanup, so it is safe to run
 * repeatedly against the real deployment.
 *
 * Prerequisites:
 *   - Playwright's Chromium installed: `npx playwright install chromium`
 *     (playwright has no postinstall, so `npm ci` never downloads browsers)
 *   - ffmpeg on PATH (to synthesize real, playable test media)
 *
 * Env overrides:
 *   E2E_BASE_URL  base URL under test (default https://www.torrentcdn.store)
 *   E2E_TOKEN     existing account token — skips registration AND user cleanup
 *   E2E_ADMIN_TOKEN  admin token used to verify a freshly registered throwaway
 *                    account (POST /admin/users/{id}/verify) when email
 *                    verification is enforced in production
 *   E2E_FILE      path to a pre-made file — skips ffmpeg synthesis
 *   E2E_FILE_ID   (embed suite) existing file id — skips API upload
 *   FFMPEG_PATH   ffmpeg binary (default `ffmpeg`)
 */
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const BASE = (process.env.E2E_BASE_URL || 'https://www.torrentcdn.store').replace(/\/+$/, '')

// Storage-host matcher. The direct-to-storage PUT and the origin media streams
// live on the storage provider's hosts — Cloudflare R2 now (the S3 endpoint
// *.r2.cloudflarestorage.com for the browser PUT, and the public base
// pub-*.r2.dev for media), Supabase before the R2 migration (*.supabase.co).
// Suites that assert "bytes went direct to storage" or that BLOCK origin
// streaming to force P2P must match ALL of these hosts, so they stay valid
// whichever provider is live (and a rollback to Supabase is caught instead of
// silently passing the wrong assertion).
//
// Forward-looking: when the production CDN domain (cdn.torrentcdn.store) goes
// live and MINIO_PUBLIC_URL points there, media URLs stop matching the defaults
// below and the P2P suites would silently stop proving P2P (origin NOT blocked
// → false pass) while embed-playback would false-fail. Add the new host either
// via E2E_STORAGE_HOSTS (comma-separated substrings, e.g. "cdn.torrentcdn.store")
// or by editing the defaults — mirroring the probes' EXPECTED_STORAGE_PREFIX.
const EXTRA_HOSTS = (process.env.E2E_STORAGE_HOSTS || '')
  .split(',').map((s) => s.trim()).filter(Boolean)
export const STORAGE_HOST_RE = new RegExp(
  ['supabase\\.co', 'r2\\.cloudflarestorage\\.com', '\\.r2\\.dev', ...EXTRA_HOSTS.map((h) => h.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))].join('|'),
  'i',
)
export const isStorageUrl = (u) => STORAGE_HOST_RE.test(u)
export const DIRECT_PUT_LABEL = 'storage direct'
// API calls normally go to BASE (the same-origin Vercel proxy). Override with
// E2E_API_URL to hit a different backend directly — used to validate a NEW
// frontend build locally (E2E_BASE_URL=http://localhost:3000) against the live
// Render backend while Vercel is mid-deploy.
export const API_BASE = (process.env.E2E_API_URL || BASE).replace(/\/+$/, '')
export const ARTIFACTS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '.artifacts')
export const MEDIA_DIR = path.join(ARTIFACTS_DIR, 'media')

export function ensureArtifacts() {
  fs.mkdirSync(MEDIA_DIR, { recursive: true })
}

/** Minimal JSON fetch helper against the API (Bearer auth, JSON body). */
export async function api(pathname, { method = 'GET', token, body } = {}) {
  const res = await fetch(`${API_BASE}${pathname}`, {
    method,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const text = await res.text()
  let data = {}
  try {
    data = text ? JSON.parse(text) : {}
  } catch {
    data = { raw: text.slice(0, 200) }
  }
  return { ok: res.ok, status: res.status, data }
}

/**
 * Decode the `sub` (user id) claim from an access-token JWT payload.
 * Deliberately NO signature verification — this only extracts the id needed
 * to address the admin-verify endpoint; the admin call itself authenticates
 * with E2E_ADMIN_TOKEN. Base64url payload segment; never throws.
 */
export function jwtSub(token) {
  try {
    const payload = token.split('.')[1]
    const json = Buffer.from(payload, 'base64url').toString('utf8')
    return JSON.parse(json).sub || null
  } catch {
    return null
  }
}

/**
 * Register a throwaway account; returns { email, token }.
 *
 * Production now restricts a register-issued token for an UNVERIFIED account
 * to the verification endpoints (the session gate). So a freshly registered
 * account can't upload until its email is verified. Two paths make the suite
 * work anyway:
 *   - No email service configured → register auto-verifies, token works.
 *   - Email service configured → the account must be admin-verified. When
 *     E2E_ADMIN_TOKEN (an admin user's token) is set, this helper verifies
 *     the throwaway account via POST /admin/users/{id}/verify so the suites
 *     can proceed. Without it and with email enforced, the first protected
 *     call will 403 — pass E2E_ADMIN_TOKEN or use E2E_TOKEN (shared verified
 *     account, which skips registration entirely).
 */
export async function registerUser(prefix = 'e2e') {
  const email = `${prefix}-${Date.now()}@example.com`
  // Retry once or twice on 429: production registers are limited to ~10
  // accounts/hr/IP, and a burst of suites can trip it transiently. Backoff
  // rides that out; an exhausted budget fails with an actionable message
  // (the old code surfaced a confusing "register failed (429)" suite error).
  let last = null
  for (let attempt = 1; attempt <= 3; attempt++) {
    const res = await api('/auth/register', {
      method: 'POST',
      body: { email, password: 'testpass123', terms_accepted: true },
    })
    if (res.ok && res.data.access_token) {
      const token = res.data.access_token
      const adminToken = process.env.E2E_ADMIN_TOKEN
      const userId = jwtSub(token)
      if (adminToken && userId) {
        const v = await api(`/admin/users/${userId}/verify`, { method: 'POST', token: adminToken })
        if (!v.ok) {
          throw new Error(`admin-verify failed (${v.status}): ${JSON.stringify(v.data).slice(0, 300)}`)
        }
        const detail = v.data?.detail || ''
        if (!/verified/i.test(detail)) {
          throw new Error(`admin-verify unexpected response: ${detail.slice(0, 200)}`)
        }
      } else if (!adminToken) {
        // No admin token: warn so a failing suite points at the config, not the gate.
        console.warn(`[registerUser] no E2E_ADMIN_TOKEN set — if email verification is enforced, ${prefix} suite will 403 on protected calls. Use E2E_TOKEN or E2E_ADMIN_TOKEN.`)
      }
      return { email, token }
    }
    last = res
    if (res.status === 429 && attempt < 3) {
      await new Promise((r) => setTimeout(r, attempt * 3000))
      continue
    }
    break
  }
  if (last?.status === 429) {
    throw new Error(
      `register rate-limited (429 — ~10 accounts/hr/IP): the ${prefix} suite couldn't create an account. ` +
      'Wait for the window to reset, or reuse a verified shared account via E2E_TOKEN.',
    )
  }
  throw new Error(`register failed (${last?.status}): ${JSON.stringify(last?.data).slice(0, 300)}`)
}

/** Best-effort cleanup: delete the uploaded file and/or the throwaway user.
 *
 * Suites pass the token of the account THEY created. When E2E_TOKEN is set,
 * user is null and suites pass token: undefined — but the uploaded test file
 * must STILL be deleted (a previous version leaked every E2E_TOKEN run's
 * file, which then polluted later suites' dashboard state). The account
 * itself is only deleted when this run created it (token param), never when
 * E2E_TOKEN is reused across suites. */
export async function cleanup({ token, fileId } = {}) {
  const authToken = token || process.env.E2E_TOKEN
  if (fileId && authToken) {
    try {
      await api(`/files/${fileId}`, { method: 'DELETE', token: authToken })
    } catch {}
  }
  if (token) {
    try {
      await api('/auth/me', { method: 'DELETE', token })
    } catch {}
  }
}

/**
 * Synthesize a real, playable media file with ffmpeg.
 *  - format 'mp4'  → H.264/AAC (plays in full Chrome; NOT decodable in headless
 *    Chromium, which lacks proprietary codecs — fine for the upload suites)
 *  - format 'webm' → VP8/Opus (plays in headless Chromium — required by the
 *    embed/playback suite)
 */
export function generateMedia({ format = 'mp4', duration = 5, bitrateMbps } = {}) {
  ensureArtifacts()
  const ffmpeg = process.env.FFMPEG_PATH || 'ffmpeg'
  const ext = format === 'webm' ? 'webm' : 'mp4'
  const filePath = path.join(MEDIA_DIR, `${ext}_${Date.now()}.${ext}`)
  const videoCodec = ext === 'webm' ? 'libvpx' : 'libx264'
  const audioCodec = ext === 'webm' ? 'libopus' : 'aac'
  const bitrate = `${bitrateMbps ?? (ext === 'webm' ? 0.5 : 2)}M`
  const args = [
    '-y',
    '-f', 'lavfi', '-i', `testsrc2=duration=${duration}:size=640x360:rate=30`,
    '-f', 'lavfi', '-i', `sine=frequency=440:duration=${duration}`,
    '-c:v', videoCodec, '-preset', 'veryfast', '-b:v', bitrate,
    '-c:a', audioCodec, '-shortest', filePath,
  ]
  const r = spawnSync(ffmpeg, args, { stdio: 'ignore' })
  if (r.status !== 0) {
    fs.rmSync(filePath, { force: true })
    throw new Error(
      'ffmpeg failed to synthesize test media — install ffmpeg (e.g. `apt install ffmpeg` / `brew install ffmpeg`)',
    )
  }
  const stats = fs.statSync(filePath)
  return { path: filePath, name: path.basename(filePath), size: stats.size }
}

/**
 * Upload a file via the API exactly as the app does: presign → direct PUT to
 * Supabase Storage → finalize. Returns the finalized file record.
 * Finalize retries once (Render cold-start blips are known to 500 the first try).
 */
export async function uploadViaApi(token, file, { filename } = {}) {
  const name = filename || file.name
  const content_type = name.endsWith('.webm') ? 'video/webm' : 'video/mp4'
  // A pre-made E2E_FILE is just { path, name } — the size needs a stat.
  // generateMedia always sets .size, so this is a no-op for synthesized files.
  if (!file.size && file.path) file.size = fs.statSync(file.path).size
  const presign = await api('/upload/presign', {
    method: 'POST',
    token,
    body: { filename: name, file_size: file.size, content_type },
  })
  const presignUrl = presign.data.presigned_url || presign.data.url
  if (!presign.ok || !presign.data.upload_id || !presignUrl) {
    throw new Error(`presign failed (${presign.status}): ${JSON.stringify(presign.data).slice(0, 300)}`)
  }
  const put = await fetch(presignUrl, {
    method: 'PUT',
    headers: { 'Content-Type': content_type },
    body: fs.readFileSync(file.path),
  })
  if (!put.ok) throw new Error(`direct-to-storage PUT failed (${put.status})`)

  let finalized = null
  for (let attempt = 1; attempt <= 3 && !finalized; attempt++) {
    const fin = await api(`/upload/presign/finalize/${presign.data.upload_id}`, { method: 'POST', token })
    if (fin.ok && (fin.data.id || fin.data.file_id)) {
      finalized = fin.data
    } else if (attempt < 3) {
      // Render cold starts can outlast a single 2s retry (observed: finalize
      // blipped for >2s in the embed suite) — back off longer on the second.
      await new Promise((r) => setTimeout(r, attempt === 1 ? 2000 : 6000))
    }
  }
  if (!finalized) throw new Error('finalize failed after retries')
  return finalized // { id|file_id, torrent_magnet, minio_url, filename, ... }
}

/**
 * Select a file in the /dashboard/upload UI and confirm the upload STARTED.
 *
 * Regression guard for the file-picker flake: the <input type=file> exists in
 * the SSR HTML before React hydrates, and a setInputFiles on the unhydrated
 * input dispatches NO onChange — the upload silently never starts ("success
 * screen never appeared" with zero presign call). Two layers of defense:
 *   1. HYDRATION WAIT — "Logout" only renders after client code reads the
 *      sessionStorage token (seeded by addInitScript), so it's a reliable
 *      client-hydrated signal; a short settle then lets the input's change
 *      handler attach (this is what embed-speed already did and it never
 *      flakes).
 *   2. START CONFIRM — after selecting, poll for proof the upload started:
 *      the uploader UI ("Uploading..."/the retry label) and/or the suite's
 *      own API-call observer (`sawUploadApi`, e.g. a presign response having
 *      landed in its apiCalls collector). If nothing fires within a few
 *      seconds the picker raced the unhydrated input — re-select once (a
 *      second setInputFiles on the hydrated input reliably dispatches
 *      onChange).
 *
 * Throws if the upload still hasn't started after the bounded re-select, so
 * the suite fails with an actionable message instead of a timeout waiting for
 * a success screen that was never going to appear.
 */
/**
 * Wait for the upload page's React hydration before touching the file input.
 *
 * The <input type=file> exists in the SSR HTML before React hydrates; a
 * setInputFiles on the unhydrated input dispatches NO onChange, so the upload
 * silently never starts (the "success screen never appeared with zero presign
 * call" flake). "Logout" renders only after client code reads the
 * sessionStorage token (seeded by addInitScript), so it's a reliable
 * client-hydration signal; the settle lets the input's change handler attach.
 */
export async function waitForUploadPageHydration(page, { settleMs = 800 } = {}) {
  await page.waitForSelector('input[type="file"]', { timeout: 30000 })
  await page.waitForSelector('text=Logout', { timeout: 15000 })
  await page.waitForTimeout(settleMs)
}

export async function selectFileForUpload(page, filePath, sawUploadApi = null) {
  await waitForUploadPageHydration(page)

  const uploadUiVisible = async () => {
    const body = await page.evaluate(() => document.body.innerText)
    return /Uploading|Connection lost/i.test(body)
  }
  for (let attempt = 1; attempt <= 2; attempt++) {
    await page.setInputFiles('input[type="file"]', filePath)
    const deadline = Date.now() + 10000
    while (Date.now() < deadline) {
      if (await uploadUiVisible()) return attempt
      if (sawUploadApi && sawUploadApi()) return attempt
      await page.waitForTimeout(250)
    }
  }
  throw new Error('upload never started after selecting the file (input onChange never fired — picker raced an unhydrated input)')
}

/** Attach console/page-error/request-failed collectors that write into `out`. */
export function attachDiagnostics(page, out) {
  page.on('console', (m) => {
    if (m.type() === 'error') out.consoleErrors.push(m.text().slice(0, 200))
  })
  page.on('pageerror', (e) => out.pageErrors.push(String(e).slice(0, 300)))
  page.on('requestfailed', (r) => {
    out.failedRequests.push(`${r.method()} ${r.url().slice(0, 140)} :: ${r.failure()?.errorText}`)
  })
  return page
}

/** Reduce the raw progress samples into a summary (min/max/advanced). */
export function summarizeProgress(out) {
  const distinct = [...new Set(out.progressSamples)]
  out.progressDistinct = distinct
  out.progressAdvanced = distinct.length >= 2
  out.progressMin = distinct.length ? Math.min(...distinct) : 0
  out.progressMax = distinct.length ? Math.max(...distinct) : 0
}

/** Print the suite report as JSON and return whether it passed. */
export function report(out, failures) {
  const passed = failures.length === 0
  console.log(JSON.stringify({ ...out, failures, passed }, null, 2))
  return passed
}
