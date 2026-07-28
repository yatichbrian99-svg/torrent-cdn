'use client';

import { useState } from 'react';
import { Copy, Check } from 'lucide-react';

export default function EmbedCode({ fileId }: { fileId: string }) {
  const [copied, setCopied] = useState(false);
  const appUrl = process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000';
  const embedHtml = `<script src="${appUrl}/sdk.js"></script>\n<div data-torrent-cdn="${fileId}"></div>`;

  const handleCopy = async () => {
    await navigator.clipboard.writeText(embedHtml);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm font-medium text-gray-300">Embed Code</p>
        <button onClick={handleCopy} className="flex items-center gap-1.5 text-sm text-brand-400 transition-colors hover:text-brand-300">
          {copied ? <Check size={16} /> : <Copy size={16} />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="overflow-x-auto rounded-lg border border-surface-border bg-surface p-4 text-sm text-gray-400">
        <code>{embedHtml}</code>
      </pre>
    </div>
  );
}
