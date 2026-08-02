#!/usr/bin/env bash
# =============================================================================
# check_og_image.sh — fail CI if link-preview cards would render broken.
#
# Guards against a regression that shipped a blank preview card everywhere:
# og:image pointed at a dynamic route that emitted a 0-byte PNG, and
# twitter:image pointed at /og-image.png which didn't exist (404). Both modes
# are covered here:
#
#   1. The og:image / twitter:image URLs in layout.tsx metadata must resolve
#      to a real file under public/ (no dynamic routes, no 404s).
#   2. That file must be a genuine PNG with a non-empty body (no 0-byte
#      images, which make Twitter/X, LinkedIn, Slack, Discord, and WhatsApp
#      render blank previews).
#
# Usage:
#   bash frontend/scripts/check_og_image.sh
#
# Exit codes: 0 = preview cards OK, 1 = broken preview asset, 2 = usage error
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAYOUT="$FRONTEND_ROOT/app/layout.tsx"
PUBLIC_DIR="$FRONTEND_ROOT/public"

FAIL=0
check() {  # check <pass:0|1> <message>
  if [ "$1" = "0" ]; then
    echo "  ✅  $2"
  else
    echo "  ❌  $2"
    FAIL=$((FAIL + 1))
  fi
}

[ -f "$LAYOUT" ] || { echo "layout.tsx not found: $LAYOUT" >&2; exit 2; }

# --- Extract the image URLs from layout.tsx metadata --------------------------
# og:image  →  url: 'https://www.torrentcdn.store/og-image.png'
# twitter:  →  images: ['https://www.torrentcdn.store/og-image.png']
# Both quote styles are accepted (['"]), so a formatter reformat can't
# false-fail the guard. NB: the openGraph block also has a plain
# `url: '<site>'` (no image path), so the og: extraction must filter for
# image URLs (.png/.jpg/.webp) to skip it. Assumption: layout.tsx declares
# exactly ONE og image and ONE twitter image — if more are added, the first
# match wins and the guard may need widening.
OG_URL="$(grep -oE "url: [\"'][^\"']+[\"']" "$LAYOUT" | grep -E "\.(png|jpe?g|webp)[\"']$" | head -1 | sed "s/url: [\"']//; s/[\"']$//")"
TW_URL="$(grep -oE "images: \[[\"'][^\"']+" "$LAYOUT" | head -1 | sed "s/images: \[[\"']//")"

# Note: only the og:image (openGraph.images) and twitter:image entries are
# guarded. If the metadata ever gains additional image entries (e.g. a
# favicon), extend the extraction below.
echo "── OG IMAGE GUARD ──"
[ -n "$OG_URL" ] && check 0 "og:image → ${OG_URL}" || check 1 "og:image URL not found in layout.tsx metadata"
[ -n "$TW_URL" ] && check 0 "twitter:image → ${TW_URL}" || check 1 "twitter:image URL not found in layout.tsx metadata"

# --- Normalize a metadata URL to a path under public/ --------------------------
# Accepts absolute (https://www.torrentcdn.store/...) or root-relative (/...)
# URLs; rejects dynamic-route-style URLs that don't map into public/.
url_to_rel() {  # url_to_rel <url> → echoes relative path, empty on failure
  local url="$1" rel=""
  case "$url" in
    https://www.torrentcdn.store/*) rel="${url#https://www.torrentcdn.store/}" ;;
    http://localhost:3000/*)        rel="${url#http://localhost:3000/}" ;;
    /*)                             rel="${url#/}" ;;
    *)                              rel="" ;;
  esac
  echo "$rel"
}

for entry in "og:image|$OG_URL" "twitter:image|$TW_URL"; do
  label="${entry%%|*}"
  url="${entry#*|}"
  [ -n "$url" ] || continue
  rel="$(url_to_rel "$url")"
  if [ -z "$rel" ]; then
    check 1 "$label → '$url' is not absolute (torrentcdn.store) or root-relative — dynamic-route URLs render blank previews"
    continue
  fi
  if [ -f "$PUBLIC_DIR/$rel" ]; then
    check 0 "$label → public/$rel exists"
  else
    check 1 "$label → public/$rel NOT FOUND (404 preview card — add the asset or fix the URL)"
  fi
done

# --- The asset must be a genuine, non-empty PNG --------------------------------
OG_REL="$(url_to_rel "$OG_URL")"
if [ -n "$OG_REL" ] && [ -f "$PUBLIC_DIR/$OG_REL" ]; then
  SIZE=$(wc -c < "$PUBLIC_DIR/$OG_REL")
  MAGIC=$(head -c 4 "$PUBLIC_DIR/$OG_REL" | od -An -tx1 | tr -d ' \n')
  [ "$MAGIC" = "89504e47" ] && check 0 "PNG magic bytes present (89504e47)" \
    || check 1 "PNG magic bytes (got $MAGIC — expected 89504e47; not a real PNG)"
  [ "$SIZE" -gt 0 ] && check 0 "PNG body non-empty (${SIZE} bytes)" \
    || check 1 "PNG body is 0 bytes (blank preview card — regenerate the asset)"
else
  check 1 "OG file missing — cannot verify PNG integrity"
fi

echo ""
if [ "$FAIL" -gt 0 ]; then
  echo "✋  $FAIL problem(s) — link preview cards would render broken. Fix before merging."
  exit 1
fi
echo "✅  OG image guard passed — preview cards will render."
exit 0
