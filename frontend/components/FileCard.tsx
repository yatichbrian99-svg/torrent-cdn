'use client'

import { useState } from 'react'
import { Trash2, Copy, Check, Loader2, Film, FolderInput, Pencil, Link2 } from 'lucide-react'
import api from '@/lib/api'
import EmbedCode from './EmbedCode'
import ShareLinkDialog from './ShareLinkDialog'

interface FileItem {
  id: string; filename: string; minio_url: string; torrent_magnet: string; file_size_mb: number
  created_at: string; views: number; p2p_offload_percent: number; bandwidth_saved_gb: number; active_peers: number
  thumbnail_url?: string | null
  folder_path?: string | null  // DFS M1: logical folder (null/undefined = root)
}

export default function FileCard({ file, onDelete, onRenamed }: { file: FileItem; onDelete: (id: string) => void; onRenamed?: (id: string, filename: string, folder_path: string | null) => void }) {
  const [showEmbed, setShowEmbed] = useState(false)
  const [showRename, setShowRename] = useState(false)
  const [showShare, setShowShare] = useState(false)
  const [renameName, setRenameName] = useState(file.filename)
  const [renameFolder, setRenameFolder] = useState(file.folder_path || '')
  const [renameBusy, setRenameBusy] = useState(false)
  const [renameError, setRenameError] = useState('')
  const [shareLink, setShareLink] = useState<{ url: string; token: string } | null>(null)
  const [shareBusy, setShareBusy] = useState(false)
  const [shareError, setShareError] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [copied, setCopied] = useState(false)

  const handleConfirmDelete = async () => {
    setDeleting(true)
    await onDelete(file.id)
    setDeleting(false)
    setConfirmDelete(false)
  }

  const handleCopy = async () => {
    await navigator.clipboard.writeText(`<div data-torrent-cdn="${file.id}"></div>`)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleRename = async () => {
    setRenameBusy(true)
    setRenameError('')
    try {
      const updated = await api.renameFile(file.id, {
        filename: renameName.trim(),
        folder_path: renameFolder.trim() === '' ? null : renameFolder.trim(),
      })
      onRenamed?.(file.id, updated.filename, updated.folder_path)
      setShowRename(false)
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setRenameError(detail || 'Rename failed')
    } finally {
      setRenameBusy(false)
    }
  }

  const handleCreateLink = async (opts: { expires_in_hours: number | null; password: string | null }) => {
    setShareBusy(true)
    setShareError('')
    try {
      const link = await api.createShareLink(file.id, {
        expires_in_hours: opts.expires_in_hours,
        password: opts.password || null,
      })
      setShareLink({ url: link.url, token: link.token })  // plaintext shown exactly once
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setShareError(detail || 'Could not create link')
    } finally {
      setShareBusy(false)
    }
  }

  return (
    <>
      <div className="border-b px-4 py-3" style={{ borderColor: 'var(--border)' }}>
        <div className="flex items-center gap-6 text-sm">
          {/* Thumbnail: small on the left, larger preview on hover */}
          <div className="group relative shrink-0">
            {file.thumbnail_url ? (
              <img
                src={file.thumbnail_url}
                alt=""
                className="border object-cover"
                style={{ width: 72, height: 40, borderColor: 'var(--border)' }}
              />
            ) : (
              <div className="flex items-center justify-center border" style={{ width: 72, height: 40, backgroundColor: 'var(--bg-card)', borderColor: 'var(--border)' }}>
                <Film size={14} style={{ color: 'var(--border-strong)' }} />
              </div>
            )}
            {file.thumbnail_url && (
              <div className="pointer-events-none absolute left-0 top-full z-30 mt-1 hidden border group-hover:block" style={{ borderColor: 'var(--border-strong)' }}>
                <img src={file.thumbnail_url} alt="" style={{ width: 200, height: 120 }} />
              </div>
            )}
          </div>
          <div className="min-w-0 flex-1">
            <p className="truncate font-medium text-[color:var(--text-primary)]">{file.filename}</p>
            {file.folder_path ? (
              <p className="truncate text-xs" style={{ color: 'var(--text-muted)' }}>
                <FolderInput size={10} className="mr-1 inline" />{file.folder_path}
              </p>
            ) : null}
          </div>
          <span className="w-16 shrink-0" style={{ color: 'var(--text-muted)' }}>{file.file_size_mb.toFixed(1)} MB</span>
          <span className="w-12 shrink-0" style={{ color: 'var(--text-muted)' }}>{file.views}</span>
          <div className="w-24 shrink-0">
            <div className="mb-1 flex items-center justify-between text-xs" style={{ color: 'var(--text-muted)' }}><span>{file.p2p_offload_percent.toFixed(0)}%</span></div>
            <div className="h-1 w-full" style={{ backgroundColor: 'var(--border)' }}>
              <div className="h-full" style={{ width: `${Math.min(file.p2p_offload_percent, 100)}%`, backgroundColor: 'var(--inverse-bg)' }} />
            </div>
          </div>
          <span className="w-20 shrink-0 text-xs" style={{ color: 'var(--text-muted)' }}>{file.bandwidth_saved_gb.toFixed(3)} GB</span>
          <div className="flex shrink-0 items-center gap-2">
            <button onClick={handleCopy} aria-label="Copy embed code" className="border px-2 py-1 text-xs transition-opacity hover:opacity-80" style={{ borderColor: 'var(--border-strong)', color: 'var(--text-muted)' }}>
              {copied ? <Check size={12} /> : <Copy size={12} />}
            </button>
            <button onClick={() => { setRenameName(file.filename); setRenameFolder(file.folder_path || ''); setRenameError(''); setShowRename(true) }} aria-label="Rename or move" className="border px-2 py-1" style={{ borderColor: 'var(--border-strong)', color: 'var(--text-muted)' }} title="Rename / move">
              <Pencil size={12} />
            </button>
            <button onClick={() => { setShareLink(null); setShareError(''); setShowShare(true) }} className="border px-2 py-1 text-xs" style={{ borderColor: 'var(--border-strong)', color: 'var(--text-muted)' }} title="Create share link">
              <Link2 size={12} className="inline" /> Share
            </button>
            <button onClick={() => setShowEmbed(true)} className="border px-2 py-1 text-xs" style={{ borderColor: 'var(--border-strong)', color: 'var(--text-muted)' }}>Embed</button>
            {confirmDelete ? (
              <div className="flex items-center gap-1">
                <button onClick={handleConfirmDelete} disabled={deleting} aria-label="Confirm delete" className="border px-2 py-1 text-xs disabled:opacity-50" style={{ borderColor: 'var(--inverse-bg)', color: 'var(--text-primary)' }}>
                  {deleting ? <Loader2 size={12} className="animate-spin" /> : 'Confirm'}
                </button>
                {!deleting && (
                  <button onClick={() => setConfirmDelete(false)} aria-label="Cancel delete" className="border px-2 py-1 text-xs" style={{ borderColor: 'var(--border-strong)', color: 'var(--text-muted)' }}>No</button>
                )}
              </div>
            ) : (
              <button onClick={() => setConfirmDelete(true)} aria-label="Delete file" className="border px-2 py-1 text-xs transition-opacity hover:opacity-80" style={{ borderColor: 'var(--border-strong)', color: 'var(--text-muted)' }}>
                <Trash2 size={12} />
              </button>
            )}
          </div>
        </div>
      </div>


      {showRename && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
          <div className="w-full max-w-sm border p-6" style={{ backgroundColor: 'var(--bg-card)', borderColor: 'var(--border-strong)' }}>
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-[color:var(--text-primary)]">Rename / Move</h3>
              <button onClick={() => setShowRename(false)} className="text-sm" style={{ color: 'var(--text-muted)' }}>&times;</button>
            </div>
            <label className="mb-1 block text-xs" style={{ color: 'var(--text-muted)' }}>Filename</label>
            <input value={renameName} onChange={(e) => setRenameName(e.target.value)} className="mb-3 w-full border px-3 py-2 text-sm" style={{ borderColor: 'var(--border-strong)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)' }} />
            <label className="mb-1 block text-xs" style={{ color: 'var(--text-muted)' }}>Folder (optional, empty = root)</label>
            <input value={renameFolder} onChange={(e) => setRenameFolder(e.target.value)} placeholder="e.g. Videos" className="mb-3 w-full border px-3 py-2 text-sm" style={{ borderColor: 'var(--border-strong)', backgroundColor: 'var(--bg-secondary)', color: 'var(--text-primary)' }} />
            {renameError && <p className="mb-3 text-xs" style={{ color: 'var(--err)' }}>{renameError}</p>}
            <button onClick={handleRename} disabled={renameBusy} className="w-full border px-4 py-2 text-sm font-medium transition-opacity hover:opacity-80 disabled:opacity-50" style={{ borderColor: 'var(--inverse-bg)', color: 'var(--text-primary)' }}>
              {renameBusy ? <Loader2 size={14} className="animate-spin" /> : 'Save'}
            </button>
          </div>
        </div>
      )}

      {showShare && (
        <ShareLinkDialog file={file} link={shareLink} busy={shareBusy} error={shareError} onClose={() => setShowShare(false)} onCreate={handleCreateLink} />
      )}

      {showEmbed && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
          <div className="w-full max-w-lg border p-6" style={{ backgroundColor: 'var(--bg-card)', borderColor: 'var(--border-strong)' }}>
            <div className="mb-6 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-[color:var(--text-primary)]">Embed Code</h3>
              <button onClick={() => setShowEmbed(false)} className="text-sm" style={{ color: 'var(--text-muted)' }}>&times;</button>
            </div>
            <EmbedCode fileId={file.id} />
          </div>
        </div>
      )}
    </>
  )
}
