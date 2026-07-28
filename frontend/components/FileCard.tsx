'use client';

import { useState } from 'react';
import { Trash2, Copy, Check, ExternalLink } from 'lucide-react';
import EmbedCode from './EmbedCode';

interface FileItem {
  id: string;
  filename: string;
  minio_url: string;
  torrent_magnet: string;
  file_size_mb: number;
  created_at: string;
  views: number;
  p2p_offload_percent: number;
  bandwidth_saved_gb: number;
  active_peers: number;
}

interface FileCardProps {
  file: FileItem;
  onDelete: (id: string) => void;
}

export default function FileCard({ file, onDelete }: FileCardProps) {
  const [showEmbed, setShowEmbed] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [copied, setCopied] = useState(false);

  const offloadColor =
    file.p2p_offload_percent > 50
      ? 'bg-green-500'
      : file.p2p_offload_percent > 20
      ? 'bg-yellow-500'
      : 'bg-red-500';

  const createdDate = new Date(file.created_at).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });

  const handleCopyMagnet = async () => {
    await navigator.clipboard.writeText(file.torrent_magnet);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <>
      <div className="border border-[#1f1f1f] bg-[#111] transition-colors hover:border-gray-700">
        <div className="flex items-center justify-between p-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <h3 className="text-sm font-medium text-white truncate">
                {file.filename}
              </h3>
              {file.views > 0 && (
                <span className="shrink-0 rounded bg-accent-500/10 px-1.5 py-0.5 text-[10px] font-medium text-accent-500">
                  {file.views} views
                </span>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-gray-500">
              <span>{file.file_size_mb.toFixed(1)} MB</span>
              <span>{createdDate}</span>
              {file.active_peers > 0 && (
                <span>{file.active_peers} active peers</span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2 ml-4">
            <button
              onClick={handleCopyMagnet}
              className="flex items-center gap-1 rounded border border-[#1f1f1f] px-2.5 py-1.5 text-[11px] text-gray-500 transition-colors hover:border-gray-700 hover:text-white"
              title="Copy magnet link"
            >
              {copied ? <Check size={12} /> : <Copy size={12} />}
              {copied ? 'Copied' : 'Magnet'}
            </button>
            <button
              onClick={() => setShowEmbed(true)}
              className="rounded border border-[#1f1f1f] px-2.5 py-1.5 text-[11px] text-gray-500 transition-colors hover:border-gray-700 hover:text-white"
            >
              Embed
            </button>
            <a
              href={`/embed/${file.id}`}
              target="_blank"
              className="rounded border border-[#1f1f1f] px-2.5 py-1.5 text-[11px] text-gray-500 transition-colors hover:border-gray-700 hover:text-white"
            >
              <ExternalLink size={12} />
            </a>
            <button
              onClick={() => {
                if (window.confirm('Delete this file?')) {
                  setDeleting(true);
                  onDelete(file.id);
                }
              }}
              disabled={deleting}
              className="rounded border border-[#1f1f1f] px-2.5 py-1.5 text-[11px] text-red-500/70 transition-colors hover:border-red-500/30 hover:text-red-500 disabled:opacity-50"
            >
              <Trash2 size={12} />
            </button>
          </div>
        </div>

        {/* P2P offload bar */}
        <div className="border-t border-[#1f1f1f] px-4 py-2.5">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-[11px] text-gray-500">
              P2P Offload: {file.p2p_offload_percent.toFixed(1)}%
            </span>
            <span className="text-[11px] text-gray-500">
              {file.bandwidth_saved_gb.toFixed(3)} GB saved
            </span>
          </div>
          <div className="h-1 bg-[#1f1f1f]">
            <div
              className={`h-full transition-all duration-500 ${offloadColor}`}
              style={{ width: `${Math.min(file.p2p_offload_percent, 100)}%` }}
            />
          </div>
        </div>
      </div>

      {showEmbed && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="w-full max-w-lg border border-[#1f1f1f] bg-[#111] p-6">
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-white">Embed Code</h3>
              <button
                onClick={() => setShowEmbed(false)}
                className="text-gray-500 hover:text-white"
              >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <path
                    d="M4 4l8 8M12 4l-8 8"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                  />
                </svg>
              </button>
            </div>
            <EmbedCode fileId={file.id} />
          </div>
        </div>
      )}
    </>
  );
}
