#!/usr/bin/env python3
"""Keep-alive pinger for the Render web service.

Prevents free-tier cold starts by hitting /health every 5 minutes.
Runs as a Render Cron Job (plan: free, schedule: "*/5 * * * *").

The cron service and the web service share the same Render region (Oregon),
so internal DNS resolves instantly. The pinger makes a single GET to /health,
logs the result, and exits — total runtime < 2 seconds, well under the
free-tier cron's 512MB/0.1CPU budget.
"""

import os
import sys
import urllib.error
import urllib.request

# The web service's URL. Render injects this automatically when you use
# `fromService` in render.yaml, but the cron service needs the URL
# directly. We use the public onrender.com URL since the cron job runs
# in the same region.
WEB_URL = os.environ.get(
    "WEB_SERVICE_URL",
    "https://torrent-cdn.onrender.com",
).rstrip("/")

HEALTH_ENDPOINT = f"{WEB_URL}/health"
TIMEOUT_SECONDS = 10


def ping():
    """Hit /health and return (success: bool, status_code: int, detail: str)."""
    try:
        req = urllib.request.Request(
            HEALTH_ENDPOINT,
            method="GET",
            headers={"User-Agent": "TorrentCDN-KeepAlive/1.0"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, resp.status, body.strip()
    except urllib.error.HTTPError as e:
        return False, e.code, str(e.reason)
    except urllib.error.URLError as e:
        return False, 0, str(e.reason)
    except Exception as e:
        return False, 0, str(e)


def main():
    success, status, detail = ping()
    if success:
        print(f"[KEEPALIVE OK] {HEALTH_ENDPOINT} -> {status}: {detail}")
    else:
        print(f"[KEEPALIVE FAIL] {HEALTH_ENDPOINT} -> {status}: {detail}", file=sys.stderr)
        # Don't exit non-zero — a single failed ping shouldn't fail the cron job.
        # The next ping in 5 minutes will try again.
    sys.exit(0)


if __name__ == "__main__":
    main()
