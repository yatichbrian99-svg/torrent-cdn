'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { aiConsole, timestamp, usd, type AiJob, type AiModel } from '@/lib/aiConsole'
import { apiErrorMessage } from '@/lib/api'
import { isLoggedIn } from '@/lib/auth'
import { Badge, Button, Card, EmptyState, ErrorState, Skeleton } from '@/components/ai/ui'

const STATUS_TONE = (status: string): 'ok' | 'warn' | 'err' | 'neutral' => {
  if (['settled', 'completed', 'succeeded'].includes(status)) return 'ok'
  if (['failed', 'expired', 'disputed'].includes(status)) return 'err'
  if (['held', 'placed', 'running', 'pending'].includes(status)) return 'warn'
  return 'neutral'
}

export default function AiUsagePage() {
  const router = useRouter()
  const [jobs, setJobs] = useState<AiJob[] | null>(null)
  const [models, setModels] = useState<AiModel[]>([])
  const [total, setTotal] = useState(0)
  const [note, setNote] = useState('')
  const [model, setModel] = useState('')
  const [status, setStatus] = useState('')
  const [limit, setLimit] = useState(50)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    setJobs(null)
    setError('')
    aiConsole
      .usage({ limit, model: model || undefined, status: status || undefined })
      .then((data) => {
        setJobs(data.jobs)
        setTotal(data.total_micro)
        setNote(data.note)
      })
      .catch((err: any) => {
        if (err?.response?.status === 401) {
          router.push('/')
          return
        }
        setError(apiErrorMessage(err, 'Could not load usage'))
      })
  }, [limit, model, status, router])

  useEffect(() => {
    if (!isLoggedIn()) {
      router.push('/')
      return
    }
    aiConsole
      .models()
      .then((data) => setModels(data.models))
      .catch(() => setModels([]))
  }, [router])

  useEffect(() => {
    if (isLoggedIn()) load()
  }, [load])

  return (
    <div className="space-y-6">
      <Card
        ear="Usage"
        title="Jobs"
        footer={
          note ||
          'Prompts and outputs are never stored, so they cannot be shown — not even to you.'
        }
      >
        <div className="mb-4 flex flex-wrap items-end gap-3">
          <label>
            <span className="text-xs font-medium" style={{ color: 'var(--text-muted)' }}>
              Model
            </span>
            <select
              value={model}
              onChange={(event) => setModel(event.target.value)}
              className="mt-1 block border px-2 py-1.5 text-xs"
              style={{ borderColor: 'var(--border-strong)', backgroundColor: 'var(--bg-primary)', color: 'var(--text-primary)' }}
            >
              <option value="">All models</option>
              {models.map((entry) => (
                <option key={entry.model_id} value={entry.model_id}>
                  {entry.model_id}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="text-xs font-medium" style={{ color: 'var(--text-muted)' }}>
              Status
            </span>
            <select
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              className="mt-1 block border px-2 py-1.5 text-xs"
              style={{ borderColor: 'var(--border-strong)', backgroundColor: 'var(--bg-primary)', color: 'var(--text-primary)' }}
            >
              <option value="">Any status</option>
              {['settled', 'held', 'placed', 'failed', 'expired'].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="text-xs font-medium" style={{ color: 'var(--text-muted)' }}>
              Limit
            </span>
            <select
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
              className="mt-1 block border px-2 py-1.5 text-xs"
              style={{ borderColor: 'var(--border-strong)', backgroundColor: 'var(--bg-primary)', color: 'var(--text-primary)' }}
            >
              {[25, 50, 100, 200].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <Button onClick={load}>Refresh</Button>
          <span className="ml-auto text-xs" style={{ color: 'var(--text-muted)' }}>
            {jobs ? `${jobs.length} shown · ${usd(total)} total` : null}
          </span>
        </div>

        {error ? (
          <ErrorState message={error} onRetry={load} />
        ) : jobs === null ? (
          <Skeleton rows={5} />
        ) : jobs.length === 0 ? (
          <EmptyState
            title={model || status ? 'No jobs match those filters' : 'No jobs yet'}
            body={
              model || status
                ? 'Clear the filters to see every job on your account.'
                : 'Once you call the gateway with your key, each job appears here with tokens, cost and settlement state.'
            }
            action={
              <Link href="/ai/quickstart">
                <Button variant="primary">Open quickstart</Button>
              </Link>
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse text-sm">
              <caption className="sr-only">Jobs on your account</caption>
              <thead>
                <tr className="border-b text-left" style={{ borderColor: 'var(--border)' }}>
                  {['Job', 'Model', 'Status', 'Tokens', 'Cost', 'Created'].map((heading) => (
                    <th
                      key={heading}
                      scope="col"
                      className="py-2 pr-4 text-[10px] font-bold uppercase tracking-widest"
                      style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}
                    >
                      {heading}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {jobs.map((job) => (
                  <tr key={job.job_id} className="border-b" style={{ borderColor: 'var(--border)' }}>
                    <td className="py-3 pr-4">
                      <Link
                        href={`/ai/usage/${encodeURIComponent(job.job_id)}`}
                        className="font-mono text-xs underline-offset-2 hover:underline"
                        style={{ color: 'var(--text-primary)' }}
                      >
                        {job.job_id}
                      </Link>
                    </td>
                    <td className="py-3 pr-4 font-mono text-xs" style={{ color: 'var(--text-soft)' }}>
                      {job.model}
                    </td>
                    <td className="py-3 pr-4">
                      <Badge tone={STATUS_TONE(job.status)}>{job.status}</Badge>
                    </td>
                    <td className="py-3 pr-4 font-mono text-xs" style={{ color: 'var(--text-soft)' }}>
                      {job.input_tokens} in / {job.output_tokens} out
                    </td>
                    <td className="py-3 pr-4 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                      {usd(job.total_micro)}
                    </td>
                    <td className="py-3 pr-4 text-xs" style={{ color: 'var(--text-muted)' }}>
                      {timestamp(job.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
