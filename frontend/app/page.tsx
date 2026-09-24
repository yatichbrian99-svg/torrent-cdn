'use client'

import { useEffect } from 'react'
import PricingTable from '@/components/PricingTable'
import { sanitizeRefCode } from '@/lib/security'

const metrics = [
  { viewers: '1,000', cloudfront: '$45', torrentcdn: '$1.20' },
  { viewers: '10,000', cloudfront: '$450', torrentcdn: '$4.80' },
  { viewers: '100,000', cloudfront: '$4,500', torrentcdn: '$12.00' },
  { viewers: '1,000,000', cloudfront: '$45,000', torrentcdn: '$28.00' },
]

export default function LandingPage() {
  // Capture ?ref= referral code and persist it for the signup flow. Sanitize
  // at the boundary: only alphanumerics survive (matches the backend's 20-char
  // UPPER(referral_code) lookup), so junk can't ride along into storage.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const ref = params.get('ref')
    const clean = sanitizeRefCode(ref || '')
    if (clean) sessionStorage.setItem('torrent_cdn_ref', clean)
  }, [])

  return (
    <div>
      {/* Hero */}
      <section className="flex min-h-[90vh] items-center justify-center border-b" style={{ borderColor: 'var(--border)' }}>
        <div className="absolute inset-0 bg-grid" />
        <div className="relative z-10 mx-auto max-w-4xl px-6 text-center">
          <h1 className="text-5xl font-bold leading-tight tracking-[-0.02em] text-[color:var(--text-primary)] sm:text-6xl lg:text-7xl">
            Video delivery that gets cheaper as you scale.
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            TorrentCDN uses WebTorrent to turn your viewers into a P2P network. The more people watch, the less you pay.
          </p>
          <div className="mt-10 flex items-center justify-center gap-4">
            <a
              href="/dashboard"
              className="border px-6 py-3 text-sm font-medium transition-opacity hover:opacity-80"
              style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}
            >
              Start for free &rarr;
            </a>
            <a
              href="/embed/demo"
              className="border px-6 py-3 text-sm font-medium transition-opacity hover:opacity-80"
              style={{ borderColor: 'var(--inverse-bg)', color: '#fff' }}
            >
              Watch demo
            </a>
          </div>
          <p className="mt-4 text-sm" style={{ color: 'var(--text-faint)' }}>
            No credit card required &middot; 100GB free forever
          </p>
          <p className="mt-6 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
            Built for high concurrency video delivery
          </p>
        </div>
      </section>

      {/* Stats */}
      <section className="border-b border-t" style={{ borderColor: 'var(--border)' }}>
        <div className="mx-auto flex max-w-3xl divide-x" style={{ borderColor: 'var(--border)' }}>
          {['90% bandwidth reduction', '< 5ms peer discovery', 'Works in any browser'].map((s) => (
            <div key={s} className="flex-1 px-6 py-8 text-center font-mono text-xs uppercase tracking-[0.1em]" style={{ color: 'var(--text-muted)' }}>
              {s}
            </div>
          ))}
        </div>
      </section>

      {/* How It Works */}
      <section className="border-b py-20" style={{ borderColor: 'var(--border)' }}>
        <div className="mx-auto max-w-5xl px-6">
          <h2 className="text-2xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">How it works</h2>
          <div className="mt-12 grid gap-4 md:grid-cols-3">
            {[
              { num: '01', title: 'Upload your file', desc: 'Drop any video or audio file. We store it and generate a magnet link.' },
              { num: '02', title: 'Paste one line of code', desc: '' },
              { num: '03', title: 'Viewers share bandwidth', desc: 'Every viewer becomes a peer. Your costs stay flat while your audience grows.' },
            ].map((item) => (
              <div key={item.num} className="border p-6" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
                <p className="mb-2 font-mono text-sm font-bold text-[color:var(--text-primary)]">{item.num}</p>
                <h3 className="mb-2 text-sm font-semibold text-[color:var(--text-primary)]">{item.title}</h3>
                {item.num === '02' ? (
                  <div className="mt-2 border p-3" style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border)' }}>
                    <div className="mb-2 flex items-center gap-1 text-[10px] uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                      <div className="terminal-dots"><span /><span /><span /></div>
                    </div>
                    <code className="text-xs text-[color:var(--text-primary)]" style={{ fontFamily: 'monospace' }}>
                      &lt;div data-torrent-cdn=&ldquo;your-id&rdquo;&gt;&lt;/div&gt;
                    </code>
                  </div>
                ) : (
                  <p className="text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>{item.desc}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Cost Comparison */}
      <section className="border-b py-20" style={{ borderColor: 'var(--border)' }}>
        <div className="mx-auto max-w-4xl px-6">
          <h2 className="text-2xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">Pay less as you grow. Not more.</h2>
          <div className="mt-10">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b" style={{ borderColor: 'var(--border)' }}>
                  <th className="py-3 pr-4 text-left font-medium text-[color:var(--text-primary)]">Viewers</th>
                  <th className="py-3 pr-4 text-left font-medium text-[color:var(--text-primary)]">AWS CloudFront</th>
                  <th className="py-3 text-left font-medium text-[color:var(--text-primary)]">TorrentCDN</th>
                </tr>
              </thead>
              <tbody>
                {metrics.map((row) => (
                  <tr key={row.viewers} className="border-b" style={{ borderColor: 'var(--border)' }}>
                    <td className="py-3 pr-4 text-[color:var(--text-primary)]">{row.viewers}</td>
                    <td className="py-3 pr-4" style={{ color: 'var(--text-muted)' }}>{row.cloudfront}</td>
                    <td className="py-3 font-bold text-[color:var(--text-primary)]">{row.torrentcdn}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* Pricing */}
      <section className="border-b py-20" style={{ borderColor: 'var(--border)' }}>
        <div className="mx-auto max-w-5xl px-6">
          <h2 className="text-2xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">Simple pricing</h2>
          <p className="mt-2 text-sm" style={{ color: 'var(--text-muted)' }}>Start free. Upgrade when you need more.</p>
          <div className="mt-10"><PricingTable /></div>
        </div>
      </section>

      {/* FAQ */}
      <section className="border-b py-20" style={{ borderColor: 'var(--border)' }}>
        <div className="mx-auto max-w-4xl px-6">
          <h2 className="text-2xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">Frequently asked questions</h2>
          <div className="mt-10 divide-y" style={{ borderColor: 'var(--border)' }}>
            {[
              {
                q: "What browsers are supported?",
                a: "TorrentCDN works in all modern browsers that support WebRTC — Chrome, Firefox, Edge, Safari 15+, and all mobile browsers."
              },
              {
                q: "What happens if there are no peers?",
                a: "If no peers are available, the player automatically falls back to direct delivery from our origin server within 5 seconds. Your viewers never see a blank screen."
              },
              {
                q: "What file types are supported?",
                a: "We support MP4, WebM, MKV, MOV, AVI for video and MP3, WAV, OGG, M4A for audio."
              },
              {
                q: "What is the maximum file size?",
                a: "The maximum file size is 2GB per file. Larger files are available on the Business plan."
              },
              {
                q: "Is my content private?",
                a: "Your files are stored in object storage and served from unlisted public URLs — there's no public index or search, and only people with your embed link or magnet can access them. But links and magnets behave like keys: anyone who has one can view and re-share your content, and P2P delivery means viewers pass chunks directly between peers. Treat your links like a shared secret — TorrentCDN is for content you're comfortable sharing, not for confidential or DRM-protected material."
              },
              {
                q: "How does billing work?",
                a: "We measure bandwidth by the amount delivered from our origin servers. P2P bandwidth between peers is free and does not count toward your limit."
              },
              {
                q: "Can I use this on my own domain?",
                a: "Yes — Business plan includes custom domain support. You can serve the embed player and SDK from your own domain."
              },
              {
                q: "Do you have an API?",
                a: "Yes. Create an API key (format: tcdn_…) in your dashboard, or get a Bearer token via POST /auth/login (see the API docs). You can upload files, list files, and generate embed codes programmatically via our REST API."
              },
            ].map((item) => (
              <div key={item.q} className="py-5">
                <p className="text-sm font-semibold text-[color:var(--text-primary)]">{item.q}</p>
                <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>{item.a}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="py-8">
        <div className="mx-auto flex max-w-6xl flex-col gap-4 px-6 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm" style={{ color: 'var(--text-faint)' }}>TorrentCDN &copy; 2026</p>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <a href="https://github.com/yatichbrian99-svg/torrent-cdn" target="_blank" rel="noopener noreferrer" className="text-sm transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-faint)' }}>GitHub</a>
            <a href="/terms" className="text-sm transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-faint)' }}>Terms</a>
            <a href="/privacy" className="text-sm transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-faint)' }}>Privacy</a>
            <a href="/cookies" className="text-sm transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-faint)' }}>Cookies</a>
            <a href="/aup" className="text-sm transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-faint)' }}>AUP</a>
            <a href="/dmca" className="text-sm transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-faint)' }}>DMCA</a>
            <a href="/docs" className="text-sm transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-faint)' }}>Docs</a>
          </div>
        </div>
        <a href="https://maidensail.com/startup/torrentcdn" rel="dofollow"><img src="https://maidensail.com/badge/torrentcdn.svg" alt="Listed on Maidensail" height="36" /></a>
      </footer>
    </div>
  )
}
