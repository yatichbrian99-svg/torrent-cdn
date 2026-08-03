/**
 * E2E: chunked fallback upload (Vercel proxy) when the direct Supabase PUT dies.
 *
 * Blocks every *.supabase.co request at the network layer, forcing the app's
 * resilience path: one transient retry of the direct PUT → "Connection lost —
 * retrying…" → chunked upload through /upload/session + /upload/chunk/* +
 * /upload/finalize. Verifies:
 *   - the retry label appears and the upload recovers on its own
 *   - progress animates across both chunks (with the 50% plateau at the
 *     chunk handoff)
 *   - the success screen renders with a real 40-hex magnet
 *   - the chunked-stored object is finalized (no orphan)
 *   - no JS page errors (the intentionally blocked PUTs are expected network
 *     errors, not page errors)
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
  summarizeProgress,
  report,
} from './helpers.mjs'

const failures = []
const out = {
  suite: 'upload-fallback',
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
  const token = process.env.E2E_TOKEN
  if (!token) user = await registerUser('upload-fallback')
  const authToken = token || user.token

  // ~7.5MB mp4 → two 4MB chunks
  const file = process.env.E2E_FILE
    ? { path: process.env.E2E_FILE, name: path.basename(process.env.E2E_FILE) }
    : generateMedia({ format: 'mp4', duration: 20, bitrateMbps: 3 })
  out.file = file.name
  out.fileSize = file.size

  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  await context.addInitScript((tok) => {
    window.sessionStorage.setItem('torrent_cdn_token', tok)
  }, authToken)

  // BLOCK every request to Supabase Storage → the direct PUT dies at the
  // network layer, which must trigger the chunked fallback.
  await context.route('**supabase.co**', (route) => {
    out.apiCalls.push(`[BLOCKED] ${route.request().method()} <supabase direct>`)
    return route.abort('failed')
  })

  const page = await context.newPage()
  attachDiagnostics(page, out)
  page.on('response', (r) => {
    const u = r.url()
    if (u.includes('/upload/')) out.apiCalls.push(`[${r.status()}] ${r.request().method()} ${u.replace(BASE, '')}`)
  })

  // 1) Load the upload page while logged in
  await page.goto(`${BASE}/dashboard/upload`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.waitForSelector('input[type="file"]', { timeout: 30000 })
  out.steps.push(`upload page loaded: ${page.url()}`)

  // 2) Throttle so progress across the two chunks is observable (~2 MB/s)
  const cdp = await context.newCDPSession(page)
  await cdp.send('Network.enable')
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false,
    latency: 50,
    downloadThroughput: 20 * 1024 * 1024,
    uploadThroughput: 2 * 1024 * 1024,
  })

  // 3) Start the upload
  await page.setInputFiles('input[type="file"]', file.path)
  out.steps.push('file selected, upload started (supabase PUTs are BLOCKED)')

  // 4) Watch for the retry label, progress, then success
  let success = false
  let retrySeen = false
  const deadline = Date.now() + 180000
  while (Date.now() < deadline) {
    const bodyText = await page.evaluate(() => document.body.innerText)
    const pct = bodyText.match(/(\d+)%/)
    if (pct) out.progressSamples.push(Number(pct[1]))
    if (bodyText.includes('Connection lost')) retrySeen = true
    if (bodyText.includes('Upload successful')) { success = true; break }
    await page.waitForTimeout(250)
  }
  summarizeProgress(out)
  out.retrySeen = retrySeen

  // 5) Assertions
  if (!success) {
    failures.push('success screen never appeared after the fallback')
    out.finalPageTextTail = (await page.evaluate(() => document.body.innerText)).slice(-600)
  } else {
    const bodyText = await page.evaluate(() => document.body.innerText)
    const magnet = await page.evaluate(() => document.querySelector('input[readonly]')?.value || null)
    const embed = bodyText.match(/data-torrent-cdn="([0-9a-f-]{36})"/)?.[1] || null
    fileId = embed
    out.success = true
    out.magnet = magnet
    out.fileId = embed
    out.realInfohash = !!magnet && /^magnet:\?xt=urn:btih:[0-9a-f]{40}/.test(magnet)
    out.hasUploadAnother = bodyText.includes('Upload another file')
    out.filenameShown = bodyText.includes(file.name)

    if (!retrySeen) failures.push('"Connection lost — retrying…" label never appeared')
    if (!out.realInfohash) failures.push('success screen lacks a real 40-hex magnet')
    if (!fileId) failures.push('success screen lacks the embed code')
    if (!out.progressAdvanced) failures.push('progress bar did not visibly advance')
    if (!out.apiCalls.some((c) => c.includes('POST /upload/session'))) {
      failures.push('chunked fallback never started (/upload/session missing)')
    }
    if (!out.apiCalls.some((c) => c.includes('/upload/chunk/'))) {
      failures.push('no chunk upload calls (/upload/chunk/* missing)')
    }
    if (!out.apiCalls.some((c) => c.includes('/upload/finalize'))) {
      failures.push('no finalize call — the chunked upload never completed')
    }
    // Blocked PUTs are expected to log console errors — anything else is a bug
    const unexpectedConsole = out.consoleErrors.filter(
      (c) => !/supabase|ERR_FAILED|net::ERR/i.test(c),
    )
    if (unexpectedConsole.length) failures.push(`unexpected console errors: ${unexpectedConsole.join(' | ')}`)
    if (out.pageErrors.length) failures.push(`JS page errors: ${out.pageErrors.join(' | ')}`)

    out.screenshot = path.join(ARTIFACTS_DIR, 'upload-fallback.png')
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
