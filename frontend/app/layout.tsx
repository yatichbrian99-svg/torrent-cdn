import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import './globals.css'
import dynamic from 'next/dynamic'

const inter = Inter({ subsets: ['latin'], weight: ['400', '500', '600', '700'] })
const Navbar = dynamic(() => import('@/components/Navbar'), { ssr: false })

export const metadata: Metadata = {
  title: 'TorrentCDN — P2P Video Delivery',
  description: 'Deliver video cheaper as you scale',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={inter.className}>
        <Navbar />
        <main className="min-h-screen pt-12">{children}</main>
      </body>
    </html>
  )
}
