'use client'

import { useEffect, useState } from 'react'
import { ArrowUpRight } from 'lucide-react'
import { AI_EARLY_ACCESS_MAILTO } from '@/lib/aiPublic'

/**
 * The one interactive element on the public TCDN-AI page.
 *
 * Two states, and only these two:
 *  - the flag probe says the console is served here → "Open dashboard", because
 *    the visitor can actually do something with it;
 *  - anything else (404, 5xx, timeout, proxy error) → the early-access email
 *    only. Fails closed, like every other use of the probe.
 *
 * The probe is the existing memoised `/api/ai/v1/console/status` request, one
 * per tab, the same one the navbar uses. This page adds no endpoint and sends
 * nothing about the visitor.
 *
 * It is imported dynamically on purpose: `lib/aiConsole` pulls in the shared
 * axios client, and a marketing page should not pay ~65 kB for a link decision.
 * The chunk is fetched after hydration instead, and the button is already usable
 * before it arrives (early access is the default state).
 */
export default function EarlyAccessCta({ align = 'start' }: { align?: 'start' | 'center' }) {
  const [enabled, setEnabled] = useState(false)

  useEffect(() => {
    let cancelled = false
    import('@/lib/aiConsole')
      .then(({ aiFeatureEnabled }) => aiFeatureEnabled())
      .then((on) => {
        if (!cancelled) setEnabled(on)
      })
      .catch(() => {
        if (!cancelled) setEnabled(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div
      className={`flex flex-col gap-3 sm:flex-row sm:items-center ${align === 'center' ? 'sm:justify-center' : ''}`}
    >
      <a
        href={AI_EARLY_ACCESS_MAILTO}
        className="inline-flex items-center justify-center gap-2 px-4 py-2 text-sm font-semibold transition-opacity hover:opacity-90"
        style={{ backgroundColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
      >
        Request early access
      </a>
      {enabled && (
        <a
          href="/ai"
          className="inline-flex items-center justify-center gap-1 border px-4 py-2 text-sm font-semibold transition-colors hover:border-[color:var(--border-strong)]"
          style={{ borderColor: 'var(--border)', color: 'var(--text-primary)' }}
        >
          Open dashboard
          <ArrowUpRight size={14} aria-hidden="true" />
        </a>
      )}
    </div>
  )
}
