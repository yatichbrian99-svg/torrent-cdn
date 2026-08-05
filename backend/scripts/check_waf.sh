#!/usr/bin/env bash
# check_waf.sh — verify WAF / CDN presence in front of the production site.
#
# The app is served by Vercel (server: Vercel), which has a native Web
# Application Firewall ("Vercel Firewall") with managed rules — that is the
# recommended no-DNS-change WAF. Cloudflare proxying is the alternative.
#
# This script reports what edge/WAF layer is actually in front of the domain so
# the operator can confirm a WAF enablement took effect. It is read-only
# (curl HEAD/GET probes only).
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
VXID=$(val 'x-vercel-id')
VXFIREWALL=$(val 'x-vercel-firewall')
[ -z "$VXFIREWALL" ] && VXFIREWALL=$(val 'x-vercel-protection')

echo "server:           ${SERVER:-<none>}"
echo "cf-ray:           ${CF_RAY:-<none>}"
echo "x-vercel-id:      ${VXID:-<none>}"
echo "vercel waf hdrs:  ${VXFIREWALL:-<none>}"
echo

if [ -n "$CF_RAY" ]; then
  echo "✅ Cloudflare proxy detected — its managed WAF rulesets are active for this host."
  echo "   Tune them at Cloudflare dashboard → Security → WAF → Managed rules."
  exit 0
fi

if [ -n "$SERVER" ] && echo "$SERVER" | grep -qi vercel; then
  echo "ℹ️  Directly on Vercel (no Cloudflare in front)."
  echo "   Recommended: enable the native Vercel Firewall (no DNS change):"
  echo "     Vercel dashboard → Project → Firewall → enable + Managed rules."
  echo "   Verify after enabling by re-running this script (look for"
  echo "   x-vercel-firewall-* / x-vercel-protection headers or 403 on the probe below)."
  echo
  echo "   Optional Cloudflare alternative:"
  echo "     1. Add torrentcdn.store as a Cloudflare zone (Free plan)."
  echo "     2. DNS → CNAME  www → cname.vercel-dns.com  with proxy (orange cloud) ON."
  echo "     3. SSL/TLS → Full (strict); ensure the apex 308-redirect is preserved."
  echo "     4. Security → WAF → Managed rules → default Cloudflare OWASP set."
  echo
  echo "   Quick sanity probe (a WAF typically answers 403 for a known-bad UA):"
  curl -s -o /dev/null -w "     UA 'sqlmap' -> HTTP %{http_code}\n" --max-time 20 \
    -A "sqlmap/1.7" "$URL"
  exit 0
fi

echo "⚠️  No Cloudflare ray and no Vercel server header — unexpected edge topology."
echo "   Verify DNS/proxy configuration before relying on WAF protection."
exit 1
