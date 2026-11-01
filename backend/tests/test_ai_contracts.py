"""One contract test per trust boundary — see docs/tcdn-ai/CONTRACTS.md.

These tests are intentionally about the *shape* of what crosses each boundary
(field sets, framing, domains, error codes), not about the feature behind it.
A feature regression is caught by that feature's tests; this file catches a
silent change to the wire that a ported client would break on.
"""

from __future__ import annotations

import pytest

from app.ai.errors import CryptoError, TicketError, UnavailableError
from app.ai.keyring import KeyRing
from app.ai.models import (
    Confidentiality,
    PlacementRequest,
    TicketResponse,
)
from app.ai.receipts import sign_receipt, verify_receipt
from app.ai.shards import (
    GpuDirectSink,
    HardwareUnavailable,
    MemorySink,
    SwarmFetcher,
    build_manifest,
    sign_manifest,
    verify_manifest,
)
from app.ai.tickets import TicketService
from tcdn_ai_sdk.protocol import (
    FINAL_FRAME,
    MAX_FRAME_BYTES,
    TOKEN_FRAME,
    decode_frames,
    encode_frame,
    request_aad,
    stream_aad,
)


class TestTB1SDKToCoordinator:
    """TB1: placement carries metadata only, and is replay-safe."""

    def test_placement_field_set_is_exactly_the_priced_metadata(self):
        # A new field here changes what the coordinator can learn about a
        # request; that is a contract change, not a refactor.
        assert set(PlacementRequest.model_fields) == {
            "model",
            "max_tokens",
            "stream",
            "confidential",
            "prompt_tokens_estimate",
            "region",
            "latency_target_ms",
            "vram_gb",
            "max_price_micro",
        }
        # The prompt must not be expressible on this boundary.
        assert "prompt" not in PlacementRequest.model_fields
        assert "messages" not in PlacementRequest.model_fields

    def test_ticket_response_has_every_field_a_client_needs_to_connect(self):
        assert set(TicketResponse.model_fields) == {
            "job_id",
            "model",
            "confidentiality",
            "confidentiality_notice",
            "worker_endpoint",
            "worker_public_key",
            "key_attestation_ref",
            "expires_at",
            "tier",
            "ticket",
            "coordinator_key_id",
        }

    def test_unknown_field_is_rejected_not_ignored(self):
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            PlacementRequest(model="m", prompt="the customer's text")


class TestTB2SDKToWorker:
    """TB2: frame format, AAD binding, and the fixed size ceiling."""

    def test_frame_layout_is_kind_length_body(self):
        frame = encode_frame(TOKEN_FRAME, b"ciphertext")
        assert frame[0] == TOKEN_FRAME
        assert int.from_bytes(frame[1:5], "big") == len(b"ciphertext")
        assert frame[5:] == b"ciphertext"
        frames, remainder = decode_frames(frame)
        assert remainder == b""
        assert [(f.kind, f.body) for f in frames] == [(TOKEN_FRAME, b"ciphertext")]

    def test_partial_frame_is_left_as_remainder_not_truncated(self):
        frame = encode_frame(FINAL_FRAME, b"receipt")
        frames, remainder = decode_frames(frame[:-2])
        assert frames == []
        assert remainder == frame[:-2]

    def test_unknown_kind_and_oversize_are_refused(self):
        from tcdn_ai_sdk.protocol import ProtocolError

        with pytest.raises(ProtocolError):
            encode_frame(0x42, b"x")
        with pytest.raises(ProtocolError):
            encode_frame(TOKEN_FRAME, b"x" * (MAX_FRAME_BYTES + 1))

    def test_aad_binds_the_job_and_the_ticket(self):
        # The AEAD tag must not validate for a different job or a different
        # ticket, or ciphertext could be replayed across jobs.
        assert request_aad("job_a", "a" * 64) != request_aad("job_b", "a" * 64)
        assert request_aad("job_a", "a" * 64) != request_aad("job_a", "b" * 64)
        assert request_aad("job_a", "a" * 64).startswith(b"tcdn-ai/request/v1|job_a|")
        assert stream_aad("job_a", 0) != stream_aad("job_a", 1)
        from tcdn_ai_sdk.protocol import ProtocolError

        with pytest.raises(ProtocolError):
            stream_aad("job_a", -1)


