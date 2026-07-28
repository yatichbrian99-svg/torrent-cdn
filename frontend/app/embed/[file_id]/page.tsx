'use client';

import { useEffect, useState, useRef } from 'react';
import { useParams } from 'next/navigation';
import { Loader2, Users, AlertTriangle, Play } from 'lucide-react';
import api from '@/lib/api';

interface FileData {
  id: string;
  filename: string;
  minio_url: string;
  magnet_link: string;
  file_size_mb: number;
}

export default function EmbedPlayer() {
  const params = useParams();
  const fileId = params.file_id as string;
  const [fileData, setFileData] = useState<FileData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [peerCount, setPeerCount] = useState(0);
  const [usingP2P, setUsingP2P] = useState(false);
  const [usingFallback, setUsingFallback] = useState(false);
  const [ready, setReady] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const webtorrentRef = useRef<any>(null);

  useEffect(() => {
    loadFile();
    return () => {
      // Cleanup WebTorrent
      if (webtorrentRef.current) {
        try {
          webtorrentRef.current.destroy();
        } catch {}
      }
    };
  }, [fileId]);

  const loadFile = async () => {
    try {
      setLoading(true);
      const res = await api.getPublicFile(fileId);
      setFileData(res.data);
      setError('');

      // Track peer joined
      await api.trackEvent(fileId, 'peer_joined');

      // Try to initialize WebTorrent
      await initWebTorrent(res.data);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'File not found');
    } finally {
      setLoading(false);
    }
  };

  const initWebTorrent = async (file: FileData) => {
    try {
      // Dynamic import of webtorrent (client-side only)
      const WebTorrent = (await import('webtorrent')).default;
      const client = new WebTorrent();
      webtorrentRef.current = client;

      // Add the magnet link
      const torrent = client.add(file.magnet_link, { path: '/tmp' });

      torrent.on('infoHash', () => {
        console.log('Torrent info hash:', torrent.infoHash);
      });

      torrent.on('wire', (wire: any) => {
        console.log('Connected to peer:', wire.peerAddress);
        setPeerCount(torrent.numPeers);
      });

      torrent.on('download', (bytes: number) => {
        setPeerCount(torrent.numPeers);
        if (bytes > 0 && !usingP2P) {
          setUsingP2P(true);
          setUsingFallback(false);
        }
        // Track P2P bytes
        api.trackEvent(fileId, 'chunk_downloaded', bytes, 0).catch(() => {});
      });

      torrent.on('ready', () => {
        const fileToStream = torrent.files.find((f: any) =>
          f.name.endsWith('.mp4') || f.name.endsWith('.webm') ||
          f.name.endsWith('.ogv') || f.name.endsWith('.mov') ||
          f.name.endsWith('.mp3') || f.name.endsWith('.ogg') ||
          f.name.endsWith('.wav') || f.name.endsWith('.m4a')
        ) || torrent.files[0];

        if (fileToStream && videoRef.current) {
          fileToStream.renderTo(videoRef.current, { autoplay: true });
          setReady(true);
        }
      });

      // Fallback to direct URL after timeout
      setTimeout(() => {
        if (!ready && videoRef.current) {
          console.log('WebTorrent timeout, falling back to direct URL');
          setUsingFallback(true);
          setUsingP2P(false);
          videoRef.current.src = file.minio_url;
          videoRef.current.play().catch(() => {});
          setReady(true);

          // Track origin bytes
          api.trackEvent(fileId, 'chunk_downloaded', 0, Math.round(file.file_size_mb * 1024 * 1024)).catch(() => {});
        }
      }, 5000);

    } catch (err) {
      console.error('WebTorrent initialization failed:', err);
      setUsingFallback(true);
      if (videoRef.current && file.minio_url) {
        videoRef.current.src = file.minio_url;
        videoRef.current.play().catch(() => {});
        setReady(true);
      }
    }
  };

  const handlePlay = () => {
    if (videoRef.current && fileData && usingFallback) {
      videoRef.current.src = fileData.minio_url;
      videoRef.current.play().catch(console.error);
    }
  };

  // Loading state
  if (loading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center bg-slate-950">
        <div className="text-center">
          <Loader2 className="mx-auto h-8 w-8 animate-spin text-primary-400" />
          <p className="mt-4 text-sm text-slate-400">Loading content...</p>
        </div>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center bg-slate-950">
        <div className="text-center">
          <AlertTriangle className="mx-auto h-12 w-12 text-red-400" />
          <h2 className="mt-4 text-lg font-semibold text-white">Content not available</h2>
          <p className="mt-2 text-sm text-slate-400">{error}</p>
        </div>
      </div>
    );
  }

  const isAudio = fileData?.filename.match(/\.(mp3|ogg|wav|m4a|aac|flac)$/i);

  return (
    <div className="min-h-screen bg-slate-950">
      {/* Player container */}
      <div className="mx-auto max-w-5xl px-4 py-8">
        {/* Video/Audio Player */}
        <div className={`relative overflow-hidden rounded-xl bg-black ${isAudio ? 'min-h-[200px]' : 'aspect-video'}`}>
          <video
            ref={videoRef}
            className="h-full w-full"
            controls
            playsInline
            crossOrigin="anonymous"
            style={isAudio ? { height: 'auto', minHeight: '100px' } : undefined}
          >
            {fileData && (
              <source src={fileData.minio_url} />
            )}
          </video>

          {/* Overlay before ready */}
          {!ready && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/60">
              <div className="text-center">
                <Loader2 className="mx-auto h-10 w-10 animate-spin text-primary-400" />
                <p className="mt-3 text-sm text-slate-300">Connecting to P2P network...</p>
                {usingFallback && (
                  <p className="mt-1 text-xs text-amber-400">Falling back to direct stream</p>
                )}
              </div>
            </div>
          )}

          {/* Play overlay for fallback */}
          {ready && usingFallback && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/40 cursor-pointer" onClick={handlePlay}>
              <div className="text-center">
                <Play className="mx-auto h-12 w-12 text-white" />
                <p className="mt-2 text-sm text-white">Click to play</p>
              </div>
            </div>
          )}
        </div>

        {/* File info bar */}
        <div className="mt-4 flex items-center justify-between rounded-lg bg-slate-900 p-4">
          <div>
            <h2 className="text-sm font-medium text-white">
              {fileData?.filename}
            </h2>
            <p className="text-xs text-slate-400">
              {fileData?.file_size_mb.toFixed(1)} MB
            </p>
          </div>
          <div className="flex items-center gap-4">
            {/* Peer count */}
            <div className="flex items-center gap-1.5">
              <Users className={`h-4 w-4 ${peerCount > 0 ? 'text-green-400' : 'text-slate-500'}`} />
              <span className={`text-xs ${peerCount > 0 ? 'text-green-400' : 'text-slate-500'}`}>
                {peerCount > 0 ? `${peerCount} peers` : 'connecting...'}
              </span>
            </div>

            {/* Source indicator */}
            <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
              usingP2P
                ? 'bg-green-500/10 text-green-400'
                : usingFallback
                ? 'bg-amber-500/10 text-amber-400'
                : 'bg-slate-700 text-slate-400'
            }`}>
              {usingP2P ? 'P2P' : usingFallback ? 'Direct' : 'Connecting'}
            </span>
          </div>
        </div>
      </div>

      {/* Track peer left on unload */}
      <script
        dangerouslySetInnerHTML={{
          __html: `
            window.addEventListener('beforeunload', function() {
              navigator.sendBeacon(
                '${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}/analytics/event',
                JSON.stringify({ file_id: '${fileId}', event_type: 'peer_left', bytes_p2p: 0, bytes_origin: 0 })
              );
            });
          `,
        }}
      />
    </div>
  );
}
