"""Component tests: scheduler, receipts, P2P shards, attestation, burn, SSRF."""

from __future__ import annotations

import hashlib
import socket
import time

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from app.ai import crypto
from app.ai.attestation import (
    AttestationService,
    NvidiaGpuVerifier,
    SevSnpVerifier,
    SimulatedVerifier,
    TdxVerifier,
    TrustPolicy,
)
from app.ai.burn import DryRunBurnStrategy, NullBurnStrategy, get_burn_strategy
from app.ai.config import AISettings
from app.ai.errors import (
    AttestationError,
    CryptoError,
    NoEligibleWorker,
    ReceiptError,
    UnavailableError,
    ValidationError,
)
from app.ai.keyring import KeyRing
from app.ai.models import (
    AckPayload,
    AttestationEvidencePayload,
    AttestationPlatform,
    Confidentiality,
    NodeType,
    PlacementRequest,
    ReceiptPayload,
)
from app.ai.receipts import JobContext, reconcile, sign_receipt, verify_receipt
from app.ai.scheduler import Requirements, WorkerCandidate, select_worker
from app.ai.shards import (
    DiskSink,
    GpuDirectSink,
    HardwareUnavailable,
    MemorySink,
    ModelManifestPayload,
    SwarmFetcher,
    build_manifest,
    merkle_proof,
    merkle_root,
    sign_manifest,
    verify_manifest,
    verify_merkle_proof,
)
from app.ai.ssrf import validate_url

# ── scheduler ───────────────────────────────────────────────────────────────


def _candidate(**overrides) -> WorkerCandidate:
    base = {
        "node_id": "node_a",
        "owner_id": "owner",
        "node_type": NodeType.COMPUTE,
        "models": frozenset({"llama-3.1-8b-instruct"}),
        "vram_gb": 24,
        "region": "eu-west",
        "price_micro_per_mtok": 1_000_000,
        "reputation": 80,
        "load": 0.1,
        "latency_ms_p95": 100,
        "healthy": True,
        "confidentiality": Confidentiality.NONE,
    }
    base.update(overrides)
    return WorkerCandidate(**base)


class TestScheduler:
    def test_eligible_worker_selected(self):
        selection = select_worker(
            [_candidate()], Requirements(model="llama-3.1-8b-instruct"), now=1_000
        )
        assert selection.worker.node_id == "node_a"

    def test_hard_filters(self):
        with pytest.raises(NoEligibleWorker):
            select_worker(
                [_candidate(vram_gb=4)],
                Requirements(model="llama-3.1-8b-instruct", vram_gb=8),
                now=1_000,
            )
        with pytest.raises(NoEligibleWorker):
            select_worker([_candidate(region="us")], Requirements(model="llama-3.1-8b-instruct", region="eu-west"), now=1_000)
        with pytest.raises(NoEligibleWorker):
            select_worker([_candidate(price_micro_per_mtok=9_000_000)],
                          Requirements(model="llama-3.1-8b-instruct", max_price_micro=1), now=1_000)
        with pytest.raises(NoEligibleWorker):
            select_worker([_candidate(suspended=True)], Requirements(model="llama-3.1-8b-instruct"), now=1_000)

    def test_confidential_never_downgrades(self):
        with pytest.raises(NoEligibleWorker) as excinfo:
            select_worker(
                [_candidate(confidentiality=Confidentiality.NONE)],
                Requirements(model="llama-3.1-8b-instruct", confidential=True),
                now=1_000,
            )
        assert excinfo.value.code == "no_attested_worker"

    def test_confidential_requires_live_attestation(self):
        attested = _candidate(
            confidentiality=Confidentiality.ATTESTED_TEE,
            attestation_verdict="verified",
            attestation_expires_at=1_500,
        )
        selection = select_worker(
            [attested], Requirements(model="llama-3.1-8b-instruct", confidential=True), now=1_000
        )
        assert selection.worker.node_id == "node_a"
        expired = _candidate(
            node_id="node_b",
            confidentiality=Confidentiality.ATTESTED_TEE,
            attestation_verdict="verified",
            attestation_expires_at=500,
        )
        with pytest.raises(NoEligibleWorker):
            select_worker([expired], Requirements(model="llama-3.1-8b-instruct", confidential=True), now=1_000)

    def test_seeder_nodes_are_not_compute_workers(self):
        with pytest.raises(NoEligibleWorker):
            select_worker([_candidate(node_type=NodeType.SEEDER)],
                          Requirements(model="llama-3.1-8b-instruct"), now=1_000)


