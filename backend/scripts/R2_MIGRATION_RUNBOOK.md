# R2 Migration Runbook

**Status: ✅ COMPLETE — migration live and verified 2026-08-07.**
The Supabase → Cloudflare R2 storage switch is done: env vars set via the Render API,
service redeployed, and a 60MB upload (hard 413 on Supabase's 50MB cap) proven live
against production along with dedup, refcount deletes, public serving, and import-by-URL.
All verification tooling lives in `backend/scripts/` (see below).

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

## Live values in use (2026-08-07)

```
MINIO_ENDPOINT=a942dd172ffb3faa3dd24d9b92df6008.r2.cloudflarestorage.com
MINIO_BUCKET=torrent-cdn-files
MINIO_SECURE=true
MINIO_REGION=auto
MINIO_PUBLIC_URL=https://pub-db587f977d274a458f30d14fa2ddfb9e.r2.dev  (until cdn.torrentcdn.store is live)
```
(The R2 access/secret keys are set as Render env vars; they are NOT written here.
Rotate any time via dash.cloudflare.com → R2 → API Tokens.)

## Tooling shipped for this migration (all in backend/scripts/)

| Script | Purpose |
|---|---|
| `r2_verify.py` | Local S3 round-trip against R2 (list/PUT/GET/HEAD/DELETE + public URL with browser UA). Creds from env: `R2_ACCESS_KEY` / `R2_SECRET_KEY`. Corrects this dev machine's skewed clock internally. |
| `r2_switch_env.py` | Swaps a Render service's storage env vars to R2 preserving every other var (prints masked diff, re-reads to verify). Usage: `RENDER_KEY=… R2_ACCESS_KEY=… R2_SECRET_KEY=… python3 r2_switch_env.py <service_id>`. |
| `r2_migration_verify.sh` | Live acceptance audit: 60MB presign→PUT→finalize, R2 URL assertion, public serving, dedup, refcount deletes, account cleanup. **21 checks, exit 0 = pass.** |

## How to re-verify after any change

```
bash backend/scripts/r2_migration_verify.sh   # 60MB upload + dedup + refcount (21 checks)
bash backend/scripts/import_audit_prod.sh      # import-by-URL + cross-path dedup + SSRF blocks (14 checks)
```

## Notes / gotchas

- Old files stay on Supabase forever (URLs keep serving); only NEW uploads go to R2.
- Cross-bucket dedup: after the switch, a new R2 upload of bytes that exist on Supabase
  dedups to the OLD Supabase URL (which still serves) — functionally fine.
- Cloudflare bot protection (error 1010) blocks public object GETs from clients with NO
  User-Agent. Real browsers send one, so the video player is unaffected; scripts that
  probe public objects must send a browser UA (both audit scripts do).
- `r2.dev` is rate-limited (dev-grade); `cdn.torrentcdn.store` is the production upgrade —
  requires moving `torrentcdn.store` DNS to Cloudflare (2 records: apex A → 216.198.79.1,
  `www` CNAME → 51a84a601d9a057c.vercel-dns-017.com), then R2 → bucket → Settings →
  Custom Domains → connect `cdn.torrentcdn.store`. Update `MINIO_PUBLIC_URL` when done.
- The R2 token is scoped to R2 only and can be rotated anytime.
