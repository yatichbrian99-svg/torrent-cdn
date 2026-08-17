#!/bin/bash
# API-key flow audit — runs against the LIVE production deployment.
# Verifies the full /auth/api-key lifecycle exactly as the dashboard drives it:
#   register (completing mandatory-2FA enrollment when required) → generate key
#   (plaintext once) → key-authed GETs (/files, /auth/me, /auth/api-key
#   metadata) → unauthenticated 401 → revoke → revoked-key 401 → throwaway-
#   account cleanup.
#
# Mirrors curl_audit_prod.sh conventions:
#   - Paces requests (~2.2s apart) so the global rate limiter (30 req / 10s per IP)
#     doesn't produce false 429 failures.
#   - Auth POSTs go through the VERCEL PROXY ($FE/auth/*) — the exact same-origin
#     path the browser uses. Direct POSTs to the Render origin can fail at the
#     network layer on Render's free tier (HTTP 000 connection reset), which used
#     to produce false 'registration failed' audit failures AND orphaned users.
#   - Cleans up the throwaway user via DELETE /auth/me at the end, so repeated
#     runs never accumulate test accounts.
#   - Aborts early (exit 2) if registration or key generation fails, since the
#     remaining steps can't pass without a key.
# Usage: bash api_key_audit_prod.sh
#        API_URL=https://torrent-cdn.onrender.com FRONTEND_URL=https://www.torrentcdn.store bash api_key_audit_prod.sh
# Exit code 0 = all pass, 1 = one or more checks failed, 2 = setup aborted.
set -uo pipefail

# The health GET hits the Render origin directly (the Vercel proxy only rewrites
# /api/* and /auth/*, not /health — same split curl_audit_prod.sh uses).
API="${API_URL:-https://torrent-cdn.onrender.com}"
FE="${FRONTEND_URL:-https://www.torrentcdn.store}"
PASS=0
FAIL=0

check() {
  if [ "$1" = "0" ]; then
    echo "  [OK]  $2"
    sum "- [OK] **$2**"
    PASS=$((PASS+1))
  else
    echo "  [FAIL]  $2"
    sum "- [FAIL] **$2**"
    FAIL=$((FAIL+1))
  fi
}

echo "============================================================"
echo "  PRODUCTION API-KEY FLOW AUDIT  —  $FE"
echo "============================================================"

# GitHub Actions step-summary support: every workflow step has
# GITHUB_STEP_SUMMARY set, so mirror the audit into the run's summary page —
# a manual dispatch or scheduled run shows the probe results at a glance
# instead of requiring a dig through the job log. No-op when run locally.
SUMMARY_FILE="${GITHUB_STEP_SUMMARY:-}"
sum() { [ -n "$SUMMARY_FILE" ] && printf '%s\n' "$1" >> "$SUMMARY_FILE"; }

if [ -n "$SUMMARY_FILE" ]; then
  sum "## Production API-Key Flow Audit — $FE"
  sum ""
  if [ -n "${GITHUB_SHA:-}" ]; then
    sum "**Commit**: \`${GITHUB_SHA:0:7}\` · **Run**: [#$GITHUB_RUN_ID](https://github.com/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID)"
    sum ""
  fi
fi

# ── 0. Health (fail fast if prod is unreachable) ──
CODE=$(curl -so /dev/null -w '%{http_code}' --max-time 30 "$API/health" 2>/dev/null || echo "000")
if [ "$CODE" != "200" ]; then
  echo "[FAIL] Health check failed → HTTP $CODE; is production up?" >&2
  sum "## Production API-Key Flow Audit — $FE"
  sum ""
  sum "**[FAIL] ABORTED** — health check → HTTP $CODE (production unreachable)."
  exit 2
fi
check 0 "Health → 200"

