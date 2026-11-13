import type { Metadata } from 'next'
import Script from 'next/script'
import { headers } from 'next/headers'
import localFont from 'next/font/local'
import './globals.css'
import Navbar from '@/components/Navbar'
import LegacyChrome from '@/components/LegacyChrome'
import ThemeProvider from '@/components/ThemeProvider'

// Nonce-based CSP (see middleware.ts) requires per-request rendering so Next.js
// can stamp the per-request nonce onto its scripts. Static prerendering would
// bake in scripts without nonces and break under the strict CSP.
export const dynamic = 'force-dynamic'

// Inter, self-hosted.
//
// This used to be `Inter` from `next/font/google`, which downloaded the font
// from fonts.googleapis.com during `next build`. That fetch is the source of
// the intermittent build failure ("TypeError: Cannot read properties of null"
// in @next/font's Google loader) and it made the build depend on a third party
// being reachable. The file below is the SAME latin subset Google served, so the
// glyphs and metrics are identical; it is an OFL-1.1 licensed variable font and
// its licence ships next to it (app/fonts/LICENSE-Inter.txt).
const inter = localFont({
  src: [
    { path: './fonts/inter-latin.woff2', weight: '400', style: 'normal' },
    { path: './fonts/inter-latin.woff2', weight: '500', style: 'normal' },
    { path: './fonts/inter-latin.woff2', weight: '600', style: 'normal' },
    { path: './fonts/inter-latin.woff2', weight: '700', style: 'normal' },
  ],
  display: 'swap',
  variable: '--font-inter',
  fallback: ['system-ui', 'sans-serif'],
  adjustFontFallback: 'Arial',
})

// Instrument Serif italic — the single display accent word in the dark hero.
// OFL-1.1, latin subset, licence beside the file. NOT preloaded: it is one word
// in one heading, so `font-display: swap` plus the serif fallback is enough and
// it never competes with the hero's real (Inter) text for bandwidth.
const serif = localFont({
  src: [{ path: './fonts/instrument-serif-italic-latin.woff2', weight: '400', style: 'italic' }],
  display: 'swap',
  variable: '--font-serif',
  preload: false,
  fallback: ['Georgia', 'Times New Roman', 'serif'],
})

// JetBrains Mono — labels, buttons, numerals, code. OFL-1.1, latin subset,
// variable (400–500), licence beside the file. Not preloaded: everything that
// uses it sits below the first screen except the small nav CTA label.
const mono = localFont({
  src: [{ path: './fonts/jetbrains-mono-latin.woff2', weight: '400 500', style: 'normal' }],
  display: 'swap',
  variable: '--font-mono',
  preload: false,
  fallback: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
})

// Pre-paint theme resolution — MUST stay inline (it runs before first paint,
// so it cannot wait for the bundle) and unblocking (CSP: script-src allows
// 'self' static files, but a SYNC network fetch here would delay every page).
//
// Mirrors lib/theme.ts — the storage key/values and the resolution rule are
// duplicated here on purpose; lib/__tests__/theme-parity.test.ts asserts the
// two stay in agreement, and that this is rendered through next/script so the
// per-request CSP nonce is stamped on it.
// Frozen surfaces (/embed, /share) keep the OLD resolution rule — dark unless
// the visitor explicitly chose light — because their appearance must not change
// with the redesign. Same inline/CSP rules as above.
const FROZEN_THEME_SCRIPT = `
(function(){var d=document.documentElement;var s=null;try{s=localStorage.getItem('tcdn_theme');}catch(e){}
var t=(s==='light')?'light':'dark';
d.setAttribute('data-theme',t);d.setAttribute('data-theme-resolved','1');
d.style.colorScheme=t;})();
`

const THEME_SCRIPT = `
(function(){var d=document.documentElement;var s=null;try{s=localStorage.getItem('tcdn_theme');}catch(e){}
var m=window.matchMedia?window.matchMedia('(prefers-color-scheme: dark)'):null;
var dark=false;if(m){if(m.matches){dark=true;}}
var t=(s==='light'||s==='dark')?s:(dark?'dark':'light');
d.setAttribute('data-theme',t);d.setAttribute('data-theme-resolved','1');
d.style.colorScheme=t;d.classList.add('js');
function tm(){var mc=document.querySelector('meta[name="theme-color"]');if(mc){mc.setAttribute('content',t==='dark'?'#000000':'#FEFEFD');}}
tm();if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',tm);}})();
`

