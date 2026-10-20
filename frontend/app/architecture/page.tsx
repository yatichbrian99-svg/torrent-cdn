import type { Metadata } from 'next'
import type { ReactNode } from 'react'

export const metadata: Metadata = {
  title: 'Architecture',
  description:
    'How TorrentCDN delivers content: object-storage origin, WebTorrent/WebRTC peer-assisted exchange between viewers, and automatic direct-origin fallback — complementing, not replacing, conventional CDN infrastructure.',
  alternates: { canonical: 'https://www.torrentcdn.store/architecture' },
}

const ORIGIN = 'var(--info)'
const PEER = 'var(--ok)'
const FALLBACK = 'var(--warn)'

function Section({ n, title, children }: { n: string; title: string; children: ReactNode }) {
  return (
    <section className="mt-12">
      <h2 className="mb-4 text-lg font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">
        <span className="mr-2 font-mono text-xs" style={{ color: 'var(--text-faint)' }}>{n}</span>
        {title}
      </h2>
      <div className="space-y-4">{children}</div>
    </section>
  )
}

function Card({ children }: { children: ReactNode }) {
  return (
    <div className="border p-5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
      {children}
    </div>
  )
}

function Label({ children, tone }: { children: ReactNode; tone?: string }) {
  return (
    <p
      className="font-mono text-[10px] font-bold uppercase tracking-[0.2em]"
      style={{ color: tone || 'var(--text-muted)' }}
    >
      {children}
    </p>
  )
}

/** One box in a delivery-path diagram. */
function Node({
  title,
  sub,
  tone,
}: {
  title: string
  sub?: string
  tone?: string
}) {
  return (
    <div
      className="w-full border px-4 py-3 text-center"
      style={{ borderColor: 'var(--border-strong)', backgroundColor: 'var(--bg-secondary)' }}
    >
      <p className="text-sm font-semibold" style={{ color: tone || 'var(--text-primary)' }}>
        {title}
      </p>
      {sub && (
        <p className="mt-1 text-[11px] leading-snug" style={{ color: 'var(--text-muted)' }}>
          {sub}
        </p>
      )}
    </div>
  )
}

function Down({ label }: { label?: string }) {
  return (
    <div className="flex flex-col items-center py-1" aria-hidden="true">
      <span className="text-base leading-none" style={{ color: 'var(--text-dim)' }}>↓</span>
      {label && (
        <span
          className="mt-0.5 font-mono text-[9px] uppercase tracking-[0.15em]"
          style={{ color: 'var(--text-faint)' }}
        >
          {label}
        </span>
      )}
    </div>
  )
}

function Legend() {
  const items = [
    { c: PEER, label: 'Peer-assisted (WebRTC)' },
    { c: ORIGIN, label: 'Origin object storage' },
    { c: FALLBACK, label: 'Fallback / edge path' },
  ]
  return (
    <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2">
      {items.map((i) => (
        <span key={i.label} className="flex items-center gap-2">
          <span className="h-2 w-2" style={{ backgroundColor: i.c }} />
          <span className="font-mono text-[10px] uppercase tracking-[0.12em]" style={{ color: 'var(--text-muted)' }}>
            {i.label}
          </span>
        </span>
      ))}
    </div>
  )
}

