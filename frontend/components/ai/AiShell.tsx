'use client'

/**
 * Section chrome: title, tab navigation, and the standing honesty notes.
 *
 * The notes are not decoration — they are the claims the console must not
 * overstate: prompts are not stored, the confidential tier is not on sale, and
 * credit purchases are test-mode only on this deployment.
 */
import Link from 'next/link'
import { usePathname } from 'next/navigation'

const TABS = [
  { href: '/ai', label: 'Overview' },
  { href: '/ai/keys', label: 'API keys' },
  { href: '/ai/usage', label: 'Usage' },
  { href: '/ai/models', label: 'Models' },
  { href: '/ai/quickstart', label: 'Quickstart' },
  { href: '/ai/billing', label: 'Credits' },
  { href: '/ai/status', label: 'Status' },
] as const

export function isActiveTab(pathname: string, href: string): boolean {
  if (href === '/ai') return pathname === '/ai'
  return pathname === href || pathname.startsWith(`${href}/`)
}

export default function AiShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || '/ai'

  return (
    <div className="ai-console mx-auto max-w-6xl px-4 py-8 sm:px-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p
            className="text-[10px] font-bold uppercase tracking-widest"
            style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}
          >
            TCDN-AI · developer console
          </p>
          <h1 className="mt-1 text-xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">
            Inference credits, keys and usage
          </h1>
        </div>
        <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
          Gateway mode · prompts are never stored
        </p>
      </header>

      <nav aria-label="TCDN-AI sections" className="mt-6 border-b" style={{ borderColor: 'var(--border)' }}>
        <ul className="flex flex-wrap gap-x-5 gap-y-2">
          {TABS.map((tab) => {
            const active = isActiveTab(pathname, tab.href)
            return (
              <li key={tab.href}>
                <Link
                  href={tab.href}
                  aria-current={active ? 'page' : undefined}
                  className="inline-block border-b-2 pb-2 text-sm transition-colors"
                  style={{
                    borderColor: active ? 'var(--text-primary)' : 'transparent',
                    color: active ? 'var(--text-primary)' : 'var(--text-muted)',
                    fontWeight: active ? 600 : 400,
                  }}
                >
                  {tab.label}
                </Link>
              </li>
            )
          })}
        </ul>
      </nav>

      <main className="mt-8">{children}</main>

      <footer className="mt-10 border-t pt-4 text-xs" style={{ borderColor: 'var(--border)', color: 'var(--text-dim)' }}>
        <p>
          {`Credits are USD-denominated; the KES figure is an approximate display rate, not an FX quote.`}
        </p>
        <p className="mt-1">
          {`Confidential tier shown as "coming soon" and is not selectable. No prompt or output is stored by this service.`}
        </p>
      </footer>
    </div>
  )
}
