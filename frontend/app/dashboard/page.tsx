'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Upload, Loader2 } from 'lucide-react'
import { isLoggedIn } from '@/lib/auth'
import api from '@/lib/api'
import FileCard from '@/components/FileCard'

interface FileItem {
  id: string
  filename: string
  minio_url: string
  torrent_magnet: string
  file_size_mb: number
  created_at: string
  views: number
  p2p_offload_percent: number
  bandwidth_saved_gb: number
  active_peers: number
}

export default function DashboardPage() {
  const router = useRouter()
  const [files, setFiles] = useState<FileItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!isLoggedIn()) { router.push('/'); return }
    api.listFiles().then(setFiles).catch((err) => setError(err.response?.data?.detail || 'Failed to load')).finally(() => setLoading(false))
  }, [router])

  const handleDelete = async (id: string) => {
    try { await api.deleteFile(id); setFiles((p) => p.filter((f) => f.id !== id)) } catch {}
  }

  const totalFiles = files.length
  const totalBW = files.reduce((s, f) => s + f.bandwidth_saved_gb, 0)
  const avgP2P = totalFiles > 0 ? files.reduce((s, f) => s + f.p2p_offload_percent, 0) / totalFiles : 0

  if (loading) return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={20} className="animate-spin" style={{ color: '#666' }} /></div>

  return (
    <div className="mx-auto max-w-5xl px-6 py-10">
      <div className="mb-10 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-[-0.02em] text-white">Your Files</h1>
          {error && <p className="mt-1 text-sm" style={{ color: 'var(--accent)' }}>{error}</p>}
        </div>
        <a
          href="/dashboard/upload"
          className="border px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90"
          style={{ backgroundColor: 'var(--accent)', borderColor: 'var(--accent)' }}
        >
          Upload file &rarr;
        </a>
      </div>

      {/* Stats */}
      <div className="mb-10 grid gap-4 sm:grid-cols-3">
        {[
          { label: 'Files uploaded', value: totalFiles },
          { label: 'Bandwidth saved', value: `${totalBW.toFixed(2)} GB` },
          { label: 'Avg P2P offload', value: `${avgP2P.toFixed(1)}%` },
        ].map((s) => (
          <div key={s.label} className="border p-4" style={{ borderColor: '#222', backgroundColor: '#111' }}>
            <p className="text-xs font-bold uppercase tracking-widest" style={{ color: '#666', fontFamily: 'monospace' }}>{s.label}</p>
            <p className="mt-1 text-lg font-bold text-white">{s.value}</p>
          </div>
        ))}
      </div>

      {/* Files */}
      {files.length === 0 ? (
        <div className="border p-16 text-center" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <Upload size={24} className="mx-auto mb-3" style={{ color: '#666' }} />
          <p className="text-sm text-white">No files yet.</p>
          <a href="/dashboard/upload" className="mt-4 inline-block border px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90" style={{ backgroundColor: 'var(--accent)', borderColor: 'var(--accent)' }}>
            Upload your first file &rarr;
          </a>
        </div>
      ) : (
        <div>
          {/* Header row */}
          <div className="flex items-center gap-6 border-b px-4 py-2 text-xs font-bold uppercase tracking-widest" style={{ borderColor: '#222', color: '#666', fontFamily: 'monospace' }}>
            <div className="flex-1">Filename</div>
            <div className="w-16">Size</div>
            <div className="w-12">Views</div>
            <div className="w-24">P2P%</div>
            <div className="w-20">Saved</div>
            <div className="w-28">Actions</div>
          </div>
          {files.map((file) => <FileCard key={file.id} file={file} onDelete={handleDelete} />)}
        </div>
      )}
    </div>
  )
}
