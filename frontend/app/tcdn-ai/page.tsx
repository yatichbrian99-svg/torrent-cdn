import type { Metadata } from 'next'
import type { ReactNode } from 'react'
import { ArrowUpRight } from 'lucide-react'
import CodeBlock from '@/components/ai/CodeBlock'
import EarlyAccessCta from '@/components/ai/EarlyAccessCta'
import { AI_EARLY_ACCESS_MAILTO, AI_PUBLIC_BASE_URL, AI_PUBLIC_CONTACT_EMAIL, AI_PUBLIC_MODEL } from '@/lib/aiPublic'

/**
 * Public TCDN-AI page.
 *
 * Deliberately independent of the feature flag: it is static content about the
 * product, not the product. The only request it makes is the existing console
 * status probe inside `EarlyAccessCta`, which decides whether the "Open
 * dashboard" link can go anywhere. With the flag off the page still renders and
 * still offers early access — the console route itself stays gated.
 *
 * Claims are limited to what is implemented: an OpenAI-compatible surface
 * (`/v1/chat/completions`, `/v1/models`), a private alpha with a small model
 * set and the standard privacy tier only. No security, audit, uptime or
 * production-readiness claims, and no numbers that were not measured.
 */
export const metadata: Metadata = {
  title: 'TCDN-AI — an OpenAI-compatible API built for a peer-to-peer GPU network',
  description:
    'TCDN-AI is an OpenAI-compatible API, built to be served by a peer-to-peer network of idle GPUs. Private alpha: the endpoint is enabled for invited accounts only, the first model is still being brought up, and only the standard privacy tier is offered.',
  alternates: { canonical: 'https://www.torrentcdn.store/tcdn-ai' },
}

// Real values, defined once in lib/aiPublic.ts.
const BASE_URL = AI_PUBLIC_BASE_URL
const MODEL = AI_PUBLIC_MODEL

const SNIPPETS = {
  python: `from openai import OpenAI

client = OpenAI(
    base_url="${BASE_URL}",
    api_key="YOUR_API_KEY",
)

response = client.chat.completions.create(
    model="${MODEL}",
    messages=[{"role": "user", "content": "Explain P2P delivery in one sentence."}],
    max_tokens=128,
)
print(response.choices[0].message.content)`,
  node: `import OpenAI from 'openai'

const client = new OpenAI({
  baseURL: '${BASE_URL}',
  apiKey: 'YOUR_API_KEY',
})

const response = await client.chat.completions.create({
  model: '${MODEL}',
  messages: [{ role: 'user', content: 'Explain P2P delivery in one sentence.' }],
  max_tokens: 128,
})
console.log(response.choices[0].message.content)`,
  curl: `curl ${BASE_URL}/chat/completions \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL}",
    "messages": [{"role": "user", "content": "Explain P2P delivery in one sentence."}],
    "max_tokens": 128
  }'`,
  // Cursor has no config file for this: the key and the base URL are two fields
  // in Settings → Models (plus a custom model id).
  cursor: `// Cursor → Settings → Models → API Keys
// 1. Paste your key into "OpenAI API Key" and save.
// 2. Turn on "Override OpenAI Base URL" and enter:
//      ${BASE_URL}
// 3. Add the model id as a custom model: ${MODEL}
// 4. Pick that model in a new chat.
// The override is global while it is on: every OpenAI-family request,
// including Cursor's own models, goes to the URL you entered.`,
  // Cline is also UI-only: provider, base URL, key and a model id field.
  cline: `// Cline → Settings (gear icon) → API Provider
// 1. API Provider: "OpenAI Compatible"
// 2. Base URL: ${BASE_URL}
//    (include /v1 — Cline appends only /models to what you type)
// 3. API Key: YOUR_API_KEY
// 4. Model ID: ${MODEL}
// 5. Save, then send a short prompt to check the connection.`,
  continue: `# Continue — config.yaml
name: tcdn
version: 1.0.0
schema: v1
models:
  - name: TCDN-AI ${MODEL}
    provider: openai
    model: ${MODEL}
    apiBase: ${BASE_URL}
    apiKey: YOUR_API_KEY
    roles: [chat, edit, apply]`,
  opencode: `// opencode.json ($XDG_CONFIG_HOME/opencode/opencode.json or ./opencode.json)
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "tcdn": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "TCDN-AI",
      "options": { "baseURL": "${BASE_URL}", "apiKey": "YOUR_API_KEY" },
      "models": { "${MODEL}": { "name": "${MODEL}" } }
    }
  }
}`,
}

const TODAY = [
  'Private alpha, invite only. The endpoint is enabled for invited accounts only: access is granted by hand and there is no self-serve way to get an account or a key.',
  `A small model set, still being brought up. The first model is planned to be ${MODEL}; no worker on this deployment serves it yet, so read it as the target rather than a live choice. The console's models page is the live list once there is one. Llama 3.1 is licensed under the Meta Llama 3.1 Community License.`,
  'Standard privacy tier only. The confidential tier is not available, so every job runs on the standard tier, where the host that runs it sees the prompt in plaintext.',
  'An OpenAI-compatible surface: /v1/chat/completions, /v1/models, streaming responses and the OpenAI error shape. The official OpenAI SDKs work unchanged.',
  'No availability promise. Hosts join and leave the network, so the alpha comes with no SLA and no uptime figure — there is no measured number to quote, so none is quoted.',
]

