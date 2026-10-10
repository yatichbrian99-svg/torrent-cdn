'use client'

import { useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { Loader2, Download, Film, AlertTriangle } from 'lucide-react'
import VideoPlayer from '@/components/VideoPlayer'
import api from '@/lib/api'

interface SharePayload {
  id: string
  filename: string
  minio_url: string
  magnet_link: string
  file_size_mb: number
}

type ShareState =
  | { kind: 'loading' }
  | { kind: 'error'; status: number; message: string }
  | { kind: 'ready'; data: SharePayload }

/**
 * Public share-link page (DFS M1).
 *
 * Recipients need no account: the token resolves server-side against
 * POST /files/share/{token} and returns the same public payload the embed
 * player already consumes. 410 = revoked/absent/expired, 401 = password
 * required.
 */
export default function SharePage() {
  const { token } = useParams<{ token: string }>()
  const router = useRouter()
  const [state, setState] = useState<ShareState>({ kind: 'loading' })
  const [password, setPassword] = useState('')
  const [passwordError, setPasswordError] = useState('')

  const resolve = (pw?: string) => {
    setState({ kind: 'loading' })
    api
      .post(`/files/share/${token}`, pw ? { password: pw } : {})
      .then((r) => setState({ kind: 'ready', data: r.data }))
      .catch((e) => {
        const status = e?.response?.status ?? 0
        const detail = e?.response?.data?.detail
        if (status === 401) {
          // Password gate — swap the spinner for the password form.
          setState({ kind: 'error', status: 401, message: 'This link requires a password.' })
        } else {
          // Default to a non-revealing message for 404/410/anything else.
          const message =
            detail && (status === 410 || status === 404)
              ? detail
              : 'This link is not available.'
          setState({ kind: 'error', status, message })
        }
      })
  }

  useEffect(() => {
    if (typeof token === 'string' && token) resolve()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token])

  const submitPassword = (e: React.FormEvent) => {
    e.preventDefault()
    if (!password.trim()) return
    resolve(password)
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-16">
      {state.kind === 'loading' && (
        <div className="flex h-64 items-center justify-center border" style={{ borderColor: 'var(--border)' }}>
          <Loader2 size={24} className="animate-spin" style={{ color: 'var(--text-muted)' }} />
        </div>
      )}

      {state.kind === 'error' && state.status === 401 && (
        <div className="border p-8" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
          <h1 className="mb-2 text-lg font-bold text-[color:var(--text-primary)]">Password required</h1>
          <p className="mb-4 text-sm" style={{ color: 'var(--text-muted)' }}>This shared file is protected.</p>
          <form onSubmit={submitPassword} className="flex gap-2">
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Enter password"
              autoFocus
              className="flex-1 border px-3 py-2 text-sm"
              style={{ borderColor: 'var(--border-strong)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)' }}
            />
            <button
              type="submit"
              className="border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80"
              style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
            >
              Open
            </button>
          </form>
          {passwordError && <p className="mt-2 text-xs" style={{ color: 'var(--err)' }}>{passwordError}</p>}
          <p className="mt-3 text-xs" style={{ color: 'var(--err)' }} id="password-attempt-notice"></p>
        </div>
      )}

      {state.kind === 'error' && state.status !== 401 && (
        <div className="border p-8 text-center" style={{ borderColor: 'var(--err-chip-border)', backgroundColor: 'var(--err-chip-bg)' }}>
          <AlertTriangle size={24} className="mx-auto mb-3" style={{ color: 'var(--err)' }} />
          <h1 className="text-lg font-bold text-[color:var(--text-primary)]">Link unavailable</h1>
          <p className="mt-2 text-sm" style={{ color: 'var(--text-soft)' }}>{state.message}</p>
          <button onClick={() => router.push('/')} className="mt-6 border px-4 py-2 text-sm transition-opacity hover:opacity-80" style={{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }}>
            Go to TorrentCDN
          </button>
        </div>
      )}

      {state.kind === 'ready' && (
        <div>
          <h1 className="mb-1 text-lg font-bold text-[color:var(--text-primary)]">{state.data.filename}</h1>
          <p className="mb-6 text-xs" style={{ color: 'var(--text-muted)' }}>
            {state.data.file_size_mb.toFixed(1)} MB · Shared via TorrentCDN P2P
          </p>
          <div className="mb-6 flex items-center justify-center" style={{ color: 'var(--text-muted)' }}>
            <Film size={48} />
          </div>
          <div className="flex gap-2">
            <a
              href={state.data.minio_url}
              target="_blank"
              rel="noreferrer"
              className="flex flex-1 items-center justify-center gap-2 border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80"
              style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
            >
              <Download size={16} /> Download
            </a>
          </div>
          <p className="mt-4 text-xs" style={{ color: 'var(--text-muted)' }}>
            This link is private — don&apos;t forward it unless you mean to share access.
          </p>
        </div>
      )}
    </div>
  )
}
