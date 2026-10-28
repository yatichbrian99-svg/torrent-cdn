#!/usr/bin/env python3
"""Provision a TCDN-AI API key and (optionally) seed the model registry.

The raw key is printed **once** and never stored in plaintext; only its
HMAC-SHA-256 hash (with the server pepper) goes to the database.

Model seeding requires real values: a model id, the SHA-256 of its signed
manifest, size, VRAM requirement and price class. There are no placeholder
models in this script — seeding invented hashes would make integrity checks
meaningless.

Usage:
    python scripts/ai_provision.py --account-id <uuid> [--scopes inference,worker]
    python scripts/ai_provision.py --account-id <uuid> --models-file models.json

models.json:
    [{"model_id": "...", "name": "...", "manifest_hash": "<64 hex>",
      "size_bytes": 16000000000, "required_vram_gb": 8,
      "license_note": "...", "price_class": "small",
      "format": "safetensors"}]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.config import get_ai_settings
from app.ai.keyring import load_keyring
from app.ai.pricing import LAUNCH_PRICES_MICRO
from app.ai.service import Coordinator
from app.ai.store import PostgresStore
from app.database import get_db_pool, get_redis

HASH_RE = re.compile(r"^[0-9a-f]{64}$")


#: Formats this script will register by default. ``safetensors`` only, because
#: P2P weight serving (``shards.py::ALLOWED_FORMATS``) can serve nothing else,
#: and pickle-based formats must never be registered at all. A staging box that
#: runs Ollama needs ``gguf`` in the registry to place jobs honestly (the model
#: really is a GGUF file); that caller passes a widened set explicitly rather
#: than this default quietly becoming permissive.
DEFAULT_FORMATS = frozenset({"safetensors"})


async def seed_models(
    store: PostgresStore,
    entries: list[dict],
    *,
    allowed_formats: frozenset[str] = DEFAULT_FORMATS,
) -> None:
    pool = store.pool
    for entry in entries:
        manifest_hash = entry["manifest_hash"]
        if not HASH_RE.match(manifest_hash):
            raise SystemExit(f"invalid manifest hash for {entry.get('model_id')!r}")
        price_class = entry.get("price_class", "small")
        if price_class not in LAUNCH_PRICES_MICRO:
            raise SystemExit(f"unknown price class {price_class!r}")
        model_format = entry.get("format", "safetensors")
        if model_format not in allowed_formats:
            raise SystemExit(
                f"format {model_format!r} is not allowed here (allowed: "
                f"{sorted(allowed_formats)})"
            )
        input_rate, output_rate = LAUNCH_PRICES_MICRO[price_class]
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO ai_models (
                    model_id, name, format, manifest_hash, size_bytes,
                    required_vram_gb, license_note, allowlisted, price_class
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,TRUE,$8)
                ON CONFLICT (model_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    format = EXCLUDED.format,
                    manifest_hash = EXCLUDED.manifest_hash,
                    size_bytes = EXCLUDED.size_bytes,
                    required_vram_gb = EXCLUDED.required_vram_gb,
                    license_note = EXCLUDED.license_note,
                    price_class = EXCLUDED.price_class
                """,
                entry["model_id"],
                entry["name"],
                model_format,
                manifest_hash,
                int(entry["size_bytes"]),
                int(entry["required_vram_gb"]),
                entry.get("license_note", ""),
                price_class,
            )
            await conn.execute(
                """
                INSERT INTO ai_model_prices (
                    model_id, input_micro_per_mtok, output_micro_per_mtok,
                    effective_from, launch_pricing
                ) VALUES ($1,$2,$3,0,TRUE)
                ON CONFLICT (model_id, effective_from) DO NOTHING
                """,
                entry["model_id"],
                input_rate,
                output_rate,
            )
        print(f"registered model {entry['model_id']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, help="existing users.id (UUID)")
    parser.add_argument("--scopes", default="inference")
    parser.add_argument("--models-file", help="JSON array of model records to seed")
    args = parser.parse_args()

    scopes = tuple(scope.strip() for scope in args.scopes.split(",") if scope.strip())
    settings = get_ai_settings()
    pool = await get_db_pool()
    store = PostgresStore(pool, settings)
    await store.init()
    redis_client = await get_redis()
    coordinator = Coordinator(
        store=store,
        keyring=load_keyring(settings),
        settings=settings,
        redis_client=redis_client,
    )

    raw = await coordinator.provision_api_key(args.account_id, scopes=scopes)
    await store.audit(
        "provision.api_key",
        actor="provision-script",
        subject=args.account_id,
        detail={"scopes": list(scopes)},
    )
    print("\nAPI key (shown once, copy it now):")
    print(raw)
    print()

    if args.models_file:
        entries = json.loads(Path(args.models_file).read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise SystemExit("models file must contain a JSON array")
        await seed_models(store, entries)

    try:
        from app.database import close_db

        await close_db()
    except Exception as exc:  # best-effort pool close
        print(f"warning: closing pool failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
