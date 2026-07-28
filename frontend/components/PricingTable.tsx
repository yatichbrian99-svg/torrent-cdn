'use client';

import { useState } from 'react';
import { Check, Loader2 } from 'lucide-react';
import api from '@/lib/api';

const TIERS = [
  {
    name: 'Free',
    price: '$0',
    period: '/month',
    limit: '100 GB',
    features: ['100 GB monthly bandwidth', 'Basic analytics', 'Embed player', 'Community support'],
    plan: 'free',
  },
  {
    name: 'Pro',
    price: '$29',
    period: '/month',
    limit: '1 TB',
    features: ['1 TB monthly bandwidth', 'Advanced analytics', 'Priority support', 'Custom domain', 'No branding'],
    plan: 'pro',
    popular: true,
  },
  {
    name: 'Business',
    price: '$99',
    period: '/month',
    limit: 'Unlimited',
    features: ['Unlimited bandwidth', 'Real-time analytics', 'Dedicated support', 'Custom domain', 'White-label', 'API access', 'SLA'],
    plan: 'business',
  },
];

export default function PricingTable({ currentPlan }: { currentPlan?: string }) {
  const [loading, setLoading] = useState<string | null>(null);

  const handleUpgrade = async (plan: string) => {
    if (plan === 'free') { window.location.href = '/dashboard/upload'; return; }
    setLoading(plan);
    try {
      const data = await api.createCheckout(plan);
      if (data.url) window.location.href = data.url;
    } catch { setLoading(null); }
  };

  return (
    <div className="grid gap-6 md:grid-cols-3">
      {TIERS.map((tier) => {
        const isCurrent = currentPlan === tier.plan;
        return (
          <div
            key={tier.plan}
            className={`relative rounded-xl border bg-surface-card p-6 transition-all ${
              tier.popular ? 'border-brand-500/40 shadow-lg shadow-brand-500/5' : 'border-surface-border hover:border-gray-700'
            }`}
          >
            {tier.popular && (
              <div className="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-brand-600 px-3 py-0.5 text-xs font-medium text-white">
                Most Popular
              </div>
            )}

            <h3 className="text-base font-semibold text-white">{tier.name}</h3>
            <div className="my-4">
              <span className="text-3xl font-bold text-white">{tier.price}</span>
              <span className="text-sm text-gray-500">{tier.period}</span>
            </div>
            <p className="mb-4 text-sm text-gray-400">{tier.limit} bandwidth</p>

            <ul className="mb-8 space-y-2.5">
              {tier.features.map((feature) => (
                <li key={feature} className="flex items-start gap-2.5 text-sm text-gray-400">
                  <Check size={16} className="mt-0.5 shrink-0 text-brand-400" />
                  {feature}
                </li>
              ))}
            </ul>

            <button
              onClick={() => handleUpgrade(tier.plan)}
              disabled={isCurrent || loading === tier.plan}
              className={`w-full rounded-lg px-4 py-2.5 text-sm font-medium transition-all disabled:opacity-50 ${
                isCurrent
                  ? 'cursor-default border border-surface-border bg-surface-card text-gray-500'
                  : tier.popular
                  ? 'bg-brand-600 text-white shadow-lg shadow-brand-600/20 hover:bg-brand-500'
                  : 'border border-surface-border text-gray-300 hover:border-gray-600 hover:text-white'
              }`}
            >
              {loading === tier.plan ? (
                <Loader2 size={16} className="mx-auto animate-spin" />
              ) : isCurrent ? (
                'Current Plan'
              ) : (
                `Get ${tier.name}`
              )}
            </button>
          </div>
        );
      })}
    </div>
  );
}
