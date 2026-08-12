/**
 * E2E: last-backup-code hint (browser proof).
 *
 * The backend reports `backup_codes_remaining` after a backup-code login, and
 * the AuthModal must hold open with a warning when that count hits 0. This
 * drives the REAL login UI with the FINAL backup code and asserts:
 *   1. the confirm consumes the code and the token is stored,
 *   2. the modal does NOT close — the "You've used your last backup code"
 *      panel renders instead (with a "Continue to dashboard" button),
 *   3. Continue completes the login (close + reload → logged-in navbar),
 *   4. /auth/mfa status now reports 0 remaining,
 *   5. no JS page errors / non-resource console errors.
 *
 * Setup: registers a throwaway account (mandatory MFA → born with 10 codes),
 * consumes 9 of them via API confirm, then logs in through the UI with the
 * 10th. Requires the local/prod backend to answer the mandatory-MFA gate.
 */
import { chromium } from 'playwright'
import { BASE, api, totp, cleanup, report } from './helpers.mjs'

const failures = []
const out = { suite: 'last-backup-code', steps: [], consoleErrors: [], pageErrors: [] }
const browser = await chromium.launch({ headless: true })
let page = null

try {
  // 1) Fresh account born MFA-enabled (mandatory MFA): register → setup → confirm.
  const email = `lastcode-${Date.now()}@example.com`
  const reg = await api('/auth/register', {
    method: 'POST', body: { email, password: 'testpass123', terms_accepted: true },
  })
  if (!reg.ok || !reg.data.mfa_token) {
    throw new Error(`register failed: ${reg.status} ${JSON.stringify(reg.data).slice(0, 150)}`)
  }
  const setup = await api('/auth/mfa/setup', { method: 'POST', token: reg.data.mfa_token })
  const secret = setup.data.secret
  const codes = setup.data.backup_codes
  if (!/^[A-Z2-7]{32}$/.test(secret || '') || !Array.isArray(codes) || codes.length !== 10) {
    throw new Error(`bad setup payload: ${JSON.stringify(setup.data).slice(0, 200)}`)
  }
  const enroll = await api('/auth/mfa/confirm', {
    method: 'POST', body: { mfa_token: reg.data.mfa_token, code: await totp(secret) },
  })
  if (!enroll.ok || !enroll.data.access_token) {
    throw new Error(`enroll confirm failed: ${enroll.status} ${JSON.stringify(enroll.data).slice(0, 150)}`)
  }
  const sessionToken = enroll.data.access_token
  out.steps.push('registered + enrolled (10 backup codes)')

  // 2) Consume 9 of the 10 codes via API (login → confirm each), leaving 1.
  for (let i = 0; i < 9; i++) {
    const lg = await api('/auth/login', { method: 'POST', body: { email, password: 'testpass123' } })
    if (!lg.ok || !lg.data.mfa_token) {
      throw new Error(`login #${i} failed: ${lg.status} ${JSON.stringify(lg.data).slice(0, 120)}`)
    }
    const c = await api('/auth/mfa/confirm', {
      method: 'POST', body: { mfa_token: lg.data.mfa_token, code: codes[i] },
    })
    if (!c.ok) {
      throw new Error(`consume #${i} failed: ${c.status} ${JSON.stringify(c.data).slice(0, 150)}`)
    }
  }
  const status = await api('/auth/mfa', { token: sessionToken })
  if (status.data.backup_codes_remaining !== 1) {
    throw new Error(`expected 1 code remaining after 9 consumed, got ${JSON.stringify(status.data)}`)
  }
  out.steps.push('consumed 9 backup codes via API (1 remains)')

  // 3) Real browser: log in with the LAST code → the modal must hold open.
  page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  page.on('console', (m) => { if (m.type() === 'error') out.consoleErrors.push(m.text().slice(0, 150)) })
  page.on('pageerror', (e) => out.pageErrors.push(String(e).slice(0, 200)))

  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.waitForSelector('nav >> text=Sign up', { timeout: 30000 })
  await page.click('nav >> text=Login')
  await page.waitForSelector('input[type="email"]', { timeout: 15000 })
  await page.fill('input[type="email"]', email)
  await page.fill('input[type="password"]', 'testpass123')
  await page.click('button[type="submit"]')
  await page.waitForSelector('input[placeholder="000000"]', { timeout: 45000 })
  out.steps.push('password login → 6-digit step appeared')

  await page.fill('input[placeholder="000000"]', codes[9])
  await page.click('button:has-text("Verify code")')

  // The login succeeded (token stored) but the modal stays open with the
  // warning instead of closing/reloading immediately.
  await page.waitForSelector("text=You've used your last backup code", { timeout: 30000 })
  const tokenStored = await page.evaluate(() => !!sessionStorage.getItem('torrent_cdn_token'))
  const continueVisible = (await page.locator('button:has-text("Continue to dashboard")').count()) > 0
  out.steps.push('last code consumed → warning panel shown, modal held open')
  if (!tokenStored) failures.push('token NOT stored after last-code login')
  if (!continueVisible) failures.push('Continue to dashboard button missing')

  // 4) Continue → close + reload → logged-in navbar.
  await page.click('button:has-text("Continue to dashboard")')
  await page.waitForSelector('nav >> text=Logout', { timeout: 30000 })
  out.steps.push('Continue to dashboard → logged-in navbar')

  // 5) Backend agrees: 0 codes left.
  const after = await api('/auth/mfa', { token: sessionToken })
  if (after.data.backup_codes_remaining !== 0) {
    failures.push(`expected 0 remaining after the last code, got ${JSON.stringify(after.data)}`)
  }
  out.steps.push('status reports 0 backup codes remaining')

  if (out.pageErrors.length) failures.push(`JS page errors: ${out.pageErrors.join(' | ')}`)
} catch (e) {
  failures.push(`suite error: ${e.message}`)
  if (page) out.finalBodyTail = (await page.evaluate(() => document.body.innerText).catch(() => '')).slice(-400)
} finally {
  // Delete the throwaway account this run created (best-effort, like the other
  // suites) — never leave unverified accounts behind on a prod/post-deploy run.
  if (typeof sessionToken === 'string') await cleanup({ token: sessionToken })
  await browser.close()
}

const passed = report(out, failures)
process.exit(passed ? 0 : 1)
