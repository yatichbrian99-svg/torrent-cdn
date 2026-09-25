'use client'

// Root-level error boundary (catches errors in the root layout too, so it must
// define its own <html>/<body> and be fully self-contained — no shared imports).
// Replaces Next.js's raw "Application error: a client-side exception..." screen
// with a branded, recoverable one. The Reload button recovers from transient
// errors (e.g. loading a page mid-deploy while chunk hashes were rolling).
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string }
  reset: () => void
}) {
  return (
    <html lang="en">
      <body style={{ backgroundColor: 'var(--bg-primary)', color: 'var(--text-primary)', minHeight: '100vh', margin: 0, fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif', WebkitFontSmoothing: 'antialiased' }}>
        <div
          style={{
            minHeight: '100vh',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: '24px',
          }}
        >
          <div style={{ maxWidth: 420, width: '100%', textAlign: 'center' }}>
            <div
              style={{
                width: 56,
                height: 56,
                margin: '0 auto 20px',
                borderRadius: '14px',
                backgroundColor: 'rgba(255,107,107,0.1)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 22,
              }}
            >
              !
            </div>
            <h1 style={{ fontSize: 18, fontWeight: 700, letterSpacing: '-0.02em', margin: '0 0 8px' }}>
              Something went wrong
            </h1>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.6, margin: '0 0 24px' }}>
              A transient error interrupted the page load. This usually clears on
              refresh — your files and account are unaffected.
            </p>
            <button
              onClick={reset}
              style={{
                backgroundColor: 'var(--inverse-bg)',
                color: 'var(--bg-primary)',
                border: 'none',
                borderRadius: '8px',
                padding: '10px 22px',
                fontSize: 12,
                fontWeight: 600,
                cursor: 'pointer',
                fontFamily: 'inherit',
              }}
            >
              Reload page
            </button>
            <p style={{ fontSize: 11, color: 'var(--border-strong)', marginTop: 20 }}>
              Still stuck? Hard-refresh (Ctrl/Cmd + Shift + R) to pull the latest build.
            </p>
          </div>
        </div>
      </body>
    </html>
  )
}
