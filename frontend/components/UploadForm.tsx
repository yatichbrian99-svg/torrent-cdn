'use client'

import { useState, useCallback, useEffect, useRef } from 'react'
import { Upload, File, CheckCircle, AlertCircle, Loader2, Link2, ArrowDownToLine } from 'lucide-react'
import api, { apiErrorMessage } from '@/lib/api'
import { startSeeding, SeederHandle } from '@/lib/seeder'
import EmbedCode from './EmbedCode'

interface UploadResult {
  file_id: string; filename: string; minio_url: string; magnet_link: string; file_size_mb: number; embed_url: string
}

export default function UploadForm() {
  const [uploading, setUploading] = useState(false)
  const [progress, setProgress] = useState(0)
  const [result, setResult] = useState<UploadResult | null>(null)
  const [error, setError] = useState('')
  const [dragOver, setDragOver] = useState(false)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [retrying, setRetrying] = useState(false)
  // Import-from-link mode: paste a URL and the server fetches a copy
  // (SSRF-guarded) through the same storage pipeline — identical bytes dedup
  // against already-stored content, so it costs the same as an upload.
  const [mode, setMode] = useState<'file' | 'link'>('file')
  const [importUrl, setImportUrl] = useState('')
  const [importFilename, setImportFilename] = useState('')
  // Auto-seed status: after a successful upload this tab shares the file back
  // into the WebTorrent swarm so the FIRST viewer can play via P2P.
  const [seeding, setSeeding] = useState(false)
  const [seedPeers, setSeedPeers] = useState(0)
  const seederRef = useRef<SeederHandle | null>(null)
  // True until unmount. Guards the ASYNC seeding continuation: handleUpload
  // awaits api.uploadFile then startSeeding, and if the user navigates away in
  // that window the unmount cleanup runs with seederRef.current still null —
  // without this flag the continuation would then attach a live webtorrent
  // client + interval with NO teardown path (a leak and a phantom seed).
  const mountedRef = useRef(true)

  const stopSeeding = useCallback(() => {
    if (seederRef.current) {
      try { seederRef.current.destroy() } catch {}
      seederRef.current = null
    }
    setSeeding(false); setSeedPeers(0)
  }, [])

  // If the user navigates away while seeding, tear the swarm down (browsers
  // cannot seed after the tab closes anyway — don't leak the webtorrent client).
  useEffect(() => () => { mountedRef.current = false; stopSeeding() }, [stopSeeding])

  const handleUpload = useCallback(async (file: File) => {
    stopSeeding()
    setUploading(true); setProgress(0); setError(''); setResult(null); setRetrying(false)
    try {
      const data = await api.uploadFile(file, {
        onProgress: (pct) => setProgress(pct),
        onRetry: () => { setRetrying(true); setProgress(0) }, // retry restarts from 0%
      })
      if (!mountedRef.current) return
      setResult(data)
      // Seed the freshly-uploaded file back to peers so the first viewer can
      // fetch it over WebRTC without a manual seeder. Best-effort: if the
      // webtorrent bundle fails to load or no swarm forms, playback still
      // falls back to direct origin streaming.
      const seeder = await startSeeding(file, data.magnet_link)
      if (!mountedRef.current || !seeder) return
      setSeeding(true)
      setSeedPeers(seeder.peers())
      const poll = setInterval(() => setSeedPeers(seeder.peers()), 2000)
      seederRef.current = {
        destroy: () => { clearInterval(poll); seeder.destroy() },
        peers: () => seeder.peers(),
      }
    }
    catch (err: any) { setError(apiErrorMessage(err, 'Upload failed. Please try again.')) }
    finally { setUploading(false); setRetrying(false) }
  }, [stopSeeding])

  const handleImport = useCallback(async () => {
    const url = importUrl.trim()
    if (!url) return
    stopSeeding()
    setUploading(true); setError(''); setResult(null)
    try {
      const data = await api.importUrl(url, importFilename.trim() || undefined)
      if (!mountedRef.current) return
      setResult(data)
      // No local File object to seed the swarm with — the first viewer plays
      // from origin storage until a peer seeds the file. Best-effort note only.
    }
    catch (err: any) { setError(apiErrorMessage(err, 'Import failed. Please try again.')) }
    finally { setUploading(false) }
  }, [importUrl, importFilename, stopSeeding])

  const onDrop = useCallback((e: React.DragEvent) => { e.preventDefault(); setDragOver(false); const file = e.dataTransfer.files[0]; if (file) { setSelectedFile(file); handleUpload(file) } }, [handleUpload])
  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => { const file = e.target.files?.[0]; if (file) { setSelectedFile(file); handleUpload(file) } }
  const fmt = (b: number) => b < 1024 * 1024 ? `${(b / 1024).toFixed(1)} KB` : `${(b / (1024 * 1024)).toFixed(1)} MB`

  if (result) {
    return (
      <div className="border p-6" style={{ backgroundColor: '#111', borderColor: '#222' }}>
        <div className="mb-6 flex items-center gap-3">
          <span className="text-white"><CheckCircle size={18} /></span>
          <div>
            <p className="text-sm font-medium text-white">Upload successful</p>
            <p className="text-sm" style={{ color: '#555' }}>{result.filename} &middot; {result.file_size_mb.toFixed(1)} MB</p>
          </div>
        </div>
        <div className="mb-6">
          <p className="mb-2 text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Embed Code</p>
          <div className="border p-3" style={{ backgroundColor: '#0c0c0c', borderColor: '#222' }}>
            <div className="mb-2 flex items-center justify-between">
              <div className="terminal-dots"><span /><span /><span /></div>
              <span className="text-[10px] uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>embed code</span>
            </div>
            <code className="block text-sm text-white" style={{ fontFamily: 'monospace' }}>&lt;div data-torrent-cdn=&quot;{result.file_id}&quot;&gt;&lt;/div&gt;</code>
          </div>
        </div>
        <div className="mb-6">
          <p className="mb-2 text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Magnet Link</p>
          <div className="flex gap-2">
            <input readOnly value={result.magnet_link} className="flex-1 border px-3 py-2 text-sm" style={{ backgroundColor: '#0c0c0c', borderColor: '#333', color: '#555' }} />
            <button onClick={() => navigator.clipboard.writeText(result.magnet_link)} className="border px-4 py-2 text-sm" style={{ borderColor: '#333', color: '#fff' }}>Copy</button>
          </div>
        </div>
        {seeding && (
          <div className="mb-6 flex items-center gap-2 border-t pt-4" style={{ borderColor: '#222' }}>
            <span className="h-2 w-2 animate-pulse rounded-full" style={{ backgroundColor: seedPeers > 0 ? '#fff' : '#444' }} />
            <span className="text-xs" style={{ color: '#555', fontFamily: 'monospace' }}>
              {seedPeers > 0 ? `seeding to ${seedPeers} peer${seedPeers === 1 ? '' : 's'}` : 'seeding this file to peers…'}
            </span>
          </div>
        )}
        <button onClick={() => { stopSeeding(); setResult(null); setSelectedFile(null); setImportUrl(''); setImportFilename(''); setProgress(0) }}
          className="w-full border py-2 text-sm font-medium" style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}>
          Upload or import another file
        </button>
      </div>
    )
  }

  return (
    <div>
      {/* Upload source: direct file vs import-by-URL */}
      <div className="mb-4 flex gap-1 border p-1" style={{ borderColor: '#222', backgroundColor: '#0c0c0c' }}>
        <button onClick={() => setMode('file')}
          className="flex flex-1 items-center justify-center gap-2 px-4 py-2 text-sm font-medium transition-colors"
          style={{
            backgroundColor: mode === 'file' ? '#fff' : 'transparent',
            color: mode === 'file' ? '#000' : '#555',
          }}>
          <Upload size={14} /> Upload file
        </button>
        <button onClick={() => setMode('link')}
          className="flex flex-1 items-center justify-center gap-2 px-4 py-2 text-sm font-medium transition-colors"
          style={{
            backgroundColor: mode === 'link' ? '#fff' : 'transparent',
            color: mode === 'link' ? '#000' : '#555',
          }}>
          <Link2 size={14} /> Import from link
        </button>
      </div>

      {mode === 'file' ? (
        <div onDragOver={(e) => { e.preventDefault(); setDragOver(true) }} onDragLeave={() => setDragOver(false)} onDrop={onDrop}
          className="relative cursor-pointer border-2 border-dashed p-16 text-center transition-colors"
          style={{ borderColor: dragOver ? '#fff' : '#333', backgroundColor: dragOver ? '#0c0c0c' : '#111' }}>
          <input type="file" accept="video/*,audio/*" onChange={handleInputChange} className="absolute inset-0 cursor-pointer opacity-0" />
          {selectedFile && !uploading ? (
            <div className="flex items-center justify-center gap-3">
              <File size={20} style={{ color: '#555' }} />
              <div className="text-left">
                <p className="text-sm font-medium text-white">{selectedFile.name}</p>
                <p className="text-sm" style={{ color: '#555' }}>{fmt(selectedFile.size)}</p>
              </div>
            </div>
          ) : (
            <div>
              <Upload size={24} className="mx-auto mb-3" style={{ color: '#555' }} />
              <p className="text-sm" style={{ color: '#555' }}>Drop your video or audio file here</p>
              <p className="mt-1 text-sm" style={{ color: '#555' }}>or click to browse</p>
              <p className="mt-1 text-xs" style={{ color: '#444' }}>MP4, WebM, MKV, MP3, WAV up to 2GB</p>
            </div>
          )}
          {uploading && (
            <div className="mx-auto mt-6 max-w-sm">
              <div className="mb-2 flex items-center justify-between text-sm">
                <span style={{ color: retrying ? '#fff' : '#555' }}>{retrying ? 'Connection lost — retrying…' : 'Uploading...'}</span>
                <span className="text-white">{progress}%</span>
              </div>
              <div className="h-1 w-full" style={{ backgroundColor: '#000' }}>
                <div className="h-full transition-all duration-300" style={{ width: `${progress}%`, backgroundColor: '#fff' }} />
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="border p-6" style={{ backgroundColor: '#111', borderColor: '#222' }}>
          <div className="mb-4 flex items-center gap-3">
            <Link2 size={18} style={{ color: '#555' }} />
            <div>
              <p className="text-sm font-medium text-white">Import a media file by URL</p>
              <p className="text-xs" style={{ color: '#555' }}>
                The server fetches a copy and runs the same pipeline — identical content is stored once (dedup).
              </p>
            </div>
          </div>
          <label className="mb-1 block text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>
            Media URL
          </label>
          <input
            type="url"
            value={importUrl}
            onChange={(e) => setImportUrl(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !uploading && importUrl.trim()) handleImport() }}
            placeholder="https://example.com/video.mp4"
            className="w-full border px-3 py-2 text-sm text-white"
            style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}
          />
          <label className="mb-1 mt-4 block text-xs font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>
            Filename <span className="font-normal normal-case" style={{ color: '#444' }}>(optional)</span>
          </label>
          <input
            type="text"
            value={importFilename}
            onChange={(e) => setImportFilename(e.target.value)}
            placeholder="my-video.mp4 — inferred from the URL if left blank"
            className="w-full border px-3 py-2 text-sm text-white"
            style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}
          />
          <button
            onClick={handleImport}
            disabled={uploading || !importUrl.trim()}
            className="mt-5 flex w-full items-center justify-center gap-2 border py-2 text-sm font-medium transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-40"
            style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}>
            {uploading ? <Loader2 size={14} className="animate-spin" /> : <ArrowDownToLine size={14} />}
            {uploading ? 'Importing — server is downloading a copy…' : 'Import & store a copy'}
          </button>
          {uploading && (
            <div className="mt-4">
              <div className="h-1 w-full overflow-hidden" style={{ backgroundColor: '#000' }}>
                <div className="h-full w-1/3 animate-pulse" style={{ backgroundColor: '#fff' }} />
              </div>
              <p className="mt-2 text-xs" style={{ color: '#555' }}>
                Large files take a moment — the download is size-capped and time-boxed server-side.
              </p>
            </div>
          )}
          <p className="mt-4 text-xs" style={{ color: '#444' }}>
            Tip: private/local network addresses are blocked for security, and imports count against the same storage limit as uploads.
          </p>
        </div>
      )}

      {error && (
        <div className="mt-3 flex items-center gap-2 border px-4 py-3 text-sm" style={{ borderColor: '#fff', backgroundColor: '#0c0c0c' }}>
          <AlertCircle size={14} className="text-white" />
          <span className="text-white">{error}</span>
        </div>
      )}
    </div>
  )
}