# ── Helpers ──
# req <label> <method> <path> <auth> <json_body> — prints "label method path -> HTTP code"
# and leaves the response body in /tmp/api_key_audit_resp.json. Paths that are
# auth POSTs (register) go through the Vercel proxy; everything else can hit it too
# since the proxy rewrites all /api and /auth paths to Render.
req() {
  local label="$1" method="$2" path="$3" auth="${4:-}" body="${5:-}"
  local code="" attempt
  # One retry after a pause on transient proxy failures (502/503/504 or an empty
  # response): a Render cold start can 502 through the Vercel proxy mid-flow —
  # the same condition post_proxy and the frontend login auto-retry already
  # absorb. A definitive code (200/401/429/…) or the second attempt stands.
  for attempt in 1 2; do
    local args=(-s -o /tmp/api_key_audit_resp.json -w '%{http_code}' --max-time 60 -X "$method" "$FE$path")
    [ -n "$auth" ] && args+=(-H "Authorization: $auth")
    [ -n "$body" ] && args+=(-H 'Content-Type: application/json' -d "$body")
    code=$(curl "${args[@]}" 2>/dev/null || echo "000")
    if [ "$attempt" = "1" ] && { [ "$code" = "000" ] || [ "$code" = "502" ] || [ "$code" = "503" ] || [ "$code" = "504" ]; }; then
      echo "  [retry] $method $path -> HTTP $code (transient), retrying…" >&2
      sleep 6
      continue
    fi
    break
  done
  echo "[$label] $method $path -> HTTP $code"
  echo "    body: $(head -c 300 /tmp/api_key_audit_resp.json)"
  echo
  echo "$code" > /tmp/api_key_audit_code.txt
  sleep 2.2
}
status() { cat /tmp/api_key_audit_code.txt; }

# delete_retry <path> <auth> — DELETE via the Vercel proxy with backoff retries,
# mirroring post_proxy. Cleanup MUST eventually land: a one-shot DELETE that
# hits a transient error (429 per-IP budgets shared with the other live probes,
# 502/503/504 while a Render instance wakes, 000 on a cold-start connection
# drop) would leave an orphaned account behind — exactly what the cleanup is
# meant to prevent. Retries 3× with escalating pauses; the last attempt's code
# stands (the caller reports it). Echoes the final HTTP code.
delete_retry() {
  local path="$1" auth="$2" code
  for attempt in 1 2 3; do
    code=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE$path" \
      -H "Authorization: Bearer $auth" 2>/dev/null || echo "000")
    case "$code" in
      000|429|502|503|504)
        [ "$attempt" = "3" ] && break
        echo "  [cleanup retry] DELETE $path -> HTTP $code (transient/rate-limited), retrying in $((attempt * 20))s…" >&2
        sleep $((attempt * 20))
        ;;
      *)
        break
        ;;
    esac
  done
  echo "$code"
}

# Cleanup helper — attempts to delete the throwaway account. Called on abort
# paths too, so a mid-flow failure never leaves an orphaned test user behind.
# (A network-level 000 on a cold start can happen AFTER the server processed
# the request, so even a "failed" register may have created an account.)
cleanup_account() {
  [ -z "${JWT:-}" ] && return 0
  local code
  code=$(delete_retry "/auth/me" "$JWT")
  echo "  cleanup DELETE /auth/me → $code" >&2
}

# post_proxy <path> <json> [<auth>] — auth POST via the Vercel proxy with
# backoff retries, mirroring curl_audit_prod.sh and the frontend login
# auto-retry.
# Retries on:
#   - empty body / 000: a Render cold start dropped the connection (possibly
#     AFTER the server processed the request)
#   - 502/503/504 through the proxy while the Render instance wakes
#   - 429 "Too many registrations": the register budget is 10/hr PER IP, and a
#     workflow_dispatch runs p2p-e2e + upload-live + api-key-live concurrently
#     from GitHub's shared runner IP pool. Rejected 429 attempts do NOT consume
#     budget (the backend INCRs only after the limit check passes), so a pause
#     + retry succeeds as soon as the competing jobs' registrations finish.
post_proxy() {
  local path="$1" json="$2" auth="${3:-}" body="" code="" args
  for attempt in 1 2 3; do
    args=(-s -m 90 -o /tmp/api_key_audit_resp.json -w '%{http_code}' \
      -X POST "$FE$path" -H "Content-Type: application/json")
    [ -n "$auth" ] && args+=(-H "Authorization: Bearer $auth")
    [ -n "$json" ] && args+=(-d "$json")
    code=$(curl "${args[@]}" 2>/dev/null || echo "000")
    body=$(cat /tmp/api_key_audit_resp.json 2>/dev/null || echo "")
    case "$code" in
      000|429|502|503|504)
        [ "$attempt" = "3" ] && break
        echo "  [retry] $path -> HTTP $code (transient/rate-limited), retrying in $((attempt * 20))s…" >&2
        sleep $((attempt * 20))
        ;;
      *)
        break
        ;;
    esac
  done
  echo "$body"
}

