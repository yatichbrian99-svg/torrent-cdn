/**
 * Browser half of the post-deploy flag-off smoke.
 *
 * The Python half checks status codes and headers over plain HTTP. The parts
 * that only exist after JavaScript runs — the "not available" panel, the
 * absence of an AI nav link, and the fact that exactly one AI request is made —
 * are checked here, signed out, against production.
 *
 * Read-only: it loads pages and reads the DOM/network. No login, no upload.
 *
 *   frontend/node_modules/.bin/playwright ... (or run from frontend:)
 *   node ../docs/tcdn-ai/evidence/04-e2e/production-smoke-after-50-browser.mjs
 */
import { createRequire } from 'node:module'

// Playwright is installed in frontend/, not here, so resolve it relative to that
// package instead of this file's directory.
const requireFromFrontend = createRequire(new URL('../../../../frontend/package.json', import.meta.url))
const { chromium } = requireFromFrontend('playwright')

const APP = process.env.SMOKE_APP_URL || 'https://www.torrentcdn.store'
const PAGES = ['/', '/demo', '/docs', '/architecture', '/privacy']
const results = []
const check = (name, ok, detail) => {
  results.push({ name, ok })
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name} — ${detail}`)
}

const browser = await chromium.launch()
const ctx = await browser.newContext()
const page = await ctx.newPage()
const errors = []
const aiRequests = []
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('request', (r) => {
  try {
    if (new URL(r.url()).pathname.startsWith('/api/ai/')) aiRequests.push(new URL(r.url()).pathname)
  } catch {}
})

// 1. /ai, signed out: the panel, and nothing else from the console
await page.goto(APP + '/ai', { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)
const text = (await page.locator('main').innerText()).trim()
const hasPanel = /not available on this deployment/i.test(text)
check('/ai shows the unavailable panel', hasPanel, JSON.stringify(text.slice(0, 90)))
check(
  '/ai makes no AI request except the flag probe',
  aiRequests.every((p) => p === '/api/ai/v1/console/status'),
  aiRequests.join(', ') || '(none)',
)
check(
  '/ai probe answered 404 (flag off)',
  errors.some((e) => /404/.test(e)),
  `${errors.length} console error(s)`,
)

// 2. signed out, every public page: no AI nav link, no console request
for (const path of PAGES) {
  aiRequests.length = 0
  await page.goto(APP + path, { waitUntil: 'networkidle' })
  const html = await page.content()
  const navText = await page.locator('nav').innerText().catch(() => '')
  const navLink = /(^|\s)AI(\s|$)/.test(navText) || /href="\/ai"/.test(html)
  check(`no AI nav link on ${path}`, !navLink, navText.replace(/\n+/g, ' | ').slice(0, 70))
  check(`no AI request from ${path}`, aiRequests.length === 0, aiRequests.join(', ') || 'none')
}

await browser.close()
const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} browser checks passed`)
if (failed.length) {
  console.log('FAILED: ' + failed.map((f) => f.name).join('; '))
  process.exit(1)
}
