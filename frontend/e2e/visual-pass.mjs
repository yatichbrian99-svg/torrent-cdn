/**
 * E2E: visual pass — landing, docs, and (logged-in) dashboard.
 *
 * Loads each page in a real headless Chromium against the live site, waits for
 * its key content to render, captures a full-page screenshot (plus a mobile
 * shot of the landing page), and reports every console/page error. This is the
 * "does it still LOOK right" layer on top of the functional suites.
 *
 * Screenshots land in e2e/.artifacts/visual-*.png (gitignored).
 * Env: E2E_TOKEN reuses an existing account; otherwise a throwaway account is
 * registered for the logged-in dashboard view and deleted in cleanup.
 */
import { chromium } from 'playwright'
import path from 'node:path'
import { BASE, ARTIFACTS_DIR, registerUser, cleanup, attachDiagnostics, report } from './helpers.mjs'

const failures = []
const out = {
  suite: 'visual-pass',
  pages: [],
  consoleErrors: [],
  pageErrors: [],
  failedRequests: [],
}
const browser = await chromium.launch({ headless: true })
let user = null

try {
  const token = process.env.E2E_TOKEN
  if (!token) user = await registerUser('visual-pass')
  const authToken = token || user.token

  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await context.addInitScript((tok) => {
    window.sessionStorage.setItem('torrent_cdn_token', tok || '')
  }, authToken)
  const page = await context.newPage()
  attachDiagnostics(page, out)

  // Retry transient navigation failures once (bounded). Long-lived headless
  // Chromium under CI load intermittently aborts a goto (net::ERR_ABORTED) or
  // races the client-side auth redirect ("interrupted by another navigation" —
  // seen on /dashboard). A real missing feature fails both attempts with the
  // same timeout; a one-off abort passes on the retry, mirroring how
  // login-flow waits out its async race instead of asserting immediately.
  const check = async (name, pathname, keyText, retries = 1) => {
    const entry = { name, url: `${BASE}${pathname}` }
    for (let attempt = 0; ; attempt++) {
      try {
        await page.goto(entry.url, { waitUntil: 'domcontentloaded', timeout: 60000 })
        if (keyText) {
          await page.waitForSelector(`text=${keyText}`, { timeout: 30000 })
          entry.keyTextFound = true
        }
        await page.waitForTimeout(1500) // let late hydration/analytics settle
        entry.screenshot = path.join(ARTIFACTS_DIR, `visual-${name}.png`)
        await page.screenshot({ path: entry.screenshot, fullPage: true })
        out.pages.push(entry)
        return entry
      } catch (e) {
        const msg = String(e.message)
        if (attempt < retries && /net::ERR_|interrupted by another navigation/i.test(msg)) {
          entry.error = `${msg.split('\n')[0].slice(0, 120)} (retrying)`
          await page.waitForTimeout(1500)
          continue
        }
        entry.keyTextFound = false
        entry.error = msg.split('\n')[0].slice(0, 160)
        failures.push(`${name}: key content "${keyText}" never rendered (${entry.error})`)
        out.pages.push(entry)
        return entry
      }
    }
  }

  // Logged-out public pages
  const landing = await check('landing', '/', 'How it works')
  await check('docs', '/docs', 'API Reference')

  // Logged-in dashboard (seeded token) — the navbar must flip to logged-in too
  const dash = await check('dashboard', '/dashboard', 'Generate API key')
  if (!dash.error) {
    const loggedInNav = (await page.locator('nav >> text=Logout').count()) > 0
    dash.navbarLoggedIn = loggedInNav
    if (!loggedInNav) failures.push('dashboard: navbar did not show the logged-in state')
  }

  // Mobile landing shot (responsive check)
  const mobile = await browser.newContext({ viewport: { width: 375, height: 812 } })
  const mpage = await mobile.newPage()
  await mpage.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await mpage.waitForSelector('text=How it works', { timeout: 30000 })
  await mpage.waitForTimeout(1000)
  const mobileShot = path.join(ARTIFACTS_DIR, 'visual-landing-mobile.png')
  await mpage.screenshot({ path: mobileShot, fullPage: true })
  out.pages.push({ name: 'landing-mobile', url: `${BASE}/`, screenshot: mobileShot, keyTextFound: true })
  await mobile.close()

  // Error accounting: page errors are hard failures; console errors are
  // reported as-is (a failed optional-asset load is cosmetic, not a JS bug).
  if (out.pageErrors.length) failures.push(`JS page errors: ${out.pageErrors.join(' | ')}`)
  const nonResource = out.consoleErrors.filter((e) => !e.startsWith('Failed to load resource'))
  if (nonResource.length) failures.push(`console errors: ${nonResource.join(' | ')}`)
  out.screenshots = out.pages.map((p) => p.screenshot)
  await context.close()
} catch (e) {
  failures.push(`suite error: ${e.message}`)
} finally {
  if (user) await cleanup({ token: user.token })
  await browser.close()
}

const passed = report(out, failures)
process.exit(passed ? 0 : 1)
