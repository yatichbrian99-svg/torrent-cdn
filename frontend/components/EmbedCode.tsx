'use client'

import { useState } from 'react'
import { Copy, Check } from 'lucide-react'

export default function EmbedCode({ fileId }: { fileId: string }) {
  const [copied, setCopied] = useState(false)
  const appUrl = typeof window !== 'undefined'
    ? (process.env.NEXT_PUBLIC_APP_URL || window.location.origin)
    : process.env.NEXT_PUBLIC_APP_URL || 'https://www.torrentcdn.store'
  const code = `<script src="${appUrl}/sdk.js"></script>\n<div data-torrent-cdn="${fileId}"></div>`

  const handleCopy = async () => { await navigator.clipboard.writeText(code); setCopied(true); setTimeout(() => setCopied(false), 2000) }

  return (
    <div className="border p-3" style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border)' }}>
      <div className="mb-2 flex items-center justify-between">
        <div className="terminal-dots"><span /><span /><span /></div>
        <button onClick={handleCopy} className="flex items-center gap-1 text-xs" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? 'copied' : 'copy'}
        </button>
      </div>
      <pre className="overflow-x-auto text-sm leading-relaxed text-[color:var(--text-primary)]" style={{ fontFamily: 'monospace' }}>
        <code>{code}</code>
      </pre>
    </div>
  )
}
