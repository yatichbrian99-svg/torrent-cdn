/**
 * E2E: upload → embed success screen within a TIME BUDGET.
 *
 * Regression guard for the upload-finalize latency work (commit 8d35c6d): the
 * presign path used to re-write the video to a second temp file, run ffmpeg
 * with a slow seek (decoding from frame 0), and do all of it synchronously on
 * the event loop — which showed up as "the embed takes ~10 seconds" after the
 * progress bar hit 100%. This suite drives the REAL upload UI and asserts the
 * "Upload successful" screen (with the embed code + real magnet) appears
 * within EMBED_BUDGET_SECONDS (default 10), recording how long the finalize
 * leg itself took as a diagnostic.
 *
 * BUDGET NOTE (default 20s): the VirusTotal scan is BLOCKING by design — every
 * fresh-file finalize submits to VT and polls every 5s (up to 30s) before the
 * success screen can appear. Measured live: a 1.1MB upload's finalize is
 * ~6.5s (≈5s VT poll + submit/hash), so the full UI-to-success time is ~15s
 * with the speed fixes in. The 20s default catches severe regressions (a
 * return of the old double-write + slow-seek path, or a broken finalize)
 * without being flaky against the intentional VT tax. Raise it only for very
 * slow networks.
 *
 * Env overrides (in addition to the shared helpers):
 *   EMBED_BUDGET_SECONDS  max seconds from file selection → success screen
 *                         (default 20 — see BUDGET NOTE above)
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
  waitForUploadPageHydration,
  report,
  isStorageUrl,
  DIRECT_PUT_LABEL,
} from './helpers.mjs'

const BUDGET_MS = (Number(process.env.EMBED_BUDGET_SECONDS) || 20) * 1000

const failures = []
const out = {
  suite: 'embed-speed',
  steps: [],
  apiCalls: [],
  consoleErrors: [],
  pageErrors: [],
  failedRequests: [],
  budgetSeconds: BUDGET_MS / 1000,
  timings: {},
}

const browser = await chromium.launch({ headless: true })
let user = null
let fileId = null

try {
  // Account + real playable media (mp4 — finalize runs ffmpeg on it, so the
  // thumbnail leg is exercised; headless Chromium can't decode H.264 but the
  // upload path doesn't need to).
  const token = process.env.E2E_TOKEN
  if (!token) user = await registerUser('embed-speed')
  const authToken = token || user.token
  const file = process.env.E2E_FILE
    ? { path: process.env.E2E_FILE, name: path.basename(process.env.E2E_FILE) }
    : generateMedia({ format: 'mp4', duration: 5 })
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
    else if (isStorageUrl(u) && m === 'PUT') out.apiCalls.push(`[${r.status()}] PUT <${DIRECT_PUT_LABEL}>`)
  })

  // Time the FINALIZE leg precisely (the torrent-hash + thumbnail step that
  // used to be the ~10s culprit): pair each POST /upload/presign/finalize/{id}
  // request with its response. The client auto-retries on a transient 502, so
  // several legs may exist — report the longest (the real processing one).
  let openFinalizeAt = null
  const finalizeLegs = []
  page.on('request', (r) => {
    if (r.method() === 'POST' && /\/upload\/presign\/finalize\//.test(r.url())) {
      openFinalizeAt = Date.now()
    }
  })
  page.on('response', (r) => {
    if (r.request().method() === 'POST' && /\/upload\/presign\/finalize\//.test(r.url())) {
      if (openFinalizeAt !== null) {
        finalizeLegs.push(Date.now() - openFinalizeAt)
        openFinalizeAt = null
      }
    }
  })

  // 1) Load the upload page while logged in. The file input exists in the SSR
  //    HTML before React hydrates, so setInputFiles fired on a not-yet-hydrated
  //    input dispatches no onChange (the upload silently never starts).
  //    waitForUploadPageHydration waits for a CLIENT-ONLY signal — the
  //    logged-in navbar's "Logout" — before touching the input, then settles
  //    so the handler can attach. (Shared with selectFileForUpload so the
  //    hydration logic can't drift between suites.)
  await page.goto(`${BASE}/dashboard/upload`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await waitForUploadPageHydration(page)
  out.steps.push(`upload page loaded + hydrated: ${page.url()}`)

  // 2) Start the upload and time EVERYTHING from here to the success screen.
  //    No CDP throttle: this suite measures latency, not progress observability.
  const t0 = Date.now()
  await page.setInputFiles('input[type="file"]', file.path)
  out.steps.push('file selected, clock started')

  let success = false
  // Hard stop far past the budget (the budget itself is the assertion); the
  // app's upload timeout is 45s+ per leg, so never wait on a stuck suite.
  const hardDeadline = t0 + BUDGET_MS + 30000
  while (Date.now() < hardDeadline) {
    const bodyText = await page.evaluate(() => document.body.innerText)
    if (bodyText.includes('Upload successful')) { success = true; break }
    await page.waitForTimeout(100)
  }
  const totalMs = Date.now() - t0
  out.timings.totalMs = totalMs
  out.timings.finalizeMs = finalizeLegs.length ? Math.max(...finalizeLegs) : null
  out.timings.finalizeLegs = finalizeLegs
  out.steps.push(success ? `success screen after ${totalMs}ms` : `TIMED OUT after ${totalMs}ms`)

  // 3) Assertions
  if (!success) {
    failures.push('success screen never appeared')
    out.finalPageTextTail = (await page.evaluate(() => document.body.innerText)).slice(-500)
  } else {
    const bodyText = await page.evaluate(() => document.body.innerText)
    const magnet = await page.evaluate(() => document.querySelector('input[readonly]')?.value || null)
    const embed = bodyText.match(/data-torrent-cdn="([0-9a-f-]{36})"/)?.[1] || null
    fileId = embed
    out.success = true
    out.magnet = magnet
    out.fileId = embed
    out.realInfohash = !!magnet && /^magnet:\?xt=urn:btih:[0-9a-f]{40}/.test(magnet)
    out.hasUploadAnother = bodyText.includes('Upload or import another file')

    // THE time-budget assertion.
    if (totalMs > BUDGET_MS) {
      failures.push(`embed success screen took ${totalMs}ms — exceeds the ${BUDGET_MS}ms budget`)
    } else {
      out.withinBudget = true
    }
    if (!out.realInfohash) failures.push('success screen lacks a real 40-hex magnet')
    if (!fileId) failures.push('success screen lacks the embed code (data-torrent-cdn)')
    if (!out.hasUploadAnother) failures.push('"Upload another file" button missing')
    if (out.pageErrors.length) failures.push(`JS page errors: ${out.pageErrors.join(' | ')}`)

    out.screenshot = path.join(ARTIFACTS_DIR, 'embed-speed.png')
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
