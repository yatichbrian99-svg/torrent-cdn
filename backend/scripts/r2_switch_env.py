#!/usr/bin/env python3
"""Switch the Render service's storage env vars from Supabase to Cloudflare R2.

Fetches ALL current env vars, swaps only the MINIO_* storage ones (endpoint,
keys, region, public URL), PUTs the full set back (Render's PUT replaces the
whole list), then re-GETs to confirm. Prints masked values only — never secrets.

Usage:
    RENDER_KEY=<rkey_...> R2_ACCESS_KEY=<ak> R2_SECRET_KEY=<sk> \\
        python3 backend/scripts/r2_switch_env.py <service_id>
"""
import json
import os
import sys
import urllib.error
import urllib.request

RENDER_KEY = os.environ["RENDER_KEY"]
R2_ACCESS_KEY = os.environ["R2_ACCESS_KEY"]
R2_SECRET_KEY = os.environ["R2_SECRET_KEY"]
ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "a942dd172ffb3faa3dd24d9b92df6008")
PUBLIC_URL = os.environ.get(
    "R2_PUBLIC_URL", "https://pub-db587f977d274a458f30d14fa2ddfb9e.r2.dev"
)
SERVICE_ID = sys.argv[1]

BASE = f"https://api.render.com/v1/services/{SERVICE_ID}"


def api(method, url, body=None):
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {RENDER_KEY}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def mask(v):
    if not v:
        return v
    if len(v) <= 12:
        return "*" * len(v)
    return v[:4] + "…" + v[-4:]


# 1) Read current env vars (full values).
current = api("GET", f"{BASE}/env-vars?limit=100")
pairs = {e["envVar"]["key"]: e["envVar"]["value"] for e in current}
print(f"Fetched {len(pairs)} env vars")

# 2) Compute the new storage values.
new = dict(pairs)
new["MINIO_ENDPOINT"] = f"{ACCOUNT_ID}.r2.cloudflarestorage.com"
new["MINIO_ACCESS_KEY"] = R2_ACCESS_KEY
new["MINIO_SECRET_KEY"] = R2_SECRET_KEY
new["MINIO_REGION"] = "auto"
new["MINIO_PUBLIC_URL"] = PUBLIC_URL
# MINIO_BUCKET + MINIO_SECURE are already correct — preserved as-is.

# 3) Show the diff (masked).
print("\nChanges:")
for k in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY",
          "MINIO_REGION", "MINIO_PUBLIC_URL", "MINIO_BUCKET", "MINIO_SECURE"):
    old = pairs.get(k, "<unset>")
    val = new.get(k, "<unset>")
    flag = "CHANGED" if old != val else "same"
    print(f"  {k}: {mask(old)} -> {mask(val)}  ({flag})")

# 4) PUT the full list back (Render replaces the whole env var set).
body = [{"key": k, "value": v} for k, v in new.items()]
print(f"\nPUT {len(body)} env vars…")
try:
    api("PUT", f"{BASE}/env-vars", body)
except urllib.error.HTTPError as e:
    print(f"FATAL: Render rejected the env-var update — HTTP {e.code}")
    print((e.read().decode("utf-8", "replace") or "")[:500])
    sys.exit(1)
except Exception as e:
    print(f"FATAL: env-var update failed: {e}")
    sys.exit(1)

# 5) Verify by re-reading.
after = api("GET", f"{BASE}/env-vars?limit=100")
after_pairs = {e["envVar"]["key"]: e["envVar"]["value"] for e in after}
print(f"Re-read {len(after_pairs)} env vars")

ok = True
for k in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY",
          "MINIO_REGION", "MINIO_PUBLIC_URL", "MINIO_BUCKET", "MINIO_SECURE"):
    got = after_pairs.get(k)
    want = new.get(k)
    if got != want:
        ok = False
        print(f"  MISMATCH {k}: expected {mask(want)}, got {mask(got)}")
    else:
        print(f"  OK {k} = {mask(got)}")

# Preserved keys sanity: count must match (nothing dropped), and the critical
# non-storage secrets must be byte-identical (a key dropped + another added
# would pass the count check, so pin the sentinels too).
if len(after_pairs) != len(new):
    ok = False
    print(f"  ENV COUNT MISMATCH: was {len(new)}, now {len(after_pairs)}")
for sentinel in ("JWT_SECRET", "DATABASE_URL", "PAYSTACK_SECRET_KEY", "REDIS_URL"):
    if sentinel in pairs and after_pairs.get(sentinel) != pairs[sentinel]:
        ok = False
        print(f"  SENTINEL MISMATCH {sentinel}: value changed!")
    elif sentinel not in after_pairs:
        ok = False
        print(f"  SENTINEL MISSING {sentinel}: dropped by the PUT!")

print("\nRESULT:", "ALL OK — service storage points at R2" if ok else "PROBLEM — check above")
sys.exit(0 if ok else 1)
