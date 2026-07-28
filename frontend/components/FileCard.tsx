'use client'

import { useState } from 'react'
import { Trash2, Copy, Check } from 'lucide-react'
import EmbedCode from './EmbedCode'

interface FileItem {
  id: string; filename: string; minio_url: string; torrent_magnet: string; file_size_mb: number
  created_at: string; views: number; p2p_offload_percent: number; bandwidth_saved_gb: number; active_peers: number
}

export default function FileCard({ file, onDelete }: { file: FileItem; onDelete: (id: string) => void }) {
  const [showEmbed, setShowEmbed] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [copied, setCopied] = useState(false)
  const createdDate = new Date(file.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })

  const handleCopy = async () => {
    await navigator.clipboard.writeText(`<div data-torrent-cdn="${file.id}"></div>`)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <>
      <div className="border-b px-4 py-3" style={{ borderColor: '#222' }}>
        <div className="flex items-center gap-6 text-sm">
          <div className="min-w-0 flex-1"><p className="truncate font-medium text-white">{file.filename}</p></div>
          <span className="w-16 shrink-0" style={{ color: '#555' }}>{file.file_size_mb.toFixed(1)} MB</span>
          <span className="w-12 shrink-0" style={{ color: '#555' }}>{file.views}</span>
          <div className="w-24 shrink-0">
            <div className="mb-1 flex items-center justify-between text-xs" style={{ color: '#555' }}><span>{file.p2p_offload_percent.toFixed(0)}%</span></div>
            <div className="h-1 w-full" style={{ backgroundColor: '#222' }}>
              <div className="h-full" style={{ width: `${Math.min(file.p2p_offload_percent, 100)}%`, backgroundColor: '#fff' }} />
            </div>
          </div>
          <span className="w-20 shrink-0 text-xs" style={{ color: '#555' }}>{file.bandwidth_saved_gb.toFixed(3)} GB</span>
          <div className="flex shrink-0 items-center gap-2">
            <button onClick={handleCopy} className="border px-2 py-1 text-xs transition-opacity hover:opacity-80" style={{ borderColor: '#333', color: '#555' }}>
              {copied ? <Check size={12} /> : <Copy size={12} />}
            </button>
            <button onClick={() => setShowEmbed(true)} className="border px-2 py-1 text-xs" style={{ borderColor: '#333', color: '#555' }}>Embed</button>
            {confirmDelete ? (
              <div className="flex items-center gap-1">
                <button onClick={() => { onDelete(file.id); setConfirmDelete(false) }} className="border px-2 py-1 text-xs" style={{ borderColor: '#fff', color: '#fff' }}>Confirm</button>
                <button onClick={() => setConfirmDelete(false)} className="border px-2 py-1 text-xs" style={{ borderColor: '#333', color: '#555' }}>No</button>
              </div>
            ) : (
              <button onClick={() => setConfirmDelete(true)} className="border px-2 py-1 text-xs transition-opacity hover:opacity-80" style={{ borderColor: '#333', color: '#555' }}>
                <Trash2 size={12} />
              </button>
            )}
          </div>
        </div>
      </div>

      {showEmbed && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
          <div className="w-full max-w-lg border p-6" style={{ backgroundColor: '#111', borderColor: '#333' }}>
            <div className="mb-6 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-white">Embed Code</h3>
              <button onClick={() => setShowEmbed(false)} className="text-sm" style={{ color: '#555' }}>&times;</button>
            </div>
            <EmbedCode fileId={file.id} />
          </div>
        </div>
      )}
    </>
  )
}
