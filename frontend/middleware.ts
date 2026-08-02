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
    // The three WebTorrent WSS trackers are the only cross-origin connections
    // the app makes. API calls route through the same-origin Vercel proxy
    // (NEXT_PUBLIC_API_URL defaults to https://www.torrentcdn.store — any
    // cross-origin override in production would be blocked by this CSP, so
    // keep the proxy model), and storage media/thumbnails are governed by
    // img-src/media-src (not connect-src). No bare https: — keeps the blast
    // radius of any script injection minimal.
    'wss://tracker.openwebtorrent.com',
    'wss://tracker.btorrent.xyz',
    'wss://tracker.fastcast.nz',
    // Localhost is allowed for local dev (NEXT_PUBLIC_API_URL=http://localhost:8000);
    // never included in production.
    ...(isProduction ? [] : ['http://localhost:*']),
  ].join(' ')

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
    `connect-src ${connectSources}`,
    "frame-src 'self'",
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
