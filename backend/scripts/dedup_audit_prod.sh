#!/bin/bash
# Content-dedup + refcount-delete audit — runs against the LIVE production
# deployment. Verifies the storage dedup and refcount-aware deletions exactly
# as users drive them:
#   register A (completing mandatory-2FA enrollment when required) → upload a
#   unique file → register B → upload IDENTICAL bytes →
#   B's minio_url must equal A's (ONE copy stored, B's duplicate deleted) →
#   DELETE A's file → the shared object must SURVIVE (B still references it,
#   and B's public embed must still resolve) → DELETE B's file (the LAST
#   reference) → the object must be REMOVED from storage and B's metadata 404.
#   Both throwaway accounts are cleaned up.
#
# Mirrors upload_audit_prod.sh / api_key_audit_prod.sh conventions:
#   - Paces requests (~2.2s apart) so the global rate limiter (30 req / 10s per
#     IP) doesn't produce false 429 failures.
#   - Auth POSTs (register/presign/finalize) go through the VERCEL PROXY
#     ($FE/*) — the same-origin path the browser uses. The file BYTES never
#     touch the proxy: they're PUT straight to the presigned storage URL.
#   - The probe file is a REAL (minimal) WebM — EBML header + varied bytes —
#     with an os.urandom tail so its content hash is UNIQUE per run: dedup can
#     only match WITHIN the run, never against a previous run's leftover rows
#     (which would break the "B's URL == A's URL" assertion and the refcount
#     checks). finalize validates magic bytes, so the header must be real.
#   - The "object gone" check accepts ANY non-200 (Supabase returns 400 for a
#     missing object, R2 returns 404 — provider-agnostic) and cache-busts the
#     URL with a fresh query param so a CDN edge can't serve a stale 200 after
#     deletion.
#   - Cleans up both files + both throwaway accounts (also on abort paths), so
#     repeated runs never accumulate test data.
#   - Aborts early (exit 2) if register/presign/finalize fail, since the
#     remaining steps can't pass without a finalized upload.
# Usage: bash dedup_audit_prod.sh
#        API_URL=https://torrent-cdn.onrender.com FRONTEND_URL=https://www.torrentcdn.store bash dedup_audit_prod.sh
# Exit code 0 = all pass, 1 = one or more checks failed, 2 = setup aborted.
set -uo pipefail

# The health GET hits the Render origin directly (the Vercel proxy only rewrites
# /api/* and /auth/*, not /health — same split curl_audit_prod.sh uses).
API="${API_URL:-https://torrent-cdn.onrender.com}"
FE="${FRONTEND_URL:-https://www.torrentcdn.store}"
PASS=0
FAIL=0

# Storage-provider assertion: every finalized minio_url must start with the
# EXPECTED_STORAGE_PREFIX (default: the R2 public base) and every presigned PUT
# URL must start with EXPECTED_S3_ENDPOINT (default: the R2 S3 endpoint). This
# makes the audit catch an accidental rollback of the MINIO_* env vars to
# Supabase (whose minio_urls start with project-ref.supabase.co and whose S3
# endpoint is project-ref.supabase.co/storage/v1/s3 — both fail the prefix
# checks). Override both when the custom CDN domain (cdn.torrentcdn.store) goes
# live and MINIO_PUBLIC_URL changes, e.g.:
#   EXPECTED_STORAGE_PREFIX=https://cdn.torrentcdn.store/ bash dedup_audit_prod.sh
# The probe file is unique per run (random tail), so a cross-bucket dedup hit
# against an OLD Supabase copy — which would legitimately produce a Supabase
# minio_url — can never occur for this check.
EXPECTED_STORAGE_PREFIX="${EXPECTED_STORAGE_PREFIX:-https://pub-db587f977d274a458f30d14fa2ddfb9e.r2.dev/}"
EXPECTED_S3_ENDPOINT="${EXPECTED_S3_ENDPOINT:-https://a942dd172ffb3faa3dd24d9b92df6008.r2.cloudflarestorage.com/}"

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
echo "  PRODUCTION CONTENT-DEDUP + REFCOUNT AUDIT  —  $FE"
echo "============================================================"

