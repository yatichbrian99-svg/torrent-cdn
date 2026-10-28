#!/usr/bin/env python3
"""Smoke-test a live TCDN-AI staging deployment, step by step, PASS/FAIL.

Every step is independent and prints its own verdict, so a first failure does
not hide the rest of the picture. Nothing here is a mock: it talks to the real
staging URL, and the completion steps go through a **real worker** — if no
worker is heartbeating, step 2 says so and steps 5-6 fail with the coordinator's
own error rather than a fake result.

    export STAGING_BASE_URL=https://torrentcdn-staging.onrender.com
    export TCDN_AI_API_KEY=tcdn_ai_...          # from scripts/staging_bootstrap.py
    export DATABASE_URL=postgresql://...        # optional, for the ledger step
    python scripts/staging_smoke.py

Steps:
  1. health               GET /health
  2. status               flag on, DB/Redis up, >=1 worker with a fresh heartbeat
  3. models               /v1/models lists the registry and a worker serves one
  4. signup credit        the account has one signup grant row and its 500-credit
                          ledger entry (SQL: a balance alone proves nothing)
  5. chat (non-streaming) official OpenAI client, real worker, usage > 0
  6. chat (streaming)     SSE through the same client, include_usage honoured
  7. balance and usage    both jobs appear settled, balance moved, holds clear
  8. ledger invariant     splits add up, no non-positive entries, ledger balance
                          equals the API's own number

Exit code 0 only when every step passes. Steps 4 and 8 read the staging database,
so ``DATABASE_URL`` is required for a full run (the Render shell has it).
``--skip-ledger`` drops step 8 and says so in the summary; step 4 still needs the
database, because a grant cannot be proven from a balance.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

PROMPT = "Reply with one short sentence about why small models run on laptops."


@dataclass
class Result:
    name: str
    passed: bool
    detail: str
    notes: list[str] = field(default_factory=list)


class Smoke:
    def __init__(
        self, base_url: str, api_key: str, *, database_url: str | None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.database_url = database_url
        self.results: list[Result] = []
        self.model: str | None = None
        self.job_ids: list[str] = []
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _json(self, path: str) -> Any:
        """GET a JSON endpoint, failing with the server's own message.

        A bare KeyError from a 401 body (`{"error": ...}`) tells the owner
        nothing; the status code and the error message tell them which key or
        configuration is wrong.
        """
        response = await self.http.get(path)
        if response.status_code != 200:
            raise AssertionError(
                f"HTTP {response.status_code} on {path}: {response.text[:160]}"
            )
        return response.json()

    async def run(self, name: str, fn: Callable[[], Awaitable[str]]) -> None:
        started = time.perf_counter()
        try:
            detail = await fn()
            passed, notes = True, []
        except Exception as exc:  # one failed step must not stop the run
            passed, detail = False, f"{type(exc).__name__}: {exc}"
            notes = []
        elapsed = int((time.perf_counter() - started) * 1000)
        self.results.append(Result(name, passed, f"{detail} ({elapsed} ms)", notes))
        mark = "PASS" if passed else "FAIL"
        index = len(self.results)
        print(f"[{mark}] {index}. {name:<24} {detail}")
        for note in notes:
            print(f"         · {note}")

    # ── steps ───────────────────────────────────────────────────────────────

    async def health(self) -> str:
        response = await self.http.get("/health")
        assert response.status_code == 200, f"HTTP {response.status_code}"
        body = response.json()
        assert body.get("status") == "healthy", body
        return "healthy"

    async def status(self) -> str:
        body = await self._json("/api/ai/v1/status")
        flag = body.get("feature_flag", {})
        assert flag.get("on") and flag.get("routes_mounted"), f"AI flag is off: {flag}"
        assert body["database"]["status"] == "ok", body["database"]
        assert body["redis"]["status"] == "ok", (
            f"redis {body['redis']['status']} — {body['redis'].get('detail', '')}"
        )
        workers = body["workers"]
        assert workers["registered"] >= 1, (
            "no worker registered — start one (STAGING.md §5)"
        )
        assert workers["healthy"] >= 1, (
            f"{workers['registered']} worker(s) but none healthy; newest heartbeat "
            f"{workers['newest_heartbeat_age_seconds']}s old (timeout "
            f"{workers['heartbeat_timeout_seconds']}s)"
        )
        return (
            f"commit {body.get('commit') or 'unknown'}, {workers['healthy']}/"
            f"{workers['registered']} worker(s) healthy, heartbeat "
            f"{workers['newest_heartbeat_age_seconds']}s, models {workers['models_served']}"
        )

    async def models(self) -> str:
        response = await self.http.get("/v1/models")
        assert response.status_code == 200, (
            f"HTTP {response.status_code} {response.text[:200]}"
        )
        body = response.json()
        assert body.get("object") == "list", body
        data = body.get("data") or []
        assert data, "registry has no models — run staging_bootstrap.py"
        served = set()
        for entry in data:
            served.add(entry["id"])
            assert entry.get("tcdn", {}).get("gateway_mode") is True, entry
        # Prefer a model a live worker actually serves; a registry row nobody
        # serves is a placement failure waiting to happen.
        status = (await self._json("/api/ai/v1/status"))["workers"]
        worker_models = set(status.get("models_served") or [])
        candidates = sorted(served & worker_models) or sorted(served)
        self.model = candidates[0]
        if not (served & worker_models):
            raise AssertionError(
                f"no worker serves a registered model (registry {sorted(served)}, "
                f"workers {sorted(worker_models)})"
            )
        return f"{len(data)} model(s), testing {self.model}"

    async def signup_credit(self) -> str:
        balance = await self._json("/api/ai/v1/balance")
        account_id = balance["account_id"]
        expected = int(os.environ.get("SMOKE_EXPECT_SIGNUP_CREDITS", "500")) * 1_000_000
        # A balance alone cannot prove a grant: it could be a purchase, or a
        # grant minus one completion from an earlier run. Only the grant row and
        # its ledger entry are evidence, so the SQL read is required here rather
        # than approximated.
        assert self.database_url, (
            "DATABASE_URL is required to verify the signup grant row (run this in the "
            "staging shell, or export the staging DATABASE_URL)"
        )
        import asyncpg

        conn = await asyncpg.connect(self.database_url)
        try:
            granted = await conn.fetchval(
                "SELECT COUNT(*) FROM ai_signup_grants WHERE account_id = $1",
                account_id,
            )
            entry = await conn.fetchval(
                "SELECT COALESCE(SUM(amount_micro), 0) FROM ai_ledger_entries "
                "WHERE idempotency_key = 'signup:' || $1 AND kind = 'signup_credit'",
                account_id,
            )
        finally:
            await conn.close()
        assert int(granted) == 1, f"{granted} signup grant rows for {account_id}"
        assert int(entry) == expected, (
            f"signup ledger entry is {entry}, expected {expected}"
        )
        return (
            f"grant row + {int(entry) / 1_000_000:.0f} credits ledger entry, verified in "
            f"SQL; account {account_id[:8]}…"
        )

    async def _openai(self):
        try:
            from openai import AsyncOpenAI
        except ImportError:  # pragma: no cover - owner's environment
            raise AssertionError(
                "the official OpenAI client is not installed: pip install 'openai>=3,<4'"
            ) from None
        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=f"{self.base_url}/v1",
            max_retries=0,
            timeout=180.0,
        )

    async def chat_once(self) -> str:
        client = await self._openai()
        try:
            raw = await client.chat.completions.with_raw_response.create(
                model=self.model,
                messages=[{"role": "user", "content": PROMPT}],
                max_tokens=48,
            )
            response = raw.parse()
        finally:
            await client.close()
        text = response.choices[0].message.content or ""
        assert text.strip(), "completion was empty"
        usage = response.usage
        assert usage and usage.total_tokens > 0, f"usage missing: {usage}"
        job_id = raw.headers.get("x-tcdn-job-id")
        assert job_id, f"no X-TCDN-Job-Id header (headers: {dict(raw.headers)})"
        assert raw.headers.get("x-tcdn-confidentiality") == "none", (
            "expected the standard tier"
        )
        self.job_ids.append(job_id)
        return (
            f"{usage.prompt_tokens}+{usage.completion_tokens} tokens, job {job_id[:8]}…, "
            f"{text.strip()[:48]!r}"
        )

    async def chat_stream(self) -> str:
        client = await self._openai()
        pieces: list[str] = []
        finish: str | None = None
        usage: Any = None
        chunk_count = 0
        stream_job_id: str | None = None
        try:
            stream = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": PROMPT}],
                max_tokens=48,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                chunk_count += 1
                if chunk.usage:
                    usage = chunk.usage
                for choice in chunk.choices:
                    if choice.delta and choice.delta.content:
                        pieces.append(choice.delta.content)
                    if choice.finish_reason:
                        finish = choice.finish_reason
                # The chunk id is chatcmpl-<job_id>; the job id is what links
                # this completion back to ai_jobs for the ledger check.
                chunk_id = getattr(chunk, "id", "") or ""
                if stream_job_id is None and chunk_id.startswith("chatcmpl-"):
                    stream_job_id = chunk_id.split("chatcmpl-", 1)[1] or None
        finally:
            await client.close()
        text = "".join(pieces)
        assert text.strip(), "stream produced no content"
        assert finish == "stop", f"finish_reason was {finish!r}"
        assert chunk_count >= 2, f"only {chunk_count} SSE chunk(s)"
        assert usage is not None and usage.total_tokens > 0, (
            "include_usage produced no usage"
        )
        assert stream_job_id, "stream carried no chatcmpl-<job id>"
        if stream_job_id not in self.job_ids:
            self.job_ids.append(stream_job_id)
        return (
            f"{chunk_count} chunks, {usage.total_tokens} tokens, job "
            f"{stream_job_id[:8]}…, {text.strip()[:48]!r}"
        )

    async def balance_and_usage(self) -> str:
        balance = await self._json("/api/ai/v1/balance")
        usage = await self._json("/api/ai/v1/usage?limit=10")
        jobs = usage.get("jobs") or []
        seen = {job["job_id"] for job in jobs}
        missing = [job for job in self.job_ids if job not in seen]
        assert not missing, f"jobs missing from /usage: {missing}"
        unsettled = []
        for job in jobs:
            if job["job_id"] in self.job_ids:
                assert job["status"] == "settled", (
                    f"job {job['job_id']} is {job['status']}"
                )
                assert job["total_micro"] > 0, f"job {job['job_id']} billed nothing"
                unsettled.append(job)
        assert balance["held_micro"] == 0, (
            f"held_micro is {balance['held_micro']} — hold not released"
        )
        spent = sum(job["total_micro"] for job in unsettled)
        assert spent > 0, "no settlement"
        assert balance["available_micro"] == balance["micro_credits"], (
            "available != balance with no holds"
        )
        return (
            f"{len(unsettled)} settled job(s), {spent / 1_000_000:.4f} credits spent, "
            f"balance {balance['micro_credits'] / 1_000_000:.4f}, holds 0"
        )

    async def ledger_invariant(self) -> str:
        assert self.database_url, "DATABASE_URL is not set — cannot read the ledger"
        import asyncpg

        api_balance = await self._json("/api/ai/v1/balance")
        account_id = api_balance["account_id"]
        conn = await asyncpg.connect(self.database_url)
        try:
            async with conn.transaction(readonly=True):
                non_positive = await conn.fetchval(
                    "SELECT COUNT(*) FROM ai_ledger_entries WHERE amount_micro <= 0"
                )
                assert int(non_positive) == 0, f"{non_positive} ledger entries are <= 0"

                self_referential = await conn.fetchval(
                    "SELECT COUNT(*) FROM ai_ledger_entries WHERE debit_account = credit_account"
                )
                assert int(self_referential) == 0, (
                    f"{self_referential} entries debit and credit the same account"
                )

                bad_splits = await conn.fetchval(
                    "SELECT COUNT(*) FROM ai_jobs WHERE status = 'settled' "
                    "AND compute_micro + seeder_micro + platform_micro <> total_micro"
                )
                assert int(bad_splits) == 0, (
                    f"{bad_splits} settled job(s) whose split does not sum to total"
                )

                drifted = await conn.fetch(
                    """
                    SELECT j.job_id, j.total_micro, COALESCE(SUM(l.amount_micro), 0) AS ledger_micro
                    FROM ai_jobs j
                    LEFT JOIN ai_ledger_entries l ON l.job_id = j.job_id
                    WHERE j.status = 'settled'
                    GROUP BY j.job_id, j.total_micro
                    HAVING COALESCE(SUM(l.amount_micro), 0) <> j.total_micro
                    LIMIT 5
                    """
                )
                assert not drifted, (
                    f"settled jobs whose ledger does not match: {[dict(r) for r in drifted]}"
                )

                ledger_balance = await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(CASE WHEN credit_account = $1 THEN amount_micro
                                             ELSE -amount_micro END), 0)
                    FROM ai_ledger_entries
                    WHERE $1 IN (debit_account, credit_account)
                    """,
                    f"account:{account_id}:credits",
                )
                assert int(ledger_balance) == int(api_balance["micro_credits"]), (
                    f"ledger says {ledger_balance}, /balance says {api_balance['micro_credits']}"
                )

                my_jobs = await conn.fetch(
                    "SELECT job_id, status, total_micro FROM ai_jobs WHERE job_id = ANY($1::text[])",
                    self.job_ids,
                )
        finally:
            await conn.close()
        assert len(my_jobs) == len(self.job_ids), (
            "a job from this run is not in ai_jobs"
        )
        return (
            f"0 non-positive entries, splits and settlement sums agree, "
            f"ledger balance == /balance ({api_balance['micro_credits'] / 1_000_000:.4f} credits)"
        )

    # ── driver ──────────────────────────────────────────────────────────────

    async def run_all(self, *, skip_ledger: bool) -> int:
        print(f"TCDN-AI staging smoke test → {self.base_url}\n")
        await self.run("health", self.health)
        await self.run("status", self.status)
        await self.run("models", self.models)
        await self.run("signup credit", self.signup_credit)
        await self.run("chat (non-streaming)", self.chat_once)
        await self.run("chat (streaming)", self.chat_stream)
        await self.run("balance and usage", self.balance_and_usage)
        if skip_ledger:
            print(
                "[SKIP] 8. ledger invariant        --skip-ledger was passed (not a full pass)"
            )
        else:
            await self.run("ledger invariant", self.ledger_invariant)
        await self.http.aclose()

        passed = sum(1 for result in self.results if result.passed)
        total = len(self.results)
        failed = [result.name for result in self.results if not result.passed]
        print()
        if failed:
            print(
                f"RESULT: FAIL — {passed}/{total} steps passed; failed: {', '.join(failed)}"
            )
            return 1
        suffix = " (ledger step skipped)" if skip_ledger else ""
        print(f"RESULT: PASS — {passed}/{total} steps passed{suffix}")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.environ.get("STAGING_BASE_URL", ""), help="staging URL"
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("TCDN_AI_API_KEY", ""),
        help="inference API key",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="staging Postgres (read-only queries; enables steps 4 and 8)",
    )
    parser.add_argument("--skip-ledger", action="store_true")
    args = parser.parse_args()

    base_url = args.base_url.strip().rstrip("/")
    api_key = args.api_key.strip()
    if not base_url:
        raise SystemExit("set STAGING_BASE_URL (or pass --base-url)")
    if not api_key:
        raise SystemExit("set TCDN_AI_API_KEY (or pass --api-key)")
    if not args.database_url:
        print(
            "note: DATABASE_URL is unset, so the signup-credit step (and the ledger "
            "step) cannot run. Run this from the staging shell, or export the staging "
            "DATABASE_URL.\n"
        )

    smoke = Smoke(base_url, api_key, database_url=args.database_url.strip() or None)
    raise SystemExit(asyncio.run(smoke.run_all(skip_ledger=args.skip_ledger)))


if __name__ == "__main__":
    main()
