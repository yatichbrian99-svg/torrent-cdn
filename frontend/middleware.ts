import { NextRequest, NextResponse } from 'next/server'

/**
 * Nonce-based Content Security Policy (official Next.js pattern).
 *
 * Next.js inlines its RSC hydration payloads as <script> tags, which a strict
 * `script-src 'self'` would block. Next.js 14.x extracts the nonce from the
 * incoming request's `content-security-policy` header and stamps it onto every
 * script tag it emits — so we pass the CSP through the REQUEST headers (for the
 * renderer) and set it on the RESPONSE headers (for the browser).
 *
 * NOTE: this requires dynamic rendering (`export const dynamic = 'force-dynamic'`
 * in the root layout) — statically-prerendered pages would bake in scripts
 * without nonces.
 */
export function middleware(request: NextRequest) {
  const nonce = crypto.randomUUID().replace(/[^a-zA-Z0-9]/g, '')
  const isProduction = process.env.NODE_ENV === 'production'

  const connectSources = [
    "'self'",
    // The WebTorrent WSS trackers plus the storage host below are the
    // hardcoded cross-origin connections the app makes. API calls ideally route
    // through the same-origin Vercel proxy (NEXT_PUBLIC_API_URL defaults to
    // https://www.torrentcdn.store),
    // BUT the build-time env may point straight at the backend (e.g.
    // https://torrent-cdn.onrender.com). If that origin is cross-origin to the
    // page, it MUST be allow-listed here or the browser silently blocks every
    // fetch — which surfaces to users as 'Could not reach the server' with no
    // HTTP response, even though the API is healthy. Derived below so the CSP
    // can never drift from the configured API URL. Storage media/thumbnails are
    // governed by img-src/media-src (not connect-src). No bare https: — keeps
    // the blast radius of any script injection minimal.
    // These MUST match the tr= trackers the backend bakes into every magnet
    // (routes/upload.py `torrent.announce_urls`) — the embed player announces
    // to exactly that list, and a tracker not in connect-src is silently
    // blocked by CSP (P2P then never connects). fastcast.nz was dropped: it is
    // defunct.
    'wss://tracker.webtorrent.dev',
    'wss://tracker.openwebtorrent.com',
    'wss://tracker.btorrent.xyz',
    // File uploads go STRAIGHT to Supabase Storage via a presigned PUT (never
    // through the same-origin Vercel proxy, whose ~4.5MB body cap rejects real
    // videos). The presigned URL lives on the storage host (*.supabase.co) and
    // the browser PUTs to it directly — without this wildcard in connect-src
    // the browser would silently block the upload via CSP.
    'https://*.supabase.co',
    // Localhost is allowed for local dev (NEXT_PUBLIC_API_URL=http://localhost:8000);
    // never included in production.
    ...(isProduction ? [] : ['http://localhost:*']),
  ]

  // If NEXT_PUBLIC_API_URL is cross-origin (direct backend call instead of the
  // same-origin Vercel proxy), add exactly that origin to connect-src. Derived
  // from the same env var the axios client uses, so this can never silently
  // diverge from what the app actually calls.
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || ''
  let apiOrigin = ''
  try {
    apiOrigin = new URL(apiUrl).origin
  } catch {
    // Malformed/empty — 'self' (or localhost in dev) is all we allow.
  }
  if (apiOrigin && apiOrigin !== request.nextUrl.origin) {
    connectSources.push(apiOrigin)
  }

  const connectSourcesJoined = connectSources.join(' ')

  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    "style-src 'self' 'unsafe-inline' fonts.googleapis.com",
    "font-src 'self' fonts.gstatic.com",
    // https: allows cross-origin video thumbnails served from MinIO/Supabase
    // Storage (MINIO_PUBLIC_URL) — dev also allows localhost for local MinIO.
    `img-src 'self' data: blob: https:${isProduction ? '' : ' http://localhost:*'}`,
    // media-src allows https: so direct (origin/Cloudflare) playback works
    // regardless of the storage endpoint; blob: is used by WebTorrent streaming
    "media-src 'self' blob: https:",
    `connect-src ${connectSourcesJoined}`,
    // Embedding is SDK-based (the <div data-torrent-cdn> snippet + sdk.js — no
    // iframes anywhere in the app), so both forms of clickjacking protection
    // are safe to lock down. form-action 'self' keeps any future form payload
    // from being exfiltrated to a cross-origin endpoint.
    "frame-src 'self'",
    "frame-ancestors 'self'",
    "form-action 'self'",
    "worker-src 'self' blob:",
    "base-uri 'self'",
    "object-src 'none'",
  ].join('; ')

  // Pass the CSP through the request headers so Next.js's app renderer can
  // extract the nonce and stamp it onto its inline/static scripts.
  const requestHeaders = new Headers(request.headers)
  requestHeaders.set('content-security-policy', csp)

  const response = NextResponse.next({ request: { headers: requestHeaders } })
  response.headers.set('Content-Security-Policy', csp)
  return response
}

export const config = {
  // sdk.js and /vendor/* are static assets — no need to run middleware on them
  matcher: ['/((?!_next/static|_next/image|favicon.ico|sdk\.js|vendor/).*)'],
}
