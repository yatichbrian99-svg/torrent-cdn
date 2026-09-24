/**
 * Guards the inline pre-paint theme script in app/layout.tsx.
 *
 * That script must run before first paint (no theme flash), which means it
 * cannot import lib/theme.ts — so it duplicates the storage key + fallback
 * logic as a literal string. This test keeps the two in lockstep: change the
 * key or the resolution rule in lib/theme.ts without updating layout.tsx (or
 * vice versa) and CI fails.
 *
 * It also pins a production incident: under the nonce-based CSP (middleware.ts)
 * a RAW inline <script> is blocked by the browser. The script must be rendered
 * via next/script so Next.js stamps the per-request nonce onto it — the raw
 * form once left data-theme-resolved unset and, with the visibility:hidden
 * theme guard in globals.css, blacked out the entire deployed site.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { THEME_KEY, readStoredTheme } from '../theme'

const layoutSource = readFileSync(join(__dirname, '..', '..', 'app', 'layout.tsx'), 'utf8')
const globalsSource = readFileSync(join(__dirname, '..', '..', 'app', 'globals.css'), 'utf8')

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

  it('is rendered via next/script so the CSP nonce is stamped on it (incident pin)', () => {
    // The theme script must be inside a <Script> (next/script), which receives
    // the CSP nonce from middleware — a raw <script dangerouslySetInnerHTML>
    // is blocked by script-src 'nonce-…' and the page would never resolve.
    expect(layoutSource).toMatch(/<Script[\s\S]*?THEME_SCRIPT[\s\S]*?\/>/)
    // No raw non-JSON-LD inline <script> remains in the layout.
    expect(layoutSource).not.toMatch(/<script(?![^>]*ld\+json)[\s\S]*?dangerouslySetInnerHTML/)
    // CSS failsafe: the visibility guard must self-release via animation even
    // if the script is ever blocked again (0.3s delay ≤ any perceivable flash).
    expect(globalsSource).toMatch(/html:not\(\[data-theme-resolved\]\) body \{[\s\S]*?visibility: hidden;[\s\S]*?animation:[^;]*theme-guard-release/)
    expect(globalsSource).toContain('@keyframes theme-guard-release')
  })
})
