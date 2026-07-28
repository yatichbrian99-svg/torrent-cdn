'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import {
  Upload, FileVideo, Trash2, ExternalLink, Copy,
  BarChart3, HardDrive, Activity, Users, Zap,
} from 'lucide-react';
import api from '@/lib/api';

interface FileItem {
  id: string;
  filename: string;
  file_size_mb: number;
  views: number;
  p2p_offload_percent: number;
  bandwidth_saved_gb: number;
  created_at: string;
  torrent_magnet: string;
  minio_url: string;
}

interface Analytics {
  total_files: number;
  total_views: number;
  total_bandwidth_saved: number;
  avg_offload: number;
  total_storage: number;
}

export default function Dashboard() {
  const router = useRouter();
  const [files, setFiles] = useState<FileItem[]>([]);
  const [analytics, setAnalytics] = useState<Analytics>({
    total_files: 0,
    total_views: 0,
    total_bandwidth_saved: 0,
    avg_offload: 0,
    total_storage: 0,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [copiedId, setCopiedId] = useState<string | null>(null);

  useEffect(() => {
    if (!api.isAuthenticated()) {
      router.push('/');
      return;
    }
    loadFiles();
  }, []);

  const loadFiles = async () => {
    try {
      setLoading(true);
      const res = await api.listFiles();
      const fileList: FileItem[] = res.data;
      setFiles(fileList);

      const totalFiles = fileList.length;
      const totalViews = fileList.reduce((sum: number, f: FileItem) => sum + f.views, 0);
      const totalSaved = fileList.reduce((sum: number, f: FileItem) => sum + f.bandwidth_saved_gb, 0);
      const totalStorage = fileList.reduce((sum: number, f: FileItem) => sum + f.file_size_mb, 0);
      const avgOffload = totalFiles > 0
        ? fileList.reduce((sum: number, f: FileItem) => sum + f.p2p_offload_percent, 0) / totalFiles
        : 0;

      setAnalytics({
        total_files: totalFiles,
        total_views: totalViews,
        total_bandwidth_saved: totalSaved,
        avg_offload: avgOffload,
        total_storage: totalStorage,
      });
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load files');
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (fileId: string) => {
    if (!confirm('Delete this file permanently?')) return;
    try {
      await api.deleteFile(fileId);
      setFiles(files.filter((f) => f.id !== fileId));
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to delete file');
    }
  };

  const copyToClipboard = (text: string, id: string) => {
    navigator.clipboard.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const formatDate = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  };

  if (loading) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <div className="spinner h-8 w-8" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Dashboard</h1>
          <p className="mt-1 text-sm text-slate-400">Manage your content and track P2P performance</p>
        </div>
        <button
          onClick={() => router.push('/dashboard/upload')}
          className="inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary-600 to-purple-600 px-4 py-2.5 text-sm font-medium text-white shadow-lg transition-all hover:from-primary-500 hover:to-purple-500"
        >
          <Upload className="h-4 w-4" />
          Upload file
        </button>
      </div>

      {/* Error state */}
      {error && (
        <div className="mt-6 rounded-lg bg-red-500/10 p-4 text-sm text-red-400">
          {error}
          <button onClick={() => setError('')} className="ml-2 underline">Dismiss</button>
        </div>
      )}

      {/* Stats Cards */}
      <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-xl border border-slate-700/50 bg-slate-800/30 p-5 backdrop-blur-xl">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-blue-500/10">
              <HardDrive className="h-5 w-5 text-blue-400" />
            </div>
            <div>
              <p className="text-sm text-slate-400">Storage Used</p>
              <p className="text-xl font-bold text-white">{analytics.total_storage.toFixed(1)} MB</p>
            </div>
          </div>
        </div>

        <div className="rounded-xl border border-slate-700/50 bg-slate-800/30 p-5 backdrop-blur-xl">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-green-500/10">
              <Activity className="h-5 w-5 text-green-400" />
            </div>
            <div>
              <p className="text-sm text-slate-400">Total Views</p>
              <p className="text-xl font-bold text-white">{analytics.total_views.toLocaleString()}</p>
            </div>
          </div>
        </div>

        <div className="rounded-xl border border-slate-700/50 bg-slate-800/30 p-5 backdrop-blur-xl">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-purple-500/10">
              <Zap className="h-5 w-5 text-purple-400" />
            </div>
            <div>
              <p className="text-sm text-slate-400">Bandwidth Saved</p>
              <p className="text-xl font-bold text-white">{analytics.total_bandwidth_saved.toFixed(3)} GB</p>
            </div>
          </div>
        </div>

        <div className="rounded-xl border border-slate-700/50 bg-slate-800/30 p-5 backdrop-blur-xl">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-orange-500/10">
              <BarChart3 className="h-5 w-5 text-orange-400" />
            </div>
            <div>
              <p className="text-sm text-slate-400">Avg. P2P Offload</p>
              <p className="text-xl font-bold text-white">{analytics.avg_offload.toFixed(1)}%</p>
            </div>
          </div>
        </div>
      </div>

      {/* Files Table */}
      <div className="mt-8">
        <h2 className="text-lg font-semibold text-white">Your Files</h2>

        {files.length === 0 ? (
          <div className="mt-6 flex flex-col items-center justify-center rounded-xl border-2 border-dashed border-slate-700 py-16">
            <FileVideo className="h-12 w-12 text-slate-600" />
            <h3 className="mt-4 text-lg font-medium text-slate-400">No files yet</h3>
            <p className="mt-1 text-sm text-slate-500">Upload your first video or audio file to get started</p>
            <button
              onClick={() => router.push('/dashboard/upload')}
              className="mt-4 inline-flex items-center gap-2 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-primary-700"
            >
              <Upload className="h-4 w-4" />
              Upload now
            </button>
          </div>
        ) : (
          <div className="mt-4 overflow-hidden rounded-xl border border-slate-700/50">
            <table className="min-w-full divide-y divide-slate-700/50">
              <thead className="bg-slate-800/50">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-slate-400">File</th>
                  <th className="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-slate-400">Size</th>
                  <th className="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-slate-400">Views</th>
                  <th className="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-slate-400">P2P Offload</th>
                  <th className="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-slate-400">Saved</th>
                  <th className="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-slate-400">Date</th>
                  <th className="px-6 py-3 text-right text-xs font-medium uppercase tracking-wider text-slate-400">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/50">
                {files.map((file) => (
                  <tr key={file.id} className="transition-colors hover:bg-slate-800/30">
                    <td className="whitespace-nowrap px-6 py-4">
                      <div className="flex items-center gap-3">
                        <FileVideo className="h-5 w-5 text-slate-500" />
                        <div>
                          <p className="text-sm font-medium text-white">{file.filename}</p>
                          <p className="text-xs text-slate-500">{file.id.slice(0, 8)}...</p>
                        </div>
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-sm text-slate-300">
                      {file.file_size_mb.toFixed(1)} MB
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-sm text-slate-300">
                      {file.views}
                    </td>
                    <td className="whitespace-nowrap px-6 py-4">
                      <div className="flex items-center gap-2">
                        <div className="h-2 w-16 overflow-hidden rounded-full bg-slate-700">
                          <div
                            className="h-full rounded-full bg-gradient-to-r from-green-500 to-green-400"
                            style={{ width: `${Math.min(file.p2p_offload_percent, 100)}%` }}
                          />
                        </div>
                        <span className="text-sm text-slate-300">{file.p2p_offload_percent.toFixed(1)}%</span>
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-sm text-green-400">
                      {file.bandwidth_saved_gb.toFixed(3)} GB
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-sm text-slate-400">
                      {formatDate(file.created_at)}
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => copyToClipboard(file.torrent_magnet, `magnet-${file.id}`)}
                          className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-700 hover:text-white"
                          title="Copy magnet link"
                        >
                          {copiedId === `magnet-${file.id}` ? (
                            <span className="text-xs text-green-400">Copied!</span>
                          ) : (
                            <Copy className="h-4 w-4" />
                          )}
                        </button>
                        <a
                          href={`/embed/${file.id}`}
                          target="_blank"
                          className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-700 hover:text-white"
                          title="Open embed player"
                        >
                          <ExternalLink className="h-4 w-4" />
                        </a>
                        <button
                          onClick={() => handleDelete(file.id)}
                          className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-red-500/20 hover:text-red-400"
                          title="Delete file"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
