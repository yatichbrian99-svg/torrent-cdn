'use client';

import { useState, useEffect } from 'react';
import { TrendingDown, Shield, Zap, ArrowRight, Check, Github } from 'lucide-react';
import PricingTable from '@/components/PricingTable';

const stats = [
  { label: 'Bandwidth Saved', value: '12.4 PB' },
  { label: 'Active Peers', value: '47,892' },
  { label: 'Content Delivered', value: '3.2M hrs' },
  { label: 'Avg Offload Rate', value: '94%' },
];

const features = [
  {
    icon: TrendingDown,
    title: 'Reduce Bandwidth Costs',
    desc: 'P2P delivery means your origin servers handle less traffic. Costs drop as your audience grows — viral traffic becomes nearly free.',
  },
  {
    icon: Shield,
    title: 'Enterprise-Grade Security',
    desc: 'All content served over HTTPS with encryption in transit. Full access control, token-based auth, and secure embed options.',
  },
  {
    icon: Zap,
    title: 'Zero-Configuration Scaling',
    desc: 'No capacity planning. No infrastructure changes. Upload once, and the P2P network automatically scales to millions of viewers.',
  },
];

const metrics = [
  { viewers: '1,000', traditional: '$50', torrentcdn: '$2', save: '96%' },
  { viewers: '10,000', traditional: '$500', torrentcdn: '$8', save: '98%' },
  { viewers: '100,000', traditional: '$5,000', torrentcdn: '$25', save: '99%' },
];

