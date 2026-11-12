'use client'

import { useEffect } from 'react'
import { readStoredTheme, systemTheme, applyTheme, type Theme } from '@/lib/theme'

/**
 * Keeps the page theme in sync with the world around it.
 *
 *  - On mount it re-applies the resolved theme (the pre-paint script already
 *    did this before first paint; re-applying here keeps `color-scheme` and the
 *    `<meta name="theme-color">` correct after hydration).
 *  - While the visitor has NOT made a choice, it listens to
 *    `prefers-color-scheme` and switches live when the OS flips — including
 *    when the OS flips while the tab is in the background.
 *  - Once a choice is stored (by the toggle) the listener stops: an explicit
 *    choice must never be overridden by the OS.
 *
 * Storage access is guarded everywhere: with storage blocked this degrades to
 * "follow the OS", which is exactly the no-choice behaviour.
 */
export default function ThemeProvider() {
  useEffect(() => {
    const media =
      typeof window.matchMedia === 'function'
        ? window.matchMedia('(prefers-color-scheme: dark)')
        : null

    const current = (): Theme => {
      const stored = readStoredTheme(window.localStorage)
      if (stored) return stored
      return systemTheme(media ? media.matches : false)
    }

    applyTheme(current())

    const onChange = (event: MediaQueryListEvent) => {
      // A stored choice wins over the OS, forever.
      if (readStoredTheme(window.localStorage)) return
      applyTheme(systemTheme(event.matches))
    }

    if (media) {
      if (typeof media.addEventListener === 'function') {
        media.addEventListener('change', onChange)
        return () => media.removeEventListener('change', onChange)
      }
      // Safari < 14.
      media.addListener(onChange)
      return () => media.removeListener(onChange)
    }
    return undefined
  }, [])

  return null
}
