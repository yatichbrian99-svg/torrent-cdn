#!/usr/bin/env bash
# Prove the ZAP "authenticated" pass really carried the session token.
#
# The two passes return suspiciously similar alert sets — that is exactly what a
# silently-unapplied bearer header would look like. So the claim is checked
# against evidence rather than assumed: /auth/referral is a JWT-only, read-only
# route, it is in the exported OpenAPI document, and both passes request it.
# Unauthenticated it answers 401; authenticated it must answer 200.
#
# Why not /auth/me: that path also exposes DELETE /auth/me (delete your own
# account), which the scan called once and therefore had to be excluded from the
# authenticated context — deleting the user cascades to ai_accounts and
# ai_api_keys and killed the AI key seeded for the third pass. /auth/referral
# has no destructive sibling, so it can stay in scope and still prove the header
# landed.
#
# Requires the CI override's `--access-logfile -` (gunicorn turns the access log
# off by default), which is why this reads `docker compose logs`.
#
#   authme.sh before   record the baseline before the authenticated pass
#   authme.sh after    require strictly more 200s than the baseline
set -euo pipefail

phase="${1:?usage: authme.sh before|after}"
state="${RUNNER_TEMP:?RUNNER_TEMP must be set}/zap/authme-count"
route=/auth/referral

log=$(docker compose -f docker-compose.yml -f .github/zap/docker-compose.zap.yml logs backend 2>/dev/null || true)
seen=$(printf '%s\n' "$log" | grep -c "$route" || true)
ok=$(printf '%s\n' "$log" | grep -c "\"GET $route HTTP/1.1\" 200" || true)

if [ "$seen" -eq 0 ]; then
  echo "::error::the backend access log contains no $route request at all, so authenticated coverage cannot be proven (check the access-logfile override)"
  exit 1
fi

if [ "$phase" = "before" ]; then
  echo "$ok" > "$state"
  echo "baseline: $seen $route requests in the log, $ok answered 200 (expected 0 while unauthenticated)"
  exit 0
fi

before=$(cat "$state")
echo "after the authenticated pass: $seen $route requests in the log, $ok answered 200 (baseline $before)"
# Strictly greater is the check: the count is printed rather than turned into a
# guessed threshold, because a route this narrow only receives the proxy's
# readiness probe plus whatever the scan actually issues. What must hold is that
# the authenticated window added at least one 200 to a JWT-only route — the
# probe alone satisfies that only because the proxy, not the caller, supplies the
# token.
if [ "$ok" -le "$before" ]; then
  echo "::error::the authenticated pass added no successful $route request ($before -> $ok): the bearer token was NOT applied, so the 'authenticated' report covers the same surface as the unauthenticated one"
  exit 1
fi
echo "authenticated coverage proven: $((ok - before)) request(s) to a JWT-only route answered 200 with the injected token (baseline $before -> $ok)"
echo "of $seen $route requests in the whole log, $ok answered 200 — the rest are the unauthenticated pass (401) and the scanner's own auth-bypass probes, which is expected"
