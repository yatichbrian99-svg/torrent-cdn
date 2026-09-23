/**
 * Guards the inline pre-paint theme script in app/layout.tsx.
 *
 * That script must run before first paint (no theme flash), which means it
 * cannot import lib/theme.ts — so it duplicates the storage key + fallback
 * logic as a literal string. This test keeps the two in lockstep: change the
 * key or the resolution rule in lib/theme.ts without updating layout.tsx (or
 * vice versa) and CI fails.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { THEME_KEY, readStoredTheme } from '../theme'

const layoutSource = readFileSync(join(__dirname, '..', '..', 'app', 'layout.tsx'), 'utf8')

/** Pull the inline script text out of layout.tsx. */
function extractInitScript(): string {
  const match = layoutSource.match(/\(function\(\)\{try\{[\s\S]*?\}\)\(\);/)
  expect(match, 'pre-paint theme script not found in app/layout.tsx').toBeTruthy()
  return match![0]
}

describe('pre-paint theme script parity', () => {
  it('uses the same storage key as lib/theme.ts', () => {
    expect(extractInitScript()).toContain(`localStorage.getItem('${THEME_KEY}')`)
  })

  it('resolves unknown/absent values to dark, matching readStoredTheme', () => {
    // readStoredTheme contract: anything except stored 'light' is dark.
    expect(readStoredTheme(null)).toBe('dark')
    expect(readStoredTheme({ getItem: () => 'light' })).toBe('light')
    expect(readStoredTheme({ getItem: () => 'dark' })).toBe('dark')
    expect(readStoredTheme({ getItem: () => 'nonsense' })).toBe('dark')
    expect(readStoredTheme({ getItem: () => '' })).toBe('dark')
    // And the inline script encodes exactly that rule: light iff stored==='light'.
    const script = extractInitScript()
    expect(script).toContain(`==='light'?'light':'dark'`)
  })

  it('sets data-theme on documentElement before resolving', () => {
    const script = extractInitScript()
    expect(script).toContain(`document.documentElement.setAttribute('data-theme',t)`)
  })
})
