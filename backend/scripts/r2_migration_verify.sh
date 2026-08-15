#!/bin/bash
# R2 migration acceptance audit — runs against LIVE production and proves the
# Supabase → Cloudflare R2 storage switch end to end:
#   register A → presign a 60MB WebM → direct PUT → finalize →
#   assert minio_url points at the R2 public URL (not Supabase) →
#   object serves publicly → register B → upload IDENTICAL bytes →
#   B's minio_url == A's (dedup fired, ONE copy stored) →
#   DELETE A → shared object survives (refcount) →
#   DELETE B (last reference) → object removed →
#   both throwaway accounts cleaned up.
#
# The 60MB upload is the point: Supabase free tier hard-capped files at 50MB
# (a 51MB PUT → HTTP 413 EntityTooLarge). If this script passes, that blocker
# is dead.
#
# Mirrors dedup_audit_prod.sh conventions: paces requests (~2.2s), auth POSTs
# through the Vercel proxy ($FE/*), the file BYTES go straight to the presigned
# storage URL (never the proxy), real minimal WebM with a unique random tail,
# non-200-accepts-any for "object gone" (provider-agnostic), cleanup on abort
# paths. NOTE: public object GETs send a browser User-Agent — Cloudflare bot
# protection (error 1010) blocks bare clients with no UA (real browsers are
# unaffected; this is exactly what the video player does).
# Usage: bash r2_migration_verify.sh
# Exit code 0 = all pass, 1 = checks failed, 2 = setup aborted.
set -uo pipefail

API="${API_URL:-https://torrent-cdn.onrender.com}"
FE="${FRONTEND_URL:-https://www.torrentcdn.store}"
PUBLIC_BASE="${R2_PUBLIC_URL:-https://pub-db587f977d274a458f30d14fa2ddfb9e.r2.dev}"
BROWSER_UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SIZE_MB=60
PASS=0
FAIL=0

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
echo "  R2 MIGRATION ACCEPTANCE AUDIT (${SIZE_MB}MB upload)  —  $FE"
echo "============================================================"

# ── 0. Health (fail fast) ──
CODE=$(curl -so /dev/null -w '%{http_code}' --max-time 30 "$API/health" 2>/dev/null || echo "000")
if [ "$CODE" != "200" ]; then
  echo "[FAIL] Health check failed → HTTP $CODE; is production up?" >&2
  exit 2
fi
check 0 "Health → 200"

# ── Helpers ──
post_proxy() {  # path json auth -> body
  local path="$1" json="$2" auth="$3" body="" code=""
  for attempt in 1 2 3; do
    code=$(curl -s -m 90 -o /tmp/r2m_resp.json -w '%{http_code}' \
      -X POST "$FE$path" -H "Content-Type: application/json" \
      -H "Authorization: Bearer $auth" -d "$json" 2>/dev/null || echo "000")
    body=$(cat /tmp/r2m_resp.json 2>/dev/null || echo "")
    case "$code" in
      000|429|502|503|504)
        [ "$attempt" = "3" ] && break
        echo "  [retry] $path -> HTTP $code, retrying in $((attempt * 20))s…" >&2
        sleep $((attempt * 20))
        ;;
      *) break ;;
    esac
  done
  echo "$body"
}

cleanup_all() {
  if [ -n "${FILE_ID_A:-}" ] && [ -n "${JWT_A:-}" ]; then
    curl -so /dev/null -w '' --max-time 60 -X DELETE "$FE/files/$FILE_ID_A" -H "Authorization: Bearer $JWT_A" 2>/dev/null || true
  fi
  if [ -n "${FILE_ID_B:-}" ] && [ -n "${JWT_B:-}" ]; then
    curl -so /dev/null -w '' --max-time 60 -X DELETE "$FE/files/$FILE_ID_B" -H "Authorization: Bearer $JWT_B" 2>/dev/null || true
  fi
  if [ -n "${JWT_A:-}" ]; then
    curl -so /dev/null -w '' --max-time 60 -X DELETE "$FE/auth/me" -H "Authorization: Bearer $JWT_A" 2>/dev/null || true
  fi
  if [ -n "${JWT_B:-}" ]; then
    curl -so /dev/null -w '' --max-time 60 -X DELETE "$FE/auth/me" -H "Authorization: Bearer $JWT_B" 2>/dev/null || true
  fi
  echo "cleanup done" >&2
}

