/**
 * E2E: the TCDN-AI developer console, flag ON.
 *
 * Drives the real journey a developer takes, against a local stack:
 *
 *   login (real form) → overview → buy test credits → create API key →
 *   call the API with that key → see the job in usage
 *
 * Plus, on every console page: an axe scan, and screenshots at mobile / tablet /
 * desktop widths so the layouts can be read and judged, not assumed.
 *
 * Env:
 *   E2E_BASE_URL   frontend under test (default http://localhost:3000)
 *   E2E_API_URL    backend origin for the direct API call (default http://127.0.0.1:8000)
 *   E2E_EMAIL / E2E_PASSWORD   an existing account; otherwise one is registered
 *
 * The purchase runs through the simulated checkout seam, which only exists when
 * the backend is started with TCDN_AI_FAKE_CHECKOUT=1 in a non-production env.
 */
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import AxeBuilder from '@axe-core/playwright'
import { API_BASE, BASE, registerUser } from './helpers.mjs'

const SHOTS = path.join(path.dirname(fileURLToPath(import.meta.url)), 'screenshots')
const API = (process.env.E2E_API_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '')
const PASSWORD = process.env.E2E_PASSWORD || 'testpass123'

const VIEWPORTS = [
  { name: 'mobile', width: 375, height: 812 },
  { name: 'tablet', width: 768, height: 1024 },
  { name: 'desktop', width: 1280, height: 900 },
]

const PAGES = [
  { path: '/ai', label: 'overview' },
  { path: '/ai/keys', label: 'keys' },
  { path: '/ai/usage', label: 'usage' },
  { path: '/ai/models', label: 'models' },
  { path: '/ai/quickstart', label: 'quickstart' },
  { path: '/ai/billing', label: 'billing' },
  { path: '/ai/status', label: 'status' },
]

const results = { suite: 'ai-console', steps: [], axe: [], shots: [], consoleErrors: [], pageErrors: [] }
let failed = 0

function step(name, ok, detail = '') {
  results.steps.push({ name, ok, detail })
  if (!ok) failed++
  console.log(`${ok ? '[OK]' : '[FAIL]'} ${name}${detail ? ` — ${detail}` : ''}`)
}

const browser = await chromium.launch({ headless: true })
let page = null
let user = null
let apiKey = null

