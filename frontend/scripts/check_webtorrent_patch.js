#!/usr/bin/env node
/**
 * Build-time guard for the uint8-util patch (webtorrent toHex fix).
 *
 * The bug is RUNTIME-only: without the patch, `arr2hex` calls the Chrome 149+/
 * Node 22+ `Uint8Array.prototype.toHex` API, so `next build` succeeds even when
 * the patch-package postinstall didn't run — the broken bundle just throws
 * `TypeError: e.toHex is not a function` in every browser, killing WebTorrent
 * P2P. This guard makes a missed patch fail the build loudly instead.
 *
 * Two independent checks:
 *   1. The patched source marker is present in node_modules (postinstall ran).
 *   2. No compiled chunk contains a `.toHex(` / `.fromHex(` call (the two
 *      new-typed-array APIs that break pre-Chrome-149 browsers).
 */
const fs = require('fs')
const path = require('path')

const root = path.join(__dirname, '..')
const targets = [
  {
    file: path.join(root, 'node_modules/uint8-util/dist/src/browser.js'),
    marker: 'PATCHED (webtorrent toHex fix)',
    hint: 'patches/uint8-util+2.3.2.patch',
  },
  {
    file: path.join(root, 'node_modules/webtorrent/lib/torrent.js'),
    marker: 'PATCHED (webtorrent globalThis fix)',
    hint: 'patches/webtorrent+2.8.5.patch',
  },
]
let failed = false

for (const t of targets) {
  if (!fs.existsSync(t.file) || !fs.readFileSync(t.file, 'utf8').includes(t.marker)) {
    console.error(
      `✗ ${path.basename(t.file)} patch is NOT applied — the patch-package postinstall likely did not run. ` +
        `Run 'npm ci'/'npm install' without --ignore-scripts, or check ${t.hint}.`,
    )
    failed = true
  }
}

const chunksDir = path.join(root, '.next/static/chunks')
if (fs.existsSync(chunksDir)) {
  const broken = /\.toHex\(|\.fromHex\(|global\.WEBTORRENT_ANNOUNCE/
  for (const file of fs.readdirSync(chunksDir)) {
    if (!file.endsWith('.js')) continue
    const content = fs.readFileSync(path.join(chunksDir, file), 'utf8')
    if (broken.test(content)) {
      console.error(
        `✗ shipped chunk ${file} still contains .toHex(/ .fromHex(/ global.WEBTORRENT_ANNOUNCE — browser-incompatible. ` +
          'Check that the patches are applied and the build used the patched source.',
      )
      failed = true
    }
  }
}

if (failed) process.exit(1)
console.log(
  '✓ webtorrent patch guard: OK (patches applied, no .toHex(/.fromHex(/global.WEBTORRENT_ANNOUNCE in the bundle)',
)
