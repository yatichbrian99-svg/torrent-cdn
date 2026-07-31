#!/bin/bash
# Full-stack security audit — runs against the LIVE production deployment.
# Adapts the CI/localhost curl_audit.sh for production realities:
#   - Paces requests (~2.2s apart) so the global rate limiter (5 req / 10s per IP)
#     doesn't produce false 429 failures.
#   - Production requires email verification, so a fresh user's LOGIN must be
#     BLOCKED with 403 (the verification gate working) rather than returning a token.
#   - Cleans up the throwaway test user via DELETE /auth/me (also proves the
#     self-deletion endpoint works in production).
#   - Covers the FRONTEND too (nonce CSP, script nonces, SRI, proxy auth) so it
#     truly is a full-stack audit.
# Usage: API_URL=https://torrent-cdn.onrender.com FRONTEND_URL=https://www.torrentcdn.store bash curl_audit_prod.sh
# Exit code 0 = all pass, non-zero = failure(s).
set -uo pipefail

API="${API_URL:-https://torrent-cdn.onrender.com}"
FE="${FRONTEND_URL:-https://www.torrentcdn.store}"
PASS=0
FAIL=0

check() {
  if [ "$1" = "0" ]; then
    echo "  ✅  $2"
    PASS=$((PASS+1))
  else
    echo "  ❌  $2"
    FAIL=$((FAIL+1))
  fi
}

echo "============================================================"
echo "  PRODUCTION Full-Stack Security Audit  —  $API"
echo "============================================================"

# ── 1. HEALTH ──
echo ""
echo "── 1. HEALTH CHECK ──"
CODE=$(curl -so /dev/null -w '%{http_code}' "$API/health" 2>/dev/null || echo "000")
[ "$CODE" = "200" ] && check 0 "Health endpoint → $CODE" || check 1 "Health endpoint → $CODE"

BODY=$(curl -s "$API/health" 2>/dev/null || echo "")
echo "$BODY" | grep -q '"status":"healthy"' && check 0 "Response contains healthy status" || check 1 "Response missing healthy status"

# ── 2. SECURITY HEADERS (HSTS added — production only) ──
echo ""
echo "── 2. SECURITY HEADERS ──"
HEADERS=$(curl -sI "$API/health" 2>/dev/null || echo "")
for h in "content-security-policy" "x-content-type-options" "x-frame-options" \
         "referrer-policy" "x-xss-protection" "permissions-policy" \
         "reporting-endpoints" "strict-transport-security"; do
  echo "$HEADERS" | grep -qi "$h" && check 0 "$h" || check 1 "$h (MISSING)"
done

# ── 3. CSP CONTENT ──
echo ""
echo "── 3. CSP CONTENT ──"
CSP=$(echo "$HEADERS" | grep -i 'content-security-policy' | head -1)
for pat in "'self'" "tracker.openwebtorrent.com" "report-uri" "report-to" "base-uri 'self'"; do
  echo "$CSP" | grep -q "$pat" && check 0 "CSP contains: $pat" || check 1 "CSP missing: $pat"
done
echo "$CSP" | grep -q "unpkg.com" && check 1 "CSP should NOT allow unpkg.com" || check 0 "CSP blocks unpkg.com"
echo "$CSP" | grep -q "script-src 'self' 'unsafe-inline'" && check 1 "CSP script-src has 'unsafe-inline'" || check 0 "CSP script-src has no 'unsafe-inline'"
echo "$CSP" | grep -q "localhost:9000" && check 1 "CSP leaks localhost:9000 (dev origin in prod!)" || check 0 "CSP has no localhost:9000 (prod media/connect sources)"

# ── 4. HOST HEADER PROTECTION (TrustedHostMiddleware) ──
echo ""
echo "── 4. HOST HEADER PROTECTION ──"
sleep 2.2
HCODE=$(curl -so /dev/null -w '%{http_code}' -H "Host: evil.com" "$API/health" 2>/dev/null || echo "000")
[ "$HCODE" != "200" ] && check 0 "Host: evil.com blocked → $HCODE" || check 1 "Host: evil.com accepted → 200!"

# ── 5. AUTH ENFORCEMENT ──
echo ""
echo "── 5. AUTH ENFORCEMENT ──"
for EP in "/auth/me" "/files" "/billing/status"; do
  sleep 2.2
  CODE=$(curl -so /dev/null -w '%{http_code}' "$API$EP" 2>/dev/null || echo "000")
  [ "$CODE" = "401" ] && check 0 "$EP → $CODE" || check 1 "$EP → $CODE"
