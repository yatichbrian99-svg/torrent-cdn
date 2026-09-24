'use client'

import { Suspense, useEffect, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { CheckCircle, XCircle, Loader2 } from 'lucide-react'
import { isLoggedIn } from '@/lib/auth'
import api, { isTransientError, retryOnTransient } from '@/lib/api'
import UsageBar from '@/components/UsageBar'
import PricingTable from '@/components/PricingTable'

interface BillingStatus { plan: string; usage_gb: number; limit_gb: number; can_upgrade: boolean }

function BillingContent() {
  const router = useRouter()
  const sp = useSearchParams()
  const [status, setStatus] = useState<BillingStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const success = sp.get('success') === 'true'
  const canceled = sp.get('canceled') === 'true'

  useEffect(() => {
    if (!isLoggedIn()) { router.push('/'); return }
    // Retry transient failures (Render cold start: 502/503/504 or timeout) and
    // REMEMBER why the load failed — the old code swallowed the error, so a
    // cold start rendered a dead-end 'Failed to load' with no explanation and
    // no way to recover without a full page reload.
    retryOnTransient(() => api.getBillingStatus())
      .then((d) => { setStatus(d); setLoadError('') })
      .catch((err: any) => {
        setLoadError(
          err?.response?.status === 401
            ? 'Your session expired — please log in again.'
            : isTransientError(err)
              ? 'Still connecting to the server — please try again in a moment.'
              : 'Could not load billing data. Please try again.'
        )
      })
      .finally(() => setLoading(false))
  }, [router])

  if (loading) return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={20} className="animate-spin" style={{ color: 'var(--text-muted)' }} /></div>
  if (!status) return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-3">
      <p className="text-sm" style={{ color: 'var(--text-muted)' }}>{loadError || 'Failed to load'}</p>
      <button
        onClick={() => { setLoading(true); setLoadError(''); window.location.reload() }}
        className="border px-4 py-2 text-xs font-bold uppercase tracking-widest text-[color:var(--text-primary)] transition-colors hover:bg-[color:var(--inverse-bg)] hover:text-[color:var(--inverse-fg)]"
        style={{ borderColor: 'var(--border-strong)' }}
      >
        Retry
      </button>
    </div>
  )

  return (
    <>
      {success && (
        <div className="mb-6 flex items-center gap-2 border px-4 py-3 text-sm text-[color:var(--text-primary)]" style={{ borderColor: 'var(--inverse-bg)' }}>
          <CheckCircle size={16} /> Plan upgraded successfully!
        </div>
      )}
      {canceled && (
        <div className="mb-6 flex items-center gap-2 border px-4 py-3 text-sm text-[color:var(--text-primary)]" style={{ borderColor: 'var(--inverse-bg)' }}>
          <XCircle size={16} /> Checkout canceled.
        </div>
      )}

      <h1 className="text-xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">Billing</h1>
      <div className="mt-8 grid gap-6 md:grid-cols-2">
        <div className="border p-5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
          <p className="mb-1 text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>Current Plan</p>
          <p className="text-lg font-bold capitalize text-[color:var(--text-primary)]">{status.plan}</p>
        </div>
        <div className="border p-5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
          <UsageBar used={status.usage_gb} limit={status.limit_gb} />
        </div>
      </div>

      {status.can_upgrade && (
        <div className="mt-10">
          <h2 className="mb-6 text-lg font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">Upgrade your plan</h2>
          <PricingTable currentPlan={status.plan} />
        </div>
      )}

      {!status.can_upgrade && (
        <div className="mt-10 border p-6 text-center" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
          <p className="text-sm font-medium text-[color:var(--text-primary)]">You&apos;re on the Business plan</p>
          <p className="mt-1 text-sm" style={{ color: 'var(--text-muted)' }}>Unlimited bandwidth with all features.</p>
        </div>
      )}
    </>
  )
}

export default function BillingPage() {
  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <Suspense fallback={<div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={20} className="animate-spin" style={{ color: 'var(--text-muted)' }} /></div>}>
        <BillingContent />
      </Suspense>
    </div>
  )
}
