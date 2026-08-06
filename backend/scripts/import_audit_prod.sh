#!/bin/bash
# Import-by-URL + cross-path dedup audit — runs against the LIVE production
# deployment. Verifies the import endpoint exactly as users drive it:
#   register A → upload a unique file → IMPORT that file's own public URL →
#   the server must fetch it (through the DNS-pinned SSRF-safe transport),
#   validate it, and dedup it against the already-stored copy: the imported
#   row's minio_url must equal the source's (identical bytes stored ONCE,
#   across upload paths) while the rows stay distinct → the imported file's
#   public embed must resolve → SSRF guard must block a cloud-metadata URL and
#   a localhost hostname (400) → a malformed URL must be rejected (400) →
#   cleanup both files + the throwaway account.
#
# Mirrors dedup_audit_prod.sh / upload_audit_prod.sh conventions:
#   - Paces requests (~2.2s apart) so the global rate limiter (30 req / 10s per
#     IP) doesn't produce false 429 failures.
#   - Auth POSTs (register/presign/finalize/import) go through the VERCEL
#     PROXY ($FE/*) — the same-origin path the browser uses. The file BYTES
#     never touch the proxy: they're PUT straight to the presigned storage URL.
#   - The probe file is a REAL (minimal) WebM — EBML header + varied bytes —
#     with an os.urandom tail so its content hash is UNIQUE per run: dedup can
#     only match WITHIN the run, never against a previous run's leftover rows.
#   - The import URL is the source file's own public object URL, so the import
#     exercises the REAL server-side fetch path (resolve-once + pinned-IP
#     transport + TLS against the original hostname) against production.
#   - Cleans up both files + the throwaway account (also on abort paths), so
#     repeated runs never accumulate test data.
#   - Aborts early (exit 2) if register/presign/finalize fail, since the
#     remaining steps can't pass without a finalized source upload.
# Usage: bash import_audit_prod.sh
#        API_URL=https://torrent-cdn.onrender.com FRONTEND_URL=https://www.torrentcdn.store bash import_audit_prod.sh
# Exit code 0 = all pass, 1 = one or more checks failed, 2 = setup aborted.
set -uo pipefail

# The health GET hits the Render origin directly (the Vercel proxy only rewrites
# /api/* and /auth/*, not /health — same split the other probes use).
API="${API_URL:-https://torrent-cdn.onrender.com}"
FE="${FRONTEND_URL:-https://www.torrentcdn.store}"
PASS=0
FAIL=0

check() {
  if [ "$1" = "0" ]; then
    echo "  ✅  $2"
    sum "- ✅ **$2**"
    PASS=$((PASS+1))
  else
    echo "  ❌  $2"
    sum "- ❌ **$2**"
    FAIL=$((FAIL+1))
  fi
}

echo "============================================================"
echo "  PRODUCTION IMPORT-BY-URL + CROSS-PATH DEDUP AUDIT  —  $FE"
echo "============================================================"

# GitHub Actions step-summary support: mirror the audit into the run's summary
# page so a manual dispatch or scheduled run shows results at a glance. No-op
# when run locally.
SUMMARY_FILE="${GITHUB_STEP_SUMMARY:-}"
sum() { [ -n "$SUMMARY_FILE" ] && printf '%s\n' "$1" >> "$SUMMARY_FILE"; }

if [ -n "$SUMMARY_FILE" ]; then
  sum "## 🔁 Production Import-by-URL + Cross-Path Dedup Audit — $FE"
  sum ""
  if [ -n "${GITHUB_SHA:-}" ]; then
    sum "**Commit**: \`${GITHUB_SHA:0:7}\` · **Run**: [#$GITHUB_RUN_ID](https://github.com/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID)"
    sum ""
  fi
fi

# ── 0. Health (fail fast if prod is unreachable) ──
CODE=$(curl -so /dev/null -w '%{http_code}' --max-time 30 "$API/health" 2>/dev/null || echo "000")
if [ "$CODE" != "200" ]; then
  echo "❌ Health check failed → HTTP $CODE; is production up?" >&2
  [ -n "$SUMMARY_FILE" ] && sum "**🔴 ABORTED** — health check → HTTP $CODE (production unreachable)."
  exit 2
fi
check 0 "Health → 200"

