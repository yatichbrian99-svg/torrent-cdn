'use client'

import { useState } from 'react'
import { Check, Copy } from 'lucide-react'

/**
 * One copy-paste block for the public TCDN-AI page.
 *
 * The copy button is the only interactive part of the snippets, so this is the
 * smallest client component that keeps the page itself server-rendered. The
 * code lives in <pre><code> so it stays selectable, screen-reader readable and
 * unaffected by the button's state.
 */
export default function CodeBlock({ code, label }: { code: string; label: string }) {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard access can be denied; the text is still selectable by hand.
      setCopied(false)
    }
  }

  return (
    <div className="border" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
      <div
        className="flex items-center justify-between gap-3 border-b px-4 py-2"
        style={{ borderColor: 'var(--border)' }}
      >
        <p className="font-mono text-[10px] font-bold uppercase tracking-[0.2em]" style={{ color: 'var(--text-muted)' }}>
          {label}
        </p>
        <button
          type="button"
          onClick={copy}
          className="inline-flex items-center gap-1 border px-2 py-1 text-[10px] font-bold uppercase tracking-widest"
          style={{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }}
          aria-label={copied ? `${label} snippet copied` : `Copy ${label} snippet`}
        >
          {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      {/* tabIndex makes a horizontally scrollable <pre> reachable by keyboard
          (axe: scrollable-region-focusable). The label names which snippet it is. */}
      <pre
        tabIndex={0}
        aria-label={`${label} snippet`}
        className="overflow-x-auto p-4 text-xs leading-relaxed focus-visible:outline focus-visible:outline-1"
        style={{ color: 'var(--text-primary)' }}
      >
        <code>{code}</code>
      </pre>
    </div>
  )
}
