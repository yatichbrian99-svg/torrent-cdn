// Local quality checks for the public /tcdn-ai page (not committed).
// Runs against `next start` on :3002 (production build, flag off, probe fails closed).
import { createRequire } from 'node:module'
const require = createRequire(process.cwd() + '/package.json')
const { chromium } = require('playwright')
const AxeBuilder = require('@axe-core/playwright').default

const BASE = 'http://127.0.0.1:3002'
const OUT = '/tmp/tcdn-ai-shots'
const results = []

function record(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`)
}

const browser = await chromium.launch()

// ── responsive screenshots, overflow, axe ───────────────────────────────────
for (const width of [375, 768, 1280]) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: 'dark' })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(String(e)))
  await page.goto(`${BASE}/tcdn-ai`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(400)

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  record(`no horizontal overflow @${width}`, overflow <= 0, `${overflow}px`)

  await page.screenshot({ path: `${OUT}/tcdn-ai-${width}-dark.png`, fullPage: true })

  const axe = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  record(
    `axe @${width}`,
    axe.violations.length === 0,
    axe.violations.map((v) => `${v.id}(${v.nodes.length})`).join(' ') || 'no violations',
  )
  record(`no page errors @${width}`, errors.length === 0, errors.join('; '))
  await ctx.close()
}

// light theme at 1280
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: 'light' })
  const page = await ctx.newPage()
  await page.addInitScript(() => localStorage.setItem('tcdn_theme', 'light'))
  await page.goto(`${BASE}/tcdn-ai`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/tcdn-ai-1280-light.png`, fullPage: true })
  await ctx.close()
}

// ── CTA: flag off (real probe) ──────────────────────────────────────────────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/tcdn-ai`, { waitUntil: 'networkidle' })
  const early = page.getByRole('link', { name: 'Request early access' })
  record('early-access link present', (await early.count()) === 2, `${await early.count()} occurrences`)
  const href = await early.first().getAttribute('href')
  record(
    'mailto href prefilled',
    href === 'mailto:support@torrentcdn.store?subject=TCDN-AI%20early%20access%20request',
    href || 'none',
  )
  record('no dashboard link with flag off', (await page.getByRole('link', { name: 'Open dashboard' }).count()) === 0)
  await ctx.close()
}

// ── CTA: flag on (probe stubbed — no flag-on backend in this run) ───────────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const page = await ctx.newPage()
  await page.route('**/api/ai/v1/console/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ feature_flag: { name: 'ENABLE_TCDN_AI', on: true, routes_mounted: true } }),
    }),
  )
  await page.goto(`${BASE}/tcdn-ai`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(600)
  const dash = page.getByRole('link', { name: 'Open dashboard' })
  record('dashboard link appears when probe says enabled', (await dash.count()) === 2, `${await dash.count()} (hero + hosts)`)
  record('dashboard link targets /ai', (await dash.first().getAttribute('href')) === '/ai')
  await page.screenshot({ path: `${OUT}/tcdn-ai-1280-flag-on.png`, fullPage: true })
  await ctx.close()
}

// ── keyboard order ──────────────────────────────────────────────────────────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/tcdn-ai`, { waitUntil: 'networkidle' })
  const order = []
  for (let i = 0; i < 30; i++) {
    await page.keyboard.press('Tab')
    order.push(
      await page.evaluate(() => {
        const el = document.activeElement
        if (!el || el === document.body) return 'BODY'
        const name = (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40)
        const style = getComputedStyle(el)
        return `${el.tagName.toLowerCase()}:${name}:outline=${style.outlineStyle}`
      }),
    )
  }
  const stops = order.filter((s) => s !== 'BODY')
  const unreachable = order.slice(0, order.indexOf('BODY') === -1 ? order.length : order.indexOf('BODY')).filter((s) => s === 'BODY').length
  record('tab order reaches every control without a <body> gap', unreachable === 0, `${stops.length} stops`)
  const noOutline = order.filter((s) => s.endsWith('outline=none')).length
  record('every focused element draws a focus ring', noOutline === 0, `${noOutline} without outline`)
  console.log('  tab order:', order.join(' | '))
  await ctx.close()
}

// ── nav + footer links ──────────────────────────────────────────────────────
{
  for (const width of [375, 768, 1280]) {
    const ctx = await browser.newContext({ viewport: { width, height: 900 } })
    const page = await ctx.newPage()
    await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
    const nav = page.locator('nav a[href="/tcdn-ai"]')
    const visible = (await nav.count()) > 0 && (await nav.first().isVisible())
    record(`nav link ${width >= 640 ? 'visible' : 'hidden'} @${width}`, visible === width >= 640)
    const footer = page.locator('footer a[href="/tcdn-ai"]')
    record(`footer link present @${width}`, (await footer.count()) === 1)
    await ctx.close()
  }
}

// ── flag-off console still shows the panel ──────────────────────────────────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/ai`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(800)
  const panel = page.getByText('TCDN-AI is not available on this deployment')
  record('/ai still shows only the unavailable panel', (await panel.count()) === 1)
  await ctx.close()
}

await browser.close()
const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} checks passed`)
if (failed.length) process.exitCode = 1
