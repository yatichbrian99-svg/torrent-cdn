# R2 Migration Runbook — resume point

**Status: CODE 100% SHIPPED AND MERGED. BLOCKED ON USER CREDENTIALS.**
Every code change needed for the Supabase → Cloudflare R2 storage switch is on `main`.
The migration cannot proceed until the credentials below are provided; once they are,
the switch is a 15-minute operation (env vars → redeploy → live verification).

## Why

Supabase free tier hard-caps every uploaded file at **50MB** (confirmed live: a 51MB
presigned PUT → `HTTP 413 EntityTooLarge`). R2 removes the cap (up to 5TB/object via
multipart), costs **$0 for egress** (the business's biggest cost), and doubles as the
CDN layer. Chosen path, no card needed on Cloudflare's free tier.

## What is already shipped (all merged on main)

| Piece | Where |
|---|---|
| App upload cap 1GB → 2GB + frontend "up to 2GB" text | `backend/app/config.py`, `frontend/components/UploadForm.tsx` (PR `chore/r2-upload-prep`) |
| `_ensure_bucket()`: head/create bucket + `put_bucket_cors` so the browser→storage presigned PUT passes CORS preflight (memoized per process) | `backend/app/routes/upload.py` |
| Browser CSP `connect-src` allow-lists `https://*.r2.cloudflarestorage.com` | `frontend/middleware.ts` (PR #14) |
| `.env.example` documents R2 / B2 / Supabase / MinIO config shapes | `.env.example` |
| Delete path fails OPEN on storage errors — old Supabase-referenced files keep deleting cleanly after the switch (row deleted, failed storage delete logged, orphan sweep reclaims) | `backend/app/routes/files.py` |

Storage is fully provider-agnostic via the `MINIO_*` env vars; the switch is purely
configuration + deploy.

## Already in hand

- **Cloudflare Account ID**: `a942dd172ffb3faa3dd24d9b92df6008` (endpoint verified live:
  `https://a942dd172ffb3faa3dd24d9b92df6008.r2.cloudflarestorage.com` returns 400 on an
  unauthenticated request = up and reachable).

## Blocking — user must provide

1. **R2 Access Key ID + Secret Access Key** — custom API token at
   https://dash.cloudflare.com/profile/api-tokens:
   - Token name: `torrent-cdn-r2`
   - Permissions: **Account → R2 → Admin Read & Write** and **Account → Account Settings → Read**
   - Account Resources: All accounts · Bucket scoping: **All R2 Buckets** (bucket doesn't exist yet)
   - Copy the Secret immediately (shown once).
2. **r2.dev public URL** — create bucket `torrent-cdn-files` (region Automatic; the Admin
   token may also create it via S3 API — otherwise 1-click), then bucket →
   **Settings → Public access → Allow access** → copy `https://pub-....r2.dev`.
3. **Render API key** — https://dashboard.render.com/account/api-keys → `rkey_...`
   (lets us set env vars + redeploy via the Render API without dashboard clicks).

## Env vars to set in Render once credentials arrive

```
MINIO_ENDPOINT=a942dd172ffb3faa3dd24d9b92df6008.r2.cloudflarestorage.com
MINIO_ACCESS_KEY=<R2 Access Key ID>
MINIO_SECRET_KEY=<R2 Secret Access Key>
MINIO_BUCKET=torrent-cdn-files
MINIO_SECURE=true
MINIO_REGION=auto
MINIO_PUBLIC_URL=<https://pub-...r2.dev now; https://cdn.torrentcdn.store once the custom domain is live>
```

(Render service env via `PUT https://api.render.com/v1/services/<id>/env-vars`, then
`POST /v1/services/<id>/deploys` with `clearCache=do_not_clear`.)

## Verification after deploy (live, against production)

1. Register a throwaway account; presign-upload a **60MB** file (was a hard 413 before) → 200.
2. Upload identical bytes from a second account → `minio_url` identical (dedup fires).
3. Delete the first upload → object survives (refcount); delete the last → object removed.
4. Import-by-URL of a public file still works (pinned transport unaffected by the switch).
5. Old Supabase files still play (their URLs are unchanged).

## Notes / gotchas

- Old files stay on Supabase forever (URLs keep serving); only NEW uploads go to R2.
- Cross-bucket dedup: after the switch, a new R2 upload of bytes that exist on Supabase
  dedups to the OLD Supabase URL (which still serves) — functionally fine.
- `r2.dev` is rate-limited (dev-grade); `cdn.torrentcdn.store` is the production upgrade —
  requires moving `torrentcdn.store` DNS to Cloudflare (2 records: apex A → 216.198.79.1,
  `www` CNAME → 51a84a601d9a057c.vercel-dns-017.com), then R2 → bucket → Settings →
  Custom Domains → connect `cdn.torrentcdn.store`. Update `MINIO_PUBLIC_URL` when done.
- The R2 token is scoped to R2 only and can be rotated anytime.
