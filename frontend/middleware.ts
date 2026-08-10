import { NextRequest, NextResponse } from 'next/server'
import { isScannerUserAgent } from './lib/security'

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
  // Edge-level scanner block — the no-DNS-change WAF gap closer. Known
  // attack-tooling UAs (sqlmap, nikto, nmap, …) get a 403 here, before the
  // page renders and before the Vercel rewrites forward /auth, /upload, etc.
  // to the backend. This is what makes check_waf.sh's probes (sqlmap/nikto
  // UAs) return 403 instead of 200. Generic clients (curl, python-requests)
  // are deliberately NOT blocked — the repo's own audit probes rely on them.
  if (isScannerUserAgent(request.headers.get('user-agent') || '')) {
    return new NextResponse('403 Forbidden — scanner traffic is not welcome here', {
      status: 403,
      headers: {
        'X-Content-Type-Options': 'nosniff',
        'Cache-Control': 'no-store',
      },
    })
  }

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
    // (torrent_builder.PUBLIC_TRACKERS) — the embed player announces
    // to exactly that list, and a tracker not in connect-src is silently
    // blocked by CSP (P2P then never connects). fastcast.nz was dropped (it is
    // defunct) and tracker.btorrent.xyz was dropped (live WS 500s; peers still
    // connect via the other two). (A stale NEXT_PUBLIC_API_URL pointing at a
    // direct backend host like https://torrent-cdn.onrender.com is NO LONGER
    // allow-listed in production — see the apiOrigin block below; lib/api.ts
    // ignores the env var in production, so the origin never needs to be in
    // this list.)
    'wss://tracker.webtorrent.dev',
    'wss://tracker.openwebtorrent.com',
    // File uploads go STRAIGHT to Supabase Storage via a presigned PUT (never
    // through the same-origin Vercel proxy, whose ~4.5MB body cap rejects real
    // videos). The presigned URL lives on the storage host (*.supabase.co) and
    // the browser PUTs to it directly — without this wildcard in connect-src
    // the browser would silently block the upload via CSP.
    'https://*.supabase.co',
    // R2 (the target of the 50MB-cap fix): once storage moves to Cloudflare
    // R2 the browser's presigned PUT goes straight to the R2 S3-compatible
    // endpoint (<account-id>.r2.cloudflarestorage.com) — same reason as the
    // Supabase wildcard: CSP would otherwise silently block the direct upload.
    'https://*.r2.cloudflarestorage.com',
    // R2 PUBLIC bucket URLs (<bucket-id>.r2.dev / pub-*.r2.dev): the demo page
    // FETCHES the video bytes from origin to seed them back into the WebTorrent
    // swarm (lib/seeder.ts) — a plain fetch() is governed by connect-src, so
    // without this wildcard the seed silently never starts (media playback is
    // media-src and already allowed via https:).
    // Localhost is allowed for local dev (NEXT_PUBLIC_API_URL=http://localhost:8000);
    // never included in production.
    ...(isProduction ? [] : ['http://localhost:*']),
    'https://*.r2.dev',
  ]

  // NEXT_PUBLIC_API_URL is ONLY honored in local dev — lib/api.ts hardcodes the
  // same-origin Vercel proxy in production (see its header comment), and a CI
  // guard (scripts/check_api_baseurl.sh) fails the frontend build if the
  // bundle's API base isn't same-origin. So in production a cross-origin API
  // origin must NEVER be allow-listed: a stale/compromised env value (e.g. a
  // dead direct-backend host) baking an extra origin into connect-src would
  // widen the CSP for zero functional benefit. Deny by default — the app
  // cannot call what the CSP blocks in production, and what it doesn't call
  // must not be allowed in.
  if (!isProduction) {
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
