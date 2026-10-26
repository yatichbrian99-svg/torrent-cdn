#!/usr/bin/env python3
"""Serve the OpenAI-compatible gateway on loopback with an in-process worker.

This exists so the **official** OpenAI clients can be pointed at a real HTTP
server instead of an ASGI transport. ``tests/test_ai_openai_compat.py`` drives
the official Python client over ASGI, which is fast but cannot be reached from
another language; row 2.1 asks for the official JavaScript client too, and an
npm client needs a socket. ``scripts/js_client_check.mjs`` is that client, and
``.github/workflows/ci-fullstack.yml`` starts this harness before running it.

Deliberately self-contained: the in-memory store and the echo worker mean the
check needs no Postgres, no Redis and no engine, and it cannot touch real data.
The echo backend's output is labelled (``transcript_source="simulated"``), so a
harness response can never be mistaken for evidence of real inference.

Writes one JSON line to ``--info`` once the app is ready, then serves:

    {"base_url": ".../v1", "api_key": "...", "model": "..."}

Usage::

    python scripts/gateway_harness.py --port 8099 --info /tmp/gateway.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

# The host app decides at import time whether to mount /api/ai/v1 and /v1.
# Set this before importing app.main, and keep the deployment in development:
# production would (correctly) refuse the echo backend and the memory store.
os.environ.setdefault("ENABLE_TCDN_AI", "true")
os.environ.setdefault("TCDN_AI_ENVIRONMENT", "development")
os.environ.setdefault("JWT_SECRET", "gateway-harness-dev-secret-32-chars!!")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai import crypto
from app.ai import routes as ai_routes
from app.ai import routes_openai as openai_routes
from app.ai.config import AISettings
from app.ai.keyring import KeyRing
from app.ai.models import (
    Confidentiality,
    NodeType,
    SignedEnvelope,
    WorkerRegistrationPayload,
)
from app.ai.service import Coordinator
from app.ai.store import MemoryStore, ModelRecord
from app.ai.worker import EchoBackend, WorkerAgent, WorkerConfig, WorkerIdentity
from tcdn_ai_sdk.protocol import FINAL_FRAME, decode_frames

ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"


def _model() -> ModelRecord:
    return ModelRecord(
        model_id="llama-3.1-8b-instruct",
        name="Llama 3.1 8B Instruct",
        format="safetensors",
        manifest_hash="a" * 64,
        size_bytes=16 * 1024**3,
        required_vram_gb=8,
        license_note="Llama 3.1 Community License",
        allowlisted=True,
        price_class="small",
        input_micro_per_mtok=3_000_000,
        output_micro_per_mtok=6_000_000,
        effective_from=0,
        history=[(0, 3_000_000, 6_000_000)],
    )


async def build() -> tuple[Coordinator, WorkerAgent, KeyRing, str]:
    settings = AISettings(
        environment="development",
        api_key_pepper="gateway-harness-pepper-not-a-secret",
        rate_limit_per_minute=10_000,
        rate_limit_burst=10_000,
    )
    keyring = KeyRing.generate("gateway-harness")
    store = MemoryStore(settings)
    await store.init()
    model = _model()
    store.models[model.model_id] = model

    coordinator = Coordinator(store=store, keyring=keyring, settings=settings)
    api_key = await coordinator.provision_api_key(
        ACCOUNT_ID, scopes=("inference", "worker")
    )
    await store.credit(
        ACCOUNT_ID,
        1_000_000_000,
        source="gateway:harness",
        idempotency_key="seed-credits",
        kind="credit_purchase",
    )
    auth = await coordinator.authenticate(api_key)

    identity = WorkerIdentity.generate("pending")
    identity.node_id = (
        "node_"
        + hashlib.sha256(identity.identity_public_key.encode("utf-8")).hexdigest()[:24]
    )
    payload = WorkerRegistrationPayload(
        identity_public_key=identity.identity_public_key,
        session_public_key=identity.session_public_key,
        node_type=NodeType.COMPUTE,
        models=[model.model_id],
        gpu_model="harness-cpu",
        vram_gb=24,
        region="localhost",
        price_micro_per_mtok=1_000_000,
        confidentiality=Confidentiality.NONE,
        software_version="harness",
        endpoint="http://127.0.0.1:9",
    ).model_dump(mode="json")
    await coordinator.register_node(
        auth,
        SignedEnvelope(
            payload=payload,
            key_id=identity.identity_key_id,
            sig=crypto.sign(
                "tcdn-ai/node-register/v1", payload, identity.identity_private_key
            ),
        ),
    )

    trust = KeyRing("coordinator-public")
    trust.add_public(keyring.active.public_key, key_id=keyring.active.key_id)
    agent = WorkerAgent(
        identity=identity,
        config=WorkerConfig(
            models={model.model_id: model.manifest_hash},
            confidentiality=Confidentiality.NONE,
            attested=False,
        ),
        backend=EchoBackend(allow=True),
        coordinator_keys=trust,
        settings=settings,
    )

    async def transport(url: str, body: dict):
        # Same signature the gateway uses for the HTTP hop to a worker; here the
        # worker runs in this process, so only the hop is replaced.
        frames = await agent.process(body)
        parsed, _ = decode_frames(frames)
        assert parsed and parsed[-1].kind == FINAL_FRAME, (
            "worker produced no final frame"
        )
        yield frames

    ai_routes.reset_for_tests()
    ai_routes.set_coordinator_for_tests(coordinator)
    openai_routes.set_worker_transport_for_tests(transport)
    # The plaintext key exists only here: the store keeps a hash. It goes into
    # the info file (root-readable, harness-only) and nowhere else.
    return coordinator, agent, keyring, api_key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--info",
        default="/tmp/gateway-harness.json",
        help="where to write the base URL, API key and model id",
    )
    args = parser.parse_args()

    _coordinator, _agent, _keyring, api_key = asyncio.run(build())

    # Import after the env is set (the flag is read at import) and after the
    # coordinator/transport overrides exist.
    import uvicorn

    from app.main import app

    info = {
        "base_url": f"http://{args.host}:{args.port}/v1",
        "api_key": api_key,
        "model": _model().model_id,
    }
    Path(args.info).write_text(json.dumps(info))
    print(
        json.dumps({"ready": True, **{k: v for k, v in info.items() if k != "api_key"}})
    )
    sys.stdout.flush()

    # lifespan="off": the host app's startup opens the CDN database pool and runs
    # the CDN DDL. This harness injects its own coordinator, so none of that is
    # needed — and skipping it keeps the check free of infrastructure.
    uvicorn.run(
        app, host=args.host, port=args.port, lifespan="off", log_level="warning"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
