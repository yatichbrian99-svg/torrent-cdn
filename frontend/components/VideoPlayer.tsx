'use client';

import { useEffect, useRef, useState } from 'react';
import { Users, Wifi, WifiOff, Play, Loader2 } from 'lucide-react';
import api from '@/lib/api';

interface VideoPlayerProps {
  fileId: string;
  magnetLink: string;
  minioUrl: string;
  filename: string;
  autoPlay?: boolean;
  className?: string;
}

export default function VideoPlayer({
  fileId,
  magnetLink,
  minioUrl,
  filename,
  autoPlay = true,
  className = '',
}: VideoPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const clientRef = useRef<any>(null);
  const [status, setStatus] = useState<'connecting' | 'p2p' | 'fallback' | 'ready'>('connecting');
  const [peerCount, setPeerCount] = useState(0);
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    let mounted = true;
    let fallbackTimer: NodeJS.Timeout;

    const init = async () => {
      if (!mounted) return;

      try {
        const WebTorrent = (await import('webtorrent')).default;
        const client = new WebTorrent();
        clientRef.current = client;

        const torrent = client.add(magnetLink);

        torrent.on('infoHash', () => {
          if (!mounted) return;
        });

        torrent.on('wire', () => {
          if (!mounted) return;
          setPeerCount(torrent.numPeers);
        });

        torrent.on('download', (bytes: number) => {
          if (!mounted) return;
          setPeerCount(torrent.numPeers);
          setProgress(torrent.progress);

          if (bytes > 0 && status === 'connecting') {
            setStatus('p2p');
          }

          // Track bytes via P2P (throttled)
          api.trackEvent(fileId, 'chunk_downloaded', bytes, 0).catch(() => {});
        });

        torrent.on('ready', () => {
          if (!mounted) return;

          const videoFile = torrent.files.find((f: any) =>
            f.name.match(/\.(mp4|webm|ogv|mov|mkv|mp3|ogg|wav)$/i)
          ) || torrent.files[0];

          if (videoFile && videoRef.current) {
            videoFile.renderTo(videoRef.current, { autoplay: autoPlay });
            setStatus('p2p');
          }
        });

        // Fallback after 5 seconds
        fallbackTimer = setTimeout(() => {
          if (!mounted || status !== 'connecting') return;

          setStatus('fallback');
          if (videoRef.current) {
            videoRef.current.src = minioUrl;
            if (autoPlay) videoRef.current.play().catch(() => {});

            // Track origin bytes
            api.trackEvent(fileId, 'chunk_downloaded', 0, 1024 * 1024).catch(() => {});
          }
        }, 5000);

      } catch (err) {
        console.error('WebTorrent init failed:', err);
        if (mounted) {
          setStatus('fallback');
          if (videoRef.current) {
            videoRef.current.src = minioUrl;
            if (autoPlay) videoRef.current.play().catch(() => {});
          }
        }
      }
    };

    init();

    return () => {
      mounted = false;
      clearTimeout(fallbackTimer);
      if (clientRef.current) {
        try { clientRef.current.destroy(); } catch {}
      }
    };
  }, [fileId, magnetLink, minioUrl]);

  useEffect(() => {
    return () => {
      // Track peer left
      api.trackEvent(fileId, 'peer_left', 0, 0).catch(() => {});
    };
  }, [fileId]);

  return (
    <div className={`relative ${className}`}>
      <video
        ref={videoRef}
        className="w-full rounded-lg bg-black"
        controls
        playsInline
        crossOrigin="anonymous"
      />

      {/* Status overlay */}
      {status === 'connecting' && (
        <div className="absolute inset-0 flex items-center justify-center rounded-lg bg-black/50">
          <div className="text-center">
            <Loader2 className="mx-auto h-8 w-8 animate-spin text-primary-400" />
            <p className="mt-2 text-sm text-white">Connecting to P2P network...</p>
          </div>
        </div>
      )}

      {/* Status bar */}
      <div className="mt-2 flex items-center justify-between rounded-lg bg-slate-900 px-3 py-2">
        <div className="flex items-center gap-2">
          {status === 'p2p' ? (
            <Wifi className="h-4 w-4 text-green-400" />
          ) : status === 'fallback' ? (
            <WifiOff className="h-4 w-4 text-amber-400" />
          ) : (
            <Loader2 className="h-4 w-4 animate-spin text-primary-400" />
          )}
          <span className="text-xs text-slate-400">
            {status === 'p2p' ? 'P2P Streaming' : status === 'fallback' ? 'Direct Stream' : 'Connecting...'}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {status === 'p2p' && (
            <div className="flex items-center gap-1">
              <Users className="h-3 w-3 text-slate-500" />
              <span className="text-xs text-slate-400">{peerCount} peers</span>
            </div>
          )}
          {progress > 0 && (
            <span className="text-xs text-slate-500">{(progress * 100).toFixed(0)}%</span>
          )}
        </div>
      </div>
    </div>
  );
}
