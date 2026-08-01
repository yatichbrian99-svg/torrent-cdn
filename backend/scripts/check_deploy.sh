#!/bin/bash
# Verify which commit is LIVE on Render — without the dashboard.
#
# Requires a read-only Render API key (Account Settings → API Keys):
#   RENDER_API_KEY=rnd_... bash check_deploy.sh [expected_commit]
#
# The script:
#   1. Finds the web service by name (default: torrent-cdn)
#   2. Lists its deploys (newest first) and finds the latest one with
#      status "live"
#   3. Reports the live commit id + message + deploy time
#   4. Compares against an expected commit (default: local HEAD short SHA)
#
# Exit codes: 0 = live commit matches expected
#             1 = live commit differs / no live deploy
#             2 = usage / API error / key missing
#
# Only GET endpoints are used, so a read-only key is sufficient.
set -uo pipefail

API="https://api.render.com/v1"
KEY="${RENDER_API_KEY:-}"
SERVICE_NAME="${SERVICE_NAME:-torrent-cdn}"
EXPECTED="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo '')}"

if [ -z "$KEY" ]; then
  echo "❌ RENDER_API_KEY is not set." >&2
  echo "   Create one: Render dashboard → Account Settings → API Keys → Create API Key" >&2
  echo "   Then: RENDER_API_KEY=rnd_... bash $0 [expected_commit]" >&2
  exit 2
fi
if [ -z "$EXPECTED" ]; then
  echo "❌ No expected commit given and local git HEAD unavailable." >&2
  exit 2
fi

echo "============================================================"
echo "  RENDER DEPLOY CHECK  (service: $SERVICE_NAME)"
echo "  expected commit: $EXPECTED"
echo "============================================================"

# ── 1. Find the service id by name ──
SERVICES=$(curl -s --max-time 20 -H "Authorization: Bearer $KEY" "$API/services")
# Fail fast on auth/API errors (e.g. 401 bad key) instead of reporting "not found"
if echo "$SERVICES" | grep -qi '"message"' ; then
  echo "❌ Render API error — check RENDER_API_KEY (is it valid / read-only?):" >&2
  echo "$SERVICES" | head -c 300 >&2
  echo "" >&2
  exit 2
fi
# Pipe the JSON in explicitly: an env-prefixed `python3` inside a command
# substitution would otherwise inherit this script's stdin — which may be an
# open pipe that never EOFs — and json.load(sys.stdin) would block forever.
SVC_ID=$(echo "$SERVICES" | SERVICE_NAME="$SERVICE_NAME" python3 -c '
import json, os, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
name = os.environ.get("SERVICE_NAME", "")
for item in data:
    svc = item.get("service", {})
    if svc.get("name") == name:
        print(svc.get("id", ""))
        break
')

if [ -z "$SVC_ID" ]; then
  echo "❌ Service '$SERVICE_NAME' not found (or empty API response). Raw response:" >&2
  echo "$SERVICES" | head -c 500 >&2
  echo "" >&2
  exit 2
fi
echo "  service id: $SVC_ID"

# ── 2. List deploys, find the newest LIVE one ──
DEPLOYS=$(curl -s --max-time 20 -H "Authorization: Bearer $KEY" "$API/services/$SVC_ID/deploys")
# Same stdin note as above: the JSON is piped in, never read from inherited stdin.
LIVE_INFO=$(echo "$DEPLOYS" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for item in data:
    dep = item.get("deploy", {})
    if dep.get("status") == "live":
        commit = dep.get("commit") or {}
        print(dep.get("id", ""))
        print(commit.get("id", ""))
        print((commit.get("message") or "").replace("\n", " ")[:80])
        print(dep.get("createdAt", ""))
        sys.exit(0)
print("NO_LIVE")
')

if [ "$LIVE_INFO" = "NO_LIVE" ] || [ -z "$LIVE_INFO" ]; then
  echo "❌ No deploy with status 'live' found for $SERVICE_NAME." >&2
  echo "   Recent deploys (id / status / commit):" >&2
  echo "$DEPLOYS" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for item in data[:5]:
    dep = item.get("deploy", {})
    commit = dep.get("commit") or {}
    print(f"  - {dep.get(\"id\")} | {dep.get(\"status\")} | {(commit.get(\"id\") or \"\")[:7]} | {dep.get(\"createdAt\")}")' 2>/dev/null || true
  exit 1
fi

DEP_ID=$(echo "$LIVE_INFO" | sed -n '1p')
LIVE_SHA=$(echo "$LIVE_INFO" | sed -n '2p')
LIVE_MSG=$(echo "$LIVE_INFO" | sed -n '3p')
LIVE_AT=$(echo "$LIVE_INFO" | sed -n '4p')

echo "  live deploy id: $DEP_ID"
echo "  live commit:    $LIVE_SHA"
echo "  live message:   $LIVE_MSG"
echo "  live since:     $LIVE_AT"

# ── 3. Compare (prefix match — EXPECTED may be short or full SHA) ──
case "$LIVE_SHA" in
  "$EXPECTED"*|"$EXPECTED")
    echo ""
    echo "✅ LIVE commit matches expected: $EXPECTED"
    exit 0
    ;;
  *)
    echo ""
    echo "❌ LIVE commit ($LIVE_SHA) does NOT match expected ($EXPECTED)." >&2
    exit 1
    ;;
esac
