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
 *   E2E_FILE      path to a pre-made file — skips ffmpeg synthesis
 *   E2E_FILE_ID   (embed suite) existing file id — skips API upload
 *   FFMPEG_PATH   ffmpeg binary (default `ffmpeg`)
 */
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const BASE = (process.env.E2E_BASE_URL || 'https://www.torrentcdn.store').replace(/\/+$/, '')
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

/** Register a throwaway account; returns { email, token }. */
export async function registerUser(prefix = 'e2e') {
  const email = `${prefix}-${Date.now()}@example.com`
  const { ok, status, data } = await api('/auth/register', {
    method: 'POST',
    body: { email, password: 'testpass123', terms_accepted: true },
  })
  if (!ok || !data.access_token) {
    throw new Error(`register failed (${status}): ${JSON.stringify(data).slice(0, 300)}`)
  }
  return { email, token: data.access_token }
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
  for (let attempt = 1; attempt <= 2 && !finalized; attempt++) {
    const fin = await api(`/upload/presign/finalize/${presign.data.upload_id}`, { method: 'POST', token })
    if (fin.ok && (fin.data.id || fin.data.file_id)) {
      finalized = fin.data
    } else if (attempt === 1) {
      await new Promise((r) => setTimeout(r, 2000))
    }
  }
  if (!finalized) throw new Error('finalize failed after retries')
  return finalized // { id|file_id, torrent_magnet, minio_url, filename, ... }
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
