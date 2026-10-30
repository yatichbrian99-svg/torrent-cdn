#!/usr/bin/env python3
"""Register the throwaway account ZAP authenticates as, and prove the token works.

Stdlib only: the GitHub runner's python3 has no httpx, and this script must not
need the backend's virtualenv (it talks HTTP, nothing else).

What it does, in order:

1. waits for ``/health``;
2. asserts ``/api/ai/v1/status`` reports the AI flag ON and the routes mounted
   — scanning a stack whose AI surface silently failed to mount would produce a
   green report that proves nothing;
3. registers ``zap-scan@example.test`` (the real signup path, so the scanned
   surface is the shipped one);
4. logs in and refuses to continue if the response is the MFA gate
   (``needs_setup``) — the workflow sets ``MFA_REQUIRED=false`` and says so;
5. calls ``/auth/me`` with the token and requires 200;
6. writes the token to ``--token-out`` with 0600 permissions.

The token is never printed and never written inside the checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_EMAIL = "zap-scan@example.test"
DEFAULT_PASSWORD = "ZapScan-3x!7-pleasedonotship"


def _call(base: str, method: str, path: str, body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode("utf-8", "replace")[:300]
        return exc.code, parsed


def _fail(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(1)


def wait_for_health(base: str, seconds: int) -> None:
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        try:
            status, body = _call(base, "GET", "/health")
            if status == 200:
                print(f"[ OK ] /health {body}")
                return
            last = f"HTTP {status}"
        except Exception as exc:  # connection refused while gunicorn boots
            last = str(exc)
        time.sleep(2)
    _fail(f"/health never answered within {seconds}s (last: {last})")


def check_ai_surface(base: str) -> None:
    status, body = _call(base, "GET", "/api/ai/v1/status")
    if status != 200:
        _fail(f"/api/ai/v1/status returned HTTP {status}; ENABLE_TCDN_AI did not take effect")
    flag = (body or {}).get("feature_flag", {})
    if not (flag.get("on") and flag.get("routes_mounted")):
        _fail(f"AI flag is not on/routes not mounted: {flag}")
    print(f"[ OK ] ai flag on, routes mounted, commit {(body or {}).get('commit')}")


def register(base: str, email: str, password: str) -> None:
    status, body = _call(
        base,
        "POST",
        "/auth/register",
        {"email": email, "password": password, "terms_accepted": True},
    )
    detail = json.dumps(body)[:200] if body is not None else ""
    if status in (200, 201):
        print(f"[ OK ] registered {email}")
        return
    if status in (400, 409) and "exist" in detail.lower():
        print(f"[ OK ] account already exists ({email})")
        return
    _fail(f"registration returned HTTP {status}: {detail}")


def login(base: str, email: str, password: str) -> str:
    status, body = _call(base, "POST", "/auth/login", {"email": email, "password": password})
    if status != 200:
        _fail(f"login returned HTTP {status}: {json.dumps(body)[:200]}")
    if isinstance(body, dict) and body.get("mfa_required"):
        _fail(
            "login answered the 2FA gate (needs_setup="
            f"{body.get('needs_setup')}); the ZAP stack sets MFA_REQUIRED=false on "
            "purpose — this stack is not the one the workflow configured"
        )
    token = (body or {}).get("access_token")
    if not token:
        _fail(f"login returned no access_token: {json.dumps(body)[:200]}")
    print("[ OK ] login minted a session token")
    return token


def check_token(base: str, token: str) -> None:
    status, body = _call(base, "GET", "/auth/me", token=token)
    if status != 200:
        _fail(f"/auth/me returned HTTP {status} with the seeded token: {json.dumps(body)[:200]}")
    print(f"[ OK ] /auth/me 200 for {(body or {}).get('email')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--token-out", required=True)
    parser.add_argument("--wait", type=int, default=180)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    wait_for_health(base, args.wait)
    check_ai_surface(base)
    register(base, args.email, args.password)
    token = login(base, args.email, args.password)
    check_token(base, token)

    fd = os.open(args.token_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    print(f"[ OK ] token written to {args.token_out} (0600, runner temp only)")


if __name__ == "__main__":
    main()
