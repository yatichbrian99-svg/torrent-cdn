"""Redundant execution wired into placement (checklist 6.1).

The sampler, the comparison and the penalty path existed before this file; what
was missing is orchestration: a sampled job must actually be *run twice*, on a
different operator, and the two receipts must be reconciled. These tests drive
the real gateway against real in-process worker agents, so the second run
travels the same encrypted protocol as the first.
"""

from __future__ import annotations

import json

import pytest

from app.ai import crypto
from app.ai.config import AISettings
from app.ai.errors import ConflictError
from app.ai.gateway import ChatRequest, Gateway
from app.ai.ledger import PLATFORM_REVENUE, compute_earnings, split_amounts
from app.ai.models import PlacementRequest, SignedEnvelope
from app.ai.pricing import MICRO_PER_MTOK, ceil_div
from app.ai.receipts import RECEIPT_DOMAIN
from app.ai.verification import VerificationSampler
from app.ai.worker import EchoBackend
from tcdn_ai_sdk.protocol import FINAL_FRAME, decode_frames


@pytest.mark.asyncio
def _request(*, max_tokens: int = 16) -> ChatRequest:
    return ChatRequest(
        model="llama-3.1-8b-instruct",
        messages=[{"role": "user", "content": "name three colours"}],
        max_tokens=max_tokens,
        temperature=0.0,
        top_p=1.0,
        stop=[],
        stream=False,
        include_usage=False,
    )


def _transport(agents: dict[str, object], receipts: list[dict]):
    """Route each outbound job to the agent named in its signed ticket."""

    async def transport(url: str, payload: dict):
        node_id = payload["ticket"]["payload"]["node_id"]
        frames = await agents[node_id].process(payload)  # type: ignore[attr-defined]
        parsed, _ = decode_frames(frames)
        for frame in parsed:
            if frame.kind == FINAL_FRAME:
                receipts.append(json.loads(frame.body.decode("utf-8")))
        yield frames

    return transport


def _envelope(document: dict) -> SignedEnvelope:
    return SignedEnvelope(
        payload=document["payload"],
        key_id=document["key_id"],
        alg=document["alg"],
        sig=document["sig"],
    )


async def _two_nodes(ai_env, ai_worker_factory) -> dict[str, object]:
    """Two workers, two endpoints, one model — enough for an alternate."""
    first = await ai_worker_factory()
    second = await ai_worker_factory()
    ai_env.store.nodes[first.identity.node_id].endpoint = "http://worker-a"
    ai_env.store.nodes[second.identity.node_id].endpoint = "http://worker-b"
    return {first.identity.node_id: first, second.identity.node_id: second}