# GitHub Actions step-summary support: mirror the audit into the run's summary
# page so a manual dispatch or scheduled run shows results at a glance. No-op
# when run locally.
SUMMARY_FILE="${GITHUB_STEP_SUMMARY:-}"
sum() { [ -n "$SUMMARY_FILE" ] && printf '%s\n' "$1" >> "$SUMMARY_FILE"; }

if [ -n "$SUMMARY_FILE" ]; then
  sum "## Production Content-Dedup + Refcount Audit — $FE"
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
  [ -n "$SUMMARY_FILE" ] && sum "**[FAIL] ABORTED** — health check → HTTP $CODE (production unreachable)."
  exit 2
fi
check 0 "Health → 200"

# ── Helpers ──
# req <label> <method> <path> <auth> <json_body> — prints "label method path -> HTTP code"
# and leaves the response body in /tmp/dedup_audit_resp.json. Auth POSTs
# (register/presign/finalize) go through the Vercel proxy.
req() {
  local label="$1" method="$2" path="$3" auth="${4:-}" body="${5:-}"
  local args=(-s -o /tmp/dedup_audit_resp.json -w '%{http_code}' --max-time 90 -X "$method" "$FE$path")
  [ -n "$auth" ] && args+=(-H "Authorization: $auth")
  [ -n "$body" ] && args+=(-H 'Content-Type: application/json' -d "$body")
  local code
  code=$(curl "${args[@]}" 2>/dev/null || echo "000")
  echo "[$label] $method $path -> HTTP $code"
  echo "    body: $(head -c 300 /tmp/dedup_audit_resp.json)"
  echo
  echo "$code" > /tmp/dedup_audit_code.txt
  sleep 2.2
}
status() { cat /tmp/dedup_audit_code.txt; }

# Cleanup helper — deletes any uploaded files + both throwaway accounts. Called
# on abort paths too, so a mid-flow failure never leaves test data behind.
# Echoes "fileA fileB acctA acctB" to stdout (the caller checks them).
cleanup_all() {
  local fa="skipped" fb="skipped" aa="skipped" ab="skipped"
  if [ -n "${FILE_ID_A:-}" ] && [ -n "${JWT_A:-}" ]; then
    fa=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/files/$FILE_ID_A" \
      -H "Authorization: Bearer $JWT_A" 2>/dev/null || echo "000")
  fi
  if [ -n "${FILE_ID_B:-}" ] && [ -n "${JWT_B:-}" ]; then
    fb=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/files/$FILE_ID_B" \
      -H "Authorization: Bearer $JWT_B" 2>/dev/null || echo "000")
  fi
  if [ -n "${JWT_A:-}" ]; then
    aa=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/auth/me" \
      -H "Authorization: Bearer $JWT_A" 2>/dev/null || echo "000")
  fi
  if [ -n "${JWT_B:-}" ]; then
    ab=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/auth/me" \
      -H "Authorization: Bearer $JWT_B" 2>/dev/null || echo "000")
  fi
  echo "cleanup fileA=$fa fileB=$fb acctA=$aa acctB=$ab" >&2
  echo "$fa $fb $aa $ab"
}

