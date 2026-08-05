/**
 * E2E: presign finalize auto-retry after a 502 (the Render cold-start case).
 *
 * The browser PUTs the file direct to Supabase, then POSTs
 * /upload/presign/finalize/{id}. That finalize is the last hop before the
 * success screen — and it is the hop that dies on a cold start: the request
 * either 502s at the Vercel proxy before reaching Render (mode A) or COMPLETES
 * server-side while the response is lost at the proxy (mode B). This suite
 * verifies the frontend re-sends the finalize and the upload still succeeds
 * through the direct path (no chunked fallback).
 *
 * Two modes (env FINALIZE_LOST_RESPONSE=1 for mode B):
 *   default  — the first finalize is blocked with a synthetic 502 (request
 *              never reaches the server). Proves the client-side retry.
 *   lost-req — the first finalize is forwarded with route.fetch() so it
 *              COMPLETES server-side (session consumed, uploads row created),
 *              then the browser is given a 502. The retried finalize re-sends
 *              the SAME upload_id and must be answered from the uploads row by
 *              the backend's idempotent lost-response path (WHERE id = $1 AND
 *              user_id = $2) — the regression the live upload-live probe
 *              caught on a cold start.
 *
 * Passes only if: the finalize request is issued >= 2 times, the retried
 * finalize returns 200, the success screen appears with a real 40-hex magnet
 * and embed code, the chunked fallback did NOT engage, and there are zero JS
 * page errors.
 */
import { chromium } from 'playwright'
import path from 'node:path'
import {
  BASE,
  ARTIFACTS_DIR,
  registerUser,
  cleanup,
  generateMedia,
  attachDiagnostics,
  report,
} from './helpers.mjs'

const LOST_RESPONSE = process.env.FINALIZE_LOST_RESPONSE === '1'
const failures = []
const out = {
  suite: 'finalize-retry',
  mode: LOST_RESPONSE ? 'lost-response (server completed, browser got 502)' : 'blocked (502 before server)',
  steps: [],
  apiCalls: [],
  consoleErrors: [],
  pageErrors: [],
  failedRequests: [],
  finalizeRequests: 0,
  retryUiSeen: false,
}
const FINALIZE_RE = /\/upload\/presign\/finalize\//

const browser = await chromium.launch({ headless: true })
let user = null
let fileId = null