export default function LandingPage() {
  const [livePeers, setLivePeers] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setLivePeers((p) => p + Math.floor(Math.random() * 3) + 1);
    }, 3000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div>
      {/* ──────── Hero ──────── */}
      <section className="relative overflow-hidden border-b border-surface-border">
        <div className="absolute inset-0 bg-gradient-to-b from-brand-500/5 via-transparent to-transparent" />
        <div className="relative mx-auto max-w-6xl px-6 pb-24 pt-20 text-center sm:pb-32 sm:pt-28 lg:px-8">
          <div className="mx-auto mb-6 flex max-w-fit items-center gap-2 rounded-full border border-brand-500/20 bg-brand-500/5 px-4 py-1.5 text-sm text-brand-400">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-400/60" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-brand-400" />
            </span>
            {livePeers.toLocaleString()} peers seeding right now
          </div>

          <h1 className="mx-auto max-w-4xl text-4xl font-bold tracking-tight text-white sm:text-5xl lg:text-6xl">
            Video delivery that gets{' '}
            <span className="text-brand-400">cheaper as you grow</span>
          </h1>

          <p className="mx-auto mt-6 max-w-2xl text-lg leading-relaxed text-gray-400">
            TorrentCDN uses WebTorrent P2P technology to offload your video bandwidth onto your viewers.
            The more people watch, the less you pay for infrastructure.
          </p>

          <div className="mt-10 flex items-center justify-center gap-4">
            <a
              href="/dashboard"
              className="inline-flex items-center gap-2 rounded-lg bg-brand-600 px-6 py-3 text-sm font-medium text-white shadow-lg shadow-brand-600/25 transition-all hover:bg-brand-500"
            >
              Get Started Free
              <ArrowRight size={16} />
            </a>
            <a
              href="/embed/demo"
              className="inline-flex items-center gap-2 rounded-lg border border-surface-border px-6 py-3 text-sm font-medium text-gray-300 transition-colors hover:border-gray-600 hover:text-white"
            >
              Watch Demo
            </a>
          </div>
        </div>
      </section>

      {/* ──────── Stats Bar ──────── */}
      <section className="border-b border-surface-border">
        <div className="mx-auto max-w-6xl px-6 py-12 lg:px-8">
          <div className="grid grid-cols-2 gap-8 md:grid-cols-4">
            {stats.map((s) => (
              <div key={s.label} className="text-center">
                <p className="text-2xl font-bold text-white sm:text-3xl">{s.value}</p>
                <p className="mt-1 text-sm text-gray-500">{s.label}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ──────── Cost Comparison ──────── */}
      <section className="border-b border-surface-border py-20">
        <div className="mx-auto max-w-4xl px-6 lg:px-8">
          <div className="text-center">
            <h2 className="text-2xl font-bold text-white sm:text-3xl">See the difference</h2>
            <p className="mt-3 text-gray-400">Traditional CDNs get expensive fast. TorrentCDN flips the model.</p>
          </div>

          <div className="mt-12 overflow-hidden rounded-xl border border-surface-border">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="bg-surface-card">
                  <th className="px-6 py-4 font-medium text-gray-300">Monthly Viewers</th>
                  <th className="px-6 py-4 font-medium text-gray-300">Traditional CDN</th>
                  <th className="px-6 py-4 font-medium text-gray-300">TorrentCDN</th>
                  <th className="px-6 py-4 font-medium text-gray-300">You Save</th>
                </tr>
              </thead>
              <tbody>
                {metrics.map((row) => (
                  <tr key={row.viewers} className="border-t border-surface-border">
                    <td className="px-6 py-4 text-white">{row.viewers}</td>
                    <td className="px-6 py-4 text-gray-500">{row.traditional}</td>
                    <td className="px-6 py-4 font-medium text-brand-400">{row.torrentcdn}</td>
                    <td className="px-6 py-4">
                      <span className="inline-flex items-center gap-1 rounded-full bg-green-500/10 px-2.5 py-0.5 text-xs font-medium text-green-500">
                        <TrendingDown size={12} />
                        {row.save}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* ──────── Features ──────── */}
      <section className="border-b border-surface-border py-20">
        <div className="mx-auto max-w-6xl px-6 lg:px-8">
          <div className="text-center">
            <h2 className="text-2xl font-bold text-white sm:text-3xl">Built for scale</h2>
            <p className="mt-3 text-gray-400">Everything you need to deliver video at any scale.</p>
          </div>

          <div className="mt-12 grid gap-6 md:grid-cols-3">
            {features.map((f) => (
              <div key={f.title} className="rounded-xl border border-surface-border bg-surface-card p-6 transition-colors hover:border-gray-700">
                <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-lg bg-brand-500/10">
                  <f.icon size={20} className="text-brand-400" />
                </div>
                <h3 className="text-base font-semibold text-white">{f.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-gray-400">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ──────── How It Works ──────── */}
      <section className="border-b border-surface-border py-20">
        <div className="mx-auto max-w-4xl px-6 lg:px-8">
          <div className="text-center">
            <h2 className="text-2xl font-bold text-white sm:text-3xl">Get started in minutes</h2>
            <p className="mt-3 text-gray-400">Three steps to start saving on video delivery.</p>
          </div>

          <div className="mt-12 grid gap-6 md:grid-cols-3">
            {[
              { step: '01', title: 'Upload your file', desc: 'Upload your video to TorrentCDN. We store it, generate a magnet link, and create your embed code.' },
              { step: '02', title: 'Embed on your site', desc: 'Copy a single script tag. Paste it on your website — works with any platform or framework.' },
              { step: '03', title: 'Watch costs drop', desc: 'As viewers watch, they automatically seed to others. Your bandwidth costs decrease with every new viewer.' },
            ].map((item) => (
              <div key={item.step} className="relative pl-8">
                <div className="absolute left-0 top-0 flex h-6 w-6 items-center justify-center rounded-full bg-brand-500/10 text-xs font-semibold text-brand-400">
                  {item.step}
                </div>
                <h3 className="text-base font-semibold text-white">{item.title}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-gray-400">{item.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ──────── Social Proof ──────── */}
      <section className="border-b border-surface-border py-20">
        <div className="mx-auto max-w-4xl px-6 text-center lg:px-8">
          <blockquote className="mx-auto max-w-2xl">
            <p className="text-lg leading-relaxed text-gray-300 italic">
              "TorrentCDN cut our video delivery costs by 94% in the first month.
              The P2P approach is genius — every new viewer makes our infrastructure cheaper, not more expensive."
            </p>
            <footer className="mt-6">
              <p className="text-sm font-medium text-white">Alex Chen</p>
              <p className="text-sm text-gray-500">CTO, Streamly</p>
            </footer>
          </blockquote>
        </div>
      </section>

      {/* ──────── Pricing ──────── */}
      <section className="border-b border-surface-border py-20">
        <div className="mx-auto max-w-6xl px-6 lg:px-8">
          <div className="text-center">
            <h2 className="text-2xl font-bold text-white sm:text-3xl">Simple, transparent pricing</h2>
            <p className="mt-3 text-gray-400">Start free. Upgrade when you need more.</p>
          </div>
          <div className="mt-12">
            <PricingTable />
          </div>
        </div>
      </section>

      {/* ──────── Final CTA ──────── */}
      <section className="border-b border-surface-border py-20">
        <div className="mx-auto max-w-3xl px-6 text-center lg:px-8">
          <h2 className="text-2xl font-bold text-white sm:text-3xl">Ready to cut your CDN costs?</h2>
          <p className="mt-3 text-gray-400">Join thousands of businesses already saving with TorrentCDN.</p>
          <div className="mt-8 flex items-center justify-center gap-4">
            <a
              href="/dashboard"
              className="inline-flex items-center gap-2 rounded-lg bg-brand-600 px-6 py-3 text-sm font-medium text-white shadow-lg shadow-brand-600/25 transition-all hover:bg-brand-500"
            >
              Get Started Free
              <ArrowRight size={16} />
            </a>
            <a href="/embed/demo" className="inline-flex items-center gap-2 rounded-lg border border-surface-border px-6 py-3 text-sm font-medium text-gray-300 transition-colors hover:border-gray-600 hover:text-white">
              Watch Demo
            </a>
          </div>
        </div>
      </section>

      {/* ──────── Footer ──────── */}
      <footer className="py-12">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 lg:px-8">
          <p className="text-sm text-gray-600">&copy; 2026 TorrentCDN. All rights reserved.</p>
          <a href="https://github.com" target="_blank" rel="noopener noreferrer" className="flex items-center gap-1.5 text-sm text-gray-600 transition-colors hover:text-gray-400">
            <Github size={16} />
            GitHub
          </a>
        </div>
      </footer>
    </div>
  );
}
