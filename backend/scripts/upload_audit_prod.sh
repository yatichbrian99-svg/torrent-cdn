#!/bin/bash
# Upload (presigned direct-to-storage) flow audit — runs against the LIVE
# production deployment. Verifies the PRIMARY upload path end-to-end exactly
# as the browser drives it:
#   register throwaway account (completing mandatory-2FA enrollment when
#   required) → POST /upload/presign (Bearer JWT) → PUT the
#   file bytes DIRECT to the presigned storage URL (Supabase before the R2
#   migration, Cloudflare R2 now) → POST /upload/presign/finalize/{id} → real
#   40-hex btih magnet → DELETE the file → DELETE /auth/me cleanup.
#
# Mirrors api_key_audit_prod.sh conventions:
#   - Paces requests (~2.2s apart) so the global rate limiter (30 req / 10s per
#     IP) doesn't produce false 429 failures.
#   - Auth POSTs (register/presign/finalize) go through the VERCEL PROXY
#     ($FE/*) — the same-origin path the browser uses. The file BYTES never
#     touch the proxy: they're PUT straight to the presigned URL (the entire
#     point of the presign design). Direct POSTs to the Render origin can fail
#     at the network layer on Render's free tier.
#   - finalize validates magic bytes, so the probe PUTs a REAL (minimal) WebM:
#     EBML header + varied bytes ≥1KB. Generated with python3 (no ffmpeg
#     needed), so the job stays a single checkout+run step.
#   - Cleans up: DELETE /files/{id} then DELETE /auth/me (also on abort paths),
#     so repeated runs never accumulate test uploads or accounts.
#   - Aborts early (exit 2) if register/presign/finalize fail, since the
#     remaining steps can't pass without a finalized upload.
# Usage: bash upload_audit_prod.sh
#        API_URL=https://torrent-cdn.onrender.com FRONTEND_URL=https://www.torrentcdn.store bash upload_audit_prod.sh
# Exit code 0 = all pass, 1 = one or more checks failed, 2 = setup aborted.
set -uo pipefail

# The health GET hits the Render origin directly (the Vercel proxy only rewrites
# /api/* and /auth/*, not /health — same split curl_audit_prod.sh uses).
API="${API_URL:-https://torrent-cdn.onrender.com}"
FE="${FRONTEND_URL:-https://www.torrentcdn.store}"
PASS=0
FAIL=0

# Storage-provider assertion: the presigned PUT URL must start with
# EXPECTED_S3_ENDPOINT and the finalized minio_url with EXPECTED_STORAGE_PREFIX
# (defaults: the Cloudflare R2 S3 endpoint + public base). This catches an
# accidental rollback of the MINIO_* env vars to Supabase (whose S3 endpoint is
# project-ref.supabase.co/storage/v1/s3 and whose public URLs start with
# project-ref.supabase.co — both fail the prefix checks). Override both when
# the custom CDN domain (cdn.torrentcdn.store) goes live and MINIO_PUBLIC_URL
# changes, e.g.:
#   EXPECTED_STORAGE_PREFIX=https://cdn.torrentcdn.store/ bash upload_audit_prod.sh
EXPECTED_STORAGE_PREFIX="${EXPECTED_STORAGE_PREFIX:-https://pub-db587f977d274a458f30d14fa2ddfb9e.r2.dev/}"
EXPECTED_S3_ENDPOINT="${EXPECTED_S3_ENDPOINT:-https://a942dd172ffb3faa3dd24d9b92df6008.r2.cloudflarestorage.com/}"

check() {
  if [ "$1" = "0" ]; then
    echo "  [OK]  $2"
    PASS=$((PASS+1))
  else
    echo "  [FAIL]  $2"
    FAIL=$((FAIL+1))
  fi
}

echo "============================================================"
echo "  PRODUCTION UPLOAD (PRESIGN) FLOW AUDIT  —  $FE"
echo "============================================================"

# ── 0. Health (fail fast if prod is unreachable) ──
CODE=$(curl -so /dev/null -w '%{http_code}' --max-time 30 "$API/health" 2>/dev/null || echo "000")
if [ "$CODE" != "200" ]; then
  echo "[FAIL] Health check failed → HTTP $CODE; is production up?" >&2
  exit 2
