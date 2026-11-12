import { createRequire } from 'node:module'
const require = createRequire(process.cwd() + '/package.json')
const { chromium } = require('playwright')
const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 1280, height: 900 } })
await p.goto('http://127.0.0.1:3002/tcdn-ai', { waitUntil: 'networkidle' })
const text = await p.evaluate(() => document.body.innerText)
const required = [
  'built for a peer-to-peer network',
  'The endpoint is enabled for invited accounts',
  'planned to be llama-3.1-8b-instruct',
  'Meta Llama 3.1 Community License',
  'The confidential tier is not available',
  'M-Pesa is coming',
  'The host agent is in development',
  'There is no withdrawal route for host earnings yet',
  'treat them as untested',
]
const forbidden = [
  'in your own currency', 'is the model this alpha serves', 'confidential tier is built',
  'payout ledger', 'split into shards. Hosts', 'served by a peer-to-peer', 'secure', 'audited',
  'production ready', '99.', 'attested hardware', 'cheaper', 'faster than',
]
let fails = 0
for (const r of required) { const ok = text.toLowerCase().includes(r.toLowerCase()); if (!ok) fails++; console.log(`${ok?'PASS':'FAIL'}  required: ${r}`) }
for (const f of forbidden) { const hit = text.toLowerCase().includes(f.toLowerCase()); if (hit) fails++; console.log(`${!hit?'PASS':'FAIL'}  absent: ${f}`) }

await p.screenshot({ path: '/tmp/tcdn-ai-shots/claims-1280.png', fullPage: true })
const m = await b.newPage({ viewport: { width: 375, height: 800 } })
await m.goto('http://127.0.0.1:3002/tcdn-ai', { waitUntil: 'networkidle' })
const ow = await m.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
console.log(`${ow===0?'PASS':'FAIL'}  no horizontal overflow @375 (${ow}px)`)
await m.screenshot({ path: '/tmp/tcdn-ai-shots/claims-375.png', fullPage: true })
await b.close()
console.log(`\n${fails === 0 ? 'all copy checks passed' : fails + ' copy checks failed'}`)
if (fails) process.exitCode = 1
