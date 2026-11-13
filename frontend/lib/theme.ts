/**
 * Theme preference handling — light/dark mode.
 *
 * Contract (redesign, 2026-11):
 *   1. No stored choice  → follow the OS (`prefers-color-scheme`), live.
 *   2. A stored choice   → that theme, and stop following the OS.
 *   3. Storage blocked   → behave exactly like "no stored choice"; the toggle
 *                          still switches for the session, it just cannot be
 *                          remembered.
 *
 * Pure logic lives here so the coverage gate (vitest counts every file under
 * lib/) keeps applying, and so the pre-paint script in layout.tsx — which
 * cannot import modules because it must run before first paint — has a single
 * source of truth to mirror. That mirror is asserted by
 * lib/__tests__/theme-parity.test.ts; change one without the other and CI
 * fails.
 *
 * Storage key is shared with the inline pre-paint script:
 *   key `tcdn_theme`, values 'light' | 'dark' — and NO KEY AT ALL means
 *   "no choice yet". (The old build stored a theme on first paint, which is
 *   why the toggle could never follow the OS.)
 */

export const THEME_KEY = 'tcdn_theme'

export type Theme = 'light' | 'dark'

/** A stored value, or null when the visitor has not chosen. */
export function readStoredTheme(
  store: Pick<Storage, 'getItem'> | null | undefined,
): Theme | null {
  try {
    const value = store?.getItem(THEME_KEY)
    return value === 'light' || value === 'dark' ? value : null
  } catch {
    // Privacy mode / blocked storage — treat as "no choice".
    return null
  }
}

/** The OS preference as a theme (`prefers-color-scheme: dark` → dark). */
export function systemTheme(prefersDark: boolean): Theme {
  return prefersDark ? 'dark' : 'light'
}

/**
 * Resolve the theme to paint: explicit choice first, then the OS, then light
 * (the light theme is the paper base; it needs no extra contrast work).
 */
export function resolveTheme(
  store: Pick<Storage, 'getItem'> | null | undefined,
  prefersDark: boolean,
): Theme {
  return readStoredTheme(store) ?? systemTheme(prefersDark)
}

/** Apply the theme to the document root and keep `color-scheme` in sync. */
export function applyTheme(theme: Theme, doc: Document | null | undefined = typeof document !== 'undefined' ? document : null): void {
  if (!doc) return
  const root = doc.documentElement
  root.setAttribute('data-theme', theme)
  // Native widgets (scrollbars, form controls, the URL bar on mobile) follow
  // color-scheme; the <meta name="theme-color"> is updated separately because
  // it is per-document metadata, not a style.
  root.style.colorScheme = theme
  const meta = doc.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
  if (meta) meta.content = theme === 'dark' ? '#000000' : '#FEFEFD'
}

/**
 * Persist a choice (never throws — storage can be unavailable). Passing null
 * clears the choice, which hands control back to the OS.
 */
export function persistTheme(
  theme: Theme | null,
  store: Storage | null | undefined,
): void {
  try {
    if (!store) return
    if (theme === null) store.removeItem(THEME_KEY)
    else store.setItem(THEME_KEY, theme)
  } catch {
    // Ignore — the toggle still works, the choice just won't survive a reload.
  }
}

/** Flip the current resolved theme and return the new one. */
export function toggleTheme(current: Theme): Theme {
  return current === 'light' ? 'dark' : 'light'
}
