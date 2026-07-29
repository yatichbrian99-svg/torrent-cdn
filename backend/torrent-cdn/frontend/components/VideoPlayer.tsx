'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { Users, Loader2 } from 'lucide-react'
import api from '@/lib/api'

interface FileData { id: string; filename: string; minio_url: string; magnet_link: string; file_size_mb: number }

export default function VideoPlayer({ fileId }: { fileId: string }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [fileData, setFileData] = useState<FileData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [peers, setPeers] = useState(0)
  const [status, setStatus] = useState<'connecting' | 'p2p' | 'direct' | 'error'>('connecting')
  const statusRef = useRef<string>('connecting')
  const clientRef = useRef<any>(null)
  const fallbackTimer = useRef<NodeJS.Timeout>()
  const pollTimer = useRef<NodeJS.Timeout>()

  useEffect(() => {
    if (fileId === 'demo') { setFileData({ id: 'demo', filename: 'Demo Video.mp4', minio_url: '', magnet_link: '', file_size_mb: 0 }); setLoading(false); return }
    api.getPublicFile(fileId).then((data: FileData) => { setFileData(data); setLoading(false) }).catch(() => { setError('File not found'); setLoading(false) })
  }, [fileId])

  const fallbackToDirect = useCallback(() => {
    if (!videoRef.current || !fileData?.minio_url) return
    videoRef.current.src = fileData.minio_url; videoRef.current.play().catch(() => {})
    setStatus('direct'); statusRef.current = 'direct'
    api.trackEvent(fileData.id, 'chunk_downloaded', 0, Math.round(fileData.file_size_mb * 1024 * 1024)).catch(() => {})
  }, [fileData])

  useEffect(() => {
    if (!fileData || fileId === 'demo' || !fileData.magnet_link) return
    let mounted = true

    const init = async () => {
      try {
        const WebTorrent = (await import('webtorrent')).default
        if (!mounted) return
        const client = new WebTorrent(); clientRef.current = client
        setStatus('connecting'); statusRef.current = 'connecting'
        api.trackEvent(fileData.id, 'peer_joined').catch(() => {})

        fallbackTimer.current = setTimeout(() => {
          if (mounted && statusRef.current === 'connecting') fallbackToDirect()
        }, 5000)

        const torrent = client.add(fileData.magnet_link)
        torrent.on('wire', () => { if (!mounted) return; setPeers(torrent.numPeers) })
        torrent.on('download', (bytes: number) => {
          if (!mounted) return
          clearTimeout(fallbackTimer.current)
          setStatus('p2p'); statusRef.current = 'p2p'
          setPeers(torrent.numPeers)
          api.trackEvent(fileData.id, 'chunk_downloaded', bytes, 0).catch(() => {})
        })
        torrent.on('ready', () => {
          if (!mounted) return
          const f = torrent.files.find((f: any) => f.name.match(/\.(mp4|webm|ogv|mov|mkv|mp3|ogg|wav)$/i)) || torrent.files[0]
          if (f && videoRef.current) { f.renderTo(videoRef.current, { autoplay: true }); setStatus('p2p'); statusRef.current = 'p2p' }
        })
        pollTimer.current = setInterval(() => { if (!mounted) return; setPeers(torrent.numPeers || peers) }, 2000)
      } catch { if (mounted) fallbackToDirect() }
    }
    init()

    return () => {
      mounted = false
      clearTimeout(fallbackTimer.current)
      clearInterval(pollTimer.current)
      if (clientRef.current) {
        try { clientRef.current.destroy() } catch {}
        clientRef.current = null
      }
      if (fileData) api.trackEvent(fileData.id, 'peer_left').catch(() => {})
    }
  }, [fileData, fallbackToDirect, fileId])

  if (loading) return <div className="flex aspect-video items-center justify-center border" style={{ borderColor: '#222', backgroundColor: '#111' }}><Loader2 size={20} className="animate-spin" style={{ color: '#555' }} /></div>
  if (error || !fileData) return <div className="flex aspect-video items-center justify-center border" style={{ borderColor: '#222', backgroundColor: '#111' }}><p className="text-sm" style={{ color: '#555' }}>{error || 'Not found'}</p></div>

  if (fileId === 'demo') {
    return (
      <div className="border" style={{ borderColor: '#222', backgroundColor: '#111' }}>
        <div className="flex aspect-video items-center justify-center">
          <div className="text-center">
            <p className="text-sm font-medium text-white">Demo Player</p>
            <p className="mt-1 text-xs" style={{ color: '#555' }}>Upload a file to see the real P2P player</p>
          </div>
        </div>
        <div className="flex items-center justify-between border-t px-4 py-2" style={{ borderColor: '#222' }}>
          <span className="font-mono text-xs" style={{ color: '#555' }}>preview mode</span>
          <span className="flex items-center gap-1 font-mono text-xs" style={{ color: '#555' }}><Users size={12} /> 0 peers</span>
        </div>
      </div>
    )
  }

  return (
    <div className="border" style={{ borderColor: '#222', backgroundColor: '#111' }}>
      <div className="relative">
        <video ref={videoRef} className="aspect-video w-full" controls playsInline crossOrigin="anonymous" />
        {status === 'connecting' && (
          <div className="absolute inset-0 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.7)' }}>
            <p className="font-mono text-sm text-white">connecting to peers...</p>
          </div>
        )}
      </div>
      <div className="flex items-center justify-between border-t px-4 py-2" style={{ borderColor: '#222' }}>
        <span className="text-sm" style={{ color: '#555' }}>{fileData.filename}</span>
        <span className="flex items-center gap-1 font-mono text-sm" style={{ color: '#555' }}>
          <span className="h-2 w-2" style={{ backgroundColor: peers > 0 ? '#fff' : '#333' }} />
          {peers} {peers === 1 ? 'peer' : 'peers'} {status === 'p2p' ? 'connected' : status === 'direct' ? '(direct)' : ''}
        </span>
      </div>
    </div>
  )
}
