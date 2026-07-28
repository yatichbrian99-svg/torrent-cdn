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
      <div className="mb-2 flex items-center justify-between">
        <p className="text-xs text-gray-500">Embed Code</p>
        <button onClick={handleCopy} className="flex items-center gap-1 text-xs text-gray-400 hover:text-white">
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="overflow-x-auto border border-[#1f1f1f] bg-[#0a0a0a] p-3 text-xs text-gray-400">
        <code>{embedHtml}</code>
      </pre>
    </div>
  );
}
