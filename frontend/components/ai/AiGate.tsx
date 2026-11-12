'use client'

/**
 * Feature gate for the whole /ai section.
 *
 * The backend mounts the AI routes only with the flag on, so the probe in
 * `lib/aiConsole.ts` is a plain 404 on a flag-off deployment. When that
 * happens this renders a short, honest panel instead of a login wall or a
 * broken dashboard — a stale bookmark should never look like an outage.
 *
 * It also keeps the flag-off promise: with the flag off the only request the
 * AI section makes is the probe itself, and the section's chunks are never
 * fetched (the pages below are separate route segments).
 */
import { useEffect, useState } from 'react'
import Link from 'next/link'
import { Loader2, Lock } from 'lucide-react'
import { aiFeatureEnabled } from '@/lib/aiConsole'

type GateState = 'checking' | 'enabled' | 'disabled'

export default function AiGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<GateState>('checking')

  useEffect(() => {
    let cancelled = false
    aiFeatureEnabled()
      .then((enabled) => {
        if (!cancelled) setState(enabled ? 'enabled' : 'disabled')
      })
      .catch(() => {
        if (!cancelled) setState('disabled')
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (state === 'checking') {
    return (
      <div
        className="ai-console flex min-h-[40vh] items-center justify-center"
        role="status"
        aria-live="polite"
      >
        <Loader2 size={18} className="animate-spin" style={{ color: 'var(--text-muted)' }} />
        <span className="sr-only">Checking whether TCDN-AI is available</span>
      </div>
    )
  }

  if (state === 'disabled') {
    return (
      <div className="ai-console mx-auto max-w-xl py-16">
        <div
          className="border p-6"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}
        >
          <Lock size={18} style={{ color: 'var(--text-dim)' }} aria-hidden="true" />
          <h1 className="mt-3 text-lg font-bold text-[color:var(--text-primary)]">
            TCDN-AI is not available on this deployment
          </h1>
          <p className="mt-2 text-sm" style={{ color: 'var(--text-muted)' }}>
            This deployment runs with the AI feature switched off, so the console and its API are
            not served here. Nothing is broken and nothing was changed in your account.
          </p>
          <p className="mt-3 text-xs" style={{ color: 'var(--text-dim)' }}>
            Status: feature flag <code>ENABLE_TCDN_AI</code> is off on this server.
          </p>
          <Link
            href="/dashboard"
            className="mt-6 inline-block border px-3 py-1.5 text-xs font-bold uppercase tracking-widest text-[color:var(--text-primary)]"
            style={{ borderColor: 'var(--border-strong)' }}
          >
            Back to dashboard
          </Link>
        </div>
      </div>
    )
  }

  return <>{children}</>
}
