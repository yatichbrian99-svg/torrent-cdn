#!/usr/bin/env python3
"""Flip the Render service's Paystack env vars from TEST to LIVE mode.

Fetches ALL current env vars, swaps PAYSTACK_SECRET_KEY / PAYSTACK_PLAN_PRO /
PAYSTACK_PLAN_BUSINESS to the live values, PUTs the full set back (Render's
PUT replaces the whole list), then re-GETs to confirm. Prints masked values
only — never secrets.

The live plans can be created automatically via the Paystack API (mirroring
the test-mode amounts: Pro KES 4,000/mo, Business KES 12,000/mo), or supplied
explicitly if you already created them in the dashboard.

Usage:
    RENDER_KEY=<rkey_...> PAYSTACK_LIVE_KEY=<sk_live_...> \\
        python3 backend/scripts/paystack_switch_env.py <service_id> [PLN_PRO PLN_BUSINESS]

Exit codes:
    0  all OK — service now points at Paystack LIVE
    1  failure (auth, env update, verification mismatch)
    2  the provided key is NOT a live key (still test mode / verification pending)
"""
import json
import os
import sys
import urllib.error
import urllib.request

RENDER_KEY = os.environ.get("RENDER_KEY")
LIVE_KEY = os.environ.get("PAYSTACK_LIVE_KEY")
SERVICE_ID = sys.argv[1] if len(sys.argv) > 1 else ""
PLAN_PRO_ARG = sys.argv[2] if len(sys.argv) > 2 else ""
PLAN_BUSINESS_ARG = sys.argv[3] if len(sys.argv) > 3 else ""

if not RENDER_KEY or not LIVE_KEY or not SERVICE_ID:
    print(__doc__)
    sys.exit(2)

PAYSTACK_API = "https://api.paystack.co"
BASE = f"https://api.render.com/v1/services/{SERVICE_ID}"

# Pro KES 4,000/mo, Business KES 12,000/mo (smallest unit = cents) — mirrors
# the amounts already hardcoded in backend/app/config.py defaults.
PRO_AMOUNT = 400000
BUSINESS_AMOUNT = 1200000


def api(url, method="GET", body=None, token=None):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def mask(v):
    if not v:
        return v
    if len(v) <= 12:
        return "*" * len(v)
    return v[:6] + "…" + v[-4:]


def paystack_ok():
    """Live keys start sk_live_; a valid live key also answers /plan with the
    LIVE plan set (test keys answer the test set)."""
    if not LIVE_KEY.startswith("sk_live_"):
        return False, "key does not start with sk_live_ — this is not a live key"
    try:
        data = api(f"{PAYSTACK_API}/plan?perPage=10", token=LIVE_KEY)
        if not data.get("status"):
            return False, f"Paystack rejected the key: {data.get('message', 'unknown error')}"
        names = [p["name"] for p in data.get("data", [])]
        return True, f"live key OK — {len(data.get('data', []))} live plan(s): {names}"
    except urllib.error.HTTPError as e:
        return False, f"Paystack HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"


def create_plan(name, amount, currency="KES"):
    """Create a recurring monthly plan, returning its plan_code."""
    data = api(
        f"{PAYSTACK_API}/plan", method="POST", token=LIVE_KEY,
        body={"name": name, "amount": amount, "interval": "monthly",
              "currency": currency},
    )
    if not data.get("status"):
        raise RuntimeError(f"plan create failed ({name}): {data.get('message')}")
    return data["data"]["plan_code"]


def main():
    # 1) Prove the key is a LIVE key before touching Render.
    ok, msg = paystack_ok()
    print(f"Paystack live key check: {'PASS' if ok else 'FAIL'} — {msg}")
    if not ok:
        print("\nLive mode is not available yet. In the Paystack dashboard:")
        print("  Settings -> Business -> Verification. Complete KYC and wait for")
        print("  approval (usually 1-3 business days). Then toggle the top-right")
        print("  switch to Live and copy the Live Secret Key (sk_live_...).")
        sys.exit(2)

    # 2) Resolve live plan codes: explicit args, else create them.
    plan_pro = PLAN_PRO_ARG or create_plan("Pro", PRO_AMOUNT)
    plan_business = PLAN_BUSINESS_ARG or create_plan("Business", BUSINESS_AMOUNT)
    print(f"Live plans: PRO={plan_pro} BUSINESS={plan_business}")

    # 3) Read current env vars (full values).
    current = api(f"{BASE}/env-vars?limit=100", token=RENDER_KEY)
    pairs = {e["envVar"]["key"]: e["envVar"]["value"] for e in current}
    print(f"Fetched {len(pairs)} env vars")

    # 4) Compute new values.
    new = dict(pairs)
    new["PAYSTACK_SECRET_KEY"] = LIVE_KEY
    new["PAYSTACK_PLAN_PRO"] = plan_pro
    new["PAYSTACK_PLAN_BUSINESS"] = plan_business

    print("\nChanges:")
    for k in ("PAYSTACK_SECRET_KEY", "PAYSTACK_PLAN_PRO", "PAYSTACK_PLAN_BUSINESS"):
        old = pairs.get(k, "<unset>")
        val = new.get(k, "<unset>")
        flag = "CHANGED" if old != val else "same"
        print(f"  {k}: {mask(old)} -> {mask(val)}  ({flag})")

    # 5) PUT the full list back.
    body = [{"key": k, "value": v} for k, v in new.items()]
    print(f"\nPUT {len(body)} env vars…")
    try:
        api(f"{BASE}/env-vars", method="PUT", token=RENDER_KEY, body=body)
    except urllib.error.HTTPError as e:
        print(f"FATAL: Render rejected the env-var update — HTTP {e.code}")
        print((e.read().decode("utf-8", "replace") or "")[:500])
        sys.exit(1)

    # 6) Verify by re-reading.
    after = api(f"{BASE}/env-vars?limit=100", token=RENDER_KEY)
    after_pairs = {e["envVar"]["key"]: e["envVar"]["value"] for e in after}
    print(f"Re-read {len(after_pairs)} env vars")

    ok = True
    for k in ("PAYSTACK_SECRET_KEY", "PAYSTACK_PLAN_PRO", "PAYSTACK_PLAN_BUSINESS"):
        got, want = after_pairs.get(k), new.get(k)
        if got != want:
            ok = False
            print(f"  MISMATCH {k}: expected {mask(want)}, got {mask(got)}")
        else:
            print(f"  OK {k} = {mask(got)}")

    if len(after_pairs) != len(new):
        ok = False
        print(f"  ENV COUNT MISMATCH: was {len(new)}, now {len(after_pairs)}")
    for sentinel in ("JWT_SECRET", "DATABASE_URL", "MINIO_SECRET_KEY", "REDIS_URL"):
        if sentinel in pairs and after_pairs.get(sentinel) != pairs[sentinel]:
            ok = False
            print(f"  SENTINEL MISMATCH {sentinel}: value changed!")

    print("\nRESULT:", "ALL OK — service now charges via Paystack LIVE" if ok
          else "PROBLEM — check above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
