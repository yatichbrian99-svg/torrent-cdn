'use client';

import { useState, useCallback } from 'react';
import { Upload, File, CheckCircle, AlertCircle, Loader2 } from 'lucide-react';
import api from '@/lib/api';
import EmbedCode from './EmbedCode';

interface UploadResult {
  file_id: string;
  filename: string;
  minio_url: string;
  magnet_link: string;
  file_size_mb: number;
  embed_url: string;
}

export default function UploadForm() {
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState('');
  const [dragOver, setDragOver] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  const handleUpload = useCallback(async (file: File) => {
    setUploading(true);
    setProgress(0);
    setError('');
    setResult(null);
    try {
      const data = await api.uploadFile(file, (percent) => setProgress(percent));
      setResult(data);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Upload failed.');
    } finally { setUploading(false); }
  }, []);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault(); setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) { setSelectedFile(file); handleUpload(file); }
  }, [handleUpload]);

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) { setSelectedFile(file); handleUpload(file); }
  };

  const formatSize = (bytes: number) => bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;

  if (result) {
    return (
      <div className="rounded-xl border border-surface-border bg-surface-card p-8">
        <div className="mb-4 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-green-500/10">
            <CheckCircle size={20} className="text-green-500" />
          </div>
          <div>
            <h3 className="text-base font-semibold text-white">Upload successful</h3>
            <p className="text-sm text-gray-500">{result.filename} &middot; {result.file_size_mb.toFixed(1)} MB</p>
          </div>
        </div>
        <div className="mb-6">
          <EmbedCode fileId={result.file_id} />
        </div>
        <div className="mb-6">
          <p className="mb-2 text-sm font-medium text-gray-300">Magnet Link</p>
          <div className="flex gap-2">
            <input readOnly value={result.magnet_link} className="flex-1 rounded-lg border border-surface-border bg-surface px-4 py-2.5 text-sm text-gray-400" />
            <button onClick={() => navigator.clipboard.writeText(result.magnet_link)} className="rounded-lg border border-surface-border px-4 py-2.5 text-sm text-gray-400 transition-colors hover:border-gray-700 hover:text-white">Copy</button>
          </div>
        </div>
        <button onClick={() => { setResult(null); setSelectedFile(null); setProgress(0); }} className="rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-medium text-white shadow-lg shadow-brand-600/20 transition-all hover:bg-brand-500">
          Upload Another
        </button>
      </div>
    );
  }

  return (
    <div>
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        className={`relative cursor-pointer rounded-xl border-2 border-dashed p-14 text-center transition-all ${
          dragOver ? 'border-brand-500 bg-brand-500/5' : 'border-surface-border bg-surface-card hover:border-gray-700'
        }`}
      >
        <input type="file" accept="video/*,audio/*" onChange={handleInputChange} className="absolute inset-0 cursor-pointer opacity-0" />

        {selectedFile && !uploading ? (
          <div className="flex items-center justify-center gap-4">
            <div className="flex h-12 w-12 items-center justify-center rounded-lg border border-surface-border bg-surface">
              <File size={22} className="text-brand-400" />
            </div>
            <div className="text-left">
              <p className="text-base font-medium text-white">{selectedFile.name}</p>
              <p className="text-sm text-gray-500">{formatSize(selectedFile.size)}</p>
            </div>
          </div>
        ) : (
          <div>
            <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-xl border border-surface-border bg-surface">
              <Upload size={24} className="text-gray-500" />
            </div>
            <p className="text-base text-gray-400">
              <span className="font-medium text-brand-400">Click to upload</span> or drag and drop
            </p>
            <p className="mt-1 text-sm text-gray-600">Video or audio files up to your plan limit</p>
          </div>
        )}

        {uploading && (
          <div className="mx-auto mt-6 max-w-sm">
            <div className="mb-2 flex items-center justify-between text-sm">
              <span className="text-gray-400">Uploading...</span>
              <span className="text-brand-400 font-medium">{progress}%</span>
            </div>
            <div className="h-2 rounded-full bg-surface-border">
              <div className="h-full rounded-full bg-brand-500 transition-all duration-300" style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}
      </div>

      {error && (
        <div className="mt-4 flex items-center gap-2 rounded-lg border border-red-500/20 bg-red-500/5 px-4 py-3 text-sm text-red-400">
          <AlertCircle size={16} /> {error}
        </div>
      )}
    </div>
  );
}
