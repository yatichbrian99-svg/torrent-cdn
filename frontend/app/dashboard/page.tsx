'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Upload, Loader2, Mail, ShieldCheck, Copy, Check } from 'lucide-react'
import { isLoggedIn } from '@/lib/auth'
import api from '@/lib/api'
import FileCard from '@/components/FileCard'

interface FileItem { id: string; filename: string; minio_url: string; torrent_magnet: string; file_size_mb: number; created_at: string; views: number; p2p_offload_percent: number; bandwidth_saved_gb: number; active_peers: number }

export default function DashboardPage() {
  const router = useRouter()
  const [files, setFiles] = useState<FileItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [verified, setVerified] = useState<boolean | null>(null)
  const [resending, setResending] = useState(false)
  const [resendMsg, setResendMsg] = useState('')
  const [user, setUser] = useState<{ id: string; email: string; plan: string } | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!isLoggedIn()) { router.push('/'); return }
    Promise.all([
      api.listFiles(),
      api.getVerificationStatus().catch(() => ({ verified: null })),
      api.getMe().catch(() => null),
    ]).then(([filesData, statusData, userData]) => {
      setFiles(filesData)
      setVerified(statusData.verified)
      if (userData) setUser(userData)
    }).catch((err) => setError(err.response?.data?.detail || 'Failed to load')).finally(() => setLoading(false))
  }, [router])

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

  const handleCopyApiKey = () => {
    if (!user) return
    const key = `tcdn_${user.id.replace(/-/g, '').substring(0, 32)}`
    navigator.clipboard.writeText(key)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleDelete = async (id: string) => {
    try { await api.deleteFile(id); setFiles((p) => p.filter((f) => f.id !== id)) }
    catch { alert('Failed to delete file. Please try again.') }
  }

  const totalFiles = files.length
  const totalBW = files.reduce((s, f) => s + f.bandwidth_saved_gb, 0)
  const avgP2P = totalFiles > 0 ? files.reduce((s, f) => s + f.p2p_offload_percent, 0) / totalFiles : 0

  if (loading) return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={20} className="animate-spin" style={{ color: '#555' }} /></div>

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

      <div className="mb-10 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-[-0.02em] text-white">Your Files</h1>
          {error && <p className="mt-1 text-sm text-white">{error}</p>}
        </div>
        <a href="/dashboard/upload" className="border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80" style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}>
          Upload file &rarr;
        </a>
      </div>

      {/* API Key */}
      <div className="mb-8 border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
        <div className="flex items-center justify-between">
          <div>
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>API Key</p>
            <p className="mt-1 font-mono text-sm text-white">Use this key to upload files via the REST API</p>
          </div>
        </div>
        <div className="mt-3 flex items-center gap-2">
          <code className="flex-1 border px-3 py-2 font-mono text-xs text-white" style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}>
            {user?.id ? `tcdn_${user.id.replace(/-/g, '').substring(0, 32)}` : 'Loading...'}
          </code>
          <button
            onClick={handleCopyApiKey}
            className="border px-3 py-2 text-xs transition-opacity hover:opacity-80"
            style={{ borderColor: '#333', color: '#555' }}
          >
            {copied ? <><Copy size={12} className="inline" style={{ color: '#4ade80' }} /> Copied</> : 'Copy'}
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

      {files.length === 0 ? (
        <div className="border p-16 text-center" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <Upload size={24} className="mx-auto mb-3" style={{ color: '#555' }} />
          <p className="text-sm text-white">No files yet.</p>
          <a href="/dashboard/upload" className="mt-4 inline-block border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80" style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}>
            Upload your first file &rarr;
          </a>
        </div>
      ) : (
        <div>
          <div className="flex items-center gap-6 border-b px-4 py-2 text-xs font-bold uppercase tracking-widest" style={{ borderColor: '#222', color: '#555', fontFamily: 'monospace' }}>
            <div className="flex-1">Filename</div><div className="w-16">Size</div><div className="w-12">Views</div><div className="w-24">P2P%</div><div className="w-20">Saved</div><div className="w-28">Actions</div>
          </div>
          {files.map((file) => <FileCard key={file.id} file={file} onDelete={handleDelete} />)}
        </div>
      )}
    </div>
  )
}