export default function ArchitecturePage() {
  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <p className="font-mono text-[10px] font-bold uppercase tracking-[0.2em]" style={{ color: 'var(--text-faint)' }}>
        TorrentCDN / Architecture
      </p>
      <h1 className="mt-2 text-xl font-bold tracking-[-0.02em] text-[color:var(--text-primary)]">
        How TorrentCDN works
      </h1>
      <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
        TorrentCDN is a <span className="font-bold text-[color:var(--text-primary)]">peer-assisted delivery layer</span>{' '}
        for video and other static media. It is not a conventional PoP-based CDN: it runs alongside origin and edge
        infrastructure, adding a second delivery path in which viewers exchange content directly with each other.
      </p>

      {/* ── 1 ─────────────────────────────────────────────────────────────── */}
      <Section n="1" title="What TorrentCDN is">
        <Card>
          <p className="text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            Every delivery starts from an origin. A publisher uploads content to object storage; TorrentCDN builds torrent
            metadata for it and hands the viewer&rsquo;s browser a magnet link. From there the player tries to fetch pieces
            from other viewers watching the same content (WebTorrent over WebRTC), and keeps the origin available as the
            authoritative fallback.
          </p>
          <p className="mt-3 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            The consequence is a mixed path: a conventional origin (or CDN edge) remains responsible for first playback,
            for content nobody else is watching, and for every viewer that cannot participate in a swarm. Peers carry
            whatever share of the transfer they can.
          </p>
        </Card>
        <div className="grid gap-4 sm:grid-cols-2">
          <Card>
            <Label tone={PEER}>What it is</Label>
            <ul className="mt-2 space-y-2 text-sm" style={{ color: 'var(--text-muted)' }}>
              <li>· A peer-assisted delivery layer on top of ordinary object storage</li>
              <li>· WebTorrent + WebRTC chunk exchange between viewers of the same content</li>
              <li>· Origin-backed: direct delivery whenever peers cannot serve a request</li>
              <li>· Observable per file (views, peers, bytes served by peers vs origin)</li>
            </ul>
          </Card>
          <Card>
            <Label tone={FALLBACK}>What it is not</Label>
            <ul className="mt-2 space-y-2 text-sm" style={{ color: 'var(--text-muted)' }}>
              <li>· Not a replacement for a CDN or its points of presence</li>
              <li>· Not a guarantee that origin bandwidth drops to zero</li>
              <li>· Not a storage system or a file host of its own — content lives in object storage</li>
              <li>· Not a DRM or confidentiality layer: links and magnets behave like keys</li>
            </ul>
          </Card>
        </div>
      </Section>

      {/* ── 2 ─────────────────────────────────────────────────────────────── */}
      <Section n="2" title="Where the content lives (origin)">
        <Card>
          <p className="mb-4 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            Delivered files are stored in object storage and served from unlisted public object URLs. Production uses
            Supabase&rsquo;s S3-compatible storage through the standard S3 API, so the origin can be changed to another
            S3-compatible provider without changing the delivery model.
          </p>
          <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-[10rem_1fr]">
            {[
              ['Provider', 'Supabase (S3-compatible object storage)'],
              ['Bucket', <code key="b" className="font-mono text-[color:var(--text-primary)]">torrent-cdn-files</code>],
              ['S3 region', <code key="r" className="font-mono text-[color:var(--text-primary)]">us-east-1</code>],
              ['Role', 'Origin and fallback source for every file'],
              ['Not the origin', 'The application host — Render (Oregon, US West) runs the TorrentCDN backend/API, it does not store or stream the media bytes'],
            ].map(([k, v]) => (
              <div key={String(k)} className="contents">
                <dt className="font-mono text-[10px] uppercase tracking-[0.15em] sm:pt-0.5" style={{ color: 'var(--text-faint)' }}>
                  {k}
                </dt>
                <dd className="text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>{v}</dd>
              </div>
            ))}
          </dl>
        </Card>
        <p className="text-xs leading-relaxed" style={{ color: 'var(--text-faint)' }}>
          Access to the bucket is server-side only. Credentials, keys and internal endpoints are never shipped to the
          browser, and the player references content through short-lived metadata and the embed/magnet link.
        </p>
      </Section>

      {/* ── 3 ─────────────────────────────────────────────────────────────── */}
      <Section n="3" title="Content delivery flow">
        <Card>
          <div className="mx-auto max-w-md" role="img" aria-label="Delivery path: uploader, object storage, torrent metadata, viewer browser, WebTorrent and WebRTC peer exchange">
            <Node title="Uploader" sub="Publisher uploads a file" />
            <Down label="upload" />
            <Node title="Object storage / origin" sub="Supabase S3 · bucket torrent-cdn-files" tone={ORIGIN} />
            <Down label="torrent metadata" />
            <Node title="Magnet link" sub="Infohash + trackers, issued per file" />
            <Down label="embed / link" />
            <Node title="Viewer's browser" sub="WebTorrent joins the swarm" />
            <Down label="WebRTC" />
            {/* Three viewers in a row on tablet/desktop, stacked on phones: a
                fixed 5-column track cannot shrink below its min-content width
                at 320px, which pushed the last node off-screen. */}
            <div className="grid grid-cols-1 items-center gap-0.5 sm:grid-cols-[1fr_auto_1fr_auto_1fr] sm:gap-1">
              <Node title="Viewer A" tone={PEER} />
              <span className="rotate-90 text-center leading-none sm:rotate-0" aria-hidden="true" style={{ color: 'var(--text-dim)' }}>↔</span>
              <Node title="Viewer B" tone={PEER} />
              <span className="rotate-90 text-center leading-none sm:rotate-0" aria-hidden="true" style={{ color: 'var(--text-dim)' }}>↔</span>
              <Node title="Viewer C" tone={PEER} />
            </div>
            <p className="mt-2 text-center font-mono text-[9px] uppercase tracking-[0.15em]" style={{ color: 'var(--text-faint)' }}>
              peer-to-peer chunk exchange
            </p>
          </div>
        </Card>
        <ol className="space-y-2 text-sm" style={{ color: 'var(--text-muted)' }}>
          <li><span className="font-mono text-xs" style={{ color: 'var(--text-faint)' }}>01</span> The file is uploaded and written to object storage.</li>
          <li><span className="font-mono text-xs" style={{ color: 'var(--text-faint)' }}>02</span> Torrent metadata (infohash, piece layout, trackers) is generated for that exact file and returned as a magnet link.</li>
          <li><span className="font-mono text-xs" style={{ color: 'var(--text-faint)' }}>03</span> A viewer opens the embed/link; the browser player resolves the file and joins the torrent swarm.</li>
          <li><span className="font-mono text-xs" style={{ color: 'var(--text-faint)' }}>04</span> Pieces arrive over WebRTC from other viewers when available, and from the origin when they are not.</li>
          <li><span className="font-mono text-xs" style={{ color: 'var(--text-faint)' }}>05</span> Delivery is metered per event, so peer-served and origin-served bytes stay distinguishable in analytics.</li>
        </ol>
        <div className="border p-4" style={{ borderColor: 'var(--warn-chip-border)', backgroundColor: 'var(--warn-chip-bg)' }}>
          <Label tone={FALLBACK}>Fallback</Label>
          <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            If no suitable peers are reachable, the player switches to{' '}
            <span className="font-bold text-[color:var(--text-primary)]">direct origin delivery</span> — the same file, fetched
            over HTTPS from the Supabase object-storage origin. The connect window is{' '}
            <span className="font-bold text-[color:var(--text-primary)]">about 5 seconds</span> before the fallback fires; when
            a peer connection has already been established the window extends to 15 seconds so a healthy swarm is not
            abandoned early. Playback continues either way.
          </p>
        </div>
      </Section>

      {/* ── 4 ─────────────────────────────────────────────────────────────── */}
      <Section n="4" title="Browser requirements">
        <Card>
          <p className="mb-4 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            Nothing is installed by the viewer: delivery runs inside the page, from the embed script or the hosted player.
            Peer-assisted delivery relies on two browser capabilities.
          </p>
          <ul className="space-y-3 text-sm" style={{ color: 'var(--text-muted)' }}>
            <li>
              <span className="font-bold text-[color:var(--text-primary)]">WebRTC data channels</span> — required for peer
              connections. Where they are unavailable the player does not fail; it delivers directly from the origin.
            </li>
            <li>
              <span className="font-bold text-[color:var(--text-primary)]">MediaSource with a supported codec</span> — allows
              progressive playback while pieces arrive. For a codec the browser cannot play through MediaSource, the file is
              buffered in full first and then played, so start-up is slower.
            </li>
          </ul>
          <p className="mt-4 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            Current Chromium-based browsers, Firefox and recent Safari provide both. Our automated delivery tests run in
            Chromium; other engines are supported targets but are not covered by the same test matrix. In all cases the
            origin path keeps playback working — the difference is whether peers can share the load.
          </p>
        </Card>
      </Section>

      {/* ── 5 ─────────────────────────────────────────────────────────────── */}
      <Section n="5" title="Why peer-assisted delivery scales">
        <Card>
          <p className="text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            Traffic to a popular file is highly redundant: many viewers request the same bytes within the same minutes. In a
            purely origin-based model every one of those requests is served by the origin or an edge. In this model a viewer
            can serve neighbours instead — so as concurrency on a file grows, the potential supply of upstream capacity grows
            with it, and a larger share of the transfer can be satisfied between viewers.
          </p>
          <p className="mt-3 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            This is what makes cost per viewer fall rather than rise. It is equally important to state the limit: peers only
            carry what they already have or are fetching, on connections they can sustain. First viewers of a file, viewers
            behind restrictive networks, and any poorly-seeded moment are still served by origin or edge. Peer assistance
            reduces origin load; it does not remove it.
          </p>
        </Card>
        <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
          Measured offload on a live file is visible on the{' '}
          <a href="/demo" className="underline transition-colors hover:text-[color:var(--text-primary)]">/demo</a> page, which
          reports peer-served vs origin-served bytes for the same content.
        </p>
      </Section>

      {/* ── 6 ─────────────────────────────────────────────────────────────── */}
      <Section n="6" title="Today vs in development">
        <div className="grid gap-4 sm:grid-cols-2">
          <Card>
            <Label tone={PEER}>Available today</Label>
            <ul className="mt-2 space-y-2 text-sm" style={{ color: 'var(--text-muted)' }}>
              <li>· Pre-recorded video and audio delivery</li>
              <li>· WebTorrent swarms per file</li>
              <li>· WebRTC peer-assisted chunk exchange</li>
              <li>· Automatic direct-origin fallback</li>
              <li>· Per-file delivery analytics (peers, bytes, offload)</li>
            </ul>
          </Card>
          <Card>
            <Label tone={FALLBACK}>In development</Label>
            <ul className="mt-2 space-y-2 text-sm" style={{ color: 'var(--text-muted)' }}>
              <li>· HLS-based live streaming with the same origin-plus-peers model</li>
              <li>· Segment-level peer exchange for live edges</li>
            </ul>
            <p className="mt-3 text-xs leading-relaxed" style={{ color: 'var(--text-faint)' }}>
              Live streaming exists as an engineering MVP and is being hardened with design partners. It is not part of the
              generally available offering described on this page, and this page makes no availability commitment for it.
            </p>
          </Card>
        </div>
      </Section>

      {/* ── 7 ─────────────────────────────────────────────────────────────── */}
      <Section n="7" title="Where a conventional CDN fits">
        <Card>
          <p className="text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            TorrentCDN is not trying to replace traditional CDN infrastructure — the two solve different halves of the same
            problem. Peer assistance is strongest when many viewers watch the same content at the same time; it is weakest
            when a viewer is alone, early, or poorly connected. That is exactly where edge infrastructure is strongest.
          </p>
          <p className="mt-4 mb-2 text-sm font-bold text-[color:var(--text-primary)]">A conventional CDN / edge provider can supply:</p>
          <ul className="space-y-2 text-sm" style={{ color: 'var(--text-muted)' }}>
            <li>· Origin and fallback delivery when peers are unavailable</li>
            <li>· Regional edge capacity close to viewers — lower latency on first playback and for cold content</li>
            <li>· Additional resilience when a swarm, network or region degrades</li>
            <li>· Coverage in regions where viewer uplinks are constrained</li>
          </ul>
          <p className="mt-4 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            An East African edge or origin layer is a concrete example: it can shorten the first-fetch path for viewers
            there, while TorrentCDN&rsquo;s peer path carries the repeated traffic that follows. Both paths can serve the same
            session — a viewer may start on edge/origin, then receive later pieces from peers, or fall back to edge/origin
            mid-playback.
          </p>
        </Card>

        <div className="border p-5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}>
          <Label>Fallback / complementary path</Label>
          <div className="mx-auto mt-3 max-w-md" role="img" aria-label="Fallback path: TorrentCDN to CDN edge infrastructure to viewer">
            <Node title="TorrentCDN" sub="session + torrent metadata" />
            <Down label="when peers can't serve" />
            <Node title="CDN / edge infrastructure" sub="origin · regional PoP · cache" tone={FALLBACK} />
            <Down label="HTTPS" />
            <Node title="Viewer" sub="continuous playback" />
          </div>
          <p className="mt-3 text-center font-mono text-[9px] uppercase tracking-[0.15em]" style={{ color: 'var(--text-faint)' }}>
            same viewer, either path, at any point in the session
          </p>
        </div>
        <Legend />
      </Section>

      {/* ── next ──────────────────────────────────────────────────────────── */}
      <section className="mt-12 border-t pt-8" style={{ borderColor: 'var(--border)' }}>
        <h2 className="text-sm font-bold text-[color:var(--text-primary)]">Next</h2>
        <div className="mt-3 flex flex-wrap gap-3">
          <a
            href="/docs"
            className="border px-4 py-2 text-sm"
            style={{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }}
          >
            API reference &rarr;
          </a>
          <a
            href="/demo"
            className="border px-4 py-2 text-sm"
            style={{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }}
          >
            Live demo &rarr;
          </a>
          <a
            href="mailto:partnerships@torrentcdn.store"
            className="border px-4 py-2 text-sm"
            style={{ borderColor: 'var(--border-strong)', color: 'var(--text-muted)' }}
          >
            partnerships@torrentcdn.store
          </a>
        </div>
      </section>
    </div>
  )
}