done

# ── 6. REGISTER & LOGIN (email-verification aware) ──
echo ""
echo "── 6. REGISTRATION & LOGIN ──"
sleep 2.2
TS=$(date +%s)
EMAIL="ci-audit-${TS}@example.com"
REG=$(curl -s -X POST "$API/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"testpass123\",\"terms_accepted\":true}" 2>/dev/null || echo "")

TOKEN=$(echo "$REG" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
if [ -n "$TOKEN" ]; then
  check 0 "Registration → token received"

  sleep 2.2
  ME=$(curl -s "$API/auth/me" -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "")
  echo "$ME" | grep -q "$EMAIL" && check 0 "Auth/me → correct user" || check 1 "Auth/me failed: $(echo "$ME" | head -c 120)"

  sleep 2.2
  BILL=$(curl -s "$API/billing/status" -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "")
  echo "$BILL" | grep -q '"plan":"free"' && check 0 "Billing → free plan" || check 1 "Billing: $(echo "$BILL" | head -c 120)"

  # Production requires verified email → login MUST be blocked (403), not a token.
  sleep 2.2
  LOGIN_CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"testpass123\"}" 2>/dev/null || echo "000")
  if [ "$LOGIN_CODE" = "403" ]; then
    check 0 "Login pre-verification → 403 (gate enforced)"
  elif [ "$LOGIN_CODE" = "200" ]; then
    check 1 "Login pre-verification → 200 (gate NOT enforced!)"
  else
    check 1 "Login pre-verification → $LOGIN_CODE (expected 403)"
  fi

  # Duplicate registration should return 400
  sleep 2.2
  DUPE_CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/auth/register" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"testpass123\",\"terms_accepted\":true}" 2>/dev/null || echo "000")
  [ "$DUPE_CODE" = "400" ] && check 0 "Duplicate registration → $DUPE_CODE" || check 1 "Duplicate registration → $DUPE_CODE"

  # Cleanup the throwaway user (also verifies DELETE /auth/me in production)
  sleep 2.2
  DEL_CODE=$(curl -so /dev/null -w '%{http_code}' -X DELETE "$API/auth/me" \
    -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
  [ "$DEL_CODE" = "200" ] && check 0 "Test user cleaned up (DELETE /auth/me) → $DEL_CODE" || check 1 "Cleanup DELETE /auth/me → $DEL_CODE"
else
  check 1 "Registration failed: $(echo "$REG" | head -c 150)"
fi

# ── 7. SQL INJECTION ──
echo ""
echo "── 7. SQL INJECTION ──"
for PAYLOAD in "' OR '1'='1" "'; DROP TABLE users; --" "' UNION SELECT * FROM users; --"; do
  sleep 2.2
  CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$PAYLOAD\",\"password\":\"x\"}" 2>/dev/null || echo "000")
  [ "$CODE" != "200" ] && check 0 "SQLi blocked: ${PAYLOAD:0:15} → $CODE" || check 1 "SQLi VULN: $PAYLOAD → $CODE"
done

# ── 8. INPUT VALIDATION ──
echo ""
echo "── 8. INPUT VALIDATION ──"
for payload in '{"email":"bad","password":"short","terms_accepted":true}' '{"email":"x@y.co","password":"1234567","terms_accepted":true}'; do
  sleep 2.2
  CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/auth/register" \
    -H "Content-Type: application/json" -d "$payload" 2>/dev/null || echo "000")
  [ "$CODE" = "400" ] && check 0 "Invalid input rejected → $CODE" || check 1 "Invalid input → $CODE"
done

# ── 9. ANALYTICS XSS ──
echo ""
echo "── 9. ANALYTICS XSS ──"
sleep 2.2
CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/analytics/event" \
  -H "Content-Type: application/json" \
  -d '{"file_id":"00000000-0000-0000-0000-000000000000","event_type":"<script>","bytes_p2p":0,"bytes_origin":0}' 2>/dev/null || echo "000")
[ "$CODE" = "400" ] && check 0 "XSS event_type blocked → $CODE" || check 1 "XSS event_type → $CODE"

# ── 10. CORS ──
echo ""
echo "── 10. CORS ──"
sleep 2.2
ACAO=$(curl -s -X OPTIONS "$API/health" \
  -H "Origin: http://evil.com" \
  -H "Access-Control-Request-Method: GET" \
  -D - -o /dev/null 2>/dev/null | grep -i 'access-control-allow-origin' | head -1 || echo "")
