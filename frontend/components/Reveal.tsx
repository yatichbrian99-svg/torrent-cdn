'use client'

import { useEffect, useRef, useState, type ReactNode } from 'react'

/**
 * One-shot scroll reveal (fade + 16px rise) with an optional stagger delay.
 *
 * - Fires once per element, then unobserves — no work after it has played.
 * - `.js .reveal` in globals.css is what hides the element, so with JavaScript
 *   disabled nothing is ever hidden (the `.js` class is only added by the
 *   pre-paint script).
 * - `prefers-reduced-motion: reduce` neutralises the animation in CSS.
 */
export default function Reveal({
  children,
  delay = 0,
  className = '',
  as: Tag = 'div',
}: {
  children: ReactNode
  delay?: number
  className?: string
  as?: 'div' | 'section' | 'li' | 'tr'
}) {
  const ref = useRef<HTMLElement | null>(null)
  const [shown, setShown] = useState(false)

  useEffect(() => {
    const node = ref.current
    if (!node) return
    if (typeof IntersectionObserver !== 'function') {
      setShown(true)
      return
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setShown(true)
            observer.unobserve(entry.target)
          }
        }
      },
      { rootMargin: '0px 0px -2% 0px', threshold: 0.01 },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  const Component = Tag as any
  return (
    <Component
      ref={ref}
      className={`reveal${shown ? ' is-in' : ''}${className ? ` ${className}` : ''}`}
      style={{ '--reveal-delay': `${delay}ms` } as React.CSSProperties}
    >
      {children}
    </Component>
  )
}
