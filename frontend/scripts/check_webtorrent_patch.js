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
const srcFile = path.join(root, 'node_modules/uint8-util/dist/src/browser.js')
const marker = 'PATCHED (webtorrent toHex fix)'
let failed = false

if (!fs.existsSync(srcFile) || !fs.readFileSync(srcFile, 'utf8').includes(marker)) {
  console.error(
    '✗ uint8-util patch is NOT applied — the patch-package postinstall likely did not run. ' +
      'Run `npm ci`/`npm install` without --ignore-scripts, or check patches/uint8-util+2.3.2.patch.',
  )
  failed = true
}

const chunksDir = path.join(root, '.next/static/chunks')
if (fs.existsSync(chunksDir)) {
  const broken = /\.toHex\(|\.fromHex\(/
  for (const file of fs.readdirSync(chunksDir)) {
    if (!file.endsWith('.js')) continue
    const content = fs.readFileSync(path.join(chunksDir, file), 'utf8')
    if (broken.test(content)) {
      console.error(
        `✗ shipped chunk ${file} still contains .toHex(/ .fromHex( — browser-incompatible. ` +
          'Check that the uint8-util patch is applied and the build used the patched source.',
      )
      failed = true
    }
  }
}

if (failed) process.exit(1)
console.log('✓ webtorrent toHex patch guard: OK (patch applied, no .toHex(/.fromHex( in the bundle)')
