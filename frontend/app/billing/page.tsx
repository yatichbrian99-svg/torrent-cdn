'use client';

import { useState, useEffect } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import {
  CreditCard, CheckCircle2, AlertCircle, ArrowLeft,
  Zap, HardDrive, Crown, Loader2,
} from 'lucide-react';
import api from '@/lib/api';

interface BillingStatus {
  plan: string;
  usage_gb: number;
  limit_gb: number;
  can_upgrade: boolean;
}

const PLAN_DETAILS: Record<string, { name: string; color: string; icon: any; features: string[] }> = {
  free: {
    name: 'Free',
    color: 'text-slate-400',
    icon: Zap,
    features: ['100GB bandwidth/month', 'Basic analytics', 'Community support'],
  },
  pro: {
    name: 'Pro',
    color: 'text-primary-400',
    icon: Crown,
    features: ['1TB bandwidth/month', 'Advanced analytics', 'Priority support', 'Custom embed domain'],
  },
  business: {
    name: 'Business',
    color: 'text-purple-400',
    icon: Crown,
    features: ['Unlimited bandwidth', 'Real-time analytics', '24/7 support', 'SSO & team accounts', 'SLA guarantee'],
  },
};

const PLAN_LIMITS: Record<string, { price: number; limit_gb: number }> = {
  free: { price: 0, limit_gb: 100 },
  pro: { price: 29, limit_gb: 1024 },
  business: { price: 99, limit_gb: Infinity },
};

