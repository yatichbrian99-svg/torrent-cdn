'use client'

import { useId, useState } from 'react'
import { Minus, Plus } from 'lucide-react'

/**
 * FAQ accordion. Questions and answers are byte-identical to the static list
 * they replace; only the interaction is new.
 *
 * Accessibility:
 *  - the trigger is a real <button> with aria-expanded + aria-controls,
 *  - each answer is a labelled region (aria-labelledby the trigger),
 *  - answers stay in the DOM when collapsed (CSS-only collapse) so search
 *    engines and no-JS readers still get the full text; collapsed answers are
 *    aria-hidden so they are not announced as visible content,
 *  - the whole header row is the click target (≥44px), and Enter/Space work
 *    for free because it is a button.
 */
export default function FaqAccordion({ items }: { items: { q: string; a: string }[] }) {
  const [open, setOpen] = useState<number | null>(null)
  const baseId = useId()

  return (
    <div className="faq">
      {items.map((item, index) => {
        const isOpen = open === index
        const triggerId = `${baseId}-q${index}`
        const panelId = `${baseId}-a${index}`
        return (
          <div key={item.q} className={`faq-item${isOpen ? ' is-open' : ''}`}>
            <h3 className="faq-heading">
              <button
                type="button"
                id={triggerId}
                className="faq-trigger"
                aria-expanded={isOpen}
                aria-controls={panelId}
                onClick={() => setOpen(isOpen ? null : index)}
              >
                <span className="faq-question">{item.q}</span>
                <span className="faq-icon" aria-hidden="true">
                  {isOpen ? <Minus size={16} /> : <Plus size={16} />}
                </span>
              </button>
            </h3>
            <div
              id={panelId}
              role="region"
              aria-labelledby={triggerId}
              aria-hidden={!isOpen}
              className="faq-panel"
            >
              <div className="faq-panel-inner">
                <p className="faq-answer">{item.a}</p>
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}
