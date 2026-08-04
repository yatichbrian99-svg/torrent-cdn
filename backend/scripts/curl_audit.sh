#!/bin/bash
# Full-stack security audit — curls a live API and checks all endpoints.
# Adapts to the app's current behavior:
#   - Paces requests (~2.2s apart) so the global rate limiter (30 req / 10s per IP,
#     /health and /csp-report exempt) doesn't produce false 429 failures.
#   - Registration requires terms_accepted: true.
#   - When the backend runs with APP_ENV=production, email verification is
#     required, so a fresh user's LOGIN is blocked with 403 (the gate working)
#     rather than returning a token. In dev mode login still returns 200.
#   - Cleans up the throwaway test user via DELETE /auth/me.
# Requires: API_URL env var pointing to a running instance.
# Exit code 0 = all pass, non-zero = failure(s).
set -uo pipefail

API="${API_URL:-http://localhost:8000}"
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
echo "  Full-Stack Security Audit  —  $API"
echo "============================================================"
echo ""

# ── 1. HEALTH ──
echo "── 1. HEALTH CHECK ──"
CODE=$(curl -so /dev/null -w '%{http_code}' "$API/health" 2>/dev/null || echo "000")
[ "$CODE" = "200" ] && check 0 "Health endpoint → $CODE" || check 1 "Health endpoint → $CODE"

BODY=$(curl -s "$API/health" 2>/dev/null || echo "")
echo "$BODY" | grep -q '"status":"healthy"' && check 0 "Response contains healthy status" || check 1 "Response missing healthy status"

# ── 2. SECURITY HEADERS ──
echo ""
echo "── 2. SECURITY HEADERS ──"
HEADERS=$(curl -sI "$API/health" 2>/dev/null || echo "")
for h in "content-security-policy" "x-content-type-options" "x-frame-options" \
         "referrer-policy" "x-xss-protection" "permissions-policy" "reporting-endpoints"; do
  echo "$HEADERS" | grep -qi "$h" && check 0 "$h" || check 1 "$h"
done

# ── 3. CSP CONTENT ──
echo ""
echo "── 3. CSP CONTENT ──"
CSP=$(echo "$HEADERS" | grep -i 'content-security-policy' | head -1)
for pat in "'self'" "tracker.openwebtorrent.com" "report-uri" "report-to" "base-uri 'self'"; do
  echo "$CSP" | grep -q "$pat" && check 0 "CSP contains: $pat" || check 1 "CSP missing: $pat"
done
echo "$CSP" | grep -q "unpkg.com" && check 1 "CSP should NOT allow unpkg.com" || check 0 "CSP blocks unpkg.com"
echo "$CSP" | grep -q "script-src 'self' 'unsafe-inline'" && check 1 "CSP script-src should not allow 'unsafe-inline'" || check 0 "CSP script-src has no 'unsafe-inline'"

# ── 4. AUTH ENFORCEMENT ──
echo ""
echo "── 4. AUTH ENFORCEMENT ──"
for EP in "/auth/me" "/files" "/billing/status"; do
  sleep 2.2
  CODE=$(curl -so /dev/null -w '%{http_code}' "$API$EP" 2>/dev/null || echo "000")
  [ "$CODE" = "401" ] && check 0 "$EP → $CODE" || check 1 "$EP → $CODE"
done

