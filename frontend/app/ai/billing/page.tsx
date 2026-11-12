'use client'

import { Suspense, useCallback, useEffect, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { isLoggedIn } from '@/lib/auth'
import { apiErrorMessage } from '@/lib/api'
import {
  aiConsole,
  usd,
  type AiCheckout,
  type AiPack,
} from '@/lib/aiConsole'
import { Badge, Button, Card, ErrorState, Loading, Skeleton, Stat } from '@/components/ai/ui'

interface PacksResponse {
  packs: AiPack[]
  currency: { code: string; kes_per_usd: number }
  mode: string
  provider: string
  note: string
}

function BillingContent() {
  const router = useRouter()
  const params = useSearchParams()
  const pending = params.get('pending')
  const simulated = params.get('simulated') === '1'

  const [catalogue, setCatalogue] = useState<PacksResponse | null>(null)
  const [error, setError] = useState('')
  const [busyPack, setBusyPack] = useState<string | null>(null)
  const [checkout, setCheckout] = useState<AiCheckout | null>(null)
  const [polled, setPolled] = useState<'idle' | 'pending' | 'paid' | 'timeout'>('idle')
  const [balance, setBalance] = useState<number | null>(null)

  const load = useCallback(() => {
    aiConsole
      .packs()
      .then(setCatalogue)
      .catch((err: any) => {
        if (err?.response?.status === 401) {
          router.push('/')
          return
        }
        setError(apiErrorMessage(err, 'Could not load credit packs'))
      })
  }, [router])

  const refreshBalance = useCallback(() => {
    aiConsole
      .summary()
      .then((summary) => setBalance(summary.balance.micro))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (!isLoggedIn()) {
      router.push('/')
      return
    }
    load()
    refreshBalance()
  }, [load, refreshBalance, router])

  // Returning from the provider: ask the ledger, not the URL, whether it paid.
  useEffect(() => {
    if (!pending) return
    let attempts = 0
    let cancelled = false
    setPolled('pending')

    const poll = async () => {
      if (cancelled) return
      attempts += 1
      try {
        const result = await aiConsole.checkoutStatus(pending)
        if (cancelled) return
        if (result.status === 'paid') {
          setPolled('paid')
          setBalance(result.balance_micro ?? null)
          return
        }
      } catch {
        // A transient read failure is not a failed payment; keep polling.
      }
      if (attempts >= 20) {
        if (!cancelled) setPolled('timeout')
        return
      }
      setTimeout(poll, 3000)
    }
    poll()
    return () => {
      cancelled = true
    }
  }, [pending])

  const startCheckout = async (pack: AiPack) => {
    setBusyPack(pack.id)
    setError('')
    try {
      const started = await aiConsole.checkout(pack.id)
      setCheckout(started)
      if (started.mode === 'simulated') {
        // No provider in this environment: stay on the page so the state
        // machine is still exercised end to end.
        window.history.replaceState(null, '', `/ai/billing?pending=${started.reference}&simulated=1`)
        router.refresh()
      } else {
        window.location.href = started.authorization_url
      }
    } catch (err: any) {
      setError(apiErrorMessage(err, 'Could not start checkout'))
    } finally {
      setBusyPack(null)
    }
  }

  const simulate = async () => {
    if (!pending) return
    setError('')
    try {
      const result = await aiConsole.simulateCheckout(pending)
      setPolled('paid')
      setBalance(result.balance_micro)
    } catch (err: any) {
      setError(apiErrorMessage(err, 'Could not complete the simulated purchase'))
    }
  }

  if (!catalogue) {
    return error ? (
      <ErrorState message={error} onRetry={load} />
    ) : (
      <Skeleton rows={3} />
    )
  }

  const reference = checkout?.reference ?? pending
  const creditMicro = checkout?.credit_micro ?? null

  return (
    <div className="space-y-6">
      {polled === 'paid' && (
        <Card ear="Receipt" title="Credits added" footer="Keep this reference for support.">
          <div className="flex flex-wrap items-center gap-3">
            <Badge tone="ok">paid</Badge>
            <span className="font-mono text-xs" style={{ color: 'var(--text-soft)' }}>
              {reference}
            </span>
          </div>
          {creditMicro !== null && (
            <p className="mt-2 text-sm" style={{ color: 'var(--text-primary)' }}>
              {usd(creditMicro, 2)} credited · new balance{' '}
              {balance !== null ? usd(balance, 2) : 'refreshing…'}
            </p>
          )}
          <div className="mt-4 flex gap-2">
            <Button onClick={() => router.push('/ai/usage')}>View usage</Button>
            <Button onClick={() => router.push('/ai/quickstart')} variant="primary">
              Start calling the API
            </Button>
          </div>
        </Card>
      )}

      {polled === 'pending' && reference && (
        <Card ear="Payment" title="Waiting for confirmation">
          <Loading label="Checking payment status" />
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            Reference <code className="font-mono text-xs">{reference}</code>. This page asks the
            ledger every 3 seconds; it never trusts the return URL alone.
          </p>
          {simulated && (
            <div className="mt-4">
              <p className="text-xs" style={{ color: 'var(--warn)' }}>
                This deployment has no payment provider configured, so checkout runs in simulated
                test mode. No real charge is possible.
              </p>
              <div className="mt-2">
                <Button variant="primary" onClick={simulate}>
                  Complete test payment
                </Button>
              </div>
            </div>
          )}
        </Card>
      )}

      {polled === 'timeout' && (
        <Card ear="Payment" title="Still pending">
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            The provider has not confirmed this payment yet. If you were charged, the credits appear
            as soon as the webhook lands — reload this page to check again.
          </p>
          <div className="mt-3">
            <Button onClick={() => router.refresh()}>Check again</Button>
          </div>
        </Card>
      )}

      {error && <ErrorState message={error} />}

      <Card ear="Mode" title={`${catalogue.provider} · ${catalogue.mode} mode`}>
        <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
          {catalogue.note}
        </p>
      </Card>

      <div className="grid gap-4 sm:grid-cols-3">
        <Stat label="Balance" value={balance !== null ? usd(balance, 2) : '—'} hint="USD credits" />
        <Stat
          label="Display rate"
          value={`${catalogue.currency.kes_per_usd} KES / $1`}
          hint="Approximate — the pack price is fixed, the rate is not a quote"
        />
        <Stat
          label="Price basis"
          value="Per token"
          hint="Usage is billed per million tokens at the published rate"
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {catalogue.packs.map((pack) => (
          <Card key={pack.id} ear={pack.label} title={usd(pack.credit_micro, 0)}>
            <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
              {pack.currency} {pack.kes.toLocaleString()}
            </p>
            <div className="mt-4">
              <Button
                variant="primary"
                onClick={() => startCheckout(pack)}
                disabled={busyPack !== null}
              >
                {busyPack === pack.id ? 'Starting…' : 'Buy credits'}
              </Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  )
}

export default function AiBillingPage() {
  return (
    <Suspense fallback={<Loading label="Loading credit packs" />}>
      <BillingContent />
    </Suspense>
  )
}
