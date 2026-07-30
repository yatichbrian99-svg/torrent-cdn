'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Loader2, Users, Film, HardDrive, Crown, ChevronRight } from 'lucide-react'
import { isLoggedIn, getToken } from '@/lib/auth'

interface Analytics {
  total_users: number
  total_files: number
  total_storage_gb: number
  free_users: number
  pro_users: number
  business_users: number
}

interface User {
  id: string
  email: string
  plan: string
  verified: boolean
  is_admin: boolean
  created_at: string
  usage_gb: number
  file_count: number
}

export default function AdminPage() {
  const router = useRouter()
  const [analytics, setAnalytics] = useState<Analytics | null>(null)
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!isLoggedIn()) { router.push('/'); return }
    fetchData()
  }, [router])

  const fetchData = async () => {
    const token = getToken()
    const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }

    try {
      const [analyticsRes, usersRes] = await Promise.all([
        fetch('/api/admin/analytics', { headers }),
        fetch('/api/admin/users', { headers }),
      ])

      if (analyticsRes.status === 403) {
        setError('Admin access required')
        setLoading(false)
        return
      }

      setAnalytics(await analyticsRes.json())
      setUsers(await usersRes.json())
    } catch {
      setError('Failed to load admin data')
    } finally {
      setLoading(false)
    }
  }

  const updatePlan = async (userId: string, plan: string) => {
    const token = getToken()
    try {
      const res = await fetch(`/api/admin/users/${userId}/plan`, {
        method: 'PUT',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan }),
      })
      if (res.ok) fetchData()
    } catch {}
  }

  const deleteUser = async (userId: string) => {
    if (!confirm('Delete this user and all their data?')) return
    const token = getToken()
    try {
      const res = await fetch(`/api/admin/users/${userId}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (res.ok) fetchData()
    } catch {}
  }

  if (loading) return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={20} className="animate-spin" style={{ color: '#555' }} /></div>
  if (error) return <div className="mx-auto max-w-4xl px-6 py-20 text-center"><p className="text-sm" style={{ color: '#ff6b6b' }}>{error}</p></div>

  return (
    <div className="mx-auto max-w-6xl px-6 py-10">
      <h1 className="text-xl font-bold tracking-[-0.02em] text-white">Admin Dashboard</h1>

      {/* Analytics Cards */}
      {analytics && (
        <div className="mt-6 grid gap-4 md:grid-cols-4">
          <div className="border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
            <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-widest" style={{ color: '#555' }}>
              <Users size={14} /> Total Users
            </div>
            <p className="text-2xl font-bold text-white">{analytics.total_users}</p>
            <p className="mt-1 text-xs" style={{ color: '#555' }}>
              {analytics.free_users} free · {analytics.pro_users} pro · {analytics.business_users} business
            </p>
          </div>
          <div className="border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
            <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-widest" style={{ color: '#555' }}>
              <Film size={14} /> Total Files
            </div>
            <p className="text-2xl font-bold text-white">{analytics.total_files}</p>
          </div>
          <div className="border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
            <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-widest" style={{ color: '#555' }}>
              <HardDrive size={14} /> Storage Used
            </div>
            <p className="text-2xl font-bold text-white">{analytics.total_storage_gb} GB</p>
          </div>
          <div className="border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
            <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-widest" style={{ color: '#555' }}>
              <Crown size={14} /> Paid Users
            </div>
            <p className="text-2xl font-bold text-white">{analytics.pro_users + analytics.business_users}</p>
          </div>
        </div>
      )}

      {/* Users Table */}
      <div className="mt-8 border" style={{ borderColor: '#222' }}>
        <div className="border-b px-5 py-3" style={{ borderColor: '#222' }}>
          <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555' }}>Users</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b" style={{ borderColor: '#222' }}>
                <th className="px-5 py-3 text-left font-medium text-white">Email</th>
                <th className="px-5 py-3 text-left font-medium text-white">Plan</th>
                <th className="px-5 py-3 text-left font-medium text-white">Verified</th>
                <th className="px-5 py-3 text-left font-medium text-white">Files</th>
                <th className="px-5 py-3 text-left font-medium text-white">Usage</th>
                <th className="px-5 py-3 text-left font-medium text-white">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id} className="border-b" style={{ borderColor: '#222' }}>
                  <td className="px-5 py-3 text-white">{user.email}</td>
                  <td className="px-5 py-3">
                    <select
                      value={user.plan}
                      onChange={(e) => updatePlan(user.id, e.target.value)}
                      className="border px-2 py-1 text-xs text-white"
                      style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}
                    >
                      <option value="free">Free</option>
                      <option value="pro">Pro</option>
                      <option value="business">Business</option>
                    </select>
                  </td>
                  <td className="px-5 py-3">
                    <span className={`text-xs ${user.verified ? '' : ''}`} style={{ color: user.verified ? '#4ade80' : '#555' }}>
                      {user.verified ? '✅' : '❌'}
                    </span>
                  </td>
                  <td className="px-5 py-3" style={{ color: '#555' }}>{user.file_count}</td>
                  <td className="px-5 py-3" style={{ color: '#555' }}>{user.usage_gb} GB</td>
                  <td className="px-5 py-3">
                    <button
                      onClick={() => deleteUser(user.id)}
                      className="text-xs hover:text-white transition-colors"
                      style={{ color: '#555' }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
