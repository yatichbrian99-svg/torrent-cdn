#!/usr/bin/env node
/**
 * Audit the authenticated / backend-gated pages that the public evidence pass
 * cannot reach: /dashboard, /admin (+ health, payouts), /live and the TCDN-AI
 * console (/ai/*), with ENABLE_TCDN_AI on.
 *
 *   node scripts/gated_pages_audit.mjs \
 *     --base http://localhost:3000 \
 *     --token-file /tmp/owner-admin-token.txt \
 *     --out ../docs/frontend/evidence/gated
 *
 * For every page × theme × width it captures a full-page screenshot and
 * records horizontal overflow. At 1280 and 375 it also runs axe (WCAG 2.0/2.1
 * A+AA) over the whole document, and at 1280 it walks the page with the
 * keyboard, recording every focus stop and whether the focused element has a
 * visible focus indicator (outline or box-shadow).
 *
 * The session token is seeded into sessionStorage before first paint, exactly
 * as the app stores it after a real login — no test-only hooks in the app.
 */
import { chromium } from 'playwright'
import AxeBuilder from '@axe-core/playwright'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'

const args = process.argv.slice(2)
const arg = (name, fallback) => {
  const i = args.indexOf(`--${name}`)
  return i >= 0 ? args[i + 1] : fallback
}

const BASE = arg('base', 'http://localhost:3000')
const OUT = arg('out', path.join(process.cwd(), '..', 'docs', 'frontend', 'evidence', 'gated'))
const TOKEN = readFileSync(arg('token-file'), 'utf8').trim()
const WIDTHS = arg('widths', '375,768,1280').split(',').map(Number)
const THEMES = arg('themes', 'light,dark').split(',')
const MAX_TABS = Number(arg('max-tabs', '26'))
const PAGES = arg(
  'pages',
  [
    '/dashboard',
    '/dashboard/upload',
    '/admin',
    '/admin/health',
    '/admin/payouts',
    '/live',
    '/ai',
    '/ai/keys',
    '/ai/usage',
    '/ai/models',
    '/ai/quickstart',
    '/ai/billing',
    '/ai/status',
  ].join(','),
).split(',')

mkdirSync(OUT, { recursive: true })

const report = { base: BASE, capturedAt: new Date().toISOString(), runs: [] }
let failures = 0

const browser = await chromium.launch({ headless: true })
try {
  for (const theme of THEMES) {
    for (const width of WIDTHS) {
      const context = await browser.newContext({
        viewport: { width, height: 900 },
        deviceScaleFactor: 1,
        colorScheme: theme,
      })
      await context.addInitScript(
        ({ token, t }) => {
          try {
            window.sessionStorage.setItem('torrent_cdn_token', token)
            localStorage.setItem('tcdn_theme', t)
          } catch {
            /* ignore */
          }
        },
        { token: TOKEN, t: theme },
      )
      const page = await context.newPage()
      for (const route of PAGES) {
        const name = route.replace(/^\//, '').replace(/\//g, '_') || 'root'
        const run = { route, theme, width, axe: null, keyboard: null }
        try {
          await page.goto(`${BASE}${route}`, { waitUntil: 'networkidle', timeout: 60000 })
          await page.waitForTimeout(700)
          // Gated pages can bounce to the landing page when the session is not
          // accepted; record where we actually landed instead of pretending.
          run.finalUrl = new URL(page.url()).pathname
          if (run.finalUrl !== route) {
            failures++
            run.redirected = run.finalUrl
            console.log(`  FAIL ${theme}@${width} ${route} redirected to ${run.finalUrl}`)
          }
          run.themeApplied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
          run.overflowX = await page.evaluate(
            () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
          )
          if (run.overflowX > 0) {
            failures++
            console.log(`  FAIL ${theme}@${width} ${route} overflow ${run.overflowX}px`)
          }
          run.title = await page.title()
          const file = path.join(OUT, `${name}-${theme}-${width}.png`)
          await page.screenshot({ path: file, fullPage: true })
          run.screenshot = file

          if (width === 1280 || width === 375) {
            const { violations } = await new AxeBuilder({ page })
              .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
              .analyze()
            run.axe = violations.map((v) => ({
              id: v.id,
              impact: v.impact,
              help: v.help,
              nodes: v.nodes.length,
              targets: v.nodes.slice(0, 3).map((n) => n.target.join(' ')),
            }))
            const serious = violations.filter((v) => ['serious', 'critical'].includes(v.impact))
            if (serious.length) {
              failures++
              console.log(
                `  FAIL ${theme}@${width} ${route} axe: ${serious.map((v) => v.id).join(', ')}`,
              )
            }
            const contrast = violations.filter((v) => v.id === 'color-contrast')
            if (contrast.length) {
              failures++
              console.log(
                `  FAIL ${theme}@${width} ${route} colour contrast on ${contrast[0].nodes.length} node(s)`,
              )
            }
          }

          if (width === 1280) {
            await page.goto(`${BASE}${route}`, { waitUntil: 'networkidle', timeout: 60000 })
            await page.waitForTimeout(500)
            const stops = []
            for (let i = 0; i < MAX_TABS; i++) {
              await page.keyboard.press('Tab')
              const focused = await page.evaluate(() => {
                const el = document.activeElement
                if (!el || el === document.body) return null
                const cs = getComputedStyle(el)
                const visible =
                  (cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0) ||
                  (cs.boxShadow && cs.boxShadow !== 'none')
                return {
                  tag: el.tagName.toLowerCase(),
                  text: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 48),
                  href: el.getAttribute('href') || undefined,
                  focusVisible: Boolean(visible),
                }
              })
              if (!focused) break
              stops.push(focused)
            }
            run.keyboard = {
              stops: stops.length,
              unique: new Set(stops.map((s) => `${s.tag}:${s.text}:${s.href || ''}`)).size,
              withoutIndicator: stops.filter((s) => !s.focusVisible),
            }
            if (run.keyboard.withoutIndicator.length) {
              failures++
              console.log(
                `  FAIL ${theme}@1280 ${route} ${run.keyboard.withoutIndicator.length} focus stop(s) without a visible indicator`,
              )
            }
            if (run.keyboard.stops < 3) {
              failures++
              console.log(`  FAIL ${theme}@1280 ${route} only ${run.keyboard.stops} focus stops`)
            }
          }

          run.ok = !run.redirected && !run.overflowX && !(run.axe || []).some((v) => ['serious', 'critical'].includes(v.impact))
          console.log(
            `${theme}@${width} ${route}: axe ${run.axe ? run.axe.length : '–'} · overflow ${run.overflowX}px` +
              (run.keyboard ? ` · ${run.keyboard.stops} focus stops` : ''),
          )
        } catch (err) {
          run.ok = false
          run.error = String(err && err.message ? err.message : err).slice(0, 300)
          failures++
          console.log(`  FAIL ${theme}@${width} ${route}: ${run.error}`)
        }
        report.runs.push(run)
      }
      await context.close()
    }
  }
} finally {
  await browser.close()
}

report.failures = failures
report.pass = failures === 0
writeFileSync(path.join(OUT, 'gated-audit.json'), JSON.stringify(report, null, 2))
console.log(`\n${failures === 0 ? 'PASS' : 'FAIL'} — ${failures} problem(s)`)
process.exit(failures ? 1 : 0)
