'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import {
  Loader2, Wallet, UserPlus, Send, ArrowLeft, RefreshCw, Trash2,
  ShieldCheck, CheckCircle, XCircle, AlertTriangle, ArrowDownToLine,
  Phone,  User, FileText, Users, Activity, Ban,
} from 'lucide-react'
import { isLoggedIn } from '@/lib/auth'
import api, { isTransientError, retryOnTransient } from '@/lib/api'

interface Recipient {
  recipient_code: string
  name: string
  account_number: string
  bank_code: string
  bank_name?: string | null
  type: string
  currency: string
  active: boolean
  created_at: string
}

interface Payout {
  transfer_code: string
  recipient_code: string
  recipient_name: string
  amount_cents: number
  amount_kes: number
  currency: string
  reason?: string | null
  status: string
  reference?: string | null
  created_at: string
  updated_at?: string | null
  // Client-generated idempotency key for the pending confirmation — reusing it
  // on retry means Paystack dedupes on reference and can't double-send.
  idempotency_key?: string
}

const STATUS_STYLES: Record<string, { color: string; bg: string; label: string }> = {
  pending: { color: '#fbbf24', bg: 'rgba(251,191,36,0.08)', label: 'Pending' },
  otp: { color: '#fb923c', bg: 'rgba(251,146,60,0.08)', label: 'OTP required' },
  in_transit: { color: '#60a5fa', bg: 'rgba(96,165,250,0.08)', label: 'In transit' },
  success: { color: '#4ade80', bg: 'rgba(74,222,128,0.08)', label: 'Success' },
  failed: { color: '#ff6b6b', bg: 'rgba(255,107,107,0.08)', label: 'Failed' },
  reversed: { color: '#a78bfa', bg: 'rgba(167,139,250,0.08)', label: 'Reversed' },
}

const kes = (n: number) => `KES ${n.toLocaleString('en-US', { maximumFractionDigits: 2 })}`
const fmtDate = (iso: string) =>
  new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

