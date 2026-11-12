'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { aiConsole, bytes, perMtok, type AiModel } from '@/lib/aiConsole'
import { apiErrorMessage } from '@/lib/api'
import { isLoggedIn } from '@/lib/auth'
import { Badge, Card, EmptyState, ErrorState, Skeleton } from '@/components/ai/ui'

interface Catalogue {
  models: AiModel[]
  tiers: { id: string; label: string; available: boolean; reason?: string }[]
}

const VRAM_LABELS: Record<AiModel['vram_tier'], string> = {
  small: 'Small · ≤16 GB VRAM',
  medium: 'Medium · ≤48 GB VRAM',
  large: 'Large · >48 GB VRAM',
}

export default function AiModelsPage() {
  const router = useRouter()
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    setError('')
    setCatalogue(null)
    aiConsole
      .models()
      .then(setCatalogue)
      .catch((err: any) => {
        if (err?.response?.status === 401) {
          router.push('/')
          return
        }
        setError(apiErrorMessage(err, 'Could not load the model catalogue'))
      })
  }, [router])

  useEffect(() => {
    if (!isLoggedIn()) {
      router.push('/')
      return
    }
    load()
  }, [load, router])

  const confidential = catalogue?.tiers.find((tier) => tier.id === 'confidential')

  return (
    <div className="space-y-6">
      <Card
        ear="Catalogue"
        title="Models and prices"
        footer="Prices are per million tokens of the standard tier. Models not allowlisted for this deployment are dimmed and cannot be selected."
      >
        {error ? (
          <ErrorState message={error} onRetry={load} />
        ) : catalogue === null ? (
          <Skeleton rows={4} />
        ) : catalogue.models.length === 0 ? (
          <EmptyState
            title="No models allowlisted yet"
            body="This deployment has the AI flag on but no model is serveable. The catalogue fills in once an operator allowlists a model."
          />
        ) : (
          <div className="w-full max-w-full overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse text-sm">
              <caption className="sr-only">Available models with prices and hardware tiers</caption>
              <thead>
                <tr className="border-b text-left" style={{ borderColor: 'var(--border)' }}>
                  {['Model', 'Hardware', 'Input', 'Output', 'Availability'].map((heading) => (
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
                {catalogue.models.map((model) => (
                  <tr
                    key={model.model_id}
                    className="border-b align-top"
                    style={{ borderColor: 'var(--border)', opacity: model.allowlisted ? 1 : 0.55 }}
                  >
                    <td className="py-3 pr-4">
                      <p className="font-medium text-[color:var(--text-primary)]">{model.name}</p>
                      <p className="font-mono text-[11px]" style={{ color: 'var(--text-muted)' }}>
                        {model.model_id} · {model.format} · {bytes(model.size_bytes)}
                      </p>
                      <p className="mt-0.5 text-[11px]" style={{ color: 'var(--text-dim)' }}>
                        {model.license_note}
                      </p>
                    </td>
                    <td className="py-3 pr-4 text-xs" style={{ color: 'var(--text-soft)' }}>
                      {VRAM_LABELS[model.vram_tier]}
                      <span className="block font-mono text-[11px]" style={{ color: 'var(--text-muted)' }}>
                        {model.required_vram_gb} GB required
                      </span>
                    </td>
                    <td className="py-3 pr-4 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                      {perMtok(model.standard.input_micro_per_mtok)}
                    </td>
                    <td className="py-3 pr-4 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                      {perMtok(model.standard.output_micro_per_mtok)}
                    </td>
                    <td className="py-3 pr-4">
                      <div className="flex flex-wrap gap-1">
                        <Badge tone={model.standard.available ? 'ok' : 'neutral'}>
                          Standard {model.standard.available ? 'available' : 'unavailable'}
                        </Badge>
                        <Badge tone="warn">
                          Confidential — {confidential?.reason || 'coming soon'}
                        </Badge>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card ear="Privacy tiers" title="What you can choose today">
        <dl className="grid gap-4 sm:grid-cols-2">
          <div className="border p-4" style={{ borderColor: 'var(--border)' }}>
            <dt className="flex items-center gap-2 text-sm font-medium text-[color:var(--text-primary)]">
              Standard <Badge tone="ok">selectable</Badge>
            </dt>
            <dd className="mt-1 text-xs" style={{ color: 'var(--text-muted)' }}>
              Runs on the shared compute pool. The coordinator sees a prompt in transit to place and
              bill the job; it does not store it.
            </dd>
          </div>
          <div className="border p-4" style={{ borderColor: 'var(--border)' }}>
            <dt className="flex items-center gap-2 text-sm font-medium text-[color:var(--text-primary)]">
              Confidential <Badge tone="warn">coming soon</Badge>
            </dt>
            <dd className="mt-1 text-xs" style={{ color: 'var(--text-muted)' }}>
              End-to-end encrypted to a verified enclave, at a published uplift. Not enabled in this
              release, so it is shown and not sold — no price here is offered.
            </dd>
          </div>
        </dl>
      </Card>
    </div>
  )
}
