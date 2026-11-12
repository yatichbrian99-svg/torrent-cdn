/**
 * Guards the inline pre-paint theme script in app/layout.tsx.
 *
 * That script must run before first paint (no theme flash), which means it
 * cannot import lib/theme.ts — so it duplicates the storage key + resolution
 * rule as a literal string. This test keeps the two in lockstep: change the
 * key or the rule in lib/theme.ts without updating layout.tsx (or vice versa)
 * and CI fails.
 *
 * It also pins two things the redesign must not regress:
 *   - The script runs via next/script so Next.js stamps the per-request CSP
 *     nonce on it. A RAW inline <script> is blocked by the nonce-based CSP; the
 *     raw form once left data-theme-resolved unset and, with the theme guard in
 *     globals.css, blacked out the entire deployed site.
 *   - The two dark token blocks in globals.css (explicit choice vs OS
 *     preference) declare identical values, so "follow the system" and "chose
 *     dark" can never drift apart visually.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { THEME_KEY, readStoredTheme, resolveTheme } from '../theme'

const layoutSource = readFileSync(join(__dirname, '..', '..', 'app', 'layout.tsx'), 'utf8')
const globalsSource = readFileSync(join(__dirname, '..', '..', 'app', 'globals.css'), 'utf8')

/** Pull the inline script text out of layout.tsx. */
function extractInitScript(): string {
  const match = layoutSource.match(/const THEME_SCRIPT = `([\s\S]*?)`/)
  expect(match, 'pre-paint theme script not found in app/layout.tsx').toBeTruthy()
  return match![1]
}

/** Extract a `{ … }` block that starts at the given selector. */
function blockAfter(source: string, selector: string): string {
  const start = source.indexOf(selector)
  expect(start, `block ${selector} not found`).toBeGreaterThan(-1)
  const open = source.indexOf('{', start)
  let depth = 0
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++
    else if (source[i] === '}') {
      depth--
      if (depth === 0) return source.slice(open + 1, i)
    }
  }
  throw new Error(`unbalanced block for ${selector}`)
}

