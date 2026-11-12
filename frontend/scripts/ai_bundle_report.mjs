#!/usr/bin/env node
/**
 * Bundle report for the TCDN-AI console.
 *
 * Two questions, answered from the real build output rather than a claim:
 *
 * 1. how big is each console page's JS, and how much of it is shared;
 * 2. is ANY chunk of the console loaded by the existing pages (/, /dashboard,
 *    /billing)? The console is a separate route segment, so the answer must be
 *    "none" — and this fails the check if it ever stops being true.
 *
 * Usage: npm run build && node scripts/ai_bundle_report.mjs
 */
import fs from 'node:fs'
import path from 'node:path'

const NEXT = path.join(process.cwd(), '.next')
const manifestPath = path.join(NEXT, 'app-build-manifest.json')

if (!fs.existsSync(manifestPath)) {
  console.error('No build found. Run `npm run build` first.')
  process.exit(2)
}

const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'))
const pages = manifest.pages || {}

const sizeOf = (file) => {
  const full = path.join(NEXT, file)
  return fs.existsSync(full) ? fs.statSync(full).size : 0
}

const rows = []
for (const [route, files] of Object.entries(pages)) {
  const shared = files.filter((file) => file.startsWith('static/chunks/') && !route.includes('/'))
  rows.push({
    route,
    files,
    bytes: files.reduce((total, file) => total + sizeOf(file), 0),
    sharedChunks: shared.length,
  })
}

const aiRoutes = rows.filter((row) => row.route.startsWith('/ai'))
const existingRoutes = rows.filter((row) => ['/page', '/dashboard/page', '/billing/page'].includes(row.route))

const aiOnly = new Set()
for (const row of aiRoutes) {
  for (const file of row.files) aiOnly.add(file)
}
for (const row of existingRoutes) {
  for (const file of row.files) aiOnly.delete(file)
}

const leaks = []
for (const row of existingRoutes) {
  const overlap = row.files.filter((file) => aiOnly.has(file))
  if (overlap.length) leaks.push({ route: row.route, overlap })
}

const kb = (bytes) => `${(bytes / 1024).toFixed(1)} kB`

console.log('route                          first-load JS')
for (const row of [...rows].sort((a, b) => a.route.localeCompare(b.route))) {
  console.log(`${row.route.padEnd(30)} ${kb(row.bytes).padStart(12)}`)
}

const totalAi = aiRoutes.reduce((total, row) => total + row.bytes, 0)
console.log(`\nconsole routes: ${aiRoutes.length}, ${kb(totalAi)} total`)
console.log(`chunks unique to the console: ${aiOnly.size}`)
for (const row of existingRoutes) {
  console.log(`existing route ${row.route.padEnd(18)} ${kb(row.bytes)}`)
}

if (leaks.length) {
  console.error('\n[FAIL] an existing page loads a console-only chunk:', JSON.stringify(leaks, null, 2))
  process.exit(1)
}
console.log('\n[OK] no existing page loads a console-only chunk')
