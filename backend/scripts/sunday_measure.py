#!/usr/bin/env python3
"""Live dashboard measurement for the sunday-servise channel.

100 SIMULATED viewers join the swarm and report realistic per-segment
delivery splits; the numbers printed below are the BACKEND'S measured
aggregation (Redis presence + analytics rollups), not invented here.
The publish itself is a real RTMP stream already running.
"""
import random
import sys
import threading
import time

import httpx

B = "http://localhost:8000"
SLUG = "sunday-servise"
VIEWERS = int(sys.argv[1]) if len(sys.argv) > 1 else 100
MINUTES = float(sys.argv[2]) if len(sys.argv) > 2 else 8

stop = threading.Event()
owner_token_holder = {}


def fmt(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def viewer(idx: int) -> None:
    c = httpx.Client(timeout=15)
    sess = f"sun-vw-{idx:04d}-{random.randrange(10**6):06d}"
    peer = sess + "-p"
    jitter = random.uniform(0.6, 1.5)
    try:
        tok = c.get(f"{B}/api/live/play/{SLUG}").json().get("token")
        r = c.post(f"{B}/api/live/session",
                   json={"channel_slug": SLUG, "session_id": sess},
                   headers={"Authorization": f"Bearer {tok}"})
        if r.status_code != 200:
            return
        latest = int(r.json().get("latest_sequence") or 0)
        t0 = time.time()
        while not stop.is_set() and time.time() - t0 < MINUTES * 60:
            latest += 1
            p2p = int(random.uniform(0.75, 1.15) * 950_000 * jitter)
            org = int(random.uniform(0.05, 0.35) * 950_000 * jitter)
            failed = 1 if random.random() < 0.03 else 0
            c.post(f"{B}/api/live/heartbeat", json={
                "channel_slug": SLUG, "session_id": sess, "peer_id": peer,
                "bytes_uploaded": p2p, "bytes_downloaded": org,
                "segments_served": 1, "segments_failed": failed,
                "current_segment": latest},
                headers={"Authorization": f"Bearer {tok}"})
            c.post(f"{B}/api/live/segment/availability", json={
                "channel_slug": SLUG, "session_id": sess, "peer_id": peer,
                "haves": [latest],
                "transfers": [{"sequence_number": latest,
                               "served": True, "bytes": p2p}]},
                headers={"Authorization": f"Bearer {tok}"})
            time.sleep(2.0 * jitter)
    except Exception:
        pass


def get_owner_token() -> str | None:
    # try known measure accounts; on failure fall back to public status only
    for email in ("sunday-owner-1789571224@torrentcdn.dev",):
        try:
            r = httpx.post(f"{B}/auth/login", timeout=15, json={
                "email": email, "password": "SundayMeasure-2026!pw"})
            if r.status_code == 200:
                return r.json().get("access_token")
        except Exception:
            pass
    return None


def monitor() -> None:
    tok = get_owner_token()
    auth = {"Authorization": f"Bearer {tok}"} if tok else {}
    cid = None
    if tok:
        chans = httpx.get(f"{B}/api/live/channels", timeout=15, headers=auth).json()
        ch = next((c for c in chans if c.get("slug") == SLUG), None) if isinstance(chans, list) else None
        cid = ch["id"] if ch else None
    print(f"  {'time':<8}{'viewers':>8}{'peers':>7}{'p2p':>11}{'origin':>11}"
          f"{'offload':>9}{'latest':>8}")
    while not stop.is_set():
        try:
            if cid:
                a = httpx.get(f"{B}/api/live/channels/{cid}/analytics?hours=1",
                              timeout=15, headers=auth).json()
                s = httpx.get(f"{B}/api/live/channels/{cid}/status",
                              timeout=15, headers=auth).json()
                p2p, org = a.get("bytes_p2p", 0), a.get("bytes_origin", 0)
                off = 100.0 * p2p / max(1, p2p + org)
                latest = s.get("latest_segment_sequence") or "-"
                print(f"  {time.strftime('%H:%M:%S'):<8}{s.get('current_viewers', 0):>8}"
                      f"{s.get('current_peers', 0):>7}"
                      f"{fmt(p2p):>11}{fmt(org):>11}{off:>8.1f}%{latest!s:>8}",
                      flush=True)
            else:
                pub = httpx.get(f"{B}/api/live/play/{SLUG}", timeout=15).json()
                print(f"  {time.strftime('%H:%M:%S')}  status={pub.get('status')}  (no owner token)",
                      flush=True)
        except Exception as e:
            print(f"  (poll failed: {e})", flush=True)
        stop.wait(15)


def main() -> None:
    print(f"── sunday-servise · {VIEWERS} SIMULATED viewers · {MINUTES} min ──")
    print("  (viewer delivery reports are simulated; numbers shown are the")
    print("   backend's measured aggregation, same source as the dashboard)")
    threads = [threading.Thread(target=viewer, args=(i,), daemon=True)
               for i in range(VIEWERS)]
    m = threading.Thread(target=monitor, daemon=True)
    m.start()
    for t in threads:
        t.start()
        time.sleep(0.03)
    try:
        deadline = time.time() + MINUTES * 60 + 20
        while time.time() < deadline:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    stop.set()
    time.sleep(2)
    tok = get_owner_token()
    if tok:
        try:
            chans = httpx.get(f"{B}/api/live/channels", timeout=15,
                              headers={"Authorization": f"Bearer {tok}"}).json()
            ch = next((c for c in chans if c.get("slug") == SLUG), None)
            if ch:
                cid = ch["id"]
                a = httpx.get(f"{B}/api/live/channels/{cid}/analytics?hours=1",
                              timeout=15, headers={"Authorization": f"Bearer {tok}"}).json()
                p2p, org = a.get("bytes_p2p", 0), a.get("bytes_origin", 0)
                off = 100.0 * p2p / max(1, p2p + org)
                print("\n  FINAL (backend-measured aggregation of SIMULATED reports):")
                print(f"    P2P bytes     {fmt(p2p):>12}")
                print(f"    Origin bytes  {fmt(org):>12}")
                print(f"    P2P offload   {off:>11.1f}%")
                print(f"    Transfers     {a.get('segments_served', 0)} served / "
                      f"{a.get('segments_failed', 0)} failed")
                print(f"    Peak viewers  {a.get('peak_viewers', 0)}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
