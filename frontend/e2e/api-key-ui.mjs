/**
 * E2E: dashboard API Key UI — Generate, Copy, Revoke, key-shown-once.
 *
 * Drives the REAL dashboard UI in headless Chromium against the live site:
 *   - logged-in /dashboard shows the "Generate API key" button (no key yet)
 *   - clicking Generate reveals the full tcdn_ key in a <code> box, the
 *     "only time the full key is shown" warning, and a Copy button
 *   - Copy writes the exact key to the clipboard and flips the button to
 *     "Copied"
 *   - RELOADING the page shows ONLY the prefix + creation date (the secret is
 *     stored hashed server-side and must never be returned again)
 *   - Revoke removes the key and the button resets to "Generate API key"
 *   - the revoked key is fail-closed: a request with it returns 401
 *   - zero JS page errors and zero failed requests
 *
 * Cleanup: deletes the throwaway account (and only that account).
 */
import { chromium } from 'playwright'
import { BASE, registerUser, cleanup, attachDiagnostics, report } from './helpers.mjs'

const failures = []
const out = {
  suite: 'api-key-ui',
  steps: [],
  apiCalls: [],
  consoleErrors: [],
  pageErrors: [],
  failedRequests: [],
  dialogs: [],
}

const browser = await chromium.launch({ headless: true })
let user = null

