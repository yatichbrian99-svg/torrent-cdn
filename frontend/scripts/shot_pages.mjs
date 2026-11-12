#!/usr/bin/env node
/**
 * Screenshot matrix for the redesign evidence.
 *
 *   node scripts/shot_pages.mjs --base http://localhost:3100 --out ../docs/frontend/evidence/baseline
 *
 * For every page × width × theme it captures a full-page PNG and records the
 * document scroll width vs the viewport width (any difference is horizontal
 * overflow, which must be 0 px).
 *
 * Theme is set the way a returning visitor sets it — an explicit choice in
 * localStorage — so the pre-paint script resolves it before first paint and the
 * screenshots show the real first-paint colours, not a post-hydration flash.
 *
 * `--pages` narrows the run (comma-separated paths). `--tag` prefixes filenames
 * so before/after sets never overwrite each other.
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'

const args = process.argv.slice(2)
const arg = (name, fallback) => {
  const i = args.indexOf(`--${name}`)
  return i >= 0 ? args[i + 1] : fallback
}

const BASE = arg('base', 'http://localhost:3100')
const OUT = arg('out', path.join(process.cwd(), '..', 'docs', 'frontend', 'evidence', 'screenshots'))
const TAG = arg('tag', '')
const PAGES = arg(
  'pages',
  '/,/docs,/architecture,/demo,/tcdn-ai,/terms,/auth/forgot-password,/embed/demo',
).split(',')
const WIDTHS = (arg('widths', '375,768,1280')).split(',').map(Number)
const THEMES = ['light', 'dark']

mkdirSync(OUT, { recursive: true })
const report = { base: BASE, capturedAt: new Date().toISOString(), shots: [] }

const browser = await chromium.launch({ headless: true })
try {
  for (const theme of THEMES) {
    for (const width of WIDTHS) {
      const context = await browser.newContext({
        viewport: { width, height: 900 },
        deviceScaleFactor: 1,
        colorScheme: theme, // system preference, used when no choice is stored
      })
      await context.addInitScript((t) => {
        try {
          localStorage.setItem('tcdn_theme', t)
        } catch {
          /* ignore */
        }
      }, theme)
      const page = await context.newPage()
      for (const route of PAGES) {
        const name = route === '/' ? 'landing' : route.replace(/^\//, '').replace(/\//g, '_')
        const file = path.join(OUT, `${TAG}${name}-${theme}-${width}.png`)
        const entry = { route, theme, width, file }
        try {
          await page.goto(`${BASE}${route}`, { waitUntil: 'networkidle', timeout: 60000 })
          await page.waitForTimeout(1200)
          // Let reveal-on-scroll sections settle before the full-page capture:
          // walk the page so every IntersectionObserver fires.
          await page.evaluate(async () => {
            const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
            // Walk the page, then visit each reveal explicitly: a fast pass can
            // outrun the observer and leave a section hidden in the capture.
            const step = Math.max(240, window.innerHeight * 0.65)
            const height = Math.max(
              document.documentElement.scrollHeight,
              document.body.scrollHeight,
            )
            for (let y = 0; y <= height; y += step) {
              window.scrollTo(0, y)
              await sleep(90)
            }
            for (const el of document.querySelectorAll('.reveal')) {
              el.scrollIntoView({ block: 'center' })
              await sleep(160)
            }
            window.scrollTo(0, 0)
            await sleep(120)
          })
          // Wait until every reveal has actually fired (hydration can land after
          // the first scroll pass on slow pages), then let the transition finish.
          await page
            .waitForFunction(
              () => {
                const all = document.querySelectorAll('.reveal')
                return all.length === 0 || document.querySelectorAll('.reveal.is-in').length === all.length
              },
              { timeout: 4000 },
            )
            .catch(() => {})
          await page.waitForTimeout(900)
          const metrics = await page.evaluate(() => ({
            scrollWidth: document.documentElement.scrollWidth,
            clientWidth: document.documentElement.clientWidth,
            scrollHeight: document.documentElement.scrollHeight,
            theme: document.documentElement.getAttribute('data-theme'),
            overflowX: null,
          }))
          entry.overflowX = metrics.scrollWidth - metrics.clientWidth
          entry.themeApplied = metrics.theme
          entry.height = metrics.scrollHeight
          await page.screenshot({ path: file, fullPage: true })
          entry.ok = true
        } catch (err) {
          entry.ok = false
          entry.error = String(err && err.message ? err.message : err)
        }
        report.shots.push(entry)
      }
      await context.close()
    }
  }
} finally {
  await browser.close()
}

const summary = path.join(OUT, `${TAG}screenshots.json`)
writeFileSync(summary, JSON.stringify(report, null, 2))
const overflow = report.shots.filter((s) => (s.overflowX ?? 0) > 0)
const failed = report.shots.filter((s) => !s.ok)
console.log(`shots: ${report.shots.length}  overflow: ${overflow.length}  failed: ${failed.length}`)
for (const s of overflow) console.log(`  OVERFLOW ${s.route} ${s.theme}@${s.width}: ${s.overflowX}px`)
for (const s of failed) console.log(`  FAILED ${s.route} ${s.theme}@${s.width}: ${s.error}`)
process.exit(overflow.length || failed.length ? 1 : 0)
