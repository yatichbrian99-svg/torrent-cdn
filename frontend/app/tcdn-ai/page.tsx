import type { Metadata } from 'next'
import type { ReactNode } from 'react'
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
  title: 'TCDN-AI — an OpenAI-compatible API on a peer-to-peer GPU network',
  description:
    'TCDN-AI serves an OpenAI-compatible API from a peer-to-peer network of idle GPUs. Private alpha: invite only, a small model set, standard privacy tier. Point the OpenAI SDK at the base URL and use your key.',
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
  cursor: `// Cursor → Settings → Models → OpenAI API
// Add the key as an OpenAI API key, then override the base URL:
{
  "openai": {
    "apiKey": "YOUR_API_KEY",
    "baseUrl": "${BASE_URL}"
  }
}
// Then pick "${MODEL}" from the model list.`,
  cline: `// Cline → Settings → API Provider: OpenAI Compatible
{
  "openAiCompatible": {
    "baseUrl": "${BASE_URL}",
    "apiKey": "YOUR_API_KEY",
    "modelId": "${MODEL}"
  }
}`,
  continue: `# Continue (config.yaml)
models:
  - name: TCDN-AI ${MODEL}
    provider: openai
    model: ${MODEL}
    apiBase: ${BASE_URL}
    apiKey: YOUR_API_KEY`,
  opencode: `// opencode.json
{
  "provider": {
    "tcdn": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "${BASE_URL}", "apiKey": "YOUR_API_KEY" },
      "models": { "${MODEL}": { "name": "${MODEL}" } }
    }
  }
}`,
}

const TODAY = [
  'Private alpha, invite only. Access is granted by hand; signing up for TorrentCDN does not create an AI account on its own.',
  `A small model set. ${MODEL} is the model this alpha serves. The list grows as hosts, hardware and licences allow — the console's models page is the live list.`,
  'Standard privacy tier only. The confidential tier is built but not offered here, so every job runs on the standard tier.',
  'An OpenAI-compatible surface: /v1/chat/completions, /v1/models, streaming responses and the OpenAI error shape. The official OpenAI SDKs work unchanged.',
  'No availability promise. Hosts join and leave the network, so the alpha comes with no SLA and no uptime figure — there is no measured number to quote, so none is quoted.',
]

const HOW_IT_WORKS: { title: string; body: string }[] = [
  {
    title: 'Your request goes to the gateway',
    body: 'You call the OpenAI-compatible endpoint with your key, exactly as you would call any provider. The gateway checks the key, your credit balance and the model, then prices the job before it runs.',
  },
  {
    title: 'Hosts run the model, not one datacenter',
    body: 'The model is split into shards. Hosts that hold a shard are sent a short-lived signed ticket for the job; each host verifies it, runs its part, and returns a receipt for the tokens it produced.',
  },
  {
    title: 'You get the tokens, hosts get paid',
    body: 'The response streams back to you while the job is in flight. Token counts and cost are settled on your account from the hosts’ receipts, and the host’s share is credited from the same job.',
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
    a: 'Credit packs are bought in the console. Prices are quoted in USD and shown in Kenyan shillings at checkout, where M-Pesa is the payment channel; cards are the other option. During the alpha, purchases on this deployment run in test mode — checkout states which mode your account is in, and no card is charged in test mode.',
  },
  {
    q: 'Who runs the models?',
    a: 'People and small operators running the model on their own GPUs in their own locations, which is why the network can reach beyond one datacenter’s regions. Host onboarding is not open yet — the section below is where that starts.',
  },
  {
    q: 'Can I send sensitive data?',
    a: 'Not in the alpha. Requests travel over TLS and a host only receives a job after verifying a short-lived ticket for it, but the prompt is processed in plaintext by the host that runs it, and the confidential tier is not available yet. Treat the alpha as suitable for non-sensitive work.',
  },
  {
    q: 'What happens when a host disappears mid-job?',
    a: 'The job fails and you are not charged for tokens the gateway did not receive. Retries are yours to make: the API returns an ordinary OpenAI-shaped error rather than silently re-running a request your client already considers failed.',
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
          TCDN-AI: an OpenAI-compatible API served by a peer-to-peer network of idle GPUs
        </h1>
        <p className="mt-4 max-w-2xl text-base leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          One endpoint, the OpenAI request shape you already use. Requests run on machines that people
          and small operators have connected to the network, in whatever countries they happen to be,
          instead of a single provider’s datacenter. You pay per million tokens in your own currency,
          including M-Pesa in Kenya.
        </p>
        <div className="mt-6">
          <EarlyAccessCta />
        </div>
        <p className="mt-3 text-xs" style={{ color: 'var(--text-muted)' }}>
          Alpha access is granted by hand. There is no self-serve signup for the API yet.
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
          Each tool below takes the same base URL and key. The model id is the same string everywhere.
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
            Hosts run the model on their own hardware and are credited a share of every job their GPU
            completes, settled from the same job the customer paid for. Onboarding is not open yet: the
            host agent, the payout ledger and the checks a host must pass are built, but there is no
            public process to join. Ask to be told when there is.
          </p>
          <div className="mt-5">
            <EarlyAccessCta />
          </div>
        </div>
      </Section>
    </div>
  )
}
