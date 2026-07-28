import type { Metadata } from 'next'
import './globals.css'
import dynamic from 'next/dynamic'

const Navbar = dynamic(() => import('@/components/Navbar'), { ssr: false })

export const metadata: Metadata = {
  title: 'TorrentCDN - P2P Content Delivery Network',
  description:
    'Deliver your video content cheaper and faster with WebTorrent-powered P2P CDN. The more viewers you have, the less you pay.',
  keywords: 'cdn, p2p, webtorrent, video streaming, content delivery, torrent',
  openGraph: {
    title: 'TorrentCDN - P2P Content Delivery Network',
    description:
      'Deliver your video content cheaper and faster with WebTorrent-powered P2P CDN.',
    type: 'website',
  },
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" className="dark">
      <body className="antialiased">
        <Navbar />
        <main className="min-h-screen pt-14">{children}</main>
      </body>
    </html>
  )
}
