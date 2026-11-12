#!/usr/bin/env python3
"""Live end-to-end journey for TCDN-AI (one command, real HTTP, real crypto).

Runs scenario 1 of the verification gate against a *running* coordinator:

    signup -> email verified -> 2FA enrolled and enforced -> API keys ->
    paid credits through the fake provider -> inference through the official
    OpenAI client (gateway mode) and through the project SDK (end-to-end
    encrypted) -> balance/usage -> ledger and privacy invariants.

Two things are configurable, because the honesty of the result depends on it:

* **Inference backend** (`--backend echo|ollama|vllm`, `--engine-url`):
  `echo` is the labelled SIMULATED dev stub; `ollama`/`vllm` drive a real
  engine on loopback. Run with a real engine to prove real inference and
  streaming — the step detail records which backend ran.
* **Email verification** (`--email-stub-url`): with no email provider the app
  auto-verifies new accounts (a documented fallback). Pass the URL of
  `scripts/dev/fake_resend.py` to exercise the **real** flow instead: the
  account must stay unverified until the token from the recorded email is used
  against `POST /auth/verify-email`. The step detail says which path ran.

Everything else is real: Postgres, Redis, HTTP, Ed25519 tickets, X25519 + HKDF
+ AES-GCM transport, signed receipts.

Prerequisites (see docs/tcdn-ai/DEV_SETUP.md):
  * coordinator on --base-url with ENABLE_TCDN_AI=true
  * Postgres from --database-url
  * `openai` (requirements-dev.txt) for the official-client step

Usage:
    python scripts/live_ai_journey.py --base-url http://127.0.0.1:8000 \
        --database-url postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn_e2e \
        --out ../docs/tcdn-ai/evidence/04-e2e/journey.json

    # real engine + real email flow (see docs/tcdn-ai/DEV_SETUP.md §7):
    python scripts/live_ai_journey.py --backend ollama \
        --engine-url http://127.0.0.1:11434 --model qwen2.5:0.5b \
        --email-stub-url http://127.0.0.1:8765 --email-out /tmp/tcdn-ai-emails.jsonl

Exit 0 = every step passed, 1 = a step failed (the failing step is printed).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL = "llama-3.1-8b-instruct"
VERIFICATION_LINK = "/verify-email?token="
FAKE_WEBHOOK_SECRET = b"dev-only-fake-webhook-secret"
CANARY = "CANARY-" + secrets.token_hex(8)


class StepFailed(RuntimeError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise StepFailed(message)


def totp(secret: str) -> str:
    from app.mfa import totp_code

    return totp_code(secret)


def manifest_hash_for(model: str) -> str:
    """Stable per-model manifest hash so repeated runs register the same model."""
    return hashlib.sha256(f"journey-manifest:{model}".encode()).hexdigest()


def verification_link_from_email(path: Path, recipient: str) -> str:
    """Find the verification link addressed to *this* run in the provider log.

    Newest first, and filtered by recipient: the file is append-only, so a naive
    scan would happily return a token from an earlier run (and silently verify
    the wrong account).
    """
    deadline = time.time() + 30
    while time.time() < deadline:
        if path.exists():
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                message = json.loads(line)["message"]
                to = message.get("to")
                recipients = to if isinstance(to, list) else [to]
                if recipient not in (recipients or []):
                    continue
                haystack = f"{message.get('text', '')}{message.get('html', '')}"
                index = haystack.find(VERIFICATION_LINK)
                if index < 0:
                    continue
                tail = haystack[index + len(VERIFICATION_LINK) :]
                token = ""
                for char in tail:
                    if char.isalnum() or char in "-_.~":
                        token += char
                    else:
                        break
                if token:
                    return token
        time.sleep(0.5)
    raise StepFailed(f"no verification link was sent to {recipient} (looked in {path})")


async def run(args: argparse.Namespace) -> dict:
    base = args.base_url.rstrip("/")
    model = args.model
    manifest_hash = manifest_hash_for(model)
    results: dict[str, object] = {
        "base_url": base,
        "model": model,
        "backend": args.backend,
        "canary": CANARY,
    }
    client = httpx.Client(base_url=base, timeout=60.0)
    pool = await asyncpg.create_pool(args.database_url, min_size=1, max_size=4)
    email = f"journey-{uuid.uuid4().hex[:10]}@example.test"
    # Generated per run: the account is disposable, one per journey, and a
    # literal password in a committed script is indistinguishable from a leak
    # to a secrets scanner (gitleaks flagged exactly this line).
    password = f"Journey-{uuid.uuid4().hex[:16]}-Aa1!"
    worker = None

    def step(name: str, detail: object = None) -> None:
        results.setdefault("steps", []).append(
            {"step": name, "ok": True, "detail": detail}
        )
        print(f"PASS {name}" + (f" :: {detail}" if detail else ""))

    try:
        # 0 ── health and feature flag ─────────────────────────────────────
        health = client.get("/health")
        check(health.status_code == 200, f"health returned {health.status_code}")
        step("health", health.json().get("status"))

        # 1 ── signup, email verification ─────────────────────────────────
        register = client.post(
            "/auth/register",
            json={"email": email, "password": password, "terms_accepted": True},
        )
        check(
            register.status_code in (200, 201),
            f"register {register.status_code}: {register.text}",
        )
        body = register.json()
        check("access_token" in body, f"register did not mint a session: {body}")
        token = body["access_token"]
        user = await pool.fetchrow(
            "SELECT id, verified, mfa_enabled, is_admin FROM users WHERE email = $1",
            email,
        )
        check(user is not None, "user row missing after registration")
        account_id = str(user["id"])
        results["account_id"] = account_id

        if args.email_stub_url:
            # REAL flow: an account with a configured email provider must stay
            # unverified until the token that was actually mailed is presented.
            check(
                not bool(user["verified"]),
                "account was auto-verified although an email provider is configured",
            )
            email_out = Path(args.email_out or "/tmp/tcdn-ai-emails.jsonl")
            email_token = verification_link_from_email(email_out, email)
            verified = client.post("/auth/verify-email", json={"token": email_token})
            check(
                verified.status_code == 200,
                f"verify-email {verified.status_code}: {verified.text}",
            )
            after_verify = await pool.fetchrow(
                "SELECT verified FROM users WHERE id = $1", user["id"]
            )
            check(
                bool(after_verify["verified"]),
                "verify-email returned 200 but the account is still unverified",
            )
            results["email_verification"] = {
                "mode": "provider-link",
                "provider": args.email_stub_url,
                "verified_before_link": False,
            }
            step(
                "signup + real email verification (unverified until the emailed token was used)",
                results["email_verification"],
            )
        else:
            check(
                bool(user["verified"]),
                "account is unverified: this coordinator has an email provider configured, "
                "so pass --email-stub-url (the emailed token cannot be guessed). With no "
                "provider at all the app auto-verifies and this step passes as such.",
            )
            results["email_verification"] = {
                "mode": "auto-verified",
                "reason": "no email provider configured (documented fallback)",
            }
            step("signup (auto-verified: no email provider configured)", None)

        def auth_headers() -> dict:
            return {"Authorization": f"Bearer {token}"}

        # 2 ── 2FA enrollment and enforcement ─────────────────────────────
        setup = client.post("/auth/mfa/setup", headers=auth_headers())
        check(setup.status_code == 200, f"mfa setup {setup.status_code}: {setup.text}")
        secret = setup.json()["secret"]
        enable = client.post(
            "/auth/mfa/enable", headers=auth_headers(), json={"code": totp(secret)}
        )
        check(
            enable.status_code == 200, f"mfa enable {enable.status_code}: {enable.text}"
        )
        flags = await pool.fetchrow(
            "SELECT mfa_enabled FROM users WHERE id = $1", user["id"]
        )
        check(bool(flags["mfa_enabled"]), "mfa_enabled is still false after enable")
        # Prove it is enforced: a fresh password login must stop at the gate.
        login = client.post("/auth/login", json={"email": email, "password": password})
        check(login.status_code == 200, f"login {login.status_code}: {login.text}")
        gate = login.json()
        check(bool(gate.get("mfa_required")), f"login bypassed MFA: {gate}")
        confirm = client.post(
            "/auth/mfa/confirm",
            json={"mfa_token": gate["mfa_token"], "code": totp(secret)},
        )
        check(
            confirm.status_code == 200,
            f"mfa confirm {confirm.status_code}: {confirm.text}",
        )
        token = confirm.json()["access_token"]
        step("2FA enrolled and enforced at login")

        # 3 ── API keys through the admin surface ────────────────────────
        # Fixture: make this account an admin so the real admin endpoint can be
        # exercised over HTTP (no key is inserted behind the API).
        await pool.execute("UPDATE users SET is_admin = TRUE WHERE id = $1", user["id"])
        gate = client.post(
            "/auth/login", json={"email": email, "password": password}
        ).json()
        token = client.post(
            "/auth/mfa/confirm",
            json={"mfa_token": gate["mfa_token"], "code": totp(secret)},
        ).json()["access_token"]
        dev_key = client.post(
            "/api/ai/v1/admin/api-keys",
            headers=auth_headers(),
            params={"account_id": account_id, "scopes": "inference"},
        )
        check(
            dev_key.status_code == 200,
            f"admin api-keys {dev_key.status_code}: {dev_key.text}",
        )
        api_key = dev_key.json()["api_key"]
        worker_key = client.post(
            "/api/ai/v1/admin/api-keys",
            headers=auth_headers(),
            params={"account_id": account_id, "scopes": "worker"},
        )
        check(
            worker_key.status_code == 200,
            f"worker key {worker_key.status_code}: {worker_key.text}",
        )
        worker_api_key = worker_key.json()["api_key"]
        results["api_key_prefix"] = api_key[:16] + "…"
        step("API key created (developer + worker scopes)")

        # 4 ── model registry via the documented provisioning script ─────
        models_file = Path("/tmp/tcdn-ai-journey-models.json")
        models_file.write_text(
            json.dumps(
                [
                    {
                        "model_id": model,
                        "name": "Llama 3.1 8B Instruct" if model == MODEL else model,
                        "manifest_hash": manifest_hash,
                        "size_bytes": 16_000_000_000 if model == MODEL else 400_000_000,
                        "required_vram_gb": args.gpu_vram_gb,
                        "license_note": (
                            "Llama 3.1 Community License"
                            if model == MODEL
                            else "Apache-2.0 (Qwen2.5-0.5B)"
                        ),
                        "price_class": "small",
                        "format": "safetensors",
                    }
                ]
            )
        )
        provision = await asyncio.create_subprocess_exec(
            sys.executable,
            "scripts/ai_provision.py",
            "--account-id",
            account_id,
            "--scopes",
            "inference",
            "--models-file",
            str(models_file),
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": args.database_url},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await provision.communicate()
        provision_stdout = out.decode(errors="replace")
        check(
            provision.returncode == 0,
            "provisioning failed: "
            + (err.decode(errors="replace") or provision_stdout)[-500:],
        )
        check(
            f"registered model {model}" in provision_stdout,
            "model registration not confirmed",
        )
        step("model registered", model)

        # 5 ── buy credits through the fake provider webhook ─────────────
        #
        # Read the balance first: the account already carries the signup credit
        # (it was granted when the emailed link verified the address), so an
        # absolute "balance == purchase" assertion was only ever true while that
        # grant had no production trigger. "Start + one purchase" also proves the
        # duplicate callback was ignored (start + two would fail the check).
        before = client.get(
            "/api/ai/v1/balance", headers={"Authorization": f"Bearer {api_key}"}
        )
        check(before.status_code == 200, f"balance {before.status_code}: {before.text}")
        starting_micro = int(before.json()["micro_credits"])
        amount_micro = 5_000_000  # $0.05 of credits
        reference = "journey-" + uuid.uuid4().hex
        event = json.dumps(
            {
                "event": "charge.success",
                "data": {
                    "reference": reference,
                    "amount_micro": amount_micro,
                    "currency": "USD",
                    "metadata": {"account_id": account_id},
                },
            }
        ).encode()
        signature = hmac.new(FAKE_WEBHOOK_SECRET, event, hashlib.sha256).hexdigest()
        paid = client.post(
            "/api/ai/v1/billing/webhook",
            content=event,
            headers={"x-tcdn-signature": signature, "content-type": "application/json"},
        )
        check(paid.status_code == 200, f"webhook {paid.status_code}: {paid.text}")
        duplicate = client.post(
            "/api/ai/v1/billing/webhook",
            content=event,
            headers={"x-tcdn-signature": signature, "content-type": "application/json"},
        )
        check(
            duplicate.status_code == 200, f"duplicate webhook {duplicate.status_code}"
        )
        balance = client.get(
            "/api/ai/v1/balance", headers={"Authorization": f"Bearer {api_key}"}
        )
        check(
            balance.status_code == 200, f"balance {balance.status_code}: {balance.text}"
        )
        check(
            int(balance.json()["micro_credits"]) == starting_micro + amount_micro,
            f"balance after one purchase (and a duplicate) is {balance.json()}; "
            f"started at {starting_micro} and bought {amount_micro}",
        )
        pre_inference_micro = int(balance.json()["micro_credits"])
        step(
            "credits purchased; duplicate callback was idempotent",
            {
                "purchase_micro": amount_micro,
                "balance_micro": pre_inference_micro,
                "signup_grant_micro": starting_micro,
            },
        )

        # 6 ── worker comes online (echo stub or a real engine) ────────
        worker_env = {
            **os.environ,
            "TCDN_AI_COORDINATOR_URL": base,
            "TCDN_AI_WORKER_API_KEY": worker_api_key,
            "TCDN_AI_WORKER_MODELS": f"{model}={manifest_hash}",
            "TCDN_AI_BACKEND": args.backend,
            "TCDN_AI_OLLAMA_URL": args.engine_url or "http://127.0.0.1:11434",
            "TCDN_AI_VLLM_URL": args.engine_url or "http://127.0.0.1:8000",
            "TCDN_AI_ENGINE_VERSION": args.engine_version,
            # The echo backend is a labelled SIMULATED dev stub; it declares
            # the GPU capacity the registered model requires so scheduling can
            # be exercised without real hardware. With a real engine these are
            # the capacity the operator declares (benchmarking is Section C
            # item 11); the engine itself runs on CPU in CI.
            "TCDN_AI_GPU_VRAM_GB": str(args.gpu_vram_gb),
            "TCDN_AI_GPU_MODEL": args.gpu_model,
            "TCDN_AI_IDENTITY_FILE": f"/tmp/tcdn-ai-worker-{uuid.uuid4().hex[:6]}.json",
            "TCDN_AI_API_KEY_PEPPER": os.environ.get(
                "TCDN_AI_API_KEY_PEPPER", "dev-pepper-change-me"
            ),
            "TCDN_AI_ENVIRONMENT": "development",
        }
        worker_log = open("/tmp/tcdn-ai-worker.log", "w")
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "scripts/ai_worker.py",
            "--host",
            "127.0.0.1",
            "--port",
            "9100",
            "--public-url",
            "http://127.0.0.1:9100",
            "--allow-plaintext-loopback",
            cwd=ROOT,
            env=worker_env,
            stdout=worker_log,
            stderr=asyncio.subprocess.STDOUT,
        )
        deadline = time.time() + 60
        node_id = None
        while time.time() < deadline:
            # Recent heartbeat only: a stale node row from an earlier run must
            # never be mistaken for the worker this run just started.
            row = await pool.fetchrow(
                """
                SELECT node_id, healthy FROM ai_nodes
                WHERE last_heartbeat >= EXTRACT(EPOCH FROM NOW()) - 120
                ORDER BY last_heartbeat DESC LIMIT 1
                """
            )
            if row is not None:
                node_id = str(row["node_id"])
                break
            await asyncio.sleep(1)
        check(
            node_id is not None, "worker never registered (see /tmp/tcdn-ai-worker.log)"
        )
        await asyncio.sleep(2)  # let one heartbeat land so scheduling sees it online
        step("worker registered", node_id)

        # 7a ── gateway mode: the OFFICIAL OpenAI client, unmodified ──
        # This is the drop-in path (Section C item 1). The gateway sees the
        # prompt in transit, which every response states via tcdn.*.
        from openai import OpenAI as OfficialOpenAI

        official = OfficialOpenAI(api_key=api_key, base_url=f"{base}/v1", timeout=120.0)
        listed = official.models.list()
        check(
            any(entry.id == model for entry in listed.data),
            f"/v1/models does not list {model}: {[e.id for e in listed.data]}",
        )
        gateway_completion = official.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": f"gateway {CANARY}"}],
            max_tokens=16,
        )
        gateway_text = gateway_completion.choices[0].message.content or ""
        check(
            gateway_completion.object == "chat.completion",
            "not a chat.completion object",
        )
        check(bool(gateway_text), "gateway completion carried no content")
        gateway_tcdn = (gateway_completion.model_extra or {}).get("tcdn") or {}
        check(
            gateway_tcdn.get("mode") == "gateway",
            f"gateway response did not declare gateway mode: {gateway_tcdn}",
        )
        check(
            gateway_tcdn.get("prompt_visibility") == "gateway",
            f"gateway response did not disclose prompt visibility: {gateway_tcdn}",
        )
        check(
            gateway_tcdn.get("confidentiality") == "none",
            f"gateway mode must be standard tier: {gateway_tcdn}",
        )
        gateway_chunks = list(
            official.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": f"gateway-stream {CANARY}"}],
                max_tokens=16,
                stream=True,
                stream_options={"include_usage": True},
            )
        )
        gateway_stream_text = "".join(
            chunk.choices[0].delta.content or ""
            for chunk in gateway_chunks
            if chunk.choices
        )
        gateway_usage = next(
            (chunk.usage for chunk in gateway_chunks if getattr(chunk, "usage", None)),
            None,
        )
        check(bool(gateway_stream_text), "gateway stream produced no content")
        check(gateway_usage is not None, "gateway stream emitted no usage chunk")
        check(
            int(gateway_usage.total_tokens) > 0, "gateway stream reported zero tokens"
        )
        official.close()
        results["openai_client_compatibility"] = {
            "verdict": "IMPLEMENTED (gateway mode, official client)",
            "models": [entry.id for entry in listed.data],
            "non_streaming_tokens": gateway_completion.usage.total_tokens,
            "streaming_chunks": len(gateway_chunks),
            "prompt_visibility": gateway_tcdn.get("prompt_visibility"),
            "backend": args.backend,
        }
        step(
            "official OpenAI client: /v1/models + streaming and non-streaming",
            results["openai_client_compatibility"],
        )

        # 7b ── SDK mode: end-to-end encrypted, coordinator never sees the prompt ──
        from tcdn_ai_sdk import Client as AI

        ai = AI(api_key=api_key, base_url=base, timeout=120.0)
        completion = ai.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": f"hello {CANARY}"}],
            max_tokens=16,
        )
        step(
            "non-streaming inference (TCDN-AI SDK, OpenAI-shaped response)",
            {
                "tokens": completion.usage.total_tokens,
                "finish": completion.choices[0].finish_reason,
            },
        )
        check(completion.usage.total_tokens > 0, "completion reported zero tokens")
        check(
            completion.choices[0].message.content is not None,
            "completion carried no content",
        )

        streamed: list[str] = []
        stream = ai.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": f"stream {CANARY}"}],
            max_tokens=16,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                streamed.append(chunk.choices[0].delta.content)
        step("streaming inference (TCDN-AI SDK)", {"chunks": len(streamed)})
        check(len(streamed) > 0, "stream produced no content chunks")
        ai.close()

        # 8 ── usage and balance reflect both jobs ─────────────────────
        # Settlement needs the worker's signed receipt and the client ack; both
        # are submitted asynchronously, so poll briefly instead of racing them.
        deadline = time.time() + 60
        after: dict = {}
        while time.time() < deadline:
            after = client.get(
                "/api/ai/v1/balance", headers={"Authorization": f"Bearer {api_key}"}
            ).json()
            if (
                int(after.get("held_micro", 0)) == 0
                and int(after.get("micro_credits", 0)) < pre_inference_micro
            ):
                break
            await asyncio.sleep(1)
        usage = client.get(
            "/api/ai/v1/usage", headers={"Authorization": f"Bearer {api_key}"}
        )
        check(usage.status_code == 200, f"usage {usage.status_code}: {usage.text}")
        usage_body = usage.json()
        check(len(usage_body.get("jobs", [])) >= 4, f"usage lists {usage_body}")
        after = client.get(
            "/api/ai/v1/balance", headers={"Authorization": f"Bearer {api_key}"}
        ).json()
        check(
            int(after["micro_credits"]) < pre_inference_micro,
            f"balance did not decrease after inference: {after} "
            f"(was {pre_inference_micro} before the jobs)",
        )
        check(
            int(after["held_micro"]) == 0, f"holds left open after settlement: {after}"
        )
        results["balance_after"] = after
        step("usage and balance updated", after)

        # 9 ── invariants ─────────────────────────────────────────────
        from app.ai.ledger import account_credits

        ledger = await pool.fetchrow(
            """
            SELECT COUNT(*) AS entries,
                   COALESCE(SUM(CASE WHEN credit_account = $1 THEN amount_micro
                                     ELSE -amount_micro END), 0) AS net,
                   COUNT(*) FILTER (WHERE amount_micro <= 0) AS non_positive
            FROM ai_ledger_entries
            WHERE $1 IN (debit_account, credit_account)
            """,
            account_credits(account_id),
        )
        check(int(ledger["non_positive"]) == 0, "non-positive ledger postings exist")
        check(int(ledger["entries"]) >= 2, "no ledger postings were written")
        check(
            int(ledger["net"]) == int(after["micro_credits"]),
            "ledger net does not equal the reported balance",
        )
        splits = await pool.fetch(
            """
            SELECT job_id, total_micro, compute_micro, seeder_micro, platform_micro
            FROM ai_jobs WHERE account_id = $1 AND status = 'settled'
            """,
            user["id"],
        )
        for row in splits:
            check(
                int(row["compute_micro"])
                + int(row["seeder_micro"])
                + int(row["platform_micro"])
                == int(row["total_micro"]),
                f"split does not sum to the job total: {dict(row)}",
            )
        jobs = await pool.fetch(
            "SELECT job_id, status FROM ai_jobs WHERE account_id = $1", user["id"]
        )
        check(
            all(j["status"] in {"settled", "refunded", "failed"} for j in jobs),
            f"job left non-terminal: {[dict(j) for j in jobs]}",
        )
        open_holds = await pool.fetchval(
            "SELECT COUNT(*) FROM ai_holds WHERE account_id = $1 AND status = 'open'",
            user["id"],
        )
        check(int(open_holds) == 0, f"{open_holds} orphaned holds remain")
        # Privacy: the canary prompt must not be stored anywhere in the DB.
        leaked = await pool.fetchval(
            """
            SELECT COUNT(*) FROM (
              SELECT row_to_json(t)::text AS blob FROM ai_jobs t
              UNION ALL SELECT row_to_json(t)::text FROM ai_receipts t
              UNION ALL SELECT row_to_json(t)::text FROM ai_audit_log t
              UNION ALL SELECT row_to_json(t)::text FROM ai_ledger_entries t
            ) s WHERE blob LIKE '%' || $1 || '%'
            """,
            CANARY,
        )
        check(int(leaked) == 0, "canary prompt found in AI tables")
        log_text = (
            Path("/tmp/tcdn-coordinator.log").read_text(errors="replace")
            if Path("/tmp/tcdn-coordinator.log").exists()
            else ""
        )
        check(CANARY not in log_text, "canary prompt found in coordinator log")
        results["invariants"] = {
            "ledger_entries": int(ledger["entries"]),
            "ledger_net_micro": int(ledger["net"]),
            "jobs": len(jobs),
            "open_holds": int(open_holds),
            "canary_leaks": int(leaked),
        }
        step(
            "invariants: ledger, holds, job states, no canary retention",
            results["invariants"],
        )

        results["ok"] = True
        return results
    except StepFailed as exc:
        results["ok"] = False
        results["failure"] = str(exc)
        print(f"FAIL {exc}")
        return results
    finally:
        if worker is not None:
            try:
                worker.terminate()
                await asyncio.wait_for(worker.wait(), timeout=10)
            except (ProcessLookupError, TimeoutError):  # pragma: no cover
                if worker.returncode is None:
                    worker.kill()
        client.close()
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL", "postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn"
        ),
    )
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--model",
        default=os.environ.get("TCDN_AI_JOURNEY_MODEL", MODEL),
        help="model id to register and serve (must exist in the engine for ollama/vllm)",
    )
    parser.add_argument(
        "--backend",
        choices=("echo", "ollama", "vllm"),
        default=os.environ.get("TCDN_AI_JOURNEY_BACKEND", "echo"),
        help="worker inference backend; echo is the labelled SIMULATED stub",
    )
    parser.add_argument(
        "--engine-url",
        default=os.environ.get("TCDN_AI_JOURNEY_ENGINE_URL", ""),
        help="loopback URL of the inference engine (ollama: :11434, vllm: :8000)",
    )
    parser.add_argument(
        "--engine-version",
        default=os.environ.get("TCDN_AI_JOURNEY_ENGINE_VERSION", "unknown"),
    )
    parser.add_argument("--gpu-vram-gb", type=int, default=8)
    parser.add_argument("--gpu-model", default="dev-stub-no-gpu")
    parser.add_argument(
        "--email-stub-url",
        default=os.environ.get("TCDN_FAKE_RESEND_URL", ""),
        help="base URL of scripts/dev/fake_resend.py; enables the real email flow",
    )
    parser.add_argument("--email-out", default=os.environ.get("TCDN_FAKE_EMAILS", ""))
    args = parser.parse_args()
    results = asyncio.run(run(args))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2))
    print("JOURNEY OK" if results.get("ok") else "JOURNEY FAILED")
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
