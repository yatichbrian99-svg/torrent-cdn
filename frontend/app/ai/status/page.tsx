'use client'

import { useCallback, useEffect, useState } from 'react'
import { aiConsole, relativeAge, timestamp, type AiStatus } from '@/lib/aiConsole'
import { apiErrorMessage } from '@/lib/api'
import { Badge, Card, EmptyState, ErrorState, Skeleton } from '@/components/ai/ui'

/**
 * Public status view. It reads the console status endpoint, which is
 * unauthenticated and detail-free on purpose: counts and ages, no node ids, no
 * endpoints, nothing an attacker mapping the fleet could use.
 */
export default function AiStatusPage() {
  const [status, setStatus] = useState<AiStatus | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    setError('')
    aiConsole
      .status()
      .then(setStatus)
      .catch((err) => setError(apiErrorMessage(err, 'AI service unavailable')))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (error) return <ErrorState message={error} onRetry={load} />
  if (!status) return <Skeleton rows={4} />

  const healthy = status.database.status === 'ok' && status.redis.status === 'ok'
  const workers = status.workers

  return (
    <div className="space-y-6">
      <Card
        ear="Overall"
        title={
          <Badge tone={healthy ? 'ok' : 'err'}>{healthy ? 'Operational' : 'Degraded'}</Badge>
        }
        footer={`Server time ${timestamp(status.server_time)} · build ${status.commit || 'local'}`}
      >
        <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
          {status.statement}
        </p>
        <ul className="mt-4 grid gap-3 sm:grid-cols-3">
          {(
            [
              { label: 'Postgres', value: status.database },
              { label: 'Redis', value: status.redis },
            ] as { label: string; value: AiStatus['database'] }[]
          ).map(({ label, value }) => {
            return (
              <li key={String(label)} className="border p-3" style={{ borderColor: 'var(--border)' }}>
                <p className="text-[10px] font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                  {label}
                </p>
                <p className="mt-1 text-sm font-medium" style={{ color: value.status === 'ok' ? 'var(--ok)' : 'var(--err)' }}>
                  {value.status}
                  <span className="ml-2 text-xs" style={{ color: 'var(--text-muted)' }}>
                    {value.latency_ms} ms
                  </span>
                </p>
                {value.detail && (
                  <p className="mt-1 font-mono text-[11px]" style={{ color: 'var(--err-soft)' }}>
                    {value.detail}
                  </p>
                )}
              </li>
            )
          })}
          <li className="border p-3" style={{ borderColor: 'var(--border)' }}>
            <p className="text-[10px] font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>
              Feature flag
            </p>
            <p className="mt-1 text-sm font-medium" style={{ color: status.feature_flag.on ? 'var(--ok)' : 'var(--text-muted)' }}>
              {status.feature_flag.on ? 'on' : 'off'}
            </p>
            <p className="mt-1 text-[11px]" style={{ color: 'var(--text-dim)' }}>
              {status.feature_flag.name}
            </p>
          </li>
        </ul>
      </Card>

      <Card
        ear="Fleet"
        title="Workers by region"
        footer={`A worker is counted healthy when its last heartbeat is within ${workers.heartbeat_timeout_seconds}s.`}
      >
        <div className="mb-4 flex flex-wrap gap-6 text-sm">
          <span style={{ color: 'var(--text-muted)' }}>
            Registered <strong style={{ color: 'var(--text-primary)' }}>{workers.registered ?? '—'}</strong>
          </span>
          <span style={{ color: 'var(--text-muted)' }}>
            Healthy <strong style={{ color: 'var(--text-primary)' }}>{workers.healthy ?? '—'}</strong>
          </span>
          <span style={{ color: 'var(--text-muted)' }}>
            Newest heartbeat{' '}
            <strong style={{ color: 'var(--text-primary)' }}>{relativeAge(workers.newest_heartbeat_age_seconds)}</strong>
          </span>
        </div>

        {status.regions.length === 0 ? (
          <EmptyState
            title="No workers have checked in"
            body="No compute node has registered with this deployment. Jobs will queue until one does."
          />
        ) : (
          <div className="w-full max-w-full overflow-x-auto">
            <table className="w-full min-w-[520px] border-collapse text-sm">
              <caption className="sr-only">Worker counts and heartbeat ages per region</caption>
              <thead>
                <tr className="border-b text-left" style={{ borderColor: 'var(--border)' }}>
                  {['Region', 'Registered', 'Healthy', 'Newest heartbeat'].map((heading) => (
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
                {status.regions.map((region) => (
                  <tr key={region.region} className="border-b" style={{ borderColor: 'var(--border)' }}>
                    <td className="py-2 pr-4 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                      {region.region}
                    </td>
                    <td className="py-2 pr-4 text-xs" style={{ color: 'var(--text-soft)' }}>
                      {region.registered}
                    </td>
                    <td className="py-2 pr-4 text-xs" style={{ color: region.healthy > 0 ? 'var(--ok)' : 'var(--text-muted)' }}>
                      {region.healthy}
                    </td>
                    <td className="py-2 pr-4 text-xs" style={{ color: 'var(--text-muted)' }}>
                      {relativeAge(region.newest_heartbeat_age_seconds)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {workers.models_served.length > 0 && (
          <p className="mt-3 text-xs" style={{ color: 'var(--text-muted)' }}>
            Models served: {workers.models_served.join(', ')}
          </p>
        )}
      </Card>
    </div>
  )
}
