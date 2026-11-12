'use client'

/**
 * Small, shared console primitives.
 *
 * Design rules (see FRONTEND_PLAN §4): CSS tokens only, 1px borders, square
 * corners, mono uppercase "ears", no shadows/gradients. Everything renders API
 * strings as React text children — there is no dangerouslySetInnerHTML in the
 * AI section, so nothing the backend returns can become markup.
 */
import type { ReactNode } from 'react'
import { AlertTriangle, Check, Inbox, Loader2 } from 'lucide-react'

export function Ear({ children }: { children: ReactNode }) {
  return (
    <p
      className="text-[10px] font-bold uppercase tracking-widest"
      style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}
    >
      {children}
    </p>
  )
}

export function Card({
  ear,
  title,
  action,
  children,
  footer,
}: {
  ear?: string
  title?: ReactNode
  action?: ReactNode
  children: ReactNode
  footer?: ReactNode
}) {
  return (
    <section
      className="min-w-0 border p-5"
      style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}
    >
      {(ear || title || action) && (
        <header className="mb-4 flex items-start justify-between gap-3">
          <div>
            {ear && <Ear>{ear}</Ear>}
            {title && (
              <h2 className="mt-1 text-base font-bold tracking-[-0.01em] text-[color:var(--text-primary)]">
                {title}
              </h2>
            )}
          </div>
          {action}
        </header>
      )}
      {children}
      {footer && (
        <footer className="mt-4 border-t pt-3 text-xs" style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
          {footer}
        </footer>
      )}
    </section>
  )
}

type Tone = 'neutral' | 'ok' | 'warn' | 'err'

const TONES: Record<Tone, { bg: string; border: string; text: string }> = {
  neutral: { bg: 'transparent', border: 'var(--border-strong)', text: 'var(--text-soft)' },
  ok: { bg: 'var(--ok-chip-bg)', border: 'var(--ok-chip-border)', text: 'var(--ok-chip-text)' },
  warn: { bg: 'var(--warn-chip-bg)', border: 'var(--warn-chip-border)', text: 'var(--warn)' },
  err: { bg: 'var(--err-chip-bg)', border: 'var(--err-chip-border)', text: 'var(--err-chip-text)' },
}

/**
 * Status pill. Text carries the meaning, colour only reinforces it — a
 * colour-blind reader and a monochrome screenshot both keep the label.
 */
export function Badge({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  const t = TONES[tone]
  return (
    <span
      className="inline-flex items-center gap-1 border px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest"
      style={{ backgroundColor: t.bg, borderColor: t.border, color: t.text, fontFamily: 'monospace' }}
    >
      {children}
    </span>
  )
}

export function Stat({
  label,
  value,
  hint,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
}) {
  return (
    <div
      className="border p-4"
      style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}
    >
      <Ear>{label}</Ear>
      <p className="mt-2 text-2xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">
        {value}
      </p>
      {hint && (
        <p className="mt-1 text-xs" style={{ color: 'var(--text-muted)' }}>
          {hint}
        </p>
      )}
    </div>
  )
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-hidden="true">
      {Array.from({ length: rows }).map((_, index) => (
        <div
          key={index}
          className="h-9 animate-pulse border"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-raised)' }}
        />
      ))}
    </div>
  )
}

export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-6 text-sm" role="status" aria-live="polite" style={{ color: 'var(--text-muted)' }}>
      <Loader2 size={16} className="animate-spin" aria-hidden="true" />
      {label}…
    </div>
  )
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string
  body: ReactNode
  action?: ReactNode
}) {
  return (
    <div
      className="flex flex-col items-start gap-2 border border-dashed p-6"
      style={{ borderColor: 'var(--border-strong)' }}
    >
      <Inbox size={18} style={{ color: 'var(--text-dim)' }} aria-hidden="true" />
      <p className="text-sm font-medium text-[color:var(--text-primary)]">{title}</p>
      <p className="max-w-prose text-sm" style={{ color: 'var(--text-muted)' }}>
        {body}
      </p>
      {action}
    </div>
  )
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string
  onRetry?: () => void
}) {
  return (
    <div
      className="flex flex-col items-start gap-3 border p-4"
      role="alert"
      style={{ borderColor: 'var(--err-chip-border)', backgroundColor: 'var(--err-chip-bg)' }}
    >
      <div className="flex items-start gap-2">
        <AlertTriangle size={16} style={{ color: 'var(--err-bright)' }} aria-hidden="true" />
        <p className="text-sm" style={{ color: 'var(--err-soft)' }}>
          {message}
        </p>
      </div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="border px-3 py-1 text-xs font-bold uppercase tracking-widest transition-colors"
          style={{ borderColor: 'var(--err-chip-border)', color: 'var(--err-soft)' }}
        >
          Retry
        </button>
      )}
    </div>
  )
}

/** A checklist row whose state comes from data, never from optimism. */
export function CheckItem({
  done,
  title,
  children,
  action,
}: {
  done: boolean
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <li className="flex items-start gap-3 py-3">
      <span
        aria-hidden="true"
        className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center border"
        style={{
          borderColor: done ? 'var(--ok-chip-border)' : 'var(--border-strong)',
          backgroundColor: done ? 'var(--ok-chip-bg)' : 'transparent',
          color: 'var(--ok)',
        }}
      >
        {done ? <Check size={12} /> : null}
      </span>
      <div className="min-w-0">
        <p className="text-sm font-medium text-[color:var(--text-primary)]">
          {title}
          <span className="sr-only">{done ? ' (done)' : ' (not done yet)'}</span>
        </p>
        {children && (
          <div className="mt-0.5 text-xs" style={{ color: 'var(--text-muted)' }}>
            {children}
          </div>
        )}
        {action && <div className="mt-2">{action}</div>}
      </div>
    </li>
  )
}

export const buttonStyle = {
  borderColor: 'var(--border-strong)',
  color: 'var(--text-primary)',
} as const

export function Button({
  children,
  onClick,
  variant = 'secondary',
  disabled,
  type = 'button',
  ariaLabel,
}: {
  children: ReactNode
  onClick?: () => void
  variant?: 'primary' | 'secondary' | 'danger'
  disabled?: boolean
  type?: 'button' | 'submit'
  ariaLabel?: string
}) {
  const styles =
    variant === 'primary'
      ? { backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }
      : variant === 'danger'
        ? { backgroundColor: 'transparent', borderColor: 'var(--err-chip-border)', color: 'var(--err-chip-text)' }
        : { backgroundColor: 'transparent', borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      className="border px-3 py-1.5 text-xs font-bold uppercase tracking-widest transition-opacity disabled:opacity-40"
      style={styles}
    >
      {children}
    </button>
  )
}
