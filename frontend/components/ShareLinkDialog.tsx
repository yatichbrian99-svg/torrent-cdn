'use client'

import { useState } from 'react'
import { Loader2, Check } from 'lucide-react'

interface ShareLinkDialogProps {
  file: { filename: string }
  link: { url: string; token: string } | null
  busy: boolean
  error: string
  onClose: () => void
  onCreate: (opts: { expires_in_hours: number | null; password: string | null }) => void
}

const EXPIRY_OPTIONS: { label: string; hours: number | null }[] = [
  { label: '1 hour', hours: 1 },
  { label: '24 hours', hours: 24 },
  { label: '7 days', hours: 24 * 7 },
  { label: 'Never expires', hours: null },
]

export default function ShareLinkDialog({ file, link, busy, error, onClose, onCreate }: ShareLinkDialogProps) {
  const [expiresOpt, setExpiresOpt] = useState(1) // index into EXPIRY_OPTIONS, default 24h
  const [password, setPassword] = useState('')
  const [copied, setCopiedFlag] = useState(false)
  const setCopied = () => {
    setCopiedFlag(true)
    setTimeout(() => setCopiedFlag(false), 2000)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
      <div className="w-full max-w-sm border p-6" style={{ backgroundColor: 'var(--bg-card)', borderColor: 'var(--border-strong)' }}>
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-[color:var(--text-primary)]">Share “{file.filename}”</h3>
          <button onClick={onClose} className="text-sm" style={{ color: 'var(--text-muted)' }}>&times;</button>
        </div>

        {!link ? (
          <>
            <label className="mb-1 block text-xs" style={{ color: 'var(--text-muted)' }}>Link expires</label>
            <select
              value={expiresOpt}
              onChange={(e) => setExpiresOpt(Number(e.target.value))}
              className="mb-3 w-full border px-3 py-2 text-sm"
              style={{ borderColor: 'var(--border-strong)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)' }}
            >
              {EXPIRY_OPTIONS.map((o, i) => <option key={o.label} value={i}>{o.label}</option>)}
            </select>

            <label className="mb-1 block text-xs" style={{ color: 'var(--text-muted)' }}>Password (optional)</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Leave empty for none"
              className="mb-3 w-full border px-3 py-2 text-sm"
              style={{ borderColor: 'var(--border-strong)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)' }}
            />

            {error && <p className="mb-3 text-xs" style={{ color: 'var(--err)' }}>{error}</p>}

            <button
              onClick={() => onCreate({ expires_in_hours: EXPIRY_OPTIONS[expiresOpt].hours, password: password || null })}
              disabled={busy}
              className="w-full border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
              style={{ borderColor: 'var(--inverse-bg)', color: 'var(--text-primary)' }}
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : 'Create link'}
            </button>
          </>
        ) : (
          <>
            <p className="mb-2 text-xs" style={{ color: 'var(--text-muted)' }}>
              Share this link. It is shown <span className="font-medium" style={{ color: 'var(--text-primary)' }}>only once</span> — copy it now.
            </p>
            <div className="mb-3 flex items-center gap-2">
              <code className="flex-1 truncate border px-3 py-2 font-mono text-xs text-[color:var(--text-primary)]" style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border-strong)' }}>
                {link.url}
              </code>
              <button
                onClick={async () => { await navigator.clipboard.writeText(link.url); setCopied() }}
                className="border px-3 py-2 text-xs transition-opacity hover:opacity-80"
                style={{ borderColor: 'var(--border-strong)', color: 'var(--text-muted)' }}
              >
                {(copied) ? <><Check size={12} className="inline" style={{ color: 'var(--ok)' }} /> Copied</> : 'Copy'}
              </button>
            </div>
            <p className="mb-4 text-xs" style={{ color: 'var(--warn)' }}>
              Revoking the link stops all FUTURE access through it. It cannot recall copies someone already downloaded.
            </p>
            <button onClick={onClose} className="w-full border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80" style={{ borderColor: 'var(--inverse-bg)', color: 'var(--text-primary)' }}>
              Done
            </button>
          </>
        )}
      </div>
    </div>
  )
}
