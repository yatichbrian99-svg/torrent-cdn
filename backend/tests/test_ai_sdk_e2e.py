"""End-to-end: SDK ↔ coordinator HTTP ↔ worker agent, real crypto end to end."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from app.ai import crypto
from app.ai import routes as ai_routes
from app.ai.config import AISettings
from app.ai.keyring import KeyRing
from app.ai.models import (
    AttestationEvidencePayload,
    AttestationPlatform,
    Confidentiality,
    NodeType,
    SignedEnvelope,
    WorkerRegistrationPayload,
)
from app.ai.service import ATTESTATION_EVIDENCE_DOMAIN, Coordinator
from app.ai.store import MemoryStore, ModelRecord
from app.ai.worker import EchoBackend, WorkerAgent, WorkerConfig, WorkerIdentity
from app.main import app
from tcdn_ai_sdk import Client
from tcdn_ai_sdk.errors import TicketVerificationError
from tcdn_ai_sdk.protocol import FINAL_FRAME, decode_frames

BASE_URL = "http://127.0.0.1:8000"


def _ai_surface_enabled() -> bool:
    """The coordinator routes are mounted by `main.py` only behind the flag."""
    from app.ai.config import get_ai_settings
    from app.config import get_settings

    try:
        return get_ai_settings().is_ai_enabled_for_host(get_settings().app_env)
    except Exception:  # no AI config at all → the app cannot mount the routes
        return False


pytestmark = pytest.mark.skipif(
    not _ai_surface_enabled(),
    reason="ENABLE_TCDN_AI is off: /api/ai/v1 is not mounted",
)
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"


def build_world(*, confidential: bool = False) -> SimpleNamespace:
    """Build a coordinator + registered worker without any network or DB."""

    async def setup() -> SimpleNamespace:
        settings = AISettings(
            environment="development",
            api_key_pepper="e2e-pepper-not-a-real-secret",
            rate_limit_per_minute=100_000,
            rate_limit_burst=100_000,
        )
        keyring = KeyRing.generate("coordinator-e2e")
        store = MemoryStore(settings)
        await store.init()
        model = ModelRecord(
            model_id="llama-3.1-8b-instruct",
            name="Llama 3.1 8B Instruct",
            format="safetensors",
            manifest_hash="c" * 64,
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
        store.models[model.model_id] = model
        coordinator = Coordinator(store=store, keyring=keyring, settings=settings)
        api_key = await coordinator.provision_api_key(
            ACCOUNT_ID, scopes=("inference", "worker")
        )
        await store.credit(
            ACCOUNT_ID,
            1_000_000_000,
            source="gateway:test",
            idempotency_key="e2e-credits",
            kind="credit_purchase",
        )
        auth = await coordinator.authenticate(api_key)

        identity = WorkerIdentity.generate("pending")
        identity.node_id = (
            "node_"
            + hashlib.sha256(identity.identity_public_key.encode()).hexdigest()[:24]
        )
        registration = WorkerRegistrationPayload(
            identity_public_key=identity.identity_public_key,
            session_public_key=identity.session_public_key,
            node_type=NodeType.COMPUTE,
            models=[model.model_id],
            gpu_model="NVIDIA H100",
            vram_gb=80,
            region="eu-west",
            price_micro_per_mtok=1_000_000,
            confidentiality=(
                Confidentiality.ATTESTED_TEE if confidential else Confidentiality.NONE
            ),
            software_version="0.1.0",
            endpoint=BASE_URL,
        ).model_dump(mode="json")
        await coordinator.register_node(
            auth,
            SignedEnvelope(
                payload=registration,
                key_id=identity.identity_key_id,
                sig=crypto.sign(
                    "tcdn-ai/node-register/v1",
                    registration,
                    identity.identity_private_key,
                ),
            ),
        )

        if confidential:
            nonce = await coordinator.issue_attestation_nonce(identity.node_id)
            evidence = AttestationEvidencePayload(
                node_id=identity.node_id,
                platform=AttestationPlatform.SEV_SNP,
                nonce=nonce,
                session_key_sha256=crypto.sha256_hex(
                    crypto.b64d(identity.session_public_key)
                ),
                report_b64=crypto.b64e(b"e2e-simulated-report"),
                cert_chain_pem=[],
                issued_at=int(coordinator.clock()),
            ).model_dump(mode="json")
            await coordinator.attest(
                auth,
                SignedEnvelope(
                    payload=evidence,
                    key_id=identity.identity_key_id,
                    sig=crypto.sign(
                        ATTESTATION_EVIDENCE_DOMAIN,
                        evidence,
                        identity.identity_private_key,
                    ),
                ),
            )

        trust = KeyRing("coordinator-public")
        trust.add_public(keyring.active.public_key, key_id=keyring.active.key_id)
        agent = WorkerAgent(
            identity=identity,
            config=WorkerConfig(
                models={model.model_id: model.manifest_hash},
                confidentiality=(
                    Confidentiality.ATTESTED_TEE
                    if confidential
                    else Confidentiality.NONE
                ),
                attested=confidential,
            ),
            backend=EchoBackend(allow=True),
            coordinator_keys=trust,
            settings=settings,
        )
        return SimpleNamespace(
            settings=settings,
            keyring=keyring,
            store=store,
            coordinator=coordinator,
            api_key=api_key,
            auth=auth,
            model=model,
            agent=agent,
        )

    return asyncio.run(setup())


class SyncHTTP:
    """Synchronous stand-in for httpx.Client that talks to the ASGI app."""

    def __init__(self) -> None:
        self._client = TestClient(app)

    def request(self, method, url, headers=None, json=None):
        path = url.split(BASE_URL, 1)[-1] or "/"
        response = self._client.request(method, path, headers=headers, json=json)
        try:
            body = response.json()
        except ValueError:  # pragma: no cover - the AI API always returns JSON
            body = None
        return httpx.Response(
            response.status_code, json=body, request=httpx.Request(method, url)
        )

    def close(self) -> None:
        self._client.close()

    def stream(
        self, *args, **kwargs
    ):  # pragma: no cover - worker transport is injected
        raise AssertionError("worker calls go through worker_transport in tests")


def make_client(world, *, tamper=None):
    ai_routes.reset_for_tests()
    ai_routes.set_coordinator_for_tests(world.coordinator)
    receipts: list[dict] = []

    def worker_transport(url, payload):
        frames = asyncio.run(world.agent.process(payload))
        parsed, _ = decode_frames(frames)
        final = [frame for frame in parsed if frame.kind == FINAL_FRAME]
        if final:
            receipts.append(json.loads(final[-1].body))
        return frames

    class TamperingHTTP(SyncHTTP):
        def request(self, method, url, headers=None, json=None):
            response = super().request(method, url, headers=headers, json=json)
            if tamper is not None and "/placements" in url:
                body = response.json()
                tamper(body)
                return httpx.Response(
                    response.status_code,
                    json=body,
                    request=httpx.Request(method, url),
                )
            return response

    http = TamperingHTTP()
    client = Client(
        api_key=world.api_key,
        base_url=BASE_URL,
        coordinator_public_keys={
            world.keyring.active.key_id: world.keyring.active.public_key
        },
        worker_transport=worker_transport,
        http_client=http,
    )
    return client, receipts


def submit_receipt(world, receipt: dict):
    return asyncio.run(
        world.coordinator.submit_receipt(
            SignedEnvelope(
                payload=receipt["payload"],
                key_id=receipt["key_id"],
                alg=receipt["alg"],
                sig=receipt["sig"],
            )
        )
    )


class TestSDKEndToEnd:
    def teardown_method(self):
        ai_routes.set_coordinator_for_tests(None)
        ai_routes.reset_for_tests()

    def test_non_streaming_completion_is_openai_shaped(self):
        world = build_world()
        client, receipts = make_client(world)
        reply = client.chat.completions.create(
            model=world.model.model_id,
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=8,
        )
        assert reply.object == "chat.completion"
        assert reply.choices[0].message.role == "assistant"
        assert reply.choices[0].message.content.startswith(
            f"echo[{world.model.model_id}]"
        )
        assert reply.choices[0].finish_reason == "stop"
        assert (
            reply.usage.total_tokens
            == reply.usage.prompt_tokens + reply.usage.completion_tokens
        )
        assert reply.tcdn["confidentiality"] == "none"
        assert len(receipts) == 1

        settled = submit_receipt(world, receipts[0])
        assert settled.status.value == "settled"
        job = asyncio.run(world.store.get_job(reply.tcdn["job_id"]))
        assert job.status == "settled"
        assert job.total_micro == settled.total_micro

    def test_worker_receipt_commits_and_stores_the_transcript(self):
        world = build_world()
        client, receipts = make_client(world)
        reply = client.chat.completions.create(
            model=world.model.model_id,
            messages=[{"role": "user", "content": "commit this"}],
            max_tokens=8,
        )
        payload = receipts[0]["payload"]
        ext = payload["verification"]
        assert ext["transcript_source"] == "simulated"
        assert ext["token_count"] == payload["output_tokens"]
        assert len(ext["transcript_root"]) == 64
        assert ext["receipt_id"]
        # The receipt carries the commitment, never the per-token records
        # themselves (those stay encrypted on the worker until the window ends).
        assert "logprob" not in repr(payload)

        from app.ai.verification2 import build_transcript_root

        records = world.agent.transcript_for(reply.tcdn["job_id"])
        assert len(records) == ext["token_count"]
        assert build_transcript_root(records) == ext["transcript_root"]

        job_id = reply.tcdn["job_id"]
        blob, _ = world.agent.transcripts._items[job_id]
        assert b'"records"' not in blob
        assert b"token_id" not in blob

        settled = submit_receipt(world, receipts[0])
        assert settled.status.value == "settled"
        world.agent.wipe_transcript(job_id)
        with pytest.raises(KeyError):
            world.agent.transcript_for(job_id)

    def test_streaming_chunks_and_usage(self):
        world = build_world()
        client, receipts = make_client(world)
        stream = client.chat.completions.create(
            model=world.model.model_id,
            messages=[{"role": "user", "content": "stream this"}],
            max_tokens=8,
            stream=True,
        )
        chunks = list(stream)
        text = "".join(
            chunk.choices[0].delta.content or ""
            for chunk in chunks
            if chunk.choices and chunk.choices[0].delta
        )
        assert text.startswith(f"echo[{world.model.model_id}]")
        assert chunks[-1].choices[0].finish_reason == "stop"
        assert chunks[-1].usage is not None
        assert len(receipts) == 1
        submit_receipt(world, receipts[0])

    def test_coordinator_never_sees_the_prompt(self):
        world = build_world()
        client, _ = make_client(world)
        canary = "CANARY-E2E-b31c-prompt"
        reply = client.chat.completions.create(
            model=world.model.model_id,
            messages=[{"role": "user", "content": canary}],
            max_tokens=8,
        )
        job = asyncio.run(world.store.get_job(reply.tcdn["job_id"]))
        assert canary not in repr(job)
        assert canary not in repr(world.store.audit_log)
        assert not hasattr(job, "prompt")
        # The worker's output does contain the prompt (echo backend); only the
        # coordinator's state must not.
        assert canary in reply.choices[0].message.content

    def test_tampered_ticket_is_rejected_by_the_sdk(self):
        world = build_world()
        client, _ = make_client(
            world,
            tamper=lambda body: body["ticket"]["payload"].update({"max_tokens": 10**6}),
        )
        with pytest.raises(TicketVerificationError):
            client.chat.completions.create(
                model=world.model.model_id,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=8,
            )

    def test_confidential_completion_reports_attested_tier(self):
        world = build_world(confidential=True)
        client, receipts = make_client(world)
        reply = client.chat.completions.create(
            model=world.model.model_id,
            messages=[{"role": "user", "content": "secret"}],
            max_tokens=8,
            confidential=True,
        )
        assert reply.tcdn["confidentiality"] == "attested-tee"
        assert reply.tcdn["attestation_ref"]
        assert "attested" in reply.tcdn["confidentiality_notice"]
        assert len(receipts) == 1
        settled = submit_receipt(world, receipts[0])
        assert settled.status.value == "settled"

    def test_confidential_is_sdk_mode_only_and_the_coordinator_stays_blind(self):
        """The row's last clause: confidential needs SDK mode.

        Both halves in one place. In SDK mode the client encrypts to the worker,
        so a canary prompt reaches the answer while never appearing in the
        coordinator's job row or audit log — the confidential tier *is* the
        encrypted path. In gateway mode the coordinator holds the plaintext, so
        the same flag cannot be honoured and is refused with a pointer to the
        SDK; accepting it would silently downgrade the tier it was asked for.
        """
        from app.ai.errors import ValidationError
        from app.ai.gateway import parse_chat_request

        world = build_world(confidential=True)
        client, receipts = make_client(world)
        canary = "CANARY-CONF-2-3-prompt"
        reply = client.chat.completions.create(
            model=world.model.model_id,
            messages=[{"role": "user", "content": canary}],
            max_tokens=8,
            confidential=True,
        )
        assert reply.tcdn["confidentiality"] == "attested-tee"
        job = asyncio.run(world.store.get_job(reply.tcdn["job_id"]))
        assert job.tier == "confidential"
        assert canary not in repr(job)
        assert canary not in repr(world.store.audit_log)
        # The worker's echo proves the prompt really travelled.
        assert canary in reply.choices[0].message.content
        submit_receipt(world, receipts[0])

        # Gateway mode: the coordinator would hold that plaintext, so it refuses.
        with pytest.raises(ValidationError) as refused:
            parse_chat_request(
                {
                    "model": world.model.model_id,
                    "messages": [{"role": "user", "content": "secret"}],
                    "confidential": True,
                },
                world.settings,
            )
        assert "SDK" in str(refused.value)
        assert refused.value.code == "unsupported_parameter"

    def test_confidential_requires_pinned_coordinator_keys(self):
        world = build_world(confidential=True)
        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(world.coordinator)
        client = Client(
            api_key=world.api_key,
            base_url=BASE_URL,
            http_client=SyncHTTP(),
            worker_transport=lambda url, payload: asyncio.run(
                world.agent.process(payload)
            ),
        )
        from tcdn_ai_sdk.errors import AttestationRequiredError

        with pytest.raises(AttestationRequiredError):
            client.chat.completions.create(
                model=world.model.model_id,
                messages=[{"role": "user", "content": "secret"}],
                max_tokens=8,
                confidential=True,
            )

    def test_client_rejects_plaintext_remote_base_url(self):
        with pytest.raises(ValueError):
            Client(api_key="tcdn_ai_x", base_url="http://example.com")

    def test_sdk_tls_context_is_tls13_only(self):
        import ssl

        from tcdn_ai_sdk._crypto import tls_context

        context = tls_context()
        assert context.minimum_version == ssl.TLSVersion.TLSv1_3
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
