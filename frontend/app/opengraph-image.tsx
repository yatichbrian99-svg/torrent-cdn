import { ImageResponse } from 'next/og'

export const runtime = 'edge'

export const size = { width: 1200, height: 630 }
export const contentType = 'image/png'

export default function Image() {
  return new ImageResponse(
    (
      <div
        style={{
          width: '100%',
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#000',
          color: '#fff',
          fontFamily: 'system-ui, -apple-system, sans-serif',
          padding: '60px',
        }}
      >
        {/* Grid background */}
        <div
          style={{
            position: 'absolute',
            inset: 0,
            backgroundImage:
              'linear-gradient(rgba(255,255,255,0.05) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.05) 1px, transparent 1px)',
            backgroundSize: '40px 40px',
          }}
        />
        {/* Content */}
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            position: 'relative',
            zIndex: 1,
          }}
        >
          <h1
            style={{
              fontSize: '72px',
              fontWeight: 700,
              letterSpacing: '-0.02em',
              textAlign: 'center',
              lineHeight: 1.1,
              margin: 0,
            }}
          >
            TorrentCDN
          </h1>
          <p
            style={{
              fontSize: '28px',
              color: '#555',
              textAlign: 'center',
              marginTop: '20px',
              letterSpacing: '-0.01em',
            }}
          >
            P2P Video Delivery That Gets Cheaper As You Scale
          </p>
          <div
            style={{
              display: 'flex',
              gap: '16px',
              marginTop: '32px',
            }}
          >
            {['90% bandwidth reduction', '<5ms peer discovery', 'Works in any browser'].map(
              (s) => (
                <span
                  key={s}
                  style={{
                    fontSize: '16px',
                    color: '#555',
                    fontFamily: 'monospace',
                    letterSpacing: '0.1em',
                    textTransform: 'uppercase',
                    border: '1px solid #222',
                    padding: '8px 16px',
                  }}
                >
                  {s}
                </span>
              )
            )}
          </div>
        </div>
      </div>
    ),
    {
      ...size,
    }
  )
}
