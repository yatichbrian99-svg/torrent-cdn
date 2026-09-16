'use client'

import { useEffect, useState, useMemo, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import {
  Loader2, Users, Film, HardDrive, Crown, DollarSign,
  Search, X, ShieldCheck, Trash2, CheckCircle, XCircle,
  AlertTriangle, BarChart3, UserCog, ArrowUpDown, Copy, Check,
  RefreshCw, Calendar, FolderOpen, UserCheck, Activity,
  ChevronDown, ChevronUp,
} from 'lucide-react'
import Link from 'next/link'
import { isLoggedIn } from '@/lib/auth'
import api, { isTransientError, retryOnTransient } from '@/lib/api'

interface Analytics {
  total_users: number
  total_files: number
  total_bandwidth_gb: number
  total_p2p_saved_gb: number
  users_by_plan: { free: number; pro: number; business: number }
  new_users_today: number
  new_files_today: number
  revenue_estimate: number
}

interface User {
  id: string
  email: string
  plan: string
  verified: boolean
  is_admin: boolean
  created_at: string
  total_gb_used: number
  total_files: number
}

type SortField = 'email' | 'plan' | 'verified' | 'total_files' | 'total_gb_used' | 'created_at'
type SortDir = 'asc' | 'desc'

export default function AdminPage() {
  const router = useRouter()
  const [analytics, setAnalytics] = useState<Analytics | null>(null)
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [sortField, setSortField] = useState<SortField>('created_at')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [deleteTarget, setDeleteTarget] = useState<User | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null)
  const [planUpdating, setPlanUpdating] = useState<string | null>(null)
  const [adminToggling, setAdminToggling] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [total, setTotal] = useState(0)
  // Copy-email affordance on the users table — the page is select-none, so
  // the email needs an explicit copy button to remain usable for admins.
  const [copiedEmailId, setCopiedEmailId] = useState<string | null>(null)

  // Toast auto-dismiss
  useEffect(() => {
    if (toast) {
      const t = setTimeout(() => setToast(null), 3000)
      return () => clearTimeout(t)
    }
  }, [toast])

  const fetchData = useCallback(async (pageNum: number) => {
    try {
      // Retry transient failures (Render cold start after idle: 502/503/504 or
      // no response) — the free-tier backend wakes in a few seconds and a retry
      // loads the page instead of an error.
      const [stats, data] = await Promise.all([
        // retries=5 (+ the escalating schedule ≈ 47s of retrying) rides out a
        // full Docker/Render cold start — the backend needs ~40-60s after a
        // deploy/restart, and giving up early reads as a broken dashboard.
        retryOnTransient(() => api.getAdminStats(), 5),
        retryOnTransient(() => api.getAdminUsers(pageNum), 5),
      ])
      setAnalytics(stats)
      setUsers(data.users || [])
      setTotal(data.total || 0)
      setPages(data.pages || 1)
      setPage(data.page || 1)
      setError('')
    } catch (err: any) {
      // ONLY a real 403 means the account isn't an admin. Any other failure
      // (cold-start 502, timeout, 500) must NOT render the 'You need admin
      // privileges…' denial — that message on an actual admin account was the
      // reported bug. The same transient gate applies to getMe() below.
      if (err?.response?.status === 403) {
        setError('Admin access required')
      } else if (isTransientError(err)) {
        setError('Still connecting to the server — please try again in a moment.')
      } else {
        setError('Failed to load admin data')
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!isLoggedIn()) { router.push('/'); return }
    // Redirect non-admins to the homepage. A transient failure of getMe()
    // itself (cold start) must NOT bounce a logged-in admin home — retry for
    // up to ~a minute (boot window), and only give up (treating it as a dead
    // session) after retries exhaust.
    retryOnTransient(() => api.getMe(), 5, 8000)
      .then((me) => { if (!me?.is_admin) { router.push('/'); return } fetchData(1) })
      .catch(() => { router.push('/') })
  }, [router, fetchData])

  const showToast = (message: string, type: 'success' | 'error') => {
    setToast({ message, type })
  }

  const updatePlan = async (userId: string, plan: string) => {
    setPlanUpdating(userId)
    try {
      await api.updateUserPlan(userId, plan)
      showToast(`Plan updated to ${plan}`, 'success')
      fetchData(page)
    } catch (err: any) {
      showToast(err?.response?.data?.detail || 'Failed to update plan', 'error')
    } finally {
      setPlanUpdating(null)
    }
  }

  const toggleAdmin = async (user: User) => {
    setAdminToggling(user.id)
    try {
      await api.toggleAdmin(user.id)
      showToast('Admin status updated', 'success')
      fetchData(page)
    } catch (err: any) {
      showToast(err?.response?.data?.detail || 'Failed to update admin status', 'error')
    } finally {
      setAdminToggling(null)
    }
  }

  const deleteUser = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await api.deleteUser(deleteTarget.id)
      showToast(`User ${deleteTarget.email} deleted`, 'success')
      setDeleteTarget(null)
      fetchData(page)
    } catch (err: any) {
      showToast(err?.response?.data?.detail || 'Failed to delete user', 'error')
    } finally {
      setDeleting(false)
    }
  }

  const copyEmail = async (userId: string, email: string) => {
    try {
      await navigator.clipboard.writeText(email)
      setCopiedEmailId(userId)
      setTimeout(() => setCopiedEmailId(null), 2000)
    } catch { /* clipboard unavailable (non-secure context) — ignore */ }
  }

  // Sorting
  const toggleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortDir('asc')
    }
  }

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return <ArrowUpDown size={11} className="ml-1 opacity-30" />
    return sortDir === 'asc' ? <ChevronUp size={11} className="ml-1" /> : <ChevronDown size={11} className="ml-1" />
  }

  // Filtered + sorted users
  const filteredUsers = useMemo(() => {
    let result = users.filter((u) =>
      u.email.toLowerCase().includes(searchQuery.toLowerCase())
    )

    result.sort((a, b) => {
      let cmp = 0
      switch (sortField) {
        case 'email': cmp = a.email.localeCompare(b.email); break
        case 'plan': cmp = a.plan.localeCompare(b.plan); break
        case 'verified': cmp = Number(b.verified) - Number(a.verified); break
        case 'total_files': cmp = a.total_files - b.total_files; break
        case 'total_gb_used': cmp = a.total_gb_used - b.total_gb_used; break
        case 'created_at': cmp = a.created_at.localeCompare(b.created_at); break
      }
      return sortDir === 'asc' ? cmp : -cmp
    })

    return result
  }, [users, searchQuery, sortField, sortDir])

  // Plan badge styling per spec:
  // free — plain white text on #111; pro — white text, #222 border; business — white text, #fff border
  const planColors: Record<string, { bg: string; text: string; border?: string }> = {
    free: { bg: '#111', text: '#fff' },
    pro: { bg: '#111', text: '#fff', border: '#222' },
    business: { bg: '#111', text: '#fff', border: '#fff' },
  }

  // Loading state
  if (loading) return (
    <div className="flex min-h-[70vh] items-center justify-center select-none">
      <div className="flex flex-col items-center gap-4">
        <Loader2 size={24} className="animate-spin" style={{ color: '#555' }} />
        <p className="text-xs tracking-widest uppercase" style={{ color: '#444' }}>Loading dashboard</p>
      </div>
    </div>
  )

  // Error state
  if (error) return (
    <div className="mx-auto max-w-4xl px-6 py-24 text-center select-none">
      <div className="inline-flex items-center justify-center w-12 h-12 rounded-full mb-4" style={{ backgroundColor: 'rgba(255,107,107,0.1)' }}>
        <ShieldCheck size={20} style={{ color: '#ff6b6b' }} />
      </div>
      <p className="text-sm mb-1" style={{ color: '#ff6b6b' }}>{error}</p>
      <p className="text-xs" style={{ color: '#555' }}>You need admin privileges to access this page.</p>
    </div>
  )

  return (
    <div className="mx-auto max-w-7xl px-4 sm:px-6 py-8 sm:py-12 select-none">
      {/* Toast */}
      {toast && (
        <div
          className="fixed top-4 right-4 z-[100] px-4 py-3 text-sm border shadow-2xl animate-in slide-in-from-top-2"
          style={{
            backgroundColor: toast.type === 'success' ? '#0a2a0a' : '#2a0a0a',
            borderColor: toast.type === 'success' ? '#1a4a1a' : '#4a1a1a',
            color: toast.type === 'success' ? '#4ade80' : '#ff6b6b',
          }}
        >
          <div className="flex items-center gap-2">
            {toast.type === 'success' ? <CheckCircle size={14} /> : <XCircle size={14} />}
            {toast.message}
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {deleteTarget && (
        <div className="fixed inset-0 z-[90] flex items-center justify-center p-4" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
          <div
            className="w-full max-w-sm border p-6 shadow-2xl"
            style={{ backgroundColor: '#0c0c0c', borderColor: '#222' }}
          >
            <div className="flex items-center gap-3 mb-4">
              <div className="flex items-center justify-center w-10 h-10 rounded-full" style={{ backgroundColor: 'rgba(255,107,107,0.1)' }}>
                <AlertTriangle size={18} style={{ color: '#ff6b6b' }} />
              </div>
              <div>
                <p className="text-sm font-medium text-white">Delete User</p>
                <p className="text-xs" style={{ color: '#555' }}>{deleteTarget.email}</p>
              </div>
            </div>
            <p className="text-xs mb-5" style={{ color: '#666' }}>
              This will permanently delete this user and all their files, analytics, and usage records. This action cannot be undone.
            </p>
            {deleteTarget.is_admin && (
              <p className="text-xs mb-4 px-3 py-2 border" style={{ color: '#fbbf24', borderColor: 'rgba(251,191,36,0.2)', backgroundColor: 'rgba(251,191,36,0.05)' }}>
                Admin accounts cannot be deleted. Demote the user first.
              </p>
            )}
            <div className="flex items-center gap-3 justify-end">
              <button
                onClick={() => setDeleteTarget(null)}
                className="px-4 py-2 text-xs transition-colors hover:text-white"
                style={{ color: '#555' }}
                disabled={deleting}
              >
                Cancel
              </button>
              <button
                onClick={deleteUser}
                className="px-4 py-2 text-xs font-medium text-white transition-opacity hover:opacity-80 disabled:opacity-50"
                style={{ backgroundColor: '#dc2626' }}
                disabled={deleting || deleteTarget.is_admin}
              >
                {deleting ? (
                  <span className="flex items-center gap-2">
                    <Loader2 size={12} className="animate-spin" /> Deleting...
                  </span>
                ) : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="mb-8">
        <div className="flex items-center gap-3 mb-1">
          <div className="flex items-center justify-center w-8 h-8 rounded-lg" style={{ backgroundColor: 'rgba(168,85,247,0.12)' }}>
            <ShieldCheck size={16} style={{ color: '#a78bfa' }} />
          </div>
          <h1 className="text-lg sm:text-xl font-bold tracking-[-0.02em] text-white">Admin Dashboard</h1>
          <span
            className="ml-1 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest rounded"
            style={{ backgroundColor: 'rgba(168,85,247,0.15)', color: '#a78bfa' }}
          >
            Admin
          </span>
        </div>
        <div className="flex items-center justify-between mt-2">
          <p className="text-xs" style={{ color: '#555' }}>Monitor platform activity and manage users</p>
          <Link
            href="/admin/payouts"
            className="flex items-center gap-1.5 border px-3 py-1.5 text-[10px] uppercase tracking-widest transition-colors hover:border-white/20"
            style={{ borderColor: '#222', color: '#fbbf24', fontFamily: 'monospace' }}
          >
            <DollarSign size={11} /> Payouts
          </Link>
          <Link
            href="/admin/health"
            className="flex items-center gap-1.5 border px-3 py-1.5 text-[10px] uppercase tracking-widest transition-colors hover:border-white/20"
            style={{ borderColor: '#222', color: '#4ade80', fontFamily: 'monospace' }}
          >
            <Activity size={11} /> Security & Health
          </Link>
        </div>
      </div>

      {/* Analytics Cards */}
      {analytics && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-5 gap-3 sm:gap-4 mb-8">
            {[
              {
                icon: Users, label: 'Total Users', value: analytics.total_users,
                sub: `${analytics.users_by_plan.free} free · ${analytics.users_by_plan.pro} pro · ${analytics.users_by_plan.business} business`,
                color: '#60a5fa', bg: 'rgba(59,130,246,0.08)',
              },
              {
                icon: Film, label: 'Total Files', value: analytics.total_files,
                sub: `${analytics.new_files_today} new today`,
                color: '#34d399', bg: 'rgba(52,211,153,0.08)',
              },
              {
                icon: HardDrive, label: 'Bandwidth Used', value: `${analytics.total_bandwidth_gb} GB`,
                sub: 'Origin bandwidth delivered',
                color: '#fbbf24', bg: 'rgba(251,191,36,0.08)',
              },
              {
                icon: Crown, label: 'P2P Saved', value: `${analytics.total_p2p_saved_gb} GB`,
                sub: `${analytics.new_users_today} new users today`,
                color: '#a78bfa', bg: 'rgba(168,85,247,0.08)',
              },
              {
                icon: DollarSign, label: 'Est. Revenue', value: `$${analytics.revenue_estimate.toLocaleString()}`,
                sub: 'Pro × $29 · Business × $99',
                color: '#4ade80', bg: 'rgba(74,222,128,0.08)',
              },
            ].map((card, i) => (
              <div
                key={i}
                className="group relative border transition-all duration-200 hover:border-opacity-40 p-4 sm:p-5 overflow-hidden"
                style={{ borderColor: '#222', backgroundColor: '#0c0c0c' }}
              >
                <div className="relative z-10">
                  <div className="flex items-center justify-between mb-3">
                    <div
                      className="flex items-center justify-center w-8 h-8 rounded-lg transition-all duration-200 group-hover:scale-110"
                      style={{ backgroundColor: card.bg }}
                    >
                      <card.icon size={15} style={{ color: card.color }} />
                    </div>
                    <span
                      className="text-[10px] uppercase tracking-widest font-medium transition-opacity duration-200 opacity-0 group-hover:opacity-100"
                      style={{ color: '#555' }}
                    >
                      View
                    </span>
                  </div>
                  <p className="text-2xl font-bold text-white tracking-tight">{card.value}</p>
                  <p className="mt-1 text-[11px] leading-relaxed" style={{ color: '#555' }}>{card.sub}</p>
                </div>
                {/* Hover accent line */}
                <div
                  className="absolute bottom-0 left-0 right-0 h-[2px] opacity-0 group-hover:opacity-100 transition-opacity duration-300"
                  style={{ backgroundColor: card.color }}
                />
              </div>
            ))}
          </div>

          {/* Plan Distribution Mini Bar */}
          <div className="mb-8 border p-4 sm:p-5" style={{ borderColor: '#222', backgroundColor: '#0c0c0c' }}>
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <BarChart3 size={14} style={{ color: '#555' }} />
                <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555' }}>Plan Distribution</p>
              </div>
              <span className="text-[10px]" style={{ color: '#444' }}>{analytics.total_users} total</span>
            </div>
            <div className="flex h-2 gap-[2px] rounded-full overflow-hidden">
              {analytics.users_by_plan.free > 0 && (
                <div
                  className="h-full transition-all duration-500"
                  style={{
                    width: `${(analytics.users_by_plan.free / analytics.total_users) * 100}%`,
                    backgroundColor: '#333',
                  }}
                  title={`Free: ${analytics.users_by_plan.free}`}
                />
              )}
              {analytics.users_by_plan.pro > 0 && (
                <div
                  className="h-full transition-all duration-500"
                  style={{
                    width: `${(analytics.users_by_plan.pro / analytics.total_users) * 100}%`,
                    backgroundColor: '#3b82f6',
                  }}
                  title={`Pro: ${analytics.users_by_plan.pro}`}
                />
              )}
              {analytics.users_by_plan.business > 0 && (
                <div
                  className="h-full transition-all duration-500"
                  style={{
                    width: `${(analytics.users_by_plan.business / analytics.total_users) * 100}%`,
                    backgroundColor: '#8b5cf6',
                  }}
                  title={`Business: ${analytics.users_by_plan.business}`}
                />
              )}
            </div>
            <div className="flex items-center gap-4 mt-3">
              <span className="flex items-center gap-1.5 text-[10px]" style={{ color: '#555' }}>
                <span className="w-2 h-2 rounded-full" style={{ backgroundColor: '#333' }} /> Free ({analytics.users_by_plan.free})
              </span>
              <span className="flex items-center gap-1.5 text-[10px]" style={{ color: '#555' }}>
                <span className="w-2 h-2 rounded-full" style={{ backgroundColor: '#3b82f6' }} /> Pro ({analytics.users_by_plan.pro})
              </span>
              <span className="flex items-center gap-1.5 text-[10px]" style={{ color: '#555' }}>
                <span className="w-2 h-2 rounded-full" style={{ backgroundColor: '#8b5cf6' }} /> Business ({analytics.users_by_plan.business})
              </span>
            </div>
          </div>
        </>
      )}

      {/* Users Section */}
      <div className="border" style={{ borderColor: '#222' }}>
        {/* Header with Search */}
        <div className="border-b px-4 sm:px-5 py-3 sm:py-4" style={{ borderColor: '#222' }}>
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <UserCog size={14} style={{ color: '#555' }} />
              <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555' }}>
                Users
                <span className="ml-2 font-normal" style={{ color: '#444' }}>({total})</span>
              </p>
            </div>
            <div className="flex items-center gap-2">
              {/* Search */}
              <div className="relative flex-1 sm:flex-initial">
                <Search size={12} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: '#444' }} />
                <input
                  type="text"
                  placeholder="Search by email..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full sm:w-56 border px-8 py-1.5 text-xs outline-none transition-colors focus:border-white/20"
                  style={{ backgroundColor: '#080808', borderColor: '#222', color: '#fff' }}
                />
                {searchQuery && (
                  <button onClick={() => setSearchQuery('')} className="absolute right-3 top-1/2 -translate-y-1/2">
                    <X size={12} style={{ color: '#444' }} />
                  </button>
                )}
              </div>
              <button
                onClick={() => fetchData(page)}
                className="border p-1.5 transition-colors hover:border-white/20"
                style={{ borderColor: '#222' }}
                title="Refresh"
              >
                <RefreshCw size={12} style={{ color: '#555' }} />
              </button>
            </div>
          </div>
        </div>

        {/* Users Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b" style={{ borderColor: '#222' }}>
                <th
                  className="px-4 sm:px-5 py-3 text-left text-xs font-medium cursor-pointer"
                  style={{ color: '#555' }}
                  onClick={() => toggleSort('email')}
                >
                  <span className="flex items-center">
                    Email <SortIcon field="email" />
                  </span>
                </th>
                <th
                  className="px-4 sm:px-5 py-3 text-left text-xs font-medium cursor-pointer"
                  style={{ color: '#555' }}
                  onClick={() => toggleSort('plan')}
                >
                  <span className="flex items-center">
                    Plan <SortIcon field="plan" />
                  </span>
                </th>
                <th
                  className="hidden sm:table-cell px-4 sm:px-5 py-3 text-left text-xs font-medium cursor-pointer"
                  style={{ color: '#555' }}
                  onClick={() => toggleSort('verified')}
                >
                  <span className="flex items-center">
                    Status <SortIcon field="verified" />
                  </span>
                </th>
                <th
                  className="hidden md:table-cell px-4 sm:px-5 py-3 text-left text-xs font-medium cursor-pointer"
                  style={{ color: '#555' }}
                  onClick={() => toggleSort('total_files')}
                >
                  <span className="flex items-center">
                    Files <SortIcon field="total_files" />
                  </span>
                </th>
                <th
                  className="hidden md:table-cell px-4 sm:px-5 py-3 text-left text-xs font-medium cursor-pointer"
                  style={{ color: '#555' }}
                  onClick={() => toggleSort('total_gb_used')}
                >
                  <span className="flex items-center">
                    Usage <SortIcon field="total_gb_used" />
                  </span>
                </th>
                <th
                  className="hidden lg:table-cell px-4 sm:px-5 py-3 text-left text-xs font-medium cursor-pointer"
                  style={{ color: '#555' }}
                  onClick={() => toggleSort('created_at')}
                >
                  <span className="flex items-center">
                    Joined <SortIcon field="created_at" />
                  </span>
                </th>
                <th className="px-4 sm:px-5 py-3 text-right text-xs font-medium" style={{ color: '#555' }}>
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {filteredUsers.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-5 py-12 text-center">
                    <div className="flex flex-col items-center gap-2">
                      <Search size={18} style={{ color: '#333' }} />
                      <p className="text-xs" style={{ color: '#555' }}>
                        {searchQuery ? `No users matching "${searchQuery}"` : 'No users found'}
                      </p>
                    </div>
                  </td>
                </tr>
              ) : (
                filteredUsers.map((user) => {
                  const planColor = planColors[user.plan] || planColors.free
                  return (
                    <tr
                      key={user.id}
                      className="group border-b transition-colors duration-150 hover:bg-white/[0.02]"
                      style={{ borderColor: '#222' }}
                    >
                      <td className="px-4 sm:px-5 py-3">
                        <div className="flex items-center gap-2.5">
                          {user.is_admin ? (
                            <div className="flex items-center justify-center w-6 h-6 rounded-full flex-shrink-0" style={{ backgroundColor: 'rgba(168,85,247,0.12)' }}>
                              <ShieldCheck size={12} style={{ color: '#a78bfa' }} />
                            </div>
                          ) : (
                            <div className="flex items-center justify-center w-6 h-6 rounded-full flex-shrink-0" style={{ backgroundColor: 'rgba(255,255,255,0.04)' }}>
                              <UserCheck size={12} style={{ color: '#555' }} />
                            </div>
                          )}
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="text-sm text-white truncate max-w-[160px] sm:max-w-[220px] block">{user.email}</span>
                              <button
                                onClick={() => copyEmail(user.id, user.email)}
                                className="p-1 rounded transition-all duration-150 opacity-0 group-hover:opacity-100 hover:bg-white/10"
                                style={{ color: '#555' }}
                                title="Copy email"
                                aria-label={`Copy ${user.email}`}
                              >
                                {copiedEmailId === user.id
                                  ? <Check size={12} style={{ color: '#4ade80' }} />
                                  : <Copy size={12} className="transition-colors hover:text-white" />}
                              </button>
                              {user.is_admin && (
                                <span
                                  className="px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider rounded flex-shrink-0"
                                  style={{ backgroundColor: 'rgba(168,85,247,0.15)', color: '#a78bfa' }}
                                >
                                  Admin
                                </span>
                              )}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td className="px-4 sm:px-5 py-3">
                        <div className="flex items-center gap-2">
                          {planUpdating === user.id ? (
                            <Loader2 size={12} className="animate-spin" style={{ color: '#555' }} />
                          ) : (
                            <select
                              value={user.plan}
                              onChange={(e) => updatePlan(user.id, e.target.value)}
                              className="border px-2 py-1 text-[11px] text-white outline-none cursor-pointer transition-colors hover:border-white/20"
                              style={{
                                backgroundColor: planColor.bg,
                                borderColor: planColor.border || 'transparent',
                                color: planColor.text,
                                borderRadius: 0,
                              }}
                            >
                              <option value="free" style={{ backgroundColor: '#111', color: '#888' }}>Free</option>
                              <option value="pro" style={{ backgroundColor: '#111', color: '#60a5fa' }}>Pro</option>
                              <option value="business" style={{ backgroundColor: '#111', color: '#a78bfa' }}>Business</option>
                            </select>
                          )}
                        </div>
                      </td>
                      <td className="hidden sm:table-cell px-4 sm:px-5 py-3">
                        <span
                          className="inline-flex items-center gap-1.5 text-[11px]"
                          style={{ color: user.verified ? '#4ade80' : '#555' }}
                        >
                          {user.verified ? (
                            <><CheckCircle size={12} /> Verified</>
                          ) : (
                            <><XCircle size={12} /> Unverified</>
                          )}
                        </span>
                      </td>
                      <td className="hidden md:table-cell px-4 sm:px-5 py-3">
                        <span className="flex items-center gap-1.5 text-xs" style={{ color: '#555' }}>
                          <FolderOpen size={11} /> {user.total_files}
                        </span>
                      </td>
                      <td className="hidden md:table-cell px-4 sm:px-5 py-3">
                        <span className="text-xs" style={{ color: '#555' }}>{user.total_gb_used} GB</span>
                      </td>
                      <td className="hidden lg:table-cell px-4 sm:px-5 py-3">
                        <span className="flex items-center gap-1.5 text-xs" style={{ color: '#555' }}>
                          <Calendar size={11} />
                          {new Date(user.created_at).toLocaleDateString('en-US', {
                            month: 'short', day: 'numeric', year: 'numeric',
                          })}
                        </span>
                      </td>
                      <td className="px-4 sm:px-5 py-3">
                        <div className="flex items-center justify-end gap-1">
                          {!user.verified && !user.is_admin && (
                            <button
                              onClick={async () => {
                                try {
                                  await api.verifyUser(user.id)
                                  showToast(`Verified ${user.email}`, 'success')
                                  fetchData(page)
                                } catch (err: any) {
                                  showToast(err?.response?.data?.detail || 'Failed to verify', 'error')
                                }
                              }}
                              className="p-1.5 rounded transition-all duration-150 opacity-0 group-hover:opacity-100 hover:bg-green-500/10"
                              style={{ color: '#555' }}
                              title="Verify user"
                            >
                              <CheckCircle size={12} className="transition-colors hover:text-green-400" />
                            </button>
                          )}
                          <button
                            onClick={() => toggleAdmin(user)}
                            disabled={adminToggling === user.id}
                            className="p-1.5 rounded transition-all duration-150 opacity-0 group-hover:opacity-100 hover:bg-purple-500/10 disabled:opacity-40"
                            style={{ color: '#555' }}
                            title={user.is_admin ? 'Revoke admin' : 'Make admin'}
                          >
                            {adminToggling === user.id ? (
                              <Loader2 size={12} className="animate-spin" />
                            ) : (
                              <UserCog size={12} className="transition-colors hover:text-purple-400" />
                            )}
                          </button>
                          <button
                            onClick={() => setDeleteTarget(user)}
                            className="p-1.5 rounded transition-all duration-150 opacity-0 group-hover:opacity-100 hover:bg-red-500/10"
                            style={{ color: '#555' }}
                            title="Delete user"
                          >
                            <Trash2 size={12} className="transition-colors hover:text-red-400" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>

        {/* Footer with pagination */}
        <div className="border-t px-5 py-3" style={{ borderColor: '#222' }}>
          <div className="flex items-center justify-between">
            <p className="text-[10px]" style={{ color: '#444' }}>
              {total} user{total === 1 ? '' : 's'}
              {searchQuery && ` · filtering "${searchQuery}"`}
            </p>
            <div className="flex items-center gap-3">
              {searchQuery && (
                <button
                  onClick={() => setSearchQuery('')}
                  className="text-[10px] transition-colors hover:text-white"
                  style={{ color: '#555' }}
                >
                  Clear filter
                </button>
              )}
              <div className="flex items-center gap-2">
                <button
                  onClick={() => fetchData(page - 1)}
                  disabled={page <= 1}
                  className="border px-3 py-1 text-[10px] uppercase tracking-widest transition-colors hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
                  style={{ borderColor: '#222', color: '#555', fontFamily: 'monospace' }}
                >
                  Previous
                </button>
                <span className="text-[10px]" style={{ color: '#555' }}>Page {page} of {pages}</span>
                <button
                  onClick={() => fetchData(page + 1)}
                  disabled={page >= pages}
                  className="border px-3 py-1 text-[10px] uppercase tracking-widest transition-colors hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
                  style={{ borderColor: '#222', color: '#555', fontFamily: 'monospace' }}
                >
                  Next
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
