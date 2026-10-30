"""Provision an inference-scoped AI API key for the authenticated /api/ai/v1 pass.

Runs **inside the backend container** (piped to ``python -`` by the DAST
workflow), where ``DATABASE_URL``, Redis and the API-key pepper are already in
the environment. The only thing it prints on stdout is the key; everything else
goes to stderr, and the workflow keeps it in a 0600 file under the runner's
temporary directory.

Why an API key at all: the session JWT pass cannot reach most of
``/api/ai/v1/*`` (``authenticate`` wants a ``tcdn_ai_`` key), so without this the
AI surface is only ever scanned as an unauthenticated caller.

It also **verifies the key it just minted** through ``Coordinator.authenticate``
— the exact function the route dependency calls. If that fails, the script exits
non-zero with the error code instead of handing the workflow a key that will get
401s, which is how a scan ends up "authenticated" in name only.
"""

from __future__ import annotations

import asyncio
import sys

EMAIL = "zap-scan@example.test"


async def main() -> int:
    from app.ai.config import get_ai_settings
    from app.ai.errors import AIError
    from app.ai.keyring import load_keyring
    from app.ai.service import Coordinator
    from app.ai.store import PostgresStore
    from app.database import get_db_pool, get_redis

    settings = get_ai_settings()
    pool = await get_db_pool()
    row = await pool.fetchrow(
        "SELECT id FROM users WHERE lower(email) = lower($1)", EMAIL
    )
    if row is None:
        print(
            f"no account {EMAIL} — the account seed step has to run first",
            file=sys.stderr,
        )
        return 1

    store = PostgresStore(pool, settings)
    await store.init()
    coordinator = Coordinator(
        store=store,
        keyring=load_keyring(settings),
        settings=settings,
        redis_client=await get_redis(),
    )
    key = await coordinator.provision_api_key(str(row["id"]), scopes=("inference",))

    try:
        auth = await coordinator.authenticate(key)
    except AIError as exc:
        print(
            f"the key was written but does not authenticate ({exc.code}: {exc.message}) "
            "— pepper or store mismatch between this process and the server",
            file=sys.stderr,
        )
        return 1
    print(
        f"key authenticates in-process: prefix {auth.key_prefix}, "
        f"scopes {sorted(auth.scopes)}",
        file=sys.stderr,
    )
    print(key)
    return 0


raise SystemExit(asyncio.run(main()))
