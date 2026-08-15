#!/usr/bin/env bash
# =============================================================================
# check_storage_public.sh — fail CI if the Cloudflare R2 public bucket is private.
#
# The app serves every file through the bucket's PUBLIC object URL
# (minio_public_url = https://pub-<id>.r2.dev/<key>). If the bucket's public
# access is ever toggled off (or the bucket is deleted/recreated without public
# access), every minio_url playback 403s — the embed player's direct-playback
# fallback silently breaks for ALL files, old and new.
#
# The check is read-only and credential-free: GET a sentinel (nonexistent) key
# on the public URL and classify the response by HTTP status (R2's public base
# serves Cloudflare's HTML error page, not S3 XML):
#   HTTP 404            → bucket is PUBLIC (sentinel missing, expected)      → PASS
#   HTTP 403            → bucket is PRIVATE / public access off              → FAIL
#   anything else / 000 → fail closed (network error or response shape change)
#
# (Historical: this script used to probe the Supabase Storage bucket
# (project-ref.supabase.co/storage/v1/object/public) and classified the
# NoSuchBucket/NoSuchKey XML bodies. Production migrated to Cloudflare R2, so
# the probe now targets the R2 public base. The logic is equivalent: a missing
# sentinel key proves the bucket is publicly reachable.)
#
# Env overrides (set as repo secrets for non-default projects):
#   R2_PUBLIC_URL   (default: https://pub-db587f977d274a458f30d14fa2ddfb9e.r2.dev)
#
# Usage:
#   bash backend/scripts/check_storage_public.sh
#
# Exit codes: 0 = bucket public, 1 = bucket private / unexpected response
# =============================================================================
set -uo pipefail

PUBLIC_BASE="${R2_PUBLIC_URL:-https://pub-db587f977d274a458f30d14fa2ddfb9e.r2.dev}"
URL="${PUBLIC_BASE%/}/__ci_bucket_publicity_check__"

CODE="$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null || echo "000")"

if [ "$CODE" = "404" ]; then
  echo "[OK]  Storage bucket is PUBLIC (sentinel key on $PUBLIC_BASE → 404, as expected)."
  exit 0
fi

if [ "$CODE" = "403" ]; then
  echo "[FAIL]  Storage bucket is PRIVATE — every file's minio_url 403s and" >&2
  echo "    playback is broken. Fix: Cloudflare R2 → bucket → Settings →" >&2
  echo "    enable Public Access (or restore the public bucket)." >&2
  exit 1
fi

echo "[FAIL]  Unexpected response from $URL: HTTP $CODE" >&2
echo "    (000 = network error; a shape change in R2's responses should be" >&2
echo "     reviewed before trusting this check again)" >&2
exit 1
