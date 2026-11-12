import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const enabled = vi.fn()
vi.mock('@/lib/aiConsole', () => ({
  aiFeatureEnabled: () => enabled(),
}))

import AiGate from '../ai/AiGate'

beforeEach(() => {
  enabled.mockReset()
})

describe('AiGate', () => {
  it('renders the section when the deployment serves the AI surface', async () => {
    enabled.mockResolvedValue(true)
    render(
      <AiGate>
        <p>console content</p>
      </AiGate>,
    )
    expect(await screen.findByText('console content')).toBeInTheDocument()
  })

  it('shows an honest unavailable panel when the flag is off', async () => {
    enabled.mockResolvedValue(false)
    render(
      <AiGate>
        <p>console content</p>
      </AiGate>,
    )
    expect(
      await screen.findByText(/not available on this deployment/i),
    ).toBeInTheDocument()
    expect(screen.queryByText('console content')).not.toBeInTheDocument()
    // The panel says why, in the user's terms rather than an error code.
    expect(screen.getByText(/switched off/i)).toBeInTheDocument()
  })

  it('treats a rejected probe as unavailable rather than crashing', async () => {
    enabled.mockRejectedValue(new Error('offline'))
    render(
      <AiGate>
        <p>console content</p>
      </AiGate>,
    )
    await waitFor(() =>
      expect(
        screen.getByText(/not available on this deployment/i),
      ).toBeInTheDocument(),
    )
  })

  it('announces the checking state to assistive tech', () => {
    enabled.mockReturnValue(new Promise(() => {}))
    render(
      <AiGate>
        <p>console content</p>
      </AiGate>,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
  })
})