# ── receipts ────────────────────────────────────────────────────────────────


def _receipt_payload(**overrides) -> dict:
    payload = {
        "job_id": "job_1",
        "node_id": "node_1",
        "account_id": "acc_1",
        "model": "llama-3.1-8b-instruct",
        "manifest_hash": "a" * 64,
        "input_tokens": 10,
        "output_tokens": 20,
        "output_sha256": hashlib.sha256(b"hello").hexdigest(),
        "started_at": 1_000,
        "finished_at": 1_001,
    }
    payload.update(overrides)
    return payload


def _job() -> JobContext:
    return JobContext(
        job_id="job_1",
        account_id="acc_1",
        node_id="node_1",
        model="llama-3.1-8b-instruct",
        manifest_hash="a" * 64,
        max_tokens=64,
    )


class TestReceipts:
    def test_signed_receipt_verifies(self):
        keys = crypto.generate_ed25519()
        document = sign_receipt(_receipt_payload(), keys["private_key"])
        receipt = verify_receipt(document, worker_public_key=keys["public_key"], job=_job())
        assert receipt.output_tokens == 20

    def test_tampered_receipt_rejected(self):
        keys = crypto.generate_ed25519()
        document = sign_receipt(_receipt_payload(), keys["private_key"])
        document["payload"]["output_tokens"] = 999
        with pytest.raises(ReceiptError):
            verify_receipt(document, worker_public_key=keys["public_key"], job=_job())

    def test_flipped_signature_rejected(self):
        """Isolate the signature check: payload is valid, only the signature is bad."""
        keys = crypto.generate_ed25519()
        document = sign_receipt(_receipt_payload(), keys["private_key"])
        document["sig"] = "A" + document["sig"][1:]
        with pytest.raises(ReceiptError) as excinfo:
            verify_receipt(document, worker_public_key=keys["public_key"], job=_job())
        assert excinfo.value.code == "bad_signature"

    def test_unassigned_node_rejected(self):
        keys = crypto.generate_ed25519()
        other_job = JobContext(
            job_id="job_1", account_id="acc_1", node_id="node_2",
            model="llama-3.1-8b-instruct", manifest_hash="a" * 64, max_tokens=64,
        )
        document = sign_receipt(_receipt_payload(), keys["private_key"])
        with pytest.raises(ReceiptError) as excinfo:
            verify_receipt(document, worker_public_key=keys["public_key"], job=other_job)
        assert "node" in str(excinfo.value)

    def test_manifest_mismatch_rejected(self):
        keys = crypto.generate_ed25519()
        document = sign_receipt(_receipt_payload(manifest_hash="b" * 64), keys["private_key"])
        with pytest.raises(ReceiptError):
            verify_receipt(document, worker_public_key=keys["public_key"], job=_job())

    def test_token_overflow_rejected(self):
        keys = crypto.generate_ed25519()
        document = sign_receipt(_receipt_payload(output_tokens=65), keys["private_key"])
        with pytest.raises(ReceiptError):
            verify_receipt(document, worker_public_key=keys["public_key"], job=_job())

    def test_reconcile_agreement(self):
        receipt = ReceiptPayload(**_receipt_payload())
        ack = AckPayload(
            job_id="job_1", tokens_received=20,
            output_sha256=receipt.output_sha256, acked_at=1_002,
        )
        decision = reconcile(receipt, ack)
        assert decision.status == "agreed"
        assert decision.billable

    def test_reconcile_hash_mismatch_bills_only_verified(self):
        receipt = ReceiptPayload(**_receipt_payload())
        ack = AckPayload(
            job_id="job_1", tokens_received=5, output_sha256="b" * 64, acked_at=1_002
        )
        decision = reconcile(receipt, ack)
        assert decision.status == "disputed"
        assert decision.output_tokens == 5
        assert not decision.billable

    def test_reconcile_token_tolerance(self):
        receipt = ReceiptPayload(**_receipt_payload(output_tokens=1_000))
        within = AckPayload(
            job_id="job_1", tokens_received=995,
            output_sha256=receipt.output_sha256, acked_at=1_002,
        )
        assert reconcile(receipt, within, tolerance_pct=2).status == "agreed"
        beyond = AckPayload(
            job_id="job_1", tokens_received=900,
            output_sha256=receipt.output_sha256, acked_at=1_002,
        )
        assert reconcile(receipt, beyond, tolerance_pct=2).status == "disputed"

    def test_missing_ack_does_not_settle(self):
        receipt = ReceiptPayload(**_receipt_payload())
        assert reconcile(receipt, None).status == "awaiting_ack"
        assert reconcile(None, None).status == "awaiting_ack"


