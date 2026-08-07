/**
 * E2E: auto-seed after upload (the uploader's tab becomes the seeder).
 *
 * The product guarantee: after `api.uploadFile` succeeds, the uploader's
 * browser seeds the freshly-uploaded file back into the WebTorrent swarm, so
 * the FIRST viewer can play via P2P without a manual seeder. This suite proves
 * it end-to-end with TWO independent headless browsers:
 *
 *   1. UPLOADER — logs in, uploads a real file through the /dashboard/upload UI
 *      (the app's own presign→PUT→finalize flow). The UploadForm then seeds the
 *      file via lib/seeder.ts (magnet `dn` name + torrentool piece-length rule
 *      + magnet `tr=` trackers), so the uploader's tab is a seeder.
 *   2. LEECHER — opens /embed/{file_id} in a SECOND browser with every request
 *      to the storage provider (R2's public URL now, Supabase before the
 *      migration) BLOCKED (direct origin streaming is impossible), so the ONLY
 *      way the video can play is via WebRTC from the uploader's seed. It must
 *      show "N peers connected" and advance currentTime.
 *
 * Asserts:
 *   - the success screen shows the "seeding…" indicator (auto-seed engaged)
 *   - the leecher connects to the uploader's seed and plays (origin blocked)
 *   - the backend recorded chunk_downloaded analytics with bytes_p2p > 0
 *
 * Env: E2E_BASE_URL, E2E_TOKEN, E2E_FILE, FFMPEG_PATH (same as other suites).
 */
import { chromium } from 'playwright'
import path from 'node:path'
import {
  BASE,
  ARTIFACTS_DIR,
  api,
  registerUser,
  cleanup,
  generateMedia,
  attachDiagnostics,
  selectFileForUpload,
  report,
  isStorageUrl,
} from './helpers.mjs'

const failures = []
const out = {
  suite: 'auto-seed',
  steps: [],
  apiCalls: [],
  consoleErrors: [],
  pageErrors: [],
  failedRequests: [],
  analyticsPosts: [],
}

const browser = await chromium.launch({ headless: true, args: ['--autoplay-policy=no-user-gesture-required'] })
const browser2 = await chromium.launch({ headless: true, args: ['--autoplay-policy=no-user-gesture-required'] })
let user = null
let fileId = null

