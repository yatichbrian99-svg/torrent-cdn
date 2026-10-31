#!/usr/bin/env python3
"""SIMULATED email provider — a local stand-in for Resend's ``POST /emails``.

Why this exists
---------------
Email verification is a security boundary: an account must stay unverified
until the link that was *actually sent* is used. Testing that path with a real
Resend key would send real mail and require a real inbox, and testing it with no
provider at all exercises the auto-verify fallback instead (a different branch).

This server accepts the exact request the ``resend`` SDK makes, records the
message to a JSONL file, and answers ``{"id": ...}`` like the real API. The
journey (``scripts/live_ai_journey.py --email-stub-url ...``) then extracts the
verification link from the recorded message and completes the real flow:
register → still unverified → emailed token → ``POST /auth/verify-email``.

It is a TEST STAND-IN, never a product component: the backend does not import
it, and it is only reachable through ``sitecustomize.py`` in this directory,
which redirects the SDK **only** when ``TCDN_FAKE_RESEND_URL`` is set.

Usage:
    python scripts/dev/fake_resend.py --port 8765 --out /tmp/tcdn-ai-emails.jsonl
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT_PATH = Path("/tmp/tcdn-ai-emails.jsonl")
_LOCK = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "role": "fake-resend"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.split("?")[0] not in {"/emails", "/emails/batch"}:
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            message = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid json"})
            return
        record = {
            "id": f"fake-{uuid.uuid4().hex[:12]}",
            "authorization_present": bool(self.headers.get("authorization")),
            "message": message,
        }
        with _LOCK, OUT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        self._json(200, {"id": record["id"]})


def serve(port: int, out: Path) -> None:
    global OUT_PATH
    OUT_PATH = out
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"fake-resend listening on http://127.0.0.1:{port} -> {OUT_PATH}", flush=True)
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out", default="/tmp/tcdn-ai-emails.jsonl")
    args = parser.parse_args()
    try:
        serve(args.port, Path(args.out))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
