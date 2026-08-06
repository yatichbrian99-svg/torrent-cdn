#!/usr/bin/env bash
# =============================================================================
# Secret rotation helper — TorrentCDN
#
# Generates fresh JWT/DB/Redis secrets, validates the JWT against the backend's
# fail-closed rules (>=32 chars, not 'changeme'), and prints a copy-paste
# checklist for the Render dashboard + Supabase/Paystack/Upstash web consoles.
#
# The dashboard-only steps (Supabase DB password reset, Supabase Storage S3
# keys, Paystack key roll, Upstash reset, GitHub PAT revocation) can only be done
# by an account holder — this script prepares everything else.
#
# Usage:
#   bash backend/scripts/rotate_secrets.sh            # full output (secrets visible)
#   bash backend/scripts/rotate_secrets.sh --json     # machine-readable JSON
# =============================================================================
set -euo pipefail

JWT_SECRET="$(openssl rand -base64 48)"
DB_PASSWORD="$(openssl rand -base64 24 | tr '+/' '-_' | tr -d '=')"
REDIS_PASSWORD="$(openssl rand -base64 24 | tr '+/' '-_' | tr -d '=')"

# Fail-closed validation mirrors backend/app/config.py:
#   jwt_secret must be >= 32 chars and not equal to 'changeme'
if [ "${#JWT_SECRET}" -lt 32 ] || [ "$JWT_SECRET" = "changeme" ]; then
  echo "ERROR: generated JWT_SECRET failed validation" >&2
  exit 1
fi

# Supabase pooled connection strings are of the form:
#   postgresql://postgres.<ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:6543/postgres
# The alphanumeric password above never needs URL-encoding. If you choose your
# own password with special chars, percent-encode them (@ -> %40, : -> %3A, / -> %2F).

if [ "${1:-}" = "--json" ]; then
  python3 - "$JWT_SECRET" "$DB_PASSWORD" "$REDIS_PASSWORD" <<'PY'
import json, sys
jwt_secret, db_password, redis_password = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({
    "JWT_SECRET": jwt_secret,
    "DATABASE_URL_PASSWORD": db_password,
    "REDIS_PASSWORD": redis_password,
    "notes": {
        "DATABASE_URL": "supabase pooled URL: postgresql://postgres.<ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:6543/postgres",
        "MINIO_ACCESS_KEY/SECRET_KEY": "generate in Supabase dashboard -> Storage -> Settings -> S3 Access Keys",
        "PAYSTACK_SECRET_KEY": "roll in Paystack dashboard -> Settings -> API Keys & Webhooks",
        "REDIS_URL": "reset password in Upstash console, full redis:// URL embeds it",
        "GITHUB_PAT": "revoke old ghp_ token at github.com/settings/tokens",
    }
}, indent=2))
PY
  exit 0
fi

cat <<EOF
============================================================
  TORRENTCDN SECRET ROTATION  (generated $(date -u +%Y-%m-%dT%H:%MZ))
============================================================

  ⚠️  SENSITIVE: The values below are live secrets. Copy them DIRECTLY into
     the Render dashboard. Do NOT paste this output into chats, tickets, or
     shared logs — that is how secrets leak (and how the originals leaked).


STEP 1 — Paste these into Render (torrent-cdn -> Environment):

  JWT_SECRET=${JWT_SECRET}
  DATABASE_URL=postgresql://postgres.<YOUR_REF>:${DB_PASSWORD}@aws-0-<REGION>.pooler.supabase.com:6543/postgres
  # REDIS_URL: Upstash generates the password in its console — use the exact
  # redis:// URL from Upstash (suggested value below is only a fallback):
  # REDIS_URL=redis://default:${REDIS_PASSWORD}@<upstash-endpoint>:6379

  (edit all values, then Save ONCE -> one redeploy)

STEP 2 — Dashboard-only actions (require account logins, cannot be scripted):

  [ ] Supabase  -> Project Settings -> Database -> Reset database password
                 -> use: ${DB_PASSWORD}
  [ ] Supabase  -> Storage -> Settings -> S3 Access Keys -> Generate new keys
                 -> paste into MINIO_ACCESS_KEY / MINIO_SECRET_KEY in Render
  [ ] Paystack  -> Settings -> API Keys & Webhooks -> Roll key  (new sk_live_...)
                 -> paste into PAYSTACK_SECRET_KEY (same key signs webhooks)
                 -> re-save the webhook on Paystack if the endpoint was recreated
  [ ] Upstash   -> console -> reset password -> update REDIS_URL in Render
  [ ] GitHub    -> github.com/settings/tokens -> revoke old ghp_ token

STEP 3 — Verify after the redeploy goes green:

  curl -s https://torrent-cdn.onrender.com/health            # -> healthy
  curl -s https://torrent-cdn.onrender.com/auth/me -H "Authorization: Bearer <OLD_TOKEN>"
                                                            # -> 401 (old token invalid)

============================================================
  NOTE: rotating JWT_SECRET logs out ALL users (old tokens die).
  In-flight verification/reset links also die (backend derives those
  secrets from JWT_SECRET via sha256) — users must request new ones.
============================================================
EOF