# ── shards ──────────────────────────────────────────────────────────────────


class TestShards:
    def test_manifest_merkle_and_signature(self):
        payloads = [b"shard-a", b"shard-b", b"shard-c"]
        manifest = build_manifest(
            model_id="m", name="M", shard_payloads=payloads, created_at=1
        )
        assert manifest.merkle_root == merkle_root([s.sha256 for s in manifest.shards])
        for index, shard in enumerate(manifest.shards):
            proof = merkle_proof([s.sha256 for s in manifest.shards], index)
            assert verify_merkle_proof(shard.sha256, proof, manifest.merkle_root)
            forged = [dict(step) for step in proof]
            forged[0]["hash"] = "0" * 64
            assert not verify_merkle_proof(shard.sha256, forged, manifest.merkle_root)

        ring = KeyRing.generate("coordinator")
        document = sign_manifest(ring, manifest)
        assert verify_manifest(document, ring).model_id == "m"
        document["payload"]["shards"][0]["sha256"] = "0" * 64
        with pytest.raises(CryptoError):
            verify_manifest(document, ring)

    def test_malformed_proof_is_rejected_not_raised(self):
        """Peer-supplied proofs must return False, never raise.

        Found by ``scripts/fuzz_ai.py``: a non-string ``hash``/``root``/``leaf``
        reached ``bytes.fromhex`` and escaped as an unhandled TypeError.
        """
        leaf = "a" * 64
        root = "b" * 64
        assert not verify_merkle_proof(leaf, [{"position": "left", "hash": 1}], root)
        assert not verify_merkle_proof(1, [], root)
        assert not verify_merkle_proof(leaf, [], 1)
        assert not verify_merkle_proof(leaf, {"position": "left"}, root)
        assert not verify_merkle_proof(leaf, ["not-a-step"], root)
        assert not verify_merkle_proof(leaf, [{"position": "sideways", "hash": "c" * 64}], root)
        assert not verify_merkle_proof(leaf, [{"hash": "c" * 64}], root)
        assert not verify_merkle_proof("short", [], root)

    def test_unsafe_model_format_rejected(self):
        with pytest.raises(ValueError):
            build_manifest(
                model_id="m", name="M", shard_payloads=[b"x"], model_format="pickle"
            )

    def test_manifest_rejects_non_contiguous_shards(self):
        payloads = [b"a", b"b"]
        manifest = build_manifest(model_id="m", name="M", shard_payloads=payloads)
        data = manifest.model_dump(mode="json")
        data["shards"][1]["index"] = 5
        with pytest.raises(ValueError):
            ModelManifestPayload(**data)

    @pytest.mark.asyncio
    async def test_corrupt_peer_banned_and_good_peer_used(self):
        payloads = [b"alpha", b"beta", b"gamma"]
        manifest = build_manifest(model_id="m", name="M", shard_payloads=payloads)

        class Peer:
            def __init__(self, peer_id, reputation, corrupt=False):
                self.peer_id = peer_id
                self.reputation = reputation
                self.corrupt = corrupt
                self.calls = 0

            async def fetch_shard(self, index, expected_sha256):
                self.calls += 1
                data = payloads[index]
                return b"corrupt" if self.corrupt else data

        malicious = Peer("evil", reputation=99, corrupt=True)
        honest = Peer("honest", reputation=10)
        sink = MemorySink()
        penalties: list[tuple[str, str]] = []
        fetcher = SwarmFetcher(
            manifest, [malicious, honest], sink=sink, penalty=lambda p, r: penalties.append((p, r))
        )
        report = await fetcher.fetch_all()
        assert report.bytes_written == sum(len(p) for p in payloads)
        assert "evil" in report.corrupt_peers
        assert sink.data[0] == b"alpha"
        assert penalties and penalties[0][0] == "evil"
        # One corrupt response is enough to ban the peer for the whole fetch; the
        # remaining shards never contact it again.
        assert malicious.calls == 1

    @pytest.mark.asyncio
    async def test_all_corrupt_raises_and_writes_nothing(self):
        payloads = [b"alpha"]
        manifest = build_manifest(model_id="m", name="M", shard_payloads=payloads)

        class Peer:
            peer_id = "evil"
            reputation = 1

            async def fetch_shard(self, index, expected_sha256):
                return b"corrupt"

        sink = MemorySink()
        with pytest.raises(UnavailableError):
            await SwarmFetcher(manifest, [Peer()], sink=sink, attempts=1).fetch_all()
        assert sink.data == {}

    @pytest.mark.asyncio
    async def test_resume_skips_verified_shards(self):
        payloads = [b"alpha", b"beta"]
        manifest = build_manifest(model_id="m", name="M", shard_payloads=payloads)
        sink = MemorySink()
        sink.put_shard(0, payloads[0])

        class Peer:
            peer_id = "honest"
            reputation = 1

            async def fetch_shard(self, index, expected_sha256):
                return payloads[index]

        report = await SwarmFetcher(manifest, [Peer()], sink=sink).fetch_all()
        assert report.shards_resumed == 1
        assert report.shards_written == 1

    def test_disk_sink_rejects_path_traversal(self, tmp_path):
        sink = DiskSink(tmp_path, quota_bytes=1024)
        with pytest.raises(ValidationError):
            sink.put_shard(-1, b"x")

    def test_disk_sink_enforces_quota(self, tmp_path):
        sink = DiskSink(tmp_path, quota_bytes=10)
        with pytest.raises(UnavailableError):
            sink.put_shard(0, b"x" * 20)

    def test_gpu_sink_fails_closed(self):
        with pytest.raises(HardwareUnavailable):
            GpuDirectSink()


