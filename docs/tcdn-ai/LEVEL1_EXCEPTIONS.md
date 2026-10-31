# Level 1 exceptions

Two [P1] rows are **not** met. Neither can be closed by a code change on this
branch, so they are recorded here with what is actually true rather than being
described as done. Nothing in this file is a substitute for the evidence in
`PUSH_CHECKLIST.md`; where the two disagree, the checklist is right.

**Level 1 requires all 56 P1 rows. These two are the reason it is NO.**

---

## 6.1 — Redundant execution: gateway mode only

The sampling machinery runs, is keyed, and pays the operator that does the
second execution. What it does **not** do is cover the tier the product calls
private.

### What is implemented and exercised

`backend/tests/test_ai_redundant_execution.py` (7 tests) and the
`TestShadowPayout` cases in `tests/test_ai_node_billing.py` drive, against the
real coordinator:

* the sample decision is `HMAC-SHA-256(coordinator_secret, domain || job_id)`,
  default **3 %** (`TCDN_AI_VERIFICATION_SAMPLE_PCT`), and
  `test_sample_is_not_derivable_from_the_job_id` proves the old public-salt
  construction cannot predict it;
* a second signed ticket is issued for a **different operator**, excluded by node
  id rather than filtered after selection, and recorded in `ai_verifications`;
* an assignment is handed out exactly once, a single registered node records
  `no_alternate_node`, the daily cap records `daily_cap_reached`;
* the shadow receipt is verified against the *second* node's identity key, and
  the two output hashes are compared as soon as both exist; disagreement is
  `inconclusive` and penalises nobody, agreement completes the sample;
* the shadow operator is paid from `platform:revenue` — the compute share of its
  own token counts, capped per run, one ledger entry per shadow job, so a retried
  receipt pays once and the customer is never charged twice.

### What is not true

1. **The decision is taken at placement, not after a commitment.** The row asks
   for a sample that cannot be influenced by the party being sampled *and* is
   drawn only once the work is committed. Today the draw happens when the job is
   placed, before any worker has produced anything. `VerificationRng.sample_decision`
   already accepts a `transcript_root`; drawing from it after the primary receipt
   exists is a protocol change (the shadow ticket would have to be issued late),
   not a configuration change. Consequence: a worker that could learn whether its
   own job was sampled — through collusion with the sampled operator, not through
   the job id, which is what the keyed HMAC removed — could cheat only on
   unsampled jobs. It is not demonstrated that this is exploitable; it is also not
   demonstrated that it is not.
2. **SDK-mode (confidential) jobs are never re-executed.** The coordinator holds
   no plaintext for an encrypted job, so it cannot dispatch a second execution.
   Effect on a customer: the 3 % sample applies to **gateway-mode** jobs only.
   A confidential job's correctness is protected by the attestation binding and
   the reproducibility of the model manifest, **not** by redundant execution.
3. **No vendor report parsers are shipped.** The confidential tier's attestation
   path is only exercisable with the simulated adapter in development
   (`LIMITATIONS.md` §1); production refuses the simulated adapter at startup.

### How to describe this to a customer

"Jobs run through the OpenAI-compatible gateway are re-executed on a second,
independent operator for ~3 % of requests, and the two outputs are compared. Jobs
that use the SDK's end-to-end encryption are **not** re-executed: the coordinator
never has the prompt, so it cannot ask a second node to run it."

That sentence is the whole exception. There is no version of "verified for all
tiers" that is true today.

### What closing it needs

* move ticket issuance for the shadow run after the primary receipt (protocol
  change, touches `place`, `submit_receipt` and the verification table);
* a client-side or SDK-side re-execution path for encrypted jobs (the SDK would
  have to run the second execution itself, or reveal the prompt to a second
  node under a second session key);
* a real attestation verifier before either can be trusted as an integrity
  anchor.

---

## 4.4 — Paystack: blocked on the owner, not on code

### What is exercised

`backend/tests/test_ai_billing.py` (11 tests) covers the billing state machine
against **signed fixtures**: webhook signature verification (valid, forged,
tampered body), replay of a delivered event, the credit-purchase → balance →
ledger path, refunds, and mismatched amounts. `payments.verify_webhook` is also a
fuzz target (0 findings).

### What is not exercised

* a **real** charge authorised by Paystack's sandbox;
* a **real** decline and the customer-visible state after it;
* a **real** refund round trip through the provider and back through the webhook;
* the **M-Pesa** channel — not enabled on the account.

### Why it is blocked

No Paystack sandbox session and no M-Pesa enablement on the account. This is an
access problem: with a sandbox key the remaining work is roughly one day (charge,
decline, refund, replay against a live endpoint), which is why the row is
`PARTIAL` rather than `MISSING`.

### What a customer could notice

Payments are code-complete but have never talked to the provider. The failure
modes tests cannot see are the provider's own: KES rounding, webhook delivery
order, how a partially refunded purchase is reported. The first live transaction
is the first real test of that path. Anyone merging this to production should
expect to watch the first purchase, and should not run the flag on for paying
customers without it.

---

## 5.5 — ZAP 40018: a path-scoped rule entry, not a suppressed rule

The 2026-10-30 DAST run failed on **HIGH / 40018 "SQL Injection"**. Across
30,513 proxied requests it had **8 instances, every one with an empty
`evidence` field**, on four paths, with **no 5xx on any `/api/ai/v1/*` path**.

The claim was tested rather than argued away:

* **static** — every statement reachable from `/api/ai/v1/placements`,
  `/nodes/register`, `/jobs/{id}/ack` and `/api/live/heartbeat` is listed with
  its `$n` binding in `SQLI_40018.md`; the AST audit
  (`scripts/sql_injection_audit.py`, in CI, with its own five-vector negative
test) fails the build on any f-string, `.format()`, `%` or concatenated SQL in
  `backend/app`;
* **dynamic** — `tests/test_ai_sqli_binding.py` sends quote, boolean,
  time-based, stacked, UNION, unicode, NUL-byte and comment payloads through
  the store methods those routes reach, recording every statement handed to
  asyncpg, and asserts the payload never appears in the SQL text and always
  arrives as a bound argument (44 passed together with the limiter tests).

The ZAP plans therefore carry an `alertFilter` entry for 40018 scoped to the
**four flagged URL patterns only**, set to "False Positive" and placed before
the scan. The rule stays active everywhere else, the paths stay scanned, and a
40018 on any other path still fails the job. Upstream reports of `alertFilter`
not applying are recorded in `SQLI_40018.md` §5: if the entry fails to apply,
the job stays red and the answer is to fix the filter, not to disable the rule.

## Everything else

Every other P1 row is `VERIFIED` in `PUSH_CHECKLIST.md`, with the command or CI
run that produced it. The two rows above are the only exceptions, and neither is
described anywhere in the product documentation as working.
