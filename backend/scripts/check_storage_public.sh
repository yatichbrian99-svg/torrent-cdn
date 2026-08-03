#!/usr/bin/env bash
# =============================================================================
# check_storage_public.sh — fail CI if the Supabase Storage bucket is private.
#
# The app serves every file through the bucket's PUBLIC object URL
# (minio_public_url = https://{ref}.supabase.co/storage/v1/object/public/{bucket}).
# If the bucket is ever toggled private (or a fresh project is deployed with a
# private bucket), every minio_url playback 404s with NoSuchBucket — the embed
# player's direct-playback fallback silently breaks for ALL files, old and new.
#
# The check is read-only and credential-free: GET a sentinel (nonexistent) key
# on the public URL and classify the response:
#   {"code":"NoSuchBucket"}   → bucket is PRIVATE                              → FAIL
#   {"code":"NoSuchKey"}      → bucket is PUBLIC (sentinel missing, expected)  → PASS
#   anything else / no body   → fail closed (network error or shape change)
#
# Env overrides (set as repo secrets for non-default projects):
#   SUPABASE_PROJECT_REF   (default: bqbfrgeimurtuyjlfagc)
#   STORAGE_BUCKET         (default: torrent-cdn-files)
#
# Usage:
#   bash backend/scripts/check_storage_public.sh
#
# Exit codes: 0 = bucket public, 1 = bucket private / unexpected response
# =============================================================================
set -uo pipefail

REF="${SUPABASE_PROJECT_REF:-bqbfrgeimurtuyjlfagc}"
BUCKET="${STORAGE_BUCKET:-torrent-cdn-files}"
URL="https://${REF}.supabase.co/storage/v1/object/public/${BUCKET}/__ci_bucket_publicity_check__"

BODY="$(curl -sS --max-time 20 "$URL" 2>/dev/null || true)"

if echo "$BODY" | grep -q 'NoSuchBucket'; then
  echo "❌  Storage bucket '$BUCKET' is PRIVATE — every file's minio_url 404s and" >&2
  echo "    playback is broken. Fix: Supabase → Storage → $BUCKET → Settings →" >&2
  echo "    toggle 'Public bucket' ON." >&2
  exit 1
fi

if echo "$BODY" | grep -q 'NoSuchKey'; then
  echo "✅  Storage bucket '$BUCKET' is PUBLIC (sentinel key missing, as expected)."
  exit 0
fi

echo "❌  Unexpected response from $URL: ${BODY:-<empty — network error?>}" >&2
exit 1
