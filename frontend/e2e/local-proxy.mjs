// e2e/local-proxy.mjs
// Mirrors the Vercel same-origin proxy in production: serves the local `next start`
// frontend on :3000 and forwards backend paths (/api/* and /files) to the Render
// API, so E2E tests run against the production backend with the production
// same-origin API contract (no CORS, no cross-origin base URL).
//
// Usage:  node e2e/local-proxy.mjs        (expects next start on :3001)
//         E2E_API_URL=https://torrent-cdn.onrender.com E2E_BASE_URL=http://localhost:3000 node e2e/p2p-two-peer.mjs

import http from 'node:http'
import https from 'node:https'

const BACKEND = process.env.E2E_API_URL || 'https://torrent-cdn.onrender.com'
const FRONTEND = process.env.E2E_FRONTEND_PORT || 3001
const PORT = process.env.E2E_PROXY_PORT || 3000
const backendUrl = new URL(BACKEND)

const server = http.createServer((req, res) => {
  // Mirror vercel.json's rewrite list exactly (same-origin proxy contract):
  // backend paths are /api, /auth, /upload, /files, /analytics, /billing,
  // /admin, /notifications — everything else is the frontend.
  const isApi = /^\/(api|auth|upload|files|analytics|billing|admin|notifications)(\/|$)/.test(req.url)
  if (isApi) {
    const upstream = https.request({
      hostname: backendUrl.hostname,
      port: 443,
      path: req.url,
      method: req.method,
      headers: { ...req.headers, host: backendUrl.host },
    }, (up) => {
      res.writeHead(up.statusCode, up.headers)
      up.pipe(res)
    })
    upstream.on('error', (e) => { res.writeHead(502); res.end('proxy error: ' + e.message) })
    req.pipe(upstream)
    return
  }
  // Frontend — pass through to local next start, rewriting nothing.
  const fwd = http.request({
    hostname: '127.0.0.1',
    port: FRONTEND,
    path: req.url,
    method: req.method,
    headers: req.headers,
  }, (up) => {
    res.writeHead(up.statusCode, up.headers)
    up.pipe(res)
  })
  fwd.on('error', (e) => { res.writeHead(502); res.end('frontend error: ' + e.message) })
  req.pipe(fwd)
})

server.listen(PORT, () => console.log(`proxy listening on :${PORT} -> backend ${BACKEND}, frontend :${FRONTEND}`))
