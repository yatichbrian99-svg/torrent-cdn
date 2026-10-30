#!/usr/bin/env python3
"""Inject the seeded session token for ZAP's authenticated pass.

ZAP's automation framework has no first-class bearer-token authentication and
the Replacer rule approach (`-config replacer.full_list(0)…`) was tried first:
the scan ran, `/auth/me` was requested 46 times, and **every** request still
answered 401. Rather than claim an authenticated scan that was not one, the
authenticated pass is pointed at this proxy, which adds the header on the way
through. Every request ZAP makes then really carries the session.

Deliberately dumb: no caching, no rewriting, no TLS (loopback only), one log
line per request (method, path, status — never the token).
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import sys
import threading

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tcdn-zap-auth-proxy"

    token: str = ""
    upstream: tuple[str, int] = ("127.0.0.1", 8000)

    # BaseHTTPRequestHandler's default logging is one noisy line per request to
    # stderr; keep a compact one instead. Never log the header we add.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("auth-proxy %s\n" % (fmt % args))

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        sys.stderr.write(f"auth-proxy {self.command} {self.path} -> {code}\n")

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP and key.lower() != "host"
        }
        headers["Authorization"] = f"Bearer {self.token}"

        connection = http.client.HTTPConnection(*self.upstream, timeout=120)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            status = response.status
        except Exception as exc:  # a dead upstream must be visible, not silent
            sys.stderr.write(f"auth-proxy {self.command} {self.path} -> 502 {exc}\n")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        finally:
            connection.close()

        try:
            self.send_response(status)
            for key, value in response.getheaders():
                if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # The scanner closed the connection (its own timeout). Not an error
            # in the proxy; the backend already answered.
            pass

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_PATCH = _forward
    do_DELETE = _forward
    do_OPTIONS = _forward
    do_HEAD = _forward


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--listen", type=int, default=8001)
    parser.add_argument("--upstream-port", type=int, default=8000)
    args = parser.parse_args()

    with open(args.token_file) as handle:
        Handler.token = handle.read().strip()
    if not Handler.token:
        raise SystemExit("token file is empty")
    Handler.upstream = ("127.0.0.1", args.upstream_port)

    server = Server(("127.0.0.1", args.listen), Handler)
    print(
        f"auth proxy: 127.0.0.1:{args.listen} -> {Handler.upstream[0]}:{Handler.upstream[1]} "
        "(bearer injected per request)",
        flush=True,
    )
    threading.current_thread().name = "auth-proxy"
    server.serve_forever()


if __name__ == "__main__":
    main()
