'use client'

import { useState } from 'react'
import { Copy, Check } from 'lucide-react'

export default function EmbedCode({ fileId }: { fileId: string }) {
  const [copied, setCopied] = useState(false)
  const appUrl = process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000'
  const code = `<script src="${appUrl}/sdk.js"></script>\n<div data-torrent-cdn="${fileId}"></div>`

  const handleCopy = async () => {
    await navigator.clipboard.writeText(code)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="border p-3" style={{ backgroundColor: '#0c0c0c', borderColor: '#222' }}>
      <div className="mb-2 flex items-center justify-between">
        <div className="terminal-dots"><span /><span /><span /></div>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 text-xs"
          style={{ color: '#666', fontFamily: 'monospace' }}
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? 'copied' : 'copy'}
        </button>
      </div>
      <pre className="overflow-x-auto text-sm leading-relaxed" style={{ color: 'var(--success)', fontFamily: 'monospace' }}>
        <code>{code}</code>
      </pre>
    </div>
  )
}
