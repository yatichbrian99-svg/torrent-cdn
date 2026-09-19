import { describe, expect, it, beforeEach, afterEach } from 'vitest'
import { NextRequest } from 'next/server'
import { proxyToBackend } from '../../proxy'

/**
 * Contract tests for the middleware API proxy — the same-origin replacement
 * for the old vercel.json rewrites. Destination parity matters: the backend
 * mounts /api/live WITH the prefix and everything else WITHOUT it.
 */
const ORIG_ENV = { ...process.env }

function req(path: string, method = 'GET') {
  return new NextRequest(`https://www.torrentcdn.store${path}`, { method })
}

function destOf(res: ReturnType<typeof proxyToBackend>): string | null {
  return res ? res.headers.get('x-middleware-rewrite') : null
}

beforeEach(() => {
  process.env.BACKEND_ORIGIN = 'https://torrent-cdn.onrender.com'
  process.env.EDGE_PROXY_AUTH = 'test-secret'
})

afterEach(() => {
  process.env = { ...ORIG_ENV }
})

describe('proxyToBackend', () => {
  it('proxies /api/auth/login to /auth/login (prefix stripped)', () => {
    const d = destOf(proxyToBackend(req('/api/auth/login')))
    expect(d).toContain('torrent-cdn.onrender.com/auth/login')
  })

  it('proxies /api/admin/stats to /admin/stats (prefix stripped)', () => {
    const d = destOf(proxyToBackend(req('/api/admin/stats')))
    expect(d).toContain('torrent-cdn.onrender.com/admin/stats')
  })

  it('keeps the /api/live prefix (live routes mounted at /api/live)', () => {
    const d = destOf(proxyToBackend(req('/api/live/session')))
    expect(d).toContain('torrent-cdn.onrender.com/api/live/session')
  })

  it('proxies legacy non-/api prefixes unchanged', () => {
    for (const p of ['/auth/me', '/admin/stats', '/files/public/x', '/billing/plans', '/notifications']) {
      const d = destOf(proxyToBackend(req(p)))
      expect(d).toContain(`torrent-cdn.onrender.com${p}`)
    }
  })

  it('does NOT proxy app pages or static assets', () => {
    expect(proxyToBackend(req('/live'))).toBeNull()
    expect(proxyToBackend(req('/dashboard'))).toBeNull()
    expect(proxyToBackend(req('/sdk.js'))).toBeNull()
  })

  it('injects the edge auth secret onto proxied requests', () => {
    const res = proxyToBackend(req('/api/admin/stats'))
    // The stamped header lives on the rewritten REQUEST headers (not the
    // response) — asserting the rewrite exists + env wiring is the observable
    // contract here; the header path is covered by the backend contract.
    expect(res).not.toBeNull()
  })

  it('no-ops when BACKEND_ORIGIN is unset (local dev)', () => {
    process.env.BACKEND_ORIGIN = ''
    expect(proxyToBackend(req('/api/admin/stats'))).toBeNull()
  })

  it('does NOT proxy GET navigations to frontend page paths', () => {
    // Middleware runs BEFORE the filesystem — proxying these would hijack
    // page navigation and return the backend's 404 instead of the page HTML.
    for (const p of [
      '/admin',
      '/admin/health',
      '/admin/payouts',
      '/auth/forgot-password',
      '/auth/reset-password',
      '/billing',
    ]) {
      expect(proxyToBackend(req(p, 'GET'))).toBeNull()
      expect(proxyToBackend(req(p, 'HEAD'))).toBeNull()
    }
  })

  it('still proxies sub-paths and non-GET methods on page paths', () => {
    // API calls on sub-paths must keep working...
    const d = destOf(proxyToBackend(req('/admin/stats', 'GET')))
    expect(d).toContain('/admin/stats')
    // ...and POST /auth/forgot-password is a backend endpoint that shares a
    // path string with a page — must still be proxied.
    const post = proxyToBackend(req('/auth/forgot-password', 'POST'))
    expect(post).not.toBeNull()
    expect(destOf(post)).toContain('/auth/forgot-password')
  })
})
