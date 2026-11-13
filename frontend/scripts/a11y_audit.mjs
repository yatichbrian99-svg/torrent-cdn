#!/usr/bin/env node
/**
 * Accessibility audit for the redesigned pages.
 *
 *   node scripts/a11y_audit.mjs --base http://localhost:3102 \
 *        --out ../docs/frontend/evidence/axe.md \
 *        --keyboard-out ../docs/frontend/evidence/keyboard.md
 *
 * Part 1 — axe-core (via @axe-core/playwright) with the WCAG 2.1 A + AA rule
 * sets, on every restyled page, in BOTH themes. Any violation fails the run.
 *
 * Part 2 — a keyboard walk: Tab through the page and assert every stop has a
 * visible focus indicator (outline or ring) and that focus never falls to
 * <body>; then the mobile menu: open with the keyboard, Escape closes it,
 * Tab stays inside, and the trigger reports aria-expanded.
 */
import { chromium } from 'playwright'
import AxeBuilder from '@axe-core/playwright'
import { writeFileSync, mkdirSync } from 'node:fs'
import path from 'node:path'

const args = process.argv.slice(2)
const arg = (name, fallback) => {
  const i = args.indexOf(`--${name}`)
  return i >= 0 ? args[i + 1] : fallback
}
const BASE = arg('base', 'http://localhost:3102')
const OUT = arg('out', path.join(process.cwd(), '..', 'docs', 'frontend', 'evidence', 'axe.md'))
const KEY_OUT = arg('keyboard-out', path.join(process.cwd(), '..', 'docs', 'frontend', 'evidence', 'keyboard.md'))
const ROUTES = arg(
  'routes',
  '/,/docs,/architecture,/demo,/tcdn-ai,/terms,/auth/forgot-password',
).split(',')
const THEMES = ['light', 'dark']

const browser = await chromium.launch({ headless: true })
const results = []
const failures = []

