/**
 * E2E: real login-form interaction on the landing page.
 *
 * Every other suite seeds sessionStorage directly; this one drives the ACTUAL
 * auth form the way a user does: open / → click the navbar "Login" button →
 * the AuthModal login form appears → type email + password → submit.
 *
 * Production gates password login on email verification (the register token
 * still works as a session, but /auth/login returns 403 for unverified
 * accounts) AND on mandatory 2FA (a verified account's password login answers
 * mfa_required + a short-lived mfa_token, not a session). So the suite asserts
 * the form surfaces the REAL backend state for every outcome it can reach:
 *   - wrong password            → "Invalid email or password" (401)
 *   - correct password, unverified account → "Email not verified…" (403) —
 *     the modal must show the actual message, not a generic error
 *   - correct password, verified account   → the 6-digit two-factor step
 *     appears; with E2E_MFA_SECRET the suite computes the TOTP, completes the
 *     login, and asserts the navbar flips to logged-in (Dashboard, email,
 *     Logout). Without the secret it asserts the code step is surfaced.
 *
 * Env:
 *   E2E_EMAIL + E2E_PASSWORD  login an existing account (skips registration;
 *                             the account is NOT deleted — it's shared/borrowed)
 *   E2E_MFA_SECRET  TOTP secret of that account (ci-e2e exports it after the
 *                   shared-account enrollment) — completes the 2FA code step
 *   (without them, a throwaway account is registered; it is unverified, so the
 *   run exercises the 401 + 403 paths and cleans itself up)
 */
import { chromium } from 'playwright'
import { BASE, registerUser, cleanup, report, totp } from './helpers.mjs'

const EMAIL = process.env.E2E_EMAIL
const PASSWORD = process.env.E2E_PASSWORD || 'testpass123'

const failures = []
const out = {
  suite: 'login-flow',
  steps: [],
  consoleErrors: [],
  pageErrors: [],
  registered: false,
  outcome: null, // 'logged-in' | 'unverified-gate'
}
const browser = await chromium.launch({ headless: true })
let user = null
let page = null