# post_proxy <path> <json> <auth> — auth POST via the Vercel proxy with backoff
# retries, mirroring curl_audit_prod.sh and the frontend auto-retry. Retries on
# empty body / 000 (cold start dropped the connection), 502/503/504 (Render
# instance waking), and 429 (per-IP budgets shared with the other live probes;
# rejected attempts don't consume budget, so a pause + retry succeeds once the
# competing jobs' requests finish).
post_proxy() {
  local path="$1" json="$2" auth="$3" body="" code=""
  for attempt in 1 2 3; do
    code=$(curl -s -m 90 -o /tmp/dedup_audit_resp.json -w '%{http_code}' \
      -X POST "$FE$path" -H "Content-Type: application/json" \
      -H "Authorization: Bearer $auth" -d "$json" 2>/dev/null || echo "000")
    body=$(cat /tmp/dedup_audit_resp.json 2>/dev/null || echo "")
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
# E2E_ADMIN_TOKEN secret. Aborts (exit 2) if the account can't be verified.
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

# ── 1. REGISTER USER A ──
echo ""
echo "── 1. REGISTER USER A ──"
TS=$(date +%s)
EMAIL_A="dedup-audit-a-${TS}@resend.dev"
PASSW="ProbePass123!"
JWT_A=$(register_and_enroll "$EMAIL_A" "$PASSW")
if [ -n "$JWT_A" ]; then
  check 0 "Register A → JWT received"
else
  check 1 "Register A failed (no access_token)"
  cleanup_all >/dev/null
  echo "[FAIL] Aborting — the remaining steps need a registered account." >&2
  [ -n "$SUMMARY_FILE" ] && sum "**[FAIL] ABORTED** — user A registration failed; remaining steps skipped."
  exit 2
fi

# Email-verify user A (see verify_account above). Abort on failure — the
# remaining steps are all verification-gated.
if ! verify_account "$JWT_A"; then
  cleanup_all >/dev/null
  echo "[FAIL] Aborting — the remaining steps need a verified account." >&2
  [ -n "$SUMMARY_FILE" ] && sum "**[FAIL] ABORTED** — user A verification failed; remaining steps skipped."
  exit 2
fi

# ── 2. SYNTHESIZE A UNIQUE REAL WEBM (passes magic-byte validation) ──
# EBML header at offset 0 (real magic), then varied bytes ≥1KB so the
# compression-ratio check passes, with an os.urandom tail so the content hash
# is unique to this run. finalize validates magic bytes, so the header is real.
echo ""
echo "── 2. TEST FILE SYNTHESIS (unique per run) ──"
TEST_FILE=/tmp/dedup-audit-probe.webm
python3 - <<'EOF'
import os
header = b"\x1a\x45\xdf\xa3"  # EBML magic (offset 0)
body = bytes((i * 7 + 3) % 256 for i in range(2048))  # varied, not trivially compressible
tail = os.urandom(512)  # unique per run → unique content hash
open("/tmp/dedup-audit-probe.webm", "wb").write(header + body + tail)
EOF
FILE_SIZE=$(stat -c%s "$TEST_FILE")
echo "  synthesized $TEST_FILE (${FILE_SIZE} bytes, EBML header + varied body + random tail)"
[ "$FILE_SIZE" -ge 1024 ] && check 0 "Test WebM ≥1KB (${FILE_SIZE} bytes)" || check 1 "Test file too small (${FILE_SIZE} bytes)"

# ── 3. USER A: PRESIGN → DIRECT PUT → FINALIZE ──
echo ""
echo "── 3. USER A UPLOAD (first copy) ──"
if [ ! -f "$TEST_FILE" ]; then
  echo "[FAIL] Test file missing — aborting." >&2
  cleanup_all >/dev/null
  exit 2
fi
IDEM_A="audit-a-${TS}-$(python3 -c 'import uuid;print(uuid.uuid4())')"
req 3a-presign POST /upload/presign "Bearer $JWT_A" \
  "{\"filename\":\"dedup-audit-probe.webm\",\"file_size\":$FILE_SIZE,\"content_type\":\"video/webm\",\"idempotency_key\":\"$IDEM_A\"}"
UPLOAD_ID_A=$(python3 -c "import json;print(json.load(open('/tmp/dedup_audit_resp.json')).get('upload_id',''))" 2>/dev/null)
PRESIGNED_URL_A=$(python3 -c "import json;print(json.load(open('/tmp/dedup_audit_resp.json')).get('presigned_url',''))" 2>/dev/null)
CT_A=$(python3 -c "import json;print(json.load(open('/tmp/dedup_audit_resp.json')).get('content_type',''))" 2>/dev/null)
if [ "$(status)" = "200" ] && [ -n "$UPLOAD_ID_A" ] && [ -n "$PRESIGNED_URL_A" ]; then
  check 0 "A presign → upload_id + presigned URL"
else
  check 1 "A presign → $(status) (no upload_id/presigned_url)"
  cleanup_all >/dev/null
  echo "[FAIL] Aborting — no presigned URL to PUT to." >&2
  [ -n "$SUMMARY_FILE" ] && sum "**[FAIL] ABORTED** — user A presign failed; remaining steps skipped."
  exit 2
fi

PUT_A=$(curl -so /dev/null -w '%{http_code}' --max-time 120 -X PUT "$PRESIGNED_URL_A" \
  -H "Content-Type: $CT_A" --data-binary @"$TEST_FILE" 2>/dev/null || echo "000")
sleep 2.2
[ "$PUT_A" = "200" ] && check 0 "A PUT to storage → 200" || check 1 "A PUT to storage → $PUT_A"

FIN_A=$(post_proxy "/upload/presign/finalize/$UPLOAD_ID_A" "{}" "$JWT_A")
FILE_ID_A=$(echo "$FIN_A" | python3 -c "import json,sys;print(json.load(sys.stdin).get('file_id',''))" 2>/dev/null)
URL_A=$(echo "$FIN_A" | python3 -c "import json,sys;print(json.load(sys.stdin).get('minio_url',''))" 2>/dev/null)
if [ -n "$FILE_ID_A" ] && [ -n "$URL_A" ]; then
  check 0 "A finalize → file_id + minio_url"
  echo "    A file_id: $FILE_ID_A"
else
  check 1 "A finalize failed: $(echo "$FIN_A" | head -c 200)"
  cleanup_all >/dev/null
  echo "[FAIL] Aborting — no finalized upload A to dedup against." >&2
  [ -n "$SUMMARY_FILE" ] && sum "**[FAIL] ABORTED** — user A finalize failed; remaining steps skipped."
  exit 2
fi

# ── 3b. STORAGE PROVIDER ASSERTION (CI catches a rollback to Supabase) ──
case "$URL_A" in
  "$EXPECTED_STORAGE_PREFIX"*)
    check 0 "minio_url on expected storage ($EXPECTED_STORAGE_PREFIX…)"
    ;;
  *)
    check 1 "STORAGE ROLLBACK? minio_url does not start with $EXPECTED_STORAGE_PREFIX: $URL_A"
    ;;
