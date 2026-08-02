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

  it('starts a Stripe checkout when the Pro plan is clicked', async () => {
    mocks.createCheckout.mockResolvedValue({ url: 'https://checkout.stripe.com/xyz' })
    const user = userEvent.setup()
    render(<PricingTable />)

    // Two "Get started" buttons exist (Free + Pro) — the Pro one is second.
    await user.click(screen.getAllByRole('button', { name: /get started/i })[1])

    expect(mocks.createCheckout).toHaveBeenCalledTimes(1)
    expect(mocks.createCheckout).toHaveBeenCalledWith('pro')
  })

  it('shows an error when checkout fails', async () => {
    mocks.createCheckout.mockRejectedValue(new Error('stripe down'))
    const user = userEvent.setup()
    render(<PricingTable />)

    await user.click(screen.getAllByRole('button', { name: /get started/i })[1])

    expect(await screen.findByText('Checkout failed. Please try again.')).toBeInTheDocument()
  })

  it('marks the current plan button as disabled', () => {
    render(<PricingTable currentPlan="free" />)
    const freeButton = screen.getAllByRole('button', { name: /get started|current plan/i })[0]
    expect(freeButton).toBeDisabled()
    expect(freeButton).toHaveTextContent('Current plan')
  })
})
