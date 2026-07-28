'use client';

import { useState, useCallback } from 'react';
import { Upload, FileVideo, X, CheckCircle2, Loader2 } from 'lucide-react';
import { useDropzone } from 'react-dropzone';
import api from '@/lib/api';

interface UploadFormProps {
  onSuccess?: (result: { file_id: string; filename: string; magnet_link: string; embed_url: string }) => void;
  maxSizeMB?: number;
}

export default function UploadForm({ onSuccess, maxSizeMB = 1024 }: UploadFormProps) {
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState(false);

  const onDrop = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length === 0) return;

    const selectedFile = acceptedFiles[0];
    const fileSizeMB = selectedFile.size / (1024 * 1024);

    if (fileSizeMB > maxSizeMB) {
      setError(`File exceeds maximum size of ${maxSizeMB}MB`);
      return;
    }

    setFile(selectedFile);
    setError('');
    setSuccess(false);
  }, [maxSizeMB]);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      'video/*': ['.mp4', '.webm', '.ogv', '.mov', '.avi', '.mkv'],
      'audio/*': ['.mp3', '.ogg', '.wav', '.m4a', '.aac'],
    },
    maxFiles: 1,
    disabled: uploading,
  });

  const handleUpload = async () => {
    if (!file) return;

    setUploading(true);
    setError('');
    setProgress(0);

    try {
      const res = await api.uploadFile(file, (percent) => {
        setProgress(percent);
      });

      setSuccess(true);
      onSuccess?.({
        file_id: res.data.file_id,
        filename: res.data.filename,
        magnet_link: res.data.magnet_link,
        embed_url: res.data.embed_url,
      });
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  const reset = () => {
    setFile(null);
    setProgress(0);
    setError('');
    setSuccess(false);
  };

  return (
    <div className="w-full">
      {!file && !success && (
        <div
          {...getRootProps()}
          className={`relative cursor-pointer rounded-xl border-2 border-dashed p-8 text-center transition-all ${
            isDragActive
              ? 'border-primary-500 bg-primary-500/10'
              : 'border-slate-600 hover:border-slate-500 hover:bg-slate-800/30'
          }`}
        >
          <input {...getInputProps()} />
          <Upload className={`mx-auto h-10 w-10 ${isDragActive ? 'text-primary-400' : 'text-slate-500'}`} />
          <p className={`mt-3 text-sm font-medium ${isDragActive ? 'text-primary-400' : 'text-slate-300'}`}>
            {isDragActive ? 'Drop your file here' : 'Drag & drop or click to upload'}
          </p>
          <p className="mt-1 text-xs text-slate-500">Video and audio files up to {maxSizeMB}MB</p>
        </div>
      )}

      {file && !success && (
        <div className="rounded-xl border border-slate-700/50 bg-slate-800/30 p-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <FileVideo className="h-8 w-8 text-primary-400" />
              <div>
                <p className="text-sm font-medium text-white">{file.name}</p>
                <p className="text-xs text-slate-400">{(file.size / (1024 * 1024)).toFixed(2)} MB</p>
              </div>
            </div>
            {!uploading && (
              <button onClick={reset} className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-700">
                <X className="h-4 w-4" />
              </button>
            )}
          </div>

          {uploading && (
            <div className="mt-4">
              <div className="flex items-center justify-between text-sm">
                <span className="text-slate-400">Uploading...</span>
                <span className="text-white">{progress}%</span>
              </div>
              <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-700">
                <div
                  className="progress-bar h-full rounded-full transition-all"
                  style={{ width: `${progress}%` }}
                />
              </div>
            </div>
          )}

          {!uploading && (
            <button
              onClick={handleUpload}
              className="mt-4 w-full rounded-lg bg-gradient-to-r from-primary-600 to-purple-600 px-4 py-2 text-sm font-medium text-white transition-all hover:from-primary-500 hover:to-purple-500"
            >
              Upload to Torrent CDN
            </button>
          )}
        </div>
      )}

      {success && (
        <div className="flex items-center gap-3 rounded-xl border border-green-500/30 bg-green-500/10 p-4">
          <CheckCircle2 className="h-6 w-6 text-green-400" />
          <div>
            <p className="text-sm font-medium text-green-400">Upload complete!</p>
            <button onClick={reset} className="text-xs text-green-400/70 underline hover:text-green-400">
              Upload another file
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="mt-4 flex items-center justify-between rounded-lg bg-red-500/10 p-3 text-sm text-red-400">
          <span>{error}</span>
          <button onClick={() => setError('')} className="ml-2 underline">Dismiss</button>
        </div>
      )}
    </div>
  );
}
