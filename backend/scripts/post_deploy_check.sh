#!/bin/bash
# Post-deploy security verification — ONE command to confirm the hardened build
# is live in production and passes the full audit.
#
# Fast pre-flight gate (a few seconds):
#   1. Backend is healthy
#   2. Backend CSP has NO unpkg.com and NO script-src 'unsafe-inline'
#      (the pre-hardening build's CSP was: script-src 'self' 'unsafe-inline' unpkg.com)
#   3. Frontend serves a nonce-based CSP ('nonce-' + 'strict-dynamic', no unsafe-inline)
#   4. Frontend HTML script tags carry nonces (>= total-1 coverage)
#
# If ALL pre-flights pass, the script runs the full paced audit
# (curl_audit_prod.sh) so you get deep coverage in the same command.
# If ANY pre-flight fails, it reports exactly what's still stale and exits 1
# WITHOUT running the ~3-minute audit (fast-fail).
#
# Usage:
#   bash post_deploy_check.sh                      # prod defaults
#   bash post_deploy_check.sh --preflight-only     # skip the full audit
#   API_URL=... FRONTEND_URL=... bash post_deploy_check.sh
#
# Exit codes: 0 = hardened build confirmed & (if not --preflight-only) full audit passed
#             1 = pre-flight or audit failure     2 = usage error
set -uo pipefail

# Resolve the directory containing this script (robust to $PATH invocation)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

API="${API_URL:-https://torrent-cdn.onrender.com}"
FE="${FRONTEND_URL:-https://www.torrentcdn.store}"
RUN_AUDIT=1
# The commit that should be live (falls back to the local HEAD if git isn't available)
EXPECTED_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo 'the latest')"

for arg in "$@"; do
  case "$arg" in
    --preflight-only) RUN_AUDIT=0 ;;
    -h|--help)
      sed -n '2,23p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg (try --preflight-only or -h)" >&2
      exit 2
      ;;
  esac
done

PF_PASS=0
PF_FAIL=0
pf() {  # pf 0 "msg" | pf 1 "msg"
  if [ "$1" = "0" ]; then
    echo "  ✅  $2"
    PF_PASS=$((PF_PASS+1))
  else
    echo "  ❌  $2"
    PF_FAIL=$((PF_FAIL+1))
  fi
}

echo "============================================================"
echo "  POST-DEPLOY VERIFICATION  (backend: $API | frontend: $FE)"
echo "============================================================"
echo ""
echo "── PRE-FLIGHT: hardened build live? ──"

# 1. Backend health
BCODE=$(curl -so /dev/null -w '%{http_code}' "$API/health" 2>/dev/null || echo "000")
[ "$BCODE" = "200" ] && pf 0 "Backend health → $BCODE" || pf 1 "Backend health → $BCODE"

# 2. Backend CSP content
BCSP=$(curl -sI "$API/health" 2>/dev/null | grep -i 'content-security-policy' | head -1)
if [ -z "$BCSP" ]; then
  pf 1 "Backend CSP header present (MISSING)"
  echo "     (skipping CSP content checks — no header to inspect)"
else
  pf 0 "Backend CSP header present"
  echo "$BCSP" | grep -q "unpkg.com" && pf 1 "Backend CSP blocks unpkg.com (STILL ALLOWED)" || pf 0 "Backend CSP blocks unpkg.com"
  echo "$BCSP" | grep -q "script-src 'self' 'unsafe-inline'" && pf 1 "Backend CSP script-src has no 'unsafe-inline' (PRESENT)" || pf 0 "Backend CSP script-src has no 'unsafe-inline'"
  echo "$BCSP" | grep -q "localhost:9000" && pf 1 "Backend CSP has no localhost:9000 (LEAKED)" || pf 0 "Backend CSP has no localhost:9000"
fi

# 3. Frontend CSP header (nonce-based)
FE_HEADERS=$(curl -s -D - -o /dev/null "$FE/" 2>/dev/null || echo "")
FECSP=$(echo "$FE_HEADERS" | grep -i 'content-security-policy' | head -1)
if [ -z "$FECSP" ]; then
  pf 1 "Frontend CSP header present (MISSING)"
  echo "     (skipping frontend CSP content checks — no header to inspect)"
else
  pf 0 "Frontend CSP header present"
  echo "$FECSP" | grep -q 'nonce-' && pf 0 "Frontend CSP uses nonces" || pf 1 "Frontend CSP uses nonces (NO nonce)"
  echo "$FECSP" | grep -q "'strict-dynamic'" && pf 0 "Frontend CSP has 'strict-dynamic'" || pf 1 "Frontend CSP has 'strict-dynamic'"
  echo "$FECSP" | grep -q "'unsafe-inline'" && pf 1 "Frontend CSP has no 'unsafe-inline' (PRESENT)" || pf 0 "Frontend CSP has no 'unsafe-inline'"
fi

# 4. Frontend HTML nonce coverage
FE_HTML=$(curl -s "$FE/" 2>/dev/null || echo "")
TOTAL=$(echo "$FE_HTML" | grep -o '<script' | wc -l)
NONCED=$(echo "$FE_HTML" | grep -o 'nonce="[^"]*"' | wc -l)
if [ "$TOTAL" -gt 0 ] && [ "$NONCED" -ge $((TOTAL - 1)) ]; then
  pf 0 "Frontend script nonce coverage: $NONCED/$TOTAL"
else
  pf 1 "Frontend script nonce coverage: $NONCED/$TOTAL (expected >= $((TOTAL - 1)))"
fi

echo ""
echo "── PRE-FLIGHT RESULT: $PF_PASS ✅  $PF_FAIL ❌ ──"

if [ "$PF_FAIL" -ne 0 ]; then
  echo ""
  echo "⚠️  The hardened build does NOT appear to be fully live yet."
  echo "    Check Render (torrentcdn-backend) and Vercel (www.torrentcdn.store)"
  echo "    deploy status for commit $EXPECTED_COMMIT, then re-run this script."
  echo "    (Skipping the full ~3-minute audit — nothing to audit yet.)"
  exit 1
fi

if [ "$RUN_AUDIT" = "0" ]; then
  echo ""
  echo "✅  Pre-flight passed. (--preflight-only: full audit skipped.)"
  exit 0
fi

echo ""
echo "✅  Pre-flight passed — hardened build is live. Running full audit..."
echo ""
bash "$SCRIPT_DIR/curl_audit_prod.sh"
exit $?
