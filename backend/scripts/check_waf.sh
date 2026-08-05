#!/usr/bin/env bash
# check_waf.sh — verify WAF / CDN protection is ACTIVE in front of production.
#
# The app is served by Vercel (server: Vercel), which has a native Web
# Application Firewall ("Vercel Firewall") with managed rules — the recommended
# no-DNS-change WAF. Cloudflare proxying is the alternative.
#
# Pass/fail logic:
#   ✅ Cloudflare ray detected                    → Cloudflare WAF layer active
#   ✅ x-vercel-firewall header OR the sqlmap-UA probe returns 403/429/451
#                                               → Vercel Firewall active (blocking)
#   ❌ otherwise                                 → no WAF detected (exit 1)
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
  echo "✅ Cloudflare proxy detected — its managed WAF rulesets are active for this host."
  echo "   Tune them at Cloudflare dashboard → Security → WAF → Managed rules."
  exit 0
fi

# Firewall rule headers in the normal response = WAF is evaluating requests.
if [ -n "$VXFIREWALL" ]; then
  echo "✅ Vercel Firewall ACTIVE — rule header present: $VXFIREWALL"
  exit 0
fi

if [ -n "$SERVER" ] && echo "$SERVER" | grep -qi vercel; then
  # The decisive test: a known-attack User-Agent must NOT get through.
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -A 'sqlmap/1.7' "$URL")
  echo "probe (UA 'sqlmap'): HTTP $CODE"
  if [ "$CODE" = "403" ] || [ "$CODE" = "429" ] || [ "$CODE" = "451" ]; then
    echo
    echo "✅ Vercel Firewall ACTIVE — the attack probe was blocked (HTTP $CODE)."
    exit 0
  fi

  echo
  echo "❌ WAF NOT DETECTED — probes pass through (HTTP $CODE) and no firewall"
  echo "   header is present, so the Vercel Firewall is not (yet) blocking."
  echo
  echo "   If you just enabled it, check the Vercel dashboard:"
  echo "     Vercel dashboard → Project → Firewall → Firewall enabled?"
  echo "     → Managed rules: enable the OWASP ruleset and set the action to"
  echo "       'Block' (the default is often 'Log' only, which lets traffic through)."
  echo "   Then wait a few minutes for edge propagation and re-run this script."
  echo
  echo "   Optional Cloudflare alternative:"
  echo "     1. Add torrentcdn.store as a Cloudflare zone (Free plan)."
  echo "     2. DNS → CNAME  www → cname.vercel-dns.com  with proxy ON."
  echo "     3. SSL/TLS → Full (strict); keep the apex 308-redirect."
  echo "     4. Security → WAF → Managed rules → OWASP set."
  exit 1
fi

echo "⚠️  No Cloudflare ray, no Vercel server header — unexpected edge topology."
echo "   Verify DNS/proxy configuration before relying on WAF protection."
exit 1