# verify_account <jwt> — make the throwaway account's email-verification state
# usable for the rest of the probe. See upload_audit_prod.sh for the full
# rationale: with no email service register auto-verifies; with email enforced
# the account is verified via POST /admin/users/{id}/verify using the
# E2E_ADMIN_TOKEN secret. Aborts (exit 2) if the account can't be verified —
# every subsequent step would otherwise 403 on the session gate.
verify_account() {
  local jwt="$1" status uid ver
  status=$(curl -s -m 30 "$FE/auth/verification-status" -H "Authorization: Bearer $jwt" 2>/dev/null)
  if echo "$status" | grep -q '"verified":true'; then
    check 0 "Email verification: auto-verified (no email service)"
    return 0
  fi
  if [ -z "${E2E_ADMIN_TOKEN:-}" ]; then
    check 1 "Email verification required but E2E_ADMIN_TOKEN not set — cannot verify throwaway account"
    return 1
  fi
  uid=$(echo "$jwt" | cut -d. -f2 | python3 -c "import sys,base64,json; p=sys.stdin.read().strip(); p+='='*(-len(p)%4); print(json.loads(base64.urlsafe_b64decode(p)).get('sub',''))" 2>/dev/null)
  if [ -z "$uid" ]; then
    check 1 "Could not decode user id from register JWT"
    return 1
  fi
  ver=$(curl -s -m 30 -X POST "$FE/admin/users/$uid/verify" \
    -H "Content-Type: application/json" -H "Authorization: Bearer $E2E_ADMIN_TOKEN" -d '{}' 2>/dev/null)
  if echo "$ver" | grep -qE 'verified successfully|already verified'; then
    check 0 "Email verification: admin-verified"
    return 0
  fi
  check 1 "Admin-verify failed: $(echo "$ver" | head -c 200)"
  return 1
}

