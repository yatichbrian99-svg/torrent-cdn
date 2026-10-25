#!/usr/bin/env python3
"""Full-history, all-refs secret scan (blob level).

Scans the content of every blob reachable from every local and remote ref, not
just the working tree and not just the current branch's diff. Matches are
redacted before printing, so the report itself is safe to commit.

Patterns are the high-signal set from backend/scripts/secret_scan.sh plus
credential-in-URL and private-key-file checks.
"""
from __future__ import annotations

import re
import subprocess
import sys

PATTERNS = [
    ("GitHub PAT (classic)", re.I, r"ghp_[A-Za-z0-9]{36}"),
    ("GitHub PAT (fine-grained)", re.I, r"github_pat_[A-Za-z0-9_]{22,}"),
    ("GitHub OAuth/server token", re.I, r"gh[ousr]_[A-Za-z0-9]{36}"),
    ("AWS access key ID", 0, r"AKIA[0-9A-Z]{16}"),
    ("AWS secret access key", re.I,
     r"aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}"),
    ("Stripe/Paystack live secret key", 0, r"sk_live_[0-9a-zA-Z]{16,}"),
    ("Stripe restricted key", 0, r"rk_live_[0-9a-zA-Z]{16,}"),
    ("Stripe webhook secret", 0, r"whsec_[0-9a-zA-Z]{16,}"),
    ("SendGrid API key", 0, r"SG\.[A-Za-z0-9_-]{16,}"),
    ("Slack token", 0, r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    ("Slack webhook", 0, r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{40,}"),
    ("npm access token", 0, r"npm_[A-Za-z0-9]{36}"),
    ("PyPI upload token", 0, r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{30,}"),
    ("Google API key", 0, r"AIza[0-9A-Za-z_-]{35}"),
    ("Twilio API key", 0, r"SK[0-9a-fA-F]{32}"),
    ("Resend API key", 0, r"re_[a-z0-9]{23,}"),
    ("Discord webhook", 0,
     r"https://discord(app)?\.com/api/webhooks/[0-9]{17,19}/[A-Za-z0-9_-]{60,}"),
    ("Private key block", 0,
     r"-----BEGIN (RSA|EC|OPENSSH|DSA|PGP) PRIVATE KEY-----"),
]

# Reviewed-class patterns: credential-shaped, but commonly test fixtures. A hit
# is reported for review rather than treated as a leak.
REVIEW_PATTERNS = [
    ("DSN with password in URL", 0,
     r"\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s'\"<>]*:[^\s'\"<>@/]+@"),
    ("hardcoded password/secret assignment", re.I,
     r"\b(password|passwd|secret|api_key|apikey|token)\s*[=:]\s*['\"][^'\"\s]{8,}['\"]"),
]

MAX_BLOB = 4 * 1024 * 1024


def git(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], input=data, capture_output=True, check=True
    ).stdout


def mask(text: str, pattern: re.Pattern[str]) -> str:
    return pattern.sub("***REDACTED***", text)


def main() -> int:
    refs = git("for-each-ref", "--format=%(refname)").decode().split()
    commits = git("rev-list", "--all").decode().split()
    objects = git("rev-list", "--objects", "--all").decode().splitlines()

    blobs: dict[str, str] = {}
    for line in objects:
        sha, _, path = line.partition(" ")
        if path and sha not in blobs:
            blobs[sha] = path

    hashes = list(blobs)
    payload = ("\n".join(hashes) + "\n").encode()
    out = git("cat-file", "--batch", data=payload)

    findings: list[tuple[str, str, int, str, str]] = []
    reviewed: list[tuple[str, str, int, str, str]] = []
    scanned = binary = oversize = 0

    pos = 0
    while pos < len(out):
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(header) < 3 or header[1] != "blob":
            continue
        sha, size = header[0], int(header[2])
        body = out[pos:pos + size]
        pos += size + 1
        scanned += 1
        if size > MAX_BLOB:
            oversize += 1
            continue
        if b"\x00" in body:
            binary += 1
            continue
        text = body.decode("utf-8", "replace")
        path = blobs.get(sha, "?")
        lines = text.splitlines()
        for name, flags, raw in PATTERNS:
            rx = re.compile(raw, flags)
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    findings.append((name, path, i, mask(line.strip(), rx), sha))
        for name, flags, raw in REVIEW_PATTERNS:
            rx = re.compile(raw, flags)
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    reviewed.append((name, path, i, mask(line.strip(), rx), sha))

    print(f"refs scanned:    {len(refs)}")
    print(f"commits scanned: {len(commits)}")
    print(f"blobs scanned:   {scanned} "
          f"(binary skipped: {binary}, over 4 MB skipped: {oversize})")

    def dump(title: str, rows: list[tuple[str, str, int, str, str]]) -> None:
        print(f"\n{title}: {len(rows)}")
        seen = set()
        for name, path, i, line, sha in rows:
            key = (name, path, line)
            if key in seen:
                continue
            seen.add(key)
            print(f"  [{name}] {path}:{i} (blob {sha[:12]})")
            print(f"    {line[:200]}")

    dump("HIGH-SIGNAL FINDINGS", findings)
    dump("REVIEW-CLASS HITS (credential-shaped, usually fixtures)", reviewed)

    # Private key files tracked anywhere in history, by path.
    key_files = sorted({
        p for p in blobs.values()
        if p.lower().endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"))
    })
    print(f"\nkey-file paths in any ref: {len(key_files)}"
          + (f" -> {key_files}" if key_files else ""))

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