# ── 5. REGISTER & LOGIN ──
echo ""
echo "── 5. REGISTRATION & LOGIN ──"
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

  # Fresh user is unverified: in production mode login must be BLOCKED (403 gate);
  # in dev mode (verification disabled) it returns a token (200). Both are correct.
  sleep 2.2
  LOGIN_CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"testpass123\"}" 2>/dev/null || echo "000")
  if [ "$LOGIN_CODE" = "403" ]; then
    check 0 "Login pre-verification → 403 (verification gate enforced)"
  elif [ "$LOGIN_CODE" = "200" ]; then
    check 0 "Login → 200 (verification disabled in this env)"
  else
    check 1 "Login pre-verification → $LOGIN_CODE (expected 403 gate or 200)"
  fi

  # Duplicate registration should return 400
  sleep 2.2
  DUPE_CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/auth/register" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"testpass123\",\"terms_accepted\":true}" 2>/dev/null || echo "000")
  # Note: only the status is checked (no second unpaced body request) so the
  # the global limiter can't turn this into a false 429.
  [ "$DUPE_CODE" = "400" ] && check 0 "Duplicate registration → $DUPE_CODE" || check 1 "Duplicate registration → $DUPE_CODE"

  # Cleanup the throwaway user (also verifies DELETE /auth/me)
  sleep 2.2
  DEL_CODE=$(curl -so /dev/null -w '%{http_code}' -X DELETE "$API/auth/me" \
    -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
  [ "$DEL_CODE" = "200" ] && check 0 "Test user cleaned up (DELETE /auth/me) → $DEL_CODE" || check 1 "Cleanup DELETE /auth/me → $DEL_CODE"
else
  check 1 "Registration — failed: $(echo "$REG" | head -c 100)"
fi

# ── 6. SQL INJECTION ──
echo ""
echo "── 6. SQL INJECTION ──"
for PAYLOAD in "' OR '1'='1" "'; DROP TABLE users; --" "' UNION SELECT * FROM users; --"; do
  sleep 2.2
  CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$PAYLOAD\",\"password\":\"x\"}" 2>/dev/null || echo "000")
  [ "$CODE" != "200" ] && check 0 "SQLi blocked: ${PAYLOAD:0:15} → $CODE" || check 1 "SQLi VULN: $PAYLOAD → $CODE"
done

# ── 7. INPUT VALIDATION ──
echo ""
echo "── 7. INPUT VALIDATION ──"
for payload in '{"email":"bad","password":"short","terms_accepted":true}' '{"email":"x@y.co","password":"1234567","terms_accepted":true}'; do
  sleep 2.2
  CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/auth/register" \
    -H "Content-Type: application/json" -d "$payload" 2>/dev/null || echo "000")
  [ "$CODE" = "400" ] && check 0 "Invalid input rejected → $CODE" || check 1 "Invalid input → $CODE"
done

# ── 8. ANALYTICS XSS ──
echo ""
echo "── 8. ANALYTICS XSS ──"
sleep 2.2
CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/analytics/event" \
  -H "Content-Type: application/json" \
  -d '{"file_id":"00000000-0000-0000-0000-000000000000","event_type":"<script>","bytes_p2p":0,"bytes_origin":0}' 2>/dev/null || echo "000")
[ "$CODE" = "400" ] && check 0 "XSS event_type blocked → $CODE" || check 1 "XSS event_type → $CODE"

# ── 9. CORS ──
echo ""
echo "── 9. CORS ──"
sleep 2.2
ACAO=$(curl -s -X OPTIONS "$API/health" \
  -H "Origin: http://evil.com" \
  -H "Access-Control-Request-Method: GET" \
  -D - -o /dev/null 2>/dev/null | grep -i 'access-control-allow-origin' | head -1 || echo "")
[ -z "$ACAO" ] && check 0 "CORS blocks evil.com (no allow-origin header)" || check 1 "CORS allows evil.com: $ACAO"

# ── 10. HTTP METHODS ──
echo ""
echo "── 10. HTTP METHOD RESTRICTIONS ──"
for M in PUT DELETE PATCH TRACE CONNECT; do
  sleep 2.2
  CODE=$(curl -so /dev/null -w '%{http_code}' -X "$M" "$API/health" 2>/dev/null || echo "000")
  [ "$CODE" != "200" ] && check 0 "$M blocked → $CODE" || check 1 "$M VULNERABLE → 200"
done

# ── SUMMARY ──
echo ""
echo "============================================================"
echo "  RESULTS: $PASS ✅  $FAIL ❌"
echo "============================================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
