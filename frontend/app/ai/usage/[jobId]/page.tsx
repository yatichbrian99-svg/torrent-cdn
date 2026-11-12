'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { useParams, useRouter } from 'next/navigation'
import { aiConsole, timestamp, usd, type AiJobDetail } from '@/lib/aiConsole'
import { apiErrorMessage } from '@/lib/api'
import { isLoggedIn } from '@/lib/auth'
import { Badge, Card, ErrorState, Skeleton } from '@/components/ai/ui'

const VERIFICATION_TONE = (state: string): 'ok' | 'warn' | 'err' | 'neutral' => {
  if (state === 'verified') return 'ok'
  if (state === 'mismatch') return 'err'
  if (state === 'pending') return 'warn'
  return 'neutral'
}

export default function AiJobDetailPage() {
  const router = useRouter()
  const params = useParams<{ jobId: string }>()
  const jobId = decodeURIComponent(String(params?.jobId || ''))
  const [detail, setDetail] = useState<AiJobDetail | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    if (!jobId) return
    setDetail(null)
    setError('')
    aiConsole
      .job(jobId)
      .then(setDetail)
      .catch((err: any) => {
        if (err?.response?.status === 401) {
          router.push('/')
          return
        }
        setError(
          err?.response?.status === 404
            ? 'That job does not exist on your account.'
            : apiErrorMessage(err, 'Could not load the job'),
        )
      })
  }, [jobId, router])

  useEffect(() => {
    if (!isLoggedIn()) {
      router.push('/')
      return
    }
    load()
  }, [load, router])

  if (error) return <ErrorState message={error} onRetry={load} />
  if (!detail) return <Skeleton rows={4} />

  const { job, verification, settlement } = detail

  const rows: { label: string; value: React.ReactNode }[] = [
    { label: 'Status', value: <Badge tone={job.status === 'settled' ? 'ok' : job.status === 'failed' ? 'err' : 'warn'}>{job.status}</Badge> },
    { label: 'Model', value: <code className="font-mono text-xs">{job.model}</code> },
    { label: 'Tier', value: job.tier },
    { label: 'Confidentiality', value: job.confidentiality },
    { label: 'Region', value: job.region },
    { label: 'GPU', value: job.gpu_model || '—' },
    { label: 'Input tokens', value: job.input_tokens },
    { label: 'Output tokens', value: job.output_tokens },
    { label: 'Created', value: timestamp(job.created_at) },
    { label: 'Settled', value: job.settled_at ? timestamp(job.settled_at) : 'Not settled' },
  ]

  return (
    <div className="space-y-6">
      <p className="text-xs">
        <Link href="/ai/usage" style={{ color: 'var(--text-muted)' }}>
          ← Back to usage
        </Link>
      </p>

      <Card
        ear="Job"
        title={<code className="font-mono text-sm">{job.job_id}</code>}
        footer="No prompt or output is stored for this job, so none can be shown here."
      >
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
          {rows.map((row) => (
            <div key={row.label} className="flex items-baseline justify-between gap-3 border-b pb-2" style={{ borderColor: 'var(--border)' }}>
              <dt className="text-xs" style={{ color: 'var(--text-muted)' }}>
                {row.label}
              </dt>
              <dd className="text-right text-sm text-[color:var(--text-primary)]">{row.value}</dd>
            </div>
          ))}
        </dl>
      </Card>

      <div className="grid gap-6 sm:grid-cols-2">
        <Card ear="Cost" title={usd(job.total_micro)}>
          <dl className="space-y-2 text-sm">
            {[
              ['Compute', settlement.compute_micro],
              ['Seeder', settlement.seeder_micro],
              ['Platform', settlement.platform_micro],
            ].map(([label, micro]) => (
              <div key={String(label)} className="flex justify-between gap-3">
                <dt style={{ color: 'var(--text-muted)' }}>{label}</dt>
                <dd className="font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                  {usd(Number(micro))}
                </dd>
              </div>
            ))}
            <div className="flex justify-between gap-3 border-t pt-2" style={{ borderColor: 'var(--border)' }}>
              <dt style={{ color: 'var(--text-muted)' }}>Held at placement</dt>
              <dd className="font-mono text-xs" style={{ color: 'var(--text-soft)' }}>
                {usd(job.hold_micro)}
              </dd>
            </div>
          </dl>
        </Card>

        <Card ear="Verification" title={<Badge tone={VERIFICATION_TONE(verification.state)}>{verification.label}</Badge>}>
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            {verification.state === 'verified'
              ? 'A second node re-ran this job and the outputs matched.'
              : verification.state === 'mismatch'
                ? 'A second node produced a different result. The charge stands while an operator reviews it.'
                : verification.state === 'pending'
                  ? 'This job was sampled for redundant execution; the comparison has not completed.'
                  : 'This job was not sampled. Sampling is a spot check, not a guarantee for every job.'}
          </p>
          {job.dispute_reason && (
            <p className="mt-3 text-xs" style={{ color: 'var(--warn)' }}>
              Under review: {job.dispute_reason}
            </p>
          )}
        </Card>
      </div>
    </div>
  )
}