fi
check 0 "Health → 200"

# ── Helpers ──
# req <label> <method> <path> <auth> <json_body> — prints "label method path -> HTTP code"
# and leaves the response body in /tmp/upload_audit_resp.json. Paths that are
# auth POSTs (register/presign/finalize) go through the Vercel proxy.
req() {
  local label="$1" method="$2" path="$3" auth="${4:-}" body="${5:-}"
  local args=(-s -o /tmp/upload_audit_resp.json -w '%{http_code}' --max-time 90 -X "$method" "$FE$path")
  [ -n "$auth" ] && args+=(-H "Authorization: $auth")
  [ -n "$body" ] && args+=(-H 'Content-Type: application/json' -d "$body")
  local code
  code=$(curl "${args[@]}" 2>/dev/null || echo "000")
  echo "[$label] $method $path -> HTTP $code"
  echo "    body: $(head -c 300 /tmp/upload_audit_resp.json)"
  echo
  echo "$code" > /tmp/upload_audit_code.txt
  sleep 2.2
}
status() { cat /tmp/upload_audit_code.txt; }

# delete_retry <path> <auth> — DELETE via the Vercel proxy with backoff retries,
# mirroring post_proxy. Cleanup MUST eventually land: a one-shot DELETE that
# hits a transient error (429 per-IP budgets shared with the other live probes,
# 502/503/504 while a Render instance wakes, 000 on a cold-start connection
# drop) would leave an orphaned file/account behind — exactly what the
# cleanup is meant to prevent. Retries 3× with escalating pauses; the last
# attempt's code stands (the caller reports it, e.g. 429 after a genuinely
# exhausted hourly budget). Echoes the final HTTP code.
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

# Cleanup helper — attempts to delete the uploaded file + throwaway account.
# Called on abort paths too, so a mid-flow failure never leaves test data behind.
# Echoes "file <code> account <code>" to stdout (the caller checks them) and
# to stderr for the abort-path callers that just want the log line.
cleanup_all() {
  local fcode="skipped" acode="skipped"
  if [ -n "${FILE_ID:-}" ] && [ -n "${JWT:-}" ]; then
    fcode=$(delete_retry "/files/$FILE_ID" "$JWT")
  fi
  if [ -n "${JWT:-}" ]; then
    acode=$(delete_retry "/auth/me" "$JWT")
  fi
  echo "cleanup file=$fcode account=$acode" >&2
  echo "$fcode $acode"
}

