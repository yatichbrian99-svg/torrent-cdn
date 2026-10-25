# Evidence — real inference engine + real email verification (Section C item 2)

Run locally on 2026-10-24, CPU only. Reproduce with
`docs/tcdn-ai/DEV_SETUP.md` §6; the same steps run nightly in
`.github/workflows/tcdn-ai-engine.yml`.

**Engine:** Ollama 0.33.2, model `qwen2.5:0.5b` (397 MB), CPU (no GPU).
**Host:** Linux, Python 3.14 venv, Postgres 16.4 + Redis 7.4 in Docker.
**Coordinator:** real uvicorn process, `ENABLE_TCDN_AI=true`, real Postgres/Redis.
**Email:** application code real; the provider transport is a local stand-in
(`backend/scripts/dev/fake_resend.py`), reached through
`backend/scripts/dev/sitecustomize.py`, which only redirects the Resend SDK when
`TCDN_FAKE_RESEND_URL` is set.

## `pytest-engine.txt`

`TCDN_AI_ENGINE_URL=http://127.0.0.1:11434 TCDN_AI_ENGINE_MODEL=qwen2.5:0.5b
pytest tests/test_ai_engine.py -q` → **4 passed**. Covers real streaming,
engine-reported usage counts, stop sequences, and the transcript invariant
(records exist ⇔ the engine supplies token ids).

## `journey.txt` / `journey.json`

`scripts/live_ai_journey.py --backend ollama --model qwen2.5:0.5b
--email-stub-url http://127.0.0.1:8765` → **JOURNEY OK**, 12/12 steps, 8 s.

What it proves with real inference:

- the **official OpenAI Python client**, unmodified, gets `/v1/models` and both
  streaming and non-streaming completions through the gateway (65 tokens,
  19 stream chunks), with `tcdn.mode=gateway` / `prompt_visibility=gateway` and
  the standard-tier label on the response;
- the **SDK path** (end-to-end encrypted) returns real completions from the
  same engine (12-16 stream chunks), and the coordinator never sees the prompt;
- the **real email-verification flow**: the account is unverified after
  registration, the token is read from the message the provider received, and
  only `POST /auth/verify-email` marks it verified (`verified_before_link=false`);
- settlement: `held_micro=0`, jobs settled, `ledger_net_micro` equals the
  reported balance, splits sum exactly, no canary string in any AI table or in
  the coordinator log.

## Two bugs this run found (both fixed, with regression tests)

1. **The credit hold was below the real settlement cost, so every real-engine
   job was disputed.** The coordinator reserves credits from the client's token
   *estimate* (it never sees the prompt). A real engine counts the prompt after
   applying the model's chat template: a 7-token estimate became **48** input
   tokens with Qwen2.5. Settlement 240 µ > hold 117 µ → `settlement_exceeds_hold`
   on all 4 jobs of the first run. Fix: a configurable reservation headroom
   (`hold_input_margin_tokens=64`, `hold_input_margin_pct=10`) added to the hold
   only — the quoted price is unchanged. Regression test:
   `tests/test_ai_service.py::TestHoldHeadroomAndDisputes::test_engine_chat_template_overhead_settles_within_the_hold`.
2. **A disputed job never released its hold (H-1).** The first run left 4 holds
   open, freezing the customer's credits. Fix: settle-as-disputed now releases
   the reservation (nobody is paid until review). Regression test:
   `...::test_disputed_job_releases_the_hold`.

Both controls are break-tested (`scripts/break_tests.py`, 13/13 CAUGHT).

## Limitation this run exposed (documented, not fixed)

**Ollama-native streaming cannot feed the Part 3 transcript protocol.** Its
logprobs carry `token`/`bytes` and **no token id**, while a verifier re-scores by
real vocabulary id; the adapter therefore discards them and the receipt carries
no verification block. Ollama-served jobs are consequently not covered by
challenge-based verification yet. Fabricating ids would be worse. Options
documented in `LIMITATIONS.md` (map ids through the model tokenizer, or a
pinned quantized verifier).

## Not proven here

- **The GitHub Actions job itself has not run yet.** The workflow file is
  validated as YAML and its commands are exactly the ones executed above, but
  the runner's install path (pinned `v0.40.11`, `.tar.zst`) differs from the
  distro build used for this evidence, which reports `ollama 0.33.2`. First
  scheduled/manual run is the proof.
- **vLLM itself** is not installed (needs a GPU): the vLLM adapter is verified
  against Ollama's OpenAI-compatible `/v1` surface, the same wire protocol.
- No GPU, no confidential-computing hardware, no multi-worker placement, no
  concurrent load, no soak. Those are Section C items 3-4 and the Part 4 gate.
