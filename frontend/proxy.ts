import { NextRequest, NextResponse } from 'next/server'

/**
 * Same-origin API proxy with edge authentication.
 *
 * Layers ON TOP of the vercel.json rewrites (kept as the zero-auth fallback):
 * when BACKEND_ORIGIN + EDGE_PROXY_AUTH are set, the middleware handles the
 * proxying itself and stamps a shared secret (`x-edge-proxy-auth`) onto every
 * proxied request — middleware runs before filesystem/rewrite handling, so
 * the vercel.json rules then never fire. With the env unset this helper
 * returns null and the old vercel.json behavior applies unchanged, so the
 * rollout is safe in any order.
 *
 * The backend trusts the client IP only from requests carrying that secret
 * — this is what keeps per-IP rate limiting per-USER behind the Vercel proxy
 * (whose egress IPs are dynamic and cannot be allow-listed; without this,
 * every user shares one rate-limit bucket and intermittently sees 429s).
 *
 * The secret travels in the EDGE→BACKEND hop only; it is never exposed to
 * browsers (middleware runs server-side on Vercel's edge runtime).
 *
 * Env: EDGE_PROXY_AUTH (secret, same value as backend) + BACKEND_ORIGIN
 * (e.g. https://torrent-cdn.onrender.com).
 */

/** Backend origin resolved at call time (edge runtime reads env lazily). */
const backendOrigin = () => (process.env.BACKEND_ORIGIN || '').replace(/\/$/, '')

/** Path prefixes proxied to the backend (mirror of the old vercel.json). */
const PROXY_PREFIXES = [
  '/api/',
  '/auth/',
  '/upload',
  '/files',
  '/analytics/',
  '/billing/',
  '/admin/',
  '/notifications',
] as const

/** Returns a proxying response, or null when the path isn't proxied. */
export function proxyToBackend(request: NextRequest): NextResponse | null {
  const BACKEND = backendOrigin()
  if (!BACKEND) return null
  const { pathname, search } = request.nextUrl
  const hit = PROXY_PREFIXES.some(
    (p) => pathname === p.replace(/\/$/, '') || pathname.startsWith(p),
  )
  if (!hit) return null

  // Destination parity with the old vercel.json rewrites:
  //   /api/live/*  → backend /api/live/*  (prefix KEPT — live routes are
  //                  mounted at /api/live on the backend)
  //   /api/*       → backend /*           (prefix STRIPPED — backend routes
  //                  are mounted without /api, e.g. /api/auth/login → /auth/login)
  //   everything else → unchanged
  let destPath = pathname
  if (pathname.startsWith('/api/') && !pathname.startsWith('/api/live/')) {
    destPath = pathname.slice(4) // strip '/api'
  } else if (pathname === '/api') {
    destPath = '/'
  }

  const headers = new Headers(request.headers)
  if (process.env.EDGE_PROXY_AUTH) {
    headers.set('x-edge-proxy-auth', process.env.EDGE_PROXY_AUTH)
  }
  // Let the backend see the ORIGINAL host (the rewrite changes it upstream).
  headers.set('x-forwarded-host', request.headers.get('host') || '')

  return NextResponse.rewrite(new URL(`${destPath}${search}`, BACKEND), {
    request: { headers },
  })
}
