'use client';

import { Suspense, useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { CheckCircle, XCircle, Loader2 } from 'lucide-react';
import { isLoggedIn } from '@/lib/auth';
import api from '@/lib/api';
import UsageBar from '@/components/UsageBar';
import PricingTable from '@/components/PricingTable';

interface BillingStatus { plan: string; usage_gb: number; limit_gb: number; can_upgrade: boolean; }

function BillingContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<BillingStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const success = searchParams.get('success') === 'true';
  const canceled = searchParams.get('canceled') === 'true';

  useEffect(() => {
    if (!isLoggedIn()) { router.push('/'); return; }
    api.getBillingStatus().then(setStatus).catch(() => {}).finally(() => setLoading(false));
  }, [router]);

  if (loading) return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={24} className="animate-spin text-gray-500" /></div>;
  if (!status) return <div className="flex min-h-[60vh] items-center justify-center"><p className="text-sm text-gray-500">Failed to load billing info</p></div>;

  return (
    <>
      {success && (
        <div className="mb-6 flex items-center gap-2 rounded-lg border border-green-500/20 bg-green-500/5 px-4 py-3 text-sm text-green-400">
          <CheckCircle size={18} /> Your plan has been upgraded successfully!
        </div>
      )}
      {canceled && (
        <div className="mb-6 flex items-center gap-2 rounded-lg border border-yellow-500/20 bg-yellow-500/5 px-4 py-3 text-sm text-yellow-400">
          <XCircle size={18} /> Checkout was canceled. No changes were made.
        </div>
      )}

      <h1 className="text-2xl font-bold text-white">Billing</h1>
      <p className="mt-1 mb-10 text-sm text-gray-500">Manage your plan and view usage</p>

      <div className="mb-10 grid gap-6 md:grid-cols-2">
        <div className="rounded-xl border border-surface-border bg-surface-card p-6">
          <p className="text-sm text-gray-500">Current Plan</p>
          <div className="mt-1 flex items-center gap-3">
            <p className="text-xl font-semibold capitalize text-white">{status.plan}</p>
            <span className={`rounded-md px-2 py-0.5 text-xs font-medium ${
              status.plan === 'business' ? 'bg-brand-500/10 text-brand-400' :
              status.plan === 'pro' ? 'bg-brand-500/10 text-brand-400' :
              'bg-gray-500/10 text-gray-500'
            }`}>
              {status.plan === 'free' ? 'Free Tier' : 'Paid'}
            </span>
          </div>
        </div>
        <div className="rounded-xl border border-surface-border bg-surface-card p-6">
          <UsageBar used={status.usage_gb} limit={status.limit_gb} />
        </div>
      </div>

      {status.can_upgrade && (
        <div>
          <h2 className="mb-6 text-lg font-semibold text-white">Upgrade Your Plan</h2>
          <PricingTable currentPlan={status.plan} />
        </div>
      )}

      {!status.can_upgrade && (
        <div className="rounded-xl border border-brand-500/20 bg-brand-500/5 p-8 text-center">
          <p className="text-lg font-semibold text-brand-400">You&apos;re on the Business plan</p>
          <p className="mt-1 text-sm text-gray-500">Unlimited bandwidth with all features unlocked.</p>
        </div>
      )}
    </>
  );
}

export default function BillingPage() {
  return (
    <div className="mx-auto max-w-5xl px-6 py-10 lg:px-8">
      <Suspense fallback={<div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={24} className="animate-spin text-gray-500" /></div>}>
        <BillingContent />
      </Suspense>
    </div>
  );
}