const HOW_IT_WORKS: { title: string; body: string }[] = [
  {
    title: 'Your request goes to the gateway',
    body: 'You call the OpenAI-compatible endpoint with your key, exactly as you would call any provider. The gateway checks the key, your credit balance and the model, then prices the job before it runs.',
  },
  {
    title: 'One host runs the model, not one datacenter',
    body: 'The model file is split into shards and delivered peer-to-peer, so a host fetches only the pieces it needs from its peers. A host that has the model loaded takes the whole request: the coordinator sends it a short-lived signed ticket bound to that one job, the host verifies the ticket, runs the request and returns a receipt for the tokens it produced.',
  },
  {
    title: 'You get the tokens, hosts get paid',
    body: 'The response streams back to you while the job is in flight. Token counts are checked at the gateway, the job is settled on your account from the host’s receipt and capped at what the gateway measured, and the host’s share of that same job is credited to its account.',
  },
]

const HOW_TO_USE: { title: string; body: ReactNode }[] = [
  {
    title: 'Request access',
    body: (
      <>
        Email{' '}
        <a
          href={AI_EARLY_ACCESS_MAILTO}
          className="font-mono underline transition-colors hover:text-[color:var(--text-primary)]"
          style={{ color: 'var(--text-muted)' }}
        >
          {AI_PUBLIC_CONTACT_EMAIL}
        </a>{' '}
        from the address you want the account under. We reply with an invite when there is room on
        the network.
      </>
    ),
  },
  {
    title: 'Get an API key and buy credits',
    body: 'In the console, create a key on the API keys page — it is shown once and only its prefix is stored after that. Then add credits on the billing page. Credits are prepaid and drawn down per request.',
  },
  {
    title: 'Point your tool at the base URL',
    body: (
      <>
        Use{' '}
        <code className="font-mono break-all" style={{ color: 'var(--text-primary)' }}>
          {BASE_URL}
        </code>{' '}
        as the base URL and your key as the bearer token. Anything that speaks the OpenAI protocol —
        the SDKs, curl, or an editor extension — works without a shim.
      </>
    ),
  },
]

const FAQ: { q: string; a: ReactNode }[] = [
  {
    q: 'Are my prompts stored?',
    a: 'No. The gateway records token counts, price, model, timing, host and job status — not the prompt or the output text. A host holds a job in memory while it runs and returns a receipt for the tokens; the console has nothing to show you because it keeps nothing.',
  },
  {
    q: 'How is it priced?',
    a: 'Per million input tokens and per million output tokens, per model. The console’s models page lists the current prices for each model, including any launch pricing and when it ends. Nothing here compares to another provider’s prices — the models page is the price list.',
  },
  {
    q: 'How do I pay?',
    a: 'Credit packs are bought in the console. Credit is denominated in USD, and each pack is priced in Kenyan shillings with the dollar equivalent shown. Cards work today. M-Pesa is coming: it is not enabled on our payment account yet, so it cannot be selected. During the alpha, purchases on this deployment run in test mode — checkout says which mode your account is in, and nothing is charged in test mode.',
  },
  {
    q: 'Who runs the models?',
    a: 'The design is people and small operators running the model on their own GPUs in their own locations, which is why the network could reach beyond one datacenter’s regions. Host onboarding is not open yet and the host agent is still in development — the section below is where that starts.',
  },
  {
    q: 'Can I send sensitive data?',
    a: 'Not in the alpha. Requests travel over TLS and a host only receives a job after verifying a short-lived ticket for it, but the prompt is processed in plaintext by the host that runs it, and the confidential tier is not available. Treat the alpha as suitable for non-sensitive work only.',
  },
  {
    q: 'What happens when a host disappears mid-job?',
    a: 'The request fails while it is in flight. The credit held for it is released when the job expires without a receipt, so you are not charged for tokens the gateway never received. Retrying is up to you: if the response had already started streaming it ends there, and the API does not silently re-run a request your client already treats as failed.',
  },
]

function Section({ id, n, title, children }: { id: string; n: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="mt-16 scroll-mt-20" aria-labelledby={`${id}-heading`}>
      <h2
        id={`${id}-heading`}
        className="mb-4 text-lg font-bold tracking-[-0.02em] text-[color:var(--text-primary)]"
      >
        <span className="mr-2 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
          {n}
        </span>
        {title}
      </h2>
      <div className="space-y-4">{children}</div>
    </section>
  )
}