try {
  // Throwaway account (or reuse E2E_TOKEN — which then skips cleanup too).
  const token = process.env.E2E_TOKEN
  if (!token) user = await registerUser('api-key-ui')
  const authToken = token || user.token

  // Clipboard permissions: the dashboard's Copy button uses
  // navigator.clipboard.writeText, which needs the permission in Chromium.
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  // Same sessionStorage token seed the other suites use (the app's real key).
  await context.addInitScript((tok) => {
    window.sessionStorage.setItem('torrent_cdn_token', tok)
  }, authToken)
  const page = await context.newPage()
  attachDiagnostics(page, out)
  // Capture the API-key calls so we can assert on the request contract.
  page.on('response', (r) => {
    const u = r.url()
    if (u.includes('/auth/api-key') || u.includes('/files')) {
      out.apiCalls.push(`[${r.status()}] ${r.request().method()} ${u.replace(BASE, '')}`)
    }
  })
  // The dashboard alerts on failure (generate/revoke). Dismiss + record rather
  // than hanging the suite on a modal.
  page.on('dialog', async (d) => {
    out.dialogs.push(d.message())
    await d.dismiss()
  })

  // 1) Dashboard while logged in, no key yet → Generate button
  await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.waitForSelector('text=Generate API key', { timeout: 30000 })
  out.steps.push('dashboard loaded, "Generate API key" button visible')
  const keyPrefixSection = page.locator('text=Generate a key to use the REST API programmatically.')
  if (await keyPrefixSection.count()) out.steps.push('pre-generate hint text shown')

  // 2) Generate → full key revealed once. The key lives in its own <code> box;
  // scope by content (tcdn_ prefix) so the selection is reorder-proof even if
  // the referral <code> ever moves above it in the DOM. The app auto-retries
  // the request on a Render cold start (502), but if a cold start outlasts the
  // built-in retry the failure alert fires — re-click Generate rather than
  // failing the suite (the alert handler above dismisses + records it).
  const dialogsBefore = out.dialogs.length
  await page.click('text=Generate API key')
  try {
    await page.waitForSelector('text=This is the only time the full key is shown', { timeout: 30000 })
  } catch {
    if (out.dialogs.length > dialogsBefore) {
      out.steps.push('cold-start alert on first Generate — retrying')
      await page.waitForTimeout(5000)
      await page.click('text=Generate API key')
      await page.waitForSelector('text=This is the only time the full key is shown', { timeout: 45000 })
    } else {
      throw new Error('Generate never revealed the key and no alert fired')
    }
  }
  const fullKey = (await page.locator('code:has-text("tcdn_")').first().innerText()).trim()
  out.keyLength = fullKey.length
  out.keyPrefix = fullKey.slice(0, 8)
  out.keyShownOnceWarning = true
  if (!/^tcdn_[A-Za-z0-9_-]{42,43}$/.test(fullKey)) {
    failures.push(`generated key doesn't look like a tcdn_ key: "${fullKey.slice(0, 20)}…"`)
  }
  out.steps.push(`key generated (${fullKey.length} chars, prefix ${fullKey.slice(0, 8)}…)`)

  // 3) Copy → clipboard holds the exact key, button flips to "Copied". The
  // button flip is itself proof writeText resolved, so a headless-clipboard
  // read failure must NOT hard-fail the suite (the app worked) — fall back to
  // the button-state verification and record the read as best-effort.
  await page.click('text=Copy')
  await page.waitForSelector('text=Copied', { timeout: 10000 })
  const clip = await page.evaluate(() => navigator.clipboard.readText().catch(() => null))
  if (clip !== null) {
    out.clipboardMatches = clip === fullKey
    if (!out.clipboardMatches) failures.push(`clipboard did not match the key (got ${clip.length} chars)`)
    out.steps.push('Copy → clipboard matches + button flipped to "Copied"')
  } else {
    out.clipboardMatches = null // read unavailable — button flip verified the copy
    out.steps.push('Copy → button flipped to "Copied" (clipboard read unavailable in headless)')
  }

  // 4) Reload → only the prefix + date remain; the secret never comes back
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.waitForSelector('text=Current key:', { timeout: 30000 })
  const bodyAfterReload = await page.evaluate(() => document.body.innerText)
  out.afterReloadHasFullKey = bodyAfterReload.includes(fullKey)
  out.afterReloadHasPrefix = bodyAfterReload.includes(fullKey.slice(0, 10))
  if (out.afterReloadHasFullKey) failures.push('full key leaked again after reload!')
  if (!out.afterReloadHasPrefix) failures.push('prefix not shown after reload')
  if (!bodyAfterReload.includes('Generating a new key revokes the old one')) {
    failures.push('"revokes the old one" hint missing after reload')
  }
  out.steps.push('reload → prefix + date only, secret not recoverable')

  // 5) Revoke → key gone, Generate button returns
  await page.click('text=Revoke')
  await page.waitForSelector('text=Generate API key', { timeout: 30000 })
  out.steps.push('Revoke → button reset to "Generate API key"')

  // 6) Fail-closed: the revoked key must 401 on an authed endpoint
  const res = await fetch(`${BASE}/files`, {
    headers: { Authorization: `Bearer ${fullKey}` },
  })
  out.revokedKeyStatus = res.status
  if (res.status !== 401) failures.push(`revoked key returned ${res.status}, expected 401`)
  out.steps.push('revoked key → 401 on /files (fail-closed)')

  if (out.pageErrors.length) failures.push(`JS page errors: ${out.pageErrors.join(' | ')}`)
  // A page RELOAD aborts the old page's still-pending requests
  // (net::ERR_ABORTED) — that's navigation, not a network failure. Filter them
  // out of the hard assertion (they're still recorded above for visibility).
  const realFailures = out.failedRequests.filter((f) => !f.includes('net::ERR_ABORTED'))
  if (realFailures.length) failures.push(`failed requests: ${realFailures.join(' | ')}`)

  // Sanity: the pre-revoke state actually called the endpoints we expect
  const createCall = out.apiCalls.find((c) => c.includes('POST /auth/api-key'))
  const revokeCall = out.apiCalls.find((c) => c.includes('DELETE /auth/api-key'))
  if (!createCall) failures.push('no POST /auth/api-key observed in the UI flow')
  if (!revokeCall) failures.push('no DELETE /auth/api-key observed in the UI flow')

  await context.close()
} catch (e) {
  failures.push(`suite error: ${e.message}`)
} finally {
  await cleanup({ token: user?.token })
  await browser.close()
}

const passed = report(out, failures)
process.exit(passed ? 0 : 1)
