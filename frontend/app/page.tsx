'use client'

import { useEffect } from 'react'
import PricingTable from '@/components/PricingTable'
import { sanitizeRefCode } from '@/lib/security'
import SiteFooter from '@/components/SiteFooter'
import CodeSnippet from '@/components/CodeSnippet'
import CompareTable from '@/components/CompareTable'
import FaqAccordion from '@/components/FaqAccordion'
import Reveal from '@/components/Reveal'
import LightHeroArt from '@/components/hero/LightHeroArt'
import DarkHeroArt from '@/components/hero/DarkHeroArt'

// Copy is unchanged from the pre-redesign page. The only edits are markup
// (two words of the headline get their own span so each theme can accent a
// different one), the FAQ becoming an accordion, and the footer moving into
// SiteFooter. Every figure below also appears in docs/frontend/COPY_CLAIMS.md.
const STEPS = [
  { num: '01', title: 'Upload your file', desc: 'Drop any video or audio file. We store it and generate a magnet link.' },
  { num: '02', title: 'Paste one line of code', desc: '' },
  { num: '03', title: 'Viewers share bandwidth', desc: 'Every viewer becomes a peer. Your costs stay flat while your audience grows.' },
]

const STATS = [
  { big: '90%', label: 'bandwidth reduction', raw: '90% bandwidth reduction' },
  { big: '< 5ms', label: 'peer discovery', raw: '< 5ms peer discovery' },
  { big: 'Works in any browser', label: '', raw: 'Works in any browser' },
]

const FAQ = [
  {
    q: 'What browsers are supported?',
    a: 'TorrentCDN works in all modern browsers that support WebRTC — Chrome, Firefox, Edge, Safari 15+, and all mobile browsers.',
  },
  {
    q: 'What happens if there are no peers?',
    a: 'If no peers are available, the player automatically falls back to direct delivery from our origin server within 5 seconds. Your viewers never see a blank screen.',
  },
  {
    q: 'What file types are supported?',
    a: 'We support MP4, WebM, MKV, MOV, AVI for video and MP3, WAV, OGG, M4A for audio.',
  },
  {
    q: 'What is the maximum file size?',
    a: 'The maximum file size is 2GB per file. Larger files are available on the Business plan.',
  },
  {
    q: 'Is my content private?',
    a: "Your files are stored in object storage and served from unlisted public URLs — there's no public index or search, and only people with your embed link or magnet can access them. But links and magnets behave like keys: anyone who has one can view and re-share your content, and P2P delivery means viewers pass chunks directly between peers. Treat your links like a shared secret — TorrentCDN is for content you're comfortable sharing, not for confidential or DRM-protected material.",
  },
  {
    q: 'How does billing work?',
    a: 'We measure bandwidth by the amount delivered from our origin servers. P2P bandwidth between peers is free and does not count toward your limit.',
  },
  {
    q: 'Can I use this on my own domain?',
    a: 'Yes — Business plan includes custom domain support. You can serve the embed player and SDK from your own domain.',
  },
  {
    q: 'Do you have an API?',
    a: 'Yes. Create an API key (format: tcdn_…) in your dashboard, or get a Bearer token via POST /auth/login (see the API docs). You can upload files, list files, and generate embed codes programmatically via our REST API.',
  },
]

