'use client'

import { Suspense, useEffect, useState } from 'react'
import { useSearchParams, useRouter } from 'next/navigation'
import { Loader2, CheckCircle, XCircle, Lock, Eye, EyeOff, ArrowLeft } from 'lucide-react'
import Link from 'next/link'
import api from '@/lib/api'

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={
      <div className="flex min-h-[80vh] items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <Loader2 size={24} className="animate-spin" style={{ color: 'var(--text-muted)' }} />
          <p className="text-xs tracking-widest uppercase" style={{ color: 'var(--text-faint)' }}>Loading...</p>
        </div>
      </div>
    }>
      <ResetPasswordForm />
    </Suspense>
  )
}

function ResetPasswordForm() {
  const searchParams = useSearchParams()
  const router = useRouter()
  const token = searchParams.get('token')

  const [status, setStatus] = useState<'validating' | 'ready' | 'loading' | 'success' | 'error'>('validating')
  const [message, setMessage] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)

  // Validate token presence on mount
  useEffect(() => {
    if (!token) {
      setStatus('error')
      setMessage('No reset token found. The link you used may be invalid.')
    } else {
      setStatus('ready')
    }
  }, [token])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!token) return

    // Client-side validation
    if (password.length < 8) {
      setMessage('Password must be at least 8 characters')
      return
    }
    if (password !== confirmPassword) {
      setMessage('Passwords do not match')
      return
    }

    setStatus('loading')
    setMessage('')

    try {
      const data = await api.resetPassword(token, password)
      setStatus('success')
      setMessage(data.detail || 'Password has been reset successfully.')
    } catch (err: any) {
      setStatus('error')
      setMessage(err.response?.data?.detail || 'Failed to reset password. The link may have expired.')
    }
  }

  // Validation state
  if (status === 'validating') {
    return (
      <div className="flex min-h-[80vh] items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <Loader2 size={24} className="animate-spin" style={{ color: 'var(--text-muted)' }} />
          <p className="text-xs tracking-widest uppercase" style={{ color: 'var(--text-faint)' }}>Checking reset link...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex min-h-[80vh] items-center justify-center px-4">
      <div className="w-full max-w-sm">
        {/* Back link */}
        <Link
          href="/"
          className="mb-6 inline-flex items-center gap-1.5 text-xs transition-colors hover:text-[color:var(--text-primary)]"
          style={{ color: 'var(--text-muted)' }}
        >
          <ArrowLeft size={12} />
          Back to home
        </Link>

        {/* Success state */}
        {status === 'success' && (
          <div className="border p-8 text-center" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
            <div className="mx-auto mb-4 flex items-center justify-center w-12 h-12 rounded-full" style={{ backgroundColor: 'rgba(74,222,128,0.1)' }}>
              <CheckCircle size={24} style={{ color: 'var(--ok)' }} />
            </div>
            <h2 className="mb-2 text-lg font-bold text-[color:var(--text-primary)]">Password Reset</h2>
            <p className="mb-2 text-sm" style={{ color: 'var(--text-soft)' }}>{message}</p>
            <p className="mb-6 text-xs" style={{ color: 'var(--text-muted)' }}>You can now log in with your new password.</p>
            <button
              onClick={() => router.push('/')}
              className="border px-5 py-2.5 text-sm font-medium transition-opacity hover:opacity-80"
              style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
            >
              Log in &rarr;
            </button>
          </div>
        )}

        {/* Error state (no token or expired) */}
        {status === 'error' && (
          <div className="border p-8 text-center" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
            <div className="mx-auto mb-4 flex items-center justify-center w-12 h-12 rounded-full" style={{ backgroundColor: 'rgba(239,68,68,0.1)' }}>
              <XCircle size={24} style={{ color: 'var(--err)' }} />
            </div>
            <h2 className="mb-2 text-lg font-bold text-[color:var(--text-primary)]">Invalid Link</h2>
            <p className="mb-6 text-sm" style={{ color: 'var(--text-soft)' }}>{message}</p>
            <button
              onClick={() => router.push('/')}
              className="border px-5 py-2.5 text-sm font-medium transition-opacity hover:opacity-80"
              style={{ borderColor: 'var(--inverse-bg)', color: 'var(--text-primary)' }}
            >
              Back to login
            </button>
          </div>
        )}

        {/* Reset form */}
        {(status === 'ready' || status === 'loading') && (
          <div className="border p-8" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
            <div className="mb-6 flex items-center gap-3">
              <div className="flex items-center justify-center w-8 h-8 rounded-lg" style={{ backgroundColor: 'rgba(59,130,246,0.1)' }}>
                <Lock size={15} style={{ color: 'var(--info)' }} />
              </div>
              <div>
                <h2 className="text-sm font-semibold text-[color:var(--text-primary)]">Reset your password</h2>
                <p className="text-xs" style={{ color: 'var(--text-muted)' }}>Choose a new password for your account</p>
              </div>
            </div>

            <form onSubmit={handleSubmit} className="flex flex-col gap-5">
              {/* New password */}
              <div>
                <label className="mb-1.5 block text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
                  New password
                </label>
                <div className="relative">
                  <input
                    type={showPassword ? 'text' : 'password'}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                    minLength={8}
                    className="w-full border px-3 py-2 pr-9 text-sm text-[color:var(--text-primary)] outline-none transition-colors focus:border-white/20"
                    style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)' }}
                    placeholder="••••••••"
                    autoFocus
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 transition-colors hover:text-[color:var(--text-primary)]"
                    style={{ color: 'var(--text-muted)' }}
                    tabIndex={-1}
                  >
                    {showPassword ? <EyeOff size={14} /> : <Eye size={14} />}
                  </button>
                </div>
                <p className="mt-1 text-[10px]" style={{ color: 'var(--text-faint)' }}>Minimum 8 characters</p>
              </div>

              {/* Confirm password */}
              <div>
                <label className="mb-1.5 block text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
                  Confirm password
                </label>
                <div className="relative">
                  <input
                    type={showConfirm ? 'text' : 'password'}
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    required
                    minLength={8}
                    className="w-full border px-3 py-2 pr-9 text-sm text-[color:var(--text-primary)] outline-none transition-colors focus:border-white/20"
                    style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)' }}
                    placeholder="••••••••"
                  />
                  <button
                    type="button"
                    onClick={() => setShowConfirm(!showConfirm)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 transition-colors hover:text-[color:var(--text-primary)]"
                    style={{ color: 'var(--text-muted)' }}
                    tabIndex={-1}
                  >
                    {showConfirm ? <EyeOff size={14} /> : <Eye size={14} />}
                  </button>
                </div>
                {/* Password match indicator */}
                {confirmPassword.length > 0 && (
                  <p
                    className="mt-1 flex items-center gap-1 text-[10px]"
                    style={{ color: password === confirmPassword ? 'var(--ok)' : 'var(--err)' }}
                  >
                    {password === confirmPassword ? (
                      <><CheckCircle size={10} /> Passwords match</>
                    ) : (
                      <><XCircle size={10} /> Passwords do not match</>
                    )}
                  </p>
                )}
              </div>

              {/* Error message */}
              {message && (
                <div
                  className="border px-3 py-2 text-xs"
                  style={{
                    backgroundColor: 'rgba(239,68,68,0.05)',
                    borderColor: 'rgba(239,68,68,0.2)',
                    color: 'var(--err)',
                  }}
                >
                  {message}
                </div>
              )}

              <button
                type="submit"
                disabled={status === 'loading'}
                className="w-full border py-2.5 text-sm font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
                style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
              >
                {status === 'loading' ? (
                  <span className="flex items-center justify-center gap-2">
                    <Loader2 size={14} className="animate-spin" /> Resetting...
                  </span>
                ) : (
                  'Reset password'
                )}
              </button>
            </form>
          </div>
        )}
      </div>
    </div>
  )
}