# ── Helpers ──
# req <label> <method> <path> <auth> <json_body> — prints "label method path -> HTTP code"
# and leaves the response body in /tmp/import_audit_resp.json. Auth POSTs
# (register/presign/finalize/import) go through the Vercel proxy.
req() {
  local label="$1" method="$2" path="$3" auth="${4:-}" body="${5:-}"
  local args=(-s -o /tmp/import_audit_resp.json -w '%{http_code}' --max-time 120 -X "$method" "$FE$path")
  [ -n "$auth" ] && args+=(-H "Authorization: $auth")
  [ -n "$body" ] && args+=(-H 'Content-Type: application/json' -d "$body")
  local code
  code=$(curl "${args[@]}" 2>/dev/null || echo "000")
  echo "[$label] $method $path -> HTTP $code"
  echo "    body: $(head -c 300 /tmp/import_audit_resp.json)"
  echo
  echo "$code" > /tmp/import_audit_code.txt
  sleep 2.2
}
status() { cat /tmp/import_audit_code.txt; }

# Cleanup helper — deletes any uploaded files + the throwaway account. Called
# on abort paths too, so a mid-flow failure never leaves test data behind.
# Echoes "fileImp fileSrc acct" to stdout (the caller checks them).
cleanup_all() {
  local fi="skipped" fs="skipped" ac="skipped"
  if [ -n "${FILE_ID_IMP:-}" ] && [ -n "${JWT:-}" ]; then
    fi=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/files/$FILE_ID_IMP" \
      -H "Authorization: Bearer $JWT" 2>/dev/null || echo "000")
  fi
  if [ -n "${FILE_ID_SRC:-}" ] && [ -n "${JWT:-}" ]; then
    fs=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/files/$FILE_ID_SRC" \
      -H "Authorization: Bearer $JWT" 2>/dev/null || echo "000")
  fi
  if [ -n "${JWT:-}" ]; then
    ac=$(curl -so /dev/null -w '%{http_code}' --max-time 60 -X DELETE "$FE/auth/me" \
      -H "Authorization: Bearer $JWT" 2>/dev/null || echo "000")
  fi
  echo "cleanup fileImp=$fi fileSrc=$fs acct=$ac" >&2
  echo "$fi $fs $ac"
}

