import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import './globals.css'

const inter = Inter({ subsets: ['latin'] })

export const metadata: Metadata = {
  title: 'Torrent CDN - P2P Content Delivery Network',
  description: 'Deliver your content cheaper and faster with WebTorrent-powered P2P CDN',
  keywords: 'cdn, p2p, webtorrent, video streaming, content delivery',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" className="dark">
      <body className={`${inter.className} antialiased`}>
        <nav className="fixed top-0 z-50 w-full border-b border-slate-800/50 bg-slate-900/80 backdrop-blur-xl">
          <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-4 sm:px-6 lg:px-8">
            <a href="/" className="flex items-center gap-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-primary-500 to-purple-600">
                <svg className="h-4 w-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                </svg>
              </div>
              <span className="text-lg font-bold text-white">Torrent CDN</span>
            </a>
            <div className="flex items-center gap-4">
              <a
                href="/dashboard"
                className="rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-primary-700"
              >
                Dashboard
              </a>
            </div>
          </div>
        </nav>
        <main className="min-h-screen pt-16">
          {children}
        </main>
      </body>
    </html>
  )
}