class TestSampledJob:
    @pytest.mark.asyncio
    async def test_runs_twice_on_two_operators_and_reconciles(
        self, ai_env, ai_worker_factory
    ):
        ai_env.coordinator.verification = VerificationSampler(
            100, secret=b"unit-test-sampling-key"
        )
        agents = await _two_nodes(ai_env, ai_worker_factory)
        receipts: list[dict] = []
        gateway = Gateway(ai_env.coordinator, transport=_transport(agents, receipts))

        stream = await gateway.open(ai_env.auth, _request())
        await gateway.complete(ai_env.auth, stream)
        assert stream.verification_task is not None
        await stream.verification_task

        record = await ai_env.store.get_verification(stream.job_id)
        assert record is not None
        # Two executions, two operators, two distinct jobs.
        assert record.primary_node_id != record.verification_node_id
        assert record.verification_job_id != stream.job_id
        assert record.status == "awaiting_primary"
        shadow = receipts[-1]
        assert shadow["payload"]["job_id"] == record.verification_job_id
        assert record.shadow_output_sha256 == shadow["payload"]["output_sha256"]

        # The worker posts the primary receipt to the coordinator (as it does in
        # production); that is the moment the pair can be compared.
        await ai_env.coordinator.submit_receipt(_envelope(receipts[0]))

        record = await ai_env.store.get_verification(stream.job_id)
        assert record.status == "completed"
        assert record.outcome == "agreed"
        # The shadow run is not a job: it has no hold and never settles.
        assert len(ai_env.store.jobs) == 1
        assert record.verification_job_id not in ai_env.store.jobs
        events = [entry["event"] for entry in ai_env.store.audit_log]
        assert "job.verification_scheduled" in events
        assert "job.verification_dispatched" in events
        assert "job.verification_completed" in events

    @pytest.mark.asyncio
    async def test_output_disagreement_is_inconclusive_and_penalises_nobody(
        self, ai_env, ai_worker_factory
    ):
        ai_env.coordinator.verification = VerificationSampler(
            100, secret=b"unit-test-sampling-key"
        )
        agents = await _two_nodes(ai_env, ai_worker_factory)

        class _Divergent(EchoBackend):
            async def stream(self, **kwargs):
                self.last_usage = {"input_tokens": 5, "output_tokens": 3}
                self.last_transcript = ()
                for index, word in enumerate(["red", "green", "blue"]):
                    yield word if index == 0 else f" {word}"

        second_id = next(iter(agents))
        agents[second_id].backend = _Divergent(allow=True)

        receipts: list[dict] = []
        gateway = Gateway(ai_env.coordinator, transport=_transport(agents, receipts))
        stream = await gateway.open(ai_env.auth, _request())
        await gateway.complete(ai_env.auth, stream)
        await stream.verification_task
        await ai_env.coordinator.submit_receipt(_envelope(receipts[0]))

        record = await ai_env.store.get_verification(stream.job_id)
        assert record.status == "completed"
        # Two executions with no majority: the honest answer is "inconclusive",
        # and no node is penalised on that evidence alone.
        assert record.outcome == "inconclusive"
        assert record.faulty_node_id is None
        for node_id in (record.primary_node_id, record.verification_node_id):
            node = await ai_env.store.get_node(node_id)
            assert node is not None
            assert node.reputation == 50


class TestVerificationRefusals:
    @pytest.mark.asyncio
    async def test_assignment_is_handed_out_once(self, ai_env, ai_worker_factory):
        ai_env.coordinator.verification = VerificationSampler(
            100, secret=b"unit-test-sampling-key"
        )
        await _two_nodes(ai_env, ai_worker_factory)
        request = _request()
        auth = ai_env.auth
        placement = await ai_env.coordinator.place(
            auth,
            PlacementRequest(
                model=request.model,
                max_tokens=request.max_tokens,
                prompt_tokens_estimate=10,
            ),
        )
        first = await ai_env.coordinator.verification_assignment(placement.job_id)
        assert first is not None
        assert first["node_id"] != placement.ticket["payload"]["node_id"]
        assert (
            await ai_env.coordinator.verification_assignment(placement.job_id) is None
        )
        record = await ai_env.store.get_verification(placement.job_id)
        assert record.status == "dispatched"

    @pytest.mark.asyncio
    async def test_single_node_records_why_no_verification_ran(
        self, ai_env, ai_worker_factory
    ):
        ai_env.coordinator.verification = VerificationSampler(
            100, secret=b"unit-test-sampling-key"
        )
        only = await ai_worker_factory()
        ai_env.store.nodes[only.identity.node_id].endpoint = "http://worker-a"
        receipts: list[dict] = []
        gateway = Gateway(
            ai_env.coordinator,
            transport=_transport({only.identity.node_id: only}, receipts),
        )
        stream = await gateway.open(ai_env.auth, _request())
        await gateway.complete(ai_env.auth, stream)
        record = await ai_env.store.get_verification(stream.job_id)
        assert record is not None
        assert record.status == "no_alternate_node"
        assert stream.verification_task is None
        events = [entry["event"] for entry in ai_env.store.audit_log]
        assert "job.verification_skipped" in events

    @pytest.mark.asyncio
    async def test_daily_cap_stops_dispatch(self, ai_env, ai_worker_factory):
        ai_env.coordinator.verification = VerificationSampler(
            100, secret=b"unit-test-sampling-key"
        )
        ai_env.settings.verification_daily_cap = 0
        await _two_nodes(ai_env, ai_worker_factory)
        gateway = Gateway(ai_env.coordinator, transport=_unreachable)
        stream = await gateway.open(ai_env.auth, _request())
        record = await ai_env.store.get_verification(stream.job_id)
        assert record is not None
        assert record.status == "daily_cap_reached"
        assert stream.pending_verification is None

    @pytest.mark.asyncio
    async def test_unsampled_job_schedules_nothing(self, ai_env, ai_worker_factory):
        ai_env.coordinator.verification = VerificationSampler(
            0, secret=b"unit-test-sampling-key"
        )
        await ai_worker_factory()
        gateway = Gateway(ai_env.coordinator, transport=_unreachable)
        stream = await gateway.open(ai_env.auth, _request())
        assert await ai_env.store.get_verification(stream.job_id) is None


