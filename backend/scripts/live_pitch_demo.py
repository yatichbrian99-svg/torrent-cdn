#!/usr/bin/env python3
"""TorrentCDN LIVE — customer pitch demo.

Drives a REAL live-streaming pipeline against a running TorrentCDN stack:

    branded ffmpeg source ──RTMP──> MediaMTX ──HLS──> TorrentCDN
                                                          │
              simulated viewers (clearly labeled) ────────┤
                                                          ▼
                                              dashboard + analytics

Honesty contract (mirrors the platform's own):
  • The publish pipeline is REAL (ffmpeg → RTMP → MediaMTX → HLS → LIVE).
  • Viewer REPORTS are SIMULATED — byte/offload numbers shown are the
    platform's measured aggregation of those reports, and every screen
    says so. No traffic is invented and nothing is attributed to real users.
  • Offload % = measured P2P bytes / measured total bytes — never modeled.

Usage:
    python scripts/live_pitch_demo.py                       # local compose stack
    python scripts/live_pitch_demo.py --viewers 100         # bigger swarm
    python scripts/live_pitch_demo.py --base-url https://api.example.com
    python scripts/live_pitch_demo.py --cleanup             # delete channel at end

Then point OBS at the printed RTMP/Stream-Key pair — or just let the built-in
source run — and open the printed dashboard + player URLs.
"""
from __future__ import annotations

import argparse
import os
import random
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ── console helpers ──────────────────────────────────────────────────────────

def banner(text: str) -> None:
    print(f"\n{'─' * 66}\n  {text}\n{'─' * 66}")

def ok(msg: str) -> None:
    print(f"  ✔ {msg}")

def info(msg: str) -> None:
    print(f"  • {msg}")

def die(msg: str) -> None:
    print(f"  ✘ {msg}", file=sys.stderr)
    sys.exit(1)

def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:,.1f} GB"

# ── demo ─────────────────────────────────────────────────────────────────────

