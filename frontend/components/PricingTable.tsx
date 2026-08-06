'use client'

import { useState } from 'react'
import { Loader2 } from 'lucide-react'
import api from '@/lib/api'
import { isSafeHttpsUrl } from '@/lib/security'

// Priced in KES to match Paystack Kenya (the provider for this market — Stripe
// does not support Kenyan merchant accounts). Paystack charges customers in KES
// regardless of where their card is issued.
const TIERS = [
  { name: 'Free', price: 'KES 0', period: '/month', limit: '100GB bandwidth', features: ['1 seat', 'Community support'], plan: 'free' },
  { name: 'Pro', price: 'KES 4,000', period: '/month', limit: '1TB bandwidth', features: ['Unlimited files', 'Analytics dashboard', 'Email support'], plan: 'pro', popular: true },
  { name: 'Business', price: 'KES 12,000', period: '/month', limit: 'Unlimited bandwidth', features: ['Priority support', 'Custom domain', 'SLA guarantee'], plan: 'business' },
]

export default function PricingTable({ currentPlan }: { currentPlan?: string }) {
  const [loading, setLoading] = useState<string | null>(null)
  const [error, setError] = useState('')

  const handleClick = async (plan: string) => {
    if (plan === 'free') { window.location.href = '/dashboard/upload'; return }
    setLoading(plan)
    setError('')
    try {
      const data = await api.createCheckout(plan)
      // The checkout URL comes from the billing backend, but a redirect target
      // deserves a scheme check regardless of the source: only ever navigate
      // to an absolute https:// URL (rejects javascript:/data:/http: values
      // and relative paths if the backend ever misbehaves).
      if (data.url && isSafeHttpsUrl(data.url)) {
        window.location.href = data.url
      } else {
        setError('Checkout failed. Please try again.')
        setLoading(null)
      }
    } catch {
      setError('Checkout failed. Please try again.')
      setLoading(null)
    }
  }

  return (
    <div>
      {error && (
        <p className="mb-4 text-center text-sm" style={{ color: '#ff6b6b' }}>{error}</p>
      )}
      <div className="grid gap-4 md:grid-cols-3">
        {TIERS.map((tier) => {
          const isCurrent = currentPlan === tier.plan
          return (
            <div key={tier.plan} className="relative border p-6" style={{ borderColor: tier.popular ? '#fff' : '#222', backgroundColor: '#111' }}>
              {tier.popular && (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2 border px-3 py-0.5 text-[10px] font-bold uppercase tracking-widest" style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000', fontFamily: 'monospace' }}>
                  Most popular
                </div>
              )}
              <h3 className="text-sm font-bold uppercase tracking-wide text-white">{tier.name}</h3>
              <div className="my-4">
                <span className="text-3xl font-bold text-white">{tier.price}</span>
                <span className="text-sm" style={{ color: '#555' }}>{tier.period}</span>
              </div>
              <p className="mb-4 text-sm" style={{ color: '#555' }}>{tier.limit}</p>
              <ul className="mb-6 space-y-2">
                {tier.features.map((f) => (
                  <li key={f} className="flex items-start gap-2 text-sm" style={{ color: '#555' }}><span className="text-white">&ndash;</span> {f}</li>
                ))}
              </ul>
              <button
                onClick={() => handleClick(tier.plan)}
                disabled={isCurrent || loading === tier.plan}
                className="w-full border py-2 text-sm font-medium transition-opacity hover:opacity-80 disabled:opacity-40"
                style={{
                  borderColor: isCurrent ? '#333' : '#fff',
                  backgroundColor: isCurrent ? 'transparent' : tier.popular ? '#fff' : 'transparent',
                  color: isCurrent ? '#555' : tier.popular ? '#000' : '#fff',
                }}
              >
                {loading === tier.plan ? <Loader2 size={14} className="mx-auto animate-spin" /> : isCurrent ? 'Current plan' : 'Get started'}
              </button>
            </div>
          )
        })}
      </div>
    </div>
  )
}
