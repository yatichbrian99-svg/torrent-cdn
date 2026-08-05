/**
 * E2E: two-factor authentication (TOTP) — enable, password login, code.
 *
 * FULL mode (E2E_EMAIL + E2E_PASSWORD of an email-VERIFIED account) drives the
 * complete user journey against the live UI:
 *   1. password login (no MFA yet) → session token
 *   2. GET /auth/mfa → disabled
 *   3. POST /auth/mfa/setup → base32 secret + otpauth URI + 10 backup codes
 *   4. compute the TOTP from the secret (RFC 6238, node:crypto) → enable
 *   5. the pre-MFA session is rotated out (401) — enabling must kick sessions
 *   6. real browser: password login → the 6-digit step appears → a WRONG code
 *      surfaces the real 401 detail → the computed code completes the login
 *   7. backup-code login works once; replaying the same code fails (single-use)
 *   8. cleanup: disable MFA with a fresh TOTP, then verify password login
 *      works again without a code (the borrowed account is left MFA-free)
 *
 * BOUNDARY mode (no E2E_EMAIL) is the production-safe subset that needs no
 * verified email. It registers a throwaway and exercises the MFA API contract
 * WITHOUT enabling — enabling rotates the session, and production gates
 * password login on email verification, so a throwaway could not get back in
 * and would be stranded:
 *   setup → secret/URI/backup shapes; enable with a WRONG code → 400 and the
 *   flag stays off; the account keeps working normally; deleted in cleanup.
 *
 * Env:
 *   E2E_EMAIL + E2E_PASSWORD   verified account (full journey; NOT deleted)
 *   E2E_TOKEN                  use this token instead of registering (boundary)
 *   E2E_BASE_URL / E2E_API_URL see helpers.mjs
 */
import { createHmac } from 'node:crypto'
import { chromium } from 'playwright'
import { BASE, api, registerUser, cleanup, report } from './helpers.mjs'

const EMAIL = process.env.E2E_EMAIL
const PASSWORD = process.env.E2E_PASSWORD || 'testpass123'

// --- RFC 6238 TOTP (stdlib only: node:crypto) -----------------------------
const BASE32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

function base32Decode(str) {
  const s = str.toUpperCase().replace(/=+$/, '')
  let bits = 0
  let value = 0
  const out = []
  for (const c of s) {
    const idx = BASE32_ALPHABET.indexOf(c)
    if (idx === -1) throw new Error(`invalid base32 char: ${c}`)
    value = (value << 5) | idx
    bits += 5
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff)
      bits -= 8
    }
  }
  return Buffer.from(out)
}