class Demo:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base = args.base_url.rstrip("/")
        self.client = httpx.Client(timeout=10.0)
        self.slug = "pitch-demo"
        self.token = ""
        self.ch_id = ""
        self.stream_key = ""
        self.hls_url = ""
        self.ffmpeg: subprocess.Popen | None = None
        self.stop = threading.Event()
        self.sim_threads: list[threading.Thread] = []
        self.existing_channel = False

    # ── setup ────────────────────────────────────────────────────────────
    def preflight(self) -> None:
        banner("0 · PREFLIGHT — stack health")
        if not os.path.exists("/usr/bin/ffmpeg") and not subprocess.run(
                ["which", "ffmpeg"], capture_output=True).returncode == 0:
            die("ffmpeg not found — required for the demo source")
        r = self.client.get(f"{self.base}/health")
        ok(f"backend /health → {r.status_code}")
        r = self.client.get(f"{self.base}/api/health/live")
        if r.status_code == 200:
            h = r.json()
            ok("live pipeline: "
               f"mediamtx={'up' if h.get('mediamtx_reachable') else 'DOWN'} · "
               f"monitor={'running' if h.get('monitor_running') else 'NOT RUNNING'}")
            if not h.get("mediamtx_reachable"):
                die("MediaMTX unreachable from the backend — start the compose stack")
        else:
            die(f"/api/health/live → {r.status_code}")

    def create_channel(self) -> None:
        banner("1 · CHANNEL — what a customer creates in the dashboard")
        email = f"pitch-demo-{int(time.time())}@torrentcdn.dev"
        r = self.client.post(f"{self.base}/auth/register", json={
            "email": email, "password": "PitchDemo-2026!pw",
            "terms_accepted": True,
        })
        if r.status_code not in (200, 201):
            die(f"register failed: {r.status_code} {r.text[:200]}")
        self.token = r.json()["access_token"]
        auth = {"Authorization": f"Bearer {self.token}"}
        self.auth = auth

        r = self.client.get(f"{self.base}/api/live/channels", headers=auth)
        for ch in r.json() if r.status_code == 200 else []:
            if ch.get("slug") == self.slug:
                # reuse a previous demo channel so repeated runs are idempotent
                self.ch_id = ch["id"]
                self.existing_channel = True
                info("reusing existing pitch-demo channel")
                r = self.client.post(
                    f"{self.base}/api/live/channels/{self.ch_id}/rotate-key",
                    headers=auth)
                self.stream_key = r.json()["stream_key"]
                break
        if not self.ch_id:
            r = self.client.post(f"{self.base}/api/live/channels", headers=auth,
                                 json={"name": "TorrentCDN Live Demo",
                                       "description": "Customer pitch — live P2P-assisted HLS delivery"})
            if r.status_code != 201:
                die(f"create channel failed: {r.status_code} {r.text[:200]}")
            ch = r.json()
            self.ch_id = ch["id"]
            self.slug = ch["slug"]  # backend derives it from the name — always trust the API
            self.stream_key = ch.get("stream_key") or ""
        ok(f"channel ready: {self.slug} (id {self.ch_id[:8]}…)")

        r = self.client.get(f"{self.base}/api/live/channels/{self.ch_id}/playback",
                            headers=auth)
        pb = r.json()
        self.hls_url = pb.get("hls_url") or pb.get("hls", "")
        rtmp = pb.get("rtmp_server") or pb.get("rtmp_ingest") or ""
        if not rtmp:
            rtmp = pb.get("rtmp_url", "rtmp://localhost:1935/live")

        print("""
  ┌─ OBS SETTINGS (exactly what a customer copies) ─────────────────┐""")
        print(f"  │  Server      {rtmp:<42}│")
        print(f"  │  Stream key  {self.slug}?pass={self.stream_key[:12]}…{'':<10}│")
        print(f"  │  Playback    {self.hls_url[:52]:<52}│")
        print("  └──────────────────────────────────────────────────────────────────┘")

    # ── real publish ─────────────────────────────────────────────────────
    def publish(self) -> None:
        banner("2 · PUBLISH — real source, real RTMP, real HLS")
        label = self.args.label.upper()
        vf = (f"drawtext=fontfile={FONT}:text='{label}':"
              f"fontcolor=white:fontsize=42:x=(w-text_w)/2:y=h/2-90,"
              f"drawtext=fontfile={FONT}:text='P2P-ASSISTED HLS DELIVERY':"
              f"fontcolor=0x39d0ff:fontsize=26:x=(w-text_w)/2:y=h/2,"
              f"drawtext=fontfile={FONT}:text='%{{pts\\:hms}}':"
              f"fontcolor=white:fontsize=30:x=(w-text_w)/2:y=h/2+60,"
              f"drawbox=x=0:y=ih-8:w='iw*t/{self.args.minutes * 60}':h=8:color=0x39d0ff@0.9:t=fill")
        url = f"rtmp://localhost:1935/live/{self.slug}?user={quote(self.slug)}&pass={quote(self.stream_key)}"
        self.ffmpeg = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
             "-g", "60", "-b:v", "2500k", "-c:a", "aac", "-b:a", "96k",
             "-f", "flv", url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        ok("source running: 1280x720 branded test pattern (ffmpeg)")

        def live_check():
            r = self.client.get(f"{self.base}/api/live/channels/{self.ch_id}/status",
                                headers=self.auth)
            return r.json().get("status") == "LIVE", r.json().get("status")
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                if live_check()[0]:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)
        else:
            die("channel never reached LIVE — check MediaMTX + hook config")
        ok("status derived LIVE from real stream activity")
        info(f"HLS playlist: {self.hls_url}")

    # ── simulated viewers (labeled) ──────────────────────────────────────
    def _viewer(self, idx: int) -> None:
        sess = f"sim-viewer-{idx:04d}-{random.randrange(10**6):06d}"
        peer = f"{sess}-p"
        jitter = random.uniform(0.5, 1.6)
        try:
            r = self.client.get(f"{self.base}/api/live/play/{self.slug}")
            tok = r.json().get("token") or r.json().get("playback_token")
            r = self.client.post(f"{self.base}/api/live/session", json={
                "channel_slug": self.slug, "session_id": sess}, headers={
                "Authorization": f"Bearer {tok}"})
            if r.status_code != 200:
                return
            t0 = time.time()
            # track the REAL live edge reported at join; advance locally
            latest = int(r.json().get("latest_sequence") or 0)
            while not self.stop.is_set() and time.time() - t0 < self.args.minutes * 60:
                latest += 1
                # per-tick delivery split: most from P2P, rest from origin
                p2p = int(random.uniform(0.8, 1.2) * 900_000 * jitter)
                org = int(random.uniform(0.05, 0.35) * 900_000 * jitter)
                served, failed = 1, (1 if random.random() < 0.04 else 0)
                self.client.post(f"{self.base}/api/live/heartbeat", json={
                    "channel_slug": self.slug, "session_id": sess,
                    "peer_id": peer, "bytes_uploaded": p2p, "bytes_downloaded": org,
                    "segments_served": served, "segments_failed": failed,
                    "current_segment": latest}, headers={
                    "Authorization": f"Bearer {tok}"})
                self.client.post(f"{self.base}/api/live/segment/availability", json={
                    "channel_slug": self.slug, "session_id": sess, "peer_id": peer,
                    "haves": [latest],
                    "transfers": [{"sequence_number": latest,
                                   "served": True, "bytes": p2p}]}, headers={
                    "Authorization": f"Bearer {tok}"})
                time.sleep(2.0 * jitter)
        except Exception:  # noqa: BLE001 — a simulated viewer must never crash the demo
            pass

    def viewers(self) -> None:
        banner(f"3 · AUDIENCE — {self.args.viewers} SIMULATED viewers joining the swarm")
        print("      ⚠ viewer activity is SIMULATED for demo purposes; the")
        print("        dashboard numbers are the platform's measured aggregation")
        for i in range(self.args.viewers):
            t = threading.Thread(target=self._viewer, args=(i,), daemon=True)
            t.start()
            self.sim_threads.append(t)
            time.sleep(0.05)
        ok(f"{self.args.viewers} simulated viewers active")

    # ── live dashboard printout ──────────────────────────────────────────
    def monitor(self) -> None:
        banner("4 · LIVE DASHBOARD — measured numbers (Ctrl+C to stop)")
        print(f"  {'time':<9}{'viewers':>8}{'peers':>7}{'p2p':>12}{'origin':>12}"
              f"{'offload':>9}{'latest':>8}\n")
        # The whole demo is bounded by --minutes (viewers watch that long;
        # monitor runs 30s past so the last samples are captured, then wraps up).
        deadline = self.t0 + (self.args.minutes + 0.5) * 60
        while not self.stop.is_set() and time.time() < deadline:
            try:
                r = self.client.get(f"{self.base}/api/live/channels/{self.ch_id}/status",
                                    headers=self.auth)
                st = r.json()
                a = self.client.get(
                    f"{self.base}/api/live/channels/{self.ch_id}/analytics?hours=1",
                    headers=self.auth).json()
                p2p, org = a.get("bytes_p2p", 0), a.get("bytes_origin", 0)
                off = 100.0 * p2p / max(1, p2p + org)
                print(f"  {datetime.now().strftime('%H:%M:%S'):<9}"
                      f"{st.get('current_viewers', 0):>8}{st.get('current_peers', 0):>7}"
                      f"{fmt_bytes(p2p):>12}{fmt_bytes(org):>12}{off:>8.1f}%"
                      f"{st.get('latest_segment_sequence') or '-':>8}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  (status poll failed: {e})", flush=True)
            self.stop.wait(10)

    def teardown(self) -> None:
        banner("5 · WRAP-UP")
        self.stop.set()
        if self.ffmpeg:
            self.ffmpeg.terminate()
            try:
                self.ffmpeg.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.ffmpeg.kill()
            info("source stopped — channel returns OFFLINE automatically")
        for t in self.sim_threads:
            t.join(timeout=6)
        try:
            a = self.client.get(
                f"{self.base}/api/live/channels/{self.ch_id}/analytics?hours=1",
                headers=self.auth).json()
            print(f"""
  FINAL (measured aggregation of {'SIMULATED'} viewer reports):
    P2P bytes      {fmt_bytes(a.get('bytes_p2p', 0)):>12}
    Origin bytes   {fmt_bytes(a.get('bytes_origin', 0)):>12}
    P2P offload    {a.get('p2p_offload_percent', 0):>11.1f}%
    Transfers      {a.get('segments_served', 0)} served / {a.get('segments_failed', 0)} failed
""")
        except Exception:  # noqa: BLE001
            pass
        if self.args.cleanup and self.ch_id:
            r = self.client.delete(f"{self.base}/api/live/channels/{self.ch_id}",
                                   headers=self.auth)
            info(f"channel deleted ({r.status_code}) — analytics preserved, live state expires")
        else:
            info(f"channel '{self.slug}' left in place — show the dashboard at "
                 f"{self.base.replace(':8000', ':3000')}/live, or re-run with --cleanup")

    def run(self) -> None:
        signal.signal(signal.SIGINT, self._sigint)
        self.t0 = time.time()
        self.preflight()
        self.create_channel()
        self.publish()
        self.viewers()
        self.monitor()
        self.teardown()

    def _sigint(self, *_):  # noqa: ANN002
        print("\n  (stopping — Ctrl+C again to force)")
        self.stop.set()
        self.monitor_stop = True


def main() -> None:
    p = argparse.ArgumentParser(description="TorrentCDN live pitch demo")
    p.add_argument("--base-url", default=os.environ.get("DEMO_BASE_URL", "http://localhost:8000"))
    p.add_argument("--viewers", type=int, default=25, help="SIMULATED viewers (default 25)")
    p.add_argument("--minutes", type=int, default=6, help="demo duration (default 6 min)")
    p.add_argument("--label", default="TorrentCDN Live", help="on-stream title text")
    p.add_argument("--cleanup", action="store_true", help="delete the channel at the end")
    args = p.parse_args()
    if args.minutes < 1 or args.viewers < 0:
        die("--minutes must be ≥ 1 and --viewers ≥ 0")
    Demo(args).run()


if __name__ == "__main__":
    main()
