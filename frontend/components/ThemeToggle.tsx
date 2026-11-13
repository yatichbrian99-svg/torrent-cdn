'use client'

import { useEffect, useState } from 'react'
import { Moon, Sun } from 'lucide-react'
import {
  applyTheme,
  persistTheme,
  readStoredTheme,
  resolveTheme,
  toggleTheme,
  type Theme,
} from '@/lib/theme'

/**
 * Light/dark toggle.
 *
 * The icon is CSS-driven (both glyphs are in the DOM and `[data-theme]` picks
 * which one is visible), so it is correct on the very first painted frame —
 * no hydration flash, and no dependence on state that the server cannot know.
 *
 * The accessible name and `aria-pressed` are set in an effect because they
 * describe the *resolved* theme, which only exists in the browser.
 *
 * Choosing a theme stores it, which stops the OS from overriding it
 * (ThemeProvider listens for OS changes only while nothing is stored).
 */
export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>('light')

  useEffect(() => {
    const stored = readStoredTheme(window.localStorage)
    const prefersDark =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-color-scheme: dark)').matches
    setTheme(resolveTheme(window.localStorage, prefersDark) ?? stored ?? 'light')
  }, [])

  const switchTheme = () => {
    const root = document.documentElement
    // Read the live attribute rather than React state: the provider may have
    // just flipped it (OS change) and state lags a tick behind.
    const live = (root.getAttribute('data-theme') as Theme | null) ?? theme
    const next = toggleTheme(live)

    // Ease the colour change, unless the visitor asked for reduced motion.
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (!reduce) {
      root.classList.add('theme-changing')
      window.setTimeout(() => root.classList.remove('theme-changing'), 260)
    }

    applyTheme(next)
    persistTheme(next, window.localStorage)
    setTheme(next)
  }

  const label = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'

  return (
    <button
      type="button"
      onClick={switchTheme}
      className="theme-toggle"
      aria-label={label}
      aria-pressed={theme === 'dark'}
      title={theme === 'dark' ? 'Light mode' : 'Dark mode'}
    >
      <Sun className="theme-toggle-sun" size={16} aria-hidden="true" />
      <Moon className="theme-toggle-moon" size={16} aria-hidden="true" />
    </button>
  )
}