export const metadata: Metadata = {
  metadataBase: new URL('https://www.torrentcdn.store'),
  title: {
    default: 'TorrentCDN — Peer-Assisted Delivery for Media, Live and AI',
    template: '%s | TorrentCDN',
  },
  description:
    'TorrentCDN turns viewers into a peer network. Deliver video and audio for less, run live streams on the same model, and use an OpenAI-compatible inference API in private alpha. Start free with 100GB bandwidth.',
  keywords: [
    'P2P CDN',
    'peer-assisted delivery',
    'WebTorrent',
    'peer to peer CDN',
    'video delivery',
    'live streaming',
    'AI inference API',
    'OpenAI-compatible API',
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
    title: 'TorrentCDN — Peer-Assisted Delivery for Media, Live and AI',
    description:
      'Deliver video and audio for less, run live streams on the same peer network, and use an OpenAI-compatible inference API in private alpha.',
    images: [
      {
        // Absolute URL — required by Twitter/X, LinkedIn, Slack, Discord,
        // WhatsApp link previews. Static asset (public/og-image.png); the
        // old next/og dynamic route emitted a 0-byte image, so previews
        // rendered blank everywhere.
        url: 'https://www.torrentcdn.store/og-image.png',
        width: 1200,
        height: 630,
        alt: 'TorrentCDN — peer-assisted media, live and AI',
      },
    ],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'TorrentCDN — Peer-Assisted Delivery for Media, Live and AI',
    description:
      'Media, live and AI on one peer network. TorrentCDN uses WebTorrent to turn viewers into delivery capacity.',
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
    'Peer-assisted delivery platform: video and audio delivery over WebTorrent, live streaming on the same model, and an OpenAI-compatible inference API in private alpha.',
  url: 'https://www.torrentcdn.store',
  offers: {
    '@type': 'Offer',
    price: '0',
    priceCurrency: 'USD',
    description: 'Free tier with 100GB bandwidth',
  },
}

/**
 * Routes whose appearance is FROZEN by the redesign contract: the customer
 * player surfaces. middleware.ts forwards the request path as `x-pathname` so
 * the layout can pick them without turning every page into a client component.
 */
function isFrozenSurface(pathname: string): boolean {
  return pathname === '/embed' || pathname.startsWith('/embed/') ||
    pathname === '/share' || pathname.startsWith('/share/')
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Explicit nonce pass-through (the Next.js CSP-guide pattern): middleware
  // puts the full CSP on the request headers; extract our nonce so the theme
  // script executes even if framework auto-stamping ever misses this tag.
  const requestHeaders = await headers()
  const csp = requestHeaders.get('content-security-policy') ?? ''
  const nonce = csp.match(/nonce-([0-9a-zA-Z]+)/)?.[1] ?? ''
  const pathname = requestHeaders.get('x-pathname') ?? '/'
  const frozen = isFrozenSurface(pathname)

  return (
    <html lang="en" {...(frozen ? { 'data-frozen-surface': 'true' } : {})}>
      <head>
        <meta name="theme-color" content={frozen ? '#000000' : '#FEFEFD'} />
        {/* Pre-paint theme: resolves the stored choice (or the OS preference)
            BEFORE the browser paints anything, because it sits in <head> and
            runs synchronously while the document is parsed.

            It carries the per-request CSP nonce, which is what makes it
            allowed under `script-src 'nonce-…' 'strict-dynamic'` — no
            'unsafe-inline', no loosening of the policy. (The 2025 incident was
            a raw inline script WITHOUT a nonce: the browser blocked it, the
            theme guard never released and the site went black. Both halves of
            that lesson are pinned: the nonce is passed explicitly here, and
            globals.css still self-releases the guard even if this script never
            runs.) */}
        <script
          id="theme-init"
          nonce={nonce}
          dangerouslySetInnerHTML={{ __html: frozen ? FROZEN_THEME_SCRIPT : THEME_SCRIPT }}
        />
        <Script
          id="jsonld-org"
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify(jsonLd).replace(/</g, '\\u003c'),
          }}
        />
        {/* No font preconnect: the fonts are self-hosted and preloaded by
            next/font, so there is no Google origin to warm up. */}
      </head>
      <body
        className={`${inter.className} ${inter.variable} ${serif.variable} ${mono.variable}`}
      >
        {/* Frozen surfaces keep the old resolution rule (dark unless light was
            chosen), so the live OS watcher must not run there — it would
            re-apply the system preference on top of the frozen script. */}
        {!frozen && <ThemeProvider />}
        {frozen ? <LegacyChrome /> : <Navbar />}
        <main className="site-main min-h-screen">{children}</main>
      </body>
    </html>
  )
}
