'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { aiConsole, kes, relativeAge, usd, type AiSummary } from '@/lib/aiConsole'
import { apiErrorMessage } from '@/lib/api'
import { isLoggedIn } from '@/lib/auth'
import { Badge, Button, Card, CheckItem, ErrorState, Skeleton, Stat } from '@/components/ai/ui'

function statusTone(status: AiSummary | null): 'ok' | 'warn' | 'err' {
  if (!status) return 'warn'
  const { database, redis, workers } = status.status
  if (database !== 'ok' || redis !== 'ok') return 'err'
  if (!workers.healthy) return 'warn'
  return 'ok'
}

export default function AiOverviewPage() {
  const router = useRouter()
  const [summary, setSummary] = useState<AiSummary | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(() => {
    setLoading(true)
    aiConsole
      .summary()
      .then((data) => {
        setSummary(data)
        setError('')
      })
      .catch((err: any) => {
        if (err?.response?.status === 401) {
          router.push('/')
          return
        }
        setError(apiErrorMessage(err, 'Could not load your AI account'))
      })
      .finally(() => setLoading(false))
  }, [router])

  useEffect(() => {
    if (!isLoggedIn()) {
      router.push('/')
      return
    }
    load()
  }, [load, router])

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton rows={2} />
        <Skeleton rows={3} />
      </div>
    )
  }

  if (!summary) {
    return <ErrorState message={error || 'Could not load your AI account'} onRetry={load} />
  }

  const { balance, usage, checklist, status } = summary
  const tone = statusTone(summary)
  const workers = status.workers
  const nextStep = !checklist.has_key
    ? { href: '/ai/keys', label: 'Create your first API key' }
    : !checklist.has_credit
      ? { href: '/ai/billing', label: 'Add credits to run a job' }
      : !checklist.has_job
        ? { href: '/ai/quickstart', label: 'Send your first request' }
        : null

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Credit balance"
          value={usd(balance.micro, 2)}
          hint={
            <>
              {kes(balance.micro, balance.kes_per_usd)} · {usd(balance.available_micro, 2)} available
            </>
          }
        />
        <Stat label="Jobs (latest page)" value={usage.jobs} hint={usage.jobs ? `Spent ${usd(usage.spend_micro, usage.spend_micro >= 10_000 ? 2 : 4)}` : 'No jobs yet'} />
        <Stat
          label="Active API keys"
          value={summary.keys.active}
          hint={summary.keys.total > summary.keys.active ? `${summary.keys.total - summary.keys.active} revoked` : 'Key auth for the API'}
        />
        <Stat
          label="Workers online"
          value={workers.healthy ?? '—'}
          hint={`${workers.registered ?? 0} registered · newest heartbeat ${relativeAge(workers.newest_heartbeat_age_seconds)}`}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <Card
            ear="Getting started"
            title="Four steps to a first billed job"
            footer="Each step is derived from your account, not from a stored checklist."
          >
            <ul className="divide-y" style={{ borderColor: 'var(--border)' }}>
              <CheckItem done={checklist.account_ready} title="Account ready">
                Signed in with a verified TorrentCDN account — the same login as the rest of the
                product.
              </CheckItem>
              <CheckItem done={checklist.has_key} title="Create an API key">
                The key is shown once, then only its prefix is kept.
              </CheckItem>
              <CheckItem done={checklist.has_credit} title="Add credits">
                Balance: {usd(balance.micro, 2)}. Purchases are test-mode on this deployment.
              </CheckItem>
              <CheckItem done={checklist.has_job} title="Send a request">
                {checklist.has_job
                  ? 'Your account has at least one job — see Usage for the tokens and cost.'
                  : 'Copy a snippet from Quickstart and call the gateway once.'}
              </CheckItem>
            </ul>
            {nextStep && (
              <div className="mt-4">
                <Link href={nextStep.href}>
                  <Button variant="primary">{nextStep.label}</Button>
                </Link>
              </div>
            )}
          </Card>
        </div>

        <div className="space-y-6">
          <Card ear="System status" title={<Badge tone={tone}>{tone === 'ok' ? 'Operational' : tone === 'warn' ? 'Degraded' : 'Unavailable'}</Badge>}>
            <dl className="space-y-2 text-sm">
              <div className="flex items-center justify-between gap-3">
                <dt style={{ color: 'var(--text-muted)' }}>Postgres</dt>
                <dd className="font-mono text-xs" style={{ color: status.database === 'ok' ? 'var(--ok)' : 'var(--err)' }}>
                  {status.database}
                </dd>
              </div>
              <div className="flex items-center justify-between gap-3">
                <dt style={{ color: 'var(--text-muted)' }}>Redis</dt>
                <dd className="font-mono text-xs" style={{ color: status.redis === 'ok' ? 'var(--ok)' : 'var(--err)' }}>
                  {status.redis}
                </dd>
              </div>
              <div className="flex items-center justify-between gap-3">
                <dt style={{ color: 'var(--text-muted)' }}>Build</dt>
                <dd className="font-mono text-xs" style={{ color: 'var(--text-soft)' }}>
                  {status.commit ? status.commit.slice(0, 12) : 'local'}
                </dd>
              </div>
              <div className="flex items-center justify-between gap-3">
                <dt style={{ color: 'var(--text-muted)' }}>Environment</dt>
                <dd className="font-mono text-xs" style={{ color: 'var(--text-soft)' }}>
                  {status.environment}
                </dd>
              </div>
            </dl>
            <div className="mt-4">
              <Link href="/ai/status" className="text-xs underline" style={{ color: 'var(--text-soft)' }}>
                Full status and regions →
              </Link>
            </div>
          </Card>

          <Card ear="What this service stores">
            <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
              Token counts, prices and job status. <strong style={{ color: 'var(--text-primary)' }}>Not</strong> your
              prompts or model outputs — the gateway does not persist them, so there is nothing to show here
              and nothing to leak.
            </p>
          </Card>
        </div>
      </div>
    </div>
  )
}
