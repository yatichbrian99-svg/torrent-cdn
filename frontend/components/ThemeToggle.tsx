'use client'

import { useEffect, useState } from 'react'
import { Moon, Sun } from 'lucide-react'
import { applyTheme, persistTheme, readStoredTheme, toggleTheme, type Theme } from '@/lib/theme'

/**
 * Light/dark toggle for the navbar.
 *
 * Renders the dark icon on the server and during the first client render
 * (theme comes from localStorage, which the server cannot read), then syncs to
 * the real persisted theme in an effect. The pre-paint script in layout.tsx
 * has already set `<html data-theme>` before this hydrates, so the PAGE never
 * flashes — only this tiny icon settles within the first effect tick.
 */
export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>('dark')

  useEffect(() => {
    setTheme(readStoredTheme(window.localStorage))
  }, [])

  const switchTheme = () => {
    const next = toggleTheme(theme)
    setTheme(next)
    applyTheme(next)
    persistTheme(next, window.localStorage)
  }

  return (
    <button
      onClick={switchTheme}
      className="p-1 transition-opacity hover:opacity-80"
      style={{ color: 'var(--text-muted)' }}
      aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
      title={theme === 'dark' ? 'Light mode' : 'Dark mode'}
    >
      {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  )
}