#!/usr/bin/env node
/**
 * Ensures the uint8-util patch (webtorrent toHex fix) is applied before
 * `next build`, even when the CI/deploy environment skipped or gated the
 * `npm install` postinstall (patch-package). The toHex bug is runtime-only —
 * without this, a missed patch would silently ship a broken WebTorrent P2P
 * bundle. Idempotent: skips when the patch marker is already present.
 */
const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

const root = path.join(__dirname, '..')
const srcFile = path.join(root, 'node_modules', 'uint8-util', 'dist', 'src', 'browser.js')
const marker = 'PATCHED (webtorrent toHex fix)'

if (fs.existsSync(srcFile) && fs.readFileSync(srcFile, 'utf8').includes(marker)) {
  console.log('✓ webtorrent toHex patch already applied')
  process.exit(0)
}

console.log('webtorrent toHex patch missing — applying via patch-package...')
// Spawn the local bin directly (node_modules/.bin is only on PATH inside npm
// scripts); no flags — patch-package 8 applies patches/ on install by default
// and exits non-zero if any patch fails.
const bin = process.platform === 'win32'
  ? path.join(root, 'node_modules', '.bin', 'patch-package.cmd')
  : path.join(root, 'node_modules', '.bin', 'patch-package')
const r = spawnSync(bin, [], { cwd: root, stdio: 'inherit' })
if (r.status !== 0) {
  console.error('✗ patch-package failed to apply patches/uint8-util+2.3.2.patch')
  process.exit(r.status ?? 1)
}
// Verify it actually took
if (!fs.existsSync(srcFile) || !fs.readFileSync(srcFile, 'utf8').includes(marker)) {
  console.error('✗ patch reported success but the marker is still missing — aborting')
  process.exit(1)
}
console.log('✓ webtorrent toHex patch applied during build')
