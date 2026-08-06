'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Upload, Loader2, Mail, ShieldCheck, Copy, Check, AlertTriangle, Users, Gift, Activity, KeyRound } from 'lucide-react'
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip } from 'recharts'
import QRCode from 'qrcode'
import { isLoggedIn, removeToken } from '@/lib/auth'
import api from '@/lib/api'
import FileCard from '@/components/FileCard'

interface FileItem { id: string; filename: string; minio_url: string; torrent_magnet: string; file_size_mb: number; created_at: string; views: number; p2p_offload_percent: number; bandwidth_saved_gb: number; active_peers: number; thumbnail_url?: string | null }

export default function DashboardPage() {
  const router = useRouter()
  const [files, setFiles] = useState<FileItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [verified, setVerified] = useState<boolean | null>(null)
  const [resending, setResending] = useState(false)
  const [resendMsg, setResendMsg] = useState('')
  const [user, setUser] = useState<{ id: string; email: string; plan: string } | null>(null)
  const [referral, setReferral] = useState<{ referral_code: string; total_referrals: number; bonus_gb_earned: number } | null>(null)
  const [copiedRef, setCopiedRef] = useState(false)
  const [apiKey, setApiKey] = useState<{ api_key: string | null; prefix: string | null; created_at: string | null } | null>(null)
  const [generatingKey, setGeneratingKey] = useState(false)
  const [revokingKey, setRevokingKey] = useState(false)
  const [copiedKey, setCopiedKey] = useState(false)
  const [bandwidth, setBandwidth] = useState<{ date: string; gb: number }[]>([])
  const [deleteAccountConfirm, setDeleteAccountConfirm] = useState(false)
  const [deletingAccount, setDeletingAccount] = useState(false)
  // Two-factor authentication state
  const [mfa, setMfa] = useState<{ enabled: boolean; is_admin: boolean } | null>(null)
  const [mfaSetup, setMfaSetup] = useState<{ secret: string; otpauth_uri: string; backup_codes: string[]; qr?: string } | null>(null)
  const [mfaCode, setMfaCode] = useState('')
  const [mfaBusy, setMfaBusy] = useState(false)
  const [mfaMsg, setMfaMsg] = useState('')
  const [copiedUri, setCopiedUri] = useState(false)

  // Auth gate
  useEffect(() => {
    if (!isLoggedIn()) { router.push('/') }
  }, [router])

  // User profile — fetched independently so a failing files call can never
  // leave the API key (or anything else user-derived) stuck on 'Loading...'.
  useEffect(() => {
    if (!isLoggedIn()) return
    api.getMe().then(setUser).catch(() => {})
  }, [])

  // Referral stats — live counts from the backend, with 0/0 fallback. The
  // referral CODE itself is derived client-side from user.id below (the backend
  // derives it the same way), so the URL renders without waiting on this call.
  useEffect(() => {
    if (!isLoggedIn()) return
    api.getReferral().then(setReferral).catch(() => {})
  }, [])

  // API key metadata (prefix + age — the secret itself is never returned again).
  useEffect(() => {
    if (!isLoggedIn()) return
    api.getApiKey().then(setApiKey).catch(() => {})
  }, [])

  // Verification status + bandwidth history (independent of files)
  useEffect(() => {
    if (!isLoggedIn()) return
    api.getVerificationStatus().then((s) => setVerified(s.verified)).catch(() => {})
    api.getBandwidthHistory().then((b) => setBandwidth(b || [])).catch(() => {})
  }, [])

  // MFA state (enabled + admin flag for the recommended-for-admins nudge).
  useEffect(() => {
    if (!isLoggedIn()) return
    api.mfaStatus().then(setMfa).catch(() => {})
  }, [])

  // Files — with explicit error handling: show the real API error, redirect on
  // 401, and offer a refresh button for transient failures.
  const loadFiles = useCallback(() => {
    setLoading(true)
    api.listFiles()
      .then((data) => { setFiles(data); setError('') })
      .catch((err) => {
        if (err.response?.status === 401) {
          removeToken()
          router.push('/')
          return
        }
        setFiles([])
        setError(err.response?.data?.detail || 'Failed to load files. Please refresh the page.')
      })
      .finally(() => setLoading(false))
  }, [router])

  useEffect(() => {
    if (!isLoggedIn()) return
    loadFiles()
  }, [loadFiles])

  // Referral code — derived client-side from the user id (same derivation the
  // backend uses at registration), so it's available immediately.
  const refCode = user?.id
    ? user.id.replace(/-/g, '').substring(0, 8).toUpperCase()
    : referral?.referral_code || null
  const appUrl = typeof window !== 'undefined'
    ? (process.env.NEXT_PUBLIC_APP_URL || window.location.origin)
    : process.env.NEXT_PUBLIC_APP_URL || 'https://www.torrentcdn.store'
  const referralUrl = refCode ? `${appUrl}?ref=${refCode}` : null

  const handleResend = async () => {
    setResending(true)
    try {
      const res = await api.resendVerification()
      setResendMsg(res.detail || 'Verification email sent!')
    } catch {
      setResendMsg('Failed to send. Try again later.')
    } finally {
      setResending(false)
      setTimeout(() => setResendMsg(''), 5000)
    }
  }

  const handleGenerateKey = async () => {
    setGeneratingKey(true)
    try {
      const data = await api.createApiKey()
      setApiKey(data)
      setCopiedKey(false)
    } catch { alert('Failed to generate API key. Please try again.') }
    finally { setGeneratingKey(false) }
  }

  const handleCopyKey = async () => {
    if (!apiKey?.api_key) return
    await navigator.clipboard.writeText(apiKey.api_key)
    setCopiedKey(true)
    setTimeout(() => setCopiedKey(false), 3000)
  }

  const handleRevokeKey = async () => {
    setRevokingKey(true)
    try {
      await api.revokeApiKey()
      setApiKey(null)
    } catch { alert('Failed to revoke API key. Please try again.') }
    finally { setRevokingKey(false) }
  }

  const handleMfaSetup = async () => {
    setMfaBusy(true)
    setMfaMsg('')
    try {
      const data = await api.mfaSetup()
      // The otpauth:// URI renders as a QR for scanning (and stays copyable
      // for manual entry). data: URLs are allowed by the page CSP (img-src).
      // margin: 4 is the spec quiet zone — margin 1 made phone camera
      // scanners fail to detect the code ("scans, nothing happens").
      const qr = await QRCode.toDataURL(data.otpauth_uri, { width: 240, margin: 4 })
      setMfaSetup({ ...data, qr })
      setMfaCode('')
    } catch (err: any) {
      setMfaMsg(err.response?.data?.detail || 'Failed to start setup. Please try again.')
    } finally { setMfaBusy(false) }
  }

  const handleMfaEnable = async () => {
    setMfaBusy(true)
    setMfaMsg('')
    try {
      await api.mfaEnable(mfaCode)
      // Enabling rotates sessions server-side (pw_change marker) — the current
      // session is invalid by design; sign in again with the code.
      removeToken()
      router.push('/')
    } catch (err: any) {
      setMfaMsg(err.response?.data?.detail || 'Invalid code. Try again.')
      setMfaBusy(false)
    }
  }

  const handleMfaDisable = async () => {
    if (!mfaCode) {
      setMfaMsg('Enter your current code (authenticator or backup) to disable.')
      return
    }
    setMfaBusy(true)
    setMfaMsg('')
    try {
      await api.mfaDisable(mfaCode)
      // Same session rotation on disable.
      removeToken()
      router.push('/')
    } catch (err: any) {
      setMfaMsg(err.response?.data?.detail || 'Invalid code. Try again.')
      setMfaBusy(false)
    }
  }

  const handleDelete = async (id: string) => {
    try { await api.deleteFile(id); setFiles((p) => p.filter((f) => f.id !== id)) }
    catch { alert('Failed to delete file. Please try again.') }
  }

  const handleDeleteAccount = async () => {
    if (!deleteAccountConfirm) { setDeleteAccountConfirm(true); return }
    setDeletingAccount(true)
    try {
      await api.deleteAccount()
      removeToken()
      router.push('/')
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to delete account. Please try again.')
      setDeleteAccountConfirm(false)
    } finally {
      setDeletingAccount(false)
    }
  }

  const totalFiles = files.length
  const totalBW = files.reduce((s, f) => s + f.bandwidth_saved_gb, 0)
  const avgP2P = totalFiles > 0 ? files.reduce((s, f) => s + f.p2p_offload_percent, 0) / totalFiles : 0
  const totalBandwidth7d = bandwidth.reduce((s, d) => s + d.gb, 0)
  const allZero = bandwidth.length > 0 && bandwidth.every((d) => d.gb === 0)

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      {/* Email verification banner */}
      {verified === false && (
        <div className="mb-6 flex items-center gap-3 border px-4 py-3" style={{ borderColor: '#333', backgroundColor: '#1a1a1a' }}>
          <Mail size={16} style={{ color: '#fbbf24' }} />
          <p className="flex-1 text-xs" style={{ color: '#ccc' }}>
            Please verify your email address to unlock all features.
          </p>
          <button
            onClick={handleResend}
            disabled={resending}
            className="border px-3 py-1 text-xs font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
            style={{ borderColor: '#555', color: '#fff' }}
          >
            {resending ? 'Sending...' : 'Resend email'}
          </button>
          {resendMsg && (
            <span className="text-xs" style={{ color: '#4ade80' }}>{resendMsg}</span>
          )}
        </div>
      )}
      {verified === true && (
        <div className="mb-6 flex items-center gap-3 border px-4 py-3" style={{ borderColor: '#1a3a1a', backgroundColor: '#0d1f0d' }}>
          <ShieldCheck size={16} style={{ color: '#4ade80' }} />
          <p className="flex-1 text-xs" style={{ color: '#86efac' }}>
            Email verified
          </p>
        </div>
      )}

      {/* Files load error banner */}
      {error && (
        <div className="mb-6 flex items-center gap-3 border px-4 py-3" style={{ borderColor: '#331111', backgroundColor: '#1a0d0d' }}>
          <AlertTriangle size={16} style={{ color: '#ef4444' }} />
          <p className="flex-1 text-xs" style={{ color: '#fca5a5' }}>{error}</p>
          <button
            onClick={loadFiles}
            className="border px-3 py-1 text-xs font-medium transition-opacity hover:opacity-80"
            style={{ borderColor: '#7f1d1d', color: '#fff' }}
          >
            Refresh
          </button>
        </div>
      )}

      <div className="mb-10 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-[-0.02em] text-white">Your Files</h1>
        </div>
        <a href="/dashboard/upload" className="border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80" style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}>
          Upload file &rarr;
        </a>
      </div>

      {/* API Key — real tcdn_ key: plaintext is shown exactly once at creation
          and stored only as a hash server-side (never recoverable again). */}
      <div className="mb-8 border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
        <div className="flex items-center justify-between">
          <div>
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>API Key</p>
            <p className="mt-1 text-sm" style={{ color: '#555' }}>
              {apiKey?.api_key
                ? 'Authenticate API requests with: Authorization: Bearer <key>'
                : 'Generate a key to use the REST API programmatically.'}
            </p>
          </div>
          {apiKey?.api_key || apiKey?.prefix ? (
            <button
              onClick={handleRevokeKey}
              disabled={revokingKey}
              className="border px-4 py-2 text-xs transition-opacity hover:opacity-80 disabled:opacity-50"
              style={{ borderColor: '#553333', color: '#ef4444' }}
            >
              {revokingKey ? <Loader2 size={12} className="animate-spin" /> : 'Revoke'}
            </button>
          ) : (
            <button
              onClick={handleGenerateKey}
              disabled={generatingKey}
              className="border px-4 py-2 text-xs font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
              style={{ borderColor: '#fff', color: '#fff' }}
            >
              {generatingKey ? <Loader2 size={12} className="animate-spin" /> : 'Generate API key'}
            </button>
          )}
        </div>

        {apiKey?.api_key ? (
          <>
            <div className="mt-3 flex items-center gap-2">
              <code className="flex-1 border px-3 py-2 font-mono text-xs text-white" style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}>
                {apiKey.api_key}
              </code>
              <button
                onClick={handleCopyKey}
                className="border px-3 py-2 text-xs transition-opacity hover:opacity-80"
                style={{ borderColor: '#333', color: '#555' }}
              >
                {copiedKey ? <><Check size={12} className="inline" style={{ color: '#4ade80' }} /> Copied</> : 'Copy'}
              </button>
            </div>
            <p className="mt-2 text-xs" style={{ color: '#fbbf24' }}>
              This is the only time the full key is shown — copy it now. It is stored hashed and cannot be recovered.
            </p>
          </>
        ) : apiKey?.prefix ? (
          <p className="mt-3 text-xs" style={{ color: '#555' }}>
            Current key: <code className="font-mono text-white">{apiKey.prefix}</code>
            {apiKey.created_at ? ` (created ${new Date(apiKey.created_at).toLocaleDateString()})` : ''}.
            Generating a new key revokes the old one.
          </p>
        ) : null}
      </div>

      {/* Two-Factor Authentication — TOTP; setup shows the QR/secret/backup
          codes once, then verifies possession before the flag flips. */}
      <div className="mb-8 border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <KeyRound size={16} style={{ color: '#555' }} />
            <div>
              <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Two-Factor Authentication</p>
              <p className="mt-1 text-sm" style={{ color: '#555' }}>
                {mfa?.enabled
                  ? 'Enabled — signing in requires a code from your authenticator app.'
                  : 'Add a second factor so a stolen password alone cannot access your account.'}
              </p>
            </div>
          </div>
          {mfa && !mfa.enabled && (
            <button
              onClick={handleMfaSetup}
              disabled={mfaBusy}
              className="border px-4 py-2 text-xs font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
              style={{ borderColor: '#fff', color: '#fff' }}
            >
              {mfaBusy ? <Loader2 size={12} className="animate-spin" /> : 'Set up'}
            </button>
          )}
        </div>

        {mfa && !mfa.enabled && mfa.is_admin && (
          <p className="mt-3 text-xs" style={{ color: '#fbbf24' }}>
            Recommended: two-factor authentication is strongly encouraged for administrator accounts.
          </p>
        )}
        {mfa?.enabled && (
          <p className="mt-2 flex items-center gap-2 text-xs" style={{ color: '#4ade80' }}>
            <ShieldCheck size={14} /> Enabled
          </p>
        )}

        {mfaSetup && !mfa?.enabled && (
          <div className="mt-4 border p-4" style={{ borderColor: '#333', backgroundColor: '#0c0c0c' }}>
            <p className="text-xs" style={{ color: '#888' }}>
              1. Scan this QR code with your authenticator app (Google Authenticator, Authy, 1Password…)
              or copy the otpauth link and enter the secret manually.
            </p>
            <div className="mt-3 flex items-start gap-4">
              {mfaSetup.qr && (
                <img src={mfaSetup.qr} alt="TOTP QR code" width={180} height={180}
                  className="border" style={{ backgroundColor: '#fff' }} />
              )}
              <div className="flex-1">
                <p className="text-[11px] uppercase tracking-widest" style={{ color: '#555' }}>Secret</p>
                <code className="block font-mono text-xs text-white" style={{ wordBreak: 'break-all' }}>
                  {mfaSetup.secret}
                </code>
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(mfaSetup.otpauth_uri)
                    setCopiedUri(true)
                    setTimeout(() => setCopiedUri(false), 2000)
                  }}
                  className="mt-2 border px-2 py-1 text-[11px] transition-opacity hover:opacity-80"
                  style={{ borderColor: '#333', color: '#555' }}
                >
                  {copiedUri ? <><Check size={12} className="inline" style={{ color: '#4ade80' }} /> Copied</> : 'Copy otpauth link'}
                </button>
              </div>
            </div>
            <p className="mt-3 text-xs" style={{ color: '#fbbf24' }}>
              2. Save these backup codes somewhere safe. Each is single-use and they are shown only once:
            </p>
            <div className="mt-2 grid grid-cols-2 gap-1 sm:grid-cols-5">
              {mfaSetup.backup_codes.map((c) => (
                <code key={c} className="border px-2 py-1 text-center font-mono text-[11px]"
                  style={{ borderColor: '#333', color: '#ccc' }}>
                  {c}
                </code>
              ))}
            </div>
            <div className="mt-3 flex items-center gap-2">
              <input
                type="text"
                inputMode="numeric"
                value={mfaCode}
                onChange={(e) => setMfaCode(e.target.value)}
                placeholder="6-digit code"
                className="w-40 border px-3 py-2 text-sm text-white"
                style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}
              />
              <button
                onClick={handleMfaEnable}
                disabled={mfaBusy}
                className="border px-4 py-2 text-xs font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
                style={{ borderColor: '#fff', color: '#fff' }}
              >
                {mfaBusy ? <Loader2 size={12} className="animate-spin" /> : 'Enable'}
              </button>
              <p className="text-[11px]" style={{ color: '#888' }}>
                You'll be signed out and must sign in with your code after enabling.
              </p>
            </div>
          </div>
        )}

        {mfa?.enabled && (
          <div className="mt-3 flex items-center gap-2">
            <input
              type="text"
              inputMode="numeric"
              value={mfaCode}
              onChange={(e) => setMfaCode(e.target.value)}
              placeholder="Current code"
              className="w-40 border px-3 py-2 text-sm text-white"
              style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}
            />
            <button
              onClick={handleMfaDisable}
              disabled={mfaBusy}
              className="border px-4 py-2 text-xs font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
              style={{ borderColor: '#553333', color: '#ef4444' }}
            >
              {mfaBusy ? <Loader2 size={12} className="animate-spin" /> : 'Disable'}
            </button>
          </div>
        )}

        {mfaMsg && <p className="mt-3 text-xs" style={{ color: '#fca5a5' }}>{mfaMsg}</p>}
      </div>

      {/* Referral */}
      <div className="mb-8 border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
        <div className="flex items-center gap-3">
          <Users size={16} style={{ color: '#555' }} />
          <div>
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Refer a friend</p>
            <p className="mt-1 text-sm" style={{ color: '#555' }}>You earn <span className="font-medium text-white">10GB</span> for each friend who signs up. They get <span className="font-medium text-white">5GB</span> free.</p>
          </div>
        </div>
        <div className="mt-3 flex items-center gap-2">
          <code className="flex-1 border px-3 py-2 font-mono text-xs text-white" style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}>
            {referralUrl || 'Loading...'}
          </code>
          <button
            onClick={() => {
              if (!referralUrl) return
              navigator.clipboard.writeText(referralUrl)
              setCopiedRef(true)
              setTimeout(() => setCopiedRef(false), 2000)
            }}
            className="border px-3 py-2 text-xs transition-opacity hover:opacity-80"
            style={{ borderColor: '#333', color: '#555' }}
          >
            {copiedRef ? <><Copy size={12} className="inline" style={{ color: '#4ade80' }} /> Copied</> : 'Copy'}
          </button>
        </div>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <div className="border p-4" style={{ borderColor: '#222', backgroundColor: '#0c0c0c' }}>
            <p className="flex items-center gap-2 text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>
              <Users size={12} /> Friends referred
            </p>
            <p className="mt-1 text-lg font-bold text-white">{referral?.total_referrals ?? 0}</p>
          </div>
          <div className="border p-4" style={{ borderColor: '#222', backgroundColor: '#0c0c0c' }}>
            <p className="flex items-center gap-2 text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>
              <Gift size={12} /> Bonus earned
            </p>
            <p className="mt-1 text-lg font-bold text-white">{referral?.bonus_gb_earned ?? 0} GB</p>
          </div>
        </div>
      </div>

      {/* Delete Account */}
      <div className="mb-8 border p-5" style={{ borderColor: '#331111', backgroundColor: '#110808' }}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <AlertTriangle size={16} style={{ color: '#ef4444' }} />
            <div>
              <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#ef4444', fontFamily: 'monospace' }}>Danger Zone</p>
              <p className="mt-1 text-sm" style={{ color: '#888' }}>Permanently delete your account and all associated data. This cannot be undone.</p>
            </div>
          </div>
          <button
            onClick={handleDeleteAccount}
            disabled={deletingAccount}
            className="border px-4 py-2 text-xs font-medium transition-opacity hover:opacity-80 disabled:opacity-50"
            style={{ borderColor: deleteAccountConfirm ? '#ef4444' : '#553333', backgroundColor: deleteAccountConfirm ? '#7f1d1d' : 'transparent', color: '#ef4444' }}
          >
            {deletingAccount ? 'Deleting...' : deleteAccountConfirm ? 'Click again to confirm' : 'Delete account'}
          </button>
        </div>
      </div>

      <div className="mb-10 grid gap-4 sm:grid-cols-3">
        {[
          { label: 'Files uploaded', value: totalFiles },
          { label: 'Bandwidth saved', value: `${totalBW.toFixed(2)} GB` },
          { label: 'Avg P2P offload', value: `${avgP2P.toFixed(1)}%` },
        ].map((s) => (
          <div key={s.label} className="border p-4" style={{ borderColor: '#222', backgroundColor: '#111' }}>
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>{s.label}</p>
            <p className="mt-1 text-lg font-bold text-white">{s.value}</p>
          </div>
        ))}
      </div>

      {/* Bandwidth Usage Graph */}
      <div className="mb-10 border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Activity size={14} style={{ color: '#555' }} />
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>
              Bandwidth Usage — Last 7 Days
            </p>
          </div>
          <p className="text-xs" style={{ color: '#555' }}>
            Total (last 7 days): <span className="font-medium text-white">{totalBandwidth7d.toFixed(2)} GB</span>
          </p>
        </div>
        {allZero || bandwidth.length === 0 ? (
          <div className="flex h-[200px] items-center justify-center border" style={{ borderColor: '#222' }}>
            <p className="text-sm" style={{ color: '#555' }}>No data yet — upload a file to see usage</p>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={bandwidth}>
              <CartesianGrid stroke="#111" />
              <XAxis
                dataKey="date"
                stroke="#222"
                tick={{ fill: '#555', fontSize: 11 }}
                tickFormatter={(v: string) => v.slice(5)}
              />
              <YAxis stroke="#222" tick={{ fill: '#555', fontSize: 11 }} tickFormatter={(v: number) => `${v} GB`} />
              <Tooltip
                contentStyle={{ backgroundColor: '#111', border: '1px solid #222', color: '#fff' }}
                labelStyle={{ color: '#fff' }}
                itemStyle={{ color: '#fff' }}
                // recharts 3 types: value can be undefined/unknown — coerce safely.
                formatter={(value: unknown) => [`${Number(value ?? 0).toFixed(2)} GB`, 'Bandwidth']}
              />
              <Line type="monotone" dataKey="gb" stroke="#fff" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {loading ? (
        <div className="flex h-40 items-center justify-center border" style={{ borderColor: '#222' }}>
          <Loader2 size={20} className="animate-spin" style={{ color: '#555' }} />
        </div>
      ) : !error && files.length === 0 ? (
        <div className="border p-16 text-center" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <Upload size={24} className="mx-auto mb-3" style={{ color: '#555' }} />
          <p className="text-sm text-white">No files yet.</p>
          <a href="/dashboard/upload" className="mt-4 inline-block border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80" style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}>
            Upload your first file &rarr;
          </a>
        </div>
      ) : files.length > 0 ? (
        <div>
          <div className="flex items-center gap-6 border-b px-4 py-2 text-xs font-bold uppercase tracking-widest" style={{ borderColor: '#222', color: '#555', fontFamily: 'monospace' }}>
            <div className="flex-1">Filename</div><div className="w-16">Size</div><div className="w-12">Views</div><div className="w-24">P2P%</div><div className="w-20">Saved</div><div className="w-28">Actions</div>
          </div>
          {files.map((file) => <FileCard key={file.id} file={file} onDelete={handleDelete} />)}
        </div>
      ) : null}
    </div>
  )
}
