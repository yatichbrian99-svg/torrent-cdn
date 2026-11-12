#!/usr/bin/env node
/**
 * WCAG contrast matrix for the design tokens.
 *
 * Reads the token blocks out of app/globals.css (so it always measures what
 * ships, not a copy), computes WCAG 2.1 contrast ratios for every text/UI pair
 * that appears on the redesigned pages in BOTH themes, and writes a markdown
 * table. Exits non-zero if body text drops below 4.5:1 or a non-text UI pair
 * below 3:1.
 *
 *   node scripts/check_contrast.mjs --out ../docs/frontend/evidence/contrast.md
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import path from 'node:path'

const args = process.argv.slice(2)
const arg = (name, fallback) => {
  const i = args.indexOf(`--${name}`)
  return i >= 0 ? args[i + 1] : fallback
}

const css = readFileSync(path.join(process.cwd(), 'app', 'globals.css'), 'utf8')
const out = arg('out', path.join(process.cwd(), '..', 'docs', 'frontend', 'evidence', 'contrast.md'))

function blockAfter(source, selector) {
  const start = source.indexOf(selector)
  if (start < 0) throw new Error(`selector not found: ${selector}`)
  const open = source.indexOf('{', start)
  let depth = 0
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++
    else if (source[i] === '}') {
      depth--
      if (depth === 0) return source.slice(open + 1, i)
    }
  }
  throw new Error(`unbalanced block: ${selector}`)
}

function tokensOf(block) {
  const map = {}
  for (const m of block.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/gi)) {
    map[m[1]] = m[2].trim()
  }
  return map
}

const lightBlock = blockAfter(css, ':root {')
const darkBlock = blockAfter(css, "[data-theme='dark'] {")

function resolveTokens(block, key, seen = new Set()) {
  const raw = block[key]
  if (!raw) return null
  const varRef = raw.match(/^var\((--[a-z0-9-]+)\)$/i)
  if (varRef) {
    if (seen.has(varRef[1])) return null
    seen.add(varRef[1])
    return resolveTokens(block, varRef[1], seen)
  }
  return raw
}

const light = tokensOf(lightBlock)
const dark = tokensOf(darkBlock)

function parseColor(value) {
  if (!value) return null
  const v = value.trim()
  let m = v.match(/^#([0-9a-f]{6})$/i)
  if (m) {
    const n = parseInt(m[1], 16)
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1]
  }
  m = v.match(/^#([0-9a-f]{3})$/i)
  if (m) {
    const [r, g, b] = m[1].split('').map((c) => parseInt(c + c, 16))
    return [r, g, b, 1]
  }
  m = v.match(/^rgba?\(([^)]+)\)$/i)
  if (m) {
    const parts = m[1].split(/[,/ ]+/).filter(Boolean).map(Number)
    return [parts[0], parts[1], parts[2], parts[3] ?? 1]
  }
  return null
}

function blend(fg, bg) {
  const [r, g, b, a] = fg
  if (a >= 1) return [r, g, b, 1]
  return [r * a + bg[0] * (1 - a), g * a + bg[1] * (1 - a), b * a + bg[2] * (1 - a), 1]
}

function luminance([r, g, b]) {
  const channel = (c) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

function ratio(fgRaw, bgRaw) {
  const bg = parseColor(bgRaw)
  let fg = parseColor(fgRaw)
  if (!fg || !bg) return null
  fg = blend(fg, bg)
  const a = luminance(fg)
  const b = luminance(bg)
  const [hi, lo] = a > b ? [a, b] : [b, a]
  return (hi + 0.05) / (lo + 0.05)
}

// [foreground token, background token, minimum ratio, what it is used for]
const PAIRS = [
  ['--text', '--bg', 4.5, 'body text on page'],
  ['--text', '--surface', 4.5, 'body text on tinted section'],
  ['--text', '--surface-2', 4.5, 'body text on card'],
  ['--muted', '--bg', 4.5, 'secondary text (hero sub, footer) on page'],
  ['--muted', '--surface', 4.5, 'secondary text on tinted section'],
  ['--muted', '--surface-2', 4.5, 'secondary text on card'],
  ['--text-faint', '--bg', 4.5, 'legacy faint text alias'],
  ['--text-dim', '--surface', 4.5, 'legacy dim text alias'],
  ['--text-soft', '--surface-2', 4.5, 'legacy soft text alias'],
  ['--accent-text', '--bg', 4.5, 'accent headline word / link hover'],
  ['--accent-text', '--surface', 4.5, 'accent text on tinted section'],
  ['--accent-text', '--surface-2', 4.5, 'accent text on card'],
  ['--accent-contrast', '--accent', 4.5, 'label on filled accent button'],
  // Light hovers to a darker accent (contrast rises); dark keeps the accent
  // fill on hover because white on #F04A0A is only 3.7:1 — see the note in
  // globals.css. So this pair only applies to light.
  ['--accent-contrast', '--accent-hover', 4.5, 'label on hovered accent button', ['light']],
  ['--ok', '--surface-2', 4.5, 'success text on card'],
  ['--warn', '--surface-2', 4.5, 'warning text on card'],
  ['--err', '--surface-2', 4.5, 'error text on card'],
  ['--info', '--surface-2', 4.5, 'info text on card'],
  // Decorative hairline separators are explicitly exempt from WCAG 1.4.11 (they
  // convey no information and identify no control); every border that outlines
  // a control uses --border-strong and is held to 3:1.
  ['--border', '--bg', 1.0, 'decorative hairline separator (exempt from 1.4.11)'],
  ['--border-strong', '--bg', 3.0, 'control outline: inputs, secondary buttons'],
  ['--border-strong', '--surface-2', 3.0, 'control outline on a card'],
  ['--accent', '--bg', 3.0, 'focus ring / accent rule (non-text UI)'],
]

const rows = []
let failures = 0
for (const [themeName, block] of [['light', light], ['dark', dark]]) {
  for (const [fg, bg, min, use, themes] of PAIRS) {
    if (themes && !themes.includes(themeName)) continue
    const fgVal = resolveTokens(block, fg)
    const bgVal = resolveTokens(block, bg)
    const r = ratio(fgVal, bgVal)
    const pass = r !== null && r >= min
    if (!pass) failures++
    rows.push({
      theme: themeName,
      fg,
      bg,
      fgVal,
      bgVal,
      ratio: r === null ? null : Number(r.toFixed(2)),
      min,
      result: pass ? 'PASS' : 'FAIL',
      use,
    })
  }
}

const lines = [
  '# Contrast matrix (WCAG 2.1)',
  '',
  'Generated by `frontend/scripts/check_contrast.mjs` from the token block in',
  '`frontend/app/globals.css` — the values that actually ship. Thresholds:',
  '4.5:1 for body/UI text, 3:1 for large text and non-text UI.',
  '',
  '| Theme | Foreground | Background | Ratio | Min | Result | Used for |',
  '| --- | --- | --- | --- | --- | --- | --- |',
  ...rows.map(
    (r) =>
      `| ${r.theme} | \`${r.fg}\` ${r.fgVal} | \`${r.bg}\` ${r.bgVal} | ${r.ratio ?? 'n/a'}:1 | ${r.min}:1 | ${r.result === 'PASS' ? 'pass' : '**FAIL**'} | ${r.use} |`,
  ),
  '',
  failures === 0
    ? `All ${rows.length} pairs pass.`
    : `${failures} pair(s) FAIL — see the bold rows above.`,
  '',
]

mkdirSync(path.dirname(out), { recursive: true })
writeFileSync(out, lines.join('\n'))
console.log(lines.slice(-4).join('\n'))
console.log(`table: ${out}`)
if (failures) {
  for (const r of rows.filter((x) => x.result === 'FAIL')) {
    console.log(`  FAIL ${r.theme} ${r.fg} on ${r.bg}: ${r.ratio}:1 (min ${r.min})`)
  }
  process.exit(1)
}
