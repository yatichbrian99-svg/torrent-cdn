'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Check, Copy } from 'lucide-react'
import { aiConsole, timestamp, type AiKey } from '@/lib/aiConsole'
import { apiErrorMessage } from '@/lib/api'
import { isLoggedIn } from '@/lib/auth'
import { Badge, Button, Card, EmptyState, ErrorState, Skeleton } from '@/components/ai/ui'

const SCOPES = [
  { id: 'inference', label: 'inference', help: 'Call the gateway (/chat/completions, /models).' },
  { id: 'worker', label: 'worker', help: 'Register a node and submit receipts.' },
] as const

function CopyButton({ value, label = 'Copy' }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard permission can be denied; the key is selectable text, so the
      // user is not stuck — we just do not claim it was copied.
      setCopied(false)
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      className="inline-flex items-center gap-1 border px-2 py-1 text-[10px] font-bold uppercase tracking-widest"
      style={{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }}
      aria-label={copied ? `${label} — copied` : label}
    >
      {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
      {copied ? 'Copied' : label}
    </button>
  )
}

export default function AiKeysPage() {
  const router = useRouter()
  const [keys, setKeys] = useState<AiKey[] | null>(null)
  const [error, setError] = useState('')
  const [loadError, setLoadError] = useState('')
  const [busy, setBusy] = useState(false)
  const [scopes, setScopes] = useState<string[]>(['inference'])
  const [freshKey, setFreshKey] = useState<string | null>(null)
  const [confirmRevoke, setConfirmRevoke] = useState<AiKey | null>(null)
  const confirmRef = useRef<HTMLDivElement>(null)

  const load = useCallback(() => {
    aiConsole
      .keys()
      .then((data) => {
        setKeys(data.keys)
        setLoadError('')
      })
      .catch((err: any) => {
        if (err?.response?.status === 401) {
          router.push('/')
          return
        }
        setLoadError(apiErrorMessage(err, 'Could not load your API keys'))
      })
  }, [router])

  useEffect(() => {
    if (!isLoggedIn()) {
      router.push('/')
      return
    }
    load()
  }, [load, router])

  // Escape closes the revoke confirmation, like every other dialog in the app.
  useEffect(() => {
    if (!confirmRevoke) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setConfirmRevoke(null)
    }
    document.addEventListener('keydown', onKey)
    confirmRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
    return () => document.removeEventListener('keydown', onKey)
  }, [confirmRevoke])

  const create = async () => {
    setBusy(true)
    setError('')
    try {
      const created = await aiConsole.createKey(scopes)
      setFreshKey(created.api_key)
      load()
    } catch (err: any) {
      setError(apiErrorMessage(err, 'Could not create a key'))
    } finally {
      setBusy(false)
    }
  }

  const revoke = async () => {
    if (!confirmRevoke) return
    setBusy(true)
    setError('')
    try {
      await aiConsole.revokeKey(confirmRevoke.id)
      setConfirmRevoke(null)
      load()
    } catch (err: any) {
      setError(apiErrorMessage(err, 'Could not revoke that key'))
    } finally {
      setBusy(false)
    }
  }

  const toggleScope = (scope: string) =>
    setScopes((current) =>
      current.includes(scope) ? current.filter((item) => item !== scope) : [...current, scope],
    )

  return (
    <div className="space-y-6">
      {freshKey && (
        <div
          className="border p-5"
          style={{ borderColor: 'var(--ok-chip-border)', backgroundColor: 'var(--ok-chip-bg)' }}
          role="status"
          aria-live="polite"
        >
          <p className="text-[10px] font-bold uppercase tracking-widest" style={{ color: 'var(--ok-chip-text)', fontFamily: 'monospace' }}>
            New API key — shown once
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <code
              className="break-all border px-3 py-2 font-mono text-xs"
              style={{ borderColor: 'var(--ok-chip-border)', color: 'var(--text-primary)', backgroundColor: 'var(--bg-primary)' }}
            >
              {freshKey}
            </code>
            <CopyButton value={freshKey} label="Copy key" />
          </div>
          <p className="mt-2 text-xs" style={{ color: 'var(--ok-chip-text)' }}>
            This is the only time the key is shown. Store it in your secret manager now — we keep only its
            prefix, and it is never written to this browser&apos;s storage.
          </p>
          <div className="mt-3">
            <Button onClick={() => setFreshKey(null)}>I have saved it</Button>
          </div>
        </div>
      )}

      {error && <ErrorState message={error} />}

      <Card
        ear="Create"
        title="New API key"
        footer="Keys are bound to your account. Revoking one takes effect immediately for new requests."
      >
        <fieldset>
          <legend className="text-xs font-medium" style={{ color: 'var(--text-muted)' }}>
            Scopes
          </legend>
          <div className="mt-2 space-y-2">
            {SCOPES.map((scope) => (
              <label key={scope.id} className="flex items-start gap-2 text-sm" style={{ color: 'var(--text-primary)' }}>
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={scopes.includes(scope.id)}
                  onChange={() => toggleScope(scope.id)}
                  aria-describedby={`scope-${scope.id}-help`}
                />
                <span>
                  <span className="font-mono text-xs">{scope.label}</span>
                  <span id={`scope-${scope.id}-help`} className="block text-xs" style={{ color: 'var(--text-muted)' }}>
                    {scope.help}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        <div className="mt-4">
          <Button variant="primary" onClick={create} disabled={busy || scopes.length === 0}>
            {busy ? 'Creating…' : 'Create key'}
          </Button>
          {scopes.length === 0 && (
            <span className="ml-3 text-xs" style={{ color: 'var(--text-muted)' }}>
              Pick at least one scope.
            </span>
          )}
        </div>
      </Card>

      <Card ear="Keys" title="Your keys">
        {loadError ? (
          <ErrorState message={loadError} onRetry={load} />
        ) : keys === null ? (
          <Skeleton rows={3} />
        ) : keys.length === 0 ? (
          <EmptyState
            title="No API keys yet"
            body="Create one above, then paste it into a Quickstart snippet. Keys are shown once."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] border-collapse text-sm">
              <caption className="sr-only">API keys on your account</caption>
              <thead>
                <tr className="border-b text-left" style={{ borderColor: 'var(--border)' }}>
                  {['Prefix', 'Scopes', 'Status', 'Created', 'Last used', ''].map((heading) => (
                    <th
                      key={heading || 'actions'}
                      scope="col"
                      className="py-2 pr-4 text-[10px] font-bold uppercase tracking-widest"
                      style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}
                    >
                      {heading || <span className="sr-only">Actions</span>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {keys.map((key) => (
                  <tr key={key.id} className="border-b align-top" style={{ borderColor: 'var(--border)' }}>
                    <td className="py-3 pr-4 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>
                      {key.prefix}…
                    </td>
                    <td className="py-3 pr-4 font-mono text-xs" style={{ color: 'var(--text-soft)' }}>
                      {key.scopes.join(', ')}
                    </td>
                    <td className="py-3 pr-4">
                      <Badge tone={key.status === 'active' ? 'ok' : 'neutral'}>{key.status}</Badge>
                    </td>
                    <td className="py-3 pr-4 text-xs" style={{ color: 'var(--text-muted)' }}>
                      {timestamp(key.created_at)}
                    </td>
                    <td className="py-3 pr-4 text-xs" style={{ color: 'var(--text-muted)' }}>
                      {key.last_used_at ? timestamp(key.last_used_at) : 'Never'}
                    </td>
                    <td className="py-3 text-right">
                      {key.status === 'active' && (
                        <Button variant="danger" onClick={() => setConfirmRevoke(key)}>
                          Revoke
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {confirmRevoke && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div
            ref={confirmRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="revoke-title"
            className="w-full max-w-sm border p-5"
            style={{ borderColor: 'var(--border-strong)', backgroundColor: 'var(--bg-card)' }}
          >
            <h2 id="revoke-title" className="text-base font-bold text-[color:var(--text-primary)]">
              Revoke this key?
            </h2>
            <p className="mt-2 text-sm" style={{ color: 'var(--text-muted)' }}>
              <code className="font-mono text-xs">{confirmRevoke.prefix}…</code> will stop working
              immediately. This cannot be undone.
            </p>
            <div className="mt-5 flex gap-2">
              <Button variant="danger" onClick={revoke} disabled={busy}>
                {busy ? 'Revoking…' : 'Revoke key'}
              </Button>
              <Button onClick={() => setConfirmRevoke(null)}>Cancel</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
