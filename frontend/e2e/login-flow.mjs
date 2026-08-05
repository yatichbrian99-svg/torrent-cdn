/**
 * E2E: real login-form interaction on the landing page.
 *
 * Every other suite seeds sessionStorage directly; this one drives the ACTUAL
 * auth form the way a user does: open / → click the navbar "Login" button →
 * the AuthModal login form appears → type email + password → submit.
 *
 * Production gates password login on email verification (the register token
 * still works as a session, but /auth/login returns 403 for unverified
 * accounts). So the suite asserts the form surfaces the REAL backend state
 * for every outcome it can reach:
 *   - wrong password            → "Invalid email or password" (401)
 *   - correct password, unverified account → "Email not verified…" (403) —
 *     the modal must show the actual message, not a generic error
 *   - correct password, verified account   → modal closes, page reloads, the
 *     navbar flips to logged-in (Dashboard link, email, Logout) — only
 *     reachable with E2E_EMAIL of an account whose email was verified.
 *
 * Env:
 *   E2E_EMAIL + E2E_PASSWORD  login an existing account (skips registration;
 *                             the account is NOT deleted — it's shared/borrowed)
 *   (without them, a throwaway account is registered; it is unverified, so the
 *   run exercises the 401 + 403 paths and cleans itself up)
 */
import { chromium } from 'playwright'
import { BASE, registerUser, cleanup, report } from './helpers.mjs'

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

  // 4) Correct credentials → either the logged-in navbar (verified account) or
  //    the real 403 verification gate message (unverified account)
  await page.fill('input[type="password"]', PASSWORD)
  await page.click('button[type="submit"]')
  try {
    await page.waitForSelector('nav >> text=Logout', { timeout: 45000 })
    out.outcome = 'logged-in'
    out.tokenStored = await page.evaluate(() => !!sessionStorage.getItem('torrent_cdn_token'))
    out.modalClosed = !(await page.locator('input[type="email"]').count())
    out.navbarDashboard = (await page.locator('nav >> text=Dashboard').count()) > 0
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