describe('pre-paint theme script parity', () => {
  it('uses the same storage key as lib/theme.ts', () => {
    expect(extractInitScript()).toContain(`localStorage.getItem('tcdn_theme')`)
    expect(THEME_KEY).toBe('tcdn_theme')
  })

  it('resolves exactly like resolveTheme: stored choice, else the OS, else light', () => {
    // lib/theme.ts contract…
    expect(readStoredTheme(null)).toBeNull()
    expect(readStoredTheme({ getItem: () => 'light' })).toBe('light')
    expect(readStoredTheme({ getItem: () => 'dark' })).toBe('dark')
    expect(readStoredTheme({ getItem: () => 'nonsense' })).toBeNull()
    expect(resolveTheme({ getItem: () => null }, true)).toBe('dark')
    expect(resolveTheme({ getItem: () => null }, false)).toBe('light')
    expect(resolveTheme({ getItem: () => 'light' }, true)).toBe('light')

    // …and the inline script encodes the same rule: an explicit value wins,
    // otherwise prefers-color-scheme decides.
    const script = extractInitScript()
    expect(script).toContain(`(s==='light'||s==='dark')?s:(dark?'dark':'light')`)
    expect(script).toContain('if(m){if(m.matches){dark=true;}}')
    expect(script).toContain("matchMedia('(prefers-color-scheme: dark)')")
    // No `&&` inside the inline script: React escapes a literal `&` in the
    // rendered tag text, which turned into a SyntaxError and stopped the script
    // from running at all (found by the browser-level theme audit).
    expect(script).not.toContain('&&')
    // The "no choice" case must NOT be written back to storage, or the site
    // would stop following the OS after the first visit.
    expect(script).not.toMatch(/setItem\(\s*'tcdn_theme'/)
  })

  it('sets data-theme and data-theme-resolved before resolving', () => {
    const script = extractInitScript()
    expect(script).toContain("setAttribute('data-theme',t)")
    expect(script).toContain("setAttribute('data-theme-resolved','1')")
    expect(script).toContain('d.style.colorScheme=t')
  })

  it('is a synchronous nonce-stamped script inside <head> (incident pin)', () => {
    // Synchronous matters: the theme must resolve while the document is parsed,
    // not when a bundle finishes — otherwise the visibility guard hides the
    // page for the whole network round-trip.
    const tag = layoutSource.match(/<script[\s\S]*?THEME_SCRIPT[\s\S]*?\/>/)
    expect(tag, 'theme script tag not found').toBeTruthy()
    expect(tag![0]).toContain('nonce={nonce}')
    expect(tag![0]).not.toMatch(/async|defer|strategy=/)
    expect(layoutSource.indexOf('THEME_SCRIPT')).toBeLessThan(layoutSource.indexOf('</head>'))
    // The incident: a raw inline <script> without a nonce is blocked by
    // `script-src 'nonce-…'`, the guard never releases, the site goes black.
    // Every inline script in the layout must therefore carry the nonce.
    const inlineScripts = Array.from(
      layoutSource.matchAll(/<script[\s\S]*?dangerouslySetInnerHTML/g),
    ).map((m) => m[0])
    expect(inlineScripts.length).toBeGreaterThan(0)
    for (const tag of inlineScripts) expect(tag).toContain('nonce={nonce}')
    // CSS failsafe: the visibility guard must self-release via animation even
    // if the script is ever blocked again (0.3s delay ≤ any perceivable flash).
    expect(globalsSource).toMatch(/html:not\(\[data-theme-resolved\]\) body \{[\s\S]*?visibility: hidden;[\s\S]*?animation:[^;]*theme-guard-release/)
    expect(globalsSource).toContain('@keyframes theme-guard-release')
  })
})

describe('frozen-surface theme script', () => {
  it('keeps the pre-redesign rule (dark unless light was chosen) for /embed and /share', () => {
    const match = layoutSource.match(/const FROZEN_THEME_SCRIPT = `([\s\S]*?)`/)
    expect(match, 'FROZEN_THEME_SCRIPT not found').toBeTruthy()
    const script = match![1]
    // The old contract: anything except a stored 'light' is dark — the OS is
    // NOT consulted, because those surfaces must look exactly as before.
    expect(script).toContain(`(s==='light')?'light':'dark'`)
    expect(script).not.toContain('prefers-color-scheme')
    expect(script).not.toContain('&&')
    // And the layout picks it for frozen routes only.
    expect(layoutSource).toContain('frozen ? FROZEN_THEME_SCRIPT : THEME_SCRIPT')
  })
})

describe('theme token blocks', () => {
  it('declares the same dark palette for "chose dark" and "OS dark"', () => {
    const explicit = blockAfter(globalsSource, "[data-theme='dark'] {").trim()
    const mediaBlock = blockAfter(globalsSource, '@media (prefers-color-scheme: dark)')
    const osDark = blockAfter(mediaBlock, ":root:not([data-theme='light'])").trim()
    // Compare only token declarations, order-independent.
    const pairs = (block: string) =>
      Object.fromEntries(
        Array.from(block.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/gi)).map((m) => [m[1], m[2].trim()]),
      )
    const a = pairs(explicit)
    const b = pairs(osDark)
    expect(Object.keys(a).length).toBeGreaterThan(20)
    expect(b).toEqual(a)
  })

  it('keeps the frozen surfaces pinned to the pre-redesign palette', () => {
    expect(globalsSource).toMatch(/:root\[data-frozen-surface\]\s*\{[\s\S]*?--bg-primary: #000000;/)
    expect(globalsSource).toMatch(/:root\[data-frozen-surface\]\[data-theme='light'\]\s*\{[\s\S]*?--bg-primary: #fafaf9;/)
  })
})
