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

export default function FileCard({ file, onDelete }: { file: FileItem; onDelete: (id: string) => void }) {
  const [showEmbed, setShowEmbed] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [copied, setCopied] = useState(false);
  const createdDate = new Date(file.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

  const handleCopyMagnet = async () => {
    await navigator.clipboard.writeText(file.torrent_magnet);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <>
      <div className="rounded-xl border border-surface-border bg-surface-card transition-colors hover:border-gray-700">
        <div className="flex items-center justify-between p-5">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-3">
              <h3 className="truncate text-sm font-medium text-white">{file.filename}</h3>
              <span className="shrink-0 rounded-md bg-brand-500/10 px-2 py-0.5 text-xs font-medium text-brand-400">{file.views} views</span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-5 gap-y-1 text-sm text-gray-500">
              <span>{file.file_size_mb.toFixed(1)} MB</span>
              <span>{createdDate}</span>
              {file.active_peers > 0 && <span>{file.active_peers} active peers</span>}
            </div>
          </div>

          <div className="ml-4 flex items-center gap-2">
            <button onClick={handleCopyMagnet} className="rounded-lg border border-surface-border px-3 py-1.5 text-sm text-gray-500 transition-colors hover:border-gray-700 hover:text-white">
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </button>
            <button onClick={() => setShowEmbed(true)} className="rounded-lg border border-surface-border px-3 py-1.5 text-sm text-gray-500 transition-colors hover:border-gray-700 hover:text-white">Embed</button>
            <a href={`/embed/${file.id}`} target="_blank" className="rounded-lg border border-surface-border px-3 py-1.5 text-sm text-gray-500 transition-colors hover:border-gray-700 hover:text-white"><ExternalLink size={14} /></a>
            <button
              onClick={() => { if (window.confirm('Delete this file?')) { setDeleting(true); onDelete(file.id); } }}
              disabled={deleting}
              className="rounded-lg border border-surface-border px-3 py-1.5 text-sm text-red-400 transition-colors hover:border-red-500/30 hover:text-red-300 disabled:opacity-50"
            ><Trash2 size={14} /></button>
          </div>
        </div>

        <div className="border-t border-surface-border px-5 py-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-gray-500">P2P offload: <span className="text-white">{file.p2p_offload_percent.toFixed(1)}%</span></span>
            <span className="text-sm text-gray-500">{file.bandwidth_saved_gb.toFixed(3)} GB saved</span>
          </div>
          <div className="mt-2 h-2 rounded-full bg-surface-border">
            <div
              className={`h-full rounded-full transition-all duration-500 ${file.p2p_offload_percent > 50 ? 'bg-brand-500' : file.p2p_offload_percent > 20 ? 'bg-yellow-500' : 'bg-red-500'}`}
              style={{ width: `${Math.min(file.p2p_offload_percent, 100)}%` }}
            />
          </div>
        </div>
      </div>

      {showEmbed && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
          <div className="w-full max-w-lg rounded-xl border border-surface-border bg-surface-card p-8 shadow-2xl">
            <div className="mb-6 flex items-center justify-between">
              <h3 className="text-lg font-semibold text-white">Embed Code</h3>
              <button onClick={() => setShowEmbed(false)} className="text-gray-500 transition-colors hover:text-white">
                <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M4 4l10 10M14 4l-10 10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
              </button>
            </div>
            <EmbedCode fileId={file.id} />
          </div>
        </div>
      )}
    </>
  );
}
