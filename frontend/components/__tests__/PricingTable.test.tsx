import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PricingTable from '../PricingTable'

const mocks = vi.hoisted(() => ({ createCheckout: vi.fn() }))

vi.mock('@/lib/api', () => ({
  default: { createCheckout: mocks.createCheckout },
}))

describe('PricingTable', () => {
  beforeEach(() => {
    mocks.createCheckout.mockReset()
    // Free/Business buttons navigate via window.location.href — stub it so
    // jsdom doesn't throw "navigation not implemented".
    vi.stubGlobal('location', { href: '' })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders all three tiers', () => {
    render(<PricingTable />)
    expect(screen.getByText('Free')).toBeInTheDocument()
    expect(screen.getByText('Pro')).toBeInTheDocument()
    expect(screen.getByText('Business')).toBeInTheDocument()
  })

  it('starts a Paystack checkout when the Pro plan is clicked', async () => {
    mocks.createCheckout.mockResolvedValue({ url: 'https://checkout.paystack.com/xyz' })
    const user = userEvent.setup()
    render(<PricingTable />)

    // Three "Get started" buttons exist (Free, Pro, Business) — Pro is second.
    await user.click(screen.getAllByRole('button', { name: /get started/i })[1])

    expect(mocks.createCheckout).toHaveBeenCalledTimes(1)
    expect(mocks.createCheckout).toHaveBeenCalledWith('pro')
  })

  it('starts a checkout when the Business plan is clicked (self-serve, no mailto)', async () => {
    mocks.createCheckout.mockResolvedValue({ url: 'https://checkout.paystack.com/xyz' })
    const user = userEvent.setup()
    render(<PricingTable />)

    // Business is the third "Get started" button.
    await user.click(screen.getAllByRole('button', { name: /get started/i })[2])

    expect(mocks.createCheckout).toHaveBeenCalledTimes(1)
    expect(mocks.createCheckout).toHaveBeenCalledWith('business')
  })

  it('shows an error when checkout fails', async () => {
    mocks.createCheckout.mockRejectedValue(new Error('paystack down'))
    const user = userEvent.setup()
    render(<PricingTable />)

    await user.click(screen.getAllByRole('button', { name: /get started/i })[1])

    expect(await screen.findByText('Checkout failed. Please try again.')).toBeInTheDocument()
  })

  it('refuses to navigate when the backend returns a non-https checkout URL', async () => {
    // Defense-in-depth: a redirect target that isn't an absolute https URL
    // (e.g. javascript: or a relative path) must never reach window.location.
    mocks.createCheckout.mockResolvedValue({ url: 'javascript:alert(1)' })
    const user = userEvent.setup()
    render(<PricingTable />)

    await user.click(screen.getAllByRole('button', { name: /get started/i })[1])

    expect(await screen.findByText('Checkout failed. Please try again.')).toBeInTheDocument()
    expect(window.location.href).toBe('')
  })

  it('marks the current plan button as disabled', () => {
    render(<PricingTable currentPlan="free" />)
    const freeButton = screen.getAllByRole('button', { name: /get started|current plan/i })[0]
    expect(freeButton).toBeDisabled()
    expect(freeButton).toHaveTextContent('Current plan')
  })
})
