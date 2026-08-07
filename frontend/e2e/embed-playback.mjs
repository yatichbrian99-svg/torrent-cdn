/**
 * E2E: embed page playback — the video player renders and streams.
 *
 * Uploads a real VP8/Opus WebM via the API (WebM is decodable in headless
 * Chromium, unlike H.264), then opens /embed/{file_id} and verifies:
 *   - the <video> element renders
 *   - playback actually starts (videoWidth > 0 and currentTime advances)
 *   - the player reaches the "(direct)" fallback (or "connecting to peers")
 *     state and streams the object from storage (R2's public URL now, Supabase
 *     before the migration) with an HTTP 200/206
 *   - ZERO JS page errors — in particular the WebTorrent P2P attempt must NOT
 *     throw `TypeError: e.toHex is not a function` (the uint8-util patch
 *     regression guard; see patches/uint8-util+2.3.2.patch)
 *   - zero failed requests
 *
 * Env: E2E_FILE_ID skips the API upload (uses an existing file).
 */
import { chromium } from 'playwright'
import path from 'node:path'
import {
  BASE,
  ARTIFACTS_DIR,
  registerUser,
  cleanup,
  generateMedia,
  uploadViaApi,
  attachDiagnostics,
  report,
  isStorageUrl,
} from './helpers.mjs'

const failures = []
const out = {
  suite: 'embed-playback',
  steps: [],
  consoleErrors: [],
  pageErrors: [],
  failedRequests: [],
  mediaResponses: [],
}

const browser = await chromium.launch({
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required'],
})
let user = null
let fileId = null

try {
  // Set up the file (or use E2E_FILE_ID)
  if (process.env.E2E_FILE_ID) {
    fileId = process.env.E2E_FILE_ID
    out.steps.push(`using existing file: ${fileId}`)
  } else {
    const token = process.env.E2E_TOKEN
    if (!token) user = await registerUser('embed')
    const authToken = token || user.token
    const file = process.env.E2E_FILE
      ? { path: process.env.E2E_FILE, name: path.basename(process.env.E2E_FILE) }
      : generateMedia({ format: 'webm', duration: 6 })
    out.uploadedName = file.name
    const record = await uploadViaApi(authToken, file)
    fileId = record.id || record.file_id
    out.steps.push(`uploaded ${file.name} (${file.size} bytes) -> ${fileId}`)
    out.apiMagnet = record.magnet_link || record.torrent_magnet
    if (!/^magnet:\?xt=urn:btih:[0-9a-f]{40}/.test(out.apiMagnet || '')) {
      failures.push('uploaded file record has no real 40-hex magnet')
    }
  }

  // Browser
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const page = await context.newPage()
  attachDiagnostics(page, out)
  page.on('response', (r) => {
    const u = r.url()
    if (isStorageUrl(u)) {
      out.mediaResponses.push(
        `[${r.status()}] ${r.request().method()} .../${u.split('/').slice(-2).join('/')} ct=${r.headers()['content-type'] || '?'}`,
      )
    }
  })

  // 1) Load the embed page
  await page.goto(`${BASE}/embed/${fileId}`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.waitForSelector('video', { timeout: 20000 })
  out.steps.push('video element rendered')

  // 2) Wait for either direct-fallback playback or WebTorrent P2P playback
  let playing = false
  let sawConnecting = false
  let sawDirect = false
  let finalState = null
  const deadline = Date.now() + 50000
  while (Date.now() < deadline) {
    const st = await page.evaluate(() => {
      const v = document.querySelector('video')
      return {
        readyState: v ? v.readyState : -1,
        currentTime: v ? v.currentTime : -1,
        videoWidth: v ? v.videoWidth : 0,
        videoHeight: v ? v.videoHeight : 0,
        duration: v ? v.duration : 0,
        paused: v ? v.paused : true,
        error: v && v.error ? v.error.code : null,
        src: v ? v.currentSrc || v.src || '' : '',
      }
    })
    const bodyText = await page.evaluate(() => document.body.innerText)
    if (bodyText.includes('connecting to peers')) sawConnecting = true
    if (bodyText.includes('(direct)')) sawDirect = true
    if (st.videoWidth > 0 && st.currentTime > 0.5) { playing = true; finalState = st; break }
    await page.waitForTimeout(500)
  }
  out.sawConnecting = sawConnecting
  out.sawDirect = sawDirect
  out.playing = playing
  out.finalState = finalState || await page.evaluate(() => {
    const v = document.querySelector('video')
    return v
      ? { readyState: v.readyState, currentTime: v.currentTime, videoWidth: v.videoWidth, videoHeight: v.videoHeight, duration: v.duration, paused: v.paused, error: v.error ? v.error.code : null, src: v.currentSrc || v.src || '' }
      : null
  })
  const expectedName = out.uploadedName || path.basename(process.env.E2E_FILE || '')
  out.filenameShown = expectedName ? (await page.evaluate(() => document.body.innerText)).includes(expectedName) : undefined

  // 3) Assertions
  if (!out.playing) failures.push('video never started playing (currentTime not advancing)')
  if (!(out.sawDirect || out.sawConnecting)) failures.push('player never showed connecting/direct state')
  if (out.pageErrors.length) {
    failures.push(`JS page errors: ${out.pageErrors.join(' | ')}`)
    if (out.pageErrors.some((e) => /toHex|fromHex/.test(e))) {
      failures.push('WebTorrent P2P path threw a toHex/fromHex error — uint8-util patch not effective in this bundle')
    }
  }
  if (out.failedRequests.length) failures.push(`failed requests: ${out.failedRequests.join(' | ')}`)
  if (!out.mediaResponses.some((r) => /\[20[06]\]/.test(r) && /PUT|GET/.test(r))) {
    failures.push('no successful media response from storage (direct streaming not observed)')
  }

  out.screenshot = path.join(ARTIFACTS_DIR, 'embed-playback.png')
  await page.screenshot({ path: out.screenshot, fullPage: true })
  await context.close()
} catch (e) {
  failures.push(`suite error: ${e.message}`)
} finally {
  // Only delete the file we created ourselves
  await cleanup({ token: user?.token, fileId: user ? fileId : undefined })
  await browser.close()
}

const passed = report(out, failures)
process.exit(passed ? 0 : 1)
