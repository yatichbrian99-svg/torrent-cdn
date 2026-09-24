import type { ReactNode } from 'react'

/**
 * Shared layout for all legal pages (Terms, Privacy, Cookies, DMCA, AUP).
 * Keeps the identical visual structure the site already uses — dark cards on
 * the grid background, mono accents — so legal content stays consistent
 * without duplicating the wrapper markup in five files.
 */
export function LegalSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h2 className="mb-3 text-base font-semibold text-[color:var(--text-primary)]">{title}</h2>
      {children}
    </section>
  )
}

export function LegalList({ items }: { items: ReactNode[] }) {
  return (
    <ul className="mt-2 space-y-2">
      {items.map((item, i) => (
        <li key={i} className="flex items-start gap-2">
          <span className="mt-0.5 text-[color:var(--text-primary)]">&ndash;</span>
          <span>{item}</span>
        </li>
      ))}
    </ul>
  )
}

export default function LegalPage({
  title,
  updated,
  children,
}: {
  title: string
  updated: string
  children: ReactNode
}) {
  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">{title}</h1>
      <p className="mt-2 text-sm" style={{ color: 'var(--text-muted)' }}>Last updated: {updated}</p>
      <div className="mt-10 space-y-8 text-sm leading-relaxed" style={{ color: 'var(--text-soft)' }}>
        {children}
      </div>
    </div>
  )
}