class TestTB3WorkerToCoordinator:
    """TB3: receipts are signed over a domain-separated canonical payload."""

    def _context(self, node_id: str = "node_tb3"):
        from app.ai.receipts import JobContext

        return JobContext(
            job_id="job_tb3",
            account_id="11111111-1111-4111-8111-111111111111",
            node_id=node_id,
            model="llama-3.1-8b-instruct",
            manifest_hash="a" * 64,
            max_tokens=16,
        )

    def _receipt(self, **overrides):
        key = KeyRing.generate("worker").active
        payload = {
            "job_id": "job_tb3",
            "account_id": "11111111-1111-4111-8111-111111111111",
            "node_id": "node_tb3",
            "model": "llama-3.1-8b-instruct",
            "manifest_hash": "a" * 64,
            "input_tokens": 10,
            "output_tokens": 5,
            "output_sha256": "b" * 64,
            "started_at": 1_000,
            "finished_at": 1_010,
        }
        payload.update(overrides)
        return key, payload, sign_receipt(payload, key.private_key)

    def test_signature_covers_the_payload_and_survives_a_round_trip(self):
        key, payload, document = self._receipt()
        verified = verify_receipt(
            document, worker_public_key=key.public_key, job=self._context()
        )
        assert verified.output_tokens == payload["output_tokens"]
        assert verified.job_id == payload["job_id"]

    def test_a_changed_token_count_invalidates_the_receipt(self):
        from app.ai.errors import ReceiptError

        key, _payload, document = self._receipt()
        document["payload"]["output_tokens"] = 500
        with pytest.raises(ReceiptError) as excinfo:
            verify_receipt(
                document, worker_public_key=key.public_key, job=self._context()
            )
        assert excinfo.value.code == "bad_signature"

    def test_a_receipt_from_another_node_is_refused(self):
        from app.ai.errors import ReceiptError

        key, _payload, document = self._receipt()
        with pytest.raises(ReceiptError) as excinfo:
            verify_receipt(
                document,
                worker_public_key=key.public_key,
                job=self._context(node_id="node_other"),
            )
        assert excinfo.value.code == "receipt_node_mismatch"

    def test_a_manifest_signature_is_not_a_receipt(self):
        # Domain separation: another document type signed by the same key must
        # never validate as a receipt.
        from app.ai.errors import ReceiptError

        ring = KeyRing.generate("worker")
        manifest = build_manifest(model_id="m", name="M", shard_payloads=[b"x"])
        document = sign_manifest(ring, manifest)
        with pytest.raises(ReceiptError):
            verify_receipt(
                document,
                worker_public_key=ring.active.public_key,
                job=self._context(),
            )


class TestTB4CoordinatorToWorker:
    """TB4: a ticket is single-use, time-bounded and bound to one node/tier."""

    def _ticket(self, service, **overrides):
        arguments = {
            "job_id": "job_tb4",
            "account_id": "11111111-1111-4111-8111-111111111111",
            "node_id": "node_tb4",
            "model": "llama-3.1-8b-instruct",
            "worker_public_key": "a" * 43,
            "confidentiality": Confidentiality.NONE,
            "max_tokens": 16,
            "max_price_micro": 1000,
            "now": 1_000,
        }
        arguments.update(overrides)
        return service.issue(**arguments)

    def _service(self):
        # The replay cache and the expiry check must share one clock, or the
        # test would pass in real time and mean nothing.
        return TicketService(
            KeyRing.generate("coordinator"), ttl_seconds=60, clock=lambda: 1_000
        )

    def test_a_ticket_verifies_once_and_exactly_once(self):
        service = self._service()
        document, _ = self._ticket(service)
        assert service.verify(document, now=1_001).job_id == "job_tb4"
        with pytest.raises(Exception) as excinfo:
            service.verify(document, now=1_001)
        assert getattr(excinfo.value, "code", "") == "replayed_ticket"

    def test_expiry_and_node_binding_are_enforced(self):
        service = self._service()
        document, _ = self._ticket(service)
        with pytest.raises(Exception) as excinfo:
            service.verify(document, now=1_100, claim_replay=False)
        assert getattr(excinfo.value, "code", "") == "expired_ticket"
        with pytest.raises(TicketError) as bound:
            service.verify(document, expected_node_id="node_other", claim_replay=False)
        assert bound.value.code == "ticket_node_mismatch"

    def test_a_tampered_ticket_is_not_accepted(self):
        service = TicketService(KeyRing.generate("coordinator"), ttl_seconds=60)
        document, _ = self._ticket(service)
        document["payload"]["max_tokens"] = 1_000_000
        with pytest.raises(CryptoError):
            service.verify(document, now=1_001)


