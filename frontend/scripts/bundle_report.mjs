#!/usr/bin/env node
/**
 * First-load JS budget report.
 *
 * Parses the route table out of two `next build` logs (baseline = main before
 * the redesign, after = this branch), pairs routes by path, and enforces the
 * redesign budget: no existing page may grow by more than 30 kB of first-load
 * JS.
 *
 *   node scripts/bundle_report.mjs --before /tmp/base-build.log --after /tmp/new-build.log \
 *        --out ../docs/frontend/evidence/bundle.md
 *
 * Routes that only exist after (never "before") are listed but not budgeted.
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import path from 'node:path'

const args = process.argv.slice(2)
const arg = (name, fallback) => {
  const i = args.indexOf(`--${name}`)
  return i >= 0 ? args[i + 1] : fallback
}
const BEFORE = arg('before', '/tmp/base-build.log')
const AFTER = arg('after', '/tmp/new-build.log')
const OUT = arg('out', path.join(process.cwd(), '..', 'docs', 'frontend', 'evidence', 'bundle.md'))
const BUDGET_KB = Number(arg('budget', '30'))

/** "├ ƒ /dashboard   8.8 kB   250 kB" → { route: '/dashboard', size: 250 } */
function parse(logPath) {
  const routes = new Map()
  let text = ''
  try {
    text = readFileSync(logPath, 'utf8')
  } catch {
    return routes
  }
  for (const line of text.split('\n')) {
    const m = line.match(/[├└│]\s+\S+\s+(\/\S*)\s+([\d.]+)\s*(kB|B)\s+([\d.]+)\s*(kB|B)\s*$/)
    if (!m) continue
    const route = m[1]
    const size = m[5] === 'kB' ? Number(m[4]) : Number(m[4]) / 1024
    routes.set(route, Number(size.toFixed(1)))
  }
  // Shared chunk size, for context.
  const shared = text.match(/First Load JS shared by all\s+([\d.]+)\s*kB/)
  return { routes, shared: shared ? Number(shared[1]) : null }
}

const before = parse(BEFORE)
const after = parse(AFTER)

if (!after.routes || after.routes.size === 0) {
  console.error(`could not parse a route table out of ${AFTER}`)
  process.exit(2)
}

const rows = []
let worst = 0
let failures = 0
for (const [route, kb] of [...after.routes.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
  const prev = before.routes?.get(route)
  const delta = prev === undefined ? null : Number((kb - prev).toFixed(1))
  if (delta !== null) {
    worst = Math.max(worst, delta)
    if (delta > BUDGET_KB) failures++
  }
  rows.push({ route, prev, kb, delta })
}

const fmt = (n) => (n === null || n === undefined ? '—' : `${n > 0 ? '+' : ''}${n.toFixed(1)}`)
const lines = [
  '# First-load JS budget',
  '',
  'Parsed from the two `next build` logs by `frontend/scripts/bundle_report.mjs`.',
  `Budget enforced here: **no existing page may grow by more than ${BUDGET_KB} kB**.`,
  '',
  `Shared chunk: before ${before.shared ?? '—'} kB → after ${after.shared ?? '—'} kB.`,
  '',
  '| Route | Before (kB) | After (kB) | Δ (kB) | Within budget |',
  '| --- | --- | --- | --- | --- |',
  ...rows.map(
    (r) =>
      `| \`${r.route}\` | ${r.prev ?? '—'} | ${r.kb} | ${fmt(r.delta)} | ${
        r.delta === null ? 'new route' : r.delta > BUDGET_KB ? '**OVER**' : 'yes'
      } |`,
  ),
  '',
  `Worst existing-page change: ${fmt(worst)} kB. ${failures === 0 ? 'All within budget.' : `${failures} route(s) OVER budget.`}`,
  '',
]
mkdirSync(path.dirname(OUT), { recursive: true })
writeFileSync(OUT, lines.join('\n'))
console.log(lines.slice(-3).join('\n'))
console.log(`table: ${OUT}`)
if (failures) process.exit(1)
