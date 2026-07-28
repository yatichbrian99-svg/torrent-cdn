'use client';

import { useState, useEffect } from 'react';
import { Github } from 'lucide-react';
import PricingTable from '@/components/PricingTable';

function CostTable() {
  const rows = [
    { viewers: '1,000', traditional: '~$50', torrentCdn: '~$2' },
    { viewers: '10,000', traditional: '~$500', torrentCdn: '~$8' },
    { viewers: '100,000', traditional: '~$5,000', torrentCdn: '~$25' },
  ];

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-[#1f1f1f]">
          <th className="pb-2 pr-4 text-left text-xs font-medium text-gray-500">Viewers</th>
          <th className="pb-2 pr-4 text-left text-xs font-medium text-gray-500">Traditional</th>
          <th className="pb-2 text-left text-xs font-medium text-gray-500">TorrentCDN</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.viewers} className="border-b border-[#1f1f1f] last:border-0">
            <td className="py-2 pr-4 text-white">{row.viewers}</td>
            <td className="py-2 pr-4 text-gray-500">{row.traditional}</td>
            <td className="py-2 text-white">{row.torrentCdn}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function LandingPage() {
  const [peers, setPeers] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setPeers((p) => p + Math.floor(Math.random() * 3) + 1);
    }, 2000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div>
      {/* Hero */}
      <section className="border-b border-[#1f1f1f] py-20">
        <div className="mx-auto max-w-3xl px-4 text-center sm:px-6">
          <p className="mb-4 text-xs text-gray-500">{peers.toLocaleString()} live peers</p>
          <h1 className="text-2xl font-bold text-white sm:text-3xl">
            Deliver Video Cheaper As You Scale
          </h1>
          <p className="mx-auto mt-3 max-w-xl text-sm text-gray-500">
            P2P CDN powered by WebTorrent. The more viewers you have, the less you pay.
          </p>
          <div className="mt-6 flex items-center justify-center gap-3">
            <a
              href="/dashboard"
              className="rounded border border-gray-700 bg-gray-800 px-4 py-2 text-sm text-white hover:bg-gray-700"
            >
              Get Started Free
            </a>
            <a
              href="/embed/demo"
              className="rounded border border-[#1f1f1f] px-4 py-2 text-sm text-gray-400 hover:text-white"
            >
              View Demo
            </a>
          </div>
        </div>
      </section>

      {/* How It Works */}
      <section className="border-b border-[#1f1f1f] py-16">
        <div className="mx-auto max-w-4xl px-4 sm:px-6">
          <h2 className="mb-8 text-center text-base font-semibold text-white">How It Works</h2>
          <div className="grid gap-6 md:grid-cols-3">
            {[
              { step: '01', title: 'Upload', desc: 'Upload your video or audio file. We store it and generate a magnet link.' },
              { step: '02', title: 'Embed', desc: 'Copy a script tag and paste it on your website like any other embed.' },
              { step: '03', title: 'Share', desc: 'Viewers automatically seed to others. Your bandwidth costs drop as you grow.' },
            ].map((item) => (
              <div key={item.step} className="border border-[#1f1f1f] bg-[#111] p-5">
                <p className="mb-1 text-xs text-gray-600">{item.step}</p>
                <h3 className="mb-1 text-sm font-medium text-white">{item.title}</h3>
                <p className="text-xs text-gray-500">{item.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Cost Comparison */}
      <section className="border-b border-[#1f1f1f] py-16">
        <div className="mx-auto max-w-lg px-4 sm:px-6">
          <h2 className="mb-6 text-center text-base font-semibold text-white">Cost Comparison</h2>
          <CostTable />
        </div>
      </section>

      {/* Pricing */}
      <section className="border-b border-[#1f1f1f] py-16">
        <div className="mx-auto max-w-4xl px-4 sm:px-6">
          <h2 className="mb-6 text-center text-base font-semibold text-white">Pricing</h2>
          <PricingTable />
        </div>
      </section>

      {/* Footer */}
      <footer className="py-8">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 sm:px-6">
          <p className="text-xs text-gray-600">Built with WebTorrent</p>
          <a
            href="https://github.com"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 text-xs text-gray-600 hover:text-white"
          >
            <Github size={14} />
            GitHub
          </a>
        </div>
      </footer>
    </div>
  );
}
