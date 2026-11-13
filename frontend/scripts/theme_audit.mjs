#!/usr/bin/env node
/**
 * Browser-level theme audit (Playwright + real Chromium).
 *
 * Proves the parts unit tests cannot:
 *   1. no flash of the wrong theme — the <html data-theme> and the painted
 *      background agree on the very first animation frame, forced both ways;
 *   2. zero CSP violations in either theme, on load and after toggling
 *      (the pre-paint script must run under the nonce, no 'unsafe-inline');
 *   3. storage blocked → the site still resolves a theme (the OS one) and the
 *      toggle still works for the session;
 *   4. no stored choice → an OS preference change applies live, and a stored
 *      choice ignores it;
 *   5. prefers-reduced-motion → entrance/reveal animation off;
 *   6. the toggle works from the keyboard and reports aria-pressed.
 *
 *   node scripts/theme_audit.mjs --base http://localhost:3100 --out ../docs/frontend/evidence/theme-audit.txt
 */
import { chromium } from 'playwright'
import { writeFileSync, mkdirSync } from 'node:fs'
import path from 'node:path'

const args = process.argv.slice(2)
const arg = (name, fallback) => {
  const i = args.indexOf(`--${name}`)
  return i >= 0 ? args[i + 1] : fallback
}
const BASE = arg('base', 'http://localhost:3100')
const OUT = arg('out', path.join(process.cwd(), '..', 'docs', 'frontend', 'evidence', 'theme-audit.txt'))
const PAGE = `${BASE}/`

