'use client'

import { Suspense, useEffect, useState } from 'react'
import { useSearchParams, useRouter } from 'next/navigation'
import { Loader2, CheckCircle, XCircle } from 'lucide-react'
import api from '@/lib/api'

export default function VerifyEmailPage() {
  return (
    <Suspense fallback={
      <div className="flex min-h-[80vh] items-center justify-center">
        <Loader2 size={20} className="animate-spin" style={{ color: 'var(--text-muted)' }} />
      </div>
    }>
      <VerifyEmailContent />
    </Suspense>
  )
}

function VerifyEmailContent() {
  const searchParams = useSearchParams()
  const router = useRouter()
  const token = searchParams.get('token')
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading')
  const [message, setMessage] = useState('')

  useEffect(() => {
    if (!token) {
      setStatus('error')
      setMessage('No verification token found.')
      return
    }

    api.verifyEmail(token)
      .then(() => {
        setStatus('success')
        setMessage('Email verified successfully!')
      })
      .catch((err) => {
        setStatus('error')
        setMessage(err.response?.data?.detail || 'Verification failed. The link may have expired.')
      })
  }, [token])

  return (
    <div className="flex min-h-[80vh] items-center justify-center">
      <div className="w-full max-w-sm border p-8 text-center" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
        {status === 'loading' && (
          <>
            <Loader2 size={32} className="mx-auto mb-4 animate-spin" style={{ color: 'var(--text-muted)' }} />
            <p className="text-sm text-[color:var(--text-primary)]">Verifying your email...</p>
          </>
        )}
        {status === 'success' && (
          <>
            <CheckCircle size={32} className="mx-auto mb-4" style={{ color: '#4ade80' }} />
            <h2 className="mb-2 text-lg font-bold text-[color:var(--text-primary)]">Email Verified</h2>
            <p className="mb-6 text-sm" style={{ color: 'var(--text-soft)' }}>{message}</p>
            <button
              onClick={() => router.push('/dashboard')}
              className="border px-4 py-2 text-sm font-medium"
              style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
            >
              Go to Dashboard &rarr;
            </button>
          </>
        )}
        {status === 'error' && (
          <>
            <XCircle size={32} className="mx-auto mb-4" style={{ color: '#ef4444' }} />
            <h2 className="mb-2 text-lg font-bold text-[color:var(--text-primary)]">Verification Failed</h2>
            <p className="mb-6 text-sm" style={{ color: 'var(--text-soft)' }}>{message}</p>
            <button
              onClick={() => router.push('/dashboard')}
              className="border px-4 py-2 text-sm font-medium"
              style={{ borderColor: 'var(--inverse-bg)', color: '#fff' }}
            >
              Back to Dashboard
            </button>
          </>
        )}
      </div>
    </div>
  )
}
