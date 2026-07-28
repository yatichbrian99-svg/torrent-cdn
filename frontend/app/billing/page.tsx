'use client';

import { Suspense, useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { CheckCircle, XCircle, Loader2 } from 'lucide-react';
import { isLoggedIn } from '@/lib/auth';
import api from '@/lib/api';
import UsageBar from '@/components/UsageBar';
import PricingTable from '@/components/PricingTable';

interface BillingStatus {
  plan: string;
  usage_gb: number;
  limit_gb: number;
  can_upgrade: boolean;
}

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

  if (loading) {
    return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={20} className="animate-spin text-gray-500" /></div>;
  }

  if (!status) {
    return <div className="flex min-h-[60vh] items-center justify-center"><p className="text-sm text-gray-500">Failed to load billing info</p></div>;
  }

  return (
    <>
      {success && (
        <div className="mb-6 flex items-center gap-2 border border-green-500/20 bg-green-500/5 px-4 py-3 text-xs text-green-500">
          <CheckCircle size={14} /> Your plan has been upgraded successfully!
        </div>
      )}
      {canceled && (
        <div className="mb-6 flex items-center gap-2 border border-yellow-500/20 bg-yellow-500/5 px-4 py-3 text-xs text-yellow-500">
          <XCircle size={14} /> Checkout was canceled. No changes were made.
        </div>
      )}

      <h1 className="mb-1 text-lg font-semibold text-white">Billing</h1>
      <p className="mb-8 text-xs text-gray-500">Manage your plan and view usage</p>

      <div className="mb-8 grid gap-4 md:grid-cols-2">
        <div className="border border-[#1f1f1f] bg-[#111] p-5">
          <p className="mb-1 text-xs text-gray-500">Current Plan</p>
          <p className="text-base font-semibold capitalize text-white">{status.plan}</p>
        </div>
        <div className="border border-[#1f1f1f] bg-[#111] p-5">
          <UsageBar used={status.usage_gb} limit={status.limit_gb} />
        </div>
      </div>

      {status.can_upgrade && (
        <div>
          <h2 className="mb-6 text-sm font-semibold text-white">Upgrade Your Plan</h2>
          <PricingTable currentPlan={status.plan} />
        </div>
      )}

      {!status.can_upgrade && (
        <div className="border border-gray-800 bg-[#111] p-6 text-center">
          <p className="text-sm font-medium text-white">You're on the Business plan</p>
          <p className="mt-1 text-xs text-gray-500">Unlimited bandwidth with all features.</p>
        </div>
      )}
    </>
  );
}

export default function BillingPage() {
  return (
    <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6">
      <Suspense fallback={<div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={20} className="animate-spin text-gray-500" /></div>}>
        <BillingContent />
      </Suspense>
    </div>
  );
}
