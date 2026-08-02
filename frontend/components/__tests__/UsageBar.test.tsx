import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import UsageBar from '../UsageBar'

describe('UsageBar', () => {
  it('shows used and limit with the correct percentage width', () => {
    const { container } = render(<UsageBar used={100} limit={250} />)
    expect(screen.getByText('100.00 GB / 250 GB')).toBeInTheDocument()
    expect(container.querySelector('div[style*="width: 40%"]')).not.toBeNull()
  })

  it('clamps the bar at 100% when used exceeds the limit', () => {
    const { container } = render(<UsageBar used={300} limit={250} />)
    expect(container.querySelector('div[style*="width: 100%"]')).not.toBeNull()
  })

  it('handles an unlimited plan (Infinity) without a limit suffix', () => {
    const { container } = render(<UsageBar used={50.5} limit={Infinity} />)
    expect(screen.getByText('50.50 GB')).toBeInTheDocument()
    expect(screen.queryByText(/\/ \d+ GB/)).not.toBeInTheDocument()
    expect(container.querySelector('div[style*="width: 100%"]')).not.toBeNull()
  })
})
