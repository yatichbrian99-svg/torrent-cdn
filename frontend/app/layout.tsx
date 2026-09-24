import type { Metadata } from 'next'
import Script from 'next/script'
import { headers } from 'next/headers'
import { Inter } from 'next/font/google'
import './globals.css'
import Navbar from '@/components/Navbar'

// Nonce-based CSP (see middleware.ts) requires per-request rendering so Next.js
// can stamp the per-request nonce onto its scripts. Static prerendering would
// bake in scripts without nonces and break under the strict CSP.
export const dynamic = 'force-dynamic'

const inter = Inter({ subsets: ['latin'], weight: ['400', '500', '600', '700'] })

// Pre-paint theme resolution — MUST stay inline (it runs before first paint,
// so it cannot wait for the bundle) and unblocking (CSP: script-src allows
// 'self' static files, but a SYNC network fetch here would delay every page).
// Mirrors lib/theme.ts — the storage key/values are duplicated here on
// purpose; a test asserts the two stay in agreement.
const THEME_SCRIPT = `
(function(){try{var t=localStorage.getItem('tcdn_theme')==='light'?'light':'dark';document.documentElement.setAttribute('data-theme',t);}catch(e){}})();
document.documentElement.setAttribute('data-theme-resolved','1');
`

export const metadata: Metadata = {
  metadataBase: new URL('https://www.torrentcdn.store'),
  title: {
    default: 'TorrentCDN — P2P Video Delivery That Gets Cheaper As You Scale',
    template: '%s | TorrentCDN',
  },
  description:
    'TorrentCDN uses WebTorrent P2P technology to deliver video at a fraction of traditional CDN costs. The more people watch, the less you pay. Start free with 100GB bandwidth.',
  keywords: [
    'P2P CDN',
    'video delivery',
    'WebTorrent',
    'peer to peer CDN',
    'cheap video streaming',
    'video hosting',
    'TorrentCDN',
    'P2P streaming',
    'cost effective CDN',
  ],
  authors: [{ name: 'TorrentCDN' }],
  creator: 'TorrentCDN',
  publisher: 'TorrentCDN',
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      'max-video-preview': -1,
      'max-image-preview': 'large',
      'max-snippet': -1,
    },
  },
  openGraph: {
    type: 'website',
    locale: 'en_US',
    url: 'https://www.torrentcdn.store',
    siteName: 'TorrentCDN',
    title: 'TorrentCDN — P2P Video Delivery That Gets Cheaper As You Scale',
    description:
      'TorrentCDN uses WebTorrent P2P technology to deliver video at a fraction of traditional CDN costs. The more people watch, the less you pay.',
    images: [
      {
        // Absolute URL — required by Twitter/X, LinkedIn, Slack, Discord,
        // WhatsApp link previews. Static asset (public/og-image.png); the
        // old next/og dynamic route emitted a 0-byte image, so previews
        // rendered blank everywhere.
        url: 'https://www.torrentcdn.store/og-image.png',
        width: 1200,
        height: 630,
        alt: 'TorrentCDN — P2P Video Delivery',
      },
    ],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'TorrentCDN — P2P Video Delivery That Gets Cheaper As You Scale',
    description:
      'Deliver video cheaper as you scale. TorrentCDN uses WebTorrent P2P technology.',
    images: ['https://www.torrentcdn.store/og-image.png'],
    creator: '@torrentcdn',
  },
  alternates: {
    canonical: 'https://www.torrentcdn.store',
  },
  category: 'technology',
}

const jsonLd = {
  '@context': 'https://schema.org',
  '@type': 'SoftwareApplication',
  name: 'TorrentCDN',
  applicationCategory: 'Multimedia',
  operatingSystem: 'Web',
  description:
    'P2P video delivery platform that uses WebTorrent to reduce CDN costs. The more people watch, the less you pay.',
  url: 'https://www.torrentcdn.store',
  offers: {
    '@type': 'Offer',
    price: '0',
    priceCurrency: 'USD',
    description: 'Free tier with 100GB bandwidth',
  },
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Explicit nonce pass-through (the Next.js CSP-guide pattern): middleware
  // puts the full CSP on the request headers; extract our nonce so the theme
  // script executes even if framework auto-stamping ever misses this tag.
  const csp = (await headers()).get('content-security-policy') ?? ''
  const nonce = csp.match(/nonce-([0-9a-zA-Z]+)/)?.[1] ?? ''

  return (
    <html lang="en">
      <head>
        <Script
          id="jsonld-org"
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(jsonLd).replace(/</g, '\\u003c'),
          }}
        />
        {/* Preconnect to performance origins */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      </head>
      <body className={inter.className}>
        {/* Pre-paint theme: resolves the persisted theme before first paint.
            next/script (beforeInteractive) is REQUIRED here — middleware sets
            a nonce-based CSP, so only nonce-stamped inline scripts execute.
            The raw-tag form rendered without a nonce, the browser blocked it,
            and combined with the old visibility:hidden theme guard that
            blacked out the entire deployed site. The parity test pins this. */}
        <Script
          id="theme-init"
          strategy="beforeInteractive"
          nonce={nonce}
          dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }}
        />
        <Navbar />
        <main className="min-h-screen pt-12">{children}</main>
      </body>
    </html>
  )
}
