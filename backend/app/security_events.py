"""
Bounded in-memory security-event recorder for the admin health dashboard.

Every security-relevant action (login failure, MFA failure, admin action,
password reset, register throttling) is appended here so the dashboard can show
a recent-events feed — the abuse telemetry that otherwise only lives in logs.

Design constraints:
- Bounded: a deque with a hard maxlen, so a flood of events can never grow
  memory without bound (defense in depth on top of the rate limiters).
- No sensitive data: callers must pass short, safe `detail` strings (email of
  the ACTOR is fine for an admin-only feed; never passwords/tokens/secrets).
- In-memory only: lost on process restart. That is acceptable for a dashboard
  feed (matches the app-wide "Redis optional" posture); the events are NOT an
  audit log — the structured logger remains the durable record.
"""

import threading
import time
from collections import deque

MAX_EVENTS = 200
_LOCK = threading.Lock()
_EVENTS: deque = deque(maxlen=MAX_EVENTS)


def record(event_type: str, detail: str = "", ip: str | None = None) -> None:
    """Append a security event. Never raises (telemetry must not break flows)."""
    try:
        with _LOCK:
            _EVENTS.append(
                {
                    "ts": time.time(),
                    "type": event_type,
                    "detail": (detail or "")[:160],
                    "ip": ip or "",
                }
            )
    except Exception:
        pass


def recent(limit: int = 50) -> list[dict]:
    """Return the most recent events, newest first."""
    with _LOCK:
        items = list(_EVENTS)
    items.reverse()
    return items[: max(1, min(limit, MAX_EVENTS))]


def clear() -> None:
    """Wipe the feed (test helper)."""
    with _LOCK:
        _EVENTS.clear()
