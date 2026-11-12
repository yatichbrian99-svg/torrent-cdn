'use client'

import { useEffect, useRef, useState } from 'react'
import { Check, Copy } from 'lucide-react'

/**
 * Copy-to-clipboard for the embed snippet.
 *
 * `navigator.clipboard` needs a secure context and can reject (permissions,
 * old browsers), so there is a plain `document.execCommand('copy')` fallback
 * over a temporary textarea. The label swap is announced through an
 * `aria-live` region so screen-reader users hear the result.
 */
export default function CopyButton({
  value,
  label = 'Copy',
  copiedLabel = 'Copied',
  className = '',
}: {
  value: string
  label?: string
  copiedLabel?: string
  className?: string
}) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | null>(null)

  useEffect(() => () => {
    if (timer.current) window.clearTimeout(timer.current)
  }, [])

  const copy = async () => {
    let ok = false
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value)
        ok = true
      }
    } catch {
      ok = false
    }
    if (!ok) {
      try {
        const area = document.createElement('textarea')
        area.value = value
        area.setAttribute('readonly', '')
        area.style.position = 'fixed'
        area.style.top = '-1000px'
        document.body.appendChild(area)
        area.select()
        ok = document.execCommand('copy')
        document.body.removeChild(area)
      } catch {
        ok = false
      }
    }
    if (!ok) return
    setCopied(true)
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setCopied(false), 1800)
  }

  return (
    <button
      type="button"
      onClick={copy}
      className={`copy-btn ${className}`.trim()}
      aria-label={label}
    >
      {copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
      <span aria-live="polite">{copied ? copiedLabel : label}</span>
    </button>
  )
}
