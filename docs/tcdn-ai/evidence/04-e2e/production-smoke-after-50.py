#!/usr/bin/env python3
"""Non-authenticated production smoke for the flag-off console merge (PR #50).

GET/POST only, no credentials, no data changes (NUL-byte bodies are rejected
before any write). Prints a pass/fail line per item and a summary.
"""
from __future__ import annotations

import http.client
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP = "https://www.torrentcdn.store"
API = "https://torrent-cdn.onrender.com"
UA = "tcdn-audit-smoke/1.0 (+non-authenticated)"

results: list[tuple[str, bool, str]] = []


def req(method: str, url: str, *, body: bytes | None = None, headers: dict | None = None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    ctx = ssl.create_default_context()
    r = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=45, context=ctx) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}")


def main() -> int:
    print(f"PRODUCTION SMOKE (non-authenticated) — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print("ENABLE_TCDN_AI is not set in production; nothing below authenticates.\n")

    print("== 1. health ==")
    s, _, b = req("GET", f"{API}/health")
    check("GET /health", s == 200 and '"healthy"' in b, f"[{s}] {b[:80]}")
    s, _, b = req("GET", f"{API}/api/health/live")
    check("GET /api/health/live", s == 200 and '"status":"healthy"' in b, f"[{s}] {b[:120]}")

    print("\n== 2. NUL-byte input is still a 400, not a 500 ==")
    nul = json.dumps({"email": "a\u0000b@example.com", "password": "x" * 12}).encode()
    for path in ("/auth/login", "/auth/register", "/auth/forgot-password"):
        s, _, b = req("POST", f"{API}{path}", body=nul, headers={"Content-Type": "application/json"})
        check(f"POST {path} (NUL email)", s == 400, f"[{s}] {b[:60]}")

    print("\n== 3. the AI surface must not exist ==")
    for path in ("/api/ai/v1/status", "/api/ai/v1/models", "/api/ai/v1/never-existed"):
        s, _, _ = req("GET", f"{API}{path}")
        check(f"GET {API.split('//')[1]}{path}", s == 404, f"[{s}]")
    for path in ("/api/ai/v1/status", "/api/ai/v1/never-existed", "/api/api/ai/v1/status"):
        s, _, _ = req("GET", f"{APP}{path}")
        check(f"through the app origin: {path}", s == 404, f"[{s}]")
    for path in ("/v1/chat/completions", "/v1/models"):
        s, _, b = req("GET", f"{APP}{path}")
        check(f"OpenAI surface flag-off: GET {path}", s in (404, 405), f"[{s}] {b[:60]}")

    print("\n== 4. public pages ==")
    for path, want in (
        ("/", 200), ("/demo", 200), ("/docs", 200), ("/architecture", 200),
        ("/pricing", 404), ("/privacy", 200), ("/terms", 200),
        ("/cookies", 200), ("/dmca", 200),
    ):
        s, _, _ = req("GET", f"{APP}{path}")
        check(f"GET {path}", s == want, f"[{s}] want {want}")

    print("\n== 5. /ai exists but is gated, and no page links to it ==")
    # The panel itself is rendered client-side after the flag probe 404s, so the
    # server HTML only has the console shell. The panel text is asserted in
    # production-smoke-after-50-browser.mjs, which drives a real browser.
    s, _, body = req("GET", f"{APP}/ai")
    shell = "ai-console" in body.lower() or "AI console" in body
    check("GET /ai serves the gated shell", s == 200 and shell, f"[{s}] shell present={shell}")
    for path in ("/", "/demo", "/docs", "/architecture", "/privacy"):
        _, _, page = req("GET", f"{APP}{path}")
        check(f"no AI nav link on {path}", 'href="/ai"' not in page, "href=\"/ai\" absent")

    print("\n== 6. auth endpoints behave as before ==")
    bad = json.dumps({"email": "nobody-smoke@example.com", "password": "wrong-password"}).encode()
    s, _, b = req("POST", f"{API}/auth/login", body=bad, headers={"Content-Type": "application/json"})
    check("POST /auth/login (bad credentials)", s in (401, 429), f"[{s}] {b[:60]}")
    s, _, b = req("GET", f"{API}/auth/me")
    check("GET /auth/me (no token)", s == 401, f"[{s}] {b[:60]}")
    s, _, b = req("POST", f"{APP}/api/auth/login", body=bad, headers={"Content-Type": "application/json"})
    check("POST through the app origin: /api/auth/login (bad credentials)", s in (401, 404, 429), f"[{s}]")

    print("\n== 7. live routes keep their prefix (unchanged proxy behaviour) ==")
    s, _, b = req("GET", f"{APP}/api/live/channels")
    check("GET /api/live/channels reaches the backend", s in (200, 401, 403), f"[{s}] {b[:60]}")
    s, _, _ = req("GET", f"{APP}/api/live/never-existed")
    check("GET /api/live/never-existed", s == 404, f"[{s}]")

    print("\n== 8. headers (HTML 200) ==")
    s, h, _ = req("GET", f"{APP}/")
    hdr = {k.lower(): v for k, v in h.items()}
    need = [
        "content-security-policy", "cross-origin-opener-policy", "permissions-policy",
        "referrer-policy", "strict-transport-security", "x-content-type-options", "x-frame-options",
    ]
    missing = [k for k in need if k not in hdr]
    check("security headers on HTML 200", not missing, f"missing={missing or 'none'}")
    check("no x-powered-by", "x-powered-by" not in hdr, f"x-powered-by={hdr.get('x-powered-by', 'absent')}")

    print("\n== 9. headers (backend 404) ==")
    # The backend never sent COOP on API responses (that is the frontend/edge's
    # header); requiring it here would fail on a healthy deploy, as it did on the
    # first run of this script. The rest of the set is the backend's own.
    backend_need = [k for k in need if k != "cross-origin-opener-policy"]
    _, h, _ = req("GET", f"{API}/api/ai/v1/status")
    hdr = {k.lower(): v for k, v in h.items()}
    missing = [k for k in backend_need if k not in hdr]
    check("security headers on backend 404", not missing, f"missing={missing or 'none'}")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{'=' * 60}")
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
    print("done", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