def _unreachable(url: str, payload: dict):
    """A transport that must not be used: these tests never stream a job."""
    raise AssertionError("no worker call expected in this test")


class TestShadowPayout:
    """The second operator is paid by the platform, once, from its own share."""

    @staticmethod
    def _shadow_amount(job, receipt_payload: dict) -> int:
        cost = ceil_div(
            receipt_payload["input_tokens"] * job.input_micro_per_mtok, MICRO_PER_MTOK
        ) + ceil_div(
            receipt_payload["output_tokens"] * job.output_micro_per_mtok,
            MICRO_PER_MTOK,
        )
        return split_amounts(cost, job.split).compute_micro

    @pytest.mark.asyncio
    async def test_shadow_operator_is_paid_from_platform_revenue_once(
        self, ai_env, ai_worker_factory
    ):
        ai_env.coordinator.verification = VerificationSampler(
            100, secret=b"unit-test-sampling-key"
        )
        agents = await _two_nodes(ai_env, ai_worker_factory)
        receipts: list[dict] = []
        gateway = Gateway(ai_env.coordinator, transport=_transport(agents, receipts))
        stream = await gateway.open(ai_env.auth, _request())
        await gateway.complete(ai_env.auth, stream)
        await stream.verification_task

        record = await ai_env.store.get_verification(stream.job_id)
        assert record is not None
        shadow_receipt = receipts[-1]
        assert shadow_receipt["payload"]["job_id"] == record.verification_job_id
        job = await ai_env.store.get_job(stream.job_id)
        assert job is not None
        expected = self._shadow_amount(job, shadow_receipt["payload"])
        assert (
            expected > 0
        )  # The gateway submits the shadow receipt itself (it is the only party
        # holding the plaintext), so the payment is already recorded here; a
        # retried submission must not add another.
        result = await ai_env.coordinator.submit_verification_receipt(
            _envelope(shadow_receipt)
        )
        assert result["payout_micro"] == expected

        payouts = [
            entry
            for entry in ai_env.store.ledger.entries
            if entry.kind == "verification_compute"
        ]
        assert len(payouts) == 1
        entry = payouts[0]
        assert entry.debit == PLATFORM_REVENUE
        assert entry.credit == compute_earnings(record.verification_node_id)
        assert entry.amount_micro == expected
        assert (
            entry.job_id == stream.job_id
        )  # The platform paid and nobody else did: the shadow run has no hold, so
        # at this point the platform's balance is exactly minus the payout and
        # the customer's credits are untouched.
        assert ai_env.store.ledger.balance(PLATFORM_REVENUE) == -expected
        assert ai_env.store.ledger.balance(f"account:{ai_env.account_id}:credits") == (
            1_000_000_000
        )
        assert ai_env.store.ledger.invariant_ok()
        events = [entry["event"] for entry in ai_env.store.audit_log]
        assert "job.verification_paid" in events

        # A retried receipt (the worker did not hear the 200) pays once.
        replay = await ai_env.coordinator.submit_verification_receipt(
            _envelope(shadow_receipt)
        )
        assert replay["payout_micro"] == expected
        assert (
            len(
                [
                    entry
                    for entry in ai_env.store.ledger.entries
                    if entry.kind == "verification_compute"
                ]
            )
            == 1
        )

        # And the primary job still settles exactly once, to the primary node:
        # the platform ends with its own share of the job minus the shadow payout.
        await ai_env.coordinator.submit_receipt(_envelope(receipts[0]))
        settlements = [
            entry
            for entry in ai_env.store.ledger.entries
            if entry.kind == "job_compute"
        ]
        assert len(settlements) == 1
        assert settlements[0].credit == compute_earnings(record.primary_node_id)
        settled = await ai_env.store.get_job(stream.job_id)
        assert settled is not None and settled.status == "settled"
        assert ai_env.store.ledger.balance(PLATFORM_REVENUE) == (
            settled.platform_micro - expected
        )
        assert ai_env.store.ledger.balance(f"account:{ai_env.account_id}:credits") == (
            1_000_000_000 - settled.total_micro
        )
        assert ai_env.store.ledger.invariant_ok()

    @pytest.mark.asyncio
    async def test_the_cap_bounds_one_shadow_payment(self, ai_env, ai_worker_factory):
        ai_env.coordinator.verification = VerificationSampler(
            100, secret=b"unit-test-sampling-key"
        )
        ai_env.settings.verification_payout_cap_micro = 1
        agents = await _two_nodes(ai_env, ai_worker_factory)
        receipts: list[dict] = []
        gateway = Gateway(ai_env.coordinator, transport=_transport(agents, receipts))
        stream = await gateway.open(ai_env.auth, _request())
        await gateway.complete(ai_env.auth, stream)
        await stream.verification_task

        result = await ai_env.coordinator.submit_verification_receipt(
            _envelope(receipts[-1])
        )
        assert result["payout_micro"] == 1
        paid = [
            entry["detail"]
            for entry in ai_env.store.audit_log
            if entry["event"] == "job.verification_paid"
        ]
        assert paid and paid[0]["capped"] is True
        assert paid[0]["amount_micro"] == 1

    @pytest.mark.asyncio
    async def test_a_second_different_shadow_receipt_is_refused(
        self, ai_env, ai_worker_factory
    ):
        ai_env.coordinator.verification = VerificationSampler(
            100, secret=b"unit-test-sampling-key"
        )
        agents = await _two_nodes(ai_env, ai_worker_factory)
        receipts: list[dict] = []
        gateway = Gateway(ai_env.coordinator, transport=_transport(agents, receipts))
        stream = await gateway.open(ai_env.auth, _request())
        await gateway.complete(ai_env.auth, stream)
        await stream.verification_task

        first = receipts[-1]
        await ai_env.coordinator.submit_verification_receipt(_envelope(first))

        # Re-signed, so the signature check passes and the *content* rule is what
        # refuses it: one shadow job cannot swap its output after the fact.
        record = await ai_env.store.get_verification(stream.job_id)
        assert record is not None
        agent = agents[record.verification_node_id]
        payload = dict(first["payload"])
        payload["output_sha256"] = "b" * 64
        signed = crypto.sign(
            RECEIPT_DOMAIN, payload, agent.identity.identity_private_key
        )
        forged = SignedEnvelope(
            payload=payload,
            key_id=agent.identity.identity_key_id,
            alg=first["alg"],
            sig=signed,
        )
        with pytest.raises(ConflictError):
            await ai_env.coordinator.submit_verification_receipt(forged)
        # The single payment recorded is for the first, genuine receipt.
        assert (
            len(
                [
                    entry
                    for entry in ai_env.store.ledger.entries
                    if entry.kind == "verification_compute"
                ]
            )
            == 1
        )


def test_default_sample_rate_and_cap_are_bounded():
    settings = AISettings(
        environment="development",
        api_key_pepper="unit-test-pepper-not-a-real-secret",
        coordinator_key_file=None,
    )
    assert 0 <= settings.verification_sample_pct <= 100
    assert settings.verification_daily_cap > 0