esac
case "$PRESIGNED_URL_A" in
  "$EXPECTED_S3_ENDPOINT"*)
    check 0 "Presigned PUT URL on expected S3 endpoint ($EXPECTED_S3_ENDPOINT…)"
    ;;
  *)
    check 1 "STORAGE ROLLBACK? presigned URL not on $EXPECTED_S3_ENDPOINT: $PRESIGNED_URL_A"
    ;;
esac

# ── 4. REGISTER USER B ──
echo ""
echo "── 4. REGISTER USER B ──"
EMAIL_B="dedup-audit-b-${TS}@resend.dev"
JWT_B=$(register_and_enroll "$EMAIL_B" "$PASSW")
if [ -n "$JWT_B" ]; then
  check 0 "Register B → JWT received"
else
  check 1 "Register B failed (no access_token)"
  cleanup_all >/dev/null
  echo "[FAIL] Aborting — user B is needed for the dedup half of the audit." >&2
  [ -n "$SUMMARY_FILE" ] && sum "**[FAIL] ABORTED** — user B registration failed; remaining steps skipped."
  exit 2
fi

# Email-verify user B (see verify_account above). Abort on failure.
if ! verify_account "$JWT_B"; then
  cleanup_all >/dev/null
  echo "[FAIL] Aborting — the remaining steps need a verified account." >&2
  [ -n "$SUMMARY_FILE" ] && sum "**[FAIL] ABORTED** — user B verification failed; remaining steps skipped."
  exit 2
fi

