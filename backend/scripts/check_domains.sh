#!/usr/bin/env bash
# =============================================================================
# check_domains.sh — fail CI if the backend source or IaC references the dead
# api.torrentcdn.store host or apex-only torrentcdn.store URLs.
#
# Guards the same regression class as frontend/scripts/check_domains.sh but for
# the backend surfaces:
#   - https://api.torrentcdn.store has NO DNS record (proven by curl 000 in the
#     6dadd9d docs-accuracy pass) — any example or reference built on it is
#     broken.
#   - https://torrentcdn.store (the apex) 308-redirects to www, so baking the
#     apex into a URL adds a redirect hop; a stale apex entry in the CORS
#     allow-list is dead weight (no browser page is ever served from the apex
#     origin — it always redirects).
#   - Any host listed in check_domains.denylist (dead hosts / placeholder
#     hosts like storage.example.com) must never appear in backend source or
#     IaC. NOTE: torrent-cdn.onrender.com is LIVE (the Render backend the Vercel
#     proxy rewrites to) and is deliberately NOT denylisted.
# The canonical frontend origin is https://www.torrentcdn.store. A dead or
# apex-only reference in backend source or IaC must never ship again.
#
# Usage:
#   bash backend/scripts/check_domains.sh
#
# Exit codes: 0 = OK, 1 = forbidden host/URL found, 2 = usage error
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$BACKEND_ROOT/.." && pwd)"

FAIL=0
check() {  # check <pass:0|1> <message>
  if [ "$1" = "0" ]; then
    echo "  [OK]  $2"
  else
    echo "  [FAIL]  $2"
    FAIL=$((FAIL + 1))
  fi
}

cd "$REPO_ROOT" || {
  echo "repo root not found: $REPO_ROOT" >&2
  exit 2
}

# Backend surfaces only: backend/app source + the two IaC files. backend/scripts/
# is intentionally excluded — guard scripts legitimately DOCUMENT the hosts
# (e.g. curl_audit_prod.sh's www-origin default and this file's own docstring).
# Note: the backend also references the DEAD torrentcdn.com domain in comments
# and regression tests (documenting its removal) — that is a different TLD and
# does not match these patterns, so those documented-warning references pass.
mapfile -t FILES < <(
  find backend/app -type f ! -path '*/__pycache__/*' 2>/dev/null
  printf '%s\n' Caddyfile render.yaml
)
if [ "${#FILES[@]}" -eq 0 ]; then
  echo "No backend source files found to scan — did the layout change?" >&2
  exit 2
fi

# The two root-level IaC files must exist — a renamed Caddyfile or render.yaml
# silently shrinking the scan scope is exactly the kind of drift this guards
# against.
for f in Caddyfile render.yaml; do
  if [ ! -f "$f" ]; then
    echo "Missing $f at repo root ($REPO_ROOT) — cannot scan it" >&2
    exit 2
  fi
done

# --- 1) The dead API host ------------------------------------------------------
# Any reference is broken EXCEPT a documented warning comment of the form
# "NO separate api.torrentcdn.store host" (the same exemption the frontend
# guard grants its docs warning) — it exists precisely so future devs don't
# reintroduce the host.
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
# never acceptable in source or IaC — use https://www.torrentcdn.store. (Bare
# words like "noreply@torrentcdn.store" in an email from-address or
# ".torrentcdn.store" in TRUSTED_HOSTS are fine — only URLs fail.)
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

# --- 3) Denylisted dead / placeholder hosts -------------------------------------
# Reads check_domains.denylist (one host per line, '#' comments and blank lines
# ignored) and greps each host as a literal substring across the scanned
# surfaces. Any occurrence fails — a dead host or placeholder URL baked into a
# CORS origin, response sample, email template, or IaC env value must never
# ship. The dead torrentcdn.com domain is referenced in comments/tests
# DOCUMENTING its removal and is deliberately not denylisted (see the file's
# header); the app's dead-DNS auto-correction in config.py handles the runtime
# case.
DENYLIST_FILE="$SCRIPT_DIR/check_domains.denylist"
if [ ! -f "$DENYLIST_FILE" ]; then
  echo "Denylist file missing: $DENYLIST_FILE" >&2
  exit 2
fi

DENY_HITS=""
while IFS= read -r HOST || [ -n "$HOST" ]; do
  # Tolerate casual editing: strip inline comments and surrounding whitespace
  # so a line like "  storage.example.com  # placeholder " still matches the
  # host (hosts never contain '#'). A line that is ONLY a comment becomes ''
  # and is skipped below.
  HOST="${HOST%%\#*}"
  HOST="${HOST#"${HOST%%[![:space:]]*}"}"
  HOST="${HOST%"${HOST##*[![:space:]]}"}"
  case "$HOST" in
    ''|\#*) continue ;;
  esac
  HITS="$(grep -rnF "$HOST" "${FILES[@]}" 2>/dev/null \
    | grep -v 'NO separate api\.torrentcdn\.store host' \
    || true)"
  if [ -n "$HITS" ]; then
    DENY_HITS="${DENY_HITS}${HITS}"$'\n'
  fi
done < "$DENYLIST_FILE"

if [ -n "$DENY_HITS" ]; then
  echo "Denylisted host references found:"
  echo "$DENY_HITS"
  check 1 "no references to denylisted dead/placeholder hosts (see check_domains.denylist)"
else
  check 0 "no references to denylisted dead/placeholder hosts"
fi

echo
if [ "$FAIL" -ne 0 ]; then
  echo "Domain guard FAILED — see [FAIL] above." >&2
  exit 1
fi
echo "Domain guard PASSED — only www.torrentcdn.store references remain."
exit 0
