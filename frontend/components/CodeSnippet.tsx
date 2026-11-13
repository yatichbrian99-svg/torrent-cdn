import CopyButton from './CopyButton'

/**
 * The one-line embed snippet, syntax-coloured with accent + neutrals only.
 *
 * The literal value is `<div data-torrent-cdn="your-id"></div>` with straight
 * quotes — it has to be copy-pasteable into a customer's page and it matches
 * what public/sdk.js scans for (`data-torrent-cdn`). The colouring is done with
 * spans around the pieces, so the copied text and the DOM text are identical.
 */
export const EMBED_SNIPPET = '<div data-torrent-cdn="your-id"></div>'

export default function CodeSnippet({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`snippet${compact ? ' snippet-compact' : ''}`}>
      <div className="snippet-bar">
        <span className="terminal-dots" aria-hidden="true"><span /><span /><span /></span>
        <span className="snippet-lang label">html</span>
        <CopyButton value={EMBED_SNIPPET} />
      </div>
      <pre className="snippet-code"><code><span className="tok-punc">&lt;</span><span className="tok-tag">div</span> <span className="tok-attr">data-torrent-cdn</span><span className="tok-punc">=</span><span className="tok-str">&quot;your-id&quot;</span><span className="tok-punc">&gt;&lt;/</span><span className="tok-tag">div</span><span className="tok-punc">&gt;</span></code></pre>
    </div>
  )
}
