#!/usr/bin/env bash
# =============================================================================
# check_api_baseurl.sh — fail CI if the shipped bundle would call a cross-origin
# API that the page CSP blocks (or that breaks at the network layer).
#
# Guards against the regression class behind "Could not reach the server" with
# a healthy backend: if a build-time NEXT_PUBLIC_API_URL override (or a future
# hardcoded default) bakes a cross-origin baseURL into the axios instance, the
# browser either (a) silently blocks every fetch via connect-src, or (b) the
# direct cross-origin POST fails at the network layer. Production must always
# resolve same-origin ('' — Vercel rewrites /api/*, /auth/*, /upload, /files/*,
# /analytics/*, /billing/*, /admin/*, /notifications/* to the backend).
#
# This checks the COMPILED bundle (.next) — the ground truth of what ships —
# for a literal baseURL:"..." and passes only if it is:
#   - empty ('' — same-origin relative), or
#   - the frontend's own origin (same-origin absolute), or
#   - explicitly allow-listed in middleware.ts's connect-src (defense in depth)
#
# Usage:
#   bash frontend/scripts/check_api_baseurl.sh        # after `next build`
#
# Exit codes: 0 = OK, 1 = cross-origin baseURL not allow-listed, 2 = usage error
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHUNKS_DIR="$FRONTEND_ROOT/.next/static/chunks"
MIDDLEWARE="$FRONTEND_ROOT/middleware.ts"

FAIL=0
check() {  # check <pass:0|1> <message>
  if [ "$1" = "0" ]; then
    echo "  [OK]  $2"
  else
    echo "  [FAIL]  $2"
    FAIL=$((FAIL + 1))
  fi
}

[ -d "$CHUNKS_DIR" ] || {
  echo "Build output not found: $CHUNKS_DIR — run 'next build' first." >&2
  exit 2
}
[ -f "$MIDDLEWARE" ] || {
  echo "middleware.ts not found: $MIDDLEWARE" >&2
  exit 2
}

# --- Extract every literal baseURL:"..." from the compiled chunks ---------------
# We deliberately match the quoted form so the minified `baseURL:s` (a variable
# reference in axios's DEFAULTS) is NOT treated as a real configured base URL.
readarray -t BASE_URLS < <(
  grep -rhoE 'baseURL:"[^"]*"' "$CHUNKS_DIR" 2>/dev/null \
    | sed 's/^baseURL:"\(.*\)"$/\1/' \
    | sort -u
)

if [ "${#BASE_URLS[@]}" -eq 0 ]; then
  echo "  [FAIL]  No literal baseURL:\"...\" found in the compiled chunks — the guard "
  echo "      cannot verify what the app calls. Did the axios bundle change shape?"
  exit 1
fi

echo "Literal baseURL values found in the compiled bundle:"
for b in "${BASE_URLS[@]}"; do
  echo "    - '$b'"
done
echo

# Same-origin absolute URLs that are fine (the frontend's own domains).
SAME_ORIGIN_OK="https://www.torrentcdn.store https://torrentcdn.store"

# Local dev fallback: API_BASE is 'http://localhost:8000' when NODE_ENV !==
# 'production' (the isProduction ternary in api.ts), and middleware.ts allow-lists
# 'http://localhost:*' in connect-src for dev. This can NEVER ship in production
# (NODE_ENV=production hardcodes '' via dead-code elimination), so matching any
# localhost baseURL as allowed is safe — it only shows up in local dev builds.
ALLOWED_LOCALHOST_REGEX='^http://localhost(:[0-9]+)?$'

# CSP connect-src allow-list from middleware.ts — extract ONLY the literal
# connectSources array entries: line-start quoted URLs ('wss://…') plus the
# spread-wrapped localhost dev entry (...(['http://localhost:*'])). We
# deliberately do NOT grep the whole file: comments mention cross-origin
# backends (e.g. https://torrent-cdn.onrender.com) and would otherwise be
# mistaken for allow-listed origins, neutering the guard.
readarray -t CSP_ORIGINS < <(
  {
    grep -oE "^[[:space:]]*'[a-z]+://[^']+'" "$MIDDLEWARE" \
      | sed -E "s/^[[:space:]]*'([a-z]+:\/\/[^']+)'/\1/"
    grep -oE "'http://localhost:[*0-9]+'" "$MIDDLEWARE" \
      | tr -d "'"
  } | sort -u
)

# NEXT_PUBLIC_API_URL is inlined at build time in BOTH api.ts and middleware.ts;
# if it's cross-origin, middleware derives apiOrigin from that same var and adds
# it to connect-src (they can't drift). Mirror that here: an env-set cross-origin
# baseURL that matches NEXT_PUBLIC_API_URL is allow-listed by construction.
ENV_API_ORIGIN=""
if [[ -n "${NEXT_PUBLIC_API_URL:-}" && "$NEXT_PUBLIC_API_URL" =~ ^https?://[^/]+ ]]; then
  ENV_API_ORIGIN="${BASH_REMATCH[0]}"
fi

for b in "${BASE_URLS[@]}"; do
  if [ -z "$b" ]; then
    check 0 "'' (same-origin relative — the production default) is allowed"
    continue
  fi

  if echo "$b" | grep -qE "$ALLOWED_LOCALHOST_REGEX"; then
    check 0 "$b is the local-dev fallback (never ships in production builds)"
    continue
  fi

  SAME_ORIGIN=0
  for tok in $SAME_ORIGIN_OK; do
    [ "$b" = "$tok" ] && SAME_ORIGIN=1 && break
  done
  if [ "$SAME_ORIGIN" = "1" ]; then
    check 0 "$b is the frontend's own origin (same-origin absolute)"
    continue
  fi

  if printf '%s\n' "${CSP_ORIGINS[@]}" | grep -qxF "$b"; then
    check 0 "$b is allow-listed in middleware.ts connect-src"
    echo "      [WARN]  allow-listed cross-origin baseURL — CSP allows it, but the network"
    echo "          layer still bypasses the Vercel proxy. Prefer '' (same-origin)."
    continue
  fi

  if [ -n "$ENV_API_ORIGIN" ] && [ "$b" = "$ENV_API_ORIGIN" ]; then
    check 0 "$b matches NEXT_PUBLIC_API_URL — middleware.ts derives connect-src from the same var"
    echo "      [WARN]  env-derived cross-origin baseURL — CSP will allow it, but the network"
    echo "          layer still bypasses the Vercel proxy. Prefer '' (same-origin)."
    continue
  fi

  check 1 "cross-origin baseURL '$b' is NOT in connect-src — the browser would block every API fetch ('Could not reach the server'). Set API_BASE to '' in production (same-origin Vercel proxy) or add '$b' to middleware.ts connect-src."
done

echo
if [ "$FAIL" -ne 0 ]; then
  echo "API baseURL guard FAILED — see [FAIL] above." >&2
  exit 1
fi
echo "API baseURL guard PASSED — production resolves same-origin (or is explicitly allow-listed)."
exit 0