try {
  const email = process.env.E2E_EMAIL
  if (!email) {
    user = await registerUser('ai-console')
  }
  const loginEmail = email || user.email

  page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  page.on('console', (m) => {
    if (m.type() === 'error') results.consoleErrors.push(m.text().slice(0, 200))
  })
  page.on('pageerror', (e) => results.pageErrors.push(String(e).slice(0, 200)))

  // 1. Real login through the form.
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: /^login$/i }).first().click()
  // The modal's labels are not programmatically associated with their inputs
  // (pre-existing markup), so select by input type — the same selectors the
  // login-flow suite uses.
  await page.locator('input[type="email"]').fill(loginEmail)
  await page.locator('input[type="password"]').first().fill(PASSWORD)
  await page.locator('form button[type="submit"]').click()
  await page.getByRole('button', { name: /logout/i }).waitFor({ timeout: 20_000 })
  step('login through the form', true, loginEmail)

  // 2. The AI link appears only because the backend reports the flag on.
  const aiLink = page.getByRole('link', { name: /^AI$/ })
  await aiLink.waitFor({ timeout: 10_000 })
  step('AI nav link visible with the flag on', true)

  // 3. Overview.
  await page.goto(`${BASE}/ai`, { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: /inference credits, keys and usage/i }).waitFor()
  await page.getByText(/credit balance/i).first().waitFor()
  step('overview renders balance and checklist', true)

  // 4. Buy test credits (simulated provider).
  await page.goto(`${BASE}/ai/billing`, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: /^buy credits$/i }).first().click()
  const simulate = page.getByRole('button', { name: /complete test payment/i })
  await simulate.waitFor({ timeout: 15_000 })
  await simulate.click()
  await page.getByText(/credits added/i).waitFor({ timeout: 20_000 })
  const receiptText = await page.getByText(/credited · new balance/i).first().innerText()
  step('buy test credits', true, receiptText.replace(/\s+/g, ' ').slice(0, 80))

  // 5. Create an API key and capture it from the one-time panel.
  await page.goto(`${BASE}/ai/keys`, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: /^create key$/i }).click()
  const keyCode = page.locator('code').filter({ hasText: /^tcdn_ai_/ }).first()
  await keyCode.waitFor({ timeout: 15_000 })
  apiKey = (await keyCode.innerText()).trim()
  step('create API key (shown once)', apiKey.startsWith('tcdn_ai_'), apiKey.slice(0, 18) + '…')

  // 6. The key authenticates a real request to the gateway.
  const modelsResponse = await fetch(`${API}/api/ai/v1/models`, {
    headers: { Authorization: `Bearer ${apiKey}` },
  })
  step('created key authenticates against the API', modelsResponse.ok, `HTTP ${modelsResponse.status}`)

  const model = (await modelsResponse.json())[0]?.model_id
  const chat = await fetch(`${API}/v1/chat/completions`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model,
      messages: [{ role: 'user', content: 'ping' }],
      max_tokens: 8,
    }),
  })
  const chatBody = await chat.json().catch(() => ({}))
  const jobId = chatBody?.tcdn?.job_id
  step('gateway completes a chat request', chat.ok && Boolean(jobId), jobId ? `job ${jobId}` : `HTTP ${chat.status}`)

  // 7. "Test my key" in the browser (same key, one request).
  await page.goto(`${BASE}/ai/quickstart`, { waitUntil: 'networkidle' })
  await page.getByLabel(/^api key$/i).fill(apiKey)
  await page.getByRole('button', { name: /^test key$/i }).click()
  await page.getByText(/key works/i).waitFor({ timeout: 30_000 })
  step('quickstart "test my key" succeeds', true)

  // 8. The job shows up in usage, with tokens and cost, and opens.
  await page.goto(`${BASE}/ai/usage`, { waitUntil: 'networkidle' })
  const row = page.getByRole('link', { name: jobId })
  await row.waitFor({ timeout: 20_000 })
  step('job appears in usage', true)
  await row.click()
  await page.getByText(/verification/i).first().waitFor({ timeout: 15_000 })
  step('job detail renders verification and cost', true)

  // 9. Every page: axe + screenshots at three widths + no horizontal scroll.
  const storageState = await page.context().storageState()
  // The app keeps its session token in sessionStorage by default, which
  // storageState does not carry — seed it explicitly or every sized context is
  // signed out and /ai redirects to the landing page.
  const sessionToken = await page.evaluate(() => sessionStorage.getItem('torrent_cdn_token'))
  for (const viewport of VIEWPORTS) {
    // A fresh context per width: resizing a page created by browser.newPage()
    // is not allowed by Playwright, and a new context also proves the section
    // works from a cold browser at that size.
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      storageState,
    })
    if (sessionToken) {
      await context.addInitScript((token) => {
        try {
          window.sessionStorage.setItem('torrent_cdn_token', token)
        } catch {}
      }, sessionToken)
    }
    const sized = await context.newPage()
    sized.on('console', (m) => {
      if (m.type() === 'error') results.consoleErrors.push(m.text().slice(0, 200))
    })
    sized.on('pageerror', (e) => results.pageErrors.push(String(e).slice(0, 200)))
    for (const target of PAGES) {
      await sized.goto(`${BASE}${target.path}`, { waitUntil: 'networkidle' })
      await sized.waitForTimeout(400)
      // The gate renders a spinner (also inside .ai-console) before the section
      // mounts; scanning during that frame would measure nothing.
      await sized.locator('.ai-console').first().waitFor({ timeout: 15_000 })

      const overflow = await sized.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      )
      if (overflow > 1) {
        step(`no horizontal scroll — ${target.label} @ ${viewport.width}`, false, `${overflow}px overflow`)
      }

      const file = path.join(SHOTS, `ai-${target.label}-${viewport.name}.png`)
      await sized.screenshot({ path: file, fullPage: true })
      results.shots.push(file)

      if (viewport.name === 'desktop') {
        // Scope to the console subtree: the shared navbar/footer are page
        // chrome this change did not author, and their --text-muted contrast
        // (2.81:1 on black) is a pre-existing, app-wide finding recorded in the
        // evidence rather than quietly "fixed" by restyling every page.
        const { violations } = await new AxeBuilder({ page: sized })
          .include('.ai-console')
          .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
          .analyze()
        const serious = violations.filter((v) => ['serious', 'critical'].includes(v.impact))
        results.axe.push({
          page: target.path,
          violations: violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.length })),
        })
        step(
          `axe: no serious/critical violations — ${target.label}`,
          serious.length === 0,
          serious.map((v) => v.id).join(', ') || 'clean',
        )
      }
    }
    await context.close()
  }
} catch (error) {
  step('journey completed', false, String(error).slice(0, 300))
  if (page) await page.screenshot({ path: path.join(SHOTS, 'ai-failure.png'), fullPage: true }).catch(() => {})
} finally {
  await browser.close()
}

results.ok = failed === 0
results.registeredEmail = user?.email || null
console.log(JSON.stringify(results, null, 2))
process.exit(failed ? 1 : 0)