export default function LandingPage() {
  // Capture ?ref= referral code and persist it for the signup flow. Sanitize
  // at the boundary: only alphanumerics survive (matches the backend's 20-char
  // UPPER(referral_code) lookup), so junk can't ride along into storage.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const ref = params.get('ref')
    const clean = sanitizeRefCode(ref || '')
    if (clean) {
      try {
        sessionStorage.setItem('torrent_cdn_ref', clean)
      } catch {
        // Storage blocked — the referral simply does not travel with signup.
      }
    }
  }, [])

  return (
    <div className="landing">
      {/* ---------------------------------------------------------------- Hero */}
      <section className="hero">
        <DarkHeroArt />
        <span className="hero-wordmark" aria-hidden="true">TorrentCDN</span>

        <div className="wrap hero-inner">
          {/* Not wrapped in the fade-up: this line is the mobile LCP element,
              and an element that starts at opacity 0 does not count as painted,
              so animating it pushes LCP out by the animation delay. */}
          <p className="label hero-eyebrow">
            Built for high concurrency video delivery
          </p>

          <h1 className="hero-title h-display hero-rise" style={{ '--rise-delay': '120ms' } as React.CSSProperties}>
            Video delivery that gets{' '}
            <span className="hero-word-accent">cheaper</span> as you{' '}
            <span className="hero-word-serif">scale</span>.
          </h1>

          <p className="hero-sub hero-rise" style={{ '--rise-delay': '200ms' } as React.CSSProperties}>
            TorrentCDN uses WebTorrent to turn your viewers into a P2P network. The more people watch, the less you pay.
          </p>

          <div className="hero-ctas hero-rise" style={{ '--rise-delay': '280ms' } as React.CSSProperties}>
            <a href="/dashboard" className="btn btn-primary btn-hero">
              <span>Start for free</span>
              <span className="btn-disc" aria-hidden="true">→</span>
            </a>
            <a href="/embed/demo" className="btn btn-secondary btn-hero">
              Watch demo
            </a>
          </div>

          <p className="hero-micro hero-rise" style={{ '--rise-delay': '360ms' } as React.CSSProperties}>
            No credit card required &middot; 100GB free forever
          </p>
        </div>

        <LightHeroArt />

        <div className="wrap hero-stats-wrap">
          <ul className="hero-stats">
            {STATS.map((stat, i) => (
              <li key={stat.raw} className="hero-stat" style={{ '--reveal-delay': `${120 + i * 80}ms` } as React.CSSProperties}>
                <span className="hero-stat-big num">{stat.big}</span>
                {stat.label ? <span className="hero-stat-label label">{stat.label}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      </section>

      {/* --------------------------------------------------------- How it works */}
      <section className="section" id="how-it-works">
        <div className="wrap">
          <Reveal>
            <h2 className="h-section">How it works</h2>
          </Reveal>

          <ol className="steps">
            <span className="steps-connector" aria-hidden="true" />
            {STEPS.map((step, index) => (
              <Reveal as="li" key={step.num} delay={index * 90} className="step">
                <span className="step-num num" aria-hidden="true">{step.num}</span>
                <h3 className="step-title">{step.title}</h3>
                {step.num === '02' ? (
                  <div className="step-code">
                    <CodeSnippet compact />
                  </div>
                ) : (
                  <p className="step-desc">{step.desc}</p>
                )}
              </Reveal>
            ))}
          </ol>
        </div>
      </section>

      {/* ---------------------------------------------------------- Comparison */}
      <section className="section section-tint" id="comparison">
        <div className="wrap">
          <Reveal>
            <h2 className="h-section">Pay less as you grow. Not more.</h2>
          </Reveal>
          <Reveal delay={80}>
            <CompareTable />
          </Reveal>
        </div>
      </section>

      {/* ------------------------------------------------------------- Pricing */}
      <section className="section" id="pricing">
        <div className="wrap">
          <Reveal>
            <h2 className="h-section">Simple pricing</h2>
            <p className="section-sub">Start free. Upgrade when you need more.</p>
          </Reveal>
          <Reveal delay={80}>
            <PricingTable />
          </Reveal>
        </div>
      </section>

      {/* ----------------------------------------------------------------- FAQ */}
      <section className="section section-tint" id="faq">
        <div className="wrap wrap-narrow">
          <Reveal>
            <h2 className="h-section">Frequently asked questions</h2>
          </Reveal>
          <Reveal delay={80}>
            <FaqAccordion items={FAQ} />
          </Reveal>
        </div>
      </section>

      <SiteFooter />
    </div>
  )
}