[ -z "$ACAO" ] && check 0 "CORS blocks evil.com (no allow-origin header)" || check 1 "CORS allows evil.com: $ACAO"

# ── 11. HTTP METHODS ──
echo ""
echo "── 11. HTTP METHOD RESTRICTIONS ──"
for M in PUT DELETE PATCH TRACE CONNECT; do
  sleep 2.2
  CODE=$(curl -so /dev/null -w '%{http_code}' -X "$M" "$API/health" 2>/dev/null || echo "000")
  # Any non-200 is blocked (405 at app level, 400 at the Render proxy for CONNECT)
  [ "$CODE" != "200" ] && check 0 "$M blocked → $CODE" || check 1 "$M VULNERABLE → 200"
done

# ── 12. FRONTEND SECURITY ──
echo ""
echo "── 12. FRONTEND ($FE) ──"
sleep 2.2
FE_HEADERS=$(curl -s -D - -o /dev/null "$FE/" 2>/dev/null || echo "")
echo "$FE_HEADERS" | grep -qi 'content-security-policy' && check 0 "Frontend CSP header present" || check 1 "Frontend CSP header MISSING"
echo "$FE_HEADERS" | grep -qi 'strict-transport-security' && check 0 "Frontend HSTS" || check 1 "Frontend HSTS MISSING"
FECSP=$(echo "$FE_HEADERS" | grep -i 'content-security-policy' | head -1)
echo "$FECSP" | grep -q 'nonce-' && check 0 "Frontend CSP uses nonces" || check 1 "Frontend CSP has no nonce"
# Scope to the script-src directive only — style-src legitimately uses 'unsafe-inline'
FESCRIPT_SRC=$(echo "$FECSP" | grep -o 'script-src[^;]*')
echo "$FESCRIPT_SRC" | grep -q "'unsafe-inline'" && check 1 "Frontend CSP script-src has 'unsafe-inline'" || check 0 "Frontend CSP script-src has no 'unsafe-inline'"

echo "$FE_HEADERS" | grep -qi 'server' && FE_SRV=$(echo "$FE_HEADERS" | grep -i '^server:' | head -1 | tr -d '\r') || FE_SRV="(no server header)"
check 0 "Frontend server: $FE_SRV (informational)"

FE_HTML=$(curl -s "$FE/" 2>/dev/null || echo "")
TOTAL=$(echo "$FE_HTML" | grep -o '<script' | wc -l)
NONCED=$(echo "$FE_HTML" | grep -o 'nonce="[^"]*"' | wc -l)
if [ "$TOTAL" -gt 0 ] && [ "$NONCED" -ge $((TOTAL - 1)) ]; then
  check 0 "Script nonce coverage: $NONCED/$TOTAL"
else
  check 1 "Script nonce coverage: $NONCED/$TOTAL (expected >= $((TOTAL - 1)))"
fi

# SRI is applied DYNAMICALLY by sdk.js (script.integrity = 'sha384-...' on the
# self-hosted /vendor/webtorrent.min.js) — not statically in HTML, so verify sdk.js
SDK=$(curl -s "$FE/sdk.js" 2>/dev/null || echo "")
echo "$SDK" | grep -q 'webtorrent.min.js' && check 0 "sdk.js loads self-hosted webtorrent.min.js" || check 1 "sdk.js does not reference webtorrent.min.js"
echo "$SDK" | grep -q 'script.integrity' && check 0 "sdk.js applies SRI integrity to vendor script" || check 1 "sdk.js SRI integrity MISSING"

for EP in "/api/files" "/api/auth/me" "/api/billing/status"; do
  sleep 2.2
  CODE=$(curl -so /dev/null -w '%{http_code}' "$FE$EP" 2>/dev/null || echo "000")
  [ "$CODE" = "401" ] && check 0 "Proxy $EP → $CODE" || check 1 "Proxy $EP → $CODE"
done

sleep 2.2
CODE=$(curl -so /dev/null -w '%{http_code}' "$FE/sdk.js" 2>/dev/null || echo "000")
[ "$CODE" = "200" ] && check 0 "sdk.js served → $CODE" || check 1 "sdk.js → $CODE"

# ── SUMMARY ──
echo ""
echo "============================================================"
echo "  RESULTS: $PASS ✅  $FAIL ❌"
echo "============================================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
