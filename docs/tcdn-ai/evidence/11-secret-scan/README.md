# 11 — Full-history, all-branches secret scan (checklist 1.5)

**Date:** 2026-10-25 · **Branch:** `fix/tcdn-ai-redis-replay-nonce`

## What was scanned

Every **blob** reachable from **every local and remote ref** — not the working tree, not a
single-branch diff.

| Measure | Value |
|---|---|
| refs (local + remote-tracking) | 39 |
| commits reachable from `--all` | 396 |
| distinct blobs scanned | 1661 |
| blobs skipped | 1 (binary); 0 over the 4 MB cap |
| **high-signal findings** | **0** |
| key files (`.pem`/`.key`/`.p12`/`.pfx`/`.jks`/`.keystore`) in any ref | 0 |

## How

`git rev-list --objects --all` enumerated every blob; each was streamed through
`git cat-file --batch` and matched against the high-signal pattern set from
`backend/scripts/secret_scan.sh` (GitHub PATs and OAuth/server tokens, AWS key IDs and
secret-access-key assignments, Stripe/Paystack live and restricted keys, Stripe webhook
secrets, SendGrid, Slack tokens and webhooks, npm and PyPI tokens, Google API keys, Twilio,
Resend, Discord webhooks, and `-----BEGIN … PRIVATE KEY-----` blocks).

A second, deliberately noisier pass looked for credential-shaped text that the high-signal
set ignores — DSNs with an inline password (`postgres://user:pass@…`, redis, mongo, mysql,
amqp) and `password|secret|token|api_key = "…"` assignments. Those are reported as
**review-class**, not as leaks, because test fixtures live in exactly that shape.

## Review-class triage

209 review-class hits, deduplicated to 24 distinct path/line pairs in `history-scan.txt`
(all values replaced with `***REDACTED***` before writing this file).

Every distinct value was classified; the classes are placeholder / env-indirection /
masked / empty / `UNCLASSIFIED`. Only `UNCLASSIFIED` needs a human, and there were five
distinct values:

| Value class | Where | Verdict |
|---|---|---|
| `securePass123` | `backend/tests/*` bcrypt fixtures | test fixture |
| `JBSWY3DPEHPK3PXP` | `backend/tests/test_mfa.py` | the RFC 6238 published TOTP test vector |
| `s3cret-bootstrap` | `backend/tests/test_security.py` ADMIN_SETUP_TOKEN fixture | test fixture |
| `live_wrongkey…` | `backend/scripts/live_e2e.py` | deliberately invalid stream key |
| `tok.ensig` (truncated) | test token | test fixture |

The 116 placeholder-class hits are `postgres://user:password@…`-style defaults in
`.env.example`, `docker-compose.yml`, `app/config.py` and the docs.

## Limitation — stated, not hidden

**gitleaks is not installed in this environment**, so this is a pattern scan, not the
gitleaks full-history run that `security.yml` performs in CI. It closes the "whole history,
all branches" question with the pattern set above and adds credential-in-URL coverage that
the shipped script does not have; it does not claim gitleaks' entropy heuristics.

## Reproduce

```bash
# whole history, every ref (this directory's scan.py, committed so the run is reproducible)
python3 docs/tcdn-ai/evidence/11-secret-scan/scan.py \
  > docs/tcdn-ai/evidence/11-secret-scan/history-scan.txt
echo "exit=$?"   # exit 1 iff a high-signal finding exists

# working-tree variant shipped with the repo
bash backend/scripts/secret_scan.sh --full
```

`scan.py` prints redacted matches only, so its own output is safe to commit.
