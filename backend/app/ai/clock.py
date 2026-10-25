"""One clock for the whole AI module.

Every comparison (ticket expiry, attestation freshness, payout delay) goes
through here so tests can move time without sleeping, and so production has a
single place to add NTP-drift monitoring (see SECURITY.md "Clock skew").
"""

from __future__ import annotations

import time


def now_unix() -> int:
    return int(time.time())
