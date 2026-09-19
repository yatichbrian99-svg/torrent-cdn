#!/usr/bin/env bash
# ============================================================================
# TorrentCDN live-media edge — Oracle Cloud Always-Free VM bootstrap
# ============================================================================
# Run ON THE VM as root. Preferred (from a repo checkout, uses repo configs):
#     sudo bash deploy/oracle/bootstrap.sh
#
# What it does:
#   1. Installs Docker Engine + Compose plugin (official apt repo)
#   2. Writes /opt/torrentcdn-edge/ : mediamtx.yml, Dockerfile (busybox+sh so
#      the runOnReady hooks work), Caddyfile (TLS for the HLS host), and the
#      VM docker-compose.yml — copy-from-repo when available, embedded
#      verbatim fallback otherwise (this repo is private; no raw-URL fetch)
#   3. Generates fresh secrets on first run → /opt/torrentcdn-edge/.env
#   4. Opens iptables for 80/443/1935. Oracle's Ubuntu images ship REJECT
#      rules in INPUT — the #1 cause of "security list open but port dead".
#   5. Brings up the stack: MediaMTX (RTMP 1935 public; HLS/API loopback via
#      compose) + Caddy (80/443 → mediamtx:8888, TLS auto-issued for the host)
#
# After it finishes (all inside README.md):
#   a) Oracle Console: open the instance's Security List for 80/443/1935
#   b) DNS: A records hls.<domain> AND rtmp.<domain> → the VM public IP
#   c) Render env: values printed at the end of this script
# ============================================================================
set -euo pipefail

EDGE_DIR=/opt/torrentcdn-edge
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd || true)"
IN_REPO=0
if [ -n "$REPO_DIR" ] && [ -f "$REPO_DIR/deploy/mediamtx/mediamtx.yml" ]; then IN_REPO=1; fi

# ── 0. Preflight ────────────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash $0"; exit 1; }
. /etc/os-release 2>/dev/null || true
echo "== TorrentCDN edge bootstrap (${ID:-unknown} ${VERSION_ID:-?}) =="
echo "   public IP : $(curl -fsSL --max-time 10 https://checkip.amazonaws.com || echo 'UNKNOWN')"
command -v curl >/dev/null || { apt-get update -y; apt-get install -y curl ca-certificates; }

# ── 1. Docker ───────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  echo "-- installing Docker Engine"
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi
docker compose version >/dev/null 2>&1 || { echo "ERROR: docker compose plugin missing"; exit 1; }

mkdir -p "$EDGE_DIR"; cd "$EDGE_DIR"

# ── 2. Config files ─────────────────────────────────────────────────────────
# mediamtx.yml + Dockerfile: copy from the repo checkout when present (single
# source of truth), else fall back to embedded verbatim copies below.
echo "-- writing configs to $EDGE_DIR"
if [ "$IN_REPO" = "1" ]; then
  cp "$REPO_DIR/deploy/mediamtx/mediamtx.yml" mediamtx.yml
  cp "$REPO_DIR/deploy/mediamtx/Dockerfile"   Dockerfile
else
  echo "   (no repo checkout found — using embedded copies)"
  cat > Dockerfile <<'DOCKERFILE_EOF'
# MediaMTX + busybox applets — the distroless upstream image has no shell, so
# runOnReady/runOnNotReady hook commands (executed as `/bin/sh -c …`) cannot
# run. busybox /bin (~1 MB, static musl) is copied in without touching the
# entrypoint, user, or the MediaMTX binary.
FROM busybox:1.36.1-musl AS busybox
FROM bluenviron/mediamtx:1.11.3
COPY --from=busybox /bin /bin
DOCKERFILE_EOF

  cat > mediamtx.yml <<'MEDIAMTX_EOF'
# Verbatim copy of deploy/mediamtx/mediamtx.yml (source of truth in the repo).
# Auth is delegated to the TorrentCDN backend (authMethod: http); the real
# hook secret arrives via MTX_AUTHHTTPADDRESS (set in docker-compose.yml).
# Control API (9997) and metrics (9998) are INTERNAL ONLY — the compose file
# below publishes them on 127.0.0.1 and they are never exposed publicly.
logLevel: info
logDestinations: [stdout]