# ── 5. USER B: UPLOAD THE IDENTICAL BYTES ──
echo ""
echo "── 5. USER B UPLOAD (identical bytes → should dedup) ──"
IDEM_B="audit-b-${TS}-$(python3 -c 'import uuid;print(uuid.uuid4())')"
req 5b-presign POST /upload/presign "Bearer $JWT_B" \
  "{\"filename\":\"dedup-audit-probe.webm\",\"file_size\":$FILE_SIZE,\"content_type\":\"video/webm\",\"idempotency_key\":\"$IDEM_B\"}"
UPLOAD_ID_B=$(python3 -c "import json;print(json.load(open('/tmp/dedup_audit_resp.json')).get('upload_id',''))" 2>/dev/null)
PRESIGNED_URL_B=$(python3 -c "import json;print(json.load(open('/tmp/dedup_audit_resp.json')).get('presigned_url',''))" 2>/dev/null)
CT_B=$(python3 -c "import json;print(json.load(open('/tmp/dedup_audit_resp.json')).get('content_type',''))" 2>/dev/null)
if [ "$(status)" = "200" ] && [ -n "$UPLOAD_ID_B" ] && [ -n "$PRESIGNED_URL_B" ]; then
  check 0 "B presign → upload_id + presigned URL"
else
  check 1 "B presign → $(status) (no upload_id/presigned_url)"
  cleanup_all >/dev/null
  echo "[FAIL] Aborting — no presigned URL for B to PUT to." >&2
  exit 2
fi

PUT_B=$(curl -so /dev/null -w '%{http_code}' --max-time 120 -X PUT "$PRESIGNED_URL_B" \
  -H "Content-Type: $CT_B" --data-binary @"$TEST_FILE" 2>/dev/null || echo "000")
sleep 2.2
[ "$PUT_B" = "200" ] && check 0 "B PUT to storage → 200" || check 1 "B PUT to storage → $PUT_B"

FIN_B=$(post_proxy "/upload/presign/finalize/$UPLOAD_ID_B" "{}" "$JWT_B")
FILE_ID_B=$(echo "$FIN_B" | python3 -c "import json,sys;print(json.load(sys.stdin).get('file_id',''))" 2>/dev/null)
URL_B=$(echo "$FIN_B" | python3 -c "import json,sys;print(json.load(sys.stdin).get('minio_url',''))" 2>/dev/null)
if [ -n "$FILE_ID_B" ] && [ -n "$URL_B" ]; then
  check 0 "B finalize → file_id + minio_url"
  echo "    B file_id: $FILE_ID_B"
else
  check 1 "B finalize failed: $(echo "$FIN_B" | head -c 200)"
  cleanup_all >/dev/null
  echo "[FAIL] Aborting — no finalized upload B to verify dedup on." >&2
  exit 2
fi

# ── 6. DEDUP ASSERTIONS ──
echo ""
echo "── 6. DEDUP (identical bytes store once) ──"
if [ "$URL_B" = "$URL_A" ]; then
  check 0 "B's minio_url == A's (B points at the SAME stored object)"
else
  check 1 "B's minio_url differs — dedup did NOT fire"
  echo "    A: $URL_A"
  echo "    B: $URL_B"
fi
if [ "$FILE_ID_B" != "$FILE_ID_A" ] && [ -n "$FILE_ID_B" ] && [ -n "$FILE_ID_A" ]; then
  check 0 "Separate upload rows (file_id_B ≠ file_id_A)"
else
  check 1 "Rows are NOT distinct (file_id_B == file_id_A)"
fi

# ── 7. REFCOUNT: DELETE A (original owner) — shared bytes must SURVIVE ──
echo ""
echo "── 7. REFCOUNT DELETE (A deletes, B still references) ──"
req 7-del-a DELETE "/files/$FILE_ID_A" "Bearer $JWT_A"
[ "$(status)" = "200" ] && check 0 "DELETE A's file → 200" || check 1 "DELETE A's file → $(status)"

