import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// Mock the api client so submit errors are controllable per test.
const mocks = vi.hoisted(() => ({
  login: vi.fn(),
}))
vi.mock('@/lib/api', () => ({
  default: {
    login: mocks.login,
    register: vi.fn(),
  },
}))
vi.mock('@/lib/auth', () => ({
  saveToken: vi.fn(),
}))

import AuthModal from '../AuthModal'

describe('AuthModal', () => {
  beforeEach(() => {
    mocks.login.mockReset()
  })

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

  it('shows the API detail message on auth failure', async () => {
    const user = userEvent.setup()
    mocks.login.mockRejectedValue({ response: { status: 401, data: { detail: 'Invalid email or password' } } })
    render(<AuthModal onClose={vi.fn()} />)

    await user.type(screen.getByPlaceholderText('you@example.com'), 'user@example.com')
    await user.type(screen.getByPlaceholderText('password'), 'wrongpass1')
    await user.click(screen.getByRole('button', { name: 'Login' }))

    expect(await screen.findByText('Invalid email or password')).toBeInTheDocument()
  })

  it('explains cold-start 502 instead of the generic message', async () => {
    const user = userEvent.setup()
    mocks.login.mockRejectedValue({ response: { status: 502, data: '<html>Bad Gateway</html>' } })
    render(<AuthModal onClose={vi.fn()} />)

    await user.type(screen.getByPlaceholderText('you@example.com'), 'user@example.com')
    await user.type(screen.getByPlaceholderText('password'), 'correctpass1')
    await user.click(screen.getByRole('button', { name: 'Login' }))

    expect(
      await screen.findByText('The server is still waking up. Give it a few seconds and try again.')
    ).toBeInTheDocument()
  })

  it('shows a connection message when there is no response (timeout/offline)', async () => {
    const user = userEvent.setup()
    mocks.login.mockRejectedValue(new Error('Network Error'))
    render(<AuthModal onClose={vi.fn()} />)

    await user.type(screen.getByPlaceholderText('you@example.com'), 'user@example.com')
    await user.type(screen.getByPlaceholderText('password'), 'correctpass1')
    await user.click(screen.getByRole('button', { name: 'Login' }))

    expect(
      await screen.findByText('Could not reach the server. Check your connection and try again.')
    ).toBeInTheDocument()
  })
})
