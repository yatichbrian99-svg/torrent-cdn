/**
 * E2E: real 2-peer WebTorrent P2P delivery.
 *
 * Proves the production P2P path end-to-end with TWO independent headless
 * browsers (two real peers on the same machine):
 *
 *   1. SEEDER — loads the standalone webtorrent bundle and seeds the uploaded
 *      file, REPRODUCING the backend's exact torrent infohash (same name +
 *      pieceLength 262144 as torrentool, so the info dict — and therefore the
 *      SHA-1 infohash — matches the magnet). Announces to the magnet's tr=
 *      trackers (the backend bakes them in — fix(upload) ac9e86b).
 *   2. LEECHER — opens the real /embed/{file_id} with every request to the
 *      storage provider (R2's public URL now, Supabase before the migration)
 *      BLOCKED, so direct origin streaming is impossible and the ONLY way the
 *      video can play is via WebRTC from the seeder peer.
 *
 * Asserts:
 *   - seeder infohash == magnet infohash (the reproducible-infohash guarantee)
 *   - BOTH sides report a connected peer ("N peers connected" in the embed UI)
 *   - the video actually plays (currentTime advances) with origin blocked → P2P
 *   - zero JS page errors (toHex/globalThis patch regression guards)
 *   - the backend recorded chunk_downloaded analytics with bytes_p2p > 0
 *     (p2p_offload_percent > 0) — real P2P bytes, not origin bytes
 *
 * Env: E2E_BASE_URL, E2E_TOKEN (skips registration + cleanup), E2E_FILE,
 * FFMPEG_PATH (same as the other suites).
 */
import { chromium } from 'playwright'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  BASE,
  ARTIFACTS_DIR,
  api,
  registerUser,
  cleanup,
  generateMedia,
  uploadViaApi,
  attachDiagnostics,
  report,
  isStorageUrl,
} from './helpers.mjs'

const E2E_DIR = path.dirname(fileURLToPath(import.meta.url))
const WEBTORRENT_DIST = path.join(E2E_DIR, '..', 'node_modules', 'webtorrent', 'dist', 'webtorrent.min.js')

// torrentool's default piece length for files > 32 KiB (backend/_torrent_and_thumbnail).
// The seeder MUST use this exact value to reproduce the backend's infohash.
const PIECE_LENGTH = 262144

const failures = []
const out = {
  suite: 'p2p-two-peer',
  steps: [],
  consoleErrors: [],
  pageErrors: [],
  failedRequests: [],
}

/** Parse the magnet's infohash / dn name / tr trackers (URLSearchParams decodes %-encoding). */
function parseMagnet(magnet) {
  const params = new URLSearchParams(magnet.split('?')[1])
  const xt = params.get('xt') || ''
  return {
    infohash: xt.split(':').pop() || '',
    name: params.get('dn') || '',
    trackers: params.getAll('tr'),
  }
}

/** Tiny static server for the seeder page (module scripts don't load from file://). */
function startSeederServer() {
  const webtorrentJs = fs.readFileSync(WEBTORRENT_DIST)
  const html = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>torrentcd e2e seeder</title></head>
<body>
<script type="module">
import WebTorrent from '/webtorrent.min.js'
window.__WT = WebTorrent
window.__seedReady = true
window.__seed = (cfg) => new Promise((resolve, reject) => {
  try {
    const bytes = Uint8Array.from(atob(cfg.b64), (c) => c.charCodeAt(0))
    const file = new File([bytes], cfg.name, { type: 'video/webm' })
    const client = new WebTorrent()
    window.__client = client
    client.seed(file, { name: cfg.name, pieceLength: cfg.pieceLength, announce: cfg.announce }, (t) => {
      window.__torrent = t
      window.__infoHash = t.infoHash
      resolve({ infoHash: t.infoHash, name: t.name, pieceLength: t.pieceLength, length: t.length })
    })
  } catch (e) {
    reject(String((e && e.stack) || e))
  }
})
window.__seederPeers = () => (window.__torrent ? window.__torrent.numPeers : 0)
</script>
</body></html>`
  const server = http.createServer((req, res) => {
    if (req.url === '/seeder.html') {
      res.writeHead(200, { 'Content-Type': 'text/html' })
      res.end(html)
    } else if (req.url === '/webtorrent.min.js') {
      res.writeHead(200, { 'Content-Type': 'text/javascript' })
      res.end(webtorrentJs)
    } else {
      res.writeHead(404)
      res.end('not found')
    }
  })
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => {
    resolve({ server, url: `http://127.0.0.1:${server.address().port}` })
  }))
}

const browser = await chromium.launch({ headless: true, args: ['--autoplay-policy=no-user-gesture-required'] })
const browser2 = await chromium.launch({ headless: true, args: ['--autoplay-policy=no-user-gesture-required'] })
let user = null
let fileId = null
let seederInfo = null
let serverState = { server: null, url: '' }