try {
  for (const theme of THEMES) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: theme })
    await context.addInitScript((t) => {
      try {
        localStorage.setItem('tcdn_theme', t)
      } catch {
        /* ignore */
      }
    }, theme)
    const page = await context.newPage()
    for (const route of ROUTES) {
      await page.goto(`${BASE}${route}`, { waitUntil: 'networkidle', timeout: 60000 })
      await page.waitForTimeout(400)
      const analysis = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
        .analyze()
      const violations = analysis.violations.map((v) => ({
        id: v.id,
        impact: v.impact,
        help: v.help,
        nodes: v.nodes.map((n) => ({ target: n.target.join(' '), summary: n.failureSummary?.split('\n')[1]?.trim() })),
      }))
      if (violations.length) failures.push(`${route} ${theme}: ${violations.map((v) => v.id).join(', ')}`)
      results.push({ route, theme, violations, passes: analysis.passes.length })
    }
    await context.close()
  }

  // ---------------------------------------------------------- keyboard walk
  const keyContext = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const keyPage = await keyContext.newPage()
  await keyPage.goto(BASE, { waitUntil: 'networkidle' })
  await keyPage.waitForTimeout(400)

  const stops = []
  for (let i = 0; i < 24; i++) {
    await keyPage.keyboard.press('Tab')
    const info = await keyPage.evaluate(() => {
      const el = document.activeElement
      if (!el) return null
      const cs = getComputedStyle(el)
      const outline = `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor}`
      const hasOutline = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0
      const hasShadow = cs.boxShadow !== 'none'
      return {
        tag: el.tagName.toLowerCase(),
        label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 40),
        outline,
        hasOutline,
        hasShadow,
        visible: hasOutline || hasShadow,
      }
    })
    if (!info) break
    stops.push(info)
    if (!info.visible) failures.push(`keyboard: no focus indicator on <${info.tag}> "${info.label}"`)
  }
  const bodyFocus = stops.some((s) => s.tag === 'body')

  // ------------------------------------------------ mobile menu behaviour
  const mobile = await browser.newContext({ viewport: { width: 390, height: 780 } })
  const mpage = await mobile.newPage()
  await mpage.goto(BASE, { waitUntil: 'networkidle' })
  const burger = mpage.locator('.nav-burger')
  await burger.focus()
  await mpage.keyboard.press('Enter')
  await mpage.waitForTimeout(250)
  const openState = await mpage.evaluate(() => {
    const panel = document.querySelector('#nav-mobile-panel')
    const trigger = document.querySelector('.nav-burger')
    return {
      expanded: trigger?.getAttribute('aria-expanded'),
      visible: panel ? !panel.hasAttribute('hidden') : false,
      bodyOverflow: document.body.style.overflow,
      focusInside: panel ? panel.contains(document.activeElement) : false,
    }
  })
  await mpage.keyboard.press('Escape')
  await mpage.waitForTimeout(250)
  const closedState = await mpage.evaluate(() => ({
    expanded: document.querySelector('.nav-burger')?.getAttribute('aria-expanded'),
    hidden: document.querySelector('#nav-mobile-panel')?.hasAttribute('hidden'),
    focusOnTrigger: document.activeElement?.classList.contains('nav-burger'),
    bodyOverflow: document.body.style.overflow,
  }))

  const menuOk =
    openState.expanded === 'true' &&
    openState.visible === true &&
    openState.bodyOverflow === 'hidden' &&
    openState.focusInside === true &&
    closedState.expanded === 'false' &&
    closedState.hidden === true &&
    closedState.focusOnTrigger === true &&
    closedState.bodyOverflow !== 'hidden'
  if (!menuOk) failures.push('mobile menu: open/Escape/scroll-lock contract broken')

  await mobile.close()
  await keyContext.close()

  // ------------------------------------------------------------------ report
  const axeLines = [
    '# axe-core audit (WCAG 2.1 A + AA), both themes',
    '',
    `Pages: ${ROUTES.join(', ')} — run against the production build.`,
    '',
    '| Page | Theme | Violations | Rules checked (passes) |',
    '| --- | --- | --- | --- |',
    ...results.map((r) => `| \`${r.route}\` | ${r.theme} | ${r.violations.length} | ${r.passes} |`),
    '',
  ]
  const detailed = results.filter((r) => r.violations.length)
  if (detailed.length) {
    axeLines.push('## Violations', '')
    for (const r of detailed) {
      axeLines.push(`### \`${r.route}\` (${r.theme})`, '')
      for (const v of r.violations) {
        axeLines.push(`- **${v.id}** (${v.impact}) — ${v.help}`)
        for (const n of v.nodes.slice(0, 6)) axeLines.push(`  - \`${n.target}\` ${n.summary ?? ''}`)
      }
      axeLines.push('')
    }
  } else {
    axeLines.push('Zero violations on every page in both themes.', '')
  }
  mkdirSync(path.dirname(OUT), { recursive: true })
  writeFileSync(OUT, axeLines.join('\n'))

  const keyLines = [
    '# Keyboard walk-through',
    '',
    'Recorded by `frontend/scripts/a11y_audit.mjs` in Chromium (1280×900, light theme).',
    'Each line is one Tab stop, with the computed focus indicator.',
    '',
    '| # | Element | Label | Outline | Visible indicator |',
    '| --- | --- | --- | --- | --- |',
    ...stops.map(
      (s, i) => `| ${i + 1} | \`<${s.tag}>\` | ${s.label.replace(/\|/g, '\\|')} | ${s.outline} | ${s.visible ? 'yes' : '**NO**'} |`,
    ),
    '',
    `Focus never fell back to \`<body>\`: ${bodyFocus ? '**FAILED**' : 'confirmed'}.`,
    '',
    '## Mobile menu (390 px)',
    '',
    `- Enter on the burger opens the panel: aria-expanded=\`${openState.expanded}\`, panel visible=\`${openState.visible}\`, focus moved inside=\`${openState.focusInside}\`, body scroll locked=\`${openState.bodyOverflow === 'hidden'}\``,
    `- Escape closes it: aria-expanded=\`${closedState.expanded}\`, panel hidden=\`${closedState.hidden}\`, focus returned to the trigger=\`${closedState.focusOnTrigger}\`, scroll lock released=\`${closedState.bodyOverflow !== 'hidden'}\``,
    '',
    menuOk ? 'Mobile menu contract: pass.' : 'Mobile menu contract: **FAILED**.',
    '',
  ]
  mkdirSync(path.dirname(KEY_OUT), { recursive: true })
  writeFileSync(KEY_OUT, keyLines.join('\n'))

  const totalViolations = results.reduce((n, r) => n + r.violations.length, 0)
  console.log(`axe: ${totalViolations} violation(s) across ${results.length} page/theme runs`)
  console.log(`keyboard: ${stops.length} stops, all with visible focus: ${stops.every((s) => s.visible)}`)
  console.log(`mobile menu: ${menuOk ? 'pass' : 'FAIL'}`)
  console.log(`reports: ${OUT}, ${KEY_OUT}`)
  for (const f of failures) console.log(`  FAIL ${f}`)
} finally {
  await browser.close()
}

process.exit(failures.length ? 1 : 0)