const lines = []
const failures = []
const record = (ok, label, detail = '') => {
  lines.push(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failures.push(label)
}

const browser = await chromium.launch({ headless: true })

const firstPaintProbe = () => {
  // Runs before any page script. The theme guard in globals.css keeps <body>
  // invisible until the pre-paint script has resolved the theme, so the frame
  // that matters is the FIRST VISIBLE one: capture the theme and the painted
  // background at that moment. A wrong-theme frame that is invisible is not a
  // flash; a wrong-theme frame that is visible is.
  window.__firstPaint = new Promise((resolve) => {
    const start = performance.now()
    const step = () => {
      const html = document.documentElement
      const visibility = getComputedStyle(document.body).visibility
      const visible = visibility !== 'hidden'
      if (visible || performance.now() - start > 800) {
        resolve({
          theme: html.getAttribute('data-theme'),
          bg: getComputedStyle(document.body).backgroundColor,
          resolved: html.getAttribute('data-theme-resolved'),
          visibility,
          waitedMs: Math.round(performance.now() - start),
        })
        return
      }
      requestAnimationFrame(step)
    }
    /* eslint-disable no-undef */
    requestAnimationFrame(step)
  })
}

const cspProbe = () => {
  window.__cspViolations = []
  document.addEventListener('securitypolicyviolation', (event) => {
    window.__cspViolations.push(`${event.violatedDirective} ${event.blockedURI}`)
  })
}

try {
  // ---------------------------------------------------------------- 1. no flash
  for (const theme of ['light', 'dark']) {
    const context = await browser.newContext({ colorScheme: theme })
    await context.addInitScript(() => {
      try {
        /* eslint-disable no-undef */
        localStorage.setItem('tcdn_theme', 'PLACEHOLDER')
      } catch {
        /* ignore */
      }
    })
    // Replace the placeholder with the wanted theme before first paint.
    await context.addInitScript((t) => {
      try {
        localStorage.setItem('tcdn_theme', t)
      } catch {
        /* ignore */
      }
    }, theme)
    await context.addInitScript(firstPaintProbe)
    const page = await context.newPage()
    await page.goto(PAGE, { waitUntil: 'commit' })
    const first = await page.evaluate(() => window.__firstPaint)
    const expected = theme === 'dark' ? 'rgb(0, 0, 0)' : 'rgb(254, 254, 253)'
    record(
      first.theme === theme && first.bg === expected && first.resolved === '1' && first.visibility === 'visible',
      `first visible frame is ${theme} with no flash`,
      `data-theme=${first.theme} bg=${first.bg} resolved=${first.resolved} after ${first.waitedMs}ms`,
    )
    await context.close()
  }

  // ------------------------------------------------------- 2. CSP violations
  for (const theme of ['light', 'dark']) {
    const context = await browser.newContext({ colorScheme: theme })
    await context.addInitScript(cspProbe)
    await context.addInitScript((t) => {
      try {
        localStorage.setItem('tcdn_theme', t)
      } catch {
        /* ignore */
      }
    }, theme)
    const page = await context.newPage()
    const consoleErrors = []
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text())
    })
    await page.goto(PAGE, { waitUntil: 'networkidle' })
    await page.evaluate(() => document.querySelector('.theme-toggle')?.click())
    await page.waitForTimeout(400)
    const violations = await page.evaluate(() => window.__cspViolations)
    const cspErrors = consoleErrors.filter((text) => /Content Security Policy/i.test(text))
    record(
      violations.length === 0 && cspErrors.length === 0,
      `no CSP violations (${theme} load + toggle)`,
      violations.concat(cspErrors).slice(0, 3).join(' | ') || 'none',
    )
    await context.close()
  }

  // ------------------------------------------------- 3. storage blocked
  {
    const context = await browser.newContext({ colorScheme: 'dark' })
    await context.addInitScript(() => {
      // Simulate blocked storage (Safari private mode / third-party cookies).
      const boom = () => {
        throw new DOMException('blocked', 'SecurityError')
      }
      Object.defineProperty(window, 'localStorage', {
        configurable: true,
        get() {
          return { getItem: boom, setItem: boom, removeItem: boom, clear: boom }
        },
      })
    })
    const page = await context.newPage()
    await page.goto(PAGE, { waitUntil: 'networkidle' })
    const before = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    await page.click('.theme-toggle')
    await page.waitForTimeout(250)
    const after = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    record(
      before === 'dark' && after === 'light',
      'storage blocked: follows the OS, toggle still switches',
      `${before} → ${after}`,
    )
    await context.close()
  }

  // ------------------------------------ 4. live OS change / stored wins
  {
    const context = await browser.newContext({ colorScheme: 'light' })
    const page = await context.newPage()
    await page.goto(PAGE, { waitUntil: 'networkidle' })
    const start = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    await page.emulateMedia({ colorScheme: 'dark' })
    await page.waitForTimeout(300)
    const live = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    record(start === 'light' && live === 'dark', 'no choice: OS change applies live', `${start} → ${live}`)

    // Now choose light explicitly, then change the OS again.
    await page.click('.theme-toggle')
    await page.waitForTimeout(200)
    const chosen = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    await page.emulateMedia({ colorScheme: 'light' })
    await page.waitForTimeout(150)
    await page.emulateMedia({ colorScheme: 'dark' })
    await page.waitForTimeout(300)
    const stubborn = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    const stored = await page.evaluate(() => localStorage.getItem('tcdn_theme'))
    record(
      chosen === 'light' && stubborn === 'light' && stored === 'light',
      'after a choice, the OS is ignored and the choice is stored',
      `chosen=${chosen} afterOSflip=${stubborn} stored=${stored}`,
    )
    await context.close()
  }

  // --------------------------------------------- 5. reduced motion
  {
    const context = await browser.newContext({ colorScheme: 'light', reducedMotion: 'reduce' })
    const page = await context.newPage()
    await page.goto(PAGE, { waitUntil: 'networkidle' })
    const motion = await page.evaluate(() => {
      const hero = document.querySelector('.hero-rise')
      const art = document.querySelector('.art-drift')
      const reveal = document.querySelector('.reveal')
      const cs = (el) => (el ? getComputedStyle(el) : null)
      return {
        heroAnim: cs(hero)?.animationName ?? 'none',
        artAnim: cs(art)?.animationName ?? 'none',
        revealOpacity: reveal ? Number(cs(reveal).opacity) : 1,
      }
    })
    record(
      motion.heroAnim === 'none' && motion.artAnim === 'none' && motion.revealOpacity === 1,
      'prefers-reduced-motion: entrance, drift and reveal disabled',
      JSON.stringify(motion),
    )
    await context.close()
  }

  // ------------------------------------ 6. keyboard + aria on the toggle
  {
    const context = await browser.newContext({ colorScheme: 'light' })
    const page = await context.newPage()
    await page.goto(PAGE, { waitUntil: 'networkidle' })
    const button = page.locator('.theme-toggle')
    const label = await button.getAttribute('aria-label')
    const pressedBefore = await button.getAttribute('aria-pressed')
    await button.focus()
    const focused = await page.evaluate(() => document.activeElement?.classList.contains('theme-toggle'))
    await page.keyboard.press('Space')
    await page.waitForTimeout(250)
    const themeAfter = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    const pressedAfter = await button.getAttribute('aria-pressed')
    const labelAfter = await button.getAttribute('aria-label')
    record(
      focused === true && themeAfter === 'dark' && pressedBefore === 'false' && pressedAfter === 'true' && label !== labelAfter,
      'toggle: keyboard operable, aria-pressed and label update',
      `focus=${focused} theme=${themeAfter} ${pressedBefore}→${pressedAfter}`,
    )
    await context.close()
  }
} finally {
  await browser.close()
}

mkdirSync(path.dirname(OUT), { recursive: true })
writeFileSync(OUT, lines.join('\n') + '\n')
console.log(lines.join('\n'))
console.log(failures.length ? `\n${failures.length} check(s) FAILED` : '\nall checks passed')
process.exit(failures.length ? 1 : 0)
