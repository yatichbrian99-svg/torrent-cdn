#!/usr/bin/env node
/**
 * Ensures the webtorrent patches (uint8-util toHex fix + webtorrent globalThis
 * fix) are applied before `next build`, even when the CI/deploy environment
 * skipped or gated the `npm install` postinstall (patch-package). Both bugs are
 * runtime-only — without these, a missed patch would silently ship a broken
 * WebTorrent P2P bundle (`.toHex is not a function` / `global is not defined`).
 * Idempotent: skips when every patch marker is already present.
 */
const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

const root = path.join(__dirname, '..')

// Wipe Next.js's persistent webpack cache BEFORE building. The cache key in
// CI/Vercel doesn't include the node_modules patch state, so a restore can
// serve previously-compiled chunks (the pre-patch, `.toHex(`-containing
// output) even though node_modules is patched — the post-build guard then
// fails the build with the old chunk hash. Recompiling from scratch
// guarantees the bundle reflects the patched source. (Small app — a fresh
// compile is ~1 min; correctness wins.)
const cacheDir = path.join(root, '.next', 'cache')
if (fs.existsSync(cacheDir)) {
  fs.rmSync(cacheDir, { recursive: true, force: true })
  console.log('[OK] cleared stale Next.js build cache (.next/cache)')
}

const targets = [
  {
    file: path.join(root, 'node_modules', 'uint8-util', 'dist', 'src', 'browser.js'),
    marker: 'PATCHED (webtorrent toHex fix)',
    name: 'uint8-util (toHex)',
  },
  {
    file: path.join(root, 'node_modules', 'webtorrent', 'lib', 'torrent.js'),
    marker: 'PATCHED (webtorrent globalThis fix)',
    name: 'webtorrent (globalThis)',
  },
]

const missing = targets.filter(
  (t) => !fs.existsSync(t.file) || !fs.readFileSync(t.file, 'utf8').includes(t.marker),
)

if (missing.length === 0) {
  console.log('[OK] webtorrent patches already applied')
  process.exit(0)
}

console.log(
  `webtorrent patch(es) missing (${missing.map((t) => t.name).join(', ')}) — applying via patch-package...`,
)
// Spawn the local bin directly (node_modules/.bin is only on PATH inside npm
// scripts); no flags — patch-package 8 applies every patch in patches/ by
// default and exits non-zero if any fails.
const bin = process.platform === 'win32'
  ? path.join(root, 'node_modules', '.bin', 'patch-package.cmd')
  : path.join(root, 'node_modules', '.bin', 'patch-package')
const r = spawnSync(bin, [], { cwd: root, stdio: 'inherit' })
if (r.error && r.error.code === 'ENOENT') {
  console.error('[FAIL] patch-package not found — install devDependencies (npm ci) before building')
  process.exit(1)
}
if (r.status !== 0) {
  console.error(
    '[FAIL] patch-package failed to apply patches/ — ' +
      'reset node_modules (npm ci) and retry',
  )
  process.exit(r.status ?? 1)
}
// Verify it actually took
const stillMissing = targets.filter(
  (t) => !fs.existsSync(t.file) || !fs.readFileSync(t.file, 'utf8').includes(t.marker),
)
if (stillMissing.length > 0) {
  console.error(
    `[FAIL] patch reported success but ${stillMissing.map((t) => t.name).join(', ')} marker(s) ` +
      'are still missing — aborting',
  )
  process.exit(1)
}
console.log('[OK] webtorrent patches applied during build')