OBJ_CODE=$(curl -so /dev/null -w '%{http_code}' --max-time 60 "$URL_A" 2>/dev/null || echo "000")
sleep 2.2
[ "$OBJ_CODE" = "200" ] && check 0 "Shared object still served after A deleted (refcount kept it)" || check 1 "Shared object → HTTP $OBJ_CODE after A deleted (should be 200!)"

PUB_B=$(curl -so /dev/null -w '%{http_code}' --max-time 60 "$FE/files/public/$FILE_ID_B" 2>/dev/null || echo "000")
sleep 2.2
[ "$PUB_B" = "200" ] && check 0 "B's public embed metadata resolves after A deleted" || check 1 "B's public metadata → HTTP $PUB_B (should be 200)"

# ── 8. LAST-REFERENCE DELETE (B deletes) — object must be REMOVED ──
echo ""
echo "── 8. LAST-REFERENCE DELETE (B deletes → object removed) ──"
req 8-del-b DELETE "/files/$FILE_ID_B" "Bearer $JWT_B"
[ "$(status)" = "200" ] && check 0 "DELETE B's file (last reference) → 200" || check 1 "DELETE B's file → $(status)"

# Cache-bust with a fresh query param: a CDN edge must not serve a stale 200
# for a deleted object (Supabase returns 400 for missing, R2 404 — any non-200
# proves removal).
GONE_CODE=$(curl -so /dev/null -w '%{http_code}' --max-time 60 "$URL_A?audit=$TS" 2>/dev/null || echo "000")
sleep 2.2
if [ "$GONE_CODE" != "200" ]; then
  check 0 "Object removed after last reference deleted (HTTP $GONE_CODE)"
else
  check 1 "Object STILL served after last reference deleted (HTTP 200)!"
fi

PUB_B2=$(curl -so /dev/null -w '%{http_code}' --max-time 60 "$FE/files/public/$FILE_ID_B" 2>/dev/null || echo "000")
sleep 2.2
[ "$PUB_B2" = "404" ] && check 0 "B's public metadata now 404 (row gone)" || check 1 "B's public metadata → HTTP $PUB_B2 (expected 404)"

# ── 9. CLEANUP ──
echo ""
echo "── 9. CLEANUP ──"
CLEAN_CODES=$(cleanup_all)
CLEAN_FILEA=$(echo "$CLEAN_CODES" | awk '{print $1}')
CLEAN_FILEB=$(echo "$CLEAN_CODES" | awk '{print $2}')
CLEAN_ACCTA=$(echo "$CLEAN_CODES" | awk '{print $3}')
CLEAN_ACCTB=$(echo "$CLEAN_CODES" | awk '{print $4}')
# A's and B's files are already deleted in steps 7/8 — the cleanup results are
# informational; what matters is that BOTH accounts are gone.
[ "$CLEAN_ACCTA" = "200" ] && check 0 "Account A deleted (DELETE /auth/me) → 200" || check 1 "Account A cleanup → ${CLEAN_ACCTA:-skipped}"
[ "$CLEAN_ACCTB" = "200" ] && check 0 "Account B deleted (DELETE /auth/me) → 200" || check 1 "Account B cleanup → ${CLEAN_ACCTB:-skipped}"

# ── SUMMARY ──
echo ""
echo "============================================================"
echo "  RESULTS: $PASS [OK]  $FAIL [FAIL]"
echo "============================================================"
if [ -n "$SUMMARY_FILE" ]; then
  sum ""
  if [ "$FAIL" -eq 0 ]; then
    sum "**Result: $PASS [OK] / $FAIL [FAIL] — [PASS] PASS**"
    sum "Content dedup and refcount deletions are healthy on production."
  else
    sum "**Result: $PASS [OK] / $FAIL [FAIL] — [FAIL] FAIL**"
    sum "See the job log for the failing checks."
  fi
  sum ""
  sum "<sub>Generated by \`dedup_audit_prod.sh\` — $(date -u '+%Y-%m-%d %H:%M UTC')</sub>"
fi
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
