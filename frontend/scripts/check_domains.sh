#!/usr/bin/env bash
# =============================================================================
# check_domains.sh — fail CI if the frontend source references the dead
# api.torrentcdn.store host or apex-only torrentcdn.store URLs.
#
# Guards the regression class behind the 6dadd9d docs-accuracy fix:
#   - The docs page's BASE_URL once pointed at https://api.torrentcdn.store —
#     a host with NO DNS record (curl 000), so every curl example was broken.
#   - https://torrentcdn.store (the apex) 308-redirects to www, so baking the
#     apex into a URL adds a redirect hop and breaks the embed SDK's origin
#     resolution (getScriptOrigin() would load /vendor/webtorrent.min.js from
#     the apex).
# The canonical frontend origin is https://www.torrentcdn.store. A dead or
# apex-only reference in source must never ship again.
#
# Usage:
#   bash frontend/scripts/check_domains.sh
#
# Exit codes: 0 = OK, 1 = forbidden host/URL found, 2 = usage error
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

FAIL=0
check() {  # check <pass:0|1> <message>
  if [ "$1" = "0" ]; then
    echo "  ✅  $2"
  else
    echo "  ❌  $2"
    FAIL=$((FAIL + 1))
  fi
}

cd "$FRONTEND_ROOT" || {
  echo "frontend root not found: $FRONTEND_ROOT" >&2
  exit 2
}

# SHIPPED source only: app/components/lib/public + root configs. Scripts under
# scripts/ are intentionally excluded — guard scripts legitimately DOCUMENT the
# hosts (e.g. check_api_baseurl.sh's same-origin allow-list and this file's own
# docstring), so scanning them would self-flag. node_modules, .next build
# output, and the vendored WebTorrent bundle are also skipped (third-party
# minified code could legitimately contain the string).
mapfile -t FILES < <(
  find app components lib public -type f ! -path 'public/vendor/*' 2>/dev/null
  printf '%s\n' middleware.ts next.config.js vercel.json
)
if [ "${#FILES[@]}" -eq 0 ]; then
  echo "No frontend source files found to scan — did the layout change?" >&2
  exit 2
fi

# --- 1) The dead API host ------------------------------------------------------
# Any reference is broken EXCEPT the single documented warning in
# docs/page.tsx ("There is NO separate api.torrentcdn.store host — use the
# www origin"), which exists precisely so future devs don't reintroduce it.
DEAD_HITS="$(
  grep -rEn 'https?://api\.torrentcdn\.store|api\.torrentcdn\.store' "${FILES[@]}" 2>/dev/null \
    | grep -v 'NO separate api\.torrentcdn\.store host' \
    || true
)"

if [ -n "$DEAD_HITS" ]; then
  echo "Dead-host references found:"
  echo "$DEAD_HITS"
  check 1 "no references to the dead api.torrentcdn.store host (no DNS — the docs BASE_URL regression)"
else
  check 0 "no references to the dead api.torrentcdn.store host"
fi

# --- 2) Apex-only torrentcdn.store URLs ----------------------------------------
# The apex 308-redirects to www. A scheme'd or protocol-relative apex URL is
# never acceptable in source — use https://www.torrentcdn.store. (Bare words
# like "(torrentcdn.store)" in a diagnostic message are fine — only URLs fail.)
APEX_HITS="$(
  grep -rEn 'https?://torrentcdn\.store|//torrentcdn\.store' "${FILES[@]}" 2>/dev/null || true
)"

if [ -n "$APEX_HITS" ]; then
  echo "Apex-only URL references found:"
  echo "$APEX_HITS"
  check 1 "no apex-only torrentcdn.store URLs (must be https://www.torrentcdn.store)"
else
  check 0 "no apex-only torrentcdn.store URLs"
fi

echo
if [ "$FAIL" -ne 0 ]; then
  echo "Domain guard FAILED — see ❌ above." >&2
  exit 1
fi
echo "Domain guard PASSED — only www.torrentcdn.store references remain."
exit 0