# post_proxy <path> <json> <auth> — auth POST via the Vercel proxy with backoff
# retries, mirroring dedup_audit_prod.sh. Retries on empty body / 000 (cold
# start dropped the connection), 502/503/504 (Render instance waking), and 429
# (per-IP budgets shared with the other live probes; rejected attempts don't
# consume budget, so a pause + retry succeeds once the competing jobs'
# requests finish).
post_proxy() {
  local path="$1" json="$2" auth="$3" body="" code=""
  for attempt in 1 2 3; do
    code=$(curl -s -m 120 -o /tmp/import_audit_resp.json -w '%{http_code}' \
      -X POST "$FE$path" -H "Content-Type: application/json" \
      -H "Authorization: Bearer $auth" -d "$json" 2>/dev/null || echo "000")
    body=$(cat /tmp/import_audit_resp.json 2>/dev/null || echo "")
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

# ── 1. REGISTER USER ──
echo ""
echo "── 1. REGISTER USER ──"
TS=$(date +%s)
EMAIL="import-audit-${TS}@probe.torrentcdn.test"
PASSW="ProbePass123!"
REG=$(post_proxy "/auth/register" "{\"email\":\"$EMAIL\",\"password\":\"$PASSW\",\"terms_accepted\":true}" "")
JWT=$(echo "$REG" | python3 -c "import json,sys;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)
if [ -n "$JWT" ]; then
  check 0 "Register → JWT received"
else
  check 1 "Register failed (no access_token): $(echo "$REG" | head -c 200)"
  cleanup_all >/dev/null
  echo "❌ Aborting — the remaining steps need a registered account." >&2
  [ -n "$SUMMARY_FILE" ] && sum "**🔴 ABORTED** — registration failed; remaining steps skipped."
  exit 2
fi

# ── 2. SYNTHESIZE A UNIQUE REAL WEBM (passes magic-byte validation) ──
# EBML header at offset 0 (real magic), then varied bytes ≥1KB so the
# compression-ratio check passes, with an os.urandom tail so the content hash
# is unique to this run. finalize AND the import route validate magic bytes,
# so the header must be real.
echo ""
echo "── 2. TEST FILE SYNTHESIS (unique per run) ──"
TEST_FILE=/tmp/import-audit-probe.webm
python3 - <<'EOF'
import os
header = b"\x1a\x45\xdf\xa3"  # EBML magic (offset 0)
body = bytes((i * 7 + 3) % 256 for i in range(2048))  # varied, not trivially compressible
tail = os.urandom(512)  # unique per run → unique content hash
open("/tmp/import-audit-probe.webm", "wb").write(header + body + tail)
EOF
FILE_SIZE=$(stat -c%s "$TEST_FILE")
echo "  synthesized $TEST_FILE (${FILE_SIZE} bytes, EBML header + varied body + random tail)"
[ "$FILE_SIZE" -ge 1024 ] && check 0 "Test WebM ≥1KB (${FILE_SIZE} bytes)" || check 1 "Test file too small (${FILE_SIZE} bytes)"

# ── 3. PRESIGN → DIRECT PUT → FINALIZE (the SOURCE the import will fetch) ──
echo ""
echo "── 3. SOURCE UPLOAD (presign → direct PUT → finalize) ──"
if [ ! -f "$TEST_FILE" ]; then
  echo "❌ Test file missing — aborting." >&2
  cleanup_all >/dev/null
  exit 2
fi
IDEM_SRC="audit-src-${TS}-$(python3 -c 'import uuid;print(uuid.uuid4())')"
req 3a-presign POST /upload/presign "Bearer $JWT" \
  "{\"filename\":\"import-audit-probe.webm\",\"file_size\":$FILE_SIZE,\"content_type\":\"video/webm\",\"idempotency_key\":\"$IDEM_SRC\"}"
UPLOAD_ID_SRC=$(python3 -c "import json;print(json.load(open('/tmp/import_audit_resp.json')).get('upload_id',''))" 2>/dev/null)
PRESIGNED_URL_SRC=$(python3 -c "import json;print(json.load(open('/tmp/import_audit_resp.json')).get('presigned_url',''))" 2>/dev/null)
CT_SRC=$(python3 -c "import json;print(json.load(open('/tmp/import_audit_resp.json')).get('content_type',''))" 2>/dev/null)
if [ "$(status)" = "200" ] && [ -n "$UPLOAD_ID_SRC" ] && [ -n "$PRESIGNED_URL_SRC" ]; then
  check 0 "Source presign → upload_id + presigned URL"
else
  check 1 "Source presign → $(status) (no upload_id/presigned_url)"
  cleanup_all >/dev/null
  echo "❌ Aborting — no presigned URL to PUT to." >&2
  [ -n "$SUMMARY_FILE" ] && sum "**🔴 ABORTED** — source presign failed; remaining steps skipped."
  exit 2
fi

PUT_SRC=$(curl -so /dev/null -w '%{http_code}' --max-time 120 -X PUT "$PRESIGNED_URL_SRC" \
  -H "Content-Type: $CT_SRC" --data-binary @"$TEST_FILE" 2>/dev/null || echo "000")
sleep 2.2
[ "$PUT_SRC" = "200" ] && check 0 "Source PUT to storage → 200" || check 1 "Source PUT to storage → $PUT_SRC"

FIN_SRC=$(post_proxy "/upload/presign/finalize/$UPLOAD_ID_SRC" "{}" "$JWT")
FILE_ID_SRC=$(echo "$FIN_SRC" | python3 -c "import json,sys;print(json.load(sys.stdin).get('file_id',''))" 2>/dev/null)
URL_SRC=$(echo "$FIN_SRC" | python3 -c "import json,sys;print(json.load(sys.stdin).get('minio_url',''))" 2>/dev/null)
if [ -n "$FILE_ID_SRC" ] && [ -n "$URL_SRC" ]; then
  check 0 "Source finalize → file_id + minio_url"
  echo "    source file_id: $FILE_ID_SRC"
else
  check 1 "Source finalize failed: $(echo "$FIN_SRC" | head -c 200)"
  cleanup_all >/dev/null
  echo "❌ Aborting — no finalized source upload to import from." >&2
  [ -n "$SUMMARY_FILE" ] && sum "**🔴 ABORTED** — source finalize failed; remaining steps skipped."
  exit 2
fi

# ── 4. IMPORT THE SOURCE'S OWN PUBLIC URL ──
# The server fetches the source file's public HTTPS object URL — this exercises
# the REAL import fetch path against production: resolve-once DNS + pinned-IP
# transport + TLS validated against the original hostname. Identical bytes →
# content-hash dedup must fire ACROSS upload paths: the imported row points at
# the source's stored object (no second copy).
echo ""
echo "── 4. IMPORT BY URL (server fetches the source's public URL) ──"
IMP=$(post_proxy "/upload/import" "{\"url\":\"$URL_SRC\",\"filename\":\"import-audit-copy.webm\"}" "$JWT")
FILE_ID_IMP=$(echo "$IMP" | python3 -c "import json,sys;print(json.load(sys.stdin).get('file_id',''))" 2>/dev/null)
URL_IMP=$(echo "$IMP" | python3 -c "import json,sys;print(json.load(sys.stdin).get('minio_url',''))" 2>/dev/null)
if [ -n "$FILE_ID_IMP" ] && [ -n "$URL_IMP" ]; then
  check 0 "Import → 200 with file_id + minio_url"
  echo "    imported file_id: $FILE_ID_IMP"
else
  check 1 "Import failed: $(echo "$IMP" | head -c 300)"
  cleanup_all >/dev/null
  echo "❌ Aborting — no finalized import to assert dedup on." >&2
  [ -n "$SUMMARY_FILE" ] && sum "**🔴 ABORTED** — import-by-URL failed; remaining steps skipped."
  exit 2
fi

# ── 5. CROSS-PATH DEDUP ASSERTIONS ──
echo ""
echo "── 5. CROSS-PATH DEDUP (identical bytes store once, across upload paths) ──"
if [ "$URL_IMP" = "$URL_SRC" ]; then
  check 0 "Imported minio_url == source minio_url (points at the SAME stored object)"
else
  check 1 "Imported minio_url differs — dedup did NOT fire across paths"
  echo "    source:  $URL_SRC"
  echo "    imported: $URL_IMP"
fi
if [ -n "$FILE_ID_IMP" ] && [ "$FILE_ID_IMP" != "$FILE_ID_SRC" ]; then
  check 0 "Separate upload rows (file_id_imp ≠ file_id_src)"
else
  check 1 "Rows are NOT distinct (file_id_imp == file_id_src)"
fi

# ── 6. IMPORTED FILE RESOLVES PUBLICLY ──
echo ""
echo "── 6. IMPORTED FILE PUBLIC METADATA ──"
PUB_IMP=$(curl -so /dev/null -w '%{http_code}' --max-time 60 "$FE/files/public/$FILE_ID_IMP" 2>/dev/null || echo "000")
sleep 2.2
[ "$PUB_IMP" = "200" ] && check 0 "Imported file's public embed metadata → 200" || check 1 "Imported file's public metadata → HTTP $PUB_IMP (should be 200)"

# ── 7. SSRF GUARD (private targets must be blocked) ──
echo ""
echo "── 7. SSRF GUARD (private/internal targets blocked) ──"
req 7a-ssrf-metadata POST /upload/import "Bearer $JWT" \
  '{"url":"http://169.254.169.254/latest/meta-data/iam/security-credentials/"}'
[ "$(status)" = "400" ] && check 0 "Cloud-metadata URL (169.254.169.254) → 400" || check 1 "Cloud-metadata URL → $(status) (expected 400)"

req 7b-ssrf-localhost POST /upload/import "Bearer $JWT" '{"url":"http://localhost:8000/admin/stats"}'
[ "$(status)" = "400" ] && check 0 "localhost hostname → 400" || check 1 "localhost hostname → $(status) (expected 400)"

req 7c-ssrf-malformed POST /upload/import "Bearer $JWT" '{"url":"not a url"}'
[ "$(status)" = "400" ] && check 0 "Malformed URL → 400" || check 1 "Malformed URL → $(status) (expected 400)"

# ── 8. CLEANUP ──
echo ""
echo "── 8. CLEANUP ──"
CLEAN_CODES=$(cleanup_all)
CLEAN_FILEIMP=$(echo "$CLEAN_CODES" | awk '{print $1}')
CLEAN_FILESRC=$(echo "$CLEAN_CODES" | awk '{print $2}')
CLEAN_ACCT=$(echo "$CLEAN_CODES" | awk '{print $3}')
# The imported row is deleted first, then the source row — the shared object is
# removed by the refcount-aware delete once BOTH references are gone. Results
# are informational; what matters is that the account is gone.
[ "$CLEAN_ACCT" = "200" ] && check 0 "Account deleted (DELETE /auth/me) → 200" || check 1 "Account cleanup → ${CLEAN_ACCT:-skipped}"

# ── SUMMARY ──
echo ""
echo "============================================================"
echo "  RESULTS: $PASS ✅  $FAIL ❌"
echo "============================================================"
if [ -n "$SUMMARY_FILE" ]; then
  sum ""
  if [ "$FAIL" -eq 0 ]; then
    sum "**Result: $PASS ✅ / $FAIL ❌ — 🟢 PASS**"
    sum "Import-by-URL, cross-path dedup, and the SSRF guard are healthy on production."
  else
    sum "**Result: $PASS ✅ / $FAIL ❌ — 🔴 FAIL**"
    sum "See the job log for the failing checks."
  fi
  sum ""
  sum "<sub>Generated by \`import_audit_prod.sh\` — $(date -u '+%Y-%m-%d %H:%M UTC')</sub>"
fi
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
