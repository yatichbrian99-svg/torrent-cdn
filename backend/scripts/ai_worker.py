#!/usr/bin/env python3
"""Run a TCDN-AI worker agent.

Required environment:

    TCDN_AI_COORDINATOR_URL        e.g. https://api.torrentcdn.example
    TCDN_AI_WORKER_API_KEY         API key with the `worker` scope
    TCDN_AI_WORKER_MODELS          "model_id=manifest_sha256,model2=sha..."
    TCDN_AI_BACKEND                ollama | vllm | echo
    TCDN_AI_IDENTITY_FILE          path to the 0600 worker identity file

Backend URLs (loopback only):
    TCDN_AI_VLLM_URL     default http://127.0.0.1:8000
    TCDN_AI_OLLAMA_URL   default http://127.0.0.1:11434

Verification receipt declarations (Part 3):
    TCDN_AI_ENGINE_VERSION     engine build, e.g. "vllm 0.6.3"
    TCDN_AI_WORKER_DTYPE       e.g. bfloat16 | float16 | float32
    TCDN_AI_WORKER_QUANTIZATION e.g. none | q4_k_m | gptq
    TCDN_AI_GPU_CLASS          hardware bucket, e.g. h100-80g

TLS: pass --tls-cert/--tls-key. Plaintext is refused unless
`--allow-plaintext-loopback` is passed in a non-production environment, because
prompts must never cross a network unencrypted.

`TCDN_AI_BACKEND=echo` is a development stub and is refused in production.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai import crypto
from app.ai.config import get_ai_settings
from app.ai.models import Confidentiality, NodeType
from app.ai.supervision import supervise
from app.ai.worker import (
    EchoBackend,
    OllamaBackend,
    VLLMBackend,
    WorkerAgent,
    WorkerConfig,
    WorkerIdentity,
    create_worker_app,
    load_worker_identity,
    save_worker_identity,
)

REGISTER_DOMAIN = "tcdn-ai/node-register/v1"
HEARTBEAT_DOMAIN = "tcdn-ai/heartbeat/v1"
DEFAULT_IDENTITY_FILE = ".tcdn-ai/worker-identity.json"


def _load_or_create_identity(path: Path) -> WorkerIdentity:
    if path.exists():
        return load_worker_identity(str(path))
    identity = WorkerIdentity.generate("pending")
    path.parent.mkdir(parents=True, exist_ok=True)
    save_worker_identity(str(path), identity)
    print(f"generated worker identity at {path} (mode 0600)")
    return identity


def _parse_models(raw: str) -> dict[str, str]:
    models: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        if "=" not in item:
            raise SystemExit(
                f"malformed model mapping: {item!r} (expected model=manifest_hash)"
            )
        model_id, digest = item.split("=", 1)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise SystemExit(f"invalid manifest hash for {model_id!r}")
        models[model_id.strip()] = digest.strip()
    if not models:
        raise SystemExit("TCDN_AI_WORKER_MODELS must list at least one model")
    return models


def _backend(name: str, settings):
    if name == "echo":
        return EchoBackend(allow=settings.allow_dev_stub_backend)
    if name == "vllm":
        return VLLMBackend(os.environ.get("TCDN_AI_VLLM_URL", "http://127.0.0.1:8000"))
    if name == "ollama":
        return OllamaBackend(
            os.environ.get("TCDN_AI_OLLAMA_URL", "http://127.0.0.1:11434")
        )
    raise SystemExit(f"unknown backend {name!r}")


async def _heartbeat_loop(
    identity: WorkerIdentity,
    agent: WorkerAgent,
    *,
    coordinator_url: str,
    api_key: str,
    interval_seconds: int = 30,
) -> None:
    """Keep this node's last_heartbeat fresh.

    A failed beat is reported and re-raised rather than swallowed: the caller
    runs this under ``supervise``, which restarts it with capped exponential
    backoff. Swallowing the error here meant a node whose beats never landed
    kept printing to stderr while the coordinator's liveness gate quietly
    dropped it from placement — the failure looked exactly like an idle node.
    """
    while True:
        try:
            payload = {
                "node_id": identity.node_id,
                "free_vram_gb": 0,
                "loaded_models": sorted(agent.config.models),
                "queue_depth": 0,
                "relay_reachable": True,
                "sent_at": int(time.time()),
            }
            envelope = {
                "payload": payload,
                "key_id": identity.identity_key_id,
                "alg": "Ed25519",
                "sig": crypto.sign(
                    HEARTBEAT_DOMAIN, payload, identity.identity_private_key
                ),
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{coordinator_url.rstrip('/')}/api/ai/v1/nodes/heartbeat",
                    json=envelope,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                response.raise_for_status()
        except Exception as exc:  # the supervisor's restart is the recovery
            print(f"heartbeat failed: {type(exc).__name__}", file=sys.stderr)
            raise
        await asyncio.sleep(interval_seconds)


async def _register(
    identity: WorkerIdentity,
    *,
    coordinator_url: str,
    api_key: str,
    models: dict[str, str],
    template: dict,
) -> str:
    payload = {
        "identity_public_key": identity.identity_public_key,
        "session_public_key": identity.session_public_key,
        "node_type": NodeType.COMPUTE.value,
        "models": sorted(models),
        "gpu_model": template.get("gpu_model", "unknown"),
        "vram_gb": int(template.get("vram_gb", 0)),
        "region": template.get("region", "unknown"),
        "price_micro_per_mtok": int(template.get("price_micro_per_mtok", 0)),
        "confidentiality": template.get("confidentiality", Confidentiality.NONE.value),
        "software_version": "0.1.0",
        "endpoint": template.get("endpoint", ""),
    }
    envelope = {
        "payload": payload,
        "key_id": identity.identity_key_id,
        "alg": "Ed25519",
        "sig": crypto.sign(REGISTER_DOMAIN, payload, identity.identity_private_key),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{coordinator_url.rstrip('/')}/api/ai/v1/nodes/register",
            json=envelope,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        body = response.json()
    identity.node_id = body["node_id"]
    return identity.node_id


async def main_async(
    args: argparse.Namespace, *, heartbeat_interval: int = 30
) -> WorkerAgent:
    ai_settings = get_ai_settings()
    identity_path = Path(os.environ.get("TCDN_AI_IDENTITY_FILE", DEFAULT_IDENTITY_FILE))
    identity = _load_or_create_identity(identity_path)
    models = _parse_models(os.environ["TCDN_AI_WORKER_MODELS"])
    backend_name = os.environ.get("TCDN_AI_BACKEND", "ollama")
    confidentiality = Confidentiality(
        os.environ.get("TCDN_AI_WORKER_CONFIDENTIALITY", Confidentiality.NONE.value)
    )
    template = {
        "gpu_model": os.environ.get("TCDN_AI_GPU_MODEL", "unknown"),
        "vram_gb": os.environ.get("TCDN_AI_GPU_VRAM_GB", "0"),
        "region": os.environ.get("TCDN_AI_REGION", "unknown"),
        "price_micro_per_mtok": os.environ.get("TCDN_AI_PRICE_MICRO_PER_MTOK", "0"),
        "confidentiality": confidentiality.value,
        "endpoint": args.public_url,
    }

    trust = KeyRingForCoordinator()
    await trust.load(os.environ["TCDN_AI_COORDINATOR_URL"])
    agent = WorkerAgent(
        identity=identity,
        config=WorkerConfig(
            models=models,
            confidentiality=confidentiality,
            attested=os.environ.get("TCDN_AI_WORKER_ATTESTED", "false").lower()
            == "true",
            # Declared environment committed into every verification receipt.
            # Undeclared values stay "unknown" and cannot match a tolerance
            # profile once calibration exists.
            engine_version=os.environ.get("TCDN_AI_ENGINE_VERSION", "unknown"),
            dtype=os.environ.get("TCDN_AI_WORKER_DTYPE", "unknown"),
            quantization=os.environ.get("TCDN_AI_WORKER_QUANTIZATION", "unknown"),
            gpu_class=os.environ.get(
                "TCDN_AI_GPU_CLASS", os.environ.get("TCDN_AI_GPU_MODEL", "unknown")
            ),
        ),
        backend=_backend(backend_name, ai_settings),
        coordinator_keys=trust.ring,
        settings=ai_settings,
    )
    node_id = await _register(
        identity,
        coordinator_url=os.environ["TCDN_AI_COORDINATOR_URL"],
        api_key=os.environ["TCDN_AI_WORKER_API_KEY"],
        models=models,
        template=template,
    )
    agent.enable_reporting(
        os.environ["TCDN_AI_COORDINATOR_URL"], os.environ["TCDN_AI_WORKER_API_KEY"]
    )
    print(f"registered as {node_id}")
    # Liveness: without heartbeats the coordinator cannot tell a running node
    # from one that died after registering, and keeps routing jobs to the dead
    # one. Every 30s, signed with the node identity key — supervised, so a crash
    # restarts it with capped backoff instead of ending the beats silently.
    heartbeat = asyncio.create_task(
        supervise(
            lambda: _heartbeat_loop(
                identity,
                agent,
                coordinator_url=os.environ["TCDN_AI_COORDINATOR_URL"],
                api_key=os.environ["TCDN_AI_WORKER_API_KEY"],
                interval_seconds=heartbeat_interval,
            ),
            on_crash=lambda exc, failures, delay: print(
                f"heartbeat crashed ({type(exc).__name__}); restart {failures} "
                f"in {delay:.0f}s",
                file=sys.stderr,
            ),
        ),
        name="heartbeat-supervisor",
    )
    # Keep a strong reference: an unreferenced task can be garbage-collected
    # mid-flight, which would stop the beats without any error at all.
    agent.background_tasks.append(heartbeat)
    return agent


class KeyRingForCoordinator:
    """Fetch and pin the coordinator's public keys before registering."""

    def __init__(self) -> None:
        from app.ai.keyring import KeyRing

        self.ring = KeyRing("coordinator-public")

    async def load(self, coordinator_url: str) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{coordinator_url.rstrip('/')}/api/ai/v1/keys/coordinator"
            )
            response.raise_for_status()
            document = response.json()["bundle"]
        payload = document["payload"]
        signing = next(
            key for key in payload["keys"] if key["key_id"] == document["key_id"]
        )
        if not crypto.verify(
            "tcdn-ai/keys/v1", payload, document["sig"], signing["public_key"]
        ):
            raise SystemExit("coordinator key bundle signature is invalid")
        for key in payload["keys"]:
            self.ring.add_public(key["public_key"], key_id=key["key_id"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Workers serve the SDK directly, so binding all interfaces is the intended
    # production default; TLS is required unless the listener is loopback
    # (enforced below). Bandit B104 is therefore accepted by design.
    parser.add_argument("--host", default="0.0.0.0")  # nosec B104
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--public-url", required=True, help="https URL the SDK reaches")
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    parser.add_argument("--allow-plaintext-loopback", action="store_true")
    parser.add_argument(
        "--heartbeat-interval",
        type=int,
        default=30,
        help="seconds between signed heartbeats (the coordinator's liveness gate "
        "is 120 s, so anything well under that works)",
    )
    args = parser.parse_args()

    settings = get_ai_settings()
    if not args.tls_cert or not args.tls_key:
        loopback = args.host in {"127.0.0.1", "localhost"}
        if not (
            args.allow_plaintext_loopback and loopback and not settings.is_production
        ):
            raise SystemExit(
                "TLS is required: pass --tls-cert/--tls-key (plaintext is only allowed "
                "for a loopback development listener with --allow-plaintext-loopback)"
            )

    import ssl

    import uvicorn

    def _tls13_context(config, default_context_factory):
        context = default_context_factory()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.options |= ssl.OP_NO_COMPRESSION
        return context

    # The TLS 1.3 context factory only makes sense when a certificate is
    # configured: uvicorn's default factory raises when ssl_certfile is None,
    # which used to crash the documented plaintext-loopback dev worker before
    # it could serve a single heartbeat.
    tls_kwargs = {}
    if args.tls_cert and args.tls_key:
        tls_kwargs = {
            "ssl_certfile": args.tls_cert,
            "ssl_keyfile": args.tls_key,
            "ssl_context_factory": _tls13_context,
        }

    async def _serve() -> None:
        """Register, then serve on the SAME event loop the heartbeat started on.

        The heartbeat task used to be created inside ``asyncio.run(main_async(...))``
        and the server then ran on a second, fresh loop. Closing the first loop
        cancelled the heartbeat task, so the node registered, beat once (or not
        at all) and then went silent while still serving jobs: the coordinator's
        liveness gate dropped it after 120 s and every placement failed with
        ``no_eligible_worker``. Found on the first staging run; pinned by
        ``tests/test_ai_worker_launcher.py``.
        """
        agent = await main_async(args, heartbeat_interval=args.heartbeat_interval)
        config = uvicorn.Config(
            create_worker_app(agent),
            host=args.host,
            port=args.port,
            log_level="info",
            **tls_kwargs,
        )
        await uvicorn.Server(config).serve()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
