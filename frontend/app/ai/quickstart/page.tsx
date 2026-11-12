'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Check, Copy } from 'lucide-react'
import { aiConsole, testApiKey, type AiModel } from '@/lib/aiConsole'
import { apiErrorMessage } from '@/lib/api'
import { isLoggedIn } from '@/lib/auth'
import { Badge, Button, Card, ErrorState, Skeleton } from '@/components/ai/ui'

type SnippetId = 'curl' | 'python' | 'node' | 'cursor' | 'cline' | 'continue' | 'opencode'

const TABS: { id: SnippetId; label: string }[] = [
  { id: 'curl', label: 'curl' },
  { id: 'python', label: 'Python' },
  { id: 'node', label: 'Node' },
  { id: 'cursor', label: 'Cursor' },
  { id: 'cline', label: 'Cline' },
  { id: 'continue', label: 'Continue' },
  { id: 'opencode', label: 'OpenCode' },
]

function snippets(baseUrl: string, model: string): Record<SnippetId, string> {
  return {
    curl: `curl ${baseUrl}/chat/completions \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${model}",
    "messages": [{"role": "user", "content": "Explain P2P delivery in one sentence."}],
    "max_tokens": 128
  }'`,
    python: `from openai import OpenAI

client = OpenAI(
    base_url="${baseUrl}",
    api_key="YOUR_API_KEY",
)

response = client.chat.completions.create(
    model="${model}",
    messages=[{"role": "user", "content": "Explain P2P delivery in one sentence."}],
    max_tokens=128,
)
print(response.choices[0].message.content)`,
    node: `import OpenAI from 'openai'

const client = new OpenAI({
  baseURL: '${baseUrl}',
  apiKey: 'YOUR_API_KEY',
})

const response = await client.chat.completions.create({
  model: '${model}',
  messages: [{ role: 'user', content: 'Explain P2P delivery in one sentence.' }],
  max_tokens: 128,
})
console.log(response.choices[0].message.content)`,
    cursor: `// Cursor → Settings → Models → OpenAI API
// Override the base URL and add the key as an OpenAI API key:
{
  "openai": {
    "apiKey": "YOUR_API_KEY",
    "baseUrl": "${baseUrl}"
  }
}
// Then pick "${model}" from the model list.`,
    cline: `// Cline → Settings → API Provider: OpenAI Compatible
{
  "openAiCompatible": {
    "baseUrl": "${baseUrl}",
    "apiKey": "YOUR_API_KEY",
    "modelId": "${model}"
  }
}`,
    continue: `# Continue (config.yaml)
models:
  - name: TCDN-AI ${model}
    provider: openai
    model: ${model}
    apiBase: ${baseUrl}
    apiKey: YOUR_API_KEY`,
    opencode: `// opencode.json
{
  "provider": {
    "tcdn": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "${baseUrl}", "apiKey": "YOUR_API_KEY" },
      "models": { "${model}": { "name": "${model}" } }
    }
  }
}`,
  }
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)
    }
  }
  return (
    <button
      type="button"
      onClick={copy}
      className="inline-flex items-center gap-1 border px-2 py-1 text-[10px] font-bold uppercase tracking-widest"
      style={{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }}
      aria-label={copied ? 'Snippet copied' : 'Copy snippet'}
    >
      {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

export default function AiQuickstartPage() {
  const router = useRouter()
  const [models, setModels] = useState<AiModel[] | null>(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState<SnippetId>('curl')
  const [apiKey, setApiKey] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [test, setTest] = useState<{ status: number; ok: boolean; body: string } | null>(null)
  const [testing, setTesting] = useState(false)

  const load = useCallback(() => {
    setError('')
    setModels(null)
    aiConsole
      .models()
      .then((data) => setModels(data.models.filter((model) => model.allowlisted)))
      .catch((err: any) => {
        if (err?.response?.status === 401) {
          router.push('/')
          return
        }
        setError(apiErrorMessage(err, 'Could not load the model list'))
      })
  }, [router])

  useEffect(() => {
    if (!isLoggedIn()) {
      router.push('/')
      return
    }
    load()
  }, [load, router])

  const baseUrl = useMemo(() => {
    if (typeof window === 'undefined') return '/api/ai/v1'
    return `${window.location.origin}/api/ai/v1`
  }, [])

  const model = models?.[0]?.model_id ?? 'your-model-id'
  const body = snippets(baseUrl, model)[tab]

  const runTest = async () => {
    if (!apiKey.trim()) return
    setTesting(true)
    setTest(null)
    try {
      const result = await testApiKey(apiKey.trim(), model)
      setTest(result)
      // The key was only needed for this request; drop it from state.
      setApiKey('')
    } catch (err: any) {
      setError(apiErrorMessage(err, 'The test request could not be sent'))
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="space-y-6">
      <Card
        ear="Endpoint"
        title="OpenAI-compatible base URL"
        footer="Use a key from the API keys page. Send it as a bearer token; never put it in a URL or in client-side code."
      >
        <div className="flex flex-wrap items-center gap-2">
          <code
            className="break-all border px-3 py-2 font-mono text-xs"
            style={{ borderColor: 'var(--border)', color: 'var(--text-primary)', backgroundColor: 'var(--bg-primary)' }}
          >
            {baseUrl}
          </code>
          <CopyButton value={baseUrl} />
        </div>
        {models && models.length > 0 && (
          <p className="mt-3 text-xs" style={{ color: 'var(--text-muted)' }}>
            Models served here: {models.map((m) => m.model_id).join(', ')}
          </p>
        )}
      </Card>

      <Card
        ear="Snippets"
        title="Copy, paste, run"
        action={
          <div className="flex flex-wrap gap-1">
            {TABS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                aria-pressed={tab === item.id}
                className="border px-2 py-1 text-[10px] font-bold uppercase tracking-widest"
                style={{
                  borderColor: tab === item.id ? 'var(--text-primary)' : 'var(--border)',
                  color: tab === item.id ? 'var(--text-primary)' : 'var(--text-muted)',
                  backgroundColor: tab === item.id ? 'var(--bg-raised)' : 'transparent',
                }}
              >
                {item.label}
              </button>
            ))}
          </div>
        }
      >
        {error ? (
          <ErrorState message={error} onRetry={load} />
        ) : models === null ? (
          <Skeleton rows={4} />
        ) : models.length === 0 ? (
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            No model is allowlisted on this deployment, so there is nothing to call yet.
          </p>
        ) : (
          <>
            <div className="flex items-start justify-between gap-3">
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                Replace <code className="font-mono">YOUR_API_KEY</code> with a key you created.
              </p>
              <CopyButton value={body} />
            </div>
            <pre
              className="mt-3 overflow-x-auto border p-4 font-mono text-[11px] leading-relaxed"
              style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-primary)', color: 'var(--text-soft)' }}
            >
              <code>{body}</code>
            </pre>
          </>
        )}
      </Card>

      <Card
        ear="Verify"
        title="Test my key"
        footer="One minimal request (max_tokens=8). The key stays in this tab's memory, is cleared after the call, and is never logged by this page."
      >
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex-1" style={{ minWidth: '260px' }}>
            <span className="text-xs font-medium" style={{ color: 'var(--text-muted)' }}>
              API key
            </span>
            <input
              type={showKey ? 'text' : 'password'}
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder="tcdn_ai_…"
              className="mt-1 w-full border px-3 py-2 font-mono text-xs outline-none"
              style={{
                borderColor: 'var(--border-strong)',
                backgroundColor: 'var(--bg-primary)',
                color: 'var(--text-primary)',
              }}
            />
          </label>
          <Button onClick={() => setShowKey((value) => !value)}>
            {showKey ? 'Hide' : 'Show'}
          </Button>
          <Button variant="primary" onClick={runTest} disabled={testing || !apiKey.trim()}>
            {testing ? 'Testing…' : 'Test key'}
          </Button>
        </div>

        {test && (
          <div className="mt-4" role="status" aria-live="polite">
            <div className="flex items-center gap-2">
              <Badge tone={test.ok ? 'ok' : 'err'}>
                {test.ok ? 'Key works' : `HTTP ${test.status}`}
              </Badge>
              <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
                {test.ok
                  ? 'The gateway accepted the key and returned a completion.'
                  : 'The gateway rejected the request — the body below says why.'}
              </span>
            </div>
            <pre
              className="mt-2 max-h-64 overflow-auto border p-3 font-mono text-[11px]"
              style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-primary)', color: 'var(--text-soft)' }}
            >
              <code>{test.body}</code>
            </pre>
          </div>
        )}
      </Card>
    </div>
  )
}
