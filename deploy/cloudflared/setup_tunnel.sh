#!/bin/sh
# One-time helper for Option A setup on THIS machine (requires cloudflared on
# the host, or run it inside a throwaway container):
#
#   1. interactive `cloudflared tunnel login` (opens a browser) → cert.pem
#   2. create the named tunnel → prints TUNNEL_ID + credentials JSON path
#   3. route DNS: hls.<DOMAIN> CNAME → <tunnel-id>.cfargotunnel.com
#
# After this, `docker compose --profile edge up -d cloudflared-hls` keeps the
# tunnel alive with zero further interaction. NOT run by compose — this exists
# so the operator doesn't memorize cloudflared flags.
set -eu
BASE="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="${DOMAIN:-torrentcdn.store}"
TUNNEL_NAME="${TUNNEL_NAME:-torrentcdn-hls}"
HOST_NAME="${HOST_NAME:-hls.$DOMAIN}"

command -v cloudflared >/dev/null 2>&1 || {
  echo "cloudflared not found on PATH. Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
  exit 1
}

CERT="$HOME/.cloudflared/cert.pem"
if [ ! -f "$CERT" ]; then
  echo "==> Opening browser to authorize cloudflared for $DOMAIN …"
  cloudflared tunnel login
fi

echo "==> Creating tunnel '$TUNNEL_NAME' (idempotent)…"
if cloudflared tunnel list 2>/dev/null | grep -qw "$TUNNEL_NAME"; then
  echo "    tunnel already exists"
else
  cloudflared tunnel create "$TUNNEL_NAME"
fi

TUNNEL_ID="$(cloudflared tunnel list --output json | python3 -c \
  "import json,sys; print([t['id'] for t in json.load(sys.stdin) if t['name']=='$TUNNEL_NAME'][0])")"
echo "    TUNNEL_ID=$TUNNEL_ID"
echo "$TUNNEL_ID" > "$BASE/TUNNEL_ID"

echo "==> Routing $HOST_NAME → tunnel…"
cloudflared tunnel route dns "$TUNNEL_NAME" "$HOST_NAME" || echo "    (DNS record may already exist — continuing)"

echo "==> Writing credentials file for compose…"
TUNNEL_ID="$TUNNEL_ID" DNS_NAME="$HOST_NAME" "$BASE/write_tunnel_json.sh"

cat <<EOF

DONE. Next steps:
  1. docker compose --profile edge up -d cloudflared-hls
  2. Verify:  curl -sI https://$HOST_NAME/live/<channel>/index.m3u8
  3. Render:  MEDIAMTX_HLS_URL=https://$HOST_NAME
EOF