# ── attestation ─────────────────────────────────────────────────────────────


def _dev_settings(**overrides) -> AISettings:
    defaults = {
        "environment": "development",
        "api_key_pepper": "test-pepper",
        "allow_simulated_attestation": True,
    }
    defaults.update(overrides)
    return AISettings(**defaults)


def _evidence(service: AttestationService, node_id="node_1", **overrides):
    nonce = service.issue_nonce(node_id)
    payload = {
        "node_id": node_id,
        "platform": AttestationPlatform.SEV_SNP,
        "nonce": nonce,
        "session_key_sha256": hashlib.sha256(b"session-key").hexdigest(),
        "report_b64": crypto.b64e(b"evidence-report-bytes"),
        "cert_chain_pem": [],
        "issued_at": int(time.time()),
    }
    payload.update(overrides)
    return AttestationEvidencePayload(**payload)


class TestAttestation:
    def test_simulated_verdict_is_labelled(self):
        ring = KeyRing.generate("coordinator")
        service = AttestationService(keyring=ring, settings=_dev_settings())
        verdict, document = service.attest(_evidence(service))
        assert verdict.status == "simulated"
        assert verdict.verifier == "simulated"
        assert "simulated" in (verdict.reason or "")
        assert ring.verify(document, "tcdn-ai/attestation-verdict/v1")["status"] == "simulated"

    def test_nonce_cannot_be_reused(self):
        ring = KeyRing.generate("coordinator")
        service = AttestationService(keyring=ring, settings=_dev_settings())
        evidence = _evidence(service)
        service.attest(evidence)
        with pytest.raises(AttestationError):
            service.attest(evidence)

    def test_nonce_is_bound_to_node(self):
        ring = KeyRing.generate("coordinator")
        service = AttestationService(keyring=ring, settings=_dev_settings())
        evidence = _evidence(service)
        with pytest.raises(AttestationError):
            service.attest(evidence.model_copy(update={"node_id": "node_2"}))

    def test_session_key_binding_enforced(self):
        from app.ai.models import AttestationVerdict

        ring = KeyRing.generate("coordinator")
        service = AttestationService(keyring=ring, settings=_dev_settings())

        class MismatchedVerifier:
            name = "mismatched"

            def verify(self, evidence, *, now):
                return AttestationVerdict(
                    node_id=evidence.node_id,
                    platform=evidence.platform,
                    status="verified",
                    confidentiality=Confidentiality.ATTESTED_TEE,
                    nonce=evidence.nonce,
                    session_key_sha256="0" * 64,  # not the submitted key
                    verifier=self.name,
                    issued_at=now,
                    expires_at=now + 60,
                )

        service.verifiers[AttestationPlatform.SEV_SNP] = MismatchedVerifier()
        with pytest.raises(AttestationError) as excinfo:
            service.attest(_evidence(service))
        assert excinfo.value.code == "key_binding_mismatch"

    def test_simulated_verdict_rejected_in_production(self):
        dev = _dev_settings()
        ring = KeyRing.generate("coordinator")
        prod = AISettings(
            environment="production",
            require_tls=True,
            allow_simulated_attestation=False,
            allow_dev_stub_backend=False,
            payment_provider="paystack",
            api_key_pepper="prod-pepper",
            coordinator_key_file="/run/secrets/tcdn-ai-keys.json",
        )
        service = AttestationService(keyring=ring, settings=prod)
        dev_service = AttestationService(keyring=ring, settings=dev)
        verdict = SimulatedVerifier(allow=True).verify(
            _evidence(dev_service), now=int(time.time())
        )
        with pytest.raises(AttestationError) as excinfo:
            service.accept(verdict)
        assert "production" in str(excinfo.value)

    def test_real_verifiers_fail_closed(self):
        ring = KeyRing.generate("coordinator")
        settings = _dev_settings(allow_simulated_attestation=False)
        service = AttestationService(keyring=ring, settings=settings)
        evidence = _evidence(service)
        cases = [
            (SevSnpVerifier(), AttestationPlatform.SEV_SNP),
            (TdxVerifier(), AttestationPlatform.TDX),
            (NvidiaGpuVerifier(), AttestationPlatform.NVIDIA_GPU),
        ]
        for verifier, platform in cases:
            with pytest.raises(AttestationError) as excinfo:
                verifier.verify(
                    evidence.model_copy(update={"platform": platform}),
                    now=int(time.time()),
                )
            assert excinfo.value.code == "attestation_verifier_unavailable"
        assert not TrustPolicy().configured

    def test_platform_mismatch_rejected(self):
        verifier = NvidiaGpuVerifier(TrustPolicy(allowed_platforms=(AttestationPlatform.NVIDIA_GPU,),
                                                vendor_roots_pem=("root",)))
        evidence = _evidence(AttestationService(KeyRing.generate("c"), settings=_dev_settings()))
        with pytest.raises(AttestationError) as excinfo:
            verifier.verify(evidence, now=int(time.time()))
        assert excinfo.value.code == "platform_mismatch"


