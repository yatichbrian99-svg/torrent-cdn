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
    if (!isLoggedIn()) {
      router.push('/');
      return;
    }

    api
      .getBillingStatus()
      .then(setStatus)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [router]);

  if (loading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <Loader2 size={20} className="animate-spin text-gray-500" />
      </div>
    );
  }

  if (!status) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <p className="text-sm text-gray-500">Failed to load billing info</p>
      </div>
    );
  }

  return (
    <>
      {/* Success/Canceled messages */}
      {success && (
        <div className="mb-6 flex items-center gap-2 border border-green-500/20 bg-green-500/5 px-4 py-3 text-xs text-green-500">
          <CheckCircle size={14} />
          Your plan has been upgraded successfully!
        </div>
      )}
      {canceled && (
        <div className="mb-6 flex items-center gap-2 border border-yellow-500/20 bg-yellow-500/5 px-4 py-3 text-xs text-yellow-500">
          <XCircle size={14} />
          Checkout was canceled. No changes were made.
        </div>
      )}

      <h1 className="mb-1 text-lg font-semibold text-white">Billing</h1>
      <p className="mb-8 text-xs text-gray-500">
        Manage your plan and view usage
      </p>

      {/* Current Plan + Usage */}
      <div className="mb-8 grid gap-4 md:grid-cols-2">
        <div className="border border-[#1f1f1f] bg-[#111] p-5">
          <p className="mb-1 text-[11px] font-medium uppercase tracking-wider text-gray-500">
            Current Plan
          </p>
          <div className="flex items-center gap-2">
            <span className="text-base font-semibold capitalize text-white">
              {status.plan}
            </span>
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                status.plan === 'business'
                  ? 'bg-accent-500/10 text-accent-500'
                  : status.plan === 'pro'
                  ? 'bg-purple-500/10 text-purple-500'
                  : 'bg-gray-500/10 text-gray-500'
              }`}
            >
              {status.plan === 'free' ? 'Free Tier' : 'Paid'}
            </span>
          </div>
        </div>

        <div className="border border-[#1f1f1f] bg-[#111] p-5">
          <UsageBar used={status.usage_gb} limit={status.limit_gb} />
        </div>
      </div>

      {/* Upgrade Options */}
      {status.can_upgrade && (
        <div>
          <h2 className="mb-6 text-sm font-semibold text-white">
            Upgrade Your Plan
          </h2>
          <PricingTable currentPlan={status.plan} />
        </div>
      )}

      {!status.can_upgrade && (
        <div className="border border-accent-500/20 bg-accent-500/5 p-6 text-center">
          <p className="text-sm font-medium text-accent-500">
            You&apos;re on the Business plan
          </p>
          <p className="mt-1 text-xs text-gray-500">
            Unlimited bandwidth with all features unlocked.
          </p>
        </div>
      )}
    </>
  );
}

export default function BillingPage() {
  return (
    <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
      <Suspense
        fallback={
          <div className="flex min-h-[60vh] items-center justify-center">
            <Loader2 size={20} className="animate-spin text-gray-500" />
          </div>
        }
      >
        <BillingContent />
      </Suspense>
    </div>
  );
}
