#!/bin/sh
# Cloudflare-API DDNS updater — keeps the RTMP ingest DNS record pointed at
# this machine's CURRENT public IP. This is the piece that makes a home/office
# broadband connection a permanent RTMP endpoint:
#
#     OBS → rtmp://rtmp.torrentcdn.store:1935  (A record → your public IP)
#         → router port-forward :1935 → this host's MediaMTX
#
# Uses the Cloudflare DNS API directly (no third-party DDNS client): the
# domain's DNS lives on Cloudflare after the one-time onboarding, so this
# same API also serves the named tunnel record for HLS.
#
# REQUIRED env (compose wires these from .env):
#   CF_API_TOKEN  Cloudflare API token scoped to Zone.DNS Edit on ONE zone
#   CF_ZONE_ID    Cloudflare zone ID for the domain (dashboard, right column)
#   DNS_NAME      the record to maintain, e.g. rtmp.torrentcdn.store
#   TTL/INTERVAL  record TTL and poll interval in seconds (defaults 300)
#
# Behavior: polls the public IP, and only touches the API when the IP has
# actually changed (state kept in /data/last_ip). The record is created on
# first run if missing. NOT proxied (grey-cloud) — arbitrary TCP can't be
# proxied without Spectrum.
set -eu

: "${CF_API_TOKEN:?CF_API_TOKEN must be set}"
: "${CF_ZONE_ID:?CF_ZONE_ID must be set}"
DNS_NAME="${DNS_NAME:?DNS_NAME must be set}"
TTL="${TTL:-300}"
INTERVAL="${INTERVAL:-300}"
STATE="${STATE:-/data/last_ip}"
mkdir -p "$(dirname "$STATE")" 2>/dev/null || STATE=/tmp/last_ip

API="https://api.cloudflare.com/client/v4"
log() { echo "[ddns $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
api() {
  curl -sS --max-time 30 \
    -H "Authorization: Bearer $CF_API_TOKEN" \
    -H "Content-Type: application/json" "$@"
}
current_ip() {
  curl -s --max-time 15 https://api.ipify.org \
    || curl -s --max-time 15 https://ifconfig.me \
    || true
}

last_ip=""
[ -f "$STATE" ] && last_ip="$(cat "$STATE" 2>/dev/null || true)"

log "watching $DNS_NAME (poll every ${INTERVAL}s)"
while :; do
  ip="$(current_ip)"
  if [ -z "$ip" ]; then
    log "could not determine public IP; retrying next cycle"
  elif [ "$ip" = "$last_ip" ]; then
    : # no change — stay quiet
  else
    log "public IP: $ip (previous: ${last_ip:-none})"
    resp="$(api "$API/zones/$CF_ZONE_ID/dns_records?type=A&name=$DNS_NAME&per_page=1" || true)"
    if [ -n "$resp" ] && echo "$resp" | jq -e '.success' >/dev/null 2>&1; then
      record_id="$(echo "$resp" | jq -r '.result[0].id // empty')"
    else
      record_id=""
      log "Cloudflare lookup failed — check CF_API_TOKEN/CF_ZONE_ID: $(echo "$resp" | head -c 200)"
    fi
    body="$(printf '{"type":"A","name":"%s","content":"%s","ttl":%s,"proxied":false}' \
      "$DNS_NAME" "$ip" "$TTL")"
    if [ -n "$record_id" ]; then
      if api -X PATCH "$API/zones/$CF_ZONE_ID/dns_records/$record_id" --data "$body" \
        | jq -e '.success' >/dev/null 2>&1; then
        log "UPDATED $DNS_NAME -> $ip"
        last_ip="$ip"; echo "$ip" > "$STATE"
      else
        log "UPDATE FAILED — token needs Zone.DNS Edit on this zone"
      fi
    else
      if api -X POST "$API/zones/$CF_ZONE_ID/dns_records" --data "$body" \
        | jq -e '.success' >/dev/null 2>&1; then
        log "CREATED $DNS_NAME -> $ip"
        last_ip="$ip"; echo "$ip" > "$STATE"
      else
        log "CREATE FAILED — token needs Zone.DNS Edit on this zone"
      fi
    fi
  fi
  sleep "$INTERVAL"
done
