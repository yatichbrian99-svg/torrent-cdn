import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

const mocks = vi.hoisted(() => ({
  getMe: vi.fn(),
  getAdminHealth: vi.fn(),
  push: vi.fn(),
}))

vi.mock('@/lib/api', () => ({
  default: {
    getMe: mocks.getMe,
    getAdminHealth: mocks.getAdminHealth,
  },
}))
vi.mock('@/lib/auth', () => ({
  isLoggedIn: vi.fn(() => true),
}))
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mocks.push }),
}))

import AdminHealthPage from '../../app/admin/health/page'

// Exactly what the backend /admin/health returns today (including the WAF
// probe statuses the page was NOT designed for: vercel / cloudflare / vercel-firewall).
const realisticPayload = {
  generated_at: Math.floor(Date.now() / 1000),
  uptime_seconds: 123456,
  checks: {
    database: { status: 'ok', detail: 'SELECT 1 succeeded' },
    redis: { status: 'absent', detail: 'not configured / unreachable (fail-open fallbacks active)' },
    storage: { status: 'ok', detail: 'bucket reachable' },
  },
  waf: {
    status: 'vercel',
    detail: 'Vercel edge — no firewall header observed (managed rules may be off / Hobby plan)',
    headers: { 'x-vercel-firewall': 'off', server: 'Vercel' },
  },
  posture: [
    { check: 'CSP enforcement', status: 'pass', detail: 'enforcing' },
    { check: 'Admin bootstrap (/auth/setup)', status: 'warn', detail: 'ENABLED — first-admin bootstrap still open' },
    { check: 'Environment', status: 'warn', detail: 'development' },
  ],
  events: [
    { ts: Math.floor(Date.now() / 1000), type: 'login_failure', detail: 'bad@x.com', ip: '1.2.3.4' },
    { ts: Math.floor(Date.now() / 1000) - 120, type: 'admin_toggle', detail: 'aaa -> bbb = True', ip: '' },
  ],
}

describe('AdminHealth page', () => {
  beforeEach(() => {
    mocks.getMe.mockReset()
    mocks.getAdminHealth.mockReset()
    mocks.push.mockReset()
  })

  it('renders the dashboard without crashing on a realistic backend payload', async () => {
    mocks.getMe.mockResolvedValue({ is_admin: true })
    mocks.getAdminHealth.mockResolvedValue(realisticPayload)

    render(<AdminHealthPage />)

    // Header appears once data lands
    expect(await screen.findByText('Security & Health')).toBeTruthy()
    expect(screen.getByText('Database')).toBeTruthy()
    expect(screen.getByText('Redis')).toBeTruthy()
    expect(screen.getByText('Object storage')).toBeTruthy()
    expect(screen.getByText('Edge protection (WAF)')).toBeTruthy()
    expect(screen.getByText('Login failure')).toBeTruthy()
    expect(screen.getByText('Admin toggled')).toBeTruthy()
  })

  it('redirects when the user is not an admin', async () => {
    mocks.getMe.mockResolvedValue({ is_admin: false })

    render(<AdminHealthPage />)
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith('/'))
  })

  it('handles a payload where the WAF probe returned cloudflare', async () => {
    mocks.getMe.mockResolvedValue({ is_admin: true })
    mocks.getAdminHealth.mockResolvedValue({
      ...realisticPayload,
      waf: { status: 'cloudflare', detail: 'Cloudflare proxy detected (cf-ray header)', headers: { 'cf-ray': 'abc123' } },
    })

    render(<AdminHealthPage />)
    expect(await screen.findByText('Security & Health')).toBeTruthy()
    expect(screen.getAllByText(/cloudflare/i).length).toBeGreaterThan(0)
  })

  it('shows a graceful error instead of crashing on an HTML (shadowed-route) payload', async () => {
    mocks.getMe.mockResolvedValue({ is_admin: true })
    // This is EXACTLY what the old bug produced: the page at /admin/health
    // shadowed the API rewrite, so the fetch returned HTML, not JSON.
    mocks.getAdminHealth.mockResolvedValue('<html><body>Checking systems</body></html>')

    render(<AdminHealthPage />)
    expect(await screen.findByText('Failed to load health data')).toBeTruthy()
  })
})
