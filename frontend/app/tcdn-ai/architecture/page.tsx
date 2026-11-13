import type { Metadata } from 'next'
import type { ReactNode } from 'react'

/**
 * Public TCDN-AI architecture page.
 *
 * Like `/tcdn-ai` itself this is static content about the product, not the
 * product: no flag, no request, no console data. Everything below is taken
 * from `docs/tcdn-ai/ARCHITECTURE.md` and the limitations list beside it, and
 * the two rules those documents set apply here too: no claim that the code
 * does not back, and every known gap stated where a reader would otherwise
 * assume the opposite.
 */
export const metadata: Metadata = {
  title: 'TCDN-AI architecture — how one request runs on a peer-to-peer GPU network',
  description:
    'The components of TCDN-AI — coordinator, worker agent, seeder swarm, gateway and ledger — and the path a single inference request takes through them: placement, signed ticket, shard delivery, encrypted streaming, signed receipt and settlement.',
  alternates: { canonical: 'https://www.torrentcdn.store/tcdn-ai/architecture' },
}

const COORDINATOR = 'var(--info)'
const WORKER = 'var(--ok)'
const MONEY = 'var(--warn)'

function Section({ id, n, title, children }: { id: string; n: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="mt-12 scroll-mt-20" aria-labelledby={`${id}-heading`}>
      <h2
        id={`${id}-heading`}
        className="mb-4 text-lg font-bold tracking-[-0.02em] text-[color:var(--text-primary)]"
      >
        <span className="mr-2 font-mono text-xs" style={{ color: 'var(--text-faint)' }}>
          {n}
        </span>
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

function Node({ title, sub, tone }: { title: string; sub?: string; tone?: string }) {
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
      <span className="text-base leading-none" style={{ color: 'var(--text-dim)' }}>
        ↓
      </span>
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
    { c: COORDINATOR, label: 'Coordinator (control plane)' },
    { c: WORKER, label: 'Worker + seeders (compute, weights)' },
    { c: MONEY, label: 'Ledger / settlement' },
  ]
  return (
    <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2">
      {items.map((i) => (
        <span key={i.label} className="flex items-center gap-2">
          <span className="h-2 w-2" style={{ backgroundColor: i.c }} />
          <span
            className="font-mono text-[10px] uppercase tracking-[0.12em]"
            style={{ color: 'var(--text-muted)' }}
          >
            {i.label}
          </span>
        </span>
      ))}
    </div>
  )
}

const COMPONENTS = [
  {
    name: 'Coordinator',
    role: 'The control plane. Authenticates the key, prices the job, places the credit hold, picks a worker, signs a short-lived ticket, verifies receipts, settles the job and writes the audit row.',
    note: 'Never receives prompt content and holds no session key, so it cannot read worker traffic.',
  },
  {
    name: 'Worker agent',
    role: 'Runs on the host machine beside the engine (vLLM or an Ollama/Ollama-compatible server). Verifies the ticket, decrypts the request, fetches any weights it does not have, streams tokens back and signs a receipt for what it produced.',
    note: 'Holds one job in memory for the length of the request; nothing about the prompt is persisted.',
  },
  {
    name: 'Seeder swarm',
    role: 'Serves model weights as shards, peer to peer. A worker fetches only the pieces it needs from peers that already have them, each shard checked against the signed manifest before it is used.',
    note: 'A sender that serves a bad shard is rejected and banned for that fetch; the worker retries elsewhere and falls back to an HTTPS origin if it must.',
  },
  {
    name: 'Gateway',
    role: 'The OpenAI-compatible surface — /v1/chat/completions and /v1/models. It is what a stock OpenAI SDK or curl client talks to, so nothing has to be installed to try the API.',
    note: 'In gateway mode the gateway holds the plaintext, so it is also the billing authority — see §5.',
  },
  {
    name: 'SDK',
    role: 'The Python client for SDK mode. Verifies the signed ticket against the pinned coordinator key, encrypts the request to the worker and decrypts the response, so the coordinator never sees the prompt.',
    note: 'Only a Python SDK exists; a TypeScript port is specified, not implemented.',
  },
  {
    name: 'Ledger (PostgreSQL)',
    role: 'Accounts, keys, nodes, jobs, holds, ledger entries, receipts, attestations, model prices and the audit log. Every balance movement is an integer micro-credit entry with a unique idempotency key.',
    note: 'All state that matters survives a coordinator restart; in-process replay caches do not (documented limitation).',
  },
]

const LIFECYCLE = [
  {
    title: 'Place the job, before the prompt is sent',
    body: 'A placement call carries the model, the maximum tokens, the privacy tier and a price cap — never the prompt. The coordinator authenticates the key, prices the job, posts a hold against the balance and selects an eligible worker.',
  },
  {
    title: 'Hand back a signed ticket',
    body: 'The coordinator returns a ticket for that one job: job id, worker endpoint, worker public key and an expiry. In SDK mode the client verifies the coordinator’s signature before it sends anything, so a tampered ticket is refused at the edge.',
  },
  {
    title: 'Verify, then decrypt',
    body: 'In SDK mode the request travels over TLS 1.3 with per-request X25519 key agreement, HKDF-SHA-256 and AES-256-GCM. The worker checks the ticket, decrypts the payload and refuses a confidential job outright if it is not itself attested.',
  },
  {
    title: 'Fetch only the weights that are missing',
    body: 'If the model is not cached, the worker pulls shards from the seeder swarm through a signed manifest. Each shard is hash-checked against the manifest before it reaches the engine.',
  },
  {
    title: 'Stream the tokens',
    body: 'Token frames go back to the client as they are produced; in SDK mode they are encrypted to the client’s session key. A worker that dies mid-stream is detectable, because the stream simply ends without the receipt that follows it.',
  },
  {
    title: 'Sign a receipt for what was produced',
    body: 'The worker posts a signed receipt to the coordinator: token counts in and out, timing, model and the hash of what it returned. Receipts are what prove which code ran, not what a client claims happened.',
  },
  {
    title: 'Settle once, then split',
    body: 'The receipt settles the job, capped at the hold placed before it ran. Settlement is split 70 % compute / 10 % seeder pool / 20 % platform, and the whole thing is one atomic, idempotent operation — a replay returns the first result instead of double-posting.',
  },
]

const FAILURES = [
  ['A worker disappears before returning anything', 'The job expires with no receipt; the hold is released and nothing is charged. The client re-places — there is no output to keep.'],
  ['A worker fails after the first token', 'The partial text is returned with an explicit partial-result error. Billing uses the tokens the gateway actually saw, not what the client asked for.'],
  ['Two settlements race', 'One wins on the unique idempotency key; the loser returns the original result. A settled job cannot be re-priced by a late receipt.'],
  ['A shard fails its hash', 'It is rejected before it reaches the engine, the sender is banned for that fetch, and the worker retries another peer or the HTTPS origin.'],
  ['The coordinator restarts', 'Balances, holds, jobs and receipts are in PostgreSQL; the job is still there. In-process replay caches and nonces reset, which is a documented limitation.'],
  ['A confidential job meets a non-attested worker', 'Placement finds no attested worker and fails closed. The worker would refuse the job independently, so there is no downgrade path.'],
]

export default function TcdnAiArchitecturePage() {
  return (
    <div className="ai-console mx-auto max-w-3xl px-6 py-10">
      <p
        className="font-mono text-[10px] font-bold uppercase tracking-[0.2em]"
        style={{ color: 'var(--text-faint)' }}
      >
        TCDN-AI / Architecture
      </p>
      <h1 className="mt-2 page-title">How a TCDN-AI request runs</h1>
      <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
        TCDN-AI separates the part of an inference service that has to be centralised from the part that
        does not. A small <span className="font-bold text-[color:var(--text-primary)]">coordinator</span>{' '}
        owns identity, pricing, placement and settlement; the model itself runs on{' '}
        <span className="font-bold text-[color:var(--text-primary)]">workers</span> — machines operated by
        people and small operators — which get their weights from each other rather than from one
        datacenter. This page follows one request through that system, and says plainly where the design
        stops short of the guarantee a reader might assume.
      </p>

      {/* ── 1 ─────────────────────────────────────────────────────────────── */}
      <Section id="components" n="01" title="The pieces">
        <div className="grid gap-4 sm:grid-cols-2">
          {COMPONENTS.map((c) => (
            <Card key={c.name}>
              <p className="text-sm font-bold text-[color:var(--text-primary)]">{c.name}</p>
              <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
                {c.role}
              </p>
              <p className="mt-2 text-xs leading-relaxed" style={{ color: 'var(--text-faint)' }}>
                {c.note}
              </p>
            </Card>
          ))}
        </div>
      </Section>

      {/* ── 2 ─────────────────────────────────────────────────────────────── */}
      <Section id="path" n="02" title="Where the bytes go">
        <Card>
          <div
            className="mx-auto max-w-md"
            role="img"
            aria-label="Request path: client or gateway to coordinator for placement and a signed ticket; the client then sends the encrypted request to the worker over TLS; the worker fetches shards from the seeder swarm; the worker posts a signed receipt to the coordinator, which settles the job in Postgres"
          >
            <Node title="Your client" sub="OpenAI SDK, curl, or the TCDN-AI SDK" />
            <Down label="request" />
            <Node title="Gateway (optional)" sub="OpenAI-compatible /v1 surface" tone={COORDINATOR} />
            <Down label="placement · price · hold" />
            <Node title="Coordinator" sub="auth · scheduler · tickets · receipts" tone={COORDINATOR} />
            <Down label="signed ticket, scoped to one job" />
            <Node title="Worker agent" sub="vLLM / Ollama · verifies ticket, decrypts" tone={WORKER} />
            <Down label="missing shards" />
            <Node title="Seeder swarm" sub="peer-to-peer weights, Merkle-verified" tone={WORKER} />
            <p
              className="mt-2 text-center font-mono text-[9px] uppercase tracking-[0.15em]"
              style={{ color: 'var(--text-faint)' }}
            >
              the prompt goes client → worker, never through the coordinator’s hands in SDK mode
            </p>
          </div>
        </Card>
        <div className="border p-4" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-secondary)' }}>
          <Label>Two control edges, one data edge</Label>
          <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            The coordinator talks to the client (placement, ticket, ack) and to the worker (registry,
            receipts). The prompt and the completion travel on a separate connection between client and
            worker. That split is the reason the coordinator can settle a job without being able to read
            it.
          </p>
        </div>
        <Legend />
      </Section>

      {/* ── 3 ─────────────────────────────────────────────────────────────── */}
      <Section id="lifecycle" n="03" title="One request, seven steps">
        <ol className="space-y-4">
          {LIFECYCLE.map((step, i) => (
            <li key={step.title} className="flex gap-3">
              <span
                className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center border font-mono text-[10px] font-bold"
                style={{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }}
                aria-hidden="true"
              >
                {i + 1}
              </span>
              <div>
                <p className="text-sm font-semibold text-[color:var(--text-primary)]">{step.title}</p>
                <p className="mt-1 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
                  {step.body}
                </p>
              </div>
            </li>
          ))}
        </ol>
        <div
          className="border p-4"
          style={{ borderColor: 'var(--warn-chip-border)', backgroundColor: 'var(--warn-chip-bg)' }}
        >
          <Label tone={MONEY}>Ordering that matters</Label>
          <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            The hold exists before the job does, the receipt is verified against the node the job was
            assigned to, and the <span className="font-bold text-[color:var(--text-primary)]">receipt</span>{' '}
            settles the job — not the client’s acknowledgement. Billing on the acknowledgement would make
            revenue optional for the payer: a client that never acked would leave the job unsettled and the
            expiry sweep would release the hold, so the completion would be free. The ack is evidence: a
            token-count or output-hash mismatch flags the job for review while the charge stands.
          </p>
        </div>
      </Section>

      {/* ── 4 ─────────────────────────────────────────────────────────────── */}
      <Section id="money" n="04" title="Money is integer micro-credits">
        <Card>
          <p className="text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            Balances, holds, charges and payouts are integer micro-credits — no floating point ever touches
            a balance. One credit is 1,000,000 micro-credits and $1 is 100,000,000, so every price is exact
            to the micro-credit. A job can never cost more than the hold placed for it, and a receipt that
            claims more than the hold is capped at the hold.
          </p>
          <dl className="mt-4 grid gap-x-6 gap-y-3 sm:grid-cols-[9rem_1fr]">
            {[
              ['Split', '70 % compute · 10 % seeder pool · 20 % platform, from the same settled amount'],
              ['Idempotency', 'Every ledger entry carries a unique key, so a retried settlement returns the original result instead of posting twice'],
              ['Concurrency', 'A per-account advisory lock plus “available = balance − open holds” means two simultaneous placements cannot both spend the same credit'],
              ['Refunds', 'Only through the admin refund path; there is no automatic reversal in the request flow'],
            ].map(([k, v]) => (
              <div key={k} className="contents">
                <dt
                  className="font-mono text-[10px] uppercase tracking-[0.15em] sm:pt-0.5"
                  style={{ color: 'var(--text-faint)' }}
                >
                  {k}
                </dt>
                <dd className="text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
                  {v}
                </dd>
              </div>
            ))}
          </dl>
        </Card>
        <p className="text-xs leading-relaxed" style={{ color: 'var(--text-faint)' }}>
          Credit is denominated in USD. Each pack is priced in Kenyan shillings with the dollar equivalent
          shown, and purchases on this deployment run in test mode during the alpha.
        </p>
      </Section>

      {/* ── 5 ─────────────────────────────────────────────────────────────── */}
      <Section id="modes" n="05" title="Two ways in, and who can see the prompt">
        <div className="grid gap-4 sm:grid-cols-2">
          <Card>
            <Label tone={WORKER}>SDK mode (Python)</Label>
            <ul className="mt-2 space-y-2 text-sm" style={{ color: 'var(--text-muted)' }}>
              <li>· Client encrypts the request to the worker, end to end</li>
              <li>· Coordinator never holds the prompt or the completion</li>
              <li>· Receipt is the billing authority</li>
              <li>· Redundant execution cannot be dispatched by the coordinator, so sampled jobs are recorded as not compared</li>
            </ul>
          </Card>
          <Card>
            <Label tone={COORDINATOR}>Gateway mode (/v1)</Label>
            <ul className="mt-2 space-y-2 text-sm" style={{ color: 'var(--text-muted)' }}>
              <li>· Any OpenAI client works unchanged</li>
              <li>· The gateway relays plaintext in transit; prompts and completions are never logged or stored</li>
              <li>· The gateway measures both sides, so it is the billing authority; the lower of its count and the receipt is billed</li>
              <li>· Standard tier only — a <code className="font-mono">confidential</code> parameter is rejected, not downgraded</li>
            </ul>
          </Card>
        </div>
        <p className="text-xs leading-relaxed" style={{ color: 'var(--text-faint)' }}>
          A node whose receipts bill above the gateway’s measurement in more than a configured share of its
          settled jobs — once it has enough of them to judge — is suspended and its pending payouts are held
          for an operator. Both counts are recorded as evidence for review; neither is treated as a verdict.
        </p>
      </Section>

      {/* ── 6 ─────────────────────────────────────────────────────────────── */}
      <Section id="tiers" n="06" title="Privacy tiers">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border-strong)' }}>
                {['Tier', 'Status', 'What the operator running the job can see'].map((h) => (
                  <th
                    key={h}
                    className="py-2 pr-4 text-left font-mono text-[10px] font-bold uppercase tracking-[0.15em]"
                    style={{ color: 'var(--text-faint)' }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody style={{ color: 'var(--text-muted)' }}>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                <td className="py-3 pr-4 align-top font-semibold text-[color:var(--text-primary)]">Standard</td>
                <td className="py-3 pr-4 align-top">Available</td>
                <td className="py-3 align-top">
                  The prompt in plaintext. Traffic is encrypted in transit, but the host that runs the model
                  can read the request and the response.
                </td>
              </tr>
              <tr>
                <td className="py-3 pr-4 align-top font-semibold text-[color:var(--text-primary)]">
                  Confidential
                </td>
                <td className="py-3 pr-4 align-top">
                  <span style={{ color: 'var(--warn)' }}>Not available in this build</span>
                </td>
                <td className="py-3 align-top">
                  Nothing: placement requires a live, verified attestation bound to the worker’s session key,
                  and no real attestation verifier ships yet, so no eligible host exists.
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="text-xs leading-relaxed" style={{ color: 'var(--text-faint)' }}>
          Treat the alpha as suitable for non-sensitive work only. This is a design property, not a
          configuration an operator can turn on.
        </p>
      </Section>

      {/* ── 7 ─────────────────────────────────────────────────────────────── */}
      <Section id="weights" n="07" title="Weights arrive peer to peer">
        <Card>
          <p className="text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            A model file is split into shards and published with a signed manifest: shard sizes, their
            hashes and a Merkle root. A worker that needs a model fetches only the shards it is missing from
            peers that already have them, verifies each one against the manifest before it reaches the
            engine, and bans a sender that serves corrupt data for that fetch. The manifest is signed, so a
            peer cannot substitute a different file set.
          </p>
          <p className="mt-3 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            Shard transport is a seam, not a single implementation. The <span className="font-mono">ShardPeer</span>{' '}
            interface is what the fetch logic talks to: manifest verification, Merkle proofs, integrity
            checks, peer banning, resume and quota handling are transport-agnostic and tested with
            in-process peers.
          </p>
        </Card>
        <div className="border p-4" style={{ borderColor: 'var(--warn-chip-border)', backgroundColor: 'var(--warn-chip-bg)' }}>
          <Label tone={MONEY}>The honest gap</Label>
          <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            The HTTPS origin fallback ships and is tested. A production peer-to-peer transport (libtorrent,
            or a JavaScript sidecar) does not — the browser’s WebTorrent layer cannot be driven from a
            Python worker — so a real swarm is unexercised. Weight delivery beyond the tested origin path is
            design plus verified protocol logic, not a shipped swarm.
          </p>
        </div>
      </Section>

      {/* ── 8 ─────────────────────────────────────────────────────────────── */}
      <Section id="verification" n="08" title="Verification is probabilistic">
        <Card>
          <p className="text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            A deterministic, secret-keyed sample of jobs (3 % by default) is chosen for redundant execution.
            The job is re-run on a <em>different</em> operator and the two transcript commitments are
            compared: a disagreement loses the disagreeing node reputation and can suspend it, and the
            decision, the faulty node ids and the transcript root are written to the audit log.
          </p>
          <p className="mt-3 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            That is not a proof. It cannot catch a wrong answer every sampled executor agrees on, it says
            nothing about the 97 % of jobs that are never sampled, and the comparison is an exact hash, so
            two honest but non-deterministic runs are recorded as inconclusive rather than attributed. In
            gateway mode the second execution is dispatched automatically and paid from platform revenue; in
            SDK mode the coordinator never holds the plaintext, so the sample is recorded as not dispatched
            and nothing is compared.
          </p>
        </Card>
        <p className="text-xs leading-relaxed" style={{ color: 'var(--text-faint)' }}>
          Nothing here should be read as “verified inference” for an unsampled job.
        </p>
      </Section>

      {/* ── 9 ─────────────────────────────────────────────────────────────── */}
      <Section id="failures" n="09" title="What happens when something breaks">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border-strong)' }}>
                {['Situation', 'Behaviour'].map((h) => (
                  <th
                    key={h}
                    className="py-2 pr-4 text-left font-mono text-[10px] font-bold uppercase tracking-[0.15em]"
                    style={{ color: 'var(--text-faint)' }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody style={{ color: 'var(--text-muted)' }}>
              {FAILURES.map(([situation, behaviour]) => (
                <tr key={situation} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td className="w-1/2 py-3 pr-4 align-top">{situation}</td>
                  <td className="py-3 align-top">{behaviour}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      {/* ── 10 ────────────────────────────────────────────────────────────── */}
      <Section id="limits" n="10" title="What this page does not claim">
        <Card>
          <ul className="space-y-2 text-sm" style={{ color: 'var(--text-muted)' }}>
            <li>· No availability figure, uptime or SLA: hosts join and leave, and there is no measured number to quote.</li>
            <li>· No confidential tier in this build — the interfaces exist, a real attestation verifier does not.</li>
            <li>· No gateway streaming guarantee across a failure: an error after the first token arrives in-band, and the job expires and releases its hold.</li>
            <li>
              · Gateway support is chat completions only; tools, JSON mode,{' '}
              <code className="font-mono">seed</code> and sampling penalties, and embeddings return an
              explicit unsupported-parameter error.
            </li>
            <li>· Only a Python SDK exists; the TypeScript port is specified, not built.</li>
            <li>· The gateway’s token count is measured, not produced by a tokenizer — a small tolerance applies, and an engine whose tokenisation differs is flagged rather than corrected.</li>
            <li>· Multi-instance coordination is not shipped: rate-limit buckets and replay caches live in process memory, and the Redis-backed version is not implemented.</li>
          </ul>
          <p className="mt-4 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            The full list — including the simulated attestation adapter, the dev-stub inference backend and
            the free-credit abuse controls — lives in the limitations document that ships with the code.
          </p>
        </Card>
      </Section>

      {/* ── next ──────────────────────────────────────────────────────────── */}
      <section className="mt-12 border-t pt-8" style={{ borderColor: 'var(--border)' }}>
        <h2 className="text-sm font-bold text-[color:var(--text-primary)]">Next</h2>
        <div className="mt-3 flex flex-wrap gap-3">
          <a
            href="/tcdn-ai"
            className="border px-4 py-2 text-sm"
            style={{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }}
          >
            TCDN-AI overview &rarr;
          </a>
          <a
            href="/docs"
            className="border px-4 py-2 text-sm"
            style={{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }}
          >
            API reference &rarr;
          </a>
          <a
            href="/architecture"
            className="border px-4 py-2 text-sm"
            style={{ borderColor: 'var(--border-strong)', color: 'var(--text-muted)' }}
          >
            TorrentCDN delivery architecture &rarr;
          </a>
        </div>
      </section>
    </div>
  )
}
