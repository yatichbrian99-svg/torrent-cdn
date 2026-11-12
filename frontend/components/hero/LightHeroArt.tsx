'use client'

import { useEffect, useRef } from 'react'

/**
 * LIGHT THEME ("Guardbase") hero plate.
 *
 * A halftone / dithered dot field that reads as terrain, with a dotted peer
 * mesh laid over it and a pale-blue → royal-blue gradient sweeping to the
 * bottom right. Inline SVG only: no image files, no canvas, no third-party
 * assets, and `aria-hidden` because it carries no information.
 *
 * Dot count is fixed at module scope (deterministic across server and client,
 * so hydration never mismatches). The whole field drifts on one CSS transform;
 * the animation pauses when the plate is off-screen or the tab is hidden.
 */

const COLS = 34
const ROWS = 22
const SPACING = 23

const terrain = (x: number, y: number) => {
  const a = Math.sin(x * 0.11) * Math.cos(y * 0.09)
  const b = Math.sin((x + y) * 0.045) * 0.7
  const c = Math.cos(x * 0.031 - y * 0.052) * 0.5
  return (a + b + c) / 2.2 // -1 … 1
}

const DOTS: { cx: number; cy: number; r: number; o: number }[] = []
for (let row = 0; row < ROWS; row++) {
  for (let col = 0; col < COLS; col++) {
    const x = col * SPACING + 12
    const y = row * SPACING + 12
    const h = terrain(col, row) // -1 … 1
    const scaled = (h + 1) / 2
    DOTS.push({
      cx: x,
      cy: y,
      r: 0.55 + scaled * 3.9,
      o: 0.22 + scaled * 0.78,
    })
  }
}

// Peer mesh: a handful of nodes linked by dotted lines, positioned over the
// terrain so the field reads as "network across landscape".
const NODES = [
  { x: 150, y: 210 },
  { x: 268, y: 132 },
  { x: 352, y: 226 },
  { x: 470, y: 168 },
  { x: 560, y: 262 },
  { x: 640, y: 180 },
  { x: 236, y: 322 },
  { x: 392, y: 344 },
  { x: 520, y: 380 },
  { x: 660, y: 336 },
]
const EDGES: [number, number][] = [
  [0, 1], [1, 2], [2, 3], [3, 4], [4, 5],
  [0, 6], [6, 7], [2, 7], [7, 8], [8, 9], [4, 8], [5, 9],
]

export default function LightHeroArt() {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const node = ref.current
    if (!node) return
    const setPaused = (paused: boolean) => node.classList.toggle('art-paused', paused)

    const onVisibility = () => setPaused(document.hidden)
    document.addEventListener('visibilitychange', onVisibility)

    let observer: IntersectionObserver | null = null
    if (typeof IntersectionObserver === 'function') {
      observer = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) setPaused(!entry.isIntersecting || document.hidden)
        },
        { threshold: 0.01 },
      )
      observer.observe(node)
    }

    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      observer?.disconnect()
    }
  }, [])

  return (
    <div className="hero-plate" ref={ref}>
      <div className="hero-plate-inner art-drift">
        <svg
          viewBox="0 0 800 520"
          className="hero-plate-svg"
          role="presentation"
          aria-hidden="true"
          focusable="false"
        >
          <defs>
            <linearGradient id="light-halftone" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="#DEE8FB" />
              <stop offset="38%" stopColor="#9DB5EC" />
              <stop offset="68%" stopColor="#3E6FE2" />
              <stop offset="88%" stopColor="#154ECD" />
              <stop offset="100%" stopColor="#0F3BA0" />
            </linearGradient>
            <linearGradient id="light-mesh" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="#154ECD" stopOpacity="0.18" />
              <stop offset="100%" stopColor="#154ECD" stopOpacity="0.55" />
            </linearGradient>
            <radialGradient id="light-fade" cx="0.8" cy="0.86" r="1.02">
              <stop offset="0%" stopColor="#fff" stopOpacity="1" />
              <stop offset="62%" stopColor="#fff" stopOpacity="0.82" />
              <stop offset="100%" stopColor="#fff" stopOpacity="0.18" />
            </radialGradient>
            <mask id="light-mask">
              <rect width="800" height="520" fill="url(#light-fade)" />
            </mask>
          </defs>

          <g mask="url(#light-mask)" fill="url(#light-halftone)">
            {DOTS.map((dot, i) => (
              <circle key={i} cx={dot.cx} cy={dot.cy} r={dot.r} opacity={dot.o} />
            ))}
          </g>

          <g stroke="url(#light-mesh)" strokeWidth="1.4" strokeDasharray="2 7" strokeLinecap="round">
            {EDGES.map(([a, b], i) => (
              <line key={i} x1={NODES[a].x} y1={NODES[a].y} x2={NODES[b].x} y2={NODES[b].y} />
            ))}
          </g>
          <g fill="#154ECD">
            {NODES.map((node, i) => (
              <g key={i}>
                <circle cx={node.x} cy={node.y} r="9" opacity="0.10" />
                <circle cx={node.x} cy={node.y} r="3" opacity="0.9" />
              </g>
            ))}
          </g>
        </svg>
      </div>
    </div>
  )
}
