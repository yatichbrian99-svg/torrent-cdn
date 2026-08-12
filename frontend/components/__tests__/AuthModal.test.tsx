import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// Mock the api client so submit errors are controllable per test.
const mocks = vi.hoisted(() => ({
  login: vi.fn(),
  register: vi.fn(),
  mfaConfirm: vi.fn(),
  mfaSetup: vi.fn(),
  saveToken: vi.fn(),
}))
vi.mock('@/lib/api', () => ({
  default: {
    login: mocks.login,
    register: mocks.register,
    mfaConfirm: mocks.mfaConfirm,
    mfaSetup: mocks.mfaSetup,
  },
}))
vi.mock('@/lib/auth', () => ({
  saveToken: mocks.saveToken,
}))

import AuthModal from '../AuthModal'

describe('AuthModal', () => {
  beforeEach(() => {
    mocks.login.mockReset()
    mocks.register.mockReset()
    mocks.mfaConfirm.mockReset()
    mocks.mfaSetup.mockReset()
    mocks.saveToken.mockReset()
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

  it('switches to the two-factor step when login requires MFA (no token yet)', async () => {
    const user = userEvent.setup()
    mocks.login.mockResolvedValue({ mfa_required: true, mfa_token: 'mfa-jwt', token_type: 'bearer' })
    render(<AuthModal onClose={vi.fn()} />)

    await user.type(screen.getByPlaceholderText('you@example.com'), 'user@example.com')
    await user.type(screen.getByPlaceholderText('password'), 'securePass123')
    await user.click(screen.getByRole('button', { name: 'Login' }))

    // The code step appears and the session token is NOT saved yet.
    expect(await screen.findByPlaceholderText('000000')).toBeInTheDocument()
    expect(mocks.saveToken).not.toHaveBeenCalled()
  })

  it('completes login with a valid MFA code and saves the token', async () => {
    const user = userEvent.setup()
    mocks.login.mockResolvedValue({ mfa_required: true, mfa_token: 'mfa-jwt', token_type: 'bearer' })
    mocks.mfaConfirm.mockResolvedValue({ access_token: 'real-token', token_type: 'bearer' })
    render(<AuthModal onClose={vi.fn()} />)

    await user.type(screen.getByPlaceholderText('you@example.com'), 'user@example.com')
    await user.type(screen.getByPlaceholderText('password'), 'securePass123')
    await user.click(screen.getByRole('button', { name: 'Login' }))
    await user.type(await screen.findByPlaceholderText('000000'), '123456')
    await user.click(screen.getByRole('button', { name: 'Verify code' }))

    await waitFor(() => expect(mocks.mfaConfirm).toHaveBeenCalledWith('mfa-jwt', '123456'))
    expect(mocks.saveToken).toHaveBeenCalledWith('real-token')
  })

  it('shows the API detail on an invalid MFA code', async () => {
    const user = userEvent.setup()
    mocks.login.mockResolvedValue({ mfa_required: true, mfa_token: 'mfa-jwt', token_type: 'bearer' })
    mocks.mfaConfirm.mockRejectedValue({ response: { status: 401, data: { detail: 'Invalid authentication code' } } })
    render(<AuthModal onClose={vi.fn()} />)

    await user.type(screen.getByPlaceholderText('you@example.com'), 'user@example.com')
    await user.type(screen.getByPlaceholderText('password'), 'securePass123')
    await user.click(screen.getByRole('button', { name: 'Login' }))
    await user.type(await screen.findByPlaceholderText('000000'), '000000')
    await user.click(screen.getByRole('button', { name: 'Verify code' }))

    expect(await screen.findByText('Invalid authentication code')).toBeInTheDocument()
    expect(mocks.saveToken).not.toHaveBeenCalled()
  })

  it('walks through mandatory 2FA enrollment when login requires setup', async () => {
    const user = userEvent.setup()
    const setupData = {
      secret: 'JBSWY3DPEHPK3PXP',
      otpauth_uri: 'otpauth://totp/TorrentCDN:user%40example.com?secret=JBSWY3DPEHPK3PXP&issuer=TorrentCDN',
      backup_codes: ['ABC1234567', 'DEF2345678', 'GHI3456789', 'JKL4567890', 'MNO5678901', 'PQR6789012', 'STU7890123', 'VWX8901234', 'YZA9012345', 'BCD0123456'],
    }
    mocks.login.mockResolvedValue({ mfa_required: true, mfa_token: 'mfa-jwt', needs_setup: true, token_type: 'bearer' })
    mocks.mfaSetup.mockResolvedValue(setupData)
    mocks.mfaConfirm.mockResolvedValue({ access_token: 'real-token', token_type: 'bearer' })
    render(<AuthModal onClose={vi.fn()} />)

    await user.type(screen.getByPlaceholderText('you@example.com'), 'user@example.com')
    await user.type(screen.getByPlaceholderText('password'), 'securePass123')
    await user.click(screen.getByRole('button', { name: 'Login' }))

    // Setup is fetched using the mfa_token (no session exists yet).
    expect(mocks.mfaSetup).toHaveBeenCalledWith('mfa-jwt')
    // QR + secret + backup codes are shown before any session is saved.
    expect(await screen.findByAltText('TOTP QR code')).toBeInTheDocument()
    expect(screen.getByText('JBSWY3DPEHPK3PXP')).toBeInTheDocument()
    expect(screen.getByText('ABC1234567')).toBeInTheDocument()
    expect(mocks.saveToken).not.toHaveBeenCalled()

    // Verifying with the code completes BOTH enrollment and login.
    await user.type(screen.getByPlaceholderText('000000'), '123456')
    await user.click(screen.getByRole('button', { name: 'Verify & continue' }))

    await waitFor(() => expect(mocks.mfaConfirm).toHaveBeenCalledWith('mfa-jwt', '123456'))
    expect(mocks.saveToken).toHaveBeenCalledWith('real-token')
  })

  it('returns to the login form when mandatory enrollment setup fails', async () => {
    const user = userEvent.setup()
    mocks.login.mockResolvedValue({ mfa_required: true, mfa_token: 'mfa-jwt', needs_setup: true, token_type: 'bearer' })
    mocks.mfaSetup.mockRejectedValue({ response: { status: 401, data: { detail: 'MFA session expired — please log in again' } } })
    render(<AuthModal onClose={vi.fn()} />)

    await user.type(screen.getByPlaceholderText('you@example.com'), 'user@example.com')
    await user.type(screen.getByPlaceholderText('password'), 'securePass123')
    await user.click(screen.getByRole('button', { name: 'Login' }))

    // The setup failure (e.g. expired 5-min mfa_token) shows the API message
    // and bounces back to the login form so the user can start over.
    expect(await screen.findByText('MFA session expired — please log in again')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('password')).toBeInTheDocument()
    expect(mocks.saveToken).not.toHaveBeenCalled()
  })

  it('walks through mandatory 2FA enrollment right after signup', async () => {
    const user = userEvent.setup()
    const setupData = {
      secret: 'JBSWY3DPEHPK3PXP',
      otpauth_uri: 'otpauth://totp/TorrentCDN:new%40example.com?secret=JBSWY3DPEHPK3PXP&issuer=TorrentCDN',
      backup_codes: ['ABC1234567', 'DEF2345678', 'GHI3456789', 'JKL4567890', 'MNO5678901', 'PQR6789012', 'STU7890123', 'VWX8901234', 'YZA9012345', 'BCD0123456'],
    }
    // Registration answers the same second-factor gate as login: the account
    // is created but no session is minted until the authenticator is enrolled.
    mocks.register.mockResolvedValue({ mfa_required: true, mfa_token: 'mfa-jwt', needs_setup: true, token_type: 'bearer' })
    mocks.mfaSetup.mockResolvedValue(setupData)
    mocks.mfaConfirm.mockResolvedValue({ access_token: 'real-token', token_type: 'bearer' })
    render(<AuthModal onClose={vi.fn()} />)

    // Switch to the sign-up tab and register.
    await user.click(screen.getByRole('button', { name: 'Sign up' }))
    await user.type(screen.getByPlaceholderText('you@example.com'), 'new@example.com')
    await user.type(screen.getByPlaceholderText('password'), 'SecurePass123')
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    // Enrollment runs before any session is saved — setup is authenticated by
    // the registration-issued mfa_token.
    expect(mocks.mfaSetup).toHaveBeenCalledWith('mfa-jwt')
    expect(await screen.findByAltText('TOTP QR code')).toBeInTheDocument()
    expect(screen.getByText('JBSWY3DPEHPK3PXP')).toBeInTheDocument()
    expect(mocks.saveToken).not.toHaveBeenCalled()

    // Verifying with the code completes BOTH enrollment and the signup.
    await user.type(screen.getByPlaceholderText('000000'), '123456')
    await user.click(screen.getByRole('button', { name: 'Verify & continue' }))

    await waitFor(() => expect(mocks.mfaConfirm).toHaveBeenCalledWith('mfa-jwt', '123456'))
    expect(mocks.saveToken).toHaveBeenCalledWith('real-token')
  })

  it('bounces signup enrollment failures to the LOGIN view (the account already exists)', async () => {
    const user = userEvent.setup()
    mocks.register.mockResolvedValue({ mfa_required: true, mfa_token: 'mfa-jwt', needs_setup: true, token_type: 'bearer' })
    mocks.mfaSetup.mockRejectedValue({ response: { status: 401, data: { detail: 'MFA session expired — please log in again' } } })
    render(<AuthModal onClose={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Sign up' }))
    await user.type(screen.getByPlaceholderText('you@example.com'), 'new@example.com')
    await user.type(screen.getByPlaceholderText('password'), 'SecurePass123')
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Create account' }))

    // The setup failure (e.g. expired 5-min mfa_token) shows the API message.
    expect(await screen.findByText('MFA session expired — please log in again')).toBeInTheDocument()
    // The account was already created — the modal lands on LOGIN, not the
    // signup form (resubmitting would be an "Email already registered" dead end).
    expect(screen.getByRole('button', { name: 'Login' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Forgot password?' })).toBeInTheDocument()
    expect(mocks.saveToken).not.toHaveBeenCalled()
  })
})
