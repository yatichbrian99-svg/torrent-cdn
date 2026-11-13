#!/usr/bin/env node
/**
 * Hero legibility + geometry audit for the landing page.
 *
 *   node scripts/hero_audit.mjs --base http://localhost:3000 --out ../docs/frontend/evidence/hero
 *
 * For every theme × width it:
 *
 *  1. captures the hero section (light and dark, 375 / 768 / 1280) for review;
 *  2. captures the hero with its copy made invisible (visibility:hidden keeps
 *     the layout), several frames apart while the decoration animates, so the
 *     measured ground includes the moving dots/trails rather than one lucky
 *     frame;
 *  3. computes, per text element and per frame, the WORST contrast ratio any
 *     background pixel inside that element's box has against the element's
 *     own text colour — the honest number for text sitting on a graphic;
 *  4. records stat-card heights and the value baselines so "same height, same
 *     baseline" is a measurement rather than an opinion;
 *  5. fails (exit 1) on any text below 4.5:1, any overflow, or misaligned cards.
 *
 * Method note: the text colour is compared to raw background pixels (no
 * antialiasing sampling), which is the conservative reading — antialiased
 * glyph edges are darker than the nominal colour, so the true worst case is
 * the nominal colour over the brightest background pixel under the text.
 */
import { chromium } from 'playwright'
import { PNG } from 'pngjs'
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'

const args = process.argv.slice(2)
const arg = (name, fallback) => {
  const i = args.indexOf(`--${name}`)
  return i >= 0 ? args[i + 1] : fallback
}

const BASE = arg('base', 'http://localhost:3000')
const OUT = arg('out', path.join(process.cwd(), '..', 'docs', 'frontend', 'evidence', 'hero'))
const WIDTHS = arg('widths', '375,768,1280').split(',').map(Number)
const THEMES = arg('themes', 'light,dark').split(',')
const FRAMES = Number(arg('frames', '3'))
const MIN_RATIO = Number(arg('min', '4.5'))

mkdirSync(OUT, { recursive: true })

// ---------------------------------------------------------------- colour math
const srgbToLinear = (c) => {
  const v = c / 255
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
}
const luminance = (r, g, b) =>
  0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b)
const contrast = (lumA, lumB) => {
  const hi = Math.max(lumA, lumB)
  const lo = Math.min(lumA, lumB)
  return (hi + 0.05) / (lo + 0.05)
}

