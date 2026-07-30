'use client'

import { useState } from 'react'
import { Loader2, CheckCircle, Mail, ArrowLeft } from 'lucide-react'
import Link from 'next/link'
import api from '@/lib/api'

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [status, setStatus] = useState<'form' | 'loading' | 'success'>('form')
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!email.trim()) return

    setStatus('loading')
    setError('')

    try {
      await api.forgotPassword(email)
      setStatus('success')
    } catch {
      setError('Something went wrong. Please try again.')
      setStatus('form')
    }
  }

  return (
    <div className="flex min-h-[80vh] items-center justify-center px-4">
      <div className="w-full max-w-sm">
        {/* Back link */}
        <Link
          href="/"
          className="mb-6 inline-flex items-center gap-1.5 text-xs transition-colors hover:text-white"
          style={{ color: '#555' }}
        >
          <ArrowLeft size={12} />
          Back to home
        </Link>

        {/* Success state */}
        {status === 'success' && (
          <div className="border p-8 text-center" style={{ borderColor: '#222', backgroundColor: '#111' }}>
            <div className="mx-auto mb-4 flex items-center justify-center w-12 h-12 rounded-full" style={{ backgroundColor: 'rgba(74,222,128,0.1)' }}>
              <CheckCircle size={24} style={{ color: '#4ade80' }} />
            </div>
            <h2 className="mb-2 text-lg font-bold text-white">Check your email</h2>
            <p className="mb-2 text-sm" style={{ color: '#888' }}>
              If an account with that email exists, we've sent a password reset link.
            </p>
            <p className="mb-6 text-xs" style={{ color: '#555' }}>
              It may take a few minutes to arrive. Check your spam folder if you don't see it.
            </p>
            <Link
              href="/"
              className="inline-block border px-5 py-2.5 text-sm font-medium transition-opacity hover:opacity-80"
              style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}
            >
              Back to login &rarr;
            </Link>
          </div>
        )}

        {/* Form */}
        {status !== 'success' && (
          <div className="border p-8" style={{ borderColor: '#222', backgroundColor: '#111' }}>
            <div className="mb-6 flex items-center gap-3">
              <div className="flex items-center justify-center w-8 h-8 rounded-lg" style={{ backgroundColor: 'rgba(59,130,246,0.1)' }}>
                <Mail size={15} style={{ color: '#60a5fa' }} />
              </div>
              <div>
                <h2 className="text-sm font-semibold text-white">Forgot password</h2>
                <p className="text-xs" style={{ color: '#555' }}>
                  Enter your email and we'll send you a reset link
                </p>
              </div>
            </div>

            <form onSubmit={handleSubmit} className="flex flex-col gap-5">
              <div>
                <label className="mb-1.5 block text-xs uppercase tracking-widest" style={{ color: '#555' }}>
                  Email address
                </label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoFocus
                  className="w-full border px-3 py-2 text-sm text-white outline-none transition-colors focus:border-white/20"
                  style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}
                  placeholder="you@example.com"
                />
              </div>

              {error && (
                <div
                  className="border px-3 py-2 text-xs"
                  style={{
                    backgroundColor: 'rgba(239,68,68,0.05)',
                    borderColor: 'rgba(239,68,68,0.2)',
                    color: '#ef4444',
                  }}
                >
                  {error}
                </div>
              )}

              <button
                type="submit"
                disabled={status === 'loading'}
                className="w-full border py-2.5 text-sm font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
                style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}
              >
                {status === 'loading' ? (
                  <span className="flex items-center justify-center gap-2">
                    <Loader2 size={14} className="animate-spin" /> Sending...
                  </span>
                ) : (
                  'Send reset link'
                )}
              </button>
            </form>
          </div>
        )}
      </div>
    </div>
  )
}