export default function AdminPayoutsPage() {
  const router = useRouter()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null)

  const [balance, setBalance] = useState<{ balance_kes: number } | null>(null)
  const [recipients, setRecipients] = useState<Recipient[]>([])
  const [payouts, setPayouts] = useState<Payout[]>([])

  // Create-recipient form
  const [rcpName, setRcpName] = useState('')
  const [rcpPhone, setRcpPhone] = useState('')
  const [rcpDesc, setRcpDesc] = useState('')
  const [creatingRcp, setCreatingRcp] = useState(false)

  // Initiate-transfer form
  const [selRecipient, setSelRecipient] = useState('')
  const [amount, setAmount] = useState('')
  const [reason, setReason] = useState('')
  const [confirmTarget, setConfirmTarget] = useState<Payout | null>(null)
  const [transferring, setTransferring] = useState(false)
  const [deactivateTarget, setDeactivateTarget] = useState<Recipient | null>(null)
  const [deactivating, setDeactivating] = useState(false)
  const [adminDenied, setAdminDenied] = useState(false)
  const [refreshing, setRefreshing] = useState<string | null>(null)

  useEffect(() => {
    if (toast) {
      const t = setTimeout(() => setToast(null), 4000)
      return () => clearTimeout(t)
    }
  }, [toast])

  const showToast = (message: string, type: 'success' | 'error') => setToast({ message, type })

  const load = useCallback(async () => {
    try {
      // Retry transient failures (Render cold start: 502/503/504 or timeout).
      const [bal, rcps, pays] = await Promise.all([
        api.getPayoutBalance().catch(() => null), // balance is best-effort (Paystack may 502)
        retryOnTransient(() => api.listPayoutRecipients()),
        retryOnTransient(() => api.listPayouts()),
      ])
      setBalance(bal)
      setRecipients(rcps || [])
      setPayouts(pays || [])
      setError('')
    } catch (err: any) {
      // Only a real 403 means "not an admin" — a cold-start 502 previously
      // rendered the 'You need admin privileges…' denial on an admin account.
      if (err?.response?.status === 403) {
        setAdminDenied(true)
        setError('Admin access required')
      } else if (isTransientError(err)) {
        setError('Still connecting to the server — please try again in a moment.')
      } else {
        setError(err?.response?.data?.detail || 'Failed to load payout data')
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!isLoggedIn()) { router.push('/'); return }
    // A transient getMe() failure (cold start) must NOT bounce an admin home.
    // ~1min of retries covers a full Docker/Render boot before giving up.
    retryOnTransient(() => api.getMe(), 5, 8000)
      .then((me) => { if (!me?.is_admin) { router.push('/'); return } load() })
      .catch(() => { router.push('/') })
  }, [router, load])

  const createRecipient = async () => {
    if (!rcpName.trim() || !rcpPhone.trim()) {
      showToast('Name and M-PESA number are required', 'error')
      return
    }
    setCreatingRcp(true)
    try {
      await api.createPayoutRecipient({
        name: rcpName.trim(),
        account_number: rcpPhone.trim(),
        description: rcpDesc.trim() || undefined,
      })
      showToast('Recipient added — ready to pay', 'success')
      setRcpName(''); setRcpPhone(''); setRcpDesc('')
      const rcps = await api.listPayoutRecipients()
      setRecipients(rcps || [])
    } catch (err: any) {
      showToast(err?.response?.data?.detail || 'Failed to create recipient', 'error')
    } finally {
      setCreatingRcp(false)
    }
  }

  const confirmDeactivate = async () => {
    if (!deactivateTarget) return
    setDeactivating(true)
    try {
      await api.deactivatePayoutRecipient(deactivateTarget.recipient_code)
      showToast('Recipient deactivated', 'success')
      setDeactivateTarget(null)
      const rcps = await api.listPayoutRecipients()
      setRecipients(rcps || [])
    } catch (err: any) {
      showToast(err?.response?.data?.detail || 'Failed to deactivate recipient', 'error')
    } finally {
      setDeactivating(false)
    }
  }

  const openConfirm = () => {
    const amt = Number(amount)
    if (!selRecipient) { showToast('Select a recipient', 'error'); return }
    if (!Number.isFinite(amt) || amt < 10) { showToast('Minimum payout is KES 10', 'error'); return }
    const rcp = recipients.find((r) => r.recipient_code === selRecipient)
    if (!rcp) return
    setConfirmTarget({
      transfer_code: '',
      recipient_code: rcp.recipient_code,
      recipient_name: rcp.name,
      amount_cents: 0,
      amount_kes: amt,
      currency: 'KES',
      reason: reason.trim() || null,
      status: '',
      created_at: new Date().toISOString(),
      // One key per intent: if the request fails and the admin retries from
      // the same confirmation, the same reference is sent — no double pay.
      idempotency_key: typeof crypto !== 'undefined' && crypto.randomUUID
        ? crypto.randomUUID()
        : `payout-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    })
  }

  const confirmTransfer = async () => {
    if (!confirmTarget) return
    setTransferring(true)
    try {
      const res = await api.initiatePayout({
        recipient_code: confirmTarget.recipient_code,
        amount: Math.round(confirmTarget.amount_kes),
        reason: confirmTarget.reason || undefined,
        idempotency_key: confirmTarget.idempotency_key,
      })
      showToast(`Transfer initiated — ${res.transfer_code}`, 'success')
      setConfirmTarget(null); setAmount(''); setReason(''); setSelRecipient('')
      const [bal, pays] = await Promise.all([
        api.getPayoutBalance().catch(() => null),
        api.listPayouts(),
      ])
      setBalance(bal); setPayouts(pays || [])
    } catch (err: any) {
      showToast(err?.response?.data?.detail || 'Transfer failed', 'error')
    } finally {
      setTransferring(false)
    }
  }

  const refreshPayout = async (code: string) => {
    setRefreshing(code)
    try {
      const updated = await api.fetchPayout(code)
      showToast(`${code} → ${STATUS_STYLES[updated.status]?.label || updated.status}`, 'success')
      setPayouts((prev) => prev.map((p) => (p.transfer_code === code ? { ...p, ...updated } : p)))
    } catch (err: any) {
      showToast(err?.response?.data?.detail || 'Could not refresh transfer', 'error')
    } finally {
      setRefreshing(null)
    }
  }

  if (loading) return (
    <div className="flex min-h-[70vh] items-center justify-center select-none">
      <div className="flex flex-col items-center gap-4">
        <Loader2 size={24} className="animate-spin" style={{ color: 'var(--text-muted)' }} />
        <p className="text-xs tracking-widest uppercase" style={{ color: 'var(--text-faint)' }}>Loading payouts</p>
      </div>
    </div>
  )

  if (error) return (
    <div className="mx-auto max-w-4xl px-6 py-24 text-center select-none">
      <div className="inline-flex items-center justify-center w-12 h-12 rounded-full mb-4" style={{ backgroundColor: 'rgba(255,107,107,0.1)' }}>
        <ShieldCheck size={20} style={{ color: '#ff6b6b' }} />
      </div>
      <p className="text-sm mb-1" style={{ color: '#ff6b6b' }}>{error}</p>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
        {adminDenied ? 'You need admin privileges to access this page.' : 'Could not load payout data. Please try again.'}
      </p>
    </div>
  )

  const activeRecipients = recipients.filter((r) => r.active)

  return (
    <div className="mx-auto max-w-7xl px-4 sm:px-6 py-8 sm:py-12 select-none">
      {/* Toast */}
      {toast && (
        <div
          className="fixed top-4 right-4 z-[100] px-4 py-3 text-sm border shadow-2xl animate-in slide-in-from-top-2"
          style={{
            backgroundColor: toast.type === 'success' ? 'var(--ok-chip-bg)' : 'var(--err-chip-bg)',
            borderColor: toast.type === 'success' ? 'var(--ok-chip-bg)' : 'var(--err-chip-border)',
            color: toast.type === 'success' ? '#4ade80' : '#ff6b6b',
          }}
        >
          <div className="flex items-center gap-2">
            {toast.type === 'success' ? <CheckCircle size={14} /> : <XCircle size={14} />}
            {toast.message}
          </div>
        </div>
      )}

      {/* Deactivate-recipient confirmation modal — irreversible on Paystack's side */}
      {deactivateTarget && (
        <div className="fixed inset-0 z-[90] flex items-center justify-center p-4" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
          <div className="w-full max-w-sm border p-6 shadow-2xl" style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border)' }}>
            <div className="flex items-center gap-3 mb-4">
              <div className="flex items-center justify-center w-10 h-10 rounded-full" style={{ backgroundColor: 'rgba(255,107,107,0.1)' }}>
                <AlertTriangle size={18} style={{ color: '#ff6b6b' }} />
              </div>
              <div>
                <p className="text-sm font-medium text-[color:var(--text-primary)]">Deactivate Recipient</p>
                <p className="text-xs" style={{ color: 'var(--text-muted)' }}>{deactivateTarget.name}</p>
              </div>
            </div>
            <p className="text-xs mb-5" style={{ color: 'var(--text-dim)' }}>
              This permanently deactivates <span style={{ fontFamily: 'monospace' }}>{deactivateTarget.account_number}</span> on Paystack.
              Existing payouts are kept, but this M-PESA number can no longer receive new transfers.
            </p>
            <div className="flex items-center gap-3 justify-end">
              <button
                onClick={() => setDeactivateTarget(null)}
                className="px-4 py-2 text-xs transition-colors hover:text-[color:var(--text-primary)]"
                style={{ color: 'var(--text-muted)' }}
                disabled={deactivating}
              >
                Cancel
              </button>
              <button
                onClick={confirmDeactivate}
                className="px-4 py-2 text-xs font-medium text-[color:var(--text-primary)] transition-opacity hover:opacity-80 disabled:opacity-50"
                style={{ backgroundColor: '#dc2626' }}
                disabled={deactivating}
              >
                {deactivating ? (
                  <span className="flex items-center gap-2">
                    <Loader2 size={12} className="animate-spin" /> Deactivating...
                  </span>
                ) : 'Deactivate'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Transfer confirmation modal — real money, so confirm before sending */}
      {confirmTarget && (
        <div className="fixed inset-0 z-[90] flex items-center justify-center p-4" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
          <div className="w-full max-w-sm border p-6 shadow-2xl" style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border)' }}>
            <div className="flex items-center gap-3 mb-4">
              <div className="flex items-center justify-center w-10 h-10 rounded-full" style={{ backgroundColor: 'rgba(251,191,36,0.1)' }}>
                <Send size={18} style={{ color: '#fbbf24' }} />
              </div>
              <div>
                <p className="text-sm font-medium text-[color:var(--text-primary)]">Confirm Transfer</p>
                <p className="text-xs" style={{ color: 'var(--text-muted)' }}>Funds leave your Paystack balance</p>
              </div>
            </div>
            <div className="space-y-2 mb-5 text-xs">
              <div className="flex justify-between"><span style={{ color: 'var(--text-dim)' }}>Recipient</span><span className="text-[color:var(--text-primary)]">{confirmTarget.recipient_name}</span></div>
              <div className="flex justify-between"><span style={{ color: 'var(--text-dim)' }}>Amount</span><span className="text-[color:var(--text-primary)] font-semibold">{kes(confirmTarget.amount_kes)}</span></div>
              {confirmTarget.reason && (
                <div className="flex justify-between gap-4"><span style={{ color: 'var(--text-dim)' }}>Reason</span><span className="text-[color:var(--text-primary)] text-right">{confirmTarget.reason}</span></div>
              )}
            </div>
            <div className="flex items-center gap-3 justify-end">
              <button
                onClick={() => setConfirmTarget(null)}
                className="px-4 py-2 text-xs transition-colors hover:text-[color:var(--text-primary)]"
                style={{ color: 'var(--text-muted)' }}
                disabled={transferring}
              >
                Cancel
              </button>
              <button
                onClick={confirmTransfer}
                className="px-4 py-2 text-xs font-medium text-[color:var(--text-primary)] transition-opacity hover:opacity-80 disabled:opacity-50"
                style={{ backgroundColor: '#fbbf24' }}
                disabled={transferring}
              >
                {transferring ? (
                  <span className="flex items-center gap-2">
                    <Loader2 size={12} className="animate-spin" /> Sending...
                  </span>
                ) : `Send ${kes(confirmTarget.amount_kes)}`}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="mb-8">
        <div className="flex items-center gap-3 mb-1">
          <div className="flex items-center justify-center w-8 h-8 rounded-lg" style={{ backgroundColor: 'rgba(251,191,36,0.12)' }}>
            <Wallet size={16} style={{ color: '#fbbf24' }} />
          </div>
          <h1 className="text-lg sm:text-xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">Payouts</h1>
          <span className="ml-1 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest rounded" style={{ backgroundColor: 'rgba(251,191,36,0.15)', color: '#fbbf24' }}>
            M-PESA
          </span>
        </div>
        <div className="flex items-center justify-between mt-2">
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>Pay out from your Paystack balance to M-PESA wallets (Transfers API)</p>
          <div className="flex items-center gap-2">
            <Link
              href="/admin/health"
              className="flex items-center gap-1.5 border px-3 py-1.5 text-[10px] uppercase tracking-widest transition-colors hover:border-white/20"
              style={{ borderColor: 'var(--border)', color: '#4ade80', fontFamily: 'monospace' }}
            >
              <Activity size={11} /> Health
            </Link>
            <Link
              href="/admin"
              className="flex items-center gap-1.5 border px-3 py-1.5 text-[10px] uppercase tracking-widest transition-colors hover:border-white/20"
              style={{ borderColor: 'var(--border)', color: '#a78bfa', fontFamily: 'monospace' }}
            >
              <ArrowLeft size={11} /> Dashboard
            </Link>
          </div>
        </div>
      </div>

      {/* Balance card */}
      <div className="mb-8 border p-5 sm:p-6 overflow-hidden relative" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-secondary)' }}>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Wallet size={14} style={{ color: '#fbbf24' }} />
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Available balance</p>
          </div>
          <button
            onClick={() => api.getPayoutBalance().then(setBalance).catch(() => showToast('Could not refresh balance', 'error'))}
            className="border p-1.5 transition-colors hover:border-white/20"
            style={{ borderColor: 'var(--border)' }}
            title="Refresh balance"
          >
            <RefreshCw size={12} style={{ color: 'var(--text-muted)' }} />
          </button>
        </div>
        <p className="text-3xl font-bold text-[color:var(--text-primary)] tracking-tight">
          {balance ? kes(balance.balance_kes) : '—'}
        </p>
        <p className="mt-2 text-[11px] leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          Transfers draw from this balance. If a transfer returns <span style={{ color: '#fb923c' }}>OTP required</span>,
          complete it in the Paystack dashboard (Settings → Preferences) or approve via the OTP sent to your business phone.
        </p>
      </div>

      <div className="grid lg:grid-cols-2 gap-4 sm:gap-6 mb-8">
        {/* Initiate transfer */}
        <div className="border p-4 sm:p-5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-secondary)' }}>
          <div className="flex items-center gap-2 mb-4">
            <Send size={14} style={{ color: '#fbbf24' }} />
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Initiate transfer</p>
          </div>
          <div className="space-y-3">
            <div>
              <label className="block text-[11px] mb-1.5" style={{ color: 'var(--text-dim)' }}>Recipient (M-PESA)</label>
              <div className="relative">
                <Users size={12} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--text-faint)' }} />
                <select
                  value={selRecipient}
                  onChange={(e) => setSelRecipient(e.target.value)}
                  className="w-full border px-8 py-2 text-xs outline-none transition-colors focus:border-white/20 appearance-none"
                  style={{ backgroundColor: 'var(--bg-raised)', borderColor: 'var(--border)', color: '#fff' }}
                >
                  <option value="">Select recipient…</option>
                  {activeRecipients.map((r) => (
                    <option key={r.recipient_code} value={r.recipient_code}>
                      {r.name} — {r.account_number}
                    </option>
                  ))}
                </select>
              </div>
              {activeRecipients.length === 0 && (
                <p className="mt-1.5 text-[11px] flex items-center gap-1" style={{ color: '#fb923c' }}>
                  <AlertTriangle size={10} /> No active recipients — create one on the right first.
                </p>
              )}
            </div>
            <div>
              <label className="block text-[11px] mb-1.5" style={{ color: 'var(--text-dim)' }}>Amount (KES)</label>
              <input
                type="number"
                min={10}
                step={1}
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="e.g. 500"
                className="w-full border px-3 py-2 text-xs outline-none transition-colors focus:border-white/20"
                style={{ backgroundColor: 'var(--bg-raised)', borderColor: 'var(--border)', color: '#fff' }}
              />
              <p className="mt-1.5 text-[11px]" style={{ color: 'var(--text-faint)' }}>Minimum KES 10 · sent to Paystack as {amount ? `${Number(amount) * 100} cents` : 'cents'}</p>
            </div>
            <div>
              <label className="block text-[11px] mb-1.5" style={{ color: 'var(--text-dim)' }}>Reason (optional)</label>
              <div className="relative">
                <FileText size={12} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--text-faint)' }} />
                <input
                  type="text"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="e.g. Creator payout — July"
                  maxLength={255}
                  className="w-full border px-8 py-2 text-xs outline-none transition-colors focus:border-white/20"
                  style={{ backgroundColor: 'var(--bg-raised)', borderColor: 'var(--border)', color: '#fff' }}
                />
              </div>
            </div>
            <button
              onClick={openConfirm}
              disabled={!selRecipient || !amount || activeRecipients.length === 0}
              className="w-full px-4 py-2.5 text-xs font-semibold text-[color:var(--text-primary)] transition-opacity hover:opacity-80 disabled:opacity-40 disabled:cursor-not-allowed"
              style={{ backgroundColor: '#fbbf24' }}
            >
              Review transfer
            </button>
          </div>
        </div>

        {/* Create recipient */}
        <div className="border p-4 sm:p-5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-secondary)' }}>
          <div className="flex items-center gap-2 mb-4">
            <UserPlus size={14} style={{ color: '#a78bfa' }} />
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Add recipient</p>
          </div>
          <div className="space-y-3">
            <div>
              <label className="block text-[11px] mb-1.5" style={{ color: 'var(--text-dim)' }}>Full name</label>
              <div className="relative">
                <User size={12} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--text-faint)' }} />
                <input
                  type="text"
                  value={rcpName}
                  onChange={(e) => setRcpName(e.target.value)}
                  placeholder="e.g. Jane M"
                  maxLength={255}
                  className="w-full border px-8 py-2 text-xs outline-none transition-colors focus:border-white/20"
                  style={{ backgroundColor: 'var(--bg-raised)', borderColor: 'var(--border)', color: '#fff' }}
                />
              </div>
            </div>
            <div>
              <label className="block text-[11px] mb-1.5" style={{ color: 'var(--text-dim)' }}>M-PESA number</label>
              <div className="relative">
                <Phone size={12} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--text-faint)' }} />
                <input
                  type="tel"
                  value={rcpPhone}
                  onChange={(e) => setRcpPhone(e.target.value)}
                  placeholder="254712345678 or 0712345678"
                  maxLength={20}
                  className="w-full border px-8 py-2 text-xs outline-none transition-colors focus:border-white/20"
                  style={{ backgroundColor: 'var(--bg-raised)', borderColor: 'var(--border)', color: '#fff' }}
                />
              </div>
              <p className="mt-1.5 text-[11px]" style={{ color: 'var(--text-faint)' }}>Sent to Paystack as a <span style={{ color: 'var(--text-dim)' }}>mobile_money</span> recipient (MPESA).</p>
            </div>
            <div>
              <label className="block text-[11px] mb-1.5" style={{ color: 'var(--text-dim)' }}>Description (optional)</label>
              <input
                type="text"
                value={rcpDesc}
                onChange={(e) => setRcpDesc(e.target.value)}
                placeholder="e.g. Creator on Business plan"
                maxLength={255}
                className="w-full border px-3 py-2 text-xs outline-none transition-colors focus:border-white/20"
                style={{ backgroundColor: 'var(--bg-raised)', borderColor: 'var(--border)', color: '#fff' }}
              />
            </div>
            <button
              onClick={createRecipient}
              disabled={creatingRcp}
              className="w-full px-4 py-2.5 text-xs font-semibold text-[color:var(--text-primary)] transition-opacity hover:opacity-80 disabled:opacity-50"
              style={{ backgroundColor: '#8b5cf6' }}
            >
              {creatingRcp ? (
                <span className="flex items-center justify-center gap-2"><Loader2 size={12} className="animate-spin" /> Creating…</span>
              ) : 'Add recipient'}
            </button>
          </div>
        </div>
      </div>

      {/* Recipients */}
      <div className="border mb-8" style={{ borderColor: 'var(--border)' }}>
        <div className="border-b px-4 sm:px-5 py-3" style={{ borderColor: 'var(--border)' }}>
          <div className="flex items-center gap-2">
            <Users size={14} style={{ color: 'var(--text-muted)' }} />
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
              Recipients <span className="ml-2 font-normal" style={{ color: 'var(--text-faint)' }}>({recipients.length})</span>
            </p>
          </div>
        </div>
        <div className="overflow-x-auto">
          {recipients.length === 0 ? (
            <p className="px-5 py-8 text-center text-xs" style={{ color: 'var(--text-faint)' }}>No recipients yet.</p>
          ) : (
            <table className="w-full text-left">
              <thead>
                <tr className="text-[10px] uppercase tracking-widest" style={{ color: 'var(--text-faint)' }}>
                  <th className="px-4 sm:px-5 py-2.5 font-medium">Name</th>
                  <th className="px-4 py-2.5 font-medium">M-PESA</th>
                  <th className="px-4 py-2.5 font-medium">Code</th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                  <th className="px-4 py-2.5 font-medium text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {recipients.map((r) => (
                  <tr key={r.recipient_code} className="border-t" style={{ borderColor: 'var(--divider)' }}>
                    <td className="px-4 sm:px-5 py-3 text-xs text-[color:var(--text-primary)]">{r.name}</td>
                    <td className="px-4 py-3 text-xs" style={{ color: 'var(--text-soft)' }}>{r.account_number}</td>
                    <td className="px-4 py-3 text-[11px]" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>{r.recipient_code}</td>
                    <td className="px-4 py-3">
                      <span
                        className="px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest rounded"
                        style={{
                          backgroundColor: r.active ? 'rgba(74,222,128,0.08)' : 'rgba(255,107,107,0.08)',
                          color: r.active ? '#4ade80' : '#ff6b6b',
                        }}
                      >
                        {r.active ? 'Active' : 'Inactive'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right">
                      {r.active && (
                        <button
                          onClick={() => setDeactivateTarget(r)}
                          className="inline-flex items-center gap-1 text-[11px] transition-colors hover:text-red-400"
                          style={{ color: 'var(--text-muted)' }}
                        >
                          <Trash2 size={11} /> Deactivate
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* Payout history */}
      <div className="border" style={{ borderColor: 'var(--border)' }}>
        <div className="border-b px-4 sm:px-5 py-3" style={{ borderColor: 'var(--border)' }}>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <ArrowDownToLine size={14} style={{ color: 'var(--text-muted)' }} />
              <p className="text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
                Transfer history <span className="ml-2 font-normal" style={{ color: 'var(--text-faint)' }}>({payouts.length})</span>
              </p>
            </div>
            <button
              onClick={() => api.listPayouts().then(setPayouts).catch(() => showToast('Could not refresh transfers', 'error'))}
              className="border p-1.5 transition-colors hover:border-white/20"
              style={{ borderColor: 'var(--border)' }}
              title="Refresh transfers"
            >
              <RefreshCw size={12} style={{ color: 'var(--text-muted)' }} />
            </button>
          </div>
        </div>
        <div className="overflow-x-auto">
          {payouts.length === 0 ? (
            <p className="px-5 py-8 text-center text-xs" style={{ color: 'var(--text-faint)' }}>No transfers yet — initiate one above.</p>
          ) : (
            <table className="w-full text-left">
              <thead>
                <tr className="text-[10px] uppercase tracking-widest" style={{ color: 'var(--text-faint)' }}>
                  <th className="px-4 sm:px-5 py-2.5 font-medium">Date</th>
                  <th className="px-4 py-2.5 font-medium">Recipient</th>
                  <th className="px-4 py-2.5 font-medium">Amount</th>
                  <th className="px-4 py-2.5 font-medium">Reason</th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                  <th className="px-4 py-2.5 font-medium text-right">Refresh</th>
                </tr>
              </thead>
              <tbody>
                {payouts.map((p) => {
                  const st = STATUS_STYLES[p.status] || { color: 'var(--text-soft)', bg: 'rgba(153,153,153,0.08)', label: p.status }
                  return (
                    <tr key={p.transfer_code} className="border-t" style={{ borderColor: 'var(--divider)' }}>
                      <td className="px-4 sm:px-5 py-3 text-xs whitespace-nowrap" style={{ color: 'var(--text-soft)' }}>{fmtDate(p.created_at)}</td>
                      <td className="px-4 py-3 text-xs text-[color:var(--text-primary)]">
                        {p.recipient_name || p.recipient_code}
                        <span className="block text-[10px]" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>{p.transfer_code}</span>
                      </td>
                      <td className="px-4 py-3 text-xs font-semibold text-[color:var(--text-primary)] whitespace-nowrap">{kes(p.amount_kes)}</td>
                      <td className="px-4 py-3 text-xs max-w-[180px] truncate" style={{ color: 'var(--text-soft)' }}>{p.reason || '—'}</td>
                      <td className="px-4 py-3">
                        <span
                          className="px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest rounded"
                          style={{ backgroundColor: st.bg, color: st.color }}
                        >
                          {st.label}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => refreshPayout(p.transfer_code)}
                          disabled={refreshing === p.transfer_code}
                          className="inline-flex items-center gap-1 text-[11px] transition-colors hover:text-[color:var(--text-primary)]"
                          style={{ color: 'var(--text-muted)' }}
                          title="Fetch latest status from Paystack"
                        >
                          {refreshing === p.transfer_code ? (
                            <Loader2 size={11} className="animate-spin" />
                          ) : (
                            <RefreshCw size={11} />
                          )}
                          {refreshing === p.transfer_code ? 'Syncing' : 'Sync'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <p className="mt-6 text-[11px] flex items-center gap-1.5" style={{ color: 'var(--text-faint)' }}>
        <Ban size={11} /> Transfers send from your Paystack balance. Transfer completion arrives via webhook
        (<span style={{ fontFamily: 'monospace' }}>transfer.success / failed / reversed</span>) and updates the status here automatically.
      </p>
    </div>
  )
}
