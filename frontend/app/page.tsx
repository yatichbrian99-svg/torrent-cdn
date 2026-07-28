'use client';

import { useState, useEffect } from 'react';
import {
  ArrowRight,
  Upload,
  Code,
  Share2,
  TrendingDown,
  Zap,
  Shield,
  Github,
  ChevronRight,
} from 'lucide-react';
import PricingTable from '@/components/PricingTable';

function AnimatedPeerCounter() {
  const [count, setCount] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setCount((prev) => {
        const increment = Math.floor(Math.random() * 3) + 1;
        return prev + increment;
      });
    }, 2000);
    return () => clearInterval(interval);
  }, []);

  return (
    <span className="inline-flex items-center gap-1.5 rounded border border-green-500/20 bg-green-500/5 px-3 py-1 text-xs text-green-500">
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-500/40" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-green-500" />
      </span>
      {count.toLocaleString()} live peers
    </span>
  );
}

function CostTable() {
  const rows = [
    { viewers: '1,000', traditional: '~$50', torrentCdn: '~$2' },
    { viewers: '10,000', traditional: '~$500', torrentCdn: '~$8' },
    { viewers: '100,000', traditional: '~$5,000', torrentCdn: '~$25' },
    { viewers: '1,000,000', traditional: '~$50,000', torrentCdn: '~$100' },
  ];

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-[#1f1f1f]">
            <th className="pb-3 pr-4 text-[11px] font-medium uppercase tracking-wider text-gray-500">
              Monthly Viewers
            </th>
            <th className="pb-3 pr-4 text-[11px] font-medium uppercase tracking-wider text-gray-500">
              Traditional CDN
            </th>
            <th className="pb-3 text-[11px] font-medium uppercase tracking-wider text-accent-500">
              TorrentCDN
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={row.viewers}
              className="border-b border-[#1f1f1f] last:border-0"
            >
              <td className="py-3 pr-4 text-white">{row.viewers}</td>
              <td className="py-3 pr-4 text-red-500/70">{row.traditional}</td>
              <td className="py-3 text-green-500">
                {row.torrentCdn}
                {i < rows.length - 1 && (
                  <span className="ml-2 text-[10px] text-green-500/60">
                    Save{' '}
                    {['96%', '98.4%', '99.5%', '99.8%'][i]}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function LandingPage() {
  return (
    <div>
      {/* Hero */}
      <section className="relative overflow-hidden border-b border-[#1f1f1f]">
        <div className="absolute inset-0 bg-gradient-to-b from-accent-500/5 via-transparent to-transparent" />
        <div className="relative mx-auto max-w-5xl px-4 pb-20 pt-20 text-center sm:px-6 lg:px-8 lg:pb-28 lg:pt-28">
          <div className="mb-6 flex items-center justify-center gap-3">
            <AnimatedPeerCounter />
          </div>
          <h1 className="mx-auto max-w-3xl text-3xl font-bold tracking-tight text-white sm:text-4xl lg:text-5xl">
            Deliver Video{' '}
            <span className="gradient-text">Cheaper</span> As You{' '}
            <span className="gradient-text">Scale</span>
          </h1>
          <p className="mx-auto mt-4 max-w-2xl text-sm text-gray-500 sm:text-base">
            P2P CDN powered by WebTorrent. The more viewers you have, the less
            you pay. Your audience shares bandwidth automatically.
          </p>
          <div className="mt-8 flex items-center justify-center gap-3">
            <a
              href="/dashboard"
              className="inline-flex items-center gap-2 border border-accent-500 bg-accent-500 px-5 py-2.5 text-sm font-medium text-black transition-all hover:bg-accent-400"
            >
              Get Started Free
              <ArrowRight size={15} />
            </a>
            <a
              href="/embed/demo"
              className="inline-flex items-center gap-2 border border-[#1f1f1f] px-5 py-2.5 text-sm font-medium text-gray-300 transition-colors hover:border-gray-700 hover:text-white"
            >
              View Demo
              <ChevronRight size={15} />
            </a>
          </div>
        </div>
      </section>

      {/* How It Works */}
      <section className="border-b border-[#1f1f1f] py-20">
        <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
          <h2 className="mb-12 text-center text-lg font-semibold text-white">
            How It Works
          </h2>
          <div className="grid gap-6 md:grid-cols-3">
            {[
              {
                icon: Upload,
                title: 'Upload Your File',
                desc: 'Upload your video or audio file. We store it securely and generate a magnet link.',
              },
              {
                icon: Code,
                title: 'Get Embed Code',
                desc: 'Copy a simple script tag. Paste it on your website like any other embed.',
              },
              {
                icon: Share2,
                title: 'Viewers Share Bandwidth',
                desc: 'As more people watch, they automatically seed to others. Your bandwidth costs drop.',
              },
            ].map((step, i) => (
              <div key={step.title} className="border border-[#1f1f1f] bg-[#111] p-6">
                <div className="mb-3 flex h-10 w-10 items-center justify-center border border-accent-500/30 bg-accent-500/5">
                  <step.icon size={18} className="text-accent-500" />
                </div>
                <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-gray-600">
                  Step {i + 1}
                </div>
                <h3 className="mb-1.5 text-sm font-semibold text-white">
                  {step.title}
                </h3>
                <p className="text-xs leading-relaxed text-gray-500">{step.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Cost Comparison */}
      <section className="border-b border-[#1f1f1f] py-20">
        <div className="mx-auto max-w-3xl px-4 sm:px-6 lg:px-8">
          <h2 className="mb-2 text-center text-lg font-semibold text-white">
            Compare Costs
          </h2>
          <p className="mb-10 text-center text-xs text-gray-500">
            See how much you save with P2P delivery
          </p>
          <CostTable />
          <div className="mt-6 flex items-center justify-center gap-2 text-xs text-gray-600">
            <TrendingDown size={14} className="text-green-500" />
            Average savings of 98% compared to traditional CDNs
          </div>
        </div>
      </section>

      {/* Pricing */}
      <section className="border-b border-[#1f1f1f] py-20">
        <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
          <h2 className="mb-2 text-center text-lg font-semibold text-white">
            Simple Pricing
          </h2>
          <p className="mb-10 text-center text-xs text-gray-500">
            Start free. Upgrade as you grow.
          </p>
          <PricingTable />
        </div>
      </section>

      {/* Features */}
      <section className="border-b border-[#1f1f1f] py-20">
        <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
          <h2 className="mb-12 text-center text-lg font-semibold text-white">
            Why TorrentCDN?
          </h2>
          <div className="grid gap-4 md:grid-cols-3">
            {[
              {
                icon: Zap,
                title: 'Blazing Fast',
                desc: 'P2P delivery means faster load times as more viewers join. No more buffering.',
              },
              {
                icon: Shield,
                title: 'Secure by Design',
                desc: 'All content is served over HTTPS. Your files are encrypted in transit and at rest.',
              },
              {
                icon: TrendingDown,
                title: 'Drops to Zero',
                desc: 'With enough peers, your origin bandwidth can drop to near zero. Viral traffic is free.',
              },
            ].map((feature) => (
              <div
                key={feature.title}
                className="border border-[#1f1f1f] bg-[#111] p-6"
              >
                <feature.icon size={20} className="mb-3 text-accent-500" />
                <h3 className="mb-1 text-sm font-semibold text-white">
                  {feature.title}
                </h3>
                <p className="text-xs leading-relaxed text-gray-500">
                  {feature.desc}
                </p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="py-8">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 sm:px-6 lg:px-8">
          <p className="text-xs text-gray-600">
            Built with WebTorrent &middot; Open source
          </p>
          <a
            href="https://github.com"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 text-xs text-gray-600 transition-colors hover:text-white"
          >
            <Github size={14} />
            GitHub
          </a>
        </div>
      </footer>
    </div>
  );
}