# totp_for <base32_secret> — current TOTP code for the secret, anchored to the
# SERVER clock: the backend verifies within a ±30s drift window, so a CI box
# with a skewed clock would produce codes ~steps off. The anchor comes from the
# /health Date response header (health is rate-limit exempt, so this request
# can't trip the global limiter).
totp_for() {
  local secret="$1" server_date
  server_date=$(curl -sI "$API/health" -D - -o /dev/null 2>/dev/null | tr -d '\r' | awk -F': ' 'tolower($1)=="date" {print $2}')
  SERVER_DATE="$server_date" python3 - "$secret" <<'PY'
import hashlib, hmac, os, struct, sys, time
from email.utils import parsedate_to_datetime

secret = sys.argv[1]
b32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
key = bytearray()
bits = value = 0
for ch in secret.upper():
    if ch == "=":
        break
    bits += 5
    value = (value << 5) | b32.index(ch)
    if bits >= 8:
        key.append((value >> (bits - 8)) & 0xFF)
        bits -= 8
try:
    offset = round(parsedate_to_datetime(os.environ["SERVER_DATE"]).timestamp() - time.time())
except Exception:
    offset = 0
msg = struct.pack(">Q", int(time.time() + offset) // 30)
h = hmac.new(bytes(key), msg, hashlib.sha1).digest()
o = h[-1] & 0x0F
print(f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}")
PY
}

# register_and_enroll <email> <password> — POST /auth/register and return a
# working session token. Production runs with MANDATORY 2FA, which gates
# REGISTRATION too: the response is mfa_required + a short-lived mfa_token and
# NO session token, so the probe completes the authenticator enrollment here —
# POST /auth/mfa/setup with the mfa_token, a computed TOTP, POST
# /auth/mfa/confirm — mirroring enrollMfa() in the e2e helpers. When MFA is
# disabled (dev) the register response carries the token directly and nothing
# extra happens. Echoes the token on stdout (empty on failure); diagnostics go
# to stderr.
register_and_enroll() {
  local email="$1" password="$2"
  local reg token mfa_token setup secret code confirm
  reg=$(post_proxy "/auth/register" "{\"email\":\"$email\",\"password\":\"$password\",\"terms_accepted\":true}" "")
  token=$(echo "$reg" | python3 -c "import json,sys;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
  if [ -n "$token" ]; then
    echo "$token"
    return 0
  fi
  if ! echo "$reg" | grep -q '"mfa_required":true'; then
    echo "  register_and_enroll: no access_token and no mfa_required — body: $(echo "$reg" | head -c 300)" >&2
    return 1
  fi
  mfa_token=$(echo "$reg" | python3 -c "import json,sys;print(json.load(sys.stdin).get('mfa_token',''))" 2>/dev/null)
  if [ -z "$mfa_token" ]; then
    echo "  register_and_enroll: mfa_required but no mfa_token — body: $(echo "$reg" | head -c 300)" >&2
    return 1
  fi
  setup=$(post_proxy "/auth/mfa/setup" "" "$mfa_token")
  secret=$(echo "$setup" | python3 -c "import json,sys;print(json.load(sys.stdin).get('secret',''))" 2>/dev/null)
  if [ -z "$secret" ]; then
    echo "  register_and_enroll: MFA setup failed — body: $(echo "$setup" | head -c 300)" >&2
    return 1
  fi
  code=$(totp_for "$secret")
  confirm=$(post_proxy "/auth/mfa/confirm" "{\"mfa_token\":\"$mfa_token\",\"code\":\"$code\"}" "")
  token=$(echo "$confirm" | python3 -c "import json,sys;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
  if [ -z "$token" ]; then
    echo "  register_and_enroll: MFA confirm failed — body: $(echo "$confirm" | head -c 300)" >&2
    return 1
  fi
  echo "$token"
  return 0
}

# ── 1. REGISTER THROWAWAY ACCOUNT ──
echo ""
echo "── 1. REGISTRATION ──"
sum "### 1. Registration"
TS=$(date +%s)
EMAIL="api-key-audit-${TS}@resend.dev"
PASSW="ProbePass123!"
JWT=$(register_and_enroll "$EMAIL" "$PASSW")
if [ -n "$JWT" ]; then
  check 0 "Registration → JWT received"
else
  check 1 "Registration failed (no access_token)"
  cleanup_account
  echo "[FAIL] Aborting — the remaining steps need a registered account." >&2
  sum "**[FAIL] ABORTED** — registration failed; remaining steps skipped."
  exit 2
fi

# Email-verify the throwaway account (see verify_account above). Abort on
# failure — the remaining steps are all verification-gated.
if ! verify_account "$JWT"; then
  cleanup_account
  echo "[FAIL] Aborting — the remaining steps need a verified account." >&2
  sum "**[FAIL] ABORTED** — verification failed; remaining steps skipped."
  exit 2
fi

# ── 2. GENERATE API KEY (plaintext returned exactly once) ──
echo ""
echo "── 2. API KEY GENERATION ──"
sum "### 2. API Key Generation"
req 2-create POST /auth/api-key "Bearer $JWT"
KEY=$(python3 -c "import json;print(json.load(open('/tmp/api_key_audit_resp.json')).get('api_key',''))" 2>/dev/null)
if [ "$(status)" = "200" ] && [ -n "$KEY" ]; then
  check 0 "Key generated → ${#KEY}-char key, prefix ${KEY:0:6}…"
else
  check 1 "Key generation → $(status) (no api_key in response)"
  cleanup_account
  echo "[FAIL] Aborting — no key to test with." >&2
  sum "**[FAIL] ABORTED** — key generation failed; remaining steps skipped."
  exit 2
fi

# ── 3. KEY-AUTHENTICATED ENDPOINTS ──
echo ""
echo "── 3. KEY AUTHENTICATION ──"
sum "### 3. Key Authentication"
req 3-metadata GET /auth/api-key "Bearer $KEY"
META_CODE=$(status)
echo "$META_CODE" | grep -q '^200' && check 0 "GET /auth/api-key with key → 200" || check 1 "GET /auth/api-key with key → $META_CODE"
# The secret must NOT be recoverable: response should carry prefix + created_at but api_key null
META=$(cat /tmp/api_key_audit_resp.json)
if [ "$META_CODE" = "200" ]; then
  echo "$META" | grep -q '"api_key":null' && check 0 "Plaintext key not recoverable (api_key: null)" || check 1 "api_key not null in metadata response!"
  echo "$META" | grep -q '"created_at"' && check 0 "created_at present" || check 1 "created_at missing"
fi

req 4-files GET /files "Bearer $KEY"
FILES_CODE=$(status)
if [ "$FILES_CODE" = "200" ] && grep -q '^\[]$' /tmp/api_key_audit_resp.json; then
  check 0 "GET /files with key → 200 []"
elif [ "$FILES_CODE" = "200" ]; then
  check 0 "GET /files with key → 200 (authed; non-empty list)"
else
  check 1 "GET /files with key → $FILES_CODE"
fi

req 5-me GET /auth/me "Bearer $KEY"
ME_CODE=$(status)
if [ "$ME_CODE" = "200" ] && grep -q "$EMAIL" /tmp/api_key_audit_resp.json; then
  check 0 "GET /auth/me with key → 200, correct account"
elif [ "$ME_CODE" = "200" ]; then
  check 1 "GET /auth/me with key → 200 but wrong account!"
else
  check 1 "GET /auth/me with key → $ME_CODE"
fi

# ── 4. UNAUTHENTICATED REJECTED ──
echo ""
echo "── 4. AUTH ENFORCEMENT ──"
sum "### 4. Auth Enforcement"
req 6-noauth GET /auth/api-key
[ "$(status)" = "401" ] && check 0 "GET /auth/api-key with no auth → 401" || check 1 "GET /auth/api-key with no auth → $(status)"

# ── 5. REVOCATION (fail-closed) ──
echo ""
echo "── 5. REVOCATION ──"
sum "### 5. Revocation (fail-closed)"
req 7-revoke DELETE /auth/api-key "Bearer $JWT"
[ "$(status)" = "200" ] && check 0 "Revoke → 200" || check 1 "Revoke → $(status)"

req 8-revoked GET /files "Bearer $KEY"
REV_CODE=$(status)
if [ "$REV_CODE" = "401" ]; then
  check 0 "Revoked key → 401 (fail-closed)"
else
  check 1 "Revoked key → $REV_CODE (expected 401!)"
fi

# ── 6. CLEANUP ──
echo ""
echo "── 6. CLEANUP ──"
sum "### 6. Cleanup"
# The final account deletion is cleanup, not an audit assertion — use
# delete_retry so a 429 on a congested egress IP can't orphan the account
# (the abort-path cleanup_account above has the same guarantee).
CLEAN_CODE=$(delete_retry "/auth/me" "$JWT")
[ "$CLEAN_CODE" = "200" ] && check 0 "Test account deleted (DELETE /auth/me) → 200" || check 1 "Cleanup → $CLEAN_CODE"

# ── SUMMARY ──
echo ""
echo "============================================================"
echo "  RESULTS: $PASS [OK]  $FAIL [FAIL]"
echo "============================================================"
if [ -n "$SUMMARY_FILE" ]; then
  sum ""
  if [ "$FAIL" -eq 0 ]; then
    sum "**Result: $PASS [OK] / $FAIL [FAIL] — [PASS] PASS**"
    sum "The API-key flow is healthy on production."
  else
    sum "**Result: $PASS [OK] / $FAIL [FAIL] — [FAIL] FAIL**"
    sum "See the job log for the failing checks."
  fi
  sum ""
  sum "<sub>Generated by \`api_key_audit_prod.sh\` — $(date -u '+%Y-%m-%d %H:%M UTC')</sub>"
fi
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
