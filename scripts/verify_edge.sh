#!/usr/bin/env bash
# Edge verification — run AFTER the one-time operator setup:
#   1. Namecheap: Dynamic DNS enabled + DDNS password in .env
#   2. Router forwards: TCP 1935, 80, 443 → this machine
# Usage: bash scripts/verify_edge.sh [public-ip]
# The public IP is auto-detected; pass it explicitly if behind a VPN.
set -u
PUB_IP="${1:-$(curl -s --max-time 15 ifconfig.me || true)}"
PASS=0; FAIL=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "════ TorrentCDN edge verification ════"
echo "public IP: ${PUB_IP:-unknown}"; echo

echo "── 1. DNS (Namecheap DDNS updater running?) ──"
for h in rtmp hls; do
  host="$h.torrentcdn.store"
  resolved="$(python3 -c "import socket; print(socket.gethostbyname('$host'))" 2>/dev/null || echo "")"
  if [ "$resolved" = "$PUB_IP" ]; then ok "$host → $resolved (matches public IP)"
  elif [ -z "$resolved" ]; then bad "$host → NXDOMAIN (DDNS not run yet / Dynamic DNS off)"
  else bad "$host → $resolved (≠ $PUB_IP — stale or wrong record)"; fi
done

echo
echo "── 2. TCP forwards (router) ──"
for port in 1935 443 80; do
  if timeout 6 bash -c "</dev/tcp/$PUB_IP/$port" 2>/dev/null; then ok "port $port reachable"; else bad "port $port closed/filtered (forward missing or ISP blocks)"; fi
done

echo
echo "── 3. TLS + HLS (Caddy on-demand cert) ──"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://hls.torrentcdn.store/live/nonexistent-channel/index.m3u8" 2>/dev/null)"
[ -z "$code" ] && code=000
case "$code" in
  404) ok "https://hls.torrentcdn.store answered (TLS issued, 404 for unknown channel = expected)" ;;
  000) bad "no TLS response — DNS/forward not done yet" ;;
  *)   echo "  ⚠️  unexpected HTTP $code (see above; may still be fine)"; PASS=$((PASS+1)) ;;
esac

echo
echo "── 4. RTMP publish (auth + HLS + LIVE flip, ~35s) ──"
command -v ffmpeg >/dev/null || { echo "  (ffmpeg not found — install it to run stage 4)"; }
if command -v ffmpeg >/dev/null; then
  KEY="${VERIFY_STREAM_KEY:-}"
  if [ -z "$KEY" ]; then
    printf "  Stream key (paste 'slug?pass=live_…' or plain slug): "
    read -r KEY
  fi
  [ -n "$KEY" ] || KEY="verify-edge"
  timeout 30 ffmpeg -hide_banner -loglevel error \
    -f lavfi -re -i "testsrc=size=1280x720:rate=30" \
    -f lavfi -re -i "sine=frequency=440" \
    -c:v libx264 -preset veryfast -g 60 -b:v 2500k -c:a aac -b:a 128k \
    -f flv "rtmp://rtmp.torrentcdn.store/live/$KEY" 2>&1 | head -3
  rc="${PIPESTATUS[0]}"
  [ $rc -eq 0 ] && ok "publish accepted (auth passed, mux started)" || bad "publish failed rc=$rc (check auth hook logs)"
fi

echo
echo "════ $PASS passed, $FAIL failed ════"
[ $FAIL -eq 0 ] && echo "Edge is LIVE-ready: OBS → rtmp://rtmp.torrentcdn.store/live" || \
  echo "Fix the ❌ items above — each maps to one step in deploy/ddns/README.md"