try {
  if (!EMAIL) {
    user = await registerUser('login-flow')
    out.registered = true
  }
  const email = EMAIL || user.email

  page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  page.on('console', (m) => {
    if (m.type() === 'error') out.consoleErrors.push(m.text().slice(0, 150))
  })
  page.on('pageerror', (e) => out.pageErrors.push(String(e).slice(0, 200)))

  // 1) Landing page, logged out (navbar shows Login / Sign up)
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.waitForSelector('nav >> text=Sign up', { timeout: 30000 })
  out.steps.push('landing loaded, navbar shows logged-out state')

  // 2) Open the auth modal via the navbar Login button
  await page.click('nav >> text=Login')
  await page.waitForSelector('input[type="email"]', { timeout: 15000 })
  out.steps.push('auth modal opened (login form)')

  // 3) Wrong password → the real 401 message, not a generic failure
  await page.fill('input[type="email"]', email)
  await page.fill('input[type="password"]', 'wrong-password-123')
  await page.click('button[type="submit"]')
  await page.waitForSelector('text=Invalid email or password', { timeout: 30000 })
  out.steps.push('wrong password → "Invalid email or password" (real 401 detail)')

  // 4) Correct credentials → the logged-in navbar (verified account, MFA
  //    completed) or the real 403 verification gate message (unverified
  //    account). Mandatory 2FA inserts the 6-digit step between the password
  //    and the session — complete it with the computed TOTP when the shared
  //    account's secret is available (ci-e2e exports it after enrollment).
  await page.fill('input[type="password"]', PASSWORD)
  await page.click('button[type="submit"]')
  // Mandatory 2FA: a verified account's password login answers mfa_required,
  // so the modal switches to the 6-digit step instead of logging in directly.
  let mfaStep = false
  try {
    await page.waitForSelector('input[placeholder="000000"]', { timeout: 45000 })
    mfaStep = true
  } catch {}
  if (mfaStep) {
    out.steps.push('correct password on verified account → 6-digit two-factor step appeared')
    if (!process.env.E2E_MFA_SECRET) {
      failures.push('2FA step appeared but E2E_MFA_SECRET is not set — cannot complete the login')
    } else {
      const code = await totp(process.env.E2E_MFA_SECRET)
      await page.fill('input[placeholder="000000"]', code)
      await page.click('button:has-text("Verify code")')
      await page.waitForSelector('nav >> text=Logout', { timeout: 45000 })
      out.outcome = 'logged-in'
      out.tokenStored = await page.evaluate(() => !!sessionStorage.getItem('torrent_cdn_token'))
      out.modalClosed = !(await page.locator('input[placeholder="000000"]').count())
      out.navbarDashboard = (await page.locator('nav >> text=Dashboard').count()) > 0
      // The navbar email renders only after the async api.getMe() resolves (the
      // Logout/Dashboard links appear as soon as loggedIn flips true — BEFORE the
      // user object arrives). Wait for the email span to show, so the assertion
      // below samples the settled state, not the race window.
      try {
        await page.waitForFunction(() => {
          const span = [...document.querySelectorAll('nav span')].find((s) => s.textContent?.includes('@'))
          return !!span
        }, { timeout: 15000 })
      } catch {}
      out.navbarEmail = await page.evaluate(() => {
        const span = [...document.querySelectorAll('nav span')].find((s) => s.textContent?.includes('@'))
        return span?.textContent || null
      })
      out.steps.push(`logged in via 2FA (token stored, modal closed, navbar shows ${out.navbarEmail})`)
      if (!out.tokenStored) failures.push('token not stored after 2FA login')
      if (!out.modalClosed) failures.push('auth modal still open after 2FA login')
      if (!out.navbarDashboard) failures.push('Dashboard link missing after 2FA login')
      if (out.navbarEmail !== email) failures.push(`navbar email mismatch: ${out.navbarEmail} != ${email}`)
    }
  } else {
    // No 2FA step: either the logged-in navbar (MFA disabled in dev) or the
    // real 403 verification gate message (unverified account).
    try {
      await page.waitForSelector('nav >> text=Logout', { timeout: 45000 })
      out.outcome = 'logged-in'
      out.tokenStored = await page.evaluate(() => !!sessionStorage.getItem('torrent_cdn_token'))
      out.modalClosed = !(await page.locator('input[type="email"]').count())
      out.navbarDashboard = (await page.locator('nav >> text=Dashboard').count()) > 0
      // The navbar email renders only after the async api.getMe() resolves (the
      // Logout/Dashboard links appear as soon as loggedIn flips true — BEFORE the
      // user object arrives). Wait for the email span to show, so the assertion
      // below samples the settled state, not the race window.
      try {
        await page.waitForFunction(() => {
          const span = [...document.querySelectorAll('nav span')].find((s) => s.textContent?.includes('@'))
          return !!span
        }, { timeout: 15000 })
      } catch {}
      out.navbarEmail = await page.evaluate(() => {
        const span = [...document.querySelectorAll('nav span')].find((s) => s.textContent?.includes('@'))
        return span?.textContent || null
      })
      out.steps.push(`logged in (token stored, modal closed, navbar shows ${out.navbarEmail})`)
      if (!out.tokenStored) failures.push('token not stored after login')
      if (!out.modalClosed) failures.push('auth modal still open after login')
      if (!out.navbarDashboard) failures.push('Dashboard link missing after login')
      if (out.navbarEmail !== email) failures.push(`navbar email mismatch: ${out.navbarEmail} != ${email}`)
    } catch {
      // Not logged in — assert the REAL verification-gate message is surfaced.
      await page.waitForSelector('text=Email not verified', { timeout: 15000 })
      out.outcome = 'unverified-gate'
      const errText = await page.evaluate(() => document.body.innerText)
      out.gateMessageShown = errText.includes(
        'Email not verified. Please check your inbox or request a new verification link.',
      )
      out.steps.push('correct credentials on unverified account → real 403 gate message shown')
      if (!out.gateMessageShown) failures.push('verification-gate message not fully surfaced')
      if (EMAIL) {
        out.steps.push('(this account is unverified — provide E2E_EMAIL of a VERIFIED account to exercise the logged-in happy path)')
      }
    }
  }
  if (out.pageErrors.length) failures.push(`JS page errors: ${out.pageErrors.join(' | ')}`)
} catch (e) {
  failures.push(`suite error: ${e.message}`)
  if (page) out.finalBodyTail = (await page.evaluate(() => document.body.innerText).catch(() => '')).slice(-400)
} finally {
  if (user) await cleanup({ token: user.token }) // only delete an account THIS run created
  await browser.close()
}

const passed = report(out, failures)
process.exit(passed ? 0 : 1)
