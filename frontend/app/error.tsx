'use client'

// Page-level error boundary. Rendered inside the layout, so it can use normal
// styling. `reset` re-renders the page (recovering from transient errors); a
// full reload button is the fallback for anything persistent.
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string }
  reset: () => void
}) {
  return (
    <div
      style={{
        minHeight: '60vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '24px',
      }}
    >
      <div style={{ maxWidth: 420, width: '100%', textAlign: 'center' }}>
        <div
          style={{
            width: 52,
            height: 52,
            margin: '0 auto 18px',
            borderRadius: '13px',
            backgroundColor: 'rgba(255,107,107,0.1)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 20,
          }}
        >
          ⚠️
        </div>
        <h1 style={{ fontSize: 17, fontWeight: 700, letterSpacing: '-0.02em', margin: '0 0 8px' }}>
          Something went wrong
        </h1>
        <p style={{ fontSize: 13, color: '#555555', lineHeight: 1.6, margin: '0 0 22px' }}>
          This page hit a temporary error. Try again — your files and account are
          safe.
        </p>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
          <button
            onClick={reset}
            style={{
              backgroundColor: '#fff',
              color: '#000',
              border: 'none',
              borderRadius: '8px',
              padding: '10px 20px',
              fontSize: 12,
              fontWeight: 600,
              cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            Try again
          </button>
          <button
            onClick={() => (window.location.href = '/')}
            style={{
              backgroundColor: 'transparent',
              color: '#888888',
              border: '1px solid #222222',
              borderRadius: '8px',
              padding: '10px 20px',
              fontSize: 12,
              fontWeight: 500,
              cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            Go home
          </button>
        </div>
      </div>
    </div>
  )
}
