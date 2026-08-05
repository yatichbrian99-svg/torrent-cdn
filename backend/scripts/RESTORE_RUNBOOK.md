# Database Restore Drill — Runbook

**Goal:** prove that a database backup can actually be restored, and that the
restored data is usable — before an incident forces it. The database is
**Render-managed PostgreSQL** (via the Supabase connection string in
production); automated daily backups + point-in-time recovery are provider
controls. This runbook turns "provider says backups exist" into a verified,
repeatable drill.

**Cadence:** quarterly (every 3 months), plus immediately after any schema
migration that touches `users` / `uploads` / `api_keys` / `notifications`.

---

## 0. Prerequisites

- `psql`/`pg_dump` ≥ 14 installed locally (or a throwaway Postgres container).
- Access to the Render dashboard (or the `render` CLI) to download a backup.
- A scratch database to restore INTO — **never restore over production**.

## 1. Verify backups are being produced (5 min)

In the Render dashboard:

1. Project → your Postgres instance → **Backups** tab.
2. Confirm a daily backup exists for today (or the most recent 24h).
3. Check retention is ≥ 7 days and note the PITR window.
4. Confirm the backup is at **rest encryption** (Render-managed) — recorded in
   the drill log below.

If no backup exists, **stop the drill and raise a ticket** — this is the
finding the drill exists to catch.

## 2. Download a backup (10 min)

Use the Render dashboard (Backups → download) or the CLI:

```bash
render psql backups list <db-name>
render psql backups download <backup-id> --output ./drill/backup.dump
ls -la ./drill/backup.dump            # non-zero size = good sign
sha256sum ./drill/backup.dump > ./drill/backup.dump.sha256
```

> **Never** download a backup to a shared/untracked location. `./drill/` is
> gitignored-by-convention; delete it after the drill.

## 3. Restore into a scratch database (20 min)

```bash
createdb torrentcdn_drill
pg_restore --verbose --no-owner --no-privileges \
  -d torrentcdn_drill ./drill/backup.dump 2>&1 | tail -20
```

The restore must exit 0 with **no FATAL errors** (WARNINGs about comments/ACLs
are fine).

## 4. Verify the restored data is usable (10 min)

```bash
# Row counts match production expectations (roughly):
psql -d torrentcdn_drill -c "SELECT COUNT(*) AS users FROM users;"
psql -d torrentcdn_drill -c "SELECT COUNT(*) AS uploads FROM uploads;"
psql -d torrentcdn_drill -c "SELECT COUNT(*) AS api_keys FROM api_keys;"

# Integrity: a restore that "succeeds" but silently drops rows is worse than
# failure. Compare against the production counts from /admin/stats.
psql -d torrentcdn_drill -c "\d+ users"          # columns present incl. mfa_secret/mfa_enabled
psql -d torrentcdn_drill -c "SELECT count(*) FROM users WHERE email IS NOT NULL;"

# Functional smoke: a user row with a valid-looking password_hash and one with
# mfa_enabled = TRUE round-trips (proves auth-relevant columns survived).
psql -d torrentcdn_drill -c "SELECT email, mfa_enabled FROM users ORDER BY created_at DESC LIMIT 3;"
```

## 5. Clean up (5 min)

```bash
dropdb torrentcdn_drill
rm -rf ./drill
```

## 6. Log the drill (required)

Record in SECURITY.md's Hardening Log (or a `docs/` drill log):

| Item | Value |
|---|---|
| Date | YYYY-MM-DD |
| Backup id / date | … |
| Restore target | scratch db `torrentcdn_drill` |
| Result | ✅ restored + row counts verified / ❌ (what failed) |
| RTO observed | time from backup download → usable data |
| RPO observed | backup age at drill start |

---

## Incident recovery (emergency)

1. Render dashboard → Postgres → Backups → restore to a **new instance**.
2. Point `DATABASE_URL` at the restored instance (Render env var) and redeploy.
3. Run `backend/scripts/post_deploy_check.sh` + `/health` before announcing.
4. Note: presign/chunked upload sessions and rate-limit counters live in Redis
   (volatile-lru, no persistence) — after a restore they reset; that is
   expected and safe (uploads rows are durable in Postgres).
