'use client'

import PricingTable from '@/components/PricingTable'

const metrics = [
  { viewers: '1,000', cloudfront: '$45', torrentcdn: '$1.20' },
  { viewers: '10,000', cloudfront: '$450', torrentcdn: '$4.80' },
  { viewers: '100,000', cloudfront: '$4,500', torrentcdn: '$12.00' },
  { viewers: '1,000,000', cloudfront: '$45,000', torrentcdn: '$28.00' },
]

export default function LandingPage() {
  return (
    <div>
      {/* ─── Hero ─── */}
      <section className="flex min-h-[90vh] items-center justify-center border-b" style={{ borderColor: '#222' }}>
        <div className="absolute inset-0 bg-grid" />
        <div className="relative z-10 mx-auto max-w-4xl px-6 text-center">
          <div
            className="mx-auto mb-6 inline-block border px-3 py-1 font-mono text-[11px] font-bold uppercase tracking-[0.1em]"
            style={{ borderColor: '#333', color: 'var(--accent)' }}
          >
            Now in public beta
          </div>
          <h1
            className="text-5xl font-bold leading-tight tracking-[-0.02em] sm:text-6xl lg:text-7xl"
            style={{ color: '#fff' }}
          >
            Video delivery that gets{' '}
            <span style={{ color: 'var(--accent)' }}>cheaper as you scale</span>.
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg leading-relaxed" style={{ color: '#999' }}>
            TorrentCDN uses WebTorrent to turn your viewers into a P2P network.
            The more people watch, the less you pay.
          </p>
          <div className="mt-10 flex items-center justify-center gap-4">
            <a
              href="/dashboard"
              className="border px-6 py-3 text-sm font-medium text-white transition-opacity hover:opacity-90"
              style={{ backgroundColor: 'var(--accent)', borderColor: 'var(--accent)' }}
            >
              Start for free &rarr;
            </a>
            <a
              href="/embed/demo"
              className="border px-6 py-3 text-sm font-medium transition-opacity hover:opacity-80"
              style={{ borderColor: '#fff', color: '#fff' }}
            >
              Watch demo
            </a>
          </div>
          <p className="mt-4 text-sm" style={{ color: '#444' }}>
            No credit card required &middot; 100GB free forever
          </p>
        </div>
      </section>

      {/* ─── Stats ─── */}
      <section className="border-b border-t" style={{ borderColor: '#222' }}>
        <div className="mx-auto flex max-w-3xl divide-x" style={{ borderColor: '#222' }}>
          {[
            '90% bandwidth reduction',
            '< 5ms peer discovery',
            'Works in any browser',
          ].map((s) => (
            <div key={s} className="flex-1 px-6 py-8 text-center font-mono text-xs uppercase tracking-[0.1em]" style={{ color: '#666' }}>
              {s}
            </div>
          ))}
        </div>
      </section>

      {/* ─── How It Works ─── */}
      <section className="border-b py-20" style={{ borderColor: '#222' }}>
        <div className="mx-auto max-w-5xl px-6">
          <h2 className="text-2xl font-bold tracking-[-0.02em] text-white">How it works</h2>
          <div className="mt-12 grid gap-4 md:grid-cols-3">
            {[
              { num: '01', title: 'Upload your file', desc: 'Drop any video or audio file. We store it and generate a magnet link.' },
              { num: '02', title: 'Paste one line of code', desc: '' },
              { num: '03', title: 'Viewers share bandwidth', desc: 'Every viewer becomes a peer. Your costs stay flat while your audience grows.' },
            ].map((item) => (
              <div key={item.num} className="border p-6" style={{ borderColor: '#222', backgroundColor: '#111' }}>
                <p className="mb-2 font-mono text-sm font-bold" style={{ color: 'var(--accent)' }}>{item.num}</p>
                <h3 className="mb-2 text-sm font-semibold text-white">{item.title}</h3>
                {item.num === '02' ? (
                  <div className="mt-2 border p-3" style={{ backgroundColor: '#0c0c0c', borderColor: '#222' }}>
                    <div className="mb-2 flex items-center gap-1 text-[10px] uppercase tracking-widest" style={{ color: '#666', fontFamily: 'monospace' }}>
                      <div className="terminal-dots"><span /><span /><span /></div>
                    </div>
                    <code className="text-xs" style={{ color: 'var(--success)', fontFamily: 'monospace' }}>
                      &lt;div data-torrent-cdn=&ldquo;your-id&rdquo;&gt;&lt;/div&gt;
                    </code>
                  </div>
                ) : (
                  <p className="text-sm leading-relaxed" style={{ color: '#999' }}>{item.desc}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ─── Cost Comparison ─── */}
      <section className="border-b py-20" style={{ borderColor: '#222' }}>
        <div className="mx-auto max-w-4xl px-6">
          <h2 className="text-2xl font-bold tracking-[-0.02em] text-white">Pay less as you grow. Not more.</h2>
          <div className="mt-10">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b" style={{ borderColor: '#222' }}>
                  <th className="py-3 pr-4 text-left font-medium text-white">Viewers</th>
                  <th className="py-3 pr-4 text-left font-medium text-white">AWS CloudFront</th>
                  <th className="py-3 text-left font-medium text-white">TorrentCDN</th>
                </tr>
              </thead>
              <tbody>
                {metrics.map((row) => (
                  <tr key={row.viewers} className="border-b" style={{ borderColor: '#222' }}>
                    <td className="py-3 pr-4 text-white">{row.viewers}</td>
                    <td className="py-3 pr-4" style={{ color: '#666' }}>{row.cloudfront}</td>
                    <td className="py-3 font-medium" style={{ color: 'var(--accent)' }}>{row.torrentcdn}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* ─── Pricing ─── */}
      <section className="border-b py-20" style={{ borderColor: '#222' }}>
        <div className="mx-auto max-w-5xl px-6">
          <h2 className="text-2xl font-bold tracking-[-0.02em] text-white">Simple pricing</h2>
          <p className="mt-2 text-sm" style={{ color: '#999' }}>Start free. Upgrade when you need more.</p>
          <div className="mt-10">
            <PricingTable />
          </div>
        </div>
      </section>

      {/* ─── Footer ─── */}
      <footer className="py-8">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6">
          <p className="text-sm" style={{ color: '#444' }}>TorrentCDN &copy; 2026</p>
          <div className="flex items-center gap-6">
            <a href="https://github.com" target="_blank" rel="noopener noreferrer" className="text-sm transition-colors hover:text-white" style={{ color: '#444' }}>GitHub</a>
            <span className="text-sm" style={{ color: '#444' }}>Docs</span>
          </div>
        </div>
      </footer>
    </div>
  )
}