authMethod: http
authHTTPAddress: http://backend:8000/api/internal/mediamtx/auth?secret=override-via-MTX_AUTHHTTPADDRESS
authHTTPExclude:
  - action: api
  - action: metrics
  - action: pprof

api: yes
apiAddress: :9997
apiEncryption: no
metrics: yes
metricsAddress: :9998

rtmp: yes
rtmpAddress: :1935
rtmpEncryption: "no"

rtsp: no
srt: no

hls: yes
hlsAddress: :8888
hlsVariant: lowLatency
hlsSegmentDuration: 2s
hlsPartDuration: 700ms
hlsSegmentCount: 7
hlsEncryption: no

webrtc: yes
webrtcAddress: :8889
webrtcAdditionalHosts: [localhost]

paths:
  all_others:
    # NOTE: single-line folded scalars — the "X-Hook-Secret: …" colon+space
    # would terminate a plain multiline YAML scalar.
    runOnReady: >-
      sh -c 'wget -q -O- -T 5 --post-data "slug=$MTX_PATH"
      --header "X-Hook-Secret: $MEDIAMTX_HOOK_SECRET"
      "$MEDIAMTX_READY_HOOK_URL/ready" || true'
    runOnNotReady: >-
      sh -c 'wget -q -O- -T 5 --post-data "slug=$MTX_PATH"
      --header "X-Hook-Secret: $MEDIAMTX_HOOK_SECRET"
      "$MEDIAMTX_READY_HOOK_URL/not-ready" || true'
MEDIAMTX_EOF
fi

# ── 3. Secrets (generated once, persisted, chmod 600) ───────────────────────
if [ ! -f .env ]; then : > .env; fi
chmod 600 .env
gen_if_missing() {
  grep -q "^$1=" .env || printf '%s=%s\n' "$1" "$2" >> .env
}
gen_if_missing MEDIAMTX_HOOK_SECRET "$(openssl rand -hex 24)"
gen_if_missing MEDIAMTX_API_USER    "torrentcdn"
gen_if_missing MEDIAMTX_API_PASS    "$(openssl rand -hex 16)"
# shellcheck disable=SC1091
. ./.env

# ── 4. Hostname → Caddyfile + compose ───────────────────────────────────────
read -rp "Public hostname for HLS playback [hls.torrentcdn.store]: " HLS_HOST_IN
HLS_HOST="${HLS_HOST_IN:-hls.torrentcdn.store}"

cat > Caddyfile <<CADDY_EOF
# Generated by deploy/oracle/bootstrap.sh — TLS edge for TorrentCDN live HLS.
# Parity with deploy/caddy/hls.caddy (repo): no-store playlist, short segment
# caching, CORS for the player on www.torrentcdn.store.
https://${HLS_HOST} {
	@playlist path /index.m3u8 */index.m3u8
	header @playlist Cache-Control "no-store, must-revalidate"
	header Cache-Control "public, max-age=2"

	header Access-Control-Allow-Origin "https://www.torrentcdn.store"
	header Access-Control-Allow-Methods "GET, HEAD, OPTIONS"
	@preflight method OPTIONS
	respond @preflight 204

	reverse_proxy mediamtx:8888

	log {
		output file /data/logs/hls_access.log
		format json
	}
}
CADDY_EOF