try {
  const token = process.env.E2E_TOKEN
  if (!token) user = await registerUser('finalize-retry')
  const authToken = token || user.token
  const file = process.env.E2E_FILE
    ? { path: process.env.E2E_FILE, name: path.basename(process.env.E2E_FILE) }
    : generateMedia({ format: 'webm', duration: 3 })
  out.file = file.name
  out.fileSize = file.size

  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  await context.addInitScript((tok) => {
    window.sessionStorage.setItem('torrent_cdn_token', tok)
  }, authToken)
  const page = await context.newPage()
  attachDiagnostics(page, out)
  page.on('response', (r) => {
    const u = r.url()
    const m = r.request().method()
    if (u.includes('/upload/')) out.apiCalls.push(`[${r.status()}] ${m} ${u.replace(BASE, '')}`)
    else if (u.includes('supabase.co') && m === 'PUT') out.apiCalls.push(`[${r.status()}] PUT <supabase direct>`)
  })

  // Intercept ONLY the presign finalize. Every browser-issued finalize passes
  // through here (the route handler observes the client's auto-retries).
  await page.route(FINALIZE_RE, async (route) => {
    out.finalizeRequests++
    if (out.finalizeRequests === 1) {
      if (LOST_RESPONSE) {
        // Forward to the REAL backend so the finalize completes server-side
        // (session consumed, uploads row created — the browser sees none of
        // it), then hand the browser a 502 as if the response died at the
        // proxy. The client's retry re-sends the same upload_id with no
        // session left — exactly the cold-start signature. Generous fetch
        // timeout: a slow cold-start finalize (stream from storage + torrent +
        // ffmpeg server-side) must not time the handler out and turn the
        // expected [502, 200] trace into a spurious network error.
        await route.fetch({ timeout: 120000 })
      }
      await route.fulfill({ status: 502, contentType: 'text/plain', body: '502: proxy lost response' })
    } else {
      await route.continue()
    }
  })

  // 1) Upload page while logged in
  await page.goto(`${BASE}/dashboard/upload`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.waitForSelector('input[type="file"]', { timeout: 30000 })
  out.steps.push(`upload page loaded: ${page.url()}`)

  // 2) Start the upload
  await page.setInputFiles('input[type="file"]', file.path)
  out.steps.push('file selected, upload started')

  // 3) Watch for the retry UI text, then the success screen
  let success = false
  const deadline = Date.now() + 180000
  while (Date.now() < deadline) {
    const bodyText = await page.evaluate(() => document.body.innerText)
    if (bodyText.includes('Connection lost — retrying')) out.retryUiSeen = true
    if (bodyText.includes('Upload successful')) { success = true; break }
    await page.waitForTimeout(250)
  }

  // 4) Assertions
  out.success = success
  out.finalizeAttempts = out.finalizeRequests
  if (!success) {
    failures.push('success screen never appeared')
    out.finalPageTextTail = (await page.evaluate(() => document.body.innerText)).slice(-500)
  } else {
    const bodyText = await page.evaluate(() => document.body.innerText)
    const magnet = await page.evaluate(() => document.querySelector('input[readonly]')?.value || null)
    const embed = bodyText.match(/data-torrent-cdn="([0-9a-f-]{36})"/)?.[1] || null
    fileId = embed
    out.magnet = magnet
    out.fileId = embed
    out.realInfohash = !!magnet && /^magnet:\?xt=urn:btih:[0-9a-f]{40}/.test(magnet)

    // THE core assertion: the client re-sent the finalize after the 502.
    if (out.finalizeRequests < 2) failures.push(`finalize was not retried (${out.finalizeRequests} request(s))`)
    // The retried finalize must have landed a real 200 from the backend.
    const finalize200s = out.apiCalls.filter((c) => /\[200\] POST \/upload\/presign\/finalize/.test(c))
    if (!finalize200s.length) failures.push('no finalize request returned 200')
    out.finalizeStatuses = out.apiCalls
      .filter((c) => c.includes('/upload/presign/finalize'))
      .map((c) => c.split(']')[0].slice(1))
    if (LOST_RESPONSE && !out.finalizeStatuses.includes('502')) {
      failures.push('lost-response mode: the first finalize did not see a 502')
    }
    if (out.finalizeStatuses[0] !== '502') failures.push('the FIRST finalize was not a 502')

    // The direct path must have recovered on its own — no chunked fallback.
    const chunked = out.apiCalls.filter((c) => c.includes('/upload/session') || c.includes('/upload/chunk'))
    if (chunked.length) {
      failures.push(`chunked fallback engaged unexpectedly: ${chunked.join(' | ')}`)
    } else {
      out.directPathRecovered = true
    }

    if (!out.realInfohash) failures.push('success screen lacks a real 40-hex magnet')
    if (!fileId) failures.push('success screen lacks the embed code (data-torrent-cdn)')
    // Informational only: the "Connection lost — retrying…" indicator fires on
    // PUT/fallback retries (onRetry); finalize retries are intentionally silent
    // (the bytes are already in storage — resetting the bar would mislead).
    if (out.pageErrors.length) failures.push(`JS page errors: ${out.pageErrors.join(' | ')}`)

    out.screenshot = path.join(ARTIFACTS_DIR, LOST_RESPONSE ? 'finalize-retry-lost-response.png' : 'finalize-retry.png')
    await page.screenshot({ path: out.screenshot, fullPage: true })
  }
  await context.close()
} catch (e) {
  failures.push(`suite error: ${e.message}`)
} finally {
  await cleanup({ token: user?.token, fileId })
  await browser.close()
}

const passed = report(out, failures)
process.exit(passed ? 0 : 1)
