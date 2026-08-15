#!/usr/bin/env bash
# restore_drill.sh — read-only restore-drill PRE-FLIGHT.
#
# Verifies the pieces a quarterly restore drill needs BEFORE the operator
# starts it (see RESTORE_RUNBOOK.md): the connection string resolves, the DB is
# reachable, the auth-critical tables/counts look sane, and the backup tooling
# is available. Runs SELECT-only queries — it NEVER writes to the database.
#
# Usage:
#   bash backend/scripts/restore_drill.sh                 # uses DATABASE_URL env
#   DATABASE_URL=postgresql://... bash backend/scripts/restore_drill.sh
set -u

# Load DATABASE_URL from .env if present and not already set (never print it).
if [ -z "${DATABASE_URL:-}" ] && [ -f .env ]; then
  export DATABASE_URL=$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2- | tr -d '"')
fi

echo "=== Restore drill pre-flight ==="

if [ -z "${DATABASE_URL:-}" ]; then
  echo "[FAIL] DATABASE_URL is not set (export it or add it to backend/.env)."
  exit 1
fi

# 1. Tooling
for tool in psql pg_dump pg_restore; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "[OK] $tool available: $(command -v $tool)"
  else
    echo "[FAIL] $tool missing — install postgresql-client (v14+)."
    HAS_TOOLS=0
  fi
done
if [ "${HAS_TOOLS:-1}" = "0" ]; then exit 1; fi

# 2. Connectivity + sanity (SELECT-only)
echo "--- connectivity ---"
if psql "$DATABASE_URL" -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "[OK] Database reachable (SELECT 1 OK)."
else
  echo "[FAIL] Cannot connect to the database (check DATABASE_URL / network)."
  exit 1
fi

echo "--- auth-critical table counts (compare with /admin/stats in prod) ---"
psql "$DATABASE_URL" -tAc \
  "SELECT 'users='||COUNT(*) FROM users UNION ALL SELECT 'uploads='||COUNT(*) FROM uploads UNION ALL SELECT 'api_keys='||COUNT(*) FROM api_keys UNION ALL SELECT 'notifications='||COUNT(*) FROM notifications;"

echo "--- schema sanity: MFA columns present on users ---"
psql "$DATABASE_URL" -tAc \
  "SELECT column_name FROM information_schema.columns WHERE table_name='users' AND column_name IN ('mfa_secret','mfa_enabled','mfa_backup_codes') ORDER BY 1;"

echo
echo "Next steps (read the full runbook: backend/scripts/RESTORE_RUNBOOK.md):"
echo "  1. Render dashboard → Postgres → Backups → confirm a daily backup + retention."
echo "  2. Download the latest backup (never to a shared location)."
echo "  3. pg_restore --no-owner --no-privileges into a SCRATCH database."
echo "  4. Verify row counts + a users row with mfa_enabled round-trips."
echo "  5. Drop the scratch DB, delete the downloaded backup, log the drill."
echo
echo "Pre-flight complete — safe to proceed with the drill."
