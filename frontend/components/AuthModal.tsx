'use client'

import { useState } from 'react'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { saveToken } from '@/lib/auth'
import api from '@/lib/api'

export default function AuthModal({ onClose }: { onClose: () => void }) {
  const [isLogin, setIsLogin] = useState(true)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [termsAccepted, setTermsAccepted] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [showPassword, setShowPassword] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      const refCode = isLogin ? undefined : (sessionStorage.getItem('torrent_cdn_ref') || undefined)
      const data = isLogin
        ? await api.login(email, password)
        : await api.register(email, password, termsAccepted, refCode)
      saveToken(data.access_token)
      // Referral code is consumed after a successful registration.
      if (!isLogin && refCode) sessionStorage.removeItem('torrent_cdn_ref')
      onClose()
      window.location.reload()
    } catch (err: any) {
      // Show the API's real message when present; otherwise explain WHY the
      // generic 'Authentication failed' happens — Render's free tier cold-starts
      // after ~15min idle, so the Vercel proxy can return 502/504 (or the
      // request times out) even with correct credentials.
      const status = err.response?.status
      const detail = err.response?.data?.detail
      if (detail) {
        setError(detail)
      } else if (status === 502 || status === 503 || status === 504) {
        setError('The server is still waking up. Give it a few seconds and try again.')
      } else if (!err.response) {
        setError('Could not reach the server. Check your connection and try again.')
      } else {
        setError(`${isLogin ? 'Login' : 'Sign up'} failed. Please try again.`)
      }
    } finally { setLoading(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
      <div className="w-full max-w-sm border p-8" style={{ backgroundColor: '#111', borderColor: '#333' }}>
        <div className="mb-6 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">{isLogin ? 'Login' : 'Create account'}</h2>
          <button onClick={onClose} className="text-sm" style={{ color: '#555' }}>&times;</button>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-5">
          <div>
            <label className="mb-1 block text-xs uppercase tracking-widest" style={{ color: '#555' }}>Email</label>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required
              className="w-full border px-3 py-2 text-sm text-white" style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}
              placeholder="you@example.com" />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase tracking-widest" style={{ color: '#555' }}>Password</label>
            <div className="relative">
              <input
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                className="w-full border px-3 py-2 pr-9 text-sm text-white"
                style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}
                placeholder="password"
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
                className="absolute right-3 top-1/2 -translate-y-1/2 transition-colors hover:text-white"
                style={{ color: '#555' }}
                tabIndex={-1}
              >
                {showPassword ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </div>
            {isLogin && (
              <a
                href="/auth/forgot-password"
                className="mt-1 inline-block text-[11px] transition-colors hover:text-white"
                style={{ color: '#555' }}
              >
                Forgot password?
              </a>
            )}
          </div>

          {error && <p className="text-xs text-white">{error}</p>}

          {!isLogin && (
            <label className="flex items-start gap-2 text-xs" style={{ color: '#555' }}>
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
                <a href="/terms" target="_blank" rel="noopener noreferrer" className="underline transition-colors hover:text-white" style={{ color: '#888' }}>
                  Terms of Service
                </a>
                {' '}and{' '}
                <a href="/privacy" target="_blank" rel="noopener noreferrer" className="underline transition-colors hover:text-white" style={{ color: '#888' }}>
                  Privacy Policy
                </a>
              </span>
            </label>
          )}

          <button type="submit" disabled={loading}
            className="w-full border py-2 text-sm font-medium disabled:opacity-50"
            style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}>
            {loading ? <Loader2 size={14} className="mx-auto animate-spin" /> : isLogin ? 'Login' : 'Create account'}
          </button>

          <p className="text-center text-xs" style={{ color: '#555' }}>
            {isLogin ? "Don't have an account?" : 'Already have an account?'}{' '}
            <button type="button" onClick={() => { setIsLogin(!isLogin); setError('') }} className="font-medium text-white hover:underline">
              {isLogin ? 'Sign up' : 'Login'}
            </button>
          </p>
        </form>
      </div>
    </div>
  )
}
