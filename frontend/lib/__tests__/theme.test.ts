/**
 * Theme resolution contract (redesign, 2026-11).
 *
 *   1. no stored choice  → the OS preference
 *   2. stored choice     → that choice, and the OS stops mattering
 *   3. storage blocked   → no choice (so: the OS preference)
 *
 * The toggle's job — persist a choice, apply it, flip — is covered here at the
 * pure-function level; the browser-level behaviour (no flash, live OS update,
 * CSP) is proven by scripts/theme_audit.mjs and recorded in
 * docs/frontend/evidence/.
 */
import { describe, expect, it, vi } from 'vitest'
import {
  THEME_KEY,
  applyTheme,
  persistTheme,
  readStoredTheme,
  resolveTheme,
  systemTheme,
  toggleTheme,
} from '../theme'

const storeOf = (value: string | null) => ({ getItem: () => value })

describe('readStoredTheme', () => {
  it('returns the stored choice', () => {
    expect(readStoredTheme(storeOf('light'))).toBe('light')
    expect(readStoredTheme(storeOf('dark'))).toBe('dark')
  })

  it('returns null when nothing (valid) is stored', () => {
    expect(readStoredTheme(storeOf(null))).toBeNull()
    expect(readStoredTheme(storeOf(''))).toBeNull()
    expect(readStoredTheme(storeOf('nonsense'))).toBeNull()
    expect(readStoredTheme(null)).toBeNull()
    expect(readStoredTheme(undefined)).toBeNull()
  })

  it('returns null when storage throws (blocked cookies / privacy mode)', () => {
    expect(
      readStoredTheme({
        getItem: () => {
          throw new Error('SecurityError')
        },
      }),
    ).toBeNull()
  })
})

describe('resolveTheme', () => {
  it('follows the OS when no choice was made', () => {
    expect(resolveTheme(storeOf(null), true)).toBe('dark')
    expect(resolveTheme(storeOf(null), false)).toBe('light')
  })

  it('ignores the OS once a choice exists', () => {
    expect(resolveTheme(storeOf('light'), true)).toBe('light')
    expect(resolveTheme(storeOf('dark'), false)).toBe('dark')
  })

  it('falls back to the OS when storage is blocked', () => {
    const blocked = {
      getItem: () => {
        throw new Error('blocked')
      },
    }
    expect(resolveTheme(blocked, true)).toBe('dark')
    expect(resolveTheme(blocked, false)).toBe('light')
  })
})

describe('systemTheme / toggleTheme', () => {
  it('maps prefers-color-scheme to a theme', () => {
    expect(systemTheme(true)).toBe('dark')
    expect(systemTheme(false)).toBe('light')
  })

  it('flips', () => {
    expect(toggleTheme('light')).toBe('dark')
    expect(toggleTheme('dark')).toBe('light')
  })
})

describe('persistTheme', () => {
  it('writes the choice under the shared key', () => {
    const setItem = vi.fn()
    persistTheme('dark', { setItem, removeItem: vi.fn() } as unknown as Storage)
    expect(setItem).toHaveBeenCalledWith(THEME_KEY, 'dark')
  })

  it('clears the choice when passed null (back to following the OS)', () => {
    const removeItem = vi.fn()
    persistTheme(null, { setItem: vi.fn(), removeItem } as unknown as Storage)
    expect(removeItem).toHaveBeenCalledWith(THEME_KEY)
  })

  it('never throws with blocked or missing storage', () => {
    const blocked = {
      setItem: () => {
        throw new Error('QuotaExceededError')
      },
      removeItem: () => {
        throw new Error('SecurityError')
      },
    } as unknown as Storage
    expect(() => persistTheme('light', blocked)).not.toThrow()
    expect(() => persistTheme(null, blocked)).not.toThrow()
    expect(() => persistTheme('light', null)).not.toThrow()
  })
})

describe('applyTheme', () => {
  it('sets data-theme, color-scheme and the theme-color meta', () => {
    const meta = { content: '' }
    const doc = {
      documentElement: {
        setAttribute: vi.fn(),
        style: { colorScheme: '' },
      },
      querySelector: vi.fn(() => meta),
    } as unknown as Document

    applyTheme('dark', doc)
    expect(doc.documentElement.setAttribute).toHaveBeenCalledWith('data-theme', 'dark')
    expect(doc.documentElement.style.colorScheme).toBe('dark')
    expect(meta.content).toBe('#000000')

    applyTheme('light', doc)
    expect(meta.content).toBe('#FEFEFD')
  })

  it('is a no-op without a document (SSR)', () => {
    expect(() => applyTheme('dark', null)).not.toThrow()
  })
})
