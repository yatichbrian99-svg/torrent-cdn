# Level 1 exceptions

Three things are recorded here, each because it is **not** a plain pass: two [P1]
rows that are not met (**6.1**, **4.4**), and one accepted scanner finding
(**5.5**, rule 40018) whose acceptance is conditional and machine-checked.
Nothing in this file is a substitute for the evidence in `PUSH_CHECKLIST.md`;
where the two disagree, the checklist is right.

**Level 1 requires all 56 P1 rows. 6.1 and 4.4 are why it is still NO.**

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

## 5.5 — ZAP 40018: accepted at rule level, under four conditions

Alert **40018 ("SQL Injection")** is a HIGH the scanner emits on `/api/ai/v1`
input routes while its `evidence` field stays empty — i.e. it matched no database
error text and no reflected payload. It is accepted, but **not** by disabling the
rule and **not** by a fixed list of URLs: the rule moved between endpoints on
consecutive runs (`/placements` → `/attestations` + `/jobs/*/ack`), so a path list
went red on whichever route the scanner happened to pick that day.

The acceptance is encoded in the gate, `.github/zap/summarise.py`, and applies to
an instance only when **all four** hold. Any failure keeps it in the HIGH bucket
and the job fails:

1. **`evidence` is empty.** A captured fragment means the scanner saw something;
   nothing here excuses that.
2. **No 5xx on that path** in the same run, counted per path from the auth proxy
   logs — the only place with a per-request status for both authenticated passes.
3. **No database error text in any response body on that path.** The proxy marks
   such responses `DBERR` (asyncpg/Postgres/SQLSTATE/"syntax error at or near"/…);
   ZAP's JSON keeps no bodies, so this is where the check can be made.
4. **The path was exercised in this run.** No requests means no evidence, and
   "no 5xx observed" is not "clean" — unobserved paths are refused.

Timing is not a gate-time check (a scan has no stable baseline). It is asserted
in the dynamic test instead, together with the rest.

`backend/tests/test_zap_gate.py` (**10 tests**) proves each condition can fail:
evidence present → red; a 500 on the path → red; a `DBERR` body on the path →
red; an unobserved path → red; any other HIGH alert → red; a missing report or a
missing proxy log → exit 2, not a pass.

### Why the alert cannot be true here

* **Static (blocking).** `backend/scripts/sql_injection_audit.py` parses every
  file under `backend/app` with `ast` and fails the build on any DB call whose
  SQL argument is not a plain literal it can resolve to a module-level constant,
  and on any f-string / `.format()` / `%` / concatenation that contains SQL
  markers. It runs as the always-on `static-audit` CI job **and** as
  `tests/test_sql_injection.py`. It is itself break-tested: a new control in
  `scripts/break_tests.py` plants an f-string query with the account id
  interpolated into it in `app/ai/store.py` and requires the audit to fail
  (**32/32 controls caught, 0 unproven**).
* **Dynamic, every input route.** `backend/tests/test_ai_sqli_routes.py` derives
  the route list from the routers rather than a hardcoded one and drives
  **12 input routes / 216 hostile requests** with a valid credential — 8 payload
  families (quote/boolean, time-based `pg_sleep`, stacked `DROP TABLE`, UNION
  against `ai_api_keys`, unicode, NUL, comment, arithmetic), each in every string
  field the route's schema declares, plus a signed-envelope variant for the five
  signed routes. It asserts, per request: never 5xx; no database/driver error
  text in the body; **no state change for a refused request** (jobs, holds,
  ledger, nodes, receipts, keys, audit log compared before/after); and no gross
  timing difference against the route's own benign baseline. `SQLI_40018.md` also
  records the statement-level proof: the payloads arrive as **bound arguments**
  for the store calls these routes reach, never inside the SQL text.
* **Nothing is hidden.** Alerts stay in the report, every accepted instance is
  printed with its URL and its observed 5xx/DB-error/request counts, and the
  totals table separates "HIGH" from "HIGH accepted".

Note that this replaces the earlier path-scoped entry: that one was honest but
brittle, and it failed on a docs-only commit because the rule chose a different
route. The conditions above are strictly stronger than a path list — they are
per-instance, measured in the same run, and they fail closed when evidence,
status data or traffic is missing.

### What would falsify it

* any 40018 instance with non-empty `evidence`, or any 5xx or `DBERR` body on its
  path in the same run;
* the AST audit passing on a planted interpolated query (the break control above
  would catch that);
* the dynamic test finding a 5xx, a database error string, a state change after a
  refused request, or a timing blow-up on any input route;
* a new input route that the test does not cover — impossible by construction,
  since the list is enumerated from the routers, and the count is printed;
* any other HIGH alert in any pass.

## Everything else

Every other P1 row is `VERIFIED` in `PUSH_CHECKLIST.md`, with the command or CI
run that produced it. 6.1 and 4.4 are the only rows that are not met, and neither
is described anywhere in the product documentation as working. 5.5's 40018 entry
is not a row exception: the scanner findings are accepted per instance under the
four conditions above, and any other HIGH still fails the build.