# ── burn ────────────────────────────────────────────────────────────────────


class TestBurn:
    def test_disabled_by_default(self):
        strategy = get_burn_strategy(_dev_settings())
        plan = strategy.plan(platform_revenue_micro=10_000)
        assert isinstance(strategy, NullBurnStrategy)
        assert plan.enabled is False
        assert plan.burn_micro == 0
        assert plan.executed is False

    def test_dry_run_computes_but_never_executes(self):
        strategy = DryRunBurnStrategy(share_pct=10)
        plan = strategy.plan(platform_revenue_micro=10_000)
        assert plan.burn_micro == 1_000
        assert plan.executed is False
        assert "DRY RUN" in plan.note

    def test_enabled_in_dev_returns_dry_run(self):
        strategy = get_burn_strategy(_dev_settings(enable_tcdn_burn=True), share_pct=5)
        assert isinstance(strategy, DryRunBurnStrategy)

    def test_production_requires_legal_ack(self):
        with pytest.raises(ValueError):
            AISettings(
                environment="production",
                require_tls=True,
                allow_simulated_attestation=False,
                allow_dev_stub_backend=False,
                payment_provider="paystack",
                api_key_pepper="p",
                coordinator_key_file="/run/secrets/keys.json",
                enable_tcdn_burn=True,
                burn_legal_review_ack=False,
            )


