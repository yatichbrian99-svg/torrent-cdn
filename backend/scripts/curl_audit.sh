#!/bin/bash
# Full-stack security audit — curls a live API and checks all endpoints.
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
for pat in "'self'" "unpkg.com" "tracker.openwebtorrent.com" "report-uri" "report-to" "base-uri 'self'"; do
  echo "$CSP" | grep -q "$pat" && check 0 "CSP contains: $pat" || check 1 "CSP missing: $pat"
done

# ── 4. AUTH ENFORCEMENT ──
echo ""
echo "── 4. AUTH ENFORCEMENT ──"
for EP in "/auth/me" "/files" "/billing/status"; do
  CODE=$(curl -so /dev/null -w '%{http_code}' "$API$EP" 2>/dev/null || echo "000")
  [ "$CODE" = "401" ] && check 0 "$EP → $CODE" || check 1 "$EP → $CODE"
done

# ── 5. REGISTER & LOGIN ──
echo ""
echo "── 5. REGISTRATION & LOGIN ──"
TS=$(date +%s)
EMAIL="ci-audit-${TS}@example.com"
REG=$(curl -s -X POST "$API/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"testpass123\"}" 2>/dev/null || echo "")

TOKEN=$(echo "$REG" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
if [ -n "$TOKEN" ]; then
  check 0 "Registration → token received"
  LOGIN=$(curl -s -X POST "$API/auth/login" -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"testpass123\"}" 2>/dev/null || echo "")
  echo "$LOGIN" | grep -q "access_token" && check 0 "Login → token received" || check 1 "Login failed"
  ME=$(curl -s "$API/auth/me" -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "")
  echo "$ME" | grep -q "$EMAIL" && check 0 "Auth/me → correct user" || check 1 "Auth/me failed"
  BILL=$(curl -s "$API/billing/status" -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "")
  echo "$BILL" | grep -q "free" && check 0 "Billing → free plan" || check 1 "Billing failed"
else
  check 1 "Registration — failed: $(echo "$REG" | head -c 100)"
fi

# ── 6. SQL INJECTION ──
echo ""
echo "── 6. SQL INJECTION ──"
for PAYLOAD in "' OR '1'='1" "'; DROP TABLE users; --" "' UNION SELECT * FROM users; --"; do
  CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$PAYLOAD\",\"password\":\"x\"}" 2>/dev/null || echo "000")
  [ "$CODE" != "200" ] && check 0 "SQLi blocked: ${PAYLOAD:0:15} → $CODE" || check 1 "SQLi VULN: $PAYLOAD → $CODE"
done

# ── 7. INPUT VALIDATION ──
echo ""
echo "── 7. INPUT VALIDATION ──"
for payload in '{"email":"bad","password":"short"}' '{"email":"x@y.co","password":"1234567"}'; do
  CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/auth/register" \
    -H "Content-Type: application/json" -d "$payload" 2>/dev/null || echo "000")
  [ "$CODE" = "400" ] && check 0 "Invalid input rejected → $CODE" || check 1 "Invalid input → $CODE"
done

# ── 8. ANALYTICS XSS ──
echo ""
echo "── 8. ANALYTICS XSS ──"
CODE=$(curl -so /dev/null -w '%{http_code}' -X POST "$API/analytics/event" \
  -H "Content-Type: application/json" \
  -d '{"file_id":"00000000-0000-0000-0000-000000000000","event_type":"<script>","bytes_p2p":0,"bytes_origin":0}' 2>/dev/null || echo "000")
[ "$CODE" = "400" ] && check 0 "XSS event_type blocked → $CODE" || check 1 "XSS event_type → $CODE"

# ── 9. CORS ──
echo ""
echo "── 9. CORS ──"
ACAO=$(curl -s -X OPTIONS "$API/health" \
  -H "Origin: http://evil.com" \
  -H "Access-Control-Request-Method: GET" \
  -I 2>/dev/null | grep -i 'access-control-allow-origin' | head -1 || echo "")
[ -z "$ACAO" ] || echo "$ACAO" | grep -qv 'evil.com' && check 0 "CORS blocks evil.com" || check 1 "CORS allows evil.com: $ACAO"

# ── 10. HTTP METHODS ──
echo ""
echo "── 10. HTTP METHOD RESTRICTIONS ──"
for M in PUT DELETE PATCH TRACE CONNECT; do
  CODE=$(curl -so /dev/null -w '%{http_code}' -X "$M" "$API/health" 2>/dev/null || echo "000")
  [ "$CODE" = "405" ] && check 0 "$M → 405" || check 1 "$M → $CODE"
done

# ── SUMMARY ──
echo ""
echo "============================================================"
echo "  RESULTS: $PASS ✅  $FAIL ❌"
echo "============================================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
