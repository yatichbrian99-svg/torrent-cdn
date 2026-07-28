'use client';

import { useState } from 'react';
import { Copy, CheckCircle2, Code2 } from 'lucide-react';

interface EmbedCodeProps {
  fileId: string;
  className?: string;
}

export default function EmbedCode({ fileId, className = '' }: EmbedCodeProps) {
  const [copied, setCopied] = useState(false);
  const [showConfig, setShowConfig] = useState(false);
  const [width, setWidth] = useState('100%');
  const [height, setHeight] = useState('auto');
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');

  const appUrl = process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000';

  const getEmbedCode = () => {
    const themeColor = theme === 'dark' ? '#0f172a' : '#ffffff';
    return `<script src="${appUrl}/sdk.js"></script>
<div
  data-torrent-cdn="${fileId}"
  data-width="${width}"
  data-height="${height}"
  data-theme="${theme}"
  style="max-width: ${width}; ${height !== 'auto' ? `height: ${height};` : ''} background: ${themeColor}; border-radius: 8px; overflow: hidden;"
></div>`;
  };

  const handleCopy = () => {
    navigator.clipboard.writeText(getEmbedCode());
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className={`rounded-xl border border-slate-700/50 bg-slate-800/30 ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between border-b border-slate-700/50 px-4 py-3">
        <div className="flex items-center gap-2">
          <Code2 className="h-4 w-4 text-primary-400" />
          <h3 className="text-sm font-medium text-white">Embed Code</h3>
        </div>
        <button
          onClick={() => setShowConfig(!showConfig)}
          className="text-xs text-primary-400 hover:text-primary-300"
        >
          {showConfig ? 'Hide settings' : 'Configure'}
        </button>
      </div>

      {/* Configuration */}
      {showConfig && (
        <div className="border-b border-slate-700/50 px-4 py-3">
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-xs text-slate-400">Width</label>
              <input
                type="text"
                value={width}
                onChange={(e) => setWidth(e.target.value)}
                className="mt-1 w-full rounded border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-white"
                placeholder="100%"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400">Height</label>
              <input
                type="text"
                value={height}
                onChange={(e) => setHeight(e.target.value)}
                className="mt-1 w-full rounded border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-white"
                placeholder="auto"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400">Theme</label>
              <select
                value={theme}
                onChange={(e) => setTheme(e.target.value as 'dark' | 'light')}
                className="mt-1 w-full rounded border border-slate-600 bg-slate-700 px-2 py-1 text-xs text-white"
              >
                <option value="dark">Dark</option>
                <option value="light">Light</option>
              </select>
            </div>
          </div>
        </div>
      )}

      {/* Code block */}
      <div className="relative p-4">
        <pre className="overflow-x-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-300">
          <code>{getEmbedCode()}</code>
        </pre>
        <button
          onClick={handleCopy}
          className="absolute right-6 top-5 rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-700 hover:text-white"
        >
          {copied ? (
            <span className="flex items-center gap-1 text-xs text-green-400">
              <CheckCircle2 className="h-3.5 w-3.5" />
              Copied!
            </span>
          ) : (
            <Copy className="h-4 w-4" />
          )}
        </button>
      </div>

      {/* Info */}
      <div className="border-t border-slate-700/50 px-4 py-2">
        <p className="text-xs text-slate-500">
          Add this snippet to any HTML page to embed your content with P2P streaming.
        </p>
      </div>
    </div>
  );
}