# ── SSRF + request validation ───────────────────────────────────────────────


class TestSSRFAndValidation:
    def test_empty_allowlist_blocks_everything(self):
        with pytest.raises(ValidationError):
            validate_url("https://example.com/x", allowed_hosts=())

    def test_non_https_blocked(self):
        with pytest.raises(ValidationError):
            validate_url("http://example.com/x", allowed_hosts=("example.com",))

    def test_loopback_and_metadata_blocked(self):
        for url in (
            "https://127.0.0.1/x",
            "https://localhost/x",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/x",
        ):
            with pytest.raises(ValidationError):
                validate_url(url, allowed_hosts=("127.0.0.1", "localhost",
                                                "169.254.169.254", "::1"))

    def test_unlisted_host_blocked(self):
        with pytest.raises(ValidationError):
            validate_url("https://evil.example/x", allowed_hosts=("good.example",))

    def test_model_name_injection_rejected(self):
        for name in (
            "llama; DROP TABLE users",
            "../../etc/passwd",
            "model name with spaces",
            "x" * 200,
            "",
        ):
            with pytest.raises(PydanticValidationError):
                PlacementRequest(model=name)

    def test_prompt_content_is_not_part_of_placement(self):
        assert "messages" not in PlacementRequest.model_fields
        assert "prompt" not in PlacementRequest.model_fields

    def test_negative_prices_rejected(self):
        with pytest.raises(PydanticValidationError):
            PlacementRequest(model="m", max_price_micro=-5)

    def test_production_refuses_insecure_defaults(self):
        with pytest.raises(ValueError):
            AISettings(environment="production")


# ── HTTPS origin fallback (row 7.1) ────────────────────────────────────────