try {
  // ---- throwaway account + playable media (webm plays in headless Chromium) ----
  const token = process.env.E2E_TOKEN
  if (!token) user = await registerUser('autoseed')
  const authToken = token || user.token
  const file = process.env.E2E_FILE
    ? { path: process.env.E2E_FILE, name: path.basename(process.env.E2E_FILE) }
    : generateMedia({ format: 'webm', duration: 8, bitrateMbps: 0.8 }) // ~1 MB → 3+ pieces @ 256 KiB
  out.file = file.name
  out.fileSize = file.size

  // ---- 1. UPLOADER: upload through the real UI + auto-seed ----
  const upCtx = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  await upCtx.addInitScript((tok) => {
    window.sessionStorage.setItem('torrent_cdn_token', tok)
  }, authToken)
  const upPage = await upCtx.newPage()
  attachDiagnostics(upPage, out)
  upPage.on('response', (r) => {
    const u = r.url()
    if (u.includes('/upload/')) out.apiCalls.push(`[${r.status()}] ${r.request().method()} ${u.replace(BASE, '')}`)
  })

  await upPage.goto(`${BASE}/dashboard/upload`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  // selectFileForUpload waits for React hydration before touching the input
  // (a setInputFiles on the unhydrated SSR input dispatches no onChange — the
  // "success screen never appeared" flake) and confirms the presign call
  // fired, re-selecting once if the picker raced.
  await selectFileForUpload(upPage, file.path, () => out.apiCalls.some((c) => c.includes('POST /upload/presign')))
  out.steps.push('uploader: file selected, upload started')

  let success = false
  const uploadDeadline = Date.now() + 120000
  while (Date.now() < uploadDeadline) {
    const body = await upPage.evaluate(() => document.body.innerText)
    if (body.includes('Upload successful')) { success = true; break }
    await upPage.waitForTimeout(250)
  }
  if (!success) failures.push('uploader: success screen never appeared')

  const magnet = success
    ? await upPage.evaluate(() => document.querySelector('input[readonly]')?.value || null)
    : null
  const embed = success
    ? await upPage.evaluate(() => document.body.innerText.match(/data-torrent-cdn="([0-9a-f-]{36})"/)?.[1] || null)
    : null
  fileId = embed
  out.magnet = magnet
  out.fileId = embed
  out.realInfohash = !!magnet && /^magnet:\?xt=urn:btih:[0-9a-f]{40}/.test(magnet)
  if (!out.realInfohash) failures.push('uploader: success screen lacks a real 40-hex magnet')
  if (!fileId) failures.push('uploader: success screen lacks the embed code')

  // The seeding indicator must appear (auto-seed engaged in the uploader tab).
  let sawSeeding = false
  let seedingToPeers = false
  const seedDeadline = Date.now() + 30000
  while (Date.now() < seedDeadline) {
    const body = await upPage.evaluate(() => document.body.innerText)
    if (/seeding this file to peers|seeding to \d+ peers?/.test(body)) {
      sawSeeding = true
      if (/seeding to \d+ peers?/.test(body)) seedingToPeers = true
      break
    }
    await upPage.waitForTimeout(500)
  }
  out.sawSeeding = sawSeeding
  out.seedingToPeers = seedingToPeers
  if (!sawSeeding) failures.push('uploader: "seeding…" indicator never appeared after upload')
  out.steps.push(`uploader: auto-seed engaged (sawSeeding=${sawSeeding}, peers=${seedingToPeers})`)

  // ---- 2. LEECHER: embed in a second browser with origin streaming blocked ----
  if (success && fileId) {
    const leechCtx = await browser2.newContext({ viewport: { width: 1280, height: 900 } })
    const page2 = await leechCtx.newPage()
    attachDiagnostics(page2, out)
    page2.on('request', (r) => {
      if (r.url().includes('/analytics/event')) {
        out.analyticsPosts.push(`-> ${r.postData() ? r.postData().slice(0, 120) : '(no body)'}`)
      }
    })
    page2.on('response', (r) => {
      if (r.url().includes('/analytics/event')) out.analyticsPosts.push(`<- [${r.status()}]`)
    })
    await page2.route('**/*', (route) => {
      if (isStorageUrl(route.request().url())) return route.abort()
      return route.continue()
    })
    await page2.goto(`${BASE}/embed/${fileId}`, { waitUntil: 'domcontentloaded', timeout: 60000 })
    await page2.waitForSelector('video', { timeout: 20000 })

    let peerConnected = false
    let playing = false
    const deadline = Date.now() + 150000
    while (Date.now() < deadline) {
      const st = await page2.evaluate(() => {
        const v = document.querySelector('video')
        return {
          readyState: v ? v.readyState : -1,
          currentTime: v ? v.currentTime : -1,
          videoWidth: v ? v.videoWidth : 0,
          error: v && v.error ? v.error.code : null,
        }
      })
      const body = await page2.evaluate(() => document.body.innerText)
      if (/peers? connected/.test(body)) peerConnected = true
      if (st.videoWidth > 0 && st.currentTime > 0.5 && peerConnected) { playing = true; break }
      await page2.waitForTimeout(1000)
    }
    out.leecherPeerConnected = peerConnected
    out.leecherPlaying = playing
    if (!peerConnected) failures.push('leecher: never connected to the uploader\'s seed (auto-seed swarm failed)')
    if (!playing) failures.push('leecher: video never played via P2P with origin blocked')

    out.leechScreenshot = path.join(ARTIFACTS_DIR, 'auto-seed-leecher.png')
    await page2.screenshot({ path: out.leechScreenshot, fullPage: true }).catch(() => {})
    await leechCtx.close()
  }

  // The uploader's tab is the seeder — it must stay open until the leecher is done.
  out.uploaderScreenshot = path.join(ARTIFACTS_DIR, 'auto-seed-uploader.png')
  await upPage.screenshot({ path: out.uploaderScreenshot, fullPage: true }).catch(() => {})

  // ---- analytics: chunk_downloaded must carry P2P bytes ----
  if (fileId) {
    let analytics = null
    const analyticsDeadline = Date.now() + 30000
    while (Date.now() < analyticsDeadline) {
      const res = await api(`/analytics/${fileId}`, { token: authToken })
      if (res.ok) {
        analytics = res.data
        if ((analytics.p2p_offload_percent || 0) > 0 && (analytics.total_views || 0) >= 1) break
      }
      await new Promise((r) => setTimeout(r, 3000))
    }
    out.analytics = analytics
    if (!analytics) failures.push('analytics endpoint returned nothing')
    else if (!(analytics.p2p_offload_percent > 0)) {
      failures.push(`p2p_offload_percent not > 0 (${analytics.p2p_offload_percent}) — no P2P bytes recorded`)
    }
  }

  // Console/page-error guards (blocked-origin noise filtered).
  const nonBlockedConsole = out.consoleErrors.filter(
    (e) => !/Failed to load resource|net::ERR|CORS|Content Security Policy|WebSocket connection to/i.test(e),
  )
  if (nonBlockedConsole.length) failures.push(`console errors: ${nonBlockedConsole.slice(0, 3).join(' | ')}`)
  if (out.pageErrors.length) failures.push(`JS page errors: ${out.pageErrors.slice(0, 3).join(' | ')}`)

  await upCtx.close()
} catch (e) {
  failures.push(`suite error: ${e.message}`)
} finally {
  await cleanup({ token: user?.token, fileId })
  await browser.close()
  await browser2.close()
}

const passed = report(out, failures)
process.exit(passed ? 0 : 1)
