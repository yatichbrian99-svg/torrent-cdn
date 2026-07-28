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
    } finally {
      setUploading(false);
    }
  }, []);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) { setSelectedFile(file); handleUpload(file); }
  }, [handleUpload]);

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) { setSelectedFile(file); handleUpload(file); }
  };

  const formatSize = (bytes: number) => {
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  if (result) {
    return (
      <div className="border border-gray-800 bg-[#111] p-6">
        <div className="mb-4 flex items-center gap-2">
          <CheckCircle size={18} className="text-green-500" />
          <span className="text-sm font-medium text-white">Upload successful!</span>
        </div>
        <div className="mb-4 space-y-1 text-sm text-gray-400">
          <p><span className="text-gray-600">File:</span> {result.filename}</p>
          <p><span className="text-gray-600">Size:</span> {result.file_size_mb.toFixed(1)} MB</p>
        </div>
        <div className="mb-4">
          <EmbedCode fileId={result.file_id} />
        </div>
        <div className="mb-6">
          <p className="mb-1 text-xs text-gray-500">Magnet Link</p>
          <div className="flex gap-2">
            <input readOnly value={result.magnet_link} className="flex-1 border border-[#1f1f1f] bg-[#0a0a0a] px-3 py-2 text-xs text-gray-400" />
            <button onClick={() => navigator.clipboard.writeText(result.magnet_link)} className="rounded border border-[#1f1f1f] px-3 py-2 text-xs text-gray-400 hover:border-gray-700 hover:text-white">
              Copy
            </button>
          </div>
        </div>
        <button
          onClick={() => { setResult(null); setSelectedFile(null); setProgress(0); }}
          className="rounded border border-gray-700 bg-gray-800 px-4 py-2 text-xs text-white hover:bg-gray-700"
        >
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
        className={`relative cursor-pointer border border-dashed p-12 text-center transition-colors ${
          dragOver ? 'border-gray-500 bg-gray-800' : 'border-[#1f1f1f] bg-[#111] hover:border-gray-700'
        }`}
      >
        <input type="file" accept="video/*,audio/*" onChange={handleInputChange} className="absolute inset-0 cursor-pointer opacity-0" />

        {selectedFile && !uploading ? (
          <div className="flex items-center justify-center gap-3">
            <File size={20} className="text-gray-500" />
            <div className="text-left">
              <p className="text-sm font-medium text-white">{selectedFile.name}</p>
              <p className="text-xs text-gray-500">{formatSize(selectedFile.size)}</p>
            </div>
          </div>
        ) : (
          <div>
            <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center border border-[#1f1f1f] bg-[#0a0a0a]">
              <Upload size={20} className="text-gray-500" />
            </div>
            <p className="text-sm text-gray-400">Click to upload or drag and drop</p>
            <p className="mt-1 text-xs text-gray-600">Video or audio files</p>
          </div>
        )}

        {uploading && (
          <div className="mt-4">
            <div className="mb-2 flex items-center justify-center gap-2 text-sm text-gray-400">
              <Loader2 size={14} className="animate-spin" />
              {progress}%
            </div>
            <div className="mx-auto h-1.5 w-full max-w-xs bg-[#1f1f1f]">
              <div className="h-full bg-gray-600 transition-all duration-300" style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}
      </div>

      {error && (
        <div className="mt-3 flex items-center gap-2 text-xs text-red-500">
          <AlertCircle size={13} />
          {error}
        </div>
      )}
    </div>
  );
}
