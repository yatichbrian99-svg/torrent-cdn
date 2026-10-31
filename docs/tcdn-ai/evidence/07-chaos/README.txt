Chaos status: NOT VERIFIED beyond the in-process failure-injection test.

What was run:
  tests/test_ai_failure_injection.py::test_abandoned_placement_does_not_hold_credits_forever
    -> PASS (worker never reports; coordinator expires the job and releases the hold)
  tests/test_ai_postgres.py::test_expire_stale_jobs_releases_the_hold -> PASS

What was NOT run:
  * killing the coordinator/DB mid-stream under load
  * network partition, disk-full, memory exhaustion, clock skew
  * payment-provider outage during a live purchase

These are open items in VERIFICATION_REPORT.md; they need a dedicated chaos pass
with toxiproxy or docker kill under load, on the full Compose stack.
