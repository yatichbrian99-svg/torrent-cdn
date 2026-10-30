#!/usr/bin/env bash
# Prove the ZAP "authenticated" pass really carried the session token.
#
# The two passes return suspiciously similar alert sets — that is exactly what a
# silently-unapplied bearer header would look like. So the claim is checked
# against evidence rather than assumed: /auth/me is a JWT-only route, it is in
# the exported OpenAPI document, and both passes request it. Unauthenticated it
# answers 401; authenticated it must answer 200.
#
# Requires the CI override's `--access-logfile -` (gunicorn turns the access log
# off by default), which is why this reads `docker compose logs`.
#
#   authme.sh before   record the baseline before the authenticated pass
#   authme.sh after    require strictly more 200s than the baseline
set -euo pipefail

phase="${1:?usage: authme.sh before|after}"
state="${RUNNER_TEMP:?RUNNER_TEMP must be set}/zap/authme-count"

minimum="${MIN_AUTHME_200:-3}"
log=$(docker compose -f docker-compose.yml -f .github/zap/docker-compose.zap.yml logs backend 2>/dev/null || true)
seen=$(printf '%s\n' "$log" | grep -c '/auth/me' || true)
ok=$(printf '%s\n' "$log" | grep -c '"GET /auth/me HTTP/1.1" 200' || true)

if [ "$seen" -eq 0 ]; then
  echo "::error::the backend access log contains no /auth/me request at all, so authenticated coverage cannot be proven (check the access-logfile override)"
  exit 1
fi

if [ "$phase" = "before" ]; then
  echo "$ok" > "$state"
  echo "baseline: $seen /auth/me requests in the log, $ok answered 200 (expected 0 while unauthenticated)"
  exit 0
fi

before=$(cat "$state")
echo "after the authenticated pass: $seen /auth/me requests in the log, $ok answered 200 (baseline $before)"
if [ "$ok" -le "$before" ]; then
  echo "::error::the authenticated pass added no successful /auth/me request ($before -> $ok): the bearer token was NOT applied, so the 'authenticated' report covers the same surface as the unauthenticated one"
  exit 1
fi
if [ "$((ok - before))" -lt "$minimum" ]; then
  echo "::error::only $((ok - before)) successful /auth/me request(s) during the authenticated pass (need >= $minimum): the session did not survive the scan (a logout, or an endpoint that revokes it)"
  exit 1
fi
echo "authenticated coverage proven: $((ok - before)) request(s) to a JWT-only route answered 200 with the injected token (minimum $minimum)"
if [ "$seen" -gt 0 ]; then
  echo "of the $seen /auth/me requests seen so far, $ok answered 200 — the rest are 401s from the unauthenticated pass and from the scanner's own auth-bypass probes, which is expected"
fi
