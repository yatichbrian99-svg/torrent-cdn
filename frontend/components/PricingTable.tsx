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
    if (plan === 'free') {
      window.location.href = '/dashboard/upload';
      return;
    }
    setLoading(plan);
    try {
      const data = await api.createCheckout(plan);
      if (data.url) window.location.href = data.url;
    } catch {
      setLoading(null);
    }
  };

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {TIERS.map((tier) => {
        const isCurrent = currentPlan === tier.plan;
        return (
          <div key={tier.plan} className="border border-[#1f1f1f] bg-[#111] p-5">
            <h3 className="text-sm font-semibold text-white">{tier.name}</h3>
            <div className="my-3">
              <span className="text-2xl font-bold text-white">{tier.price}</span>
              <span className="text-xs text-gray-500">{tier.period}</span>
            </div>
            <p className="mb-4 text-xs text-gray-500">{tier.limit} bandwidth</p>
            <ul className="mb-6 space-y-1.5">
              {tier.features.map((feature) => (
                <li key={feature} className="flex items-start gap-2 text-xs text-gray-400">
                  <Check size={12} className="mt-0.5 shrink-0 text-gray-600" />
                  {feature}
                </li>
              ))}
            </ul>
            <button
              onClick={() => handleUpgrade(tier.plan)}
              disabled={isCurrent || loading === tier.plan}
              className={`w-full rounded border px-4 py-2 text-xs transition-colors ${
                isCurrent
                  ? 'border-gray-700 bg-gray-800 text-gray-500 cursor-default'
                  : 'border-gray-700 bg-gray-800 text-white hover:bg-gray-700'
              } disabled:opacity-50`}
            >
              {loading === tier.plan ? (
                <Loader2 size={14} className="mx-auto animate-spin" />
              ) : isCurrent ? (
                'Current Plan'
              ) : (
                `Upgrade to ${tier.name}`
              )}
            </button>
          </div>
        );
      })}
    </div>
  );
}
