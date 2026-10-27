# TCDN-AI — runbook

Everything here is deliberately explicit: no step relies on an undocumented
secret, and every destructive action names its blast radius.

## 1. Deploy the coordinator

1. Set the required env (see `.env.example`, prefix `TCDN_AI_`). Production
   **must** set `TCDN_AI_ENVIRONMENT=production`, `TCDN_AI_API_KEY_PEPPER`, a
   keyring source (`TCDN_AI_COORDINATOR_KEY_FILE` or the secret-manager-injected
   `TCDN_AI_COORDINATOR_KEYRING_JSON`), `TCDN_AI_PAYMENT_PROVIDER=paystack`,
   `TCDN_AI_PAYSTACK_SECRET_KEY`, and leave simulated attestation / dev backend
   disabled. Startup validation refuses an insecure combination.
2. Create the keyring once, on the host that will serve it:

   ```bash
   install -d -m 700 /run/secrets
   python - <<'PY'
   from app.ai.keyring import FileSecretStore, KeyRing
   FileSecretStore("/run/secrets/tcdn-ai-keys.json").save(
       KeyRing.generate("coordinator", overlap_seconds=3600).to_secret()
   )
   PY
   stat -c '%a' /run/secrets/tcdn-ai-keys.json   # must print 600
   ```

   The coordinator refuses a keyring file with group/other permissions and
   refuses to auto-generate one in production. Platforms that inject secrets as
   env vars (Kubernetes, Render, systemd credentials) can instead put the same
   JSON in `TCDN_AI_COORDINATOR_KEYRING_JSON`; that store is read-only, so the
   coordinator must never be expected to create keys there.
3. Restart the backend. `AI_TABLES_DDL` runs idempotently at startup
   (`app.main.run_startup_migrations`).
4. Provision a developer key:

   ```bash
   python scripts/ai_provision.py --account-id <users.id> --scopes inference,worker
   ```

   Copy the key immediately; it is printed once and stored only as an HMAC.
5. Seed the model registry with **real** manifests:
   `--models-file deploy/tcdn-ai/models.example.json` (copy the shape, replace
   every hash with the hash of the manifest you actually signed).

## 2. Deploy a worker

Prerequisites: a GPU engine already listening on loopback (vLLM on `:8000` or
Ollama on `:11434`), a TLS certificate for the worker hostname, and a worker API
key (`worker` scope).

```bash
export TCDN_AI_COORDINATOR_URL=https://api.example
export TCDN_AI_WORKER_API_KEY=tcdn_ai_…
export TCDN_AI_WORKER_MODELS='llama-3.1-8b-instruct=<manifest sha256>'
export TCDN_AI_BACKEND=vllm
export TCDN_AI_IDENTITY_FILE=/var/lib/tcdn-ai/worker-identity.json
python scripts/ai_worker.py --public-url https://worker1.example \
  --tls-cert /etc/ssl/worker.crt --tls-key /etc/ssl/worker.key
```

The launcher generates the identity file with mode 0600 on first boot, pins the
coordinator keys (verifying the bundle signature), registers, and serves TLS 1.3
only. `--allow-plaintext-loopback` exists solely for local development.

## 3. Rotate coordinator keys

1. `KeyRing.rotate()` (or restart with a new active key written by the secret
   store). The old key retires but keeps verifying for
   `TCDN_AI_KEY_OVERLAP_SECONDS` (default 3600).
2. Publish the new bundle: SDKs and workers fetch `/api/ai/v1/keys/coordinator`.
3. Update pinned keys on SDK clients to include **current + next** key before the
   overlap expires.
4. After the overlap, revoke the old key id. Revoking the last active signing key
   is refused by design; rotate first.

## 4. Revoke a worker

```python
from app.ai.keyring import load_keyring
from app.ai.config import get_ai_settings
load_keyring(get_ai_settings()).revoke("<key_id>", "compromise")
```
Then mark the node suspended in the registry (`UPDATE ai_nodes SET suspended =
TRUE WHERE node_id = …`). Revoked keys are rejected on the next ticket/receipt
verification. Worker revocation does **not** refund in-flight holds; release them
explicitly after review.

