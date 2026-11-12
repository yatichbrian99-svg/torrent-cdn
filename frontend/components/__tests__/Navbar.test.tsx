import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

const mocks = vi.hoisted(() => ({
  getMe: vi.fn(),
  getNotifications: vi.fn(),
  get: vi.fn(),
  isLoggedIn: vi.fn(),
  removeToken: vi.fn(),
  useRouter: vi.fn(() => ({ push: vi.fn() })),
}))

vi.mock('@/lib/api', () => ({
  // The Navbar pulls in lib/aiConsole for the gated AI link, which imports
  // API_BASE from this module and probes the console status through `get`.
  API_BASE: '',
  default: {
    getMe: mocks.getMe,
    getNotifications: mocks.getNotifications,
    get: mocks.get,
  },
}))
vi.mock('@/lib/auth', () => ({
  isLoggedIn: mocks.isLoggedIn,
  removeToken: mocks.removeToken,
}))
vi.mock('next/navigation', () => ({
  useRouter: () => mocks.useRouter(),
}))
// The auth modal is only rendered when the user opens it — never in these tests.
vi.mock('../AuthModal', () => ({
  default: () => null,
}))

import Navbar from '../Navbar'
import { resetAiFeatureProbe } from '@/lib/aiConsole'

describe('Navbar session handling', () => {
  beforeEach(() => {
    mocks.getMe.mockReset()
    mocks.getNotifications.mockReset()
    // The AI flag probe fails closed, i.e. the link stays hidden.
    mocks.get.mockReset()
    mocks.get.mockRejectedValue(new Error('AI disabled'))
    mocks.isLoggedIn.mockReset()
    mocks.removeToken.mockReset()
    mocks.useRouter.mockReset()
    mocks.useRouter.mockReturnValue({ push: vi.fn() })
  })

  it('shows the AI link only when the console status probe answers', async () => {
    mocks.isLoggedIn.mockReturnValue(true)
    mocks.getMe.mockResolvedValue({ id: 'u1', email: 'a@b.c' })
    mocks.getNotifications.mockResolvedValue([])
    mocks.get.mockRejectedValue(new Error('AI disabled'))

    const { unmount } = render(<Navbar />)
    await waitFor(() => expect(mocks.getMe).toHaveBeenCalled())
    expect(screen.queryByRole('link', { name: 'AI' })).toBeNull()
    unmount()

    resetAiFeatureProbe()
    mocks.get.mockResolvedValue({ data: { feature_flag: { on: true } } })
    render(<Navbar />)
    expect(await screen.findByRole('link', { name: 'AI' })).toHaveAttribute('href', '/ai')
  })

  it('does NOT clear the session when getMe fails with 429 (rate limit)', async () => {
    mocks.isLoggedIn.mockReturnValue(true)
    mocks.getMe.mockRejectedValue({ response: { status: 429 } })
    mocks.getNotifications.mockResolvedValue([])

    render(<Navbar />)
    await waitFor(() => expect(mocks.getMe).toHaveBeenCalled())
    // Give the rejection handler a tick to run.
    await new Promise((r) => setTimeout(r, 0))
    expect(mocks.removeToken).not.toHaveBeenCalled()
  })

  it('does NOT clear the session on a transient network failure', async () => {
    mocks.isLoggedIn.mockReturnValue(true)
    mocks.getMe.mockRejectedValue(new Error('Network Error'))
    mocks.getNotifications.mockResolvedValue([])

    render(<Navbar />)
    await waitFor(() => expect(mocks.getMe).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(mocks.removeToken).not.toHaveBeenCalled()
  })

  it('clears the session on a genuine 401 (expired/revoked token)', async () => {
    mocks.isLoggedIn.mockReturnValue(true)
    mocks.getMe.mockRejectedValue({ response: { status: 401, data: { detail: 'Invalid or expired token' } } })
    mocks.getNotifications.mockResolvedValue([])

    render(<Navbar />)
    await waitFor(() => expect(mocks.getMe).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(mocks.removeToken).toHaveBeenCalled()
  })
})
