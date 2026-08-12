/**
 * E2E: two-factor authentication (TOTP) — enable, password login, code.
 *
 * FULL mode (E2E_EMAIL + E2E_PASSWORD of an email-VERIFIED account) drives the
 * complete user journey against the live UI. With mandatory MFA (production
 * default, MFA_REQUIRED) the journey is:
 *   1. password login on an MFA-free account → mfa_required + needs_setup,
 *      with a short-lived mfa_token and NO session token
 *   2. POST /auth/mfa/setup authenticated by the mfa_token → base32 secret +
 *      otpauth URI + 10 backup codes (no session exists yet)
 *   3. compute the TOTP from the secret (RFC 6238, node:crypto), anchored to
 *      the SERVER's clock via the HTTP Date header (a runner with a skewed
 *      clock — e.g. a CI box +3h — would otherwise generate codes ~360 steps
 *      off and every confirm would fail) → POST /auth/mfa/confirm enrolls the
 *      account AND returns the session token in the same response
 *   4. real browser: password login → the 6-digit step appears → a WRONG code
 *      surfaces the real 401 detail → the computed code completes the login
 *   5. backup-code login works once; replaying the same code fails (single-use)
 *   6. cleanup: disable MFA with a fresh TOTP, then verify password login
 *      gates again with needs_setup (the borrowed account is left MFA-free)
 *
 * BOUNDARY mode (no E2E_EMAIL) is the production-safe subset that needs no
 * verified email. It registers a throwaway — which, with mandatory MFA, forces
 * the account to be enrolled in 2FA during signup (register → mfa_token →
 * setup → TOTP confirm, no session until then) — and asserts the account was
 * born MFA-enabled and its session works; the throwaway is deleted in cleanup.
 * The full login second-factor journey (wrong code → real 401 → TOTP completes
 * login) needs a VERIFIED account, so it lives in FULL mode below.
 *
 * Env:
 *   E2E_EMAIL + E2E_PASSWORD   verified account (full journey; NOT deleted)
 *   E2E_TOKEN                  use this token instead of registering (boundary)
 *   E2E_BASE_URL / E2E_API_URL see helpers.mjs
 */
import { chromium } from 'playwright'
import { BASE, api, registerUser, cleanup, report, totp } from './helpers.mjs'

const EMAIL = process.env.E2E_EMAIL
const PASSWORD = process.env.E2E_PASSWORD || 'testpass123'

const failures = []
const out = {
  suite: 'mfa-flow',
  mode: EMAIL ? 'full' : 'boundary',
  steps: [],
  consoleErrors: [],
  pageErrors: [],
  registered: false,
}
const browser = await chromium.launch({ headless: true })
let user = null
let page = null

function push(step, cond, msg) {
  out.steps.push(step)
  if (!cond) failures.push(msg)
}

