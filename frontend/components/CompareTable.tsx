'use client'

import { useEffect, useRef, useState } from 'react'

/**
 * "Pay less as you grow. Not more." comparison.
 *
 * Copy, column order and every figure are unchanged from the pre-redesign
 * table — no new number is introduced anywhere. Two purely visual additions:
 *
 *  - a gap bar per row (the bar's length is the AWS figure; the accent sliver
 *    inside it is the TorrentCDN figure, both already in the row), and
 *  - a count-up that runs once when the table scrolls into view.
 *
 * The final value is rendered in the HTML on the server, so crawlers and
 * no-JS readers see the real numbers; the animation only overwrites the text
 * after hydration, starting from zero. `prefers-reduced-motion: reduce`
 * skips it entirely.
 */

type Row = { viewers: string; cloudfront: string; torrentcdn: string }

const ROWS: Row[] = [
  { viewers: '1,000', cloudfront: '$45', torrentcdn: '$1.20' },
  { viewers: '10,000', cloudfront: '$450', torrentcdn: '$4.80' },
  { viewers: '100,000', cloudfront: '$4,500', torrentcdn: '$12.00' },
  { viewers: '1,000,000', cloudfront: '$45,000', torrentcdn: '$28.00' },
]

const num = (value: string) => Number(value.replace(/[^0-9.]/g, ''))
const decimals = (value: string) => (value.split('.')[1] || '').length

function format(value: number, template: string): string {
  const prefix = template.startsWith('$') ? '$' : ''
  const places = decimals(template)
  return (
    prefix +
    value.toLocaleString('en-US', {
      minimumFractionDigits: places,
      maximumFractionDigits: places,
    })
  )
}

export default function CompareTable() {
  const tableRef = useRef<HTMLDivElement | null>(null)
  const [started, setStarted] = useState(false)
  const [progress, setProgress] = useState(0)

  useEffect(() => {
    const node = tableRef.current
    if (!node) return
    const reduce =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduce || typeof IntersectionObserver !== 'function') return

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting)) return
        observer.disconnect()
        setStarted(true)

        const duration = 900
        const start = performance.now()
        const tick = (now: number) => {
          const t = Math.min(1, (now - start) / duration)
          // easeOutCubic
          setProgress(1 - Math.pow(1 - t, 3))
          if (t < 1) requestAnimationFrame(tick)
        }
        requestAnimationFrame(tick)
      },
      { threshold: 0.25 },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  const value = (text: string) => (started && progress > 0 ? format(num(text) * progress, text) : text)

  const maxCloudfront = Math.max(...ROWS.map((r) => num(r.cloudfront)))

  return (
    <div className="compare" ref={tableRef}>
      <div className="compare-scroll">
        <table className="compare-table">
          <caption className="sr-only">Monthly delivery cost by audience size</caption>
          <thead>
            <tr>
              <th scope="col">Viewers</th>
              <th scope="col">AWS CloudFront</th>
              <th scope="col" className="compare-tcdn-col">TorrentCDN</th>
            </tr>
          </thead>
          <tbody>
            {ROWS.map((row) => {
              const cloud = num(row.cloudfront)
              const tcdn = num(row.torrentcdn)
              const tcdnWidth = Math.max(2, (tcdn / cloud) * 100)
              return (
                <tr key={row.viewers}>
                  <th scope="row" className="compare-viewers">{value(row.viewers)}</th>
                  <td className="compare-aws">
                    <span className="compare-price">{value(row.cloudfront)}</span>
                    <span className="compare-bar" aria-hidden="true">
                      <span
                        className="compare-bar-fill"
                        style={{ width: started ? `${(cloud / maxCloudfront) * 100}%` : '0%' }}
                      >
                        <span className="compare-bar-tcdn" style={{ width: `${tcdnWidth}%` }} />
                      </span>
                    </span>
                  </td>
                  <td className="compare-tcdn-col">
                    <span className="compare-price compare-price-accent">{value(row.torrentcdn)}</span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
