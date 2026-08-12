import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// The demo page also drives real P2P seeding + a 5s live-stats poll; this suite
// targets ONLY the permalink restore + 'use live' recovery wiring, so the
// heavy deps (player, seeder, chart, API) are stubbed out.
const mocks = vi.hoisted(() => ({
  getPublicAnalytics: vi.fn(),
  getPublicFile: vi.fn(),
}))

vi.mock('@/lib/api', () => ({
  default: { getPublicAnalytics: mocks.getPublicAnalytics, getPublicFile: mocks.getPublicFile },
}))
vi.mock('@/lib/seeder', () => ({ startSeeding: vi.fn(() => Promise.resolve(null)) }))
vi.mock('@/components/VideoPlayer', () => ({ default: () => <div data-testid="demo-video" /> }))
// The offload chart needs ≥2 history points before it renders, but jsdom has no
// ResizeObserver — stub the imports so ResponsiveContainer can't crash.
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: any) => <div>{children}</div>,
  LineChart: ({ children }: any) => <div>{children}</div>,
  Line: () => <div />,
  XAxis: () => <div />,
  YAxis: () => <div />,
  CartesianGrid: () => <div />,
  Tooltip: () => <div />,
  ReferenceLine: () => <div />,
}))

import DemoPage from '../demo/page'

// Live swarm stats the public-analytics poll returns (62.4% offload snaps to
// 60 on the step-5 grid).
const LIVE = {
  total_views: 42,
  p2p_offload_percent: 62.4,
  bandwidth_saved_gb: 12.345,
  active_peers: 3,
  total_gb: 18.5,
  origin_gb: 6.9,
}

const goto = (qs: string) => window.history.replaceState(null, '', `/demo${qs}`)

describe('demo permalink restore + use-live recovery', () => {
  beforeEach(() => {
    // Live stats resolve on every run: the pinned-permalink tests prove the
    // pin WINS over this rate, and the invalid/alias tests just need the poll
    // to not blow up (the mount effect calls getPublicAnalytics immediately).
    mocks.getPublicAnalytics.mockReset().mockResolvedValue(LIVE)
    mocks.getPublicFile.mockReset().mockRejectedValue(new Error('no file'))
    goto('')
  })

  it('restores a pinned permalink exactly and does not clobber it with the live rate', async () => {
    goto('?v=5000&s=200&w=50&p=90')
    render(<DemoPage />)

    // The shared scenario lands as-is…
    expect(screen.getByText(/5,000 viewers × 200 MB × 50% avg watch =/)).toBeInTheDocument()
    expect(screen.getByText(/at 90% P2P offload/)).toBeInTheDocument()

    // …and once the live swarm rate arrives (62.4%), the pinned 90 must win —
    // a shared permalink restores what the sharer set, not the current swarm.
    await screen.findByText(/live swarm is 62\.4%/)
    expect(screen.getByText(/at 90% P2P offload/)).toBeInTheDocument()
  })

  it("'use live' recovers from a pinned permalink p2p to the live rate and syncs the URL", async () => {
    const user = userEvent.setup()
    goto('?v=5000&s=200&w=50&p=90')
    render(<DemoPage />)
    await screen.findByText(/live swarm is 62\.4%/)
    expect(screen.getByText(/at 90% P2P offload/)).toBeInTheDocument()

    // The recovery path: drop the pinned rate and go back to the live swarm.
    await user.click(screen.getByRole('button', { name: 'use live' }))

    // Snapped to the live rate on the 5% grid (62.4 → 60)…
    expect(await screen.findByText(/at 60% P2P offload/)).toBeInTheDocument()
    // …and the address bar now describes the recovered scenario, so the next
    // 'copy permalink' shares the live-rate scenario, not the pinned one.
    await waitFor(() => {
      expect(window.location.search).toContain('p=60')
      expect(window.location.search).toContain('v=5000')
      expect(window.location.search).toContain('s=200')
      expect(window.location.search).toContain('w=50')
    })
  })

  it('snaps p2p to the live rate when the permalink has no p2p param', async () => {
    goto('?v=5000&s=200&w=50')
    render(<DemoPage />)

    // No pinned p2p → the live-swarm default snap is allowed.
    await screen.findByText(/live swarm is 62\.4%/)
    expect(await screen.findByText(/at 60% P2P offload/)).toBeInTheDocument()
    await waitFor(() => expect(window.location.search).toContain('p=60'))
  })

  it('recovers to clamped defaults when a hand-edited permalink has invalid values', async () => {
    // v=abc (NaN) and s=-5 (≤0) fall back to defaults; w=999 clamps to 100;
    // p=101 clamps to the 95 slider max.
    goto('?v=abc&s=-5&w=999&p=101')
    render(<DemoPage />)
    // Let the mount stats poll settle inside act (the pinned p stays 95).
    await screen.findByText(/live swarm is 62\.4%/)
    expect(screen.getByText(/1,000 viewers × 100 MB × 100% avg watch =/)).toBeInTheDocument()
    expect(screen.getByText(/at 95% P2P offload/)).toBeInTheDocument()
  })

  it('accepts the long-form permalink aliases (viewers/size/watch/p2p)', async () => {
    goto('?viewers=2500&size=250&watch=75&p2p=40')
    render(<DemoPage />)
    await screen.findByText(/live swarm is 62\.4%/)
    expect(screen.getByText(/2,500 viewers × 250 MB × 75% avg watch =/)).toBeInTheDocument()
    expect(screen.getByText(/at 40% P2P offload/)).toBeInTheDocument()
  })

  it("'use live' before the live rate arrives falls back to the default rate", async () => {
    const user = userEvent.setup()
    goto('?p=90')
    // The live poll never resolves (no demo file / API down) — liveP2pPct
    // stays null, so the recovery path has no swarm rate to snap to.
    mocks.getPublicAnalytics.mockReset().mockRejectedValue(new Error('no demo file'))
    render(<DemoPage />)
    expect(screen.getByText(/at 90% P2P offload/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'use live' }))
    expect(await screen.findByText(/at 25% P2P offload/)).toBeInTheDocument()
  })
})
