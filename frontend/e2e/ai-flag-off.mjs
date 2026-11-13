/**
 * E2E: the site with ENABLE_TCDN_AI OFF must be unchanged.
 *
 * Three things are asserted, all of them observable:
 *
 * 1. the navigation has no link into the AI console (/ai, /ai/*) anywhere —
 *    the public marketing page /tcdn-ai is deliberately flag-independent
 *    (commit 2965c3a) and is allowed;
 * 2. /ai renders the "not available" panel rather than a broken console;
 * 3. the only AI-shaped request any page makes is the single feature probe
 *    (which 404s), and no other /api/ai or /v1 call is issued;
 * 4. the existing pages still render their expected headings and nav items.
 *
 * Env: E2E_BASE_URL (default http://localhost:3000), E2E_EMAIL, E2E_PASSWORD.
 */
import { chromium } from 'playwright'
import { BASE } from './helpers.mjs'

const PASSWORD = process.env.E2E_PASSWORD || 'testpass123'
const results = { suite: 'ai-flag-off', steps: [], aiRequests: [], consoleErrors: [] }
let failed = 0

function step(name, ok, detail = '') {
  results.steps.push({ name, ok, detail })
  if (!ok) failed++
  console.log(`${ok ? '[OK]' : '[FAIL]'} ${name}${detail ? ` — ${detail}` : ''}`)
}

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
const page = await context.newPage()
const aiRequests = []
page.on('request', (request) => {
  const url = new URL(request.url())
  if (url.pathname.startsWith('/api/ai') || url.pathname.startsWith('/v1/')) {
    aiRequests.push({ path: url.pathname, method: request.method() })
  }
})
page.on('console', (m) => {
  if (m.type() === 'error') results.consoleErrors.push(m.text().slice(0, 160))
})

try {
  // Signed out: the landing page must be untouched.
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  const navText = await page.locator('nav').first().innerText()
  // The public /tcdn-ai marketing page is not flag-gated (2965c3a), so the
  // contract is "no route into the console", not "no AI-ish text".
  const consoleLink = await page
    .locator('nav a')
    .evaluateAll((links) =>
      links
        .map((a) => new URL(a.href, location.href).pathname)
        .find((p) => p === '/ai' || p.startsWith('/ai/')),
    )
  step('no AI console link while signed out', !consoleLink, consoleLink || navText.replace(/\s+/g, ' ').slice(0, 80))
  step('landing heading unchanged', await page.getByText(/video delivery that gets cheaper/i).first().isVisible())

  // Sign in: the link is gated per-user too, and still absent with the flag off.
  if (process.env.E2E_EMAIL) {
    await page.getByRole('button', { name: /^login$/i }).first().click()
    await page.locator('input[type="email"]').fill(process.env.E2E_EMAIL)
    await page.locator('input[type="password"]').first().fill(PASSWORD)
    await page.locator('form button[type="submit"]').click()
    await page.getByRole('button', { name: /logout/i }).waitFor({ timeout: 20_000 })

    const signedInNav = await page.locator('nav').first().innerText()
    const signedInConsoleLink = await page
      .locator('nav a')
      .evaluateAll((links) =>
        links
          .map((a) => new URL(a.href, location.href).pathname)
          .find((p) => p === '/ai' || p.startsWith('/ai/')),
      )
    step('no AI console link when signed in', !signedInConsoleLink, signedInConsoleLink || signedInNav.replace(/\s+/g, ' ').slice(0, 80))
    step('dashboard nav intact', /Dashboard/.test(signedInNav) && /Billing/.test(signedInNav))

    // The page itself: an honest panel, not a half-built console or a redirect.
    await page.goto(`${BASE}/ai`, { waitUntil: 'networkidle' })
    await page.getByText(/not available on this deployment/i).waitFor({ timeout: 20_000 })
    step('AI deep link shows the unavailable panel', true)

    // Existing pages still work after that navigation.
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle' })
    step('dashboard still renders', await page.getByText(/dashboard/i).first().isVisible())
    await page.goto(`${BASE}/billing`, { waitUntil: 'networkidle' })
    // Wait for the plan card rather than sampling visibility mid-render.
    const plan = page.getByText(/current plan/i).first()
    await plan.waitFor({ timeout: 20_000 })
    step('billing still renders', await plan.isVisible())

    // The probe is the ONLY AI request, and it must be a 404.
    const probe = aiRequests.filter((r) => r.path === '/api/ai/v1/console/status')
    const others = aiRequests.filter((r) => r.path !== '/api/ai/v1/console/status')
    step('only the feature probe touches an AI path', others.length === 0, JSON.stringify(others.slice(0, 3)))
    step('probe was issued once per page load at most', probe.length <= 4, `${probe.length} probes`)
    const probeStatus = await page.evaluate(async () => {
      const response = await fetch('/api/ai/v1/console/status')
      return response.status
    })
    step('AI routes are 404 on this deployment', probeStatus === 404, `HTTP ${probeStatus}`)
  } else {
    step('signed-in assertions skipped (no E2E_EMAIL)', true)
  }

  results.aiRequests = aiRequests
  results.ok = failed === 0
} catch (error) {
  step('flag-off journey completed', false, String(error).slice(0, 300))
  results.ok = false
} finally {
  await browser.close()
}

console.log(JSON.stringify(results, null, 2))
process.exit(results.ok ? 0 : 1)
