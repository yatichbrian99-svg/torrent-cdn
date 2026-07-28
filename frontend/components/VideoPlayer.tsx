'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { Users, Wifi, WifiOff, Loader2 } from 'lucide-react';
import api from '@/lib/api';

interface FileData {
  id: string;
  filename: string;
  minio_url: string;
  magnet_link: string;
  file_size_mb: number;
}

interface PeerStats { peers: number; source: 'p2p' | 'direct' | 'connecting'; downloaded: number; }

export default function VideoPlayer({ fileId }: { fileId: string }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [fileData, setFileData] = useState<FileData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [stats, setStats] = useState<PeerStats>({ peers: 0, source: 'connecting', downloaded: 0 });
  const clientRef = useRef<any>(null);
  const fallbackTimer = useRef<NodeJS.Timeout>();
  const pollTimer = useRef<NodeJS.Timeout>();

  useEffect(() => {
    if (fileId === 'demo') { setFileData({ id: 'demo', filename: 'Demo Video.mp4', minio_url: '', magnet_link: '', file_size_mb: 0 }); setLoading(false); return; }
    api.getPublicFile(fileId).then((data: FileData) => { setFileData(data); setLoading(false); }).catch(() => { setError('File not found'); setLoading(false); });
  }, [fileId]);

  const fallbackToDirect = useCallback(() => {
    if (!videoRef.current || !fileData?.minio_url) return;
    videoRef.current.src = fileData.minio_url;
    videoRef.current.play().catch(() => {});
    setStats((prev) => ({ ...prev, source: 'direct' }));
    api.trackEvent(fileData.id, 'chunk_downloaded', 0, Math.round(fileData.file_size_mb * 1024 * 1024)).catch(() => {});
  }, [fileData]);

  useEffect(() => {
    if (!fileData || fileId === 'demo' || !fileData.magnet_link) return;
    let mounted = true;
    const initWebTorrent = async () => {
      try {
        const WebTorrent = (await import('webtorrent')).default;
        if (!mounted) return;
        const client = new WebTorrent();
        clientRef.current = client;
        setStats({ peers: 0, source: 'connecting', downloaded: 0 });
        api.trackEvent(fileData.id, 'peer_joined').catch(() => {});
        fallbackTimer.current = setTimeout(() => { if (mounted && stats.source === 'connecting') fallbackToDirect(); }, 5000);
        const torrent = client.add(fileData.magnet_link);
        torrent.on('wire', () => { if (!mounted) return; setStats((prev) => ({ ...prev, peers: torrent.numPeers })); });
        torrent.on('download', (bytes: number) => {
          if (!mounted) return;
          clearTimeout(fallbackTimer.current);
          setStats((prev) => ({ peers: torrent.numPeers, source: 'p2p', downloaded: prev.downloaded + bytes }));
          api.trackEvent(fileData.id, 'chunk_downloaded', bytes, 0).catch(() => {});
        });
        torrent.on('ready', () => {
          if (!mounted) return;
          const videoFile = torrent.files.find((f: any) => f.name.match(/\.(mp4|webm|ogv|mov|mkv|mp3|ogg|wav)$/i)) || torrent.files[0];
          if (videoFile && videoRef.current) { videoFile.renderTo(videoRef.current, { autoplay: true }); setStats((prev) => ({ ...prev, source: 'p2p' })); }
        });
        pollTimer.current = setInterval(() => { if (!mounted) return; setStats((prev) => ({ ...prev, peers: torrent.numPeers || prev.peers })); }, 2000);
      } catch { if (mounted) fallbackToDirect(); }
    };
    initWebTorrent();
    return () => {
      mounted = false;
      clearTimeout(fallbackTimer.current);
      clearInterval(pollTimer.current);
      if (clientRef.current) clientRef.current.destroy();
      if (fileData) api.trackEvent(fileData.id, 'peer_left').catch(() => {});
    };
  }, [fileData, fallbackToDirect, fileId]);

  if (loading) return <div className="flex aspect-video items-center justify-center rounded-xl border border-surface-border bg-surface-card"><Loader2 size={24} className="animate-spin text-gray-500" /></div>;
  if (error || !fileData) return <div className="flex aspect-video items-center justify-center rounded-xl border border-surface-border bg-surface-card"><p className="text-sm text-gray-500">{error || 'File not found'}</p></div>;

  if (fileId === 'demo') {
    return (
      <div className="overflow-hidden rounded-xl border border-surface-border bg-surface-card">
        <div className="flex aspect-video items-center justify-center bg-gradient-to-br from-brand-500/5 to-transparent">
          <div className="text-center">
            <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-xl border border-surface-border bg-surface">
              <svg className="h-8 w-8 text-brand-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </div>
            <p className="text-base font-medium text-white">Demo Player</p>
            <p className="mt-1 text-sm text-gray-500">Upload a file to see the real P2P player</p>
          </div>
        </div>
        <div className="flex items-center justify-between border-t border-surface-border px-4 py-3">
          <span className="flex items-center gap-1.5 text-sm text-gray-600"><WifiOff size={14} /> Preview Mode</span>
          <span className="flex items-center gap-1.5 text-sm text-gray-600"><Users size={14} /> 0 peers</span>
        </div>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-surface-border bg-surface-card">
      <div className="relative bg-black">
        <video ref={videoRef} className="aspect-video w-full" controls playsInline crossOrigin="anonymous" />
        {stats.source === 'connecting' && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/60">
            <div className="text-center">
              <Loader2 size={24} className="mx-auto mb-2 animate-spin text-brand-400" />
              <p className="text-sm text-gray-400">Connecting to P2P network...</p>
            </div>
          </div>
        )}
      </div>
      <div className="flex items-center justify-between border-t border-surface-border px-4 py-3">
        <div className="flex items-center gap-3 text-sm">
          {stats.source === 'p2p' ? (
            <span className="flex items-center gap-1.5 text-green-500"><Wifi size={14} /> P2P</span>
          ) : stats.source === 'direct' ? (
            <span className="flex items-center gap-1.5 text-yellow-500"><WifiOff size={14} /> Direct</span>
          ) : (
            <span className="flex items-center gap-1.5 text-gray-500"><Loader2 size={14} className="animate-spin" /> Connecting</span>
          )}
          <span className="text-gray-500">{fileData.filename}</span>
        </div>
        <span className="flex items-center gap-1.5 text-sm text-gray-500"><Users size={14} /> {stats.peers} peers</span>
      </div>
    </div>
  );
}
