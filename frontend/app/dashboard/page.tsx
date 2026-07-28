'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Upload, FileText, TrendingUp, HardDrive, Loader2, AlertCircle } from 'lucide-react';
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
    if (!isLoggedIn()) { router.push('/'); return; }
    api.listFiles().then(setFiles).catch((err) => setError(err.response?.data?.detail || 'Failed to load files')).finally(() => setLoading(false));
  }, [router]);

  const handleDelete = async (id: string) => {
    try { await api.deleteFile(id); setFiles((prev) => prev.filter((f) => f.id !== id)); } catch {}
  };

  const totalFiles = files.length;
  const totalBandwidthSaved = files.reduce((sum, f) => sum + f.bandwidth_saved_gb, 0);
  const avgP2pOffload = totalFiles > 0 ? files.reduce((sum, f) => sum + f.p2p_offload_percent, 0) / totalFiles : 0;

  if (loading) return <div className="flex min-h-[60vh] items-center justify-center"><Loader2 size={24} className="animate-spin text-gray-500" /></div>;

  return (
    <div className="mx-auto max-w-5xl px-6 py-10 lg:px-8">
      <div className="mb-10 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Dashboard</h1>
          <p className="mt-1 text-sm text-gray-500">Manage your files and view P2P analytics</p>
        </div>
        <a href="/dashboard/upload" className="inline-flex items-center gap-2 rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-medium text-white shadow-lg shadow-brand-600/20 transition-all hover:bg-brand-500">
          <Upload size={16} /> Upload New File
        </a>
      </div>

      {error && <div className="mb-6 flex items-center gap-2 rounded-lg border border-red-500/20 bg-red-500/5 px-4 py-3 text-sm text-red-400"><AlertCircle size={16} /> {error}</div>}

      <div className="mb-10 grid gap-4 sm:grid-cols-3">
        {[
          { icon: FileText, label: 'Total Files', value: totalFiles, color: 'text-brand-400' },
          { icon: TrendingUp, label: 'Bandwidth Saved', value: `${totalBandwidthSaved.toFixed(2)} GB`, color: 'text-green-500' },
          { icon: HardDrive, label: 'Avg P2P Offload', value: `${avgP2pOffload.toFixed(1)}%`, color: 'text-yellow-500' },
        ].map((stat) => (
          <div key={stat.label} className="rounded-xl border border-surface-border bg-surface-card p-5">
            <div className="mb-2 flex items-center gap-2">
              <stat.icon size={16} className={stat.color} />
              <span className="text-sm text-gray-500">{stat.label}</span>
            </div>
            <p className="text-2xl font-bold text-white">{stat.value}</p>
          </div>
        ))}
      </div>

      {files.length === 0 ? (
        <div className="rounded-xl border border-surface-border bg-surface-card p-16 text-center">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-xl border border-surface-border bg-surface">
            <Upload size={24} className="text-gray-500" />
          </div>
          <p className="text-base text-gray-400">No files uploaded yet</p>
          <p className="mt-1 text-sm text-gray-600">Upload your first file to get started</p>
          <a href="/dashboard/upload" className="mt-6 inline-flex items-center gap-2 rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-medium text-white shadow-lg shadow-brand-600/20 transition-all hover:bg-brand-500">
            <Upload size={16} /> Upload File
          </a>
        </div>
      ) : (
        <div className="space-y-3">
          {files.map((file) => <FileCard key={file.id} file={file} onDelete={handleDelete} />)}
        </div>
      )}
    </div>
  );
}