export default function BillingPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<BillingStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [checkoutLoading, setCheckoutLoading] = useState(false);
  const [error, setError] = useState('');
  const [successMsg, setSuccessMsg] = useState('');

  useEffect(() => {
    if (!api.isAuthenticated()) {
      router.push('/');
      return;
    }

    // Check for Stripe redirect
    if (searchParams.get('success') === 'true') {
      setSuccessMsg('Payment successful! Your plan has been upgraded.');
    }

    loadBillingStatus();
  }, []);

  const loadBillingStatus = async () => {
    try {
      setLoading(true);
      const res = await api.getBillingStatus();
      setStatus(res.data);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load billing status');
    } finally {
      setLoading(false);
    }
  };

  const handleUpgrade = async (plan: string) => {
    setCheckoutLoading(true);
    setError('');

    try {
      const res = await api.createCheckout(plan);
      window.location.href = res.data.url;
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to create checkout session');
      setCheckoutLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-primary-400" />
      </div>
    );
  }

  const currentPlan = status ? PLAN_DETAILS[status.plan] || PLAN_DETAILS.free : PLAN_DETAILS.free;
  const usagePercent = status && status.limit_gb !== Infinity
    ? Math.min((status.usage_gb / status.limit_gb) * 100, 100)
    : 0;
  const isOverLimit = status && status.limit_gb !== Infinity && status.usage_gb >= status.limit_gb;

  return (
    <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
      {/* Back button */}
      <button
        onClick={() => router.push('/dashboard')}
        className="mb-6 inline-flex items-center gap-2 text-sm text-slate-400 transition-colors hover:text-white"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to dashboard
      </button>

      <h1 className="text-2xl font-bold text-white">Billing & Usage</h1>
      <p className="mt-1 text-sm text-slate-400">Manage your plan and monitor bandwidth usage</p>

      {/* Success message */}
      {successMsg && (
        <div className="mt-6 flex items-center gap-3 rounded-lg bg-green-500/10 p-4 text-sm text-green-400">
          <CheckCircle2 className="h-5 w-5 flex-shrink-0" />
          {successMsg}
        </div>
      )}

      {/* Error message */}
      {error && (
        <div className="mt-6 flex items-center gap-3 rounded-lg bg-red-500/10 p-4 text-sm text-red-400">
          <AlertCircle className="h-5 w-5 flex-shrink-0" />
          {error}
          <button onClick={() => setError('')} className="ml-auto underline">Dismiss</button>
        </div>
      )}

      {/* Current Plan */}
      {status && (
        <div className="mt-8">
          <div className="rounded-xl border border-slate-700/50 bg-slate-800/30 p-6 backdrop-blur-xl">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary-500/10">
                  <currentPlan.icon className={`h-6 w-6 ${currentPlan.color}`} />
                </div>
                <div>
                  <h2 className="text-lg font-semibold text-white">
                    {currentPlan.name} Plan
                  </h2>
                  <p className="text-sm text-slate-400">
                    {status.limit_gb === Infinity
                      ? 'Unlimited bandwidth'
                      : `${status.limit_gb.toLocaleString()} GB bandwidth per month`
                    }
                  </p>
                </div>
              </div>
              {status.can_upgrade && (
                <button
                  onClick={() => handleUpgrade('pro')}
                  disabled={checkoutLoading}
                  className="rounded-lg bg-gradient-to-r from-primary-600 to-purple-600 px-4 py-2 text-sm font-medium text-white shadow-lg transition-all hover:from-primary-500 hover:to-purple-500 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {checkoutLoading ? (
                    <span className="flex items-center gap-2">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Processing...
                    </span>
                  ) : (
                    'Upgrade plan'
                  )}
                </button>
              )}
            </div>
          </div>

          {/* Usage Bar */}
          <div className="mt-6 rounded-xl border border-slate-700/50 bg-slate-800/30 p-6 backdrop-blur-xl">
            <h3 className="text-lg font-semibold text-white">Monthly Bandwidth Usage</h3>
            <div className="mt-4">
              <div className="flex items-center justify-between text-sm">
                <span className="text-slate-400">
                  {status.usage_gb.toFixed(2)} GB used
                </span>
                <span className="text-slate-400">
                  {status.limit_gb === Infinity ? 'Unlimited' : `${status.limit_gb.toLocaleString()} GB limit`}
                </span>
              </div>
              {status.limit_gb !== Infinity && (
                <>
                  <div className="mt-2 h-3 overflow-hidden rounded-full bg-slate-700">
                    <div
                      className={`h-full rounded-full transition-all duration-500 ${
                        isOverLimit
                          ? 'bg-red-500'
                          : usagePercent > 80
                          ? 'bg-amber-500'
                          : 'bg-gradient-to-r from-primary-500 to-primary-400'
                      }`}
                      style={{ width: `${usagePercent}%` }}
                    />
                  </div>
                  <p className={`mt-1 text-xs ${isOverLimit ? 'text-red-400' : 'text-slate-500'}`}>
                    {isOverLimit
                      ? 'You have exceeded your monthly limit. Upgrade to continue uploading.'
                      : `${Math.round(usagePercent)}% of your monthly limit used`
                    }
                  </p>
                </>
              )}
            </div>
          </div>

          {/* Plan Features */}
          <div className="mt-6 rounded-xl border border-slate-700/50 bg-slate-800/30 p-6 backdrop-blur-xl">
            <h3 className="text-lg font-semibold text-white">Plan Features</h3>
            <ul className="mt-4 space-y-2">
              {currentPlan.features.map((feature) => (
                <li key={feature} className="flex items-center gap-2 text-sm text-slate-300">
                  <CheckCircle2 className="h-4 w-4 text-green-400" />
                  {feature}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {/* Upgrade Options */}
      <div className="mt-12">
        <h2 className="text-xl font-bold text-white">Available Plans</h2>
        <p className="mt-1 text-sm text-slate-400">Choose the plan that fits your needs</p>

        <div className="mt-6 grid gap-6 md:grid-cols-3">
          {Object.entries(PLAN_DETAILS).map(([key, plan]) => {
            const limit = PLAN_LIMITS[key];
            const isCurrentPlan = status?.plan === key;
            const PlanIcon = plan.icon;

            return (
              <div
                key={key}
                className={`relative rounded-xl border p-6 backdrop-blur-xl transition-all ${
                  isCurrentPlan
                    ? 'border-primary-500/50 bg-primary-500/10'
                    : 'border-slate-700/50 bg-slate-800/30 hover:border-slate-600/50'
                }`}
              >
                {isCurrentPlan && (
                  <div className="absolute -top-2.5 left-1/2 -translate-x-1/2">
                    <span className="rounded-full bg-primary-600 px-3 py-0.5 text-xs font-medium text-white">
                      Current plan
                    </span>
                  </div>
                )}

                <PlanIcon className={`h-8 w-8 ${plan.color}`} />
                <h3 className="mt-3 text-lg font-semibold text-white">{plan.name}</h3>
                <div className="mt-2">
                  <span className="text-3xl font-bold text-white">${limit.price}</span>
                  <span className="text-slate-400">/month</span>
                </div>

                <ul className="mt-4 space-y-2">
                  {plan.features.map((feature) => (
                    <li key={feature} className="flex items-center gap-2 text-sm text-slate-300">
                      <CheckCircle2 className="h-4 w-4 text-green-400" />
                      {feature}
                    </li>
                  ))}
                </ul>

                {!isCurrentPlan && (
                  <button
                    onClick={() => handleUpgrade(key)}
                    disabled={checkoutLoading}
                    className={`mt-6 w-full rounded-lg px-4 py-2.5 text-sm font-medium transition-all ${
                      key === 'business'
                        ? 'border border-slate-600 text-white hover:bg-slate-700'
                        : 'bg-gradient-to-r from-primary-600 to-purple-600 text-white shadow-lg hover:from-primary-500 hover:to-purple-500'
                    } disabled:cursor-not-allowed disabled:opacity-50`}
                  >
                    {checkoutLoading ? 'Processing...' : key === 'pro' ? 'Upgrade to Pro' : 'Contact sales'}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
