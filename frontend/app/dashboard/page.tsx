'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  Upload,
  FileText,
  TrendingUp,
  HardDrive,
  Loader2,
  AlertCircle,
} from 'lucide-react';
import { isLoggedIn } from '@/lib/auth';
import api from '@/lib/api';
import FileCard from '@/components/FileCard';

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

export default function DashboardPage() {
  const router = useRouter();
  const [files, setFiles] = useState<FileItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!isLoggedIn()) {
      router.push('/');
      return;
    }

    api
      .listFiles()
      .then(setFiles)
      .catch((err) => setError(err.response?.data?.detail || 'Failed to load files'))
      .finally(() => setLoading(false));
  }, [router]);

  const handleDelete = async (id: string) => {
    try {
      await api.deleteFile(id);
      setFiles((prev) => prev.filter((f) => f.id !== id));
    } catch {
      // Swallow
    }
  };

  // Stats
  const totalFiles = files.length;
  const totalBandwidthSaved = files.reduce(
    (sum, f) => sum + f.bandwidth_saved_gb,
    0
  );
  const avgP2pOffload =
    totalFiles > 0
      ? files.reduce((sum, f) => sum + f.p2p_offload_percent, 0) / totalFiles
      : 0;

  if (loading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <Loader2 size={20} className="animate-spin text-gray-500" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
      {/* Header */}
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-white">Dashboard</h1>
          <p className="mt-0.5 text-xs text-gray-500">
            Manage your files and view P2P analytics
          </p>
        </div>
        <a
          href="/dashboard/upload"
          className="inline-flex items-center gap-2 border border-accent-500 bg-accent-500/10 px-4 py-2 text-xs font-medium text-accent-500 transition-colors hover:bg-accent-500/20"
        >
          <Upload size={14} />
          Upload New File
        </a>
      </div>

      {/* Error */}
      {error && (
        <div className="mb-6 flex items-center gap-2 border border-red-500/20 bg-red-500/5 px-4 py-3 text-xs text-red-500">
          <AlertCircle size={14} />
          {error}
        </div>
      )}

      {/* Summary Cards */}
      <div className="mb-8 grid gap-3 sm:grid-cols-3">
        <div className="border border-[#1f1f1f] bg-[#111] p-4">
          <div className="mb-1 flex items-center gap-2">
            <FileText size={14} className="text-accent-500" />
            <span className="text-[11px] font-medium uppercase tracking-wider text-gray-500">
              Total Files
            </span>
          </div>
          <p className="text-xl font-bold text-white">{totalFiles}</p>
        </div>
        <div className="border border-[#1f1f1f] bg-[#111] p-4">
          <div className="mb-1 flex items-center gap-2">
            <TrendingUp size={14} className="text-green-500" />
            <span className="text-[11px] font-medium uppercase tracking-wider text-gray-500">
              Bandwidth Saved
            </span>
          </div>
          <p className="text-xl font-bold text-white">
            {totalBandwidthSaved.toFixed(2)}{' '}
            <span className="text-xs font-normal text-gray-500">GB</span>
          </p>
        </div>
        <div className="border border-[#1f1f1f] bg-[#111] p-4">
          <div className="mb-1 flex items-center gap-2">
            <HardDrive size={14} className="text-yellow-500" />
            <span className="text-[11px] font-medium uppercase tracking-wider text-gray-500">
              Avg P2P Offload
            </span>
          </div>
          <p className="text-xl font-bold text-white">
            {avgP2pOffload.toFixed(1)}
            <span className="text-xs font-normal text-gray-500">%</span>
          </p>
        </div>
      </div>

      {/* File List */}
      {files.length === 0 ? (
        <div className="border border-[#1f1f1f] bg-[#111] p-12 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center border border-[#1f1f1f] bg-[#0a0a0a]">
            <Upload size={20} className="text-gray-500" />
          </div>
          <p className="text-sm text-gray-400">No files uploaded yet</p>
          <p className="mt-1 text-xs text-gray-600">
            Upload your first file to get started
          </p>
          <a
            href="/dashboard/upload"
            className="mt-4 inline-flex items-center gap-2 border border-accent-500 bg-accent-500/10 px-4 py-2 text-xs font-medium text-accent-500 transition-colors hover:bg-accent-500/20"
          >
            <Upload size={14} />
            Upload File
          </a>
        </div>
      ) : (
        <div className="space-y-2">
          {files.map((file) => (
            <FileCard key={file.id} file={file} onDelete={handleDelete} />
          ))}
        </div>
      )}
    </div>
  );
}
