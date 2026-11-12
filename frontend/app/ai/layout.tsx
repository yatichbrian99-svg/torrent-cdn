import type { Metadata } from 'next'
import AiGate from '@/components/ai/AiGate'
import AiShell from '@/components/ai/AiShell'

/**
 * Route segment for the TCDN-AI console.
 *
 * Nothing outside /ai imports from here, and nothing here is imported by the
 * landing page, dashboard, billing or upload routes — which is what keeps the
 * console out of their bundles (measured in the bundle report, not asserted).
 *
 * `noindex`: a dark-release console should not be crawled, and the flag-off
 * panel is not content anyone should find in search.
 */
export const metadata: Metadata = {
  title: 'TCDN-AI console',
  robots: { index: false, follow: false },
}

export default function AiLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="pt-12">
      <AiGate>
        <AiShell>{children}</AiShell>
      </AiGate>
    </div>
  )
}
