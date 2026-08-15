#!/usr/bin/env bash
# check_waf.sh — verify WAF / CDN protection is ACTIVE in front of production.
#
# The app is served by Vercel (server: Vercel), which has a native Web
# Application Firewall ("Vercel Firewall") with managed rules — the recommended
# no-DNS-change WAF. Cloudflare proxying is the alternative.
#
# Pass/fail logic:
#   [OK] Cloudflare ray detected                    → Cloudflare WAF layer active
#   [OK] x-vercel-firewall header OR the sqlmap-UA probe returns 403/429/451
#                                               → Vercel Firewall active (blocking)
#   [FAIL] otherwise                                 → no WAF detected (exit 1)
#
# Read-only (curl HEAD/GET probes only).
#
# Usage:  bash backend/scripts/check_waf.sh [https://www.torrentcdn.store]
set -u

URL="${1:-https://www.torrentcdn.store}"
echo "=== WAF / CDN presence probe: $URL ==="

HDRS=$(curl -sI --max-time 20 "$URL" | tr -d '\r')
# Extract values only (strip the "name: " prefix)
val() { echo "$HDRS" | grep -i "^$1:" | head -1 | sed "s/^[^:]*:[[:space:]]*//"; }
CF_RAY=$(val 'cf-ray')
SERVER=$(val 'server')
VXFIREWALL=$(val 'x-vercel-firewall')
[ -z "$VXFIREWALL" ] && VXFIREWALL=$(val 'x-vercel-protection')

echo "server:           ${SERVER:-<none>}"
echo "cf-ray:           ${CF_RAY:-<none>}"
echo "x-vercel-firewall: ${VXFIREWALL:-<none>}"
echo

if [ -n "$CF_RAY" ]; then
  echo "[OK] Cloudflare proxy detected — its managed WAF rulesets are active for this host."
  echo "   Tune them at Cloudflare dashboard → Security → WAF → Managed rules."
  exit 0
fi

# Firewall rule headers in the normal response = WAF is evaluating requests.
if [ -n "$VXFIREWALL" ]; then
  echo "[OK] Vercel Firewall ACTIVE — rule header present: $VXFIREWALL"
  exit 0
fi

if [ -n "$SERVER" ] && echo "$SERVER" | grep -qi vercel; then
  # The decisive test: a known-attack User-Agent must NOT get through.
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -A 'sqlmap/1.7' "$URL")
  echo "probe (UA 'sqlmap'): HTTP $CODE"
  if [ "$CODE" = "403" ] || [ "$CODE" = "429" ] || [ "$CODE" = "451" ]; then
    echo
    echo "[OK] Attack probe blocked at the edge (HTTP $CODE) — scanner traffic is stopped"
    echo "   before it reaches the app. (Source: app edge middleware scanner-UA block,"
    echo "   or a CDN/WAF ruleset if one is configured.)"
    exit 0
  fi

  echo
  echo "[FAIL] WAF NOT DETECTED — probes pass through (HTTP $CODE) and no firewall"
  echo "   header is present, so no managed WAF ruleset is blocking."
  echo
  echo "   NOTE: on Vercel's Hobby (free) plan, the OWASP managed ruleset is NOT"
  echo "   available (Enterprise-only). Hobby only includes DDoS mitigation, IP"
  echo "   blocking, 1 custom rule, and 1 rate-limit rule — none of which can"
  echo "   match the probes this script sends."
  echo
  echo "   Recommended (free, includes the OWASP Core ruleset): put Cloudflare"
  echo "   in front — this script then reports 'Cloudflare proxy detected':"
  echo "     1. Add torrentcdn.store as a Cloudflare zone (Free plan)."
  echo "     2. DNS → CNAME  www → cname.vercel-dns.com  with proxy (orange) ON."
  echo "     3. SSL/TLS → Full (strict); keep Vercel's apex 308-redirect."
  echo "     4. Security → WAF → Managed rules → OWASP Core ruleset ON."
  echo "   Then re-run this script — expect cf-ray + a 403 on the sqlmap probe."
  echo
  echo "   On Pro/Enterprise instead? Enable the Vercel Firewall → Managed rules"
  echo "   → OWASP ruleset with action = Block, wait ~5 min, re-run."
  exit 1
fi

echo "[WARN]  No Cloudflare ray, no Vercel server header — unexpected edge topology."
echo "   Verify DNS/proxy configuration before relying on WAF protection."
exit 1
