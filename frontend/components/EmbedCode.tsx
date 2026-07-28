'use client';

import { useState } from 'react';
import { Copy, Check } from 'lucide-react';

export default function EmbedCode({ fileId }: { fileId: string }) {
  const [copied, setCopied] = useState(false);

  const appUrl =
    process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000';

  const embedHtml = `<script src="${appUrl}/sdk.js"></script>
<div data-torrent-cdn="${fileId}"></div>`;

  const handleCopy = async () => {
    await navigator.clipboard.writeText(embedHtml);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <p className="text-[11px] font-medium uppercase tracking-wider text-gray-600">
          Embed Code
        </p>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 text-[11px] text-accent-500 hover:text-accent-400"
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? 'Copied!' : 'Copy'}
        </button>
      </div>
      <pre className="overflow-x-auto border border-[#1f1f1f] bg-[#0a0a0a] p-3 text-xs text-gray-400">
        <code>{embedHtml}</code>
      </pre>
      <p className="mt-2 text-[11px] text-gray-600">
        Paste this code anywhere on your website to embed the P2P-powered player.
      </p>
    </div>
  );
}