// --------------------------------------------------------------------- report
const report = {
  base: BASE,
  capturedAt: new Date().toISOString(),
  method: 'text colour vs every background pixel inside the element box, worst frame',
  runs: [],
}
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
      await context.addInitScript((t) => {
        try {
          localStorage.setItem('tcdn_theme', t)
        } catch {
          /* ignore */
        }
      }, theme)
      const page = await context.newPage()
      const run = { theme, width, texts: [], stats: null, overflowX: null, shots: [] }

      await page.goto(`${BASE}/`, { waitUntil: 'networkidle', timeout: 60000 })
      await page.evaluate(() => document.fonts.ready)
      await page.waitForTimeout(700)

      // 1. The hero as a reader sees it.
      const hero = page.locator('section.hero')
      const heroBox = await hero.boundingBox()
      const shot = path.join(OUT, `hero-${theme}-${width}.png`)
      await hero.screenshot({ path: shot })
      run.shots.push(shot)

      // 2. Geometry: overflow, card heights, value baselines.
      run.overflowX = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      )
      run.stats = await page.evaluate(() => {
        const canvas = document.createElement('canvas')
        const ctx = canvas.getContext('2d')
        const baselineOf = (el) => {
          const style = getComputedStyle(el)
          const rect = el.getBoundingClientRect()
          const size = parseFloat(style.fontSize)
          const lineHeight =
            style.lineHeight === 'normal' ? size * 1.2 : parseFloat(style.lineHeight)
          ctx.font = `${style.fontStyle} ${style.fontWeight} ${size}px ${style.fontFamily}`
          const m = ctx.measureText('Hxg')
          const ascent = m.fontBoundingBoxAscent ?? size * 0.8
          const descent = m.fontBoundingBoxDescent ?? size * 0.2
          const halfLeading = (lineHeight - (ascent + descent)) / 2
          return rect.top + halfLeading + ascent
        }
        const cards = [...document.querySelectorAll('.hero-stat')]
        return {
          heights: cards.map((c) => Math.round(c.getBoundingClientRect().height * 10) / 10),
          cardWidths: cards.map((c) => Math.round(c.getBoundingClientRect().width)),
          // the value line inside each card (numeral or mono label)
          baselines: cards.map((c) => {
            const el = c.querySelector('.hero-stat-big, .hero-stat-mono')
            return el ? Math.round(baselineOf(el) * 10) / 10 : null
          }),
          valueTops: cards.map((c) => {
            const el = c.querySelector('.hero-stat-big, .hero-stat-mono')
            return el ? Math.round(el.getBoundingClientRect().top * 10) / 10 : null
          }),
          viewport: getComputedStyle(document.documentElement).getPropertyValue('--x'),
        }
      })

      // 3. Text targets: colour + box, relative to the hero's clip.
      const targets = await page.evaluate(() => {
        const pick = [
          ['eyebrow', '.hero-eyebrow'],
          ['headline', '.hero-title'],
          ['accent-word', '.hero-word-accent'],
          ['subtext', '.hero-sub'],
          ['microcopy', '.hero-micro'],
        ]
        const heroEl = document.querySelector('section.hero')
        const heroRect = heroEl.getBoundingClientRect()
        return pick.map(([label, selector]) => {
          const el = document.querySelector(selector)
          if (!el) return { label, missing: true }
          const r = el.getBoundingClientRect()
          const cs = getComputedStyle(el)
          const [rr, gg, bb] = cs.color.match(/[\d.]+/g).map(Number)
          return {
            label,
            color: { r: rr, g: gg, b: bb },
            box: {
              x: r.left - heroRect.left,
              y: r.top - heroRect.top,
              w: r.width,
              h: r.height,
            },
            fontSize: parseFloat(cs.fontSize),
          }
        })
      })
      run.texts = targets.filter((t) => !t.missing)

      // 4. Background frames: copy invisible (layout preserved) so the pixels
      //    under each box are the real ground.
      // The floating nav overlaps the hero's top edge in a page screenshot, so
      // it is hidden too (it is fixed, so hiding it moves nothing). Only the
      // hero's own decoration is left under the text boxes.
      await page.addStyleTag({
        content:
          '.hero-inner, .hero-stats-wrap, .hero-wordmark, .nav-shell { visibility: hidden !important; }',
      })
      const frames = []
      for (let i = 0; i < FRAMES; i++) {
        const buf = await page.screenshot({
          clip: { x: 0, y: 0, width, height: Math.ceil(heroBox.height) },
          fullPage: true,
          animations: 'allow',
        })
        frames.push(PNG.sync.read(buf))
        await page.waitForTimeout(900)
      }

      for (const t of run.texts) {
        const textLum = luminance(t.color.r, t.color.g, t.color.b)
        let worst = { ratio: Infinity, pixel: null, frame: null }
        const x0 = Math.max(0, Math.floor(t.box.x))
        const x1 = Math.min(width, Math.ceil(t.box.x + t.box.w))
        const y0 = Math.max(0, Math.floor(t.box.y))
        const y1 = Math.min(Math.ceil(heroBox.height), Math.ceil(t.box.y + t.box.h))
        frames.forEach((png, fi) => {
          for (let y = y0; y < y1; y++) {
            for (let x = x0; x < x1; x++) {
              const idx = (png.width * y + x) << 2
              const ratio = contrast(
                textLum,
                luminance(png.data[idx], png.data[idx + 1], png.data[idx + 2]),
              )
              if (ratio < worst.ratio) {
                worst = {
                  ratio: Math.round(ratio * 100) / 100,
                  pixel: [png.data[idx], png.data[idx + 1], png.data[idx + 2]],
                  frame: fi,
                  at: [x, y],
                }
              }
            }
          }
        })
        t.worst = worst
        t.pass = worst.ratio >= MIN_RATIO
        if (!t.pass) {
          failures++
          console.log(
            `  FAIL ${theme}@${width} ${t.label}: ${worst.ratio}:1 vs rgb(${worst.pixel.join(',')}) at ${worst.at.join(',')}`,
          )
        }
      }

      // Geometry assertions. Only meaningful when the cards share a row: stacked
      // at 375 they are separate rows, so different heights/baselines are the
      // layout, not a defect.
      const heights = run.stats.heights
      const sideBySide = width >= 720
      run.stats.heightSpread = Math.max(...heights) - Math.min(...heights)
      const baselines = run.stats.baselines.filter((b) => b != null)
      run.stats.baselineSpread =
        baselines.length > 1 ? Math.max(...baselines) - Math.min(...baselines) : 0
      run.stats.sideBySide = sideBySide
      if (sideBySide && run.stats.heightSpread > 1) {
        failures++
        console.log(`  FAIL ${theme}@${width} stat heights differ by ${run.stats.heightSpread}px`)
      }
      if (sideBySide && run.stats.baselineSpread > 1.5) {
        failures++
        console.log(
          `  FAIL ${theme}@${width} stat value baselines differ by ${run.stats.baselineSpread}px`,
        )
      }
      if (run.overflowX > 0) {
        failures++
        console.log(`  FAIL ${theme}@${width} horizontal overflow ${run.overflowX}px`)
      }

      console.log(
        `${theme}@${width}: ` +
          run.texts
            .map((t) => `${t.label} ${t.worst.ratio}:1${t.pass ? '' : ' FAIL'}`)
            .join(' · ') +
          ` | card heights ${heights.join('/')} baseline spread ${run.stats.baselineSpread}px`,
      )

      report.runs.push(run)
      await context.close()
    }
  }
} finally {
  await browser.close()
}

report.failures = failures
report.pass = failures === 0
writeFileSync(path.join(OUT, 'hero-audit.json'), JSON.stringify(report, null, 2))
console.log(`\n${failures === 0 ? 'PASS' : 'FAIL'} — ${failures} problem(s); report: ${path.join(OUT, 'hero-audit.json')}`)
process.exit(failures ? 1 : 0)
