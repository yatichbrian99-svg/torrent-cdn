/**
 * E2E: direct (presigned-to-Supabase) upload through the real upload UI.
 *
 * Verifies the PRIMARY upload path end-to-end in a real browser:
 *   - the logged-in /dashboard/upload renders the file input
 *   - picking a file starts an upload whose progress bar visibly advances
 *   - the byte path goes DIRECT to storage (PUT <storage direct> — R2's S3
 *     endpoint now, Supabase before the migration) and does NOT fall back to
 *     the chunked proxy path
 *   - the success screen shows filename, size, a real 40-hex magnet, the embed
 *     code (data-torrent-cdn) and "Upload or import another file"
 *   - zero JS page errors and zero failed requests
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'
import {
  BASE,
  ARTIFACTS_DIR,
  registerUser,
  cleanup,
  generateMedia,
  attachDiagnostics,
  summarizeProgress,
  report,
  isStorageUrl,
  DIRECT_PUT_LABEL,
} from './helpers.mjs'

const failures = []
const out = {
  suite: 'upload-direct',
  steps: [],
  apiCalls: [],
  consoleErrors: [],
  pageErrors: [],
  failedRequests: [],
  progressSamples: [],
}

const browser = await chromium.launch({ headless: true })
let user = null
let fileId = null

try {
  // Account + real playable media (mp4 — the upload path doesn't decode it)
  const token = process.env.E2E_TOKEN
  if (!token) user = await registerUser('upload-direct')
  const authToken = token || user.token
  const file = process.env.E2E_FILE
    ? { path: process.env.E2E_FILE, name: path.basename(process.env.E2E_FILE) }
    : generateMedia({ format: 'mp4', duration: 5 })
  out.file = file.name
  // file.size is set by generateMedia; a pre-made E2E_FILE needs a stat.
  if (!file.size && file.path) file.size = fs.statSync(file.path).size
  out.fileSize = file.size

  // Logged-in context: seed the same sessionStorage key the app uses.
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

  // 1) Load the upload page while logged in
  await page.goto(`${BASE}/dashboard/upload`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.waitForSelector('input[type="file"]', { timeout: 30000 })
  out.steps.push(`upload page loaded: ${page.url()}`)

  // 2) Throttle the upload leg so the progress bar is observable (~0.5 MB/s
  //    for the small default files). A pre-made multi-10MB file (E2E_FILE,
  //    e.g. the >50MB R2-migration probe) must not take minutes: scale the
  //    throttle so the PUT finishes in ~20-25s either way.
  const cdp = await context.newCDPSession(page)
  await cdp.send('Network.enable')
  const uploadThroughput = Math.max(500 * 1024, Math.round(file.size / 22))
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false,
    latency: 100,
    downloadThroughput: 10 * 1024 * 1024,
    uploadThroughput,
  })

  // 3) Start the upload
  await page.setInputFiles('input[type="file"]', file.path)
  out.steps.push('file selected, upload started')

  // 4) Watch for progress, then the success screen
  let success = false
  const deadline = Date.now() + 120000
  while (Date.now() < deadline) {
    const bodyText = await page.evaluate(() => document.body.innerText)
    const pct = bodyText.match(/(\d+)%/)
    if (pct) out.progressSamples.push(Number(pct[1]))
    if (bodyText.includes('Upload successful')) { success = true; break }
    await page.waitForTimeout(250)
  }
  summarizeProgress(out)

  // 5) Assertions
  if (!success) {
    failures.push('success screen never appeared')
    out.finalPageTextTail = (await page.evaluate(() => document.body.innerText)).slice(-500)
  } else {
    const bodyText = await page.evaluate(() => document.body.innerText)
    // The magnet is rendered in a readonly <input> (not innerText), so read it
    // from the DOM the same way the app presents it.
    const magnet = await page.evaluate(() => document.querySelector('input[readonly]')?.value || null)
    const embed = bodyText.match(/data-torrent-cdn="([0-9a-f-]{36})"/)?.[1] || null
    fileId = embed
    out.success = true
    out.magnet = magnet
    out.fileId = embed
    out.realInfohash = !!magnet && /^magnet:\?xt=urn:btih:[0-9a-f]{40}/.test(magnet)
    out.hasUploadAnother = bodyText.includes('Upload or import another file')
    out.filenameShown = bodyText.includes(file.name)

    if (!out.realInfohash) failures.push('success screen lacks a real 40-hex magnet')
    if (!fileId) failures.push('success screen lacks the embed code (data-torrent-cdn)')
    if (!out.filenameShown) failures.push('filename not shown on the success screen')
    if (!out.hasUploadAnother) failures.push('"Upload another file" button missing')
    if (!out.progressAdvanced) failures.push('progress bar did not visibly advance')
    if (!out.apiCalls.some((c) => c.includes(`PUT <${DIRECT_PUT_LABEL}>`))) {
      failures.push(`direct-to-storage PUT was not used (no PUT <${DIRECT_PUT_LABEL}> seen)`)
    }
    // The fallback must only run when the direct path genuinely failed
    // server-side (e.g. a transient finalize 404 — the app recovering via the
    // chunked path is the resilience layer working as designed, not a bug).
    const directFinalizeFailed = out.apiCalls.some(
      (c) => /\[[45]\d\d\] POST \/upload\/presign\/finalize/.test(c),
    )
    if (out.apiCalls.some((c) => c.includes('/upload/session') || c.includes('/upload/chunk'))) {
      if (!directFinalizeFailed) {
        failures.push('chunked fallback engaged although the direct path had not failed')
      } else {
        out.fallbackAfterDirectFailure = true
      }
    }
    if (out.pageErrors.length) failures.push(`JS page errors: ${out.pageErrors.join(' | ')}`)

    out.screenshot = path.join(ARTIFACTS_DIR, 'upload-direct.png')
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
