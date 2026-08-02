import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AuthModal from '../AuthModal'

describe('AuthModal', () => {
  it('toggles the password field between hidden and visible', async () => {
    const user = userEvent.setup()
    render(<AuthModal onClose={vi.fn()} />)

    const input = screen.getByPlaceholderText('password')
    expect(input).toHaveAttribute('type', 'password')

    // Show password
    await user.click(screen.getByRole('button', { name: 'Show password' }))
    expect(input).toHaveAttribute('type', 'text')

    // Hide again
    await user.click(screen.getByRole('button', { name: 'Hide password' }))
    expect(input).toHaveAttribute('type', 'password')
  })

  it('renders the forgot-password link on the login tab', () => {
    render(<AuthModal onClose={vi.fn()} />)
    expect(screen.getByRole('link', { name: 'Forgot password?' })).toHaveAttribute(
      'href',
      '/auth/forgot-password'
    )
  })
})
