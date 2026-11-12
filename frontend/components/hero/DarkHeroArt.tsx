'use client'

import { useEffect, useRef } from 'react'

/**
 * DARK THEME ("Fluxora") hero backdrop.
 *
 * Three cheap layers, all decorative (`aria-hidden`):
 *   1. a burnt-orange → red → black radial glow (stage lighting), gently
 *      breathing;
 *   2. a peer network: glowing nodes joined by curved paths whose dash offset
 *      animates so light appears to travel along the links;
 *   3. the page's grain (CSS, globals.css) and vertical grid lines.
 *
 * Everything is SVG/CSS — no canvas, no images, no third-party requests. The
 * whole backdrop pauses when scrolled out of view or when the tab is hidden,
 * and `prefers-reduced-motion` freezes it to a static frame (globals.css).
 */

const NODES = [
  { x: 140, y: 300, r: 4.5 },
  { x: 268, y: 208, r: 3.4 },
  { x: 342, y: 330, r: 5.2 },
  { x: 470, y: 236, r: 3.8 },
  { x: 556, y: 344, r: 4.6 },
  { x: 664, y: 250, r: 3.2 },
  { x: 760, y: 332, r: 4.2 },
  { x: 210, y: 410, r: 3.6 },
  { x: 420, y: 430, r: 4.0 },
  { x: 620, y: 424, r: 3.4 },
]

// Curved links (quadratic control offset gives the arcs a soft sag).
const LINKS: { from: number; to: number; bend: number; dur: number; delay: number }[] = [
  { from: 0, to: 2, bend: 30, dur: 9, delay: 0 },
  { from: 2, to: 4, bend: -26, dur: 11, delay: 1.2 },
  { from: 4, to: 6, bend: 24, dur: 10, delay: 2.4 },
  { from: 1, to: 3, bend: -20, dur: 8, delay: 0.6 },
  { from: 3, to: 5, bend: 22, dur: 12, delay: 1.8 },
  { from: 0, to: 7, bend: 26, dur: 10, delay: 3 },
  { from: 7, to: 8, bend: -18, dur: 9, delay: 0.9 },
  { from: 8, to: 9, bend: 20, dur: 11, delay: 2 },
  { from: 9, to: 6, bend: -24, dur: 10, delay: 1.5 },
  { from: 2, to: 8, bend: 18, dur: 13, delay: 2.7 },
]

function curve(link: (typeof LINKS)[number]) {
  const a = NODES[link.from]
  const b = NODES[link.to]
  const mx = (a.x + b.x) / 2
  const my = (a.y + b.y) / 2 + link.bend
  return `M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}`
}

export default function DarkHeroArt() {
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
    <div className="hero-dark-art" ref={ref} aria-hidden="true">
      <div className="hero-glow glow-breathe" />
      <div className="grid-lines hero-grid-lines" />
      <svg
        className="hero-network"
        viewBox="0 0 900 560"
        preserveAspectRatio="xMidYMid slice"
        role="presentation"
        focusable="false"
      >
        <defs>
          <radialGradient id="node-halo" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#FF5A1F" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#FF5A1F" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="link-stroke" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#D23A00" stopOpacity="0.15" />
            <stop offset="50%" stopColor="#FF5A1F" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#D23A00" stopOpacity="0.15" />
          </linearGradient>
        </defs>

        {LINKS.map((link, i) => (
          <path
            key={`base-${i}`}
            d={curve(link)}
            fill="none"
            stroke="url(#link-stroke)"
            strokeWidth="1"
            strokeDasharray="3 9"
          />
        ))}
        {LINKS.map((link, i) => (
          <path
            key={`trail-${i}`}
            d={curve(link)}
            fill="none"
            stroke="#FF5A1F"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeDasharray="26 240"
            className="trail"
            style={{ animationDuration: `${link.dur}s`, animationDelay: `${link.delay}s` }}
          />
        ))}

        {NODES.map((node, i) => (
          <g key={`node-${i}`}>
            <circle cx={node.x} cy={node.y} r={node.r * 6} fill="url(#node-halo)" />
            <circle cx={node.x} cy={node.y} r={node.r} fill="#FF5A1F" opacity="0.9" />
            <circle cx={node.x} cy={node.y} r={node.r * 0.45} fill="#FFD9C7" opacity="0.9" />
          </g>
        ))}
      </svg>
    </div>
  )
}