/** 6-digit TOTP for `secret` at the current 30s window. */
function totp(secret) {
  const counter = BigInt(Math.floor(Date.now() / 1000 / 30))
  const buf = Buffer.alloc(8)
  buf.writeBigUInt64BE(counter)
  const h = createHmac('sha1', base32Decode(secret)).update(buf).digest()
  const offset = h[h.length - 1] & 0x0f
  return ((h.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).toString().padStart(6, '0')
}

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

    const status = await api('/auth/mfa', { token: authToken })
    push('GET /auth/mfa on a fresh account', status.ok && status.data.enabled === false,
      `expected enabled:false, got ${status.status} ${JSON.stringify(status.data).slice(0, 120)}`)

    const setup = await api('/auth/mfa/setup', { method: 'POST', token: authToken })
    const s = setup.data
    push('POST /auth/mfa/setup returns secret + URI + 10 backup codes',
      setup.ok &&
        /^[A-Z2-7]{32}$/.test(s.secret) &&
        s.otpauth_uri.startsWith('otpauth://totp/TorrentCDN:') &&
        s.otpauth_uri.includes(`secret=${s.secret}`) &&
        Array.isArray(s.backup_codes) && s.backup_codes.length === 10 &&
        s.backup_codes.every((c) => /^[A-Z2-9]{10}$/.test(c)),
      `setup shape wrong: ${setup.status} ${JSON.stringify(s).slice(0, 200)}`)

    // Wrong code must be rejected (400) and must NOT flip the flag or rotate
    // the session — this is what lets the suite run safely without enable.
    const bad = await api('/auth/mfa/enable', { method: 'POST', token: authToken, body: { code: '000000' } })
    push('enable with a wrong code → 400, flag stays off, session survives',
      bad.status === 400 &&
        (await api('/auth/mfa', { token: authToken })).data.enabled === false &&
        (await api('/auth/me', { token: authToken })).ok,
      `wrong-code enable did not fail safe: ${bad.status} ${JSON.stringify(bad.data).slice(0, 120)}`)

    // Full journey (enable → password login → TOTP) requires a verified email;
    // that is exercised in FULL mode with E2E_EMAIL.
    out.steps.push('(boundary mode: successful enable + TOTP login need a VERIFIED account — run with E2E_EMAIL/E2E_PASSWORD)')

    // ======================================================================
    // FULL mode — the complete user journey.
    // ======================================================================
  } else {
    // 0) Pre-flight: the account must be verified and MFA-free. A verified
    //    account logs in with the password alone (no MFA yet).
    const first = await api('/auth/login', { ...API, body: { email: EMAIL, password: PASSWORD } })
    if (first.status === 403 && String(first.data.detail || '').includes('not verified')) {
      throw new Error('E2E_EMAIL account is NOT email-verified — the full MFA journey requires a verified account (verify its email first)')
    }
    if (first.data?.mfa_required) {
      throw new Error('E2E_EMAIL account already has MFA enabled — start from an MFA-free account (disable it first)')
    }
    push('password login before MFA → session token', first.ok && !!first.data.access_token,
      `login failed: ${first.status} ${JSON.stringify(first.data).slice(0, 120)}`)
    const preToken = first.data.access_token

    // 1) Enable MFA: setup → computed TOTP → enable.
    const setup = await api('/auth/mfa/setup', { method: 'POST', token: preToken })
    const s = setup.data
    push('setup returns secret + otpauth URI + 10 backup codes',
      setup.ok && /^[A-Z2-7]{32}$/.test(s.secret) && s.otpauth_uri.startsWith('otpauth://totp/TorrentCDN:') && s.backup_codes.length === 10,
      `setup failed: ${setup.status} ${JSON.stringify(s).slice(0, 200)}`)

    const code = totp(s.secret)
    const enable = await api('/auth/mfa/enable', { method: 'POST', token: preToken, body: { code } })
    push('enable with the computed TOTP → 200', enable.ok,
      `enable failed: ${enable.status} ${JSON.stringify(enable.data).slice(0, 200)}`)

    // 2) Session rotation: the pre-MFA token must now be dead.
    const oldDead = await api('/auth/me', { token: preToken })
    push('pre-MFA session rotated out (401)', oldDead.status === 401,
      `old token still valid after enable: ${oldDead.status}`)

    // 3) Real browser: password → 6-digit step → wrong code (real 401) → TOTP.
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
    push('second password login → mfa_required + mfa_token (no session yet)',
      bgLogin.ok && bgLogin.data.mfa_required && !!bgLogin.data.mfa_token,
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
    //    then verify password login works without any code.
    const disable = await api('/auth/mfa/disable', { method: 'POST', token: sessionToken, body: { code: totp(s.secret) } })
    push('disable MFA with a fresh TOTP', disable.ok, `disable failed: ${disable.status} ${JSON.stringify(disable.data).slice(0, 160)}`)
    const after = await api('/auth/login', { ...API, body: { email: EMAIL, password: PASSWORD } })
    push('password login works again after disable (no code step)',
      after.ok && !!after.data.access_token && !after.data.mfa_required,
      `login after disable: ${after.status} ${JSON.stringify(after.data).slice(0, 160)}`)
    out.steps.push('MFA disabled — borrowed account restored to its original state (not deleted)')
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