class TestOriginFallback:
    """The origin is the last resort, and the least trusted input in the fetch.

    A shard URL is derived from a manifest, so a hostile manifest must not be
    able to steer the worker at an internal host, a plain-HTTP mirror, or a
    redirect to somewhere else.
    """

    @pytest.fixture(autouse=True)
    def _public_dns(self, monkeypatch):
        # validate_url resolves once and requires every address to be public;
        # this test must not depend on the network or on a real DNS name.
        monkeypatch.setattr(
            "app.ai.ssrf.socket.getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        )

    def _origin(self, manifest, payloads, *, client=None, **kwargs):
        from app.ai.shards import HttpsOriginPeer

        def handler(request: httpx.Request) -> httpx.Response:
            index = int(request.url.path.rsplit("shard-", 1)[1].split(".")[0])
            return httpx.Response(200, content=payloads[index], request=request)

        return HttpsOriginPeer(
            base_url="https://models.tcdn.store/weights",
            manifest=manifest,
            allowed_hosts=("models.tcdn.store",),
            client=client
            or httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_origin_is_used_when_every_peer_is_down(self):
        payloads = [b"alpha", b"beta", b"gamma"]
        manifest = build_manifest(model_id="m", name="M", shard_payloads=payloads)

        class DeadPeer:
            peer_id = "dead"
            reputation = 10

            async def fetch_shard(self, index, expected_sha256):
                raise OSError("peer offline")

        sink = MemorySink()
        origin = self._origin(manifest, payloads)
        try:
            report = await SwarmFetcher(
                manifest, [DeadPeer()], sink=sink, attempts=1, origin=origin
            ).fetch_all()
        finally:
            await origin._client.aclose()

        assert report.origin_shards == len(payloads)
        assert report.failed_shards == []
        assert sink.data == {index: data for index, data in enumerate(payloads)}

    def test_origin_must_be_https(self):
        from app.ai.shards import HttpsOriginPeer

        manifest = build_manifest(model_id="m", name="M", shard_payloads=[b"x"])
        with pytest.raises(ValidationError) as excinfo:
            HttpsOriginPeer(
                base_url="http://models.tcdn.store/weights",
                manifest=manifest,
                allowed_hosts=("models.tcdn.store",),
            )
        assert excinfo.value.code == "ssrf_blocked"

    def test_origin_host_must_be_allowlisted(self):
        from app.ai.shards import HttpsOriginPeer

        manifest = build_manifest(model_id="m", name="M", shard_payloads=[b"x"])
        with pytest.raises(ValidationError) as excinfo:
            HttpsOriginPeer(
                base_url="https://models.tcdn.store/weights",
                manifest=manifest,
                allowed_hosts=("other.example",),
            )
        assert excinfo.value.code == "ssrf_blocked"

    @pytest.mark.asyncio
    async def test_oversized_origin_response_is_refused(self):
        payloads = [b"a" * 10]
        manifest = build_manifest(model_id="m", name="M", shard_payloads=payloads)
        origin = self._origin(manifest, [b"a" * 11])  # one byte too many
        try:
            with pytest.raises(ValidationError) as excinfo:
                await origin.fetch_shard(0, manifest.shards[0].sha256)
        finally:
            await origin._client.aclose()
        assert excinfo.value.code == "origin_oversize"

    @pytest.mark.asyncio
    async def test_redirect_to_another_host_is_refused(self):
        from app.ai.shards import HttpsOriginPeer

        payloads = [b"a" * 10]
        manifest = build_manifest(model_id="m", name="M", shard_payloads=payloads)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "models.tcdn.store":
                return httpx.Response(
                    302,
                    headers={"Location": "https://evil.example/shard-00000.safetensors"},
                    request=request,
                )
            # The redirect target answers happily; the point is that we never
            # accept a payload delivered from a host outside the allowlist.
            return httpx.Response(200, content=payloads[0], request=request)

        # follow_redirects=True is the hostile case: the client walks the
        # redirect, so the peer's own host check has to catch it.
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        )
        origin = HttpsOriginPeer(
            base_url="https://models.tcdn.store/weights",
            manifest=manifest,
            allowed_hosts=("models.tcdn.store",),
            client=client,
        )
        try:
            with pytest.raises(ValidationError) as excinfo:
                await origin.fetch_shard(0, manifest.shards[0].sha256)
        finally:
            await client.aclose()
        assert excinfo.value.code == "ssrf_blocked"


class TestPeerPayloadHardening:
    """Row 5.6 for P2P-sourced bytes: short, long and misaddressed payloads."""

    @pytest.mark.asyncio
    async def test_truncated_shard_is_rejected_even_with_the_right_prefix(self):
        payloads = [b"alpha", b"beta"]
        manifest = build_manifest(model_id="m", name="M", shard_payloads=payloads)

        class TruncatingPeer:
            peer_id = "truncating"
            reputation = 90

            async def fetch_shard(self, index, expected_sha256):
                return payloads[index][:-1]  # right prefix, one byte short

        class GoodPeer:
            peer_id = "good"
            reputation = 50

            async def fetch_shard(self, index, expected_sha256):
                return payloads[index]

        sink = MemorySink()
        report = await SwarmFetcher(
            manifest,
            [TruncatingPeer(), GoodPeer()],
            sink=sink,
            attempts=2,
            penalty=lambda peer_id, reason: None,
        ).fetch_all()
        assert report.corrupt_peers == {"truncating"}
        assert sink.data == {0: b"alpha", 1: b"beta"}

    def test_origin_url_cannot_carry_path_traversal(self):
        from app.ai.shards import HttpsOriginPeer

        # The manifest schema allows '/' in a model id, so the origin URL must
        # encode it rather than become a path the server (or a proxy) resolves.
        payloads = [b"x"]
        manifest = build_manifest(
            model_id="m/../../etc", name="M", shard_payloads=payloads
        )
        import socket as _socket

        from app.ai import ssrf

        original = ssrf.socket.getaddrinfo
        ssrf.socket.getaddrinfo = lambda *a, **k: [
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        try:
            origin = HttpsOriginPeer(
                base_url="https://models.tcdn.store/w",
                manifest=manifest,
                allowed_hosts=("models.tcdn.store",),
            )
        finally:
            ssrf.socket.getaddrinfo = original
        url = origin._url(0)
        assert "%2F" in url
        assert "/../" not in url
        assert url.startswith("https://models.tcdn.store/w/")