try {
  const API = { method: 'POST', headers: { 'Content-Type': 'application/json' } }

  // ======================================================================
  // BOUNDARY mode — production-safe contract test on a throwaway account.
  // ======================================================================
  if (!EMAIL) {
    const token = process.env.E2E_TOKEN
    if (!token) {
      user = await registerUser('mfa-flow')
      out.registered = true
    }
    const authToken = token || user.token

    // Mandatory MFA at registration: registerUser above had to walk the
    // enrollment gate (setup via the register-issued mfa_token → TOTP confirm)
    // to obtain a session at all. A regression that stops gating registration
    // would skip enrollment and the enabled:true assertion below would fail.
    if (!token) {
      const status = await api('/auth/mfa', { token: authToken })
      push('registration enrolled the account (MFA enabled from birth)',
        status.ok && status.data.enabled === true,
        `expected enabled:true after registration, got ${status.status} ${JSON.stringify(status.data).slice(0, 120)}`)
    } else {
      const status = await api('/auth/mfa', { token: authToken })
      push('GET /auth/mfa returns an enabled boolean (shared E2E_TOKEN account)',
        status.ok && typeof status.data.enabled === 'boolean',
        `unexpected mfa status: ${status.status} ${JSON.stringify(status.data).slice(0, 120)}`)
    }
    const me = await api('/auth/me', { token: authToken })
    push('enrolled registration session is valid (GET /auth/me → 200)', me.ok,
      `session dead: ${me.status} ${JSON.stringify(me.data).slice(0, 120)}`)

    // The login second-factor journey (wrong code → real 401 → TOTP completes
    // login) requires a verified email; that is exercised in FULL mode.
    out.steps.push('(boundary mode: the login second-factor journey needs a VERIFIED account — run with E2E_EMAIL/E2E_PASSWORD)')

    // ======================================================================
    // FULL mode — the complete user journey.
    // ======================================================================
  } else {
    // 0) Pre-flight: the account must be verified and MFA-free. Mandatory MFA
    //    means the first password login canNOT mint a session — it answers
    //    mfa_required + needs_setup with a short-lived mfa_token.
    const first = await api('/auth/login', { ...API, body: { email: EMAIL, password: PASSWORD } })
    if (first.status === 403 && String(first.data.detail || '').includes('not verified')) {
      throw new Error('E2E_EMAIL account is NOT email-verified — the full MFA journey requires a verified account (verify its email first)')
    }
    if (!(first.ok && first.data.mfa_required && first.data.needs_setup === true && first.data.mfa_token)) {
      // needs_setup=false here means the account already HAS MFA — the login
      // correctly returned the plain code step, but the suite wants to start
      // from an MFA-free account.
      throw new Error('E2E_EMAIL account is not in the MFA-free state the journey starts from (disable MFA first)')
    }
    push('password login without MFA → mfa_required + needs_setup (no session yet)',
      first.ok && first.data.needs_setup === true && !first.data.access_token,
      `login failed: ${first.status} ${JSON.stringify(first.data).slice(0, 120)}`)
    const mfaToken = first.data.mfa_token

    // 1) Enroll mid-login: setup is authenticated by the mfa_token — the user
    //    has no session token yet.
    const setup = await api('/auth/mfa/setup', { method: 'POST', token: mfaToken })
    const s = setup.data
    push('setup (via mfa_token) returns secret + otpauth URI + 10 backup codes',
      setup.ok && /^[A-Z2-7]{32}$/.test(s.secret) && s.otpauth_uri.startsWith('otpauth://totp/TorrentCDN:') && s.backup_codes.length === 10,
      `setup failed: ${setup.status} ${JSON.stringify(s).slice(0, 200)}`)

    // 2) Confirm with the computed TOTP: this BOTH enrolls the account and
    //    mints the real session token (enrollment completes the login).
    const code = await totp(s.secret)
    const confirm = await api('/auth/mfa/confirm', {
      method: 'POST', body: { mfa_token: mfaToken, code },
    })
    push('confirm with the computed TOTP enrolls + returns a session token',
      confirm.ok && !!confirm.data.access_token,
      `confirm failed: ${confirm.status} ${JSON.stringify(confirm.data).slice(0, 200)}`)
    const preToken = confirm.data.access_token
    const me = await api('/auth/me', { token: preToken })
    push('enrolled session token is valid (GET /auth/me → 200)', me.ok,
      `enrolled session dead: ${me.status}`)

    // 3) Real browser: password → 6-digit step → wrong code (real 401) → TOTP.
    //    The account is now enrolled, so the browser sees the plain code step.
    page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
    page.on('console', (m) => { if (m.type() === 'error') out.consoleErrors.push(m.text().slice(0, 150)) })
    page.on('pageerror', (e) => out.pageErrors.push(String(e).slice(0, 200)))

    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 60000 })
    await page.waitForSelector('nav >> text=Sign up', { timeout: 30000 })
    await page.click('nav >> text=Login')
    await page.waitForSelector('input[type="email"]', { timeout: 15000 })

    await page.fill('input[type="email"]', EMAIL)
    await page.fill('input[type="password"]', PASSWORD)
    await page.click('button[type="submit"]')

    // The password half succeeds → the modal switches to the code step.
    await page.waitForSelector('input[placeholder="000000"]', { timeout: 45000 })
    out.steps.push('password login → the 6-digit two-factor step appeared (no token yet)')

    // Wrong code → the real 401 detail is surfaced in the modal.
    await page.fill('input[placeholder="000000"]', '000000')
    await page.click('button:has-text("Verify code")')
    await page.waitForSelector('text=Invalid authentication code', { timeout: 30000 })
    out.steps.push('wrong code → "Invalid authentication code" (real 401 detail)')

    // Correct computed code → logged in.
    await page.fill('input[placeholder="000000"]', code)
    await page.click('button:has-text("Verify code")')
    await page.waitForSelector('nav >> text=Logout', { timeout: 45000 })
    const sessionToken = await page.evaluate(() => sessionStorage.getItem('torrent_cdn_token'))
    push('TOTP completes the login (logged-in navbar + token stored)', !!sessionToken,
      'logged-in navbar not reached after the correct TOTP')
    out.steps.push('computed TOTP → logged in')

    // 4) Backup codes: one-time use, replay rejected.
    const bgLogin = await api('/auth/login', { ...API, body: { email: EMAIL, password: PASSWORD } })
    push('second password login → mfa_required (enrolled, needs_setup false) + mfa_token',
      bgLogin.ok && bgLogin.data.mfa_required && bgLogin.data.needs_setup === false && !!bgLogin.data.mfa_token,
      `expected mfa_required, got ${bgLogin.status} ${JSON.stringify(bgLogin.data).slice(0, 120)}`)
    const bg = await api('/auth/mfa/confirm', {
      method: 'POST', body: { mfa_token: bgLogin.data.mfa_token, code: s.backup_codes[0] },
    })
    push('backup code completes the login', bg.ok && !!bg.data.access_token,
      `backup-code confirm failed: ${bg.status} ${JSON.stringify(bg.data).slice(0, 160)}`)
    const replay = await api('/auth/mfa/confirm', {
      method: 'POST', body: { mfa_token: bgLogin.data.mfa_token, code: s.backup_codes[0] },
    })
    push('replaying the same backup code → 401 (single-use)', replay.status === 401,
      `backup code was reusable: ${replay.status} ${JSON.stringify(replay.data).slice(0, 120)}`)

    // 5) Cleanup: disable MFA with a fresh TOTP (rotates the session again),
    //    then verify the login gates again with needs_setup — mandatory MFA
    //    means an MFA-free account can never skip enrollment. The borrowed
    //    account is left MFA-free, its original state.
    const disable = await api('/auth/mfa/disable', { method: 'POST', token: sessionToken, body: { code: await totp(s.secret) } })
    push('disable MFA with a fresh TOTP', disable.ok, `disable failed: ${disable.status} ${JSON.stringify(disable.data).slice(0, 160)}`)
    const after = await api('/auth/login', { ...API, body: { email: EMAIL, password: PASSWORD } })
    push('password login after disable → MFA gate again (needs_setup, no session)',
      after.ok && after.data.mfa_required && after.data.needs_setup === true && !after.data.access_token,
      `login after disable: ${after.status} ${JSON.stringify(after.data).slice(0, 160)}`)
    out.steps.push('MFA disabled — borrowed account restored to its original MFA-free state (not deleted)')
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