cat > docker-compose.yml <<COMPOSE_EOF
# Generated by deploy/oracle/bootstrap.sh — the VM-side live media stack.
# MediaMTX → backend calls go over public HTTPS to Render (rate-limit-exempt,
# hook-secret protected). HLS 8888 / API 9997 / metrics 9998 are NOT public:
# Caddy reaches 8888 over the compose network; 9997/9998 stay on loopback.
services:
  mediamtx:
    build: .
    image: torrentcdn/mediamtx:1.11.3-sh
    restart: unless-stopped
    environment:
      MTX_AUTHHTTPADDRESS: \${BACKEND_ORIGIN:-https://torrent-cdn.onrender.com}/api/internal/mediamtx/auth?secret=\${MEDIAMTX_HOOK_SECRET}
      MTX_HLSSEGMENTDURATION: 2s
      MTX_HLSPARTDURATION: 700ms
      MTX_WEBRTCADDITIONALHOSTS: ${HLS_HOST}
      MTX_APIUSER: \${MEDIAMTX_API_USER}
      MTX_APIPASS: \${MEDIAMTX_API_PASS}
      MEDIAMTX_READY_HOOK_URL: \${BACKEND_ORIGIN:-https://torrent-cdn.onrender.com}/api/internal/mediamtx
      MEDIAMTX_HOOK_SECRET: \${MEDIAMTX_HOOK_SECRET}
    ports:
      - "0.0.0.0:1935:1935"    # RTMP ingest — the ONLY public media port
      - "127.0.0.1:9997:9997"  # Control API — LOOPBACK ONLY
      - "127.0.0.1:9998:9998"  # Metrics   — LOOPBACK ONLY
    volumes:
      - ./mediamtx.yml:/mediamtx.yml:ro

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    depends_on: [mediamtx]
    ports:
      - "0.0.0.0:80:80"
      - "0.0.0.0:443:443"
      - "0.0.0.0:443:443/udp"  # HTTP/3
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config

volumes:
  caddy_data:
  caddy_config:
COMPOSE_EOF

# ── 5. Firewall: Oracle Ubuntu ships REJECT rules — insert ACCEPTs on TOP ───
echo "-- opening iptables for 80/443/1935 (and persisting)"
for PORT in 80 443 1935; do
  iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT 1 -p tcp --dport "$PORT" -j ACCEPT
  ip6tables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null || \
    ip6tables -I INPUT 1 -p tcp --dport "$PORT" -j ACCEPT
done
if command -v netfilter-persistent >/dev/null 2>&1; then
  netfilter-persistent save >/dev/null
else
  DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent >/dev/null 2>&1 || true
  command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null
fi

# ── 6. Up ───────────────────────────────────────────────────────────────────
echo "-- building + starting the stack (first build pulls ~100 MB)"
docker compose up -d --build

echo
echo "============================================================"
echo " Edge is up. Caddy will obtain the TLS cert once DNS points"
echo " ${HLS_HOST} at this VM's public IP."
echo
echo " NEXT STEPS (details: deploy/oracle/README.md):"
echo "  1. Oracle Console → instance VNIC → Security List:"
echo "     allow TCP 80, 443, 1935 from 0.0.0.0/0"
echo "  2. DNS A records → $(curl -fsSL --max-time 10 https://checkip.amazonaws.com || echo '<VM_IP>'):"
echo "       ${HLS_HOST}"
echo "       rtmp.torrentcdn.store   (RTMP needs no cert; DNS only)"
echo "  3. Render env vars (torrentcdn-backend → Environment):"
grep -E '^(MEDIAMTX_HOOK_SECRET|MEDIAMTX_API_USER|MEDIAMTX_API_PASS)=' .env \
  | sed 's/^/       /'
echo "       MEDIAMTX_RTMP_URL = rtmp://rtmp.torrentcdn.store"
echo "       MEDIAMTX_HLS_URL  = https://${HLS_HOST}"
echo "  4. Verify from OUTSIDE the VM:"
echo "       nc -vz $(curl -fsSL --max-time 10 https://checkip.amazonaws.com) 1935"
echo "       curl -sI https://${HLS_HOST}/live/<slug>/index.m3u8"
echo
echo " NOTE: Render can no longer poll MediaMTX's API (9997 is loopback"
echo " by design) — /api/health/live shows mediamtx_reachable=false and"
echo " channel status relies on the runOnReady/not-ready hooks + HLS"
echo " playlist polls, which still work. See README.md 'Known limits'."
echo "============================================================"
