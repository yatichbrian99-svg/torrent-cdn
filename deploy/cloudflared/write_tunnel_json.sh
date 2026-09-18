#!/bin/sh
# Writes deploy/cloudflared/host/config.json (the named-tunnel configuration)
# after the tunnel is created. Usage:
#   TUNNEL_ID=<uuid> [DNS_NAME=hls.torrentcdn.store] ./write_tunnel_json.sh
#
# Ingress: the cloudflared container sits on the compose network, so HLS is
# routed to the MediaMTX service directly (never expose 8888 publicly).
set -eu
BASE="$(cd "$(dirname "$0")" && pwd)"
HOST_DIR="$BASE/host"
TUNNEL_ID="${TUNNEL_ID:?TUNNEL_ID is printed by setup_tunnel.sh (the <uuid>.json filename)}"
DNS_NAME="${DNS_NAME:-hls.torrentcdn.store}"
ORIGIN="${ORIGIN:-http://mediamtx:8888}"

mkdir -p "$HOST_DIR"
jq -n --arg id "$TUNNEL_ID" --arg name "$DNS_NAME" --arg origin "$ORIGIN" \
  '{tunnel: $id,
    "credentials-file": "/etc/cloudflared/credentials.json",
    ingress: [
      {hostname: $name, service: $origin},
      {service: "http_status:404"}
    ]}' \
  > "$HOST_DIR/config.json"

cat <<EOF

Wrote $HOST_DIR/config.json for tunnel $TUNNEL_ID ($DNS_NAME -> $ORIGIN).

NOTE: copy the tunnel credentials JSON (~/.cloudflared/$TUNNEL_ID.json —
printed by 'cloudflared tunnel create') into $HOST_DIR/credentials.json,
then:  docker compose --profile edge up -d cloudflared-hls
EOF
