'use client'

import { useEffect, useState } from 'react'
import { Eye, EyeOff, Loader2, ShieldCheck, Copy, Check } from 'lucide-react'
import QRCode from 'qrcode'
import { saveToken, saveRememberedToken } from '@/lib/auth'
import api from '@/lib/api'

// Enrollment payload from /auth/mfa/setup — secret + backup codes are returned
// exactly once, so they must be held in memory only (never persisted).
interface MfaSetupData {
  secret: string
  otpauth_uri: string
  backup_codes: string[]
  qr?: string
}

export default function AuthModal({ onClose }: { onClose: () => void }) {
  const [isLogin, setIsLogin] = useState(true)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [termsAccepted, setTermsAccepted] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  // Second-factor step: set when the backend answers mfa_required: true.
  const [mfaToken, setMfaToken] = useState<string | null>(null)
  // When the account has NO 2FA yet (mandatory enrollment), login answers
  // needs_setup: true and the modal walks through QR/secret/backup codes
  // before the code field — the user cannot skip it.
  const [mfaNeedsSetup, setMfaNeedsSetup] = useState(false)
  const [mfaSetupData, setMfaSetupData] = useState<MfaSetupData | null>(null)
  const [mfaCode, setMfaCode] = useState('')
  // "Keep me logged in for 2 weeks" — set on the login form, applied when the
  // post-OTP session token is minted (the OTP is still required every login).
  const [rememberMe, setRememberMe] = useState(false)
  const [copiedUri, setCopiedUri] = useState(false)
  // Set when the confirm response reports the LAST backup code was consumed —
  // the modal holds open to warn the user before they land in the dashboard.
  const [lastBackupCodeUsed, setLastBackupCodeUsed] = useState(false)

  const resetMfa = (keepError = false) => {
    setMfaToken(null)
    setMfaNeedsSetup(false)
    setMfaSetupData(null)
    setMfaCode('')
    setLastBackupCodeUsed(false)
    if (!keepError) setError('')
  }

  // Abandon the enrollment step. During SIGNUP the account already exists at
  // this point, so bounce to the LOGIN view — resubmitting the signup form
  // would be a confusing "Email already registered" dead end (log in instead,
  // which re-triggers enrollment with a fresh token).
  const bailToLogin = (keepError = false) => {
    if (!isLogin) setIsLogin(true)
    resetMfa(keepError)
  }

  const showError = (err: any, fallback: string) => {
    // Show the API's real message when present; otherwise explain WHY the
    // generic 'Authentication failed' happens — Render's free tier cold-starts
    // after ~15min idle, so the Vercel proxy can return 502/504 (or the
    // request times out) even with correct credentials.
    const status = err.response?.status
    const detail = err.response?.data?.detail
    if (detail) setError(detail)
    else if (status === 502 || status === 503 || status === 504)
      setError('The server is still waking up. Give it a few seconds and try again.')
    else if (!err.response) setError('Could not reach the server. Check your connection and try again.')
    else setError(fallback)
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      const refCode = isLogin ? undefined : (sessionStorage.getItem('torrent_cdn_ref') || undefined)
      const data = isLogin
        ? await api.login(email, password)
        : await api.register(email, password, termsAccepted, refCode)
      // Referral credit is earned the moment the account is created — consume
      // the code even when the mandatory 2FA step below delays the session.
      if (!isLogin && refCode) sessionStorage.removeItem('torrent_cdn_ref')
      // MFA gate: the password half succeeded but this account requires a
      // second factor — switch to the code step (NO token yet). For signup
      // this IS the enrollment: the account was born MFA-on, and the modal
      // walks through the authenticator QR before any session exists.
      if (data?.mfa_required) {
        setMfaToken(data.mfa_token)
        setMfaNeedsSetup(Boolean(data.needs_setup))
        setMfaSetupData(null)
        setMfaCode('')
        return
      }
      saveToken(data.access_token)
      onClose()
      window.location.reload()
    } catch (err: any) {
      showError(err, `${isLogin ? 'Login' : 'Sign up'} failed. Please try again.`)
    } finally { setLoading(false) }
  }

  // Mandatory enrollment: fetch the TOTP secret + backup codes the moment the
  // login answers needs_setup (authenticated by the mfa_token — the user has
  // no session yet). The mfa_token is short-lived (~5 min), so a failure here
  // falls back to the login form to restart cleanly.
  useEffect(() => {
    if (!mfaToken || !mfaNeedsSetup || mfaSetupData) return
    let cancelled = false
    api.mfaSetup(mfaToken)
      .then(async (data: MfaSetupData) => {
        if (cancelled) return
        const qr = await QRCode.toDataURL(data.otpauth_uri, { width: 240, margin: 4 })
        if (!cancelled) setMfaSetupData({ ...data, qr })
      })
      .catch((err: any) => {
        if (cancelled) return
        showError(err, 'Failed to start two-factor setup. Please try again.')
        // Expired mfa_token (5 min) or any setup failure: restart the form
        // (at LOGIN during signup — the account already exists), keeping the
        // error visible so the user knows why they bounced back.
        bailToLogin(true)
      })
    return () => { cancelled = true }
  }, [mfaToken, mfaNeedsSetup, mfaSetupData])

  const handleMfaSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!mfaToken) return
    setLoading(true)
    setError('')
    try {
      const data = await api.mfaConfirm(mfaToken, mfaCode, rememberMe)
      saveRememberedToken(data.access_token, rememberMe)
      // The LAST backup code was just consumed. The login succeeded (token is
      // saved) but the modal stays open to warn the user before they land in
      // the dashboard — zero codes left means losing the authenticator app is
      // losing the account.
      if (data.backup_codes_remaining === 0) {
        setLastBackupCodeUsed(true)
        return
      }
      onClose()
      window.location.reload()
    } catch (err: any) {
      showError(err, 'Invalid code. Check your authenticator app and try again.')
      // The mfa_token is short-lived (~5 min): if it expired while the user
      // was scanning the QR or typing the code, restart cleanly at the login
      // form (keeping the reason visible) instead of stranding them on the
      // second-factor screen retrying codes forever.
      const detail = err?.response?.data?.detail
      if (typeof detail === 'string' && detail.includes('expired')) bailToLogin(true)
    } finally { setLoading(false) }
  }

  const copyOtpUri = async () => {
    if (!mfaSetupData) return
    await navigator.clipboard.writeText(mfaSetupData.otpauth_uri)
    setCopiedUri(true)
    setTimeout(() => setCopiedUri(false), 2000)
  }

  // Dismiss the last-backup-code warning: the login already succeeded (the
  // token is saved), so this completes the same close + reload as a normal
  // successful MFA login.
  const finishLogin = () => {
    onClose()
    window.location.reload()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
      <div className="w-full max-w-sm border p-8" style={{ backgroundColor: 'var(--bg-card)', borderColor: 'var(--border-strong)' }}>
        <div className="mb-6 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">
            {mfaToken && mfaNeedsSetup ? 'Set up two-factor authentication' : mfaToken ? 'Two-factor verification' : (isLogin ? 'Login' : 'Create account')}
          </h2>
          <button
            onClick={() => {
              // On the last-backup-code warning the login already succeeded
              // (token saved) — the × must complete it the same way as
              // "Continue to dashboard" (close + reload), or the page would
              // stay visually logged out despite a valid session.
              if (lastBackupCodeUsed) {
                finishLogin()
                return
              }
              // During a SIGNUP-triggered mandatory-enrollment step the account
              // already exists but has no session yet — closing the modal would
              // strand the user (retrying signup then hits "Email already
              // registered", a dead end that looks like a bug). Bounce to the
              // login view instead, where signing in re-triggers enrollment
              // with a fresh mfa_token.
              if (mfaToken && mfaNeedsSetup && !isLogin) {
                setIsLogin(true)
                resetMfa()
                setError('Your account was created — verify your email, then log in to set up two-factor authentication.')
              } else {
                onClose()
              }
            }}
            className="text-sm"
            style={{ color: 'var(--text-muted)' }}
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        {mfaToken && mfaNeedsSetup ? (
          // --- Mandatory enrollment: scan → save backup codes → verify ---
          <form onSubmit={handleMfaSubmit} className="flex flex-col gap-4">
            <p className="text-xs leading-relaxed" style={{ color: 'var(--text-soft)' }}>
              <ShieldCheck size={14} className="mr-1 inline" style={{ color: '#fbbf24' }} />
              Two-factor authentication is required for all accounts. Scan this QR
              code with your authenticator app (Google Authenticator, Authy, 1Password…)
              or copy the otpauth link to add it manually.
            </p>

            {mfaSetupData ? (
              <>
                <div className="flex flex-col items-center gap-3">
                  {mfaSetupData.qr && (
                    <img src={mfaSetupData.qr} alt="TOTP QR code" width={180} height={180}
                      className="border p-1" style={{ backgroundColor: 'var(--inverse-bg)' }} />
                  )}
                  <div className="w-full">
                    <div className="flex items-center gap-2">
                      <code className="flex-1 border px-2 py-1 font-mono text-[11px] text-white"
                        style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)', wordBreak: 'break-all' }}>
                        {mfaSetupData.secret}
                      </code>
                      <button type="button" onClick={copyOtpUri}
                        className="border px-2 py-1 text-[11px] transition-opacity hover:opacity-80"
                        style={{ borderColor: 'var(--border-strong)', color: 'var(--text-muted)' }}>
                        {copiedUri ? <><Check size={12} className="inline" style={{ color: '#4ade80' }} /> Copied</> : 'Copy link'}
                      </button>
                    </div>
                  </div>
                </div>

                <div>
                  <p className="text-[11px] leading-relaxed" style={{ color: '#fbbf24' }}>
                    Save these backup codes somewhere safe. Each is single-use and
                    shown only once — they are your only way in if you lose the app.
                  </p>
                  <div className="mt-2 grid grid-cols-2 gap-1">
                    {mfaSetupData.backup_codes.map((c) => (
                      <code key={c} className="border px-2 py-1 text-center font-mono text-[11px]"
                        style={{ borderColor: 'var(--border-strong)', color: 'var(--text-soft)' }}>
                        {c}
                      </code>
                    ))}
                  </div>
                </div>
              </>
            ) : (
              <div className="flex h-28 items-center justify-center">
                <Loader2 size={18} className="animate-spin" style={{ color: 'var(--text-muted)' }} />
              </div>
            )}

            <div>
              <label className="mb-1 block text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Authenticator code</label>
              <input
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={mfaCode}
                onChange={(e) => setMfaCode(e.target.value)}
                required
                maxLength={12}
                disabled={!mfaSetupData}
                className="w-full border px-3 py-2 text-sm text-white disabled:opacity-50"
                style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)' }}
                placeholder="000000"
              />
            </div>
            {error && <p className="text-xs text-white">{error}</p>}
            <button type="submit" disabled={loading || !mfaSetupData}
              className="w-full border py-2 text-sm font-medium disabled:opacity-50"
              style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}>
              {loading ? <Loader2 size={14} className="mx-auto animate-spin" /> : 'Verify & continue'}
            </button>
            <button
              type="button"
              onClick={() => bailToLogin()}
              className="text-center text-xs transition-colors hover:text-white"
              style={{ color: 'var(--text-muted)' }}
            >
              Back to login
            </button>
          </form>
        ) : mfaToken ? (
          // --- Already enrolled: just the code field ---
          lastBackupCodeUsed ? (
            // The login succeeded (token saved) but the LAST backup code was
            // just consumed — hold the modal open and warn before the user
            // lands in the dashboard: zero codes left means a lost
            // authenticator app is a locked-out account.
            <div className="flex flex-col gap-4">
              <div className="border p-4" style={{ borderColor: '#7c2d12', backgroundColor: 'var(--warn-chip-bg)' }}>
                <p className="text-sm font-medium" style={{ color: '#fbbf24' }}>
                  You've used your last backup code
                </p>
                <p className="mt-2 text-xs leading-relaxed" style={{ color: 'var(--text-soft)' }}>
                  Backup codes are single-use and there are none left. If you ever lose your
                  authenticator app, you won't be able to sign in again. Generate a fresh set
                  by disabling and re-enabling two-factor authentication from your dashboard.
                </p>
              </div>
              <button
                type="button"
                onClick={finishLogin}
                className="w-full border py-2 text-sm font-medium transition-opacity hover:opacity-80"
                style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
              >
                Continue to dashboard
              </button>
            </div>
          ) : (
          <form onSubmit={handleMfaSubmit} className="flex flex-col gap-5">
            <div>
              <label className="mb-1 block text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Two-factor code</label>
              <input
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={mfaCode}
                onChange={(e) => setMfaCode(e.target.value)}
                required
                maxLength={12}
                className="w-full border px-3 py-2 text-sm text-white"
                style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)' }}
                placeholder="000000"
              />
            </div>
            <p className="text-xs" style={{ color: 'var(--text-soft)' }}>
              Enter the 6-digit code from your authenticator app{''}
              {' '}(or a single-use backup code).
            </p>
            <label className="flex cursor-pointer items-center gap-2 text-xs" style={{ color: 'var(--text-soft)' }}>
              <input
                type="checkbox"
                checked={rememberMe}
                onChange={(e) => setRememberMe(e.target.checked)}
                className="h-3.5 w-3.5 accent-white"
              />
              Keep me logged in for 2 weeks
            </label>
            {error && <p className="text-xs text-white">{error}</p>}
            <button type="submit" disabled={loading}
              className="w-full border py-2 text-sm font-medium disabled:opacity-50"
              style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}>
              {loading ? <Loader2 size={14} className="mx-auto animate-spin" /> : 'Verify code'}
            </button>
            <button
              type="button"
              onClick={() => bailToLogin()}
              className="text-center text-xs transition-colors hover:text-white"
              style={{ color: 'var(--text-muted)' }}
            >
              Back to login
            </button>
          </form>
          )
        ) : (
        <form onSubmit={handleSubmit} className="flex flex-col gap-5">
          <div>
            <label className="mb-1 block text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Email</label>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required
              className="w-full border px-3 py-2 text-sm text-white" style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)' }}
              placeholder="you@example.com" />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Password</label>
            <div className="relative">
              <input
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                className="w-full border px-3 py-2 pr-9 text-sm text-white"
                style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)' }}
                placeholder="password"
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
                className="absolute right-3 top-1/2 -translate-y-1/2 transition-colors hover:text-white"
                style={{ color: 'var(--text-muted)' }}
                tabIndex={-1}
              >
                {showPassword ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </div>
            {isLogin && (
              <a
                href="/auth/forgot-password"
                className="mt-1 inline-block text-[11px] transition-colors hover:text-white"
                style={{ color: 'var(--text-muted)' }}
              >
                Forgot password?
              </a>
            )}
          </div>

          {error && <p className="text-xs text-white">{error}</p>}

          {!isLogin && (
            <label className="flex items-start gap-2 text-xs" style={{ color: 'var(--text-muted)' }}>
              <input
                type="checkbox"
                checked={termsAccepted}
                onChange={(e) => setTermsAccepted(e.target.checked)}
                required
                className="mt-0.5"
                style={{ accentColor: '#fff' }}
              />
              <span>
                I accept the{' '}
                <a href="/terms" target="_blank" rel="noopener noreferrer" className="underline transition-colors hover:text-white" style={{ color: 'var(--text-soft)' }}>
                  Terms of Service
                </a>
                {' '}and{' '}
                <a href="/privacy" target="_blank" rel="noopener noreferrer" className="underline transition-colors hover:text-white" style={{ color: 'var(--text-soft)' }}>
                  Privacy Policy
                </a>
              </span>
            </label>
          )}

          <button type="submit" disabled={loading}
            className="w-full border py-2 text-sm font-medium disabled:opacity-50"
            style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}>
            {loading ? <Loader2 size={14} className="mx-auto animate-spin" /> : isLogin ? 'Login' : 'Create account'}
          </button>

          <p className="text-center text-xs" style={{ color: 'var(--text-muted)' }}>
            {isLogin ? "Don't have an account?" : 'Already have an account?'}{' '}
            <button type="button" onClick={() => { setIsLogin(!isLogin); resetMfa() }} className="font-medium text-white hover:underline">
              {isLogin ? 'Sign up' : 'Login'}
            </button>
          </p>
        </form>
        )}
      </div>
    </div>
  )
}
