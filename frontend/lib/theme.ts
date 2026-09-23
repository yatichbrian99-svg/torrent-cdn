/**
 * Theme preference handling — light/dark mode.
 *
 * Pure logic lives here so the coverage gate (vitest counts every file under
 * lib/) keeps applying. The DOM effects (setting data-theme, persisting to
 * localStorage) are split out so tests can verify the decision logic without
 * a real document, and the pre-paint script in layout.tsx can inline its own
 * copy (it must run before first paint, so it cannot import modules).
 *
 * Storage key is shared with the inline pre-paint script — keep in sync:
 *   STORable key = 'tcdn_theme', values 'light' | 'dark' (anything else → dark).
 */

export const THEME_KEY = 'tcdn_theme'

export type Theme = 'light' | 'dark'

/** Read the persisted theme from a localStorage-like store. Defaults dark. */
export function readStoredTheme(store: Pick<Storage, 'getItem'> | null | undefined): Theme {
  try {
    return store?.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'
  } catch {
    // Privacy mode / blocked storage — fall back to the dark default.
    return 'dark'
  }
}

/** Apply the theme to the document root (`<html data-theme="...">`). */
export function applyTheme(theme: Theme): void {
  if (typeof document !== 'undefined') {
    document.documentElement.setAttribute('data-theme', theme)
  }
}

/** Persist the choice (never throws — storage can be unavailable). */
export function persistTheme(theme: Theme, store: Storage | null | undefined): void {
  try {
    store?.setItem(THEME_KEY, theme)
  } catch {
    // Ignore — the toggle still works, the choice just won't survive reload.
  }
}

/** Flip the current resolved theme and return the new one. */
export function toggleTheme(current: Theme): Theme {
  return current === 'light' ? 'dark' : 'light'
}
