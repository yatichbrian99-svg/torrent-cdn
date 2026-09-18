#!/bin/sh
# Namecheap Dynamic-DDNS updater — keeps the public A records for the live
# edge pointed at this machine's CURRENT public IP. This is what turns a
# home/office broadband connection into permanent, named endpoints:
#
#     rtmp.torrentcdn.store  → public IP :1935  (router forward → MediaMTX)
#     hls.torrentcdn.store   → public IP :443   (router forward → Caddy TLS)
#
# Namecheap DDNS needs NO API token and NO DNS migration: enable "Dynamic DNS"
# under Advanced DNS for the domain, then create a DDNS password ("Dynamic DNS
# Password") and set NAMECHEAP_DDNS_PASSWORD in .env. That single secret is
# the entire credential surface.
#
# Protocol (Namecheap's update endpoint):
#   https://dynamicdns.park-your-domain.com/update?domain=<d>&host=<h>&password=<p>&ip=<ip>
#   host=@ updates the apex; one call per subdomain. Success = <ErrCount>0.
#
# Busybox-compatible: wget + grep + sed only — runs in plain alpine/busybox
# with zero package installs. Fails SOFT: a transient network error or a
# wrong password logs clearly and retries on the next tick; the loop never
# exits, so a 2 a.m. ISP reboot self-heals.
set -u

: "${NAMECHEAP_DDNS_PASSWORD:?NAMECHEAP_DDNS_PASSWORD must be set (Namecheap > Advanced DNS > Dynamic DNS password)}"
: "${DDNS_DOMAIN:?DDNS_DOMAIN must be set (e.g. torrentcdn.store)}"
HOSTS="${HOSTS:-rtmp,hls}"
INTERVAL="${INTERVAL:-300}"
STATE_DIR="${STATE_DIR:-/data}"
mkdir -p "$STATE_DIR" 2>/dev/null || STATE_DIR=/tmp

URL_BASE="https://dynamicdns.park-your-domain.com/update"
log() { echo "[ddns $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

current_ip() {
  ip="$(wget -q -T 15 -O - https://api.ipify.org 2>/dev/null || true)"
  [ -n "$ip" ] || ip="$(wget -q -T 15 -O - https://ifconfig.me 2>/dev/null || true)"
  echo "$ip"
}

# namecheap_update <host> <ip>  — prints 0 on success, 1 on failure
namecheap_update() {
  host="$1"; ip="$2"
  resp="$(wget -q -T 30 -O - \
    "$URL_BASE?domain=$DDNS_DOMAIN&host=$host&password=$NAMECHEAP_DDNS_PASSWORD&ip=$ip" 2>/dev/null || true)"
  # Success response: <ErrCount>0</ErrCount>  (names are case-tolerant)
  if echo "$resp" | grep -qE "<ErrCount>0</ErrCount>"; then
    return 0
  fi
  err="$(echo "$resp" | grep -oE "<Err1>[^<]*</Err1>" | sed 's/<[^>]*>//g')"
  log "UPDATE FAILED for $host: ${err:-no/invalid response — check password and that Dynamic DNS is enabled}"
  return 1
}

log "watching $HOSTS.$DDNS_DOMAIN (poll every ${INTERVAL}s)"
while :; do
  ip="$(current_ip)"
  if [ -z "$ip" ]; then
    log "could not determine public IP; retrying next cycle"
  else
    changed=""
    for h in $(echo "$HOSTS" | tr ',' ' '); do
      state_f="$STATE_DIR/last_ip_$h"
      last="$(cat "$state_f" 2>/dev/null || true)"
      if [ "$ip" = "$last" ]; then
        continue  # unchanged — stay quiet
      fi
      changed=1
      if namecheap_update "$h" "$ip"; then
        log "$h.$DDNS_DOMAIN -> $ip (was ${last:-unset})"
        echo "$ip" > "$state_f"
      fi
    done
    [ -z "$changed" ] || true
  fi
  sleep "$INTERVAL"
done