export default function TcdnAiPublicPage() {
  return (
    <div className="ai-console mx-auto max-w-4xl px-6 pb-24 pt-12">
      {/* Hero */}
      <header>
        <p className="font-mono text-[10px] font-bold uppercase tracking-[0.2em]" style={{ color: 'var(--warn)' }}>
          Private alpha · invite only
        </p>
        <h1 className="mt-3 text-3xl font-bold leading-tight tracking-[-0.03em] text-[color:var(--text-primary)] sm:text-4xl">
          TCDN-AI: an OpenAI-compatible API built for a peer-to-peer network of idle GPUs
        </h1>
        <p className="mt-4 max-w-2xl text-base leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          One endpoint, the OpenAI request shape you already use. Requests are built to run on machines
          that people and small operators connect to the network, in the countries they are in, instead
          of a single provider’s datacenter. Pricing is in USD per million tokens; the console shows the
          shilling equivalent of each credit pack, and M-Pesa is the local channel — coming, not enabled
          on our payment account yet.
        </p>
        <div className="mt-6 flex flex-col gap-3 sm:flex-row sm:items-center">
          <EarlyAccessCta />
          {/* The full architecture page is otherwise reachable only from one
              inline sentence at the end of §02, which reads as "there is
              nowhere to click" for a visitor who wants the design detail. */}
          <a
            href="/tcdn-ai/architecture"
            className="inline-flex items-center justify-center gap-1 border px-4 py-2 text-sm font-semibold transition-colors hover:border-[color:var(--border-strong)]"
            style={{ borderColor: 'var(--border)', color: 'var(--text-primary)' }}
          >
            Architecture
            <ArrowUpRight size={14} aria-hidden="true" />
          </a>
        </div>
        <p className="mt-3 text-xs" style={{ color: 'var(--text-muted)' }}>
          The endpoint is enabled for invited accounts. Alpha access is granted by hand and there is no
          self-serve signup for the API yet.
        </p>
      </header>

      <Section id="today" n="01" title="What is true today">
        <ul className="space-y-3">
          {TODAY.map((item) => (
            <li key={item} className="flex gap-3 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
              <span aria-hidden="true" className="mt-2 h-1 w-1 shrink-0" style={{ backgroundColor: 'var(--text-muted)' }} />
              <span>{item}</span>
            </li>
          ))}
        </ul>
      </Section>

      <Section id="how-it-works" n="02" title="How it works in three steps">
        <ol className="grid gap-4 sm:grid-cols-3">
          {HOW_IT_WORKS.map((step, i) => (
            <li
              key={step.title}
              className="border p-4"
              style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}
            >
              <p className="font-mono text-[10px] font-bold uppercase tracking-[0.2em]" style={{ color: 'var(--text-muted)' }}>
                Step {i + 1}
              </p>
              <p className="mt-2 text-sm font-semibold text-[color:var(--text-primary)]">{step.title}</p>
              <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
                {step.body}
              </p>
            </li>
          ))}
        </ol>
        <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
          <a
            href="/tcdn-ai/architecture"
            className="underline transition-colors hover:text-[color:var(--text-primary)]"
          >
            Read the full architecture
          </a>{' '}
          — components, the seven-step job lifecycle, settlement and the gaps this build does not close.
        </p>
      </Section>

      <Section id="how-to-use" n="03" title="How to use it">
        <ol className="space-y-4">
          {HOW_TO_USE.map((step, i) => (
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

        <div className="mt-6 space-y-4">
          <CodeBlock label="OpenAI Python" code={SNIPPETS.python} />
          <CodeBlock label="OpenAI JS" code={SNIPPETS.node} />
          <CodeBlock label="curl" code={SNIPPETS.curl} />
        </div>

        <h3 className="pt-4 text-sm font-bold text-[color:var(--text-primary)]">Editor and agent settings</h3>
        <p className="text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          Each tool below takes the same base URL and key, and the model id is the same string
          everywhere. The settings were checked against each tool’s own documentation, not run from
          inside the editor: treat them as untested until you try one with an invited account.
        </p>
        <div className="space-y-4">
          <CodeBlock label="Cursor" code={SNIPPETS.cursor} />
          <CodeBlock label="Cline" code={SNIPPETS.cline} />
          <CodeBlock label="Continue" code={SNIPPETS.continue} />
          <CodeBlock label="OpenCode" code={SNIPPETS.opencode} />
        </div>
      </Section>

      <Section id="faq" n="04" title="Questions">
        <dl className="divide-y" style={{ borderColor: 'var(--border)' }}>
          {FAQ.map((item) => (
            <div key={item.q} className="py-4">
              <dt className="text-sm font-semibold text-[color:var(--text-primary)]">{item.q}</dt>
              <dd className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
                {item.a}
              </dd>
            </div>
          ))}
        </dl>
      </Section>

      <Section id="hosts" n="05" title="Have a GPU?">
        <div
          className="border p-6"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-card)' }}
        >
          <h3 className="text-base font-bold text-[color:var(--text-primary)]">
            Want to earn with your GPU? Hosts coming soon
          </h3>
          <p className="mt-2 text-sm leading-relaxed" style={{ color: 'var(--text-muted)' }}>
            The host agent is in development. When it ships, a host will run the model on its own
            hardware and be credited a share of every job it completes, booked in the same ledger the
            customer pays into. There is no withdrawal route for host earnings yet, and no public
            process to join — onboarding is not open. Ask to be told when it is.
          </p>
          <div className="mt-5">
            <EarlyAccessCta />
          </div>
        </div>
      </Section>
    </div>
  )
}