## 5. Incident response

| Incident | Immediate action | Follow-up |
|---|---|---|
| Suspected prompt exposure | Disable the affected worker (`suspended = TRUE`), rotate its identity key, notify the tenant | Preserve `ai_audit_log`; the coordinator has no prompt copy to leak, so scope the investigation to the worker host |
| Coordinator signing key compromise | Rotate immediately, publish bundle, revoke old key id, re-pin SDKs | Invalidate outstanding tickets (they expire ≤ `TCDN_AI_TICKET_TTL_SECONDS`), audit placements in the overlap window |
| API key leak | Revoke the key (`UPDATE ai_api_keys SET status='revoked'`), issue a new one | Check usage for anomalous jobs and holds |
| Corrupt shards reported | Inspect `ai_transfer_receipts` + worker logs for the banned peer id | Drop the seeder's reputation, require re-verification before it earns again |
| Payment webhook abuse | Rotate the provider secret, replay the audit log by reference | Idempotency keys make duplicate credits impossible, but verify balances anyway |
| Ledger imbalance alarm | Freeze new placements (`TCDN_AI_ENABLED=false`) | Reconcile `ai_ledger_entries` (every posting is double-entry and sums to zero), replay `settle:*` keys |

## 6. Payouts

> **Not automated in this build (LIMITATIONS.md §13).** Nothing ingests transfer
> receipts or issues a payout yet. The building blocks are in
> `app/ai/payouts.py` and are tested; until an ingestion endpoint exists, do not
> pay seeders from self-reported logs.

- Seeder pool accrues to `pool:seeders` at settlement; the intended distribution
  is proportional to accepted bytes from **co-signed transfer receipts**
  (`payouts.review_claims` + `payouts.distribute`, largest remainder, exact sum).
- Apply the payout delay (`TCDN_AI_SEEDER_PAYOUT_DELAY_SECONDS`) before paying, so
  wash-trading flags can be reviewed.
- Ledger entries in `ai_ledger_entries` (plus receipts once ingested) are the
  source of truth for any statement; reconcile before paying anything.

## 7. Monitoring

Watch: placement latency and error rate, `no_eligible_worker` rate, hold/settlement
lag, the `job.billing_review` rate (jobs billed from a receipt whose client ack
disagreed, that overclaimed the gateway's own count, or that reached the hold
ceiling — the worklist for the refund path), per-node `overclaim_rate_pct` and
`node.held_for_review` events (a node suspended and its payouts held:
`SELECT node_id, settled_jobs, overclaim_events, mismatch_events, review_reason
FROM ai_nodes WHERE review_reason IS NOT NULL`), `payout.held_node_review`,
hash/attestation failures, shard failure rate, ledger balance delta (must be
zero), and `ai_audit_log` growth.

**Clearing a node review** (after the operator has seen the jobs): set
`review_reason = NULL` and `suspended = FALSE` on that `ai_nodes` row, then pay or
refund the held `ai_payouts` rows explicitly — a hold never releases on its own,
because the money it protects is the money under suspicion. The node's counters
are deliberately *not* reset: the next overclaim starts from the same history, so
a cleared node that reoffends is held again on a much smaller sample.

No prompt content or API key may ever appear in logs; the `TestLogHygiene` suite
enforces the code-side guarantee, but log shipping configuration is still yours to
review.

## 8. Backup and restore

- Back up `ai_ledger_entries`, `ai_holds`, `ai_jobs`, `ai_receipts`,
  `ai_accounts`, `ai_api_keys`, `ai_nodes`, `ai_models`, `ai_model_prices`.
- The keyring is **not** in the database: back it up separately and encrypt it.
  Losing it invalidates every outstanding ticket and every SDK pin.
- Restore order: keyring → schema (startup DDL) → ledger/holds → jobs/receipts →
  registry. Recompute balances from entries; never restore a balance cache.