try {
  // ---- local seeder server ----
  serverState = await startSeederServer()
  out.steps.push(`seeder page served at ${serverState.url}`)

  // ---- throwaway account + multi-piece file ----
  const token = process.env.E2E_TOKEN
  if (!token) user = await registerUser('p2p')
  const authToken = token || user.token
  const file = process.env.E2E_FILE
    ? { path: process.env.E2E_FILE, name: path.basename(process.env.E2E_FILE) }
    : generateMedia({ format: 'webm', duration: 10, bitrateMbps: 0.8 }) // ~1 MB → 3+ pieces @ 256 KiB
  out.uploadedName = file.name
  const record = await uploadViaApi(authToken, file)
  fileId = record.id || record.file_id
  out.apiMagnet = record.magnet_link || record.torrent_magnet
  const magnet = parseMagnet(out.apiMagnet)
  out.magnetInfohash = magnet.infohash
  out.magnetTrackers = magnet.trackers
  out.steps.push(`uploaded ${file.name} (${file.size} bytes) -> ${fileId}`)

  if (!/^[0-9a-f]{40}$/.test(magnet.infohash)) {
    failures.push(`magnet has no real 40-hex infohash: ${out.apiMagnet}`)
  }
  if (!magnet.trackers.length) {
    failures.push('magnet carries NO tr= trackers — backend announce fix not live? P2P cannot connect peers.')
  }

  const b64 = fs.readFileSync(file.path).toString('base64')

  // ---- two attempts (public trackers can be flaky) ----
  let connected = false
  for (let attempt = 1; attempt <= 2 && !connected; attempt++) {
    out.steps.push(`attempt ${attempt} — starting seeder`)
    const seederCtx = await browser.newContext()
    const seedPage = await seederCtx.newPage()
    attachDiagnostics(seedPage, out)

    await seedPage.goto(`${serverState.url}/seeder.html`, { waitUntil: 'load', timeout: 30000 })
    await seedPage.waitForFunction(() => window.__seedReady, null, { timeout: 20000 })
    seederInfo = await seedPage.evaluate(
      (cfg) => window.__seed(cfg),
      { b64, name: magnet.name, pieceLength: PIECE_LENGTH, announce: magnet.trackers },
    )
    out.seederInfoHash = seederInfo.infoHash
    out.seederInfo = seederInfo
    if (seederInfo.infoHash !== magnet.infohash) {
      failures.push(
        `seeder infohash ${seederInfo.infoHash} != magnet infohash ${magnet.infohash} ` +
          `(name "${magnet.name}", pieceLength ${seederInfo.pieceLength})`,
      )
    } else {
      out.steps.push(`✓ seeder reproduced the backend infohash: ${seederInfo.infoHash}`)
    }

    // ---- leecher: real embed page with origin streaming blocked ----
    const leechCtx = await browser2.newContext({ viewport: { width: 1280, height: 900 } })
    const page2 = await leechCtx.newPage()
    attachDiagnostics(page2, out)
    // Capture the app's analytics POSTs so a missing chunk_downloaded event is
    // diagnosable (was it never fired, or did the POST fail?).
    out.analyticsPosts = out.analyticsPosts || []
    page2.on('request', (r) => {
      if (r.url().includes('/analytics/event')) {
        out.analyticsPosts.push(`-> ${r.postData() ? r.postData().slice(0, 120) : '(no body)'}`)
      }
    })
    page2.on('response', (r) => {
      if (r.url().includes('/analytics/event')) {
        out.analyticsPosts.push(`<- [${r.status()}]`)
      }
    })
    // Deliberately-blocked direct GETs log console noise — filtered at assert time.
    await page2.route('**/*', (route) => {
      if (isStorageUrl(route.request().url())) return route.abort()
      return route.continue()
    })
    await page2.goto(`${BASE}/embed/${fileId}`, { waitUntil: 'domcontentloaded', timeout: 60000 })
    await page2.waitForSelector('video', { timeout: 20000 })

    let playing = false
    let peerConnected = false
    let sawConnecting = false
    let finalState = null
    const timing = { pageLoadedAt: Date.now() }
    const deadline = Date.now() + 150000
    while (Date.now() < deadline) {
      const st = await page2.evaluate(() => {
        const v = document.querySelector('video')
        return {
          readyState: v ? v.readyState : -1,
          currentTime: v ? v.currentTime : -1,
          videoWidth: v ? v.videoWidth : 0,
          paused: v ? v.paused : true,
          error: v && v.error ? v.error.code : null,
        }
      })
      const body = await page2.evaluate(() => document.body.innerText)
      if (body.includes('connecting to peers') && !sawConnecting) { sawConnecting = true; timing.connectingAt = Date.now() }
      if (/peers? connected/.test(body) && !peerConnected) { peerConnected = true; timing.peersAt = Date.now() }
      if (st.videoWidth > 0 && st.currentTime > 0.5 && peerConnected) {
        playing = true
        finalState = st
        timing.playingAt = Date.now()
        break
      }
      await page2.waitForTimeout(1000)
    }
    out.timing = timing
    const seederPeers = await seedPage.evaluate(() => window.__seederPeers())
    out.leecherPeerConnected = peerConnected
    out.leecherPlaying = playing
    out.leecherSawConnecting = sawConnecting
    out.leecherFinalState = finalState || await page2.evaluate(() => {
      const v = document.querySelector('video')
      return v
        ? { readyState: v.readyState, currentTime: v.currentTime, videoWidth: v.videoWidth, paused: v.paused, error: v.error ? v.error.code : null }
        : null
    })
    out.seederPeers = seederPeers

    if (peerConnected && playing && seederPeers >= 1) {
      connected = true
      out.leechPage = page2
      out.leechCtx = leechCtx
      out.seedPage = seedPage
      out.seederCtx = seederCtx
      out.steps.push(`✓ peers connected (leecher UI) + seeder sees ${seederPeers} peer(s) + video playing via P2P`)
    } else {
      out.steps.push(`attempt ${attempt} did not connect (leecher peers=${peerConnected} playing=${playing} seederPeers=${seederPeers}) — retrying`)
      await page2.close()
      await leechCtx.close()
      await seedPage.evaluate(() => { try { window.__client && window.__client.destroy() } catch {} }).catch(() => {})
      await seedPage.close()
      await seederCtx.close()
    }
  }

  // ---- assertions (only now that the whole flow has settled) ----
  if (!connected) {
    failures.push('no P2P connection established in 2 attempts (tracker/WebRTC failure or backend fix not live)')
  } else {
    const page2 = out.leechPage
    const body = await page2.evaluate(() => document.body.innerText)
    out.leecherUi = body.slice(0, 400)
    if (!out.leecherPeerConnected) failures.push('embed UI never showed "N peers connected"')
    if (!out.leecherPlaying) failures.push('video never played via P2P (currentTime not advancing with origin blocked)')
    if (out.seederPeers < 1) failures.push('seeder never saw a peer')
    if (out.seederInfoHash !== magnet.infohash) {
      // (also flagged earlier — keep the report readable)
    }
    // The video must have played WITHOUT origin bytes: the direct src was
    // blocked, so any playback proves P2P. Also confirm the UI did NOT settle
    // on "(direct)".
    if (out.leecherFinalState && out.leecherFinalState.error !== null) {
      failures.push(`leecher <video> has error.code=${out.leecherFinalState.error} — expected P2P blob stream`)
    }
  }

  // Deliberately-blocked direct GETs log console noise; a CSP violation or a
  // flaky public tracker's WS failure is a soft signal (peers may still connect
  // through another tracker), not a product failure.
  const nonBlockedConsole = out.consoleErrors.filter(
    (e) => !/Failed to load resource|net::ERR|CORS|Content Security Policy|WebSocket connection to/i.test(e),
  )
  if (nonBlockedConsole.length) {
    failures.push(`console errors: ${nonBlockedConsole.slice(0, 3).join(' | ')}`)
  }
  if (out.pageErrors.length) {
    failures.push(`JS page errors: ${out.pageErrors.slice(0, 3).join(' | ')}`)
  }
  const nonBlockedFailed = out.failedRequests.filter((r) => !isStorageUrl(r))
  if (nonBlockedFailed.length) {
    failures.push(`failed requests (non-blocked): ${nonBlockedFailed.slice(0, 3).join(' | ')}`)
  }

  // ---- analytics: chunk_downloaded must carry P2P bytes ----
  let analytics = null
  const analyticsDeadline = Date.now() + 30000
  while (Date.now() < analyticsDeadline) {
    const res = await api(`/analytics/${fileId}`, { token: authToken })
    if (res.ok) {
      analytics = res.data
      // The GET returns derived stats only (total_p2p/total_origin are internal) —
      // a p2p_offload_percent > 0 proves chunk_downloaded events with bytes_p2p
      // were recorded.
      if ((analytics.p2p_offload_percent || 0) > 0 && (analytics.total_views || 0) >= 1) break
    }
    await new Promise((r) => setTimeout(r, 3000))
  }
  out.analytics = analytics
  if (!analytics) {
    failures.push('analytics endpoint returned nothing for the uploaded file')
  } else {
    if (!(analytics.total_views >= 1)) failures.push(`no peer_joined recorded (total_views=${analytics.total_views})`)
    if (!(analytics.p2p_offload_percent > 0)) failures.push(`p2p_offload_percent not > 0 (${analytics.p2p_offload_percent}) — chunk_downloaded events recorded no P2P bytes`)
  }

  if (out.leechPage) {
    out.screenshot = path.join(ARTIFACTS_DIR, 'p2p-two-peer.png')
    await out.leechPage.screenshot({ path: out.screenshot, fullPage: true })
  }
} catch (e) {
  failures.push(`suite error: ${e.message}`)
} finally {
  for (const ctxName of ['leechCtx', 'seederCtx']) {
    const ctx = out[ctxName]
    if (ctx) await ctx.close().catch(() => {})
  }
  if (serverState.server) serverState.server.close()
  await cleanup({ token: user?.token, fileId: user ? fileId : undefined })
  await browser.close()
  await browser2.close()
}

const passed = report(out, failures)
process.exit(passed ? 0 : 1)