# post_proxy <path> <json> <auth> — auth POST via the Vercel proxy with
# backoff retries, mirroring curl_audit_prod.sh and the frontend auto-retry.
# Retries on empty body / 000 (cold start dropped the connection), 502/503/504
# (Render instance waking), and 429 "Too many registrations" (the register
# budget is 10/hr PER IP and a workflow_dispatch runs p2p-e2e + upload-live +
# api-key-live from GitHub's shared runner IP pool). Rejected 429 attempts do
# NOT consume budget (the backend INCRs only after the limit check passes), so
# a pause + retry succeeds once the competing jobs' registrations finish.
post_proxy() {
  local path="$1" json="$2" auth="$3" body="" code=""
  for attempt in 1 2 3; do
    code=$(curl -s -m 90 -o /tmp/upload_audit_resp.json -w '%{http_code}' \
      -X POST "$FE$path" -H "Content-Type: application/json" \
      -H "Authorization: Bearer $auth" -d "$json" 2>/dev/null || echo "000")
    body=$(cat /tmp/upload_audit_resp.json 2>/dev/null || echo "")
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
# usable for the rest of the probe.
#
# Production restricts a register-issued token for an UNVERIFIED account to the
# verification endpoints (the session gate), so a fresh account can't upload
# until verified. Two paths resolve this:
#   1. No email service configured → register auto-verifies; verification-status
#      returns true and there is nothing to do.
#   2. Email service configured → the probe verifies the account through the
#      admin endpoint (POST /admin/users/{id}/verify) using E2E_ADMIN_TOKEN
#      (an ADMIN user's Bearer token, repo secret). Without a valid admin token
#      the probe aborts (exit 2): every subsequent step would 403 and the
#      cascade would be noise instead of a verdict.
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
TS=$(date +%s)
EMAIL="upload-audit-${TS}@resend.dev"
PASSW="ProbePass123!"
JWT=$(register_and_enroll "$EMAIL" "$PASSW")
if [ -n "$JWT" ]; then
  check 0 "Registration → JWT received"
else
  check 1 "Registration failed (no access_token)"
  cleanup_all
  echo "[FAIL] Aborting — the remaining steps need a registered account." >&2
  exit 2
fi

# Email-verify the throwaway account (see verify_account above). Abort on
# failure — the remaining steps are all verification-gated.
if ! verify_account "$JWT"; then
  cleanup_all
  echo "[FAIL] Aborting — the remaining steps need a verified account." >&2
  exit 2
fi

# ── 2. SYNTHESIZE A MINIMAL REAL WEBM (passes magic-byte validation) ──
# finalize runs validate_file_magic, so the file MUST be a real WebM: EBML
# header at offset 0, then varied bytes ≥1KB (the compression-ratio check
# requires ≥1KB of sample with >2 unique bytes). Python3 is on ubuntu-latest —
# no ffmpeg dependency keeps the CI job a single checkout+run step.
echo ""
echo "── 2. TEST FILE SYNTHESIS ──"
TEST_FILE=/tmp/upload-audit-probe.webm
python3 - <<'EOF'
# Minimal valid WebM: EBML header + a repetitive-but-non-compressible body.
# (It needn't be a *playable* stream — finalize only validates the header
# magic + entropy, then torrents the bytes.)
header = b"\x1a\x45\xdf\xa3"  # EBML magic (offset 0)
body = bytes((i * 7 + 3) % 256 for i in range(2048))  # varied, not trivially compressible
open("/tmp/upload-audit-probe.webm", "wb").write(header + body)
EOF
FILE_SIZE=$(stat -c%s "$TEST_FILE")
echo "  synthesized $TEST_FILE (${FILE_SIZE} bytes, EBML header + varied body)"
[ "$FILE_SIZE" -ge 1024 ] && check 0 "Test WebM ≥1KB (${FILE_SIZE} bytes)" || check 1 "Test file too small (${FILE_SIZE} bytes)"

# ── 3. PRESIGN ──
echo ""
echo "── 3. PRESIGN ──"
if [ ! -f "$TEST_FILE" ]; then
  echo "[FAIL] Test file missing — aborting." >&2
  cleanup_all >/dev/null
  exit 2
fi
IDEM_KEY="audit-${TS}-$(python3 -c 'import uuid;print(uuid.uuid4())')"
req 3-presign POST /upload/presign "Bearer $JWT" \
  "{\"filename\":\"upload-audit-probe.webm\",\"file_size\":$FILE_SIZE,\"content_type\":\"video/webm\",\"idempotency_key\":\"$IDEM_KEY\"}"
UPLOAD_ID=$(python3 -c "import json;print(json.load(open('/tmp/upload_audit_resp.json')).get('upload_id',''))" 2>/dev/null)
PRESIGNED_URL=$(python3 -c "import json;print(json.load(open('/tmp/upload_audit_resp.json')).get('presigned_url',''))" 2>/dev/null)
CT=$(python3 -c "import json;print(json.load(open('/tmp/upload_audit_resp.json')).get('content_type',''))" 2>/dev/null)
if [ "$(status)" = "200" ] && [ -n "$UPLOAD_ID" ] && [ -n "$PRESIGNED_URL" ]; then
  check 0 "Presign → upload_id + presigned URL"
else
  check 1 "Presign → $(status) (no upload_id/presigned_url)"
  cleanup_all
  echo "[FAIL] Aborting — no presigned URL to PUT to." >&2
  exit 2
fi
echo "$PRESIGNED_URL" | grep -q '^https://' && check 0 "Presigned URL is https" || check 1 "Presigned URL not https"
case "$PRESIGNED_URL" in
  "$EXPECTED_S3_ENDPOINT"*)
    check 0 "Presigned PUT URL on expected S3 endpoint ($EXPECTED_S3_ENDPOINT…)"
    ;;
  *)
    check 1 "STORAGE ROLLBACK? presigned URL not on $EXPECTED_S3_ENDPOINT: $PRESIGNED_URL"
    ;;