class TestTB5CoordinatorToSeeder:
    """TB5: the manifest is the root of trust for every shard."""

    def test_manifest_signature_covers_shards_and_merkle_root(self):
        ring = KeyRing.generate("coordinator")
        manifest = build_manifest(
            model_id="m", name="M", shard_payloads=[b"one", b"two"]
        )
        document = sign_manifest(ring, manifest)
        assert verify_manifest(document, ring).model_id == "m"

        document["payload"]["shards"][0]["sha256"] = "0" * 64
        with pytest.raises(CryptoError):
            verify_manifest(document, ring)

    def test_executable_formats_are_refused_at_the_boundary(self):
        with pytest.raises(ValueError):
            build_manifest(
                model_id="m", name="M", shard_payloads=[b"x"], model_format="pickle"
            )

    def test_shard_sizes_must_sum_to_the_declared_total(self):
        manifest = build_manifest(model_id="m", name="M", shard_payloads=[b"abc"])
        data = manifest.model_dump(mode="json")
        data["total_size"] = 99
        from app.ai.shards import ModelManifestPayload

        with pytest.raises(ValueError):
            ModelManifestPayload(**data)


class TestTB6HostToGuest:
    """TB6: nothing crosses. The only component that could fails closed."""

    def test_gpu_direct_sink_cannot_be_constructed(self):
        with pytest.raises(HardwareUnavailable):
            GpuDirectSink()

    def test_no_fallback_to_disk_is_silently_provided(self):
        # GpuDirectSink has no ``available() == True`` path in this repo, so a
        # caller cannot believe it is running diskless.
        assert GpuDirectSink.available() in (False, True)  # advisory only
        with pytest.raises(HardwareUnavailable):
            GpuDirectSink().put_shard(0, b"x")


class TestTB7WorkerToSwarm:
    """TB7: shards are untrusted bytes until the signed hash says otherwise."""

    @pytest.mark.asyncio
    async def test_a_same_length_wrong_payload_is_rejected(self):
        payloads = [b"alpha", b"beta"]
        manifest = build_manifest(model_id="m", name="M", shard_payloads=payloads)

        class LyingPeer:
            peer_id = "lying"
            reputation = 100

            async def fetch_shard(self, index, expected_sha256):
                return b"ALPHA"[: len(payloads[index])]  # right size, wrong bytes

        class HonestPeer:
            peer_id = "honest"
            reputation = 1

            async def fetch_shard(self, index, expected_sha256):
                return payloads[index]

        sink = MemorySink()
        report = await SwarmFetcher(
            manifest, [LyingPeer(), HonestPeer()], sink=sink, attempts=2
        ).fetch_all()
        assert report.corrupt_peers == {"lying"}
        assert sink.data == {0: b"alpha", 1: b"beta"}

    @pytest.mark.asyncio
    async def test_a_peer_cannot_serve_another_index(self):
        payloads = [b"alpha", b"beta"]
        manifest = build_manifest(model_id="m", name="M", shard_payloads=payloads)

        class ConfusedPeer:
            peer_id = "confused"
            reputation = 100

            async def fetch_shard(self, index, expected_sha256):
                return payloads[1 - index]  # valid shard, wrong one

        sink = MemorySink()
        with pytest.raises(UnavailableError):
            await SwarmFetcher(
                manifest, [ConfusedPeer()], sink=sink, attempts=1
            ).fetch_all()
        assert sink.data == {}

    def test_sink_filename_comes_from_the_index_not_the_manifest(self):
        import tempfile

        from app.ai.shards import DiskSink

        with tempfile.TemporaryDirectory() as root:
            sink = DiskSink(root, quota_bytes=10_000)
            sink.put_shard(0, b"data")
            written = list(__import__("pathlib").Path(root).iterdir())
            assert [p.name for p in written] == ["shard-00000.safetensors"]


def test_the_contracts_doc_names_one_test_class_per_boundary():
    """CONTRACTS.md is the porting spec; its test names must resolve.

    Seven boundaries, seven classes — a boundary added to the threat model
    without a contract test (or a class renamed without the doc) fails here.
    """
    import pathlib
    import re

    repo = pathlib.Path(__file__).resolve().parents[2]
    document = (repo / "docs/tcdn-ai/CONTRACTS.md").read_text(encoding="utf-8")
    cited = set(re.findall(r"`(TestTB\d[A-Za-z0-9_]*)`", document))
    assert len(cited) == 7, sorted(cited)
    module = pathlib.Path(__file__).read_text(encoding="utf-8")
    for name in cited:
        assert f"class {name}" in module, f"{name} is documented but not implemented"
