'use client';

import { useState, useCallback, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Upload, FileVideo, X, CheckCircle2, Copy, ArrowLeft } from 'lucide-react';
import { useDropzone } from 'react-dropzone';
import api from '@/lib/api';

interface UploadResult {
  file_id: string;
  filename: string;
  minio_url: string;
  magnet_link: string;
  file_size_mb: number;
  embed_url: string;
}

export default function UploadPage() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    if (!api.isAuthenticated()) {
      router.push('/');
    }
  }, []);

  const onDrop = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0) {
      setFile(acceptedFiles[0]);
      setError('');
      setResult(null);
    }
  }, []);

  const { getRootProps, getInputProps, isDragActive, isDragReject } = useDropzone({
    onDrop,
    accept: {
      'video/*': ['.mp4', '.webm', '.ogv', '.mov', '.avi', '.mkv'],
      'audio/*': ['.mp3', '.ogg', '.wav', '.m4a', '.aac', '.flac'],
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
      setResult(res.data);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Upload failed. Please try again.');
    } finally {
      setUploading(false);
    }
  };

  const removeFile = () => {
    setFile(null);
    setProgress(0);
    setError('');
  };

  const copyToClipboard = (text: string, id: string) => {
    navigator.clipboard.writeText(text);
    setCopied(id);
    setTimeout(() => setCopied(null), 2000);
  };

  const embedCode = result
    ? `<script src="${process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000'}/sdk.js"></script>\n<div data-torrent-cdn="${result.file_id}"></div>`
    : '';

  return (
    <div className="mx-auto max-w-3xl px-4 py-8 sm:px-6 lg:px-8">
      {/* Back button */}
      <button
        onClick={() => router.push('/dashboard')}
        className="mb-6 inline-flex items-center gap-2 text-sm text-slate-400 transition-colors hover:text-white"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to dashboard
      </button>

      <h1 className="text-2xl font-bold text-white">Upload File</h1>
      <p className="mt-1 text-sm text-slate-400">
        Upload video or audio files to distribute via P2P CDN
      </p>

      {/* Dropzone */}
      {!result && (
        <div className="mt-8">
          <div
            {...getRootProps()}
            className={`relative cursor-pointer rounded-2xl border-2 border-dashed p-12 text-center transition-all ${
              isDragActive
                ? 'border-primary-500 bg-primary-500/10'
                : isDragReject
                ? 'border-red-500 bg-red-500/5'
                : 'border-slate-600 hover:border-slate-500 hover:bg-slate-800/30'
            } ${uploading ? 'pointer-events-none opacity-50' : ''}`}
          >
            <input {...getInputProps()} />
            <div className="flex flex-col items-center">
              {isDragActive ? (
                <>
                  <Upload className="h-12 w-12 text-primary-400" />
                  <p className="mt-4 text-lg font-medium text-primary-400">Drop your file here</p>
                </>
              ) : (
                <>
                  <Upload className="h-12 w-12 text-slate-500" />
                  <p className="mt-4 text-lg font-medium text-slate-300">
                    Drag & drop your file here
                  </p>
                  <p className="mt-1 text-sm text-slate-500">
                    or click to browse — MP4, WebM, MP3, and more
                  </p>
                </>
              )}
            </div>
          </div>

          {/* Selected file */}
          {file && (
            <div className="mt-6 rounded-xl border border-slate-700/50 bg-slate-800/30 p-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <FileVideo className="h-8 w-8 text-primary-400" />
                  <div>
                    <p className="text-sm font-medium text-white">{file.name}</p>
                    <p className="text-xs text-slate-400">{(file.size / (1024 * 1024)).toFixed(2)} MB</p>
                  </div>
                </div>
                {!uploading && (
                  <button
                    onClick={removeFile}
                    className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-700 hover:text-white"
                  >
                    <X className="h-5 w-5" />
                  </button>
                )}
              </div>

              {/* Upload progress */}
              {uploading && (
                <div className="mt-4">
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-slate-400">Uploading...</span>
                    <span className="text-white">{progress}%</span>
                  </div>
                  <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-700">
                    <div
                      className="progress-bar h-full rounded-full transition-all duration-500"
                      style={{ width: `${progress}%` }}
                    />
                  </div>
                </div>
              )}

              {/* Upload button */}
              {!uploading && (
                <button
                  onClick={handleUpload}
                  className="mt-4 w-full rounded-lg bg-gradient-to-r from-primary-600 to-purple-600 px-4 py-2.5 text-sm font-medium text-white shadow-lg transition-all hover:from-primary-500 hover:to-purple-500"
                >
                  Upload to Torrent CDN
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="mt-6 rounded-lg bg-red-500/10 p-4 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Success Result */}
      {result && (
        <div className="mt-8 space-y-6">
          <div className="rounded-xl border border-green-500/30 bg-green-500/10 p-6">
            <div className="flex items-center gap-3">
              <CheckCircle2 className="h-8 w-8 text-green-400" />
              <div>
                <h3 className="text-lg font-semibold text-green-400">Upload successful!</h3>
                <p className="text-sm text-green-300/70">{result.filename} is now live on the P2P network</p>
              </div>
            </div>
          </div>

          {/* Embed Code */}
          <div className="rounded-xl border border-slate-700/50 bg-slate-800/30 p-6">
            <h3 className="text-lg font-semibold text-white">Embed Code</h3>
            <p className="mt-1 text-sm text-slate-400">
              Add this snippet to any website to serve {result.filename} via P2P
            </p>
            <div className="mt-4 relative">
              <pre className="overflow-x-auto rounded-lg bg-slate-900 p-4 text-sm text-slate-300">
                <code>{embedCode}</code>
              </pre>
              <button
                onClick={() => copyToClipboard(embedCode, 'embed')}
                className="absolute right-2 top-2 rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-700 hover:text-white"
              >
                {copied === 'embed' ? (
                  <span className="text-xs text-green-400">Copied!</span>
                ) : (
                  <Copy className="h-4 w-4" />
                )}
              </button>
            </div>
          </div>

          {/* Magnet Link */}
          <div className="rounded-xl border border-slate-700/50 bg-slate-800/30 p-6">
            <h3 className="text-lg font-semibold text-white">Magnet Link</h3>
            <p className="mt-1 text-sm text-slate-400">
              Direct magnet link for peer-to-peer distribution
            </p>
            <div className="mt-4 relative">
              <pre className="overflow-x-auto rounded-lg bg-slate-900 p-4 text-sm text-slate-300 break-all">
                <code>{result.magnet_link}</code>
              </pre>
              <button
                onClick={() => copyToClipboard(result.magnet_link, 'magnet')}
                className="absolute right-2 top-2 rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-700 hover:text-white"
              >
                {copied === 'magnet' ? (
                  <span className="text-xs text-green-400">Copied!</span>
                ) : (
                  <Copy className="h-4 w-4" />
                )}
              </button>
            </div>
          </div>

          {/* File Info */}
          <div className="rounded-xl border border-slate-700/50 bg-slate-800/30 p-6">
            <h3 className="text-lg font-semibold text-white">File Details</h3>
            <div className="mt-4 grid grid-cols-2 gap-4">
              <div>
                <p className="text-sm text-slate-400">File ID</p>
                <p className="text-sm font-medium text-white">{result.file_id}</p>
              </div>
              <div>
                <p className="text-sm text-slate-400">Size</p>
                <p className="text-sm font-medium text-white">{result.file_size_mb.toFixed(2)} MB</p>
              </div>
              <div>
                <p className="text-sm text-slate-400">Embed URL</p>
                <a
                  href={result.embed_url}
                  target="_blank"
                  className="text-sm font-medium text-primary-400 hover:text-primary-300"
                >
                  Open player →
                </a>
              </div>
              <div>
                <p className="text-sm text-slate-400">Direct URL</p>
                <p className="text-sm font-medium text-slate-400 truncate">{result.minio_url}</p>
              </div>
            </div>
          </div>

          <div className="flex gap-4">
            <button
              onClick={() => {
                setFile(null);
                setResult(null);
                setProgress(0);
              }}
              className="flex-1 rounded-lg border border-slate-600 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700"
            >
              Upload another file
            </button>
            <button
              onClick={() => router.push('/dashboard')}
              className="flex-1 rounded-lg bg-gradient-to-r from-primary-600 to-purple-600 px-4 py-2.5 text-sm font-medium text-white shadow-lg transition-all hover:from-primary-500 hover:to-purple-500"
            >
              View dashboard
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