esac

# ── 4. DIRECT PUT TO STORAGE (bytes never touch the proxy) ──
echo ""
echo "── 4. DIRECT PUT TO STORAGE ──"
# ContentType is a SIGNED header on the presigned URL — send exactly what the
# backend minted, or the provider rejects the signature.
PUT_CODE=$(curl -so /dev/null -w '%{http_code}' --max-time 120 -X PUT "$PRESIGNED_URL" \
  -H "Content-Type: $CT" --data-binary @"$TEST_FILE" 2>/dev/null || echo "000")
sleep 2.2
if [ "$PUT_CODE" = "200" ]; then
  check 0 "PUT to storage → 200"
else
  check 1 "PUT to storage → $PUT_CODE"
fi

# ── 5. FINALIZE ──
echo ""
echo "── 5. FINALIZE ──"
FIN=$(post_proxy "/upload/presign/finalize/$UPLOAD_ID" "{}" "$JWT")
FILE_ID=$(echo "$FIN" | python3 -c "import json,sys;print(json.load(sys.stdin).get('file_id',''))" 2>/dev/null)
MAGNET=$(echo "$FIN" | python3 -c "import json,sys;print(json.load(sys.stdin).get('magnet_link',''))" 2>/dev/null)
MINIO_URL=$(echo "$FIN" | python3 -c "import json,sys;print(json.load(sys.stdin).get('minio_url',''))" 2>/dev/null)
if [ -n "$FILE_ID" ]; then
  check 0 "Finalize → file_id received"
  echo "$MAGNET" | grep -qE '^magnet:\?xt=urn:btih:[0-9a-f]{40}' && check 0 "Real 40-hex btih magnet" || check 1 "Magnet not a real btih: $(echo "$MAGNET" | head -c 80)"
  [ -n "$MINIO_URL" ] && check 0 "minio_url present" || check 1 "minio_url missing"
  case "$MINIO_URL" in
    "$EXPECTED_STORAGE_PREFIX"*)
      check 0 "minio_url on expected storage ($EXPECTED_STORAGE_PREFIX…)"
      ;;
    *)
      check 1 "STORAGE ROLLBACK? minio_url does not start with $EXPECTED_STORAGE_PREFIX: $MINIO_URL"
      ;;
  esac
else
  check 1 "Finalize failed: $(echo "$FIN" | head -c 200)"
  cleanup_all
  echo "[FAIL] Aborting — no finalized upload to verify." >&2
  exit 2
fi

# ── 6. VERIFY VIA /files (the upload is listed for this user) ──
echo ""
echo "── 6. LISTED VIA /files ──"
req 6-files GET /files "Bearer $JWT"
if [ "$(status)" = "200" ] && grep -q "$FILE_ID" /tmp/upload_audit_resp.json; then
  check 0 "GET /files lists the uploaded file"
else
  check 1 "GET /files → $(status) (file_id not listed)"
fi

# ── 7. CLEANUP ──
echo ""
echo "── 7. CLEANUP ──"
CLEAN_CODES=$(cleanup_all)
CLEAN_FILE=$(echo "$CLEAN_CODES" | awk '{print $1}')
CLEAN_ACCT=$(echo "$CLEAN_CODES" | awk '{print $2}')
[ "$CLEAN_FILE" = "200" ] && check 0 "Test file deleted (DELETE /files/{id}) → 200" || check 1 "File cleanup → ${CLEAN_FILE:-skipped}"
[ "$CLEAN_ACCT" = "200" ] && check 0 "Test account deleted (DELETE /auth/me) → 200" || check 1 "Account cleanup → ${CLEAN_ACCT:-skipped}"

# ── SUMMARY ──
echo ""
echo "============================================================"
echo "  RESULTS: $PASS [OK]  $FAIL [FAIL]"
echo "============================================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
