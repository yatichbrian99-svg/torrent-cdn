#!/usr/bin/env python3
"""One-shot R2 credential verifier (local dev tooling only, not part of the app).

Verifies a full S3 round-trip against Cloudflare R2 using boto3:
  list buckets -> PUT a unique test object -> GET it back (signed) ->
  fetch it via the public r2.dev URL with a browser User-Agent ->
  DELETE it -> report.

Credentials come from the environment (R2_ACCESS_KEY / R2_SECRET_KEY) so this
script is safe to commit — it never contains secrets.

Clock note: this dev machine's clock can drift badly (it was ~3h ahead), which
breaks SigV4 signing with RequestTimeTooSkewed. The skew is measured once at
startup from a public HTTP Date header (same pattern as config.py's
_fetch_reference_epoch) and applied to botocore's signing timestamps.
Production (Render) has a correct clock and needs no patch.

Usage:
    R2_ACCESS_KEY=... R2_SECRET_KEY=... python scripts/r2_verify.py
"""
import datetime as _dtd
import os
import sys
import urllib.error
import urllib.request
import uuid
from email.utils import parsedate_to_datetime

import boto3
import botocore.auth
from botocore.config import Config

# Fallback skew constant if no reference clock is reachable (measured 2026-08-07).
_FALLBACK_SKEW_SECONDS = 10779


def _measure_skew(timeout: float = 5.0) -> int:
    """Best-effort local-vs-real clock skew in seconds — POSITIVE when the local
    clock is AHEAD of real time (0 if no reference is reachable). The signing
    correction below does `now - skew`, so a positive lead is subtracted."""
    try:
        req = urllib.request.Request("https://www.cloudflare.com/cdn-cgi/trace")
        req.add_header("User-Agent", "torrent-cdn-r2-verify/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for line in resp.read().decode("utf-8", "replace").splitlines():
                if line.startswith("ts="):
                    server = float(line.split("=", 1)[1])
                    return int(_dtd.datetime.now(_dtd.timezone.utc).timestamp() - server)
    except Exception:
        pass
    try:
        req = urllib.request.Request("https://www.google.com/", method="HEAD")
        req.add_header("User-Agent", "torrent-cdn-r2-verify/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            date_hdr = resp.headers.get("Date")
            if date_hdr:
                dt = parsedate_to_datetime(date_hdr)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_dtd.timezone.utc)
                return int(_dtd.datetime.now(_dtd.timezone.utc).timestamp() - dt.timestamp())
    except Exception:
        pass
    return 0


SKEW = _measure_skew() or _FALLBACK_SKEW_SECONDS
if SKEW:
    # Correct botocore's signing timestamps by the measured skew.
    class _Corr:
        @staticmethod
        def utcnow():
            return _dtd.datetime.now(_dtd.timezone.utc) - _dtd.timedelta(seconds=SKEW)

    botocore.auth.datetime = type("M", (), {"datetime": _Corr})()
    print(f"(clock skew correction applied: {SKEW}s)")

ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "a942dd172ffb3faa3dd24d9b92df6008")
ACCESS_KEY = os.environ["R2_ACCESS_KEY"]
SECRET_KEY = os.environ["R2_SECRET_KEY"]
BUCKET = os.environ.get("R2_BUCKET", "torrent-cdn-files")
ENDPOINT = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"
PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "https://pub-db587f977d274a458f30d14fa2ddfb9e.r2.dev")

# Cloudflare's bot protection (error 1010) blocks bare clients with no
# User-Agent; real browsers send one, so mimic that for the public-read check.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

failures = []


def check(label, fn):
    try:
        fn()
        print(f"  PASS  {label}")
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL  {label}: {str(e)[:220]}")
        failures.append(label)


def main():
    s3 = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 2}),
    )
    key = f"r2-verify-{uuid.uuid4().hex[:12]}.bin"
    payload = b"\x1a\x45\xdf\xa3" + bytes(range(256)) * 8  # fake webm-ish bytes

    def t_list():
        r = s3.list_buckets()
        names = [b["Name"] for b in r["Buckets"]]
        assert BUCKET in names, f"bucket {BUCKET} not in {names}"

    def t_put():
        s3.put_object(Bucket=BUCKET, Key=key, Body=payload, ContentType="video/webm")

    def t_get_signed():
        r = s3.get_object(Bucket=BUCKET, Key=key)
        assert r["Body"].read() == payload, "signed GET body mismatch"

    def t_get_public():
        req = urllib.request.Request(f"{PUBLIC_URL}/{key}", headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            assert resp.status == 200, f"public GET status {resp.status}"
            assert resp.read() == payload, "public GET body mismatch"

    def t_head():
        s3.head_object(Bucket=BUCKET, Key=key)

    def t_delete():
        s3.delete_object(Bucket=BUCKET, Key=key)
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
            raise AssertionError("object still exists after delete")
        except s3.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            assert code in ("404", "NoSuchKey"), f"unexpected error after delete: {code}"

    print(f"Round-trip test on bucket '{BUCKET}' (key {key}):")
    check("list buckets", t_list)
    check("PUT object", t_put)
    check("GET object (signed)", t_get_signed)
    check("GET object (public r2.dev URL)", t_get_public)
    check("HEAD object", t_head)
    check("DELETE object", t_delete)

    if failures:
        print(f"\nRESULT: {len(failures)} FAILED — {failures}")
        sys.exit(1)
    print("\nRESULT: ALL PASS — R2 credentials are valid for S3 read/write/delete.")


if __name__ == "__main__":
    main()
