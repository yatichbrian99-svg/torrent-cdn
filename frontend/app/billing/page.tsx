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

  if (loading) return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={20} className="animate-spin" style={{ color: '#555' }} /></div>
  if (!status) return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-3">
      <p className="text-sm" style={{ color: '#555' }}>{loadError || 'Failed to load'}</p>
      <button
        onClick={() => { setLoading(true); setLoadError(''); window.location.reload() }}
        className="border px-4 py-2 text-xs font-bold uppercase tracking-widest text-white transition-colors hover:bg-white hover:text-black"
        style={{ borderColor: '#333' }}
      >
        Retry
      </button>
    </div>
  )

  return (
    <>
      {success && (
        <div className="mb-6 flex items-center gap-2 border px-4 py-3 text-sm text-white" style={{ borderColor: '#fff' }}>
          <CheckCircle size={16} /> Plan upgraded successfully!
        </div>
      )}
      {canceled && (
        <div className="mb-6 flex items-center gap-2 border px-4 py-3 text-sm text-white" style={{ borderColor: '#fff' }}>
          <XCircle size={16} /> Checkout canceled.
        </div>
      )}

      <h1 className="text-xl font-bold tracking-[-0.02em] text-white">Billing</h1>
      <div className="mt-8 grid gap-6 md:grid-cols-2">
        <div className="border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <p className="mb-1 text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Current Plan</p>
          <p className="text-lg font-bold capitalize text-white">{status.plan}</p>
        </div>
        <div className="border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <UsageBar used={status.usage_gb} limit={status.limit_gb} />
        </div>
      </div>

      {status.can_upgrade && (
        <div className="mt-10">
          <h2 className="mb-6 text-lg font-bold tracking-[-0.02em] text-white">Upgrade your plan</h2>
          <PricingTable currentPlan={status.plan} />
        </div>
      )}

      {!status.can_upgrade && (
        <div className="mt-10 border p-6 text-center" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <p className="text-sm font-medium text-white">You&apos;re on the Business plan</p>
          <p className="mt-1 text-sm" style={{ color: '#555' }}>Unlimited bandwidth with all features.</p>
        </div>
      )}
    </>
  )
}

export default function BillingPage() {
  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <Suspense fallback={<div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={20} className="animate-spin" style={{ color: '#555' }} /></div>}>
        <BillingContent />
      </Suspense>
    </div>
  )
}
