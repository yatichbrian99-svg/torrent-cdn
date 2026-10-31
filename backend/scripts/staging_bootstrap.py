#!/usr/bin/env python3
"""One command to make a staging TCDN-AI deployment usable.

Run this **inside the staging web service** (Render dashboard → Shell, or
``render ssh``). It then inherits ``DATABASE_URL``, the API-key pepper, the
coordinator keyring and the service URL from the running process, so the
database never has to be opened to the internet and no secret is copied by
hand. It also runs locally if you export those variables yourself.

What it does, in order:

1. pings ``/health`` and ``/api/ai/v1/status`` and refuses to continue if the AI
   surface is not mounted (a half-deployed staging box is worse than none);
2. registers a throwaway test account over HTTP (idempotent: an existing
   account is reused), so the signup path under test is the real one;
3. reads that account's id from ``users`` and reports whether the signup credit
   landed (with no email provider configured, registration auto-verifies and the
   grant fires — that is exactly what staging is for);
4. provisions an ``inference`` and a ``worker`` API key (printed once);
5. registers one model in the registry so a worker can be placed.

It does **not** start a worker, and it does not touch production: it only ever
talks to the URL and database it is given.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.config import get_ai_settings
from app.ai.keyring import load_keyring
from app.ai.service import Coordinator
from app.ai.store import PostgresStore
from app.database import get_db_pool, get_redis
from scripts.ai_provision import seed_models

DEFAULT_EMAIL = "staging-smoke@example.test"


async def _status(client: httpx.AsyncClient) -> dict:
    response = await client.get("/api/ai/v1/status")
    if response.status_code == 404:
        raise SystemExit(
            "404 from /api/ai/v1/status — ENABLE_TCDN_AI is off, or the AI env "
            "vars failed validation. Check the deploy log first."
        )
    response.raise_for_status()
    return response.json()


async def _register(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post(
        "/auth/register",
        json={"email": email, "password": password, "terms_accepted": True},
    )
    if response.status_code in (200, 201):
        return "created"
    detail = ""
    try:
        detail = str(response.json().get("detail", ""))
    except ValueError:
        detail = response.text[:200]
    if response.status_code in (400, 409) and "exist" in detail.lower():
        return "already exists"
    raise SystemExit(f"registration failed: HTTP {response.status_code} {detail}")


async def _user_row(pool, email: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, verified FROM users WHERE lower(email) = lower($1)", email
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("RENDER_EXTERNAL_URL", ""),
        help="staging service URL (default: RENDER_EXTERNAL_URL inside Render)",
    )
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument(
        "--password",
        default=os.environ.get("STAGING_SMOKE_PASSWORD", ""),
        help="test account password (default: generated and printed once)",
    )
    parser.add_argument("--model-id", default="qwen2.5:0.5b")
    parser.add_argument(
        "--manifest-hash",
        required=True,
        help="sha256 digest of the model manifest (from Ollama's /api/tags)",
    )
    parser.add_argument("--name", default="Qwen2.5 0.5B (staging)")
    parser.add_argument(
        "--format",
        default="gguf",
        choices=("gguf", "safetensors"),
        help="weights format of the model the worker serves (Ollama = gguf)",
    )
    parser.add_argument("--size-bytes", type=int, default=397 * 1024**2)
    parser.add_argument("--vram-gb", type=int, default=1)
    parser.add_argument("--price-class", default="small")
    args = parser.parse_args()

    base_url = args.base_url.strip().rstrip("/")
    if not base_url:
        raise SystemExit(
            "--base-url is required outside Render (RENDER_EXTERNAL_URL is unset)"
        )

    password = args.password or f"Stg-{secrets.token_urlsafe(12)}"
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as client:
        health = await client.get("/health")
        health.raise_for_status()
        status = await _status(client)
        workers = status.get("workers", {})
        print(f"health ok        {base_url}")
        print(f"commit           {status.get('commit')}")
        print(f"ai flag          {status.get('feature_flag', {}).get('on')}")
        print(f"database         {status.get('database', {}).get('status')}")
        print(f"redis            {status.get('redis', {}).get('status')}")
        print(f"workers          {workers.get('registered')} registered")
        if status.get("database", {}).get("status") != "ok":
            raise SystemExit("database is not answering; fix that before bootstrapping")
        print(f"account          {await _register(client, args.email, password)}")

    settings = get_ai_settings()
    pool = await get_db_pool()
    row = await _user_row(pool, args.email)
    if row is None:
        raise SystemExit(
            "registered account is not in the database — is this the same DB?"
        )
    account_id = str(row["id"])
    print(f"account id       {account_id}")
    print(f"email verified   {bool(row['verified'])}")

    store = PostgresStore(pool, settings)
    await store.init()
    coordinator = Coordinator(
        store=store,
        keyring=load_keyring(settings),
        settings=settings,
        redis_client=await get_redis(),
    )
    balance = await store.balance_micro(account_id)
    print(
        f"credits          {balance / 1_000_000:.2f} (signup grant {settings.signup_credits})"
    )
    if balance == 0:
        print(
            "  → no credit yet. The grant needs a verified address and the AI "
            "flag on; with no RESEND_API_KEY registration auto-verifies, so a "
            "zero here usually means the flag is off or the grant was capped."
        )

    await seed_models(
        store,
        [
            {
                "model_id": args.model_id,
                "name": args.name,
                "manifest_hash": args.manifest_hash,
                "size_bytes": args.size_bytes,
                "required_vram_gb": args.vram_gb,
                "license_note": "Apache-2.0 (Qwen2.5)",
                "price_class": args.price_class,
                "format": args.format,
            }
        ],
        # gguf is what Ollama actually serves; P2P shard distribution (7.x, P2)
        # only understands safetensors, and staging does not use it.
        allowed_formats=frozenset({"safetensors", "gguf"}),
    )

    inference_key = await coordinator.provision_api_key(
        account_id, scopes=("inference",)
    )
    worker_key = await coordinator.provision_api_key(account_id, scopes=("worker",))
    await store.audit(
        "provision.staging_bootstrap",
        actor="staging-bootstrap",
        subject=account_id,
        detail={"models": [args.model_id]},
    )

    print("\n" + "=" * 72)
    print("Shown once. Put these in your shell, not in the repo:")
    print(f"  export STAGING_BASE_URL={base_url}")
    print(f"  export TCDN_AI_API_KEY={inference_key}")
    print("=" * 72)
    print(f"worker key (WORKER VM only): TCDN_AI_WORKER_API_KEY={worker_key}")
    if not args.password:
        print(f"test account password: {password}")
    print("\nnext: python scripts/staging_smoke.py")
    print(
        "      python scripts/ai_worker.py --public-url https://<your-worker-host> ..."
    )


if __name__ == "__main__":
    asyncio.run(main())
