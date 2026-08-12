/**
 * E2E: admin panel — no text selection / highlight.
 *
 * Verifies the admin panel's select-none treatment: no text on /admin,
 * /admin/health, or /admin/payouts can be selected or highlighted. This is
 * the browser-level proof behind the "remove highlighted texts" change — the
 * CSS class is compiled, but the real guarantee is behavioral: a user drag
 * across the page text must produce ZERO selected characters.
 *
 * Admin pages redirect non-admins to /, so the suite needs a real admin
 * session:
 *   E2E_ADMIN_TOKEN   session token of an ADMIN account. Without it the suite
 *                     SKIPS cleanly (exit 0, reported as skipped) — there is
 *                     no boundary subset that can exercise the admin pages.
 *                     CI has no admin token, so the batch no-ops it there;
 *                     run it locally (or after deploy) with an admin token.
 *
 * For each page it checks:
 *   1. the content root resolves to `user-select: none` (computed style),
 *   2. an actual mouse drag over real text leaves `window.getSelection()`
 *      empty — the highlight a user would see simply never happens.
 */
import { chromium } from 'playwright'
import { BASE, attachDiagnostics, report } from './helpers.mjs'

const ADMIN_TOKEN = process.env.E2E_ADMIN_TOKEN

const failures = []
const out = {
  suite: 'admin-noselect',
  pages: [],
  consoleErrors: [],
  pageErrors: [],
  skipped: !ADMIN_TOKEN,
}
const browser = await chromium.launch({ headless: true })
let page = null

try {
  if (!ADMIN_TOKEN) {
    out.skipReason = 'E2E_ADMIN_TOKEN not set — admin pages redirect non-admins, so this check needs a real admin session token.'
  } else {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    await context.addInitScript((tok) => {
      window.sessionStorage.setItem('torrent_cdn_token', tok || '')
    }, ADMIN_TOKEN)
    page = await context.newPage()
    attachDiagnostics(page, out)

    const targets = [
      { name: 'admin', path: '/admin', keyText: 'Admin Dashboard' },
      { name: 'admin-health', path: '/admin/health', keyText: 'Security & Health' },
      { name: 'admin-payouts', path: '/admin/payouts', keyText: 'Available balance' },
    ]

    for (const { name, path, keyText } of targets) {
      const entry = { name, url: `${BASE}${path}` }
      try {
        await page.goto(entry.url, { waitUntil: 'domcontentloaded', timeout: 60000 })
        await page.waitForSelector(`text=${keyText}`, { timeout: 30000 })
        await page.waitForTimeout(800) // let client-side state settle
        // Redirect guard, asserted AFTER the client settles: a non-admin (or
        // expired) token makes the page bounce to / — the keyText above would
        // never render and this fails. Re-reading the URL here also catches the
        // case where keyText exists on BOTH pages (e.g. a shared navbar label)
        // but the admin content did not — asserting we're still on the target
        // path proves the admin view, not the landing page, was sampled.
        const settledPath = new URL(page.url()).pathname
        if (settledPath !== path) {
          throw new Error(`expected to stay on ${path} but ended on ${settledPath} (token not admin/expired?)`)
        }

        // 1) Computed style: the select-none root must resolve to user-select: none.
        entry.userSelect = await page.evaluate(() => {
          const el = document.querySelector('div.select-none') || document.body
          return getComputedStyle(el).userSelect
        })

        // 2) Behavioral proof: drag across real page text (the h1) and confirm
        //    nothing can be selected. This is what the user actually sees — a
        //    drag that would normally paint the blue highlight must yield ''.
        //    Clear any pre-existing selection first: Chromium keeps the range
        //    of a PREVIOUS drag alive, so without the reset the assertion would
        //    measure leftover state, not this page's selectability.
        await page.evaluate(() => window.getSelection()?.removeAllRanges())
        const h1 = page.locator('h1').first()
        const box = await h1.boundingBox()
        entry.dragPerformed = !!box
        if (box) {
          const midY = box.y + box.height / 2
          await page.mouse.move(box.x + 10, midY)
          await page.mouse.down()
          await page.mouse.move(box.x + box.width - 10, midY, { steps: 8 })
          await page.mouse.up()
        }
        entry.selectionAfterDrag = await page.evaluate(() => window.getSelection()?.toString().length || 0)
        entry.draggedText = box ? (await h1.innerText()).slice(0, 60) : null

        if (entry.userSelect !== 'none') {
          failures.push(`${name}: computed user-select is "${entry.userSelect}", expected "none"`)
        }
        if (!entry.dragPerformed) {
          failures.push(`${name}: h1 had no bounding box — drag never ran, behavioral proof skipped`)
        }
        if (entry.selectionAfterDrag !== 0) {
          failures.push(`${name}: drag-selecting "${entry.draggedText}" produced a ${entry.selectionAfterDrag}-char selection — text is still highlightable`)
        }
        out.pages.push(entry)
      } catch (e) {
        entry.error = e.message.split('\n')[0].slice(0, 160)
        failures.push(`${name}: ${entry.error}`)
        out.pages.push(entry)
      }
    }
    await context.close()
  }

  if (out.pageErrors.length) failures.push(`JS page errors: ${out.pageErrors.join(' | ')}`)
  const nonResource = out.consoleErrors.filter((e) => !e.startsWith('Failed to load resource'))
  if (nonResource.length) failures.push(`console errors: ${nonResource.join(' | ')}`)
} catch (e) {
  failures.push(`suite error: ${e.message}`)
} finally {
  await browser.close()
}

const passed = report(out, failures)
process.exit(passed ? 0 : 1)
