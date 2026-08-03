'use client'

import { useState, type ReactNode } from 'react'
import { Copy, Check } from 'lucide-react'

const BASE_URL = 'https://api.torrentcdn.store'

function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = async () => {
    await navigator.clipboard.writeText(code)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
  return (
    <div className="border p-3" style={{ backgroundColor: '#0c0c0c', borderColor: '#222' }}>
      <div className="mb-2 flex items-center justify-between">
        <div className="terminal-dots"><span /><span /><span /></div>
        <button onClick={handleCopy} className="flex items-center gap-1 text-xs" style={{ color: '#555', fontFamily: 'monospace' }}>
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? 'copied' : 'copy'}
        </button>
      </div>
      <pre className="overflow-x-auto text-sm leading-relaxed text-white" style={{ fontFamily: 'monospace' }}>
        <code>{code}</code>
      </pre>
    </div>
  )
}

function MethodBadge({ method }: { method: 'GET' | 'POST' | 'DELETE' }) {
  const colors: Record<string, { color: string; border: string }> = {
    GET: { color: '#4ade80', border: '#1a3a1a' },
    POST: { color: '#60a5fa', border: '#1a2a4a' },
    DELETE: { color: '#f87171', border: '#3a1a1a' },
  }
  const c = colors[method]
  return (
    <span className="inline-block border px-2 py-0.5 text-xs font-bold" style={{ color: c.color, borderColor: c.border, fontFamily: 'monospace' }}>
      {method}
    </span>
  )
}

function Endpoint({
  method, url, desc, request, response,
}: {
  method: 'GET' | 'POST' | 'DELETE'
  url: string
  desc: string
  request?: string
  response?: string
}) {
  return (
    <div id={url.replace(/[^a-zA-Z0-9]/g, '-').toLowerCase()} className="border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <MethodBadge method={method} />
        <code className="text-sm text-white" style={{ fontFamily: 'monospace' }}>{url}</code>
      </div>
      <p className="mb-4 text-sm leading-relaxed" style={{ color: '#555' }}>{desc}</p>
      {request && (
        <div className="mb-4">
          <p className="mb-1 text-[10px] font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Request</p>
          <CodeBlock code={request} />
        </div>
      )}
      {response && (
        <div>
          <p className="mb-1 text-[10px] font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Response</p>
          <CodeBlock code={response} />
        </div>
      )}
    </div>
  )
}

function SectionTitle({ children }: { children: ReactNode }) {
  return <h2 className="mb-4 text-lg font-bold tracking-[-0.02em] text-white">{children}</h2>
}

