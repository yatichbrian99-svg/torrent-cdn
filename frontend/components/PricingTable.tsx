'use client';

import { useState } from 'react';
import { Check, Loader2 } from 'lucide-react';
import api from '@/lib/api';

const TIERS = [
  {
    name: 'Free',
    price: '$0',
    period: '/month',
    description: 'Perfect for getting started',
    limit: '100 GB',
    features: ['100 GB monthly bandwidth', 'Basic analytics', 'Embed player', 'Community support'],
    cta: 'Get Started',
    plan: 'free',
    highlighted: false,
  },
  {
    name: 'Pro',
    price: '$29',
    period: '/month',
    description: 'For content creators and small teams',
    limit: '1 TB',
    features: ['1 TB monthly bandwidth', 'Advanced analytics', 'Priority support', 'Custom domain', 'No TorrentCDN branding'],
    cta: 'Upgrade to Pro',
    plan: 'pro',
    highlighted: true,
  },
  {
    name: 'Business',
    price: '$99',
    period: '/month',
    description: 'For businesses at scale',
    limit: 'Unlimited',
    features: ['Unlimited bandwidth', 'Real-time analytics', 'Dedicated support', 'Custom domain', 'White-label solution', 'API access', 'SLA guarantee'],
    cta: 'Contact Sales',
    plan: 'business',
    highlighted: false,
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
      if (data.url) {
        window.location.href = data.url;
      }
    } catch (err) {
      setLoading(null);
    }
  };

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {TIERS.map((tier) => {
        const isCurrent = currentPlan === tier.plan;
        return (
          <div
            key={tier.plan}
            className={`border p-6 transition-colors ${
              tier.highlighted
                ? 'border-accent-500/50 bg-accent-500/5'
                : 'border-[#1f1f1f] bg-[#111] hover:border-gray-700'
            }`}
          >
            {tier.highlighted && (
              <div className="mb-3 inline-block border border-accent-500/30 bg-accent-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-accent-500">
                Most Popular
              </div>
            )}
            <h3 className="text-sm font-semibold text-white">{tier.name}</h3>
            <p className="mt-1 text-xs text-gray-500">{tier.description}</p>
            <div className="my-4">
              <span className="text-2xl font-bold text-white">{tier.price}</span>
              <span className="text-xs text-gray-500">{tier.period}</span>
            </div>
            <p className="mb-4 text-xs text-gray-500">
              <span className="text-white">{tier.limit}</span> bandwidth
            </p>
            <ul className="mb-6 space-y-2">
              {tier.features.map((feature) => (
                <li key={feature} className="flex items-start gap-2 text-xs text-gray-400">
                  <Check size={12} className="mt-0.5 shrink-0 text-accent-500" />
                  {feature}
                </li>
              ))}
            </ul>
            <button
              onClick={() => handleUpgrade(tier.plan)}
              disabled={isCurrent || loading === tier.plan}
              className={`w-full border px-4 py-2 text-xs font-medium transition-colors ${
                isCurrent
                  ? 'border-green-500/30 bg-green-500/5 text-green-500 cursor-default'
                  : tier.highlighted
                  ? 'border-accent-500 bg-accent-500 text-black hover:bg-accent-400'
                  : 'border-[#1f1f1f] text-gray-300 hover:border-gray-700 hover:text-white'
              } disabled:opacity-50`}
            >
              {loading === tier.plan ? (
                <Loader2 size={14} className="mx-auto animate-spin" />
              ) : isCurrent ? (
                'Current Plan'
              ) : (
                tier.cta
              )}
            </button>
          </div>
        );
      })}
    </div>
  );
}
