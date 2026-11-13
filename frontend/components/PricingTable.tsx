'use client'

import { useState } from 'react'
import { Check, Loader2 } from 'lucide-react'
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
        <p className="mb-4 text-center text-sm" style={{ color: 'var(--err-bright)' }} role="alert">{error}</p>
      )}
      <div className="pricing-grid">
        {TIERS.map((tier) => {
          const isCurrent = currentPlan === tier.plan
          return (
            <div
              key={tier.plan}
              className={`price-card${tier.popular ? ' price-card-popular' : ''}`}
            >
              {tier.popular && <span className="price-badge">Most popular</span>}
              <h3 className="price-name">{tier.name}</h3>
              <div className="price-row">
                <span className="price-amount">{tier.price}</span>
                <span className="price-period">{tier.period}</span>
              </div>
              <p className="price-limit">{tier.limit}</p>
              <ul className="price-features">
                {tier.features.map((f) => (
                  <li key={f} className="price-feature">
                    <Check size={14} className="price-check" aria-hidden="true" />
                    <span>{f}</span>
                  </li>
                ))}
              </ul>
              <div className="price-action">
                <button
                  onClick={() => handleClick(tier.plan)}
                  disabled={isCurrent || loading === tier.plan}
                  className={`btn w-full ${tier.popular ? 'btn-primary' : 'btn-secondary'}`}
                  style={isCurrent ? { borderColor: 'var(--border-strong)', color: 'var(--muted)', background: 'transparent' } : undefined}
                >
                  {loading === tier.plan ? (
                    <Loader2 size={14} className="animate-spin" aria-label="Loading" />
                  ) : isCurrent ? (
                    'Current plan'
                  ) : (
                    'Get started'
                  )}
                </button>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