export default function DocsPage() {
  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <h1 className="text-xl font-bold tracking-[-0.02em] text-white">API Reference</h1>
      <p className="mt-2 text-sm" style={{ color: '#555' }}>
        Programmatic access to upload, manage, and analyze your media.
      </p>

      {/* 1. Introduction */}
      <section className="mt-10">
        <SectionTitle>1. Introduction</SectionTitle>
        <div className="border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <p className="mb-3 text-sm" style={{ color: '#555' }}>All endpoints use the following base URL:</p>
          <CodeBlock code={`${BASE_URL}`} />
          <div className="mt-4 space-y-2 text-sm" style={{ color: '#555' }}>
            <p><span className="font-bold text-white">Authentication:</span> Bearer token in the <code className="text-white" style={{ fontFamily: 'monospace' }}>Authorization</code> header.</p>
            <p><span className="font-bold text-white">Format:</span> All requests and responses are JSON.</p>
            <p>You can find your API key in your dashboard, or get a token via <code className="text-white" style={{ fontFamily: 'monospace' }}>POST /auth/login</code>.</p>
          </div>
        </div>
      </section>

      {/* 2. Authentication */}
      <section className="mt-10">
        <SectionTitle>2. Authentication</SectionTitle>
        <div className="space-y-4">
          <Endpoint
            method="POST"
            url="/auth/register"
            desc="Create a new account. Returns an access token you can use immediately."
            request={`{
  "email": "you@example.com",
  "password": "yourpassword1"
}`}
            response={`{
  "access_token": "<your_access_token>",
  "token_type": "bearer"
}`}
          />
          <Endpoint
            method="POST"
            url="/auth/login"
            desc="Log in with your credentials. Returns an access token."
            request={`{
  "email": "you@example.com",
  "password": "yourpassword1"
}`}
            response={`{
  "access_token": "<your_access_token>",
  "token_type": "bearer"
}`}
          />
        </div>
      </section>

      {/* 3. Upload File */}
      <section className="mt-10">
        <SectionTitle>3. Upload File</SectionTitle>
        <div className="mb-4 border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <p className="text-sm leading-relaxed" style={{ color: '#555' }}>
            Two ways to upload. <code className="text-white" style={{ fontFamily: 'monospace' }}>POST /upload</code> sends the file through the API proxy — simple, but the proxy caps request bodies at ~4.5MB, so it only suits small files. For anything real (video), use the <span className="font-bold text-white">presigned flow</span>: <code className="text-white" style={{ fontFamily: 'monospace' }}>presign</code> → PUT the file bytes <span className="font-bold text-white">directly to storage</span> (never through the proxy) → <code className="text-white" style={{ fontFamily: 'monospace' }}>finalize</code>.
          </p>
        </div>
        <Endpoint
          method="POST"
          url="/upload"
          desc="Upload a small file through the API proxy. The file is stored and a magnet link + embed URL are generated. Prefer the presigned flow below for anything over ~4.5MB."
          request={`Headers:
  Authorization: Bearer <YOUR_TOKEN>
Body:
  multipart/form-data, field name: "file"`}
          response={`{
  "file_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "filename": "video.mp4",
  "minio_url": "https://storage.example.com/...",
  "magnet_link": "magnet:?xt=urn:btih:...",
  "file_size_mb": 12.4,
  "embed_url": "https://torrentcdn.store/embed/3fa85f64-..."
}`}
        />
        <div className="mt-4">
          <p className="mb-1 text-[10px] font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>curl example</p>
          <CodeBlock code={`curl -X POST ${BASE_URL}/upload \\
  -H "Authorization: Bearer <YOUR_TOKEN>" \\
  -F "file=@video.mp4"`} />
        </div>
        <Endpoint
          method="POST"
          url="/upload/presign"
          desc="Step 1 — issue a short-lived (15 min) presigned PUT URL for direct-to-storage upload. The file bytes never touch the API proxy, so any size up to your plan limit works. Idempotent: send the same idempotency_key on retries — if a previous attempt already completed, it returns as `result` and you can skip the upload entirely."
          request={`Headers:\n  Authorization: Bearer <YOUR_TOKEN>\nBody (JSON):\n{\n  \"filename\": \"video.mp4\",\n  \"file_size\": 134217728,\n  \"content_type\": \"video/mp4\",\n  \"idempotency_key\": \"optional-stable-id\"\n}`}
          response={`{\n  \"upload_id\": \"3fa85f64-5717-4562-b3fc-2c963f66afa6\",\n  \"presigned_url\": \"https://<project>.supabase.co/storage/v1/s3/...?X-Amz-Signature=...\",\n  \"object_key\": \"<user-id>/3fa85f64-.../video.mp4\",\n  \"content_type\": \"video/mp4\",\n  \"expires_in\": 900,\n  \"result\": null\n}`}
        />
        <div className="mt-4">
          <p className="mb-1 text-[10px] font-bold uppercase tracking-widest" style={{ color: '#555', fontFamily: 'monospace' }}>Step 2 — PUT the file bytes straight to storage</p>
          <CodeBlock code={`curl -X PUT \"<presigned_url>\" -H \"Content-Type: video/mp4\" --data-binary @video.mp4`} />
          <p className="mt-2 text-xs" style={{ color: '#444' }}>
            No Authorization header here — the signature is in the URL. The Content-Type must exactly match the value returned by presign.
          </p>
        </div>
        <div className="mt-4">
          <Endpoint
            method="POST"
            url="/upload/presign/finalize/{upload_id}"
            desc="Step 3 — verify + finalize the stored object (size + file-header checks), generate the torrent magnet link and thumbnail, and record the file. Idempotent: retrying after a lost response returns the original result instead of duplicating the file."
            request={`Headers:\n  Authorization: Bearer <YOUR_TOKEN>`}
            response={`{\n  \"file_id\": \"3fa85f64-5717-4562-b3fc-2c963f66afa6\",\n  \"filename\": \"video.mp4\",\n  \"minio_url\": \"https://<project>.supabase.co/storage/v1/object/public/...\",\n  \"magnet_link\": \"magnet:?xt=urn:btih:...\",\n  \"file_size_mb\": 128.0,\n  \"embed_url\": \"https://torrentcdn.store/embed/3fa85f64-...\"\n}`}
          />
        </div>
      </section>

      {/* 4. List Files */}
      <section className="mt-10">
        <SectionTitle>4. List Files</SectionTitle>
        <Endpoint
          method="GET"
          url="/files"
          desc="List all files for the authenticated user, with per-file analytics."
          request={`Headers:
  Authorization: Bearer <YOUR_TOKEN>`}
          response={`[
  {
    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "filename": "video.mp4",
    "file_size_mb": 12.4,
    "views": 42,
    "p2p_offload_percent": 91.2,
    "bandwidth_saved_gb": 1.203,
    "active_peers": 0
  }
]`}
        />
      </section>

      {/* 5. Get File */}
      <section className="mt-10">
        <SectionTitle>5. Get File</SectionTitle>
        <Endpoint
          method="GET"
          url="/files/{file_id}"
          desc="Get a single file's details by its ID."
          request={`Headers:
  Authorization: Bearer <YOUR_TOKEN>`}
          response={`{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "filename": "video.mp4",
  "file_size_mb": 12.4,
  "views": 42,
  "p2p_offload_percent": 91.2
}`}
        />
      </section>

      {/* 6. Delete File */}
      <section className="mt-10">
        <SectionTitle>6. Delete File</SectionTitle>
        <Endpoint
          method="DELETE"
          url="/files/{file_id}"
          desc="Permanently delete a file and its stored data."
          request={`Headers:
  Authorization: Bearer <YOUR_TOKEN>`}
          response={`{
  "detail": "File deleted successfully"
}`}
        />
      </section>

      {/* 7. Analytics */}
      <section className="mt-10">
        <SectionTitle>7. Analytics</SectionTitle>
        <Endpoint
          method="GET"
          url="/analytics/{file_id}"
          desc="Get delivery analytics for one of your files. Requires authentication — only the file's owner can read its stats."
          request={`Headers:
  Authorization: Bearer <YOUR_TOKEN>`}
          response={`{
  "total_views": 42,
  "p2p_offload_percent": 91.2,
  "bandwidth_saved_gb": 1.203,
  "active_peers": 3
}`}
        />
      </section>

      {/* 8. Embed */}
      <section className="mt-10">
        <SectionTitle>8. Embed</SectionTitle>
        <div className="border p-5" style={{ borderColor: '#222', backgroundColor: '#111' }}>
          <p className="mb-3 text-sm" style={{ color: '#555' }}>
            Drop the SDK script on your page and add a single div. The player auto-mounts and streams via P2P.
          </p>
          <CodeBlock code={`<!-- Add to your HTML -->
<script src="https://torrentcdn.store/sdk.js"></script>
<div data-torrent-cdn="FILE_ID"></div>`} />
        </div>
      </section>
    </div>
  )
}
