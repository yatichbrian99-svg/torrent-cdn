/**
 * E2E runner: executes the three production Playwright suites sequentially.
 *
 * Usage:
 *   node e2e/run-all.mjs                     # all suites
 *   node e2e/run-all.mjs upload-direct       # just one suite
 *   node e2e/run-all.mjs upload embed p2p    # several (prefix-matched)
 *
 * Each suite creates (and cleans up) its own throwaway account + file, so runs
 * are repeatable against the live site. See README.md for prerequisites.
 */
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = path.dirname(fileURLToPath(import.meta.url))
const suites = ['upload-direct.mjs', 'upload-fallback.mjs', 'embed-playback.mjs', 'auto-seed.mjs', 'p2p-two-peer.mjs', 'api-key-ui.mjs', 'finalize-retry.mjs', 'embed-speed.mjs', 'login-flow.mjs', 'mfa-flow.mjs', 'visual-pass.mjs']
const requested = process.argv.slice(2)
const toRun = requested.length
  ? suites.filter((s) => requested.some((r) => s.startsWith(r)))
  : suites
if (!toRun.length) {
  console.error(`unknown suite(s) "${requested.join(' ')}" — options: ${suites.join(', ')}`)
  process.exit(2)
}

let failed = 0
for (const suite of toRun) {
  console.log(`\n===== e2e/${suite} =====`)
  const r = spawnSync(process.execPath, [path.join(dir, suite)], {
    stdio: 'inherit',
    env: process.env,
    timeout: 10 * 60 * 1000,
  })
  if (r.status === 0) {
    console.log(`✓ e2e/${suite}`)
  } else {
    failed++
    console.log(`✗ e2e/${suite} (exit ${r.status ?? 'timeout/signal'})`)
  }
}

console.log(`\n${toRun.length - failed}/${toRun.length} suites passed`)
process.exit(failed ? 1 : 0)
