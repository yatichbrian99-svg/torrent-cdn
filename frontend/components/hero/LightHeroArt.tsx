'use client'

import { useEffect, useRef } from 'react'

/**
 * LIGHT THEME ("Guardbase") hero backdrop.
 *
 * A halftone / dithered dot field that reads as terrain, with a dotted peer
 * mesh laid over it and a pale-blue → royal-blue gradient sweeping to the
 * bottom right. It is the hero's BACKGROUND: absolutely positioned, full
 * width, behind the copy, and masked by CSS so it fades to nothing under the
 * headline (see `.hero-plate` in globals.css) — the text always sits on a
 * near-clean ground.
 *
 * Inline SVG only: no image files, no canvas, no third-party assets. Dot
 * coordinates are fixed at module scope (deterministic across server and
 * client, so hydration never mismatches), and the dots are split into two
 * parity groups so narrow viewports can drop half of them without changing
 * the markup. Animations pause when the art is off-screen or the tab is
 * hidden, and `prefers-reduced-motion` freezes them (globals.css).
 */

const COLS = 40
const ROWS = 22
const SPACING = 26

const terrain = (x: number, y: number) => {
  const a = Math.sin(x * 0.11) * Math.cos(y * 0.09)
  const b = Math.sin((x + y) * 0.045) * 0.7
  const c = Math.cos(x * 0.031 - y * 0.052) * 0.5
  return (a + b + c) / 2.2 // -1 … 1
}

// Two groups by (row + col) parity: same field, half the dots each, so a phone
// can render every other dot ("reduced density") without new markup.
const DOTS_A: { cx: number; cy: number; r: number; o: number }[] = []
const DOTS_B: { cx: number; cy: number; r: number; o: number }[] = []
for (let row = 0; row < ROWS; row++) {
  for (let col = 0; col < COLS; col++) {
    const h = terrain(col, row) // -1 … 1
    const scaled = (h + 1) / 2
    const dot = {
      cx: col * SPACING + 13,
      cy: row * SPACING + 13,
      r: 0.6 + scaled * 4.2,
      o: 0.3 + scaled * 0.7,
    }
    ;((row + col) % 2 === 0 ? DOTS_A : DOTS_B).push(dot)
  }
}

// Peer mesh: nodes clustered toward the right and bottom-right, where the mask
// lets the field through, linked by dotted lines with a light pulse running
// along each one.
const NODES = [
  { x: 420, y: 168 },
  { x: 560, y: 104 },
  { x: 690, y: 214 },
  { x: 830, y: 132 },
  { x: 960, y: 246 },
  { x: 1096, y: 168 },
  { x: 486, y: 336 },
  { x: 646, y: 414 },
  { x: 812, y: 352 },
  { x: 968, y: 452 },
  { x: 1112, y: 372 },
  { x: 742, y: 540 },
]
const EDGES: { a: number; b: number; dur: number; delay: number }[] = [
  { a: 0, b: 1, dur: 6.4, delay: 0 },
  { a: 1, b: 2, dur: 7.2, delay: 0.8 },
  { a: 2, b: 3, dur: 6.8, delay: 1.6 },
  { a: 3, b: 4, dur: 7.6, delay: 0.4 },
  { a: 4, b: 5, dur: 6.6, delay: 2.2 },
  { a: 0, b: 6, dur: 7.8, delay: 1.1 },
  { a: 6, b: 7, dur: 6.2, delay: 2.8 },
  { a: 2, b: 7, dur: 8.2, delay: 0.6 },
  { a: 7, b: 8, dur: 7, delay: 1.9 },
  { a: 8, b: 9, dur: 8, delay: 0.2 },
  { a: 4, b: 8, dur: 6.9, delay: 3.1 },
  { a: 5, b: 10, dur: 7.4, delay: 1.4 },
  { a: 9, b: 10, dur: 6.5, delay: 2.6 },
  { a: 8, b: 11, dur: 7.9, delay: 0.9 },
  { a: 11, b: 9, dur: 6.7, delay: 3.4 },
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
    <div className="hero-plate" ref={ref} aria-hidden="true">
      <div className="hero-plate-inner art-drift">
        <svg
          viewBox="0 0 1200 640"
          className="hero-plate-svg"
          preserveAspectRatio="xMidYMid slice"
          role="presentation"
          aria-hidden="true"
          focusable="false"
        >
          <defs>
            <linearGradient id="light-halftone" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="#E2EBFB" />
              <stop offset="34%" stopColor="#A8BEEE" />
              <stop offset="62%" stopColor="#4C79E4" />
              <stop offset="84%" stopColor="#154ECD" />
              <stop offset="100%" stopColor="#0C2F86" />
            </linearGradient>
            <linearGradient id="light-mesh" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="#154ECD" stopOpacity="0.28" />
              <stop offset="100%" stopColor="#154ECD" stopOpacity="0.7" />
            </linearGradient>
          </defs>

          <g fill="url(#light-halftone)">
            {DOTS_A.map((dot, i) => (
              <circle key={`a${i}`} cx={dot.cx} cy={dot.cy} r={dot.r} opacity={dot.o} />
            ))}
            <g className="dots-alt">
              {DOTS_B.map((dot, i) => (
                <circle key={`b${i}`} cx={dot.cx} cy={dot.cy} r={dot.r} opacity={dot.o} />
              ))}
            </g>
          </g>

          <g
            stroke="url(#light-mesh)"
            strokeWidth="1.7"
            strokeDasharray="1.5 7"
            strokeLinecap="round"
            fill="none"
          >
            {EDGES.map((edge, i) => (
              <line
                key={`e${i}`}
                x1={NODES[edge.a].x}
                y1={NODES[edge.a].y}
                x2={NODES[edge.b].x}
                y2={NODES[edge.b].y}
              />
            ))}
          </g>
          {/* Light travelling along the links. Dash length is tuned to the
              short edges; the offset sweep is long enough to read as a pulse. */}
          <g stroke="#154ECD" strokeWidth="2.6" strokeLinecap="round" fill="none" opacity="0.5">
            {EDGES.map((edge, i) => (
              <line
                key={`p${i}`}
                className="mesh-pulse"
                x1={NODES[edge.a].x}
                y1={NODES[edge.a].y}
                x2={NODES[edge.b].x}
                y2={NODES[edge.b].y}
                strokeDasharray="34 420"
                style={{ animationDuration: `${edge.dur}s`, animationDelay: `${edge.delay}s` }}
              />
            ))}
          </g>
          <g fill="#154ECD">
            {NODES.map((node, i) => (
              <g key={`n${i}`}>
                <circle cx={node.x} cy={node.y} r="13" opacity="0.13" />
                <circle cx={node.x} cy={node.y} r="4.4" opacity="0.95" />
                <circle cx={node.x} cy={node.y} r="1.6" fill="#FEFEFD" opacity="0.9" />
              </g>
            ))}
          </g>
        </svg>
      </div>
    </div>
  )
}
