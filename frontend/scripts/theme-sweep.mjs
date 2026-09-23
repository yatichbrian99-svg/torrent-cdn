#!/usr/bin/env node
/**
 * One-shot codemod: hardcoded UI hexes → theme CSS variables.
 *
 * The app was dark-only with ~1000 inline hex styles. This rewrites the THEME
 * palette (backgrounds, borders, text grays, inverted surfaces, chip tints)
 * to var(--token). STATUS/ACCENT colors (green/amber/red/purple/blue/orange)
 * are intentionally LEFT AS-IS — they are shared by both themes.
 *
 * Property-aware for the ambiguous tones:
 *   #fff  → var(--inverse-bg) for background/border, kept literal for `color`.
 *           (White-on-dark "inverted buttons" become black-on-light, but white
 *           text on a green/red chip must stay white in BOTH themes.)
 *   #ddd  → var(--text-bright) for `color`, kept literal for borderColor
 *           (the rotate confirm button's border is a tint, not themeable).
 *
 * Idempotent: var() references don't match the hex regex, so re-running is a
 * no-op. Skips test files; touches only app/ + components/.
 */
import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const ROOTS = ['app', 'components']

// Ambiguous values that depend on the CSS property they're used with.
const PROPERTY_AWARE = {
  '#fff': { color: '#fff', default: 'var(--inverse-bg)' },
  '#ffffff': { color: '#fff', default: 'var(--inverse-bg)' },
  '#ddd': { color: 'var(--text-bright)', default: '#ddd' },
  '#cccccc': { color: 'var(--text-soft)', default: '#ccc' },
  '#ccc': { color: 'var(--text-soft)', default: '#ccc' },
}

// Unambiguous values → replacement ('' value = leave untouched).
const MAP = {
  // backgrounds
  '#000': 'var(--bg-primary)',
  '#000000': 'var(--bg-primary)',
  '#0c0c0c': 'var(--bg-secondary)',
  '#111': 'var(--bg-card)',
  '#111111': 'var(--bg-card)',
  '#080808': 'var(--bg-raised)',
  // borders
  '#222': 'var(--border)',
  '#222222': 'var(--border)',
  '#333': 'var(--border-strong)',
  '#333333': 'var(--border-strong)',
  // text grays (all appear only as `color:` in the codebase)
  '#555': 'var(--text-muted)',
  '#555555': 'var(--text-muted)',
  '#444': 'var(--text-faint)',
  '#444444': 'var(--text-faint)',
  '#666': 'var(--text-dim)',
  '#666666': 'var(--text-dim)',
  '#888': 'var(--text-soft)',
  '#888888': 'var(--text-soft)',
  '#999': 'var(--text-soft)',
  // tinted chip backgrounds/borders (status-adjacent surfaces) → themed vars
  '#7f1d1d': 'var(--danger-bg)',
  '#553333': 'var(--danger-border)',
  '#331111': 'var(--err-chip-border)',
  '#1a0d0d': 'var(--err-chip-bg)',
  '#332a11': 'var(--warn-chip-border)',
  '#1a170d': 'var(--warn-chip-bg)',
  '#1c0f06': 'var(--warn-chip-bg)',
  '#1a3a1a': 'var(--ok-chip-border)',
  '#1a4a1a': 'var(--ok-chip-bg)',
  '#4a1a1a': 'var(--err-chip-border)',
  '#2a0a0a': 'var(--err-chip-bg)',
  '#0d1f0d': 'var(--ok-chip-bg)',
  '#0a2a0a': 'var(--ok-chip-bg)',
  '#3a1a1a': 'var(--err-chip-border)',
  '#86efac': 'var(--ok-chip-text)',
  '#bbf7d0': 'var(--ok-chip-text)',
}

// hex → CSS property → replacement. Applied first; remaining occurrences fall
// through to the global regex below.
const CONTEXTUAL = {
  '#fff': { color: '#fff' },
  '#ffffff': { color: '#fff' },
  '#ddd': { color: 'var(--text-bright)' },
  '#ccc': { color: 'var(--text-soft)' },
  '#cccccc': { color: 'var(--text-soft)' },
  // subtle divider/elevated tone — behaves as a border in tables, a card bg
  // in the dashboard warning banner
  '#1a1a1a': { borderColor: 'var(--divider)', backgroundColor: 'var(--bg-hover)' },
  '#ddd': { color: 'var(--text-bright)' },
  '#0d1a0d': { backgroundColor: 'var(--ok-chip-bg)' },
  '#113311': { borderColor: 'var(--ok-chip-border)' },
  '#110808': { backgroundColor: 'var(--err-chip-bg)' },
  '#1a2a4a': { border: 'var(--info-chip-border)' },
}

function walk(dir, cb) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) {
      if (name === '__tests__') continue
      walk(p, cb)
    } else if (/\.(tsx|ts)$/.test(name)) {
      cb(p)
    }
  }
}

let total = 0
const filesChanged = []

for (const root of ROOTS) {
  walk(root, (path) => {
    let src = readFileSync(path, 'utf8')
    const before = src

    // 1) Property-aware pass: rewrite `prop: HEX` pairs individually.
    for (const [hex, props] of Object.entries(CONTEXTUAL)) {
      const re = new RegExp(`(backgroundColor|borderColor|border|color)\\s*:\\s*['\"]${hex}['\"]`, 'g')
      src = src.replace(re, (m, prop) => {
        const replacement = props[prop] // regex captures the exact camelCase name
        return replacement ? `${prop}: '${replacement}'` : m
      })
      // 2) Remaining #fff occurrences outside backgroundColor/borderColor are
      //    `color:` (chips, icons) — keep literal white; but handle the case
      //    where the same object literal has both.
    }
    // After the contextual pass, any leftover bare '#fff' / '#ffffff' sits in
    // a non-surface property (color/text) or is a string we keep intentionally
    // (e.g. '#fff' inside sdk code) — only backgroundColor/borderColor ones
    // that escaped step 1 (multi-line objects) get handled here:
    src = src.replace(/(backgroundColor|borderColor):\s*'(#fff(?:fff)?)'/g, "$1: 'var(--inverse-bg)'")

    // 3) Unambiguous global pass. NOTE: the '#' must be IN the pattern —
    //    slicing it off would match bare quoted numbers (e.g. '555') and
    //    corrupt non-color strings.
    for (const [hex, v] of Object.entries(MAP)) {
      if (!v) continue
      const re = new RegExp(`['\"]${hex}['\"]`, 'g')
      src = src.replace(re, `'${v}'`)
    }

    if (src !== before) {
      writeFileSync(path, src)
      filesChanged.push(path)
      const n = (before.match(/['\"]#[0-9a-fA-F]{3,6}['\"]/g) || []).length -
                (src.match(/['\"]#[0-9a-fA-F]{3,6}['\"]/g) || []).length
      total += Math.max(n, 0)
    }
  })
}

console.log(`files changed: ${filesChanged.length}`)
for (const f of filesChanged) console.log(`  ${f}`)
console.log(`approx replacements: ${total}`)