TS=$(date +%s)

# ── 1. REGISTER USER A ──
echo ""
echo "── 1. REGISTER USER A ──"
REG=$(post_proxy "/auth/register" "{\"email\":\"r2m-a-${TS}@probe.torrentcdn.test\",\"password\":\"ProbePass123!\",\"terms_accepted\":true}" "")
JWT_A=$(echo "$REG" | python3 -c "import json,sys;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
if [ -n "$JWT_A" ]; then
  check 0 "Register A → JWT received"
else
  check 1 "Register A failed: $(echo "$REG" | head -c 200)"
  echo "[FAIL] Aborting — no account A." >&2
  exit 2
fi

# ── 2. SYNTHESIZE A 60MB REAL WEBM (unique per run) ──
echo ""
echo "── 2. SYNTHESIZE ${SIZE_MB}MB TEST WEBM ──"
TEST_FILE=/tmp/r2m-probe-${TS}.webm
python3 - <<EOF
import os
header = b"\x1a\x45\xdf\xa3"  # EBML magic (offset 0) — passes magic-byte validation
target = ${SIZE_MB} * 1024 * 1024
with open("$TEST_FILE", "wb") as f:
    f.write(header)
    written = len(header)
    while written < target:
        chunk = os.urandom(min(4 * 1024 * 1024, target - written))
        f.write(chunk)
        written += len(chunk)
EOF
FILE_SIZE=$(stat -c%s "$TEST_FILE" 2>/dev/null || echo 0)
echo "  synthesized ${SIZE_MB}MB WebM (${FILE_SIZE} bytes)"
[ "$FILE_SIZE" -ge $((SIZE_MB * 1024 * 1024)) ] && check 0 "Test WebM is ${SIZE_MB}MB (${FILE_SIZE} bytes)" || check 1 "Test file wrong size (${FILE_SIZE})"

# ── 3. USER A: PRESIGN → DIRECT PUT (THE 50MB-CAP PROOF) → FINALIZE ──
echo ""
echo "── 3. USER A: ${SIZE_MB}MB UPLOAD (was a hard 413 on Supabase) ──"
IDEM_A="r2m-a-${TS}-$(python3 -c 'import uuid;print(uuid.uuid4())')"
PRE=$(post_proxy "/upload/presign" "{\"filename\":\"r2m-probe.webm\",\"file_size\":$FILE_SIZE,\"content_type\":\"video/webm\",\"idempotency_key\":\"$IDEM_A\"}" "$JWT_A")
UPLOAD_ID_A=$(echo "$PRE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('upload_id',''))" 2>/dev/null)
PRESIGNED_URL_A=$(echo "$PRE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('presigned_url',''))" 2>/dev/null)
CT_A=$(echo "$PRE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('content_type',''))" 2>/dev/null)
if [ -n "$UPLOAD_ID_A" ] && [ -n "$PRESIGNED_URL_A" ]; then
  check 0 "A presign → upload_id + presigned URL"
else
  check 1 "A presign failed: $(echo "$PRE" | head -c 200)"
  cleanup_all >/dev/null
  exit 2
fi
sleep 2.2

echo "  PUTting ${SIZE_MB}MB straight to storage (max-time 300, 1 retry)…"
# Retrying with the SAME presigned URL is safe: presigned PUT URLs are reusable
# within their 15-min TTL (PRESIGN_URL_TTL). If that TTL ever shrinks below the
# retry window, this retry must re-presign instead.
PUT_A=$(curl -so /dev/null -w '%{http_code}' --max-time 300 -X PUT "$PRESIGNED_URL_A" \
  -H "Content-Type: $CT_A" --data-binary @"$TEST_FILE" 2>/dev/null || echo "000")
if [ "$PUT_A" != "200" ]; then
  echo "  [retry] A PUT -> HTTP $PUT_A, retrying once…" >&2
  sleep 5
  PUT_A=$(curl -so /dev/null -w '%{http_code}' --max-time 300 -X PUT "$PRESIGNED_URL_A" \
    -H "Content-Type: $CT_A" --data-binary @"$TEST_FILE" 2>/dev/null || echo "000")
fi
sleep 2.2
if [ "$PUT_A" = "200" ]; then
  check 0 "${SIZE_MB}MB direct PUT → 200 (THE 50MB CAP IS DEAD)"
else
  check 1 "${SIZE_MB}MB direct PUT → HTTP $PUT_A (should be 200!)"
fi

FIN_A=$(post_proxy "/upload/presign/finalize/$UPLOAD_ID_A" "{}" "$JWT_A")
FILE_ID_A=$(echo "$FIN_A" | python3 -c "import json,sys;print(json.load(sys.stdin).get('file_id',''))" 2>/dev/null)
URL_A=$(echo "$FIN_A" | python3 -c "import json,sys;print(json.load(sys.stdin).get('minio_url',''))" 2>/dev/null)
if [ -n "$FILE_ID_A" ] && [ -n "$URL_A" ]; then
  check 0 "A finalize → file_id + minio_url"
else
  check 1 "A finalize failed: $(echo "$FIN_A" | head -c 200)"
  cleanup_all >/dev/null
  exit 2
fi

# ── 4. THE SWITCH ASSERTION: URL IS R2, NOT SUPABASE ──
echo ""
echo "── 4. STORAGE PROVIDER ASSERTION ──"
case "$URL_A" in
  "$PUBLIC_BASE"/*) check 0 "minio_url points at R2 ($PUBLIC_BASE/…)" ;;
  *) check 1 "minio_url does NOT point at R2: $URL_A" ;;
esac
if echo "$URL_A" | grep -q 'supabase'; then
  check 1 "minio_url still references Supabase — switch failed!"
else
  check 0 "No Supabase reference in minio_url"
fi

# ── 5. PUBLIC SERVING (browser UA — what the video player does) ──
echo ""
echo "── 5. PUBLIC OBJECT SERVING ──"
PUB_A=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -A "$BROWSER_UA" "$URL_A" 2>/dev/null || echo "000")
sleep 2.2
[ "$PUB_A" = "200" ] && check 0 "Public GET of the ${SIZE_MB}MB object → 200" || check 1 "Public GET → HTTP $PUB_A (should be 200)"

# ── 6. REGISTER USER B ──
echo ""
echo "── 6. REGISTER USER B ──"
REG=$(post_proxy "/auth/register" "{\"email\":\"r2m-b-${TS}@probe.torrentcdn.test\",\"password\":\"ProbePass123!\",\"terms_accepted\":true}" "")
JWT_B=$(echo "$REG" | python3 -c "import json,sys;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
if [ -n "$JWT_B" ]; then
  check 0 "Register B → JWT received"
else
  check 1 "Register B failed: $(echo "$REG" | head -c 200)"
  cleanup_all >/dev/null
  exit 2
fi

# ── 7. USER B: UPLOAD IDENTICAL BYTES → DEDUP ──
echo ""
echo "── 7. USER B: IDENTICAL BYTES → DEDUP ──"
IDEM_B="r2m-b-${TS}-$(python3 -c 'import uuid;print(uuid.uuid4())')"
PRE=$(post_proxy "/upload/presign" "{\"filename\":\"r2m-probe.webm\",\"file_size\":$FILE_SIZE,\"content_type\":\"video/webm\",\"idempotency_key\":\"$IDEM_B\"}" "$JWT_B")
UPLOAD_ID_B=$(echo "$PRE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('upload_id',''))" 2>/dev/null)
PRESIGNED_URL_B=$(echo "$PRE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('presigned_url',''))" 2>/dev/null)
CT_B=$(echo "$PRE" | python3 -c "import json,sys;print(json.load(sys.stdin).get('content_type',''))" 2>/dev/null)
if [ -n "$UPLOAD_ID_B" ] && [ -n "$PRESIGNED_URL_B" ]; then
  check 0 "B presign → upload_id + presigned URL"
else
  check 1 "B presign failed: $(echo "$PRE" | head -c 200)"
  cleanup_all >/dev/null
  exit 2
fi
sleep 2.2

PUT_B=$(curl -so /dev/null -w '%{http_code}' --max-time 300 -X PUT "$PRESIGNED_URL_B" \
  -H "Content-Type: $CT_B" --data-binary @"$TEST_FILE" 2>/dev/null || echo "000")
if [ "$PUT_B" != "200" ]; then
  echo "  [retry] B PUT -> HTTP $PUT_B, retrying once…" >&2
  sleep 5
  PUT_B=$(curl -so /dev/null -w '%{http_code}' --max-time 300 -X PUT "$PRESIGNED_URL_B" \
    -H "Content-Type: $CT_B" --data-binary @"$TEST_FILE" 2>/dev/null || echo "000")
fi
sleep 2.2
[ "$PUT_B" = "200" ] && check 0 "B PUT to storage → 200" || check 1 "B PUT to storage → $PUT_B"

FIN_B=$(post_proxy "/upload/presign/finalize/$UPLOAD_ID_B" "{}" "$JWT_B")
FILE_ID_B=$(echo "$FIN_B" | python3 -c "import json,sys;print(json.load(sys.stdin).get('file_id',''))" 2>/dev/null)
URL_B=$(echo "$FIN_B" | python3 -c "import json,sys;print(json.load(sys.stdin).get('minio_url',''))" 2>/dev/null)
if [ -n "$FILE_ID_B" ] && [ -n "$URL_B" ]; then
  check 0 "B finalize → file_id + minio_url"
else
  check 1 "B finalize failed: $(echo "$FIN_B" | head -c 200)"
  cleanup_all >/dev/null
  exit 2
fi

# ── 8. DEDUP ASSERTIONS ──
echo ""
echo "── 8. DEDUP ──"
[ "$URL_B" = "$URL_A" ] && check 0 "B's minio_url == A's (ONE copy stored on R2)" || check 1 "Dedup did NOT fire — URLs differ"
[ "$FILE_ID_B" != "$FILE_ID_A" ] && [ -n "$FILE_ID_B" ] && check 0 "Separate upload rows (file_id_B ≠ file_id_A)" || check 1 "Rows not distinct"

# ── 9. REFCOUNT: DELETE A → OBJECT SURVIVES ──
echo ""
echo "── 9. REFCOUNT (A deletes, B still references) ──"
DEL_A=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/files/$FILE_ID_A" -H "Authorization: Bearer $JWT_A" 2>/dev/null || echo "000")
sleep 2.2
[ "$DEL_A" = "200" ] && check 0 "DELETE A → 200" || check 1 "DELETE A → $DEL_A"
OBJ_A=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -A "$BROWSER_UA" "$URL_A" 2>/dev/null || echo "000")
sleep 2.2
[ "$OBJ_A" = "200" ] && check 0 "Shared object survives after A deleted (refcount kept it)" || check 1 "Shared object → HTTP $OBJ_A after A deleted (should be 200!)"

# ── 10. LAST-REFERENCE DELETE → OBJECT REMOVED ──
echo ""
echo "── 10. LAST-REFERENCE DELETE ──"
DEL_B=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/files/$FILE_ID_B" -H "Authorization: Bearer $JWT_B" 2>/dev/null || echo "000")
sleep 2.2
[ "$DEL_B" = "200" ] && check 0 "DELETE B (last reference) → 200" || check 1 "DELETE B → $DEL_B"
GONE=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -A "$BROWSER_UA" "$URL_A?audit=$TS" 2>/dev/null || echo "000")
sleep 2.2
if [ "$GONE" != "200" ]; then
  check 0 "Object removed after last reference deleted (HTTP $GONE)"
else
  check 1 "Object STILL served after last reference deleted (HTTP 200)!"
fi

# ── 11. CLEANUP (files already deleted in steps 9-10; delete the accounts) ──
echo ""
echo "── 11. CLEANUP ──"
ACCT_A=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/auth/me" -H "Authorization: Bearer $JWT_A" 2>/dev/null || echo "000")
ACCT_B=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/auth/me" -H "Authorization: Bearer $JWT_B" 2>/dev/null || echo "000")
[ "$ACCT_A" = "200" ] && check 0 "Account A deleted → 200" || check 1 "Account A cleanup → ${ACCT_A:-skipped}"
[ "$ACCT_B" = "200" ] && check 0 "Account B deleted → 200" || check 1 "Account B cleanup → ${ACCT_B:-skipped}"
rm -f "$TEST_FILE" /tmp/r2m_resp.json

echo ""
echo "============================================================"
echo "  RESULTS: $PASS [OK]  $FAIL [FAIL]"
echo "============================================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
