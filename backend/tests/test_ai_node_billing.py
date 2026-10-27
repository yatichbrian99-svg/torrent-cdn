"""Gateway-mode billing authority and the per-node overclaim review.

The gateway holds the plaintext of every gateway-mode completion, so it can
count what it actually delivered instead of accepting the worker's receipt. These
tests pin the three rules that follow from that:

* a receipt that bills above the gateway's own count is billed at the **lower**
  (gateway) count, and the node is flagged — the customer is never charged a
  claim the coordinator can disprove;
* a receipt below the gateway's count keeps its own (lower) number and is
  flagged as an under-claim, not silently corrected upward;
* a node whose receipts overclaim in more than the configured share of its
  settled jobs is suspended and its **pending payouts are held**, so an operator
  reviews the money before it leaves.

The coordinator-level tests drive settlement directly (built receipts), which
keeps the authority rule honest: the receipt is signed by the real node identity
key and validated by the real verification path. The gateway-level test drives a
worker that lies about its token count over the real encrypted protocol.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.ai.clock import now_unix
from app.ai.ledger import PLATFORM_REVENUE, SEEDER_POOL, compute_earnings
from app.ai.models import (
    GatewayMeasurement,
    PlacementRequest,
    SignedEnvelope,
)
from app.ai.pricing import MICRO_PER_MTOK, ceil_div
from app.ai.receipts import sign_receipt
from app.ai.store import MemoryStore

#: The seeded model in the fixtures: 3 µ/Mtok in, 6 µ/Mtok out.
INPUT_MICRO_PER_MTOK = 3_000_000
OUTPUT_MICRO_PER_MTOK = 6_000_000


def cost_micro(input_tokens: int, output_tokens: int) -> int:
    """The same integer pricing the coordinator bills with, restated here."""
    return ceil_div(input_tokens * INPUT_MICRO_PER_MTOK, MICRO_PER_MTOK) + ceil_div(
        output_tokens * OUTPUT_MICRO_PER_MTOK, MICRO_PER_MTOK
    )


async def place_job(ai_env, *, max_tokens: int = 4_000, prompt_estimate: int = 40):
    """Place a job on the registered node and return its stored record."""
    placement = await ai_env.coordinator.place(
        ai_env.auth,
        PlacementRequest(
            model=ai_env.model.model_id,
            max_tokens=max_tokens,
            prompt_tokens_estimate=prompt_estimate,
        ),
    )
    job = await ai_env.store.get_job(placement.job_id)
    assert job is not None
    return job


async def measure(
    ai_env, job, *, prompt_tokens: int, completion_tokens: int, frames: int
):
    """Record the gateway's own measurement for a job (internal, no HTTP route)."""
    return await ai_env.coordinator.submit_gateway_measurement(
        job.job_id,
        GatewayMeasurement(
            job_id=job.job_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            token_frames=frames,
            output_sha256=hashlib.sha256(b"delivered").hexdigest(),
            measured_at=int(now_unix()),
        ),
    )


async def submit_receipt(ai_env, job, agent, *, input_tokens: int, output_tokens: int):
    """Submit a receipt signed by the node that was assigned the job."""
    payload = {
        "job_id": job.job_id,
        "node_id": job.node_id,
        "account_id": job.account_id,
        "model": job.model,
        "manifest_hash": job.manifest_hash,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "output_sha256": hashlib.sha256(b"delivered").hexdigest(),
        "started_at": job.created_at,
        "finished_at": job.created_at + 1,
    }
    document = sign_receipt(payload, agent.identity.identity_private_key)
    return await ai_env.coordinator.submit_receipt(
        SignedEnvelope(
            payload=document["payload"],
            key_id=document["key_id"],
            alg=document["alg"],
            sig=document["sig"],
        )
    )


def audit_events(store: MemoryStore, event: str) -> list[dict]:
    return [row for row in store.audit_log if row["event"] == event]


class TestGatewayBillingAuthority:
    @pytest.mark.asyncio
    async def test_an_overclaiming_receipt_is_billed_at_the_gateways_count(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        job = await place_job(ai_env)
        await measure(ai_env, job, prompt_tokens=40, completion_tokens=100, frames=100)

        # The worker claims 30x the output it delivered: 3000 tokens.
        settled = await submit_receipt(
            ai_env, job, agent, input_tokens=40, output_tokens=3_000
        )

        assert settled.status.value == "settled"
        assert "node_overclaim" in (settled.reason or "")
        assert settled.billable_output_tokens == 100
        assert settled.total_micro == cost_micro(40, 100)
        assert settled.total_micro < cost_micro(40, 3_000), "the claim was billed"
        assert (
            settled.compute_micro + settled.seeder_micro + settled.platform_micro
            == (settled.total_micro)
        )

        record = await ai_env.store.get_job(job.job_id)
        assert (record.input_tokens, record.output_tokens) == (40, 100)
        assert record.dispute_reason == "node_overclaim"
        # Ledger: the worker is paid the split of the capped (lower) total, and
        # nothing was posted for the 2900 tokens it invented.
        assert ai_env.store.ledger.balance(compute_earnings(record.node_id)) == (
            record.compute_micro
        )
        assert ai_env.store.ledger.balance(SEEDER_POOL) == record.seeder_micro
        assert ai_env.store.ledger.balance(PLATFORM_REVENUE) == record.platform_micro
        assert ai_env.store.ledger.invariant_ok()

        entries = [
            entry for entry in ai_env.store.ledger.entries if entry.job_id == job.job_id
        ]
        assert len(entries) == 3
        assert sum(entry.amount_micro for entry in entries) == settled.total_micro

        reviews = audit_events(ai_env.store, "job.billing_review")
        assert len(reviews) == 1
        detail = reviews[0]["detail"]
        assert detail["reason"] == "node_overclaim"
        assert detail["receipt_output_tokens"] == 3_000
        assert detail["charged_output_tokens"] == 100
        settled_event = audit_events(ai_env.store, "job.settled")[0]["detail"]
        assert settled_event["billed_from"] == "gateway_measurement"

        node = await ai_env.store.get_node(record.node_id)
        assert (node.settled_jobs, node.overclaim_events) == (1, 1)

        # The ledger above is the point: printed with `-s` this is the evidence
        # artefact (docs/tcdn-ai/evidence/12-billing-authority/). Nothing is
        # asserted here that the assertions above do not already pin.
        print("\noverclaiming worker: receipt says 3000 output tokens, gateway saw 100")
        print(f"{'account':<44} {'kind':<18} {'amount_micro':>13}")
        for entry in entries:
            print(
                f"{entry.debit + ' -> ' + entry.credit:<44} "
                f"{entry.kind:<18} {entry.amount_micro:>13}"
            )
        print(
            f"total billed: {settled.total_micro} micro (claim was {cost_micro(40, 3_000)})"
        )

    @pytest.mark.asyncio
    async def test_an_underclaiming_receipt_keeps_its_own_lower_number(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        job = await place_job(ai_env)
        # The gateway counted far more than the worker claims (an engine whose
        # tokenisation differs). Bill the worker's own lower number and record it.
        await measure(ai_env, job, prompt_tokens=40, completion_tokens=100, frames=100)

        settled = await submit_receipt(
            ai_env, job, agent, input_tokens=40, output_tokens=10
        )

        assert settled.billable_output_tokens == 10
        assert settled.total_micro == cost_micro(40, 10)
        record = await ai_env.store.get_job(job.job_id)
        assert record.dispute_reason == "node_receipt_underclaim"
        node = await ai_env.store.get_node(record.node_id)
        assert node.overclaim_events == 0, "under-claiming is not an overclaim"
        assert node.settled_jobs == 1

    @pytest.mark.asyncio
    async def test_a_receipt_inside_the_tolerance_is_billed_as_claimed(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        job = await place_job(ai_env)
        await measure(ai_env, job, prompt_tokens=40, completion_tokens=100, frames=100)

        # 110 output tokens is 10 % above the gateway's 100: inside the 25 %
        # default tolerance, so the worker's own count stands and no review flag
        # is raised — the tolerance is what keeps an honest engine honest.
        settled = await submit_receipt(
            ai_env, job, agent, input_tokens=40, output_tokens=110
        )

        assert settled.billable_output_tokens == 110
        assert settled.total_micro == cost_micro(40, 110)
        record = await ai_env.store.get_job(job.job_id)
        assert record.dispute_reason is None
        node = await ai_env.store.get_node(record.node_id)
        assert (node.settled_jobs, node.overclaim_events) == (1, 0)

    @pytest.mark.asyncio
    async def test_without_a_measurement_the_receipt_still_bills(
        self, ai_env, ai_worker_factory
    ):
        """SDK mode, or a job whose gateway never measured: the receipt is authority.

        Proves the measurement *adds* authority rather than gating payment — a
        receipt must still settle on its own (the settlement bypass fix).
        """
        agent = await ai_worker_factory()
        job = await place_job(ai_env)
        settled = await submit_receipt(
            ai_env, job, agent, input_tokens=40, output_tokens=100
        )
        assert settled.billable_output_tokens == 100
        assert settled.total_micro == cost_micro(40, 100)
        assert (await ai_env.store.get_job(job.job_id)).dispute_reason is None

    @pytest.mark.asyncio
    async def test_the_two_authorities_stack_measurement_then_hold(
        self, ai_env, ai_worker_factory
    ):
        """The hold caps the bill even when the receipt overclaims past it.

        The receipt claims 10 000 input tokens (far above the gateway's 100), so
        the gateway's count is billed — and that count is itself above the
        reservation, so the hold caps it. Both decisions are recorded, in the
        order they were made.
        """
        agent = await ai_worker_factory()
        job = await place_job(ai_env, max_tokens=8, prompt_estimate=40)
        hold_micro = job.hold_micro
        await measure(ai_env, job, prompt_tokens=100, completion_tokens=60, frames=8)
        assert cost_micro(100, 60) > hold_micro, "the measurement must exceed the hold"

        settled = await submit_receipt(
            ai_env, job, agent, input_tokens=10_000, output_tokens=8
        )

        assert settled.total_micro == hold_micro
        assert settled.total_micro < cost_micro(100, 60)
        reason = settled.reason or ""
        assert "node_overclaim" in reason
        assert "settlement_exceeds_hold" in reason
        assert (await ai_env.store.get_job(job.job_id)).dispute_reason == (
            "node_overclaim+settlement_exceeds_hold"
        )


class TestNodeOverclaimReview:
    async def _settle_overclaims(self, ai_env, agent, count: int) -> str:
        """Settle ``count`` jobs whose receipts bill far above the measurement."""
        node_id = ""
        for _ in range(count):
            job = await place_job(ai_env)
            await measure(
                ai_env, job, prompt_tokens=40, completion_tokens=100, frames=100
            )
            settled = await submit_receipt(
                ai_env, job, agent, input_tokens=40, output_tokens=3_000
            )
            assert settled.status.value == "settled"
            node_id = (await ai_env.store.get_job(job.job_id)).node_id
        return node_id

    @pytest.mark.asyncio
    async def test_an_overclaiming_node_is_suspended_and_its_payouts_held(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        threshold = ai_env.settings.node_overclaim_rate_pct
        samples = ai_env.settings.node_overclaim_min_samples
        assert threshold > 0 and samples >= 1

        node_id = await self._settle_overclaims(ai_env, agent, samples)

        node = await ai_env.store.get_node(node_id)
        assert node.settled_jobs == samples
        assert node.overclaim_events == samples
        assert node.overclaim_rate_pct == 100
        assert node.suspended is True
        assert node.payouts_held is True
        assert node.review_reason and "overclaim" in node.review_reason

        holds = audit_events(ai_env.store, "node.held_for_review")
        assert len(holds) == 1, "the review must be raised exactly once"
        assert holds[0]["subject"] == node_id
        assert holds[0]["detail"]["payouts_held"] is True
        assert holds[0]["detail"]["threshold_pct"] == threshold
        assert holds[0]["detail"]["min_samples"] == samples

    @pytest.mark.asyncio
    async def test_the_threshold_needs_the_sample_floor(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        samples = ai_env.settings.node_overclaim_min_samples
        node_id = await self._settle_overclaims(ai_env, agent, samples - 1)
        node = await ai_env.store.get_node(node_id)
        assert node.overclaim_rate_pct == 100
        assert node.suspended is False, f"held on {samples - 1} of {samples} samples"

    @pytest.mark.asyncio
    async def test_held_payouts_are_recorded_but_not_paid(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        samples = ai_env.settings.node_overclaim_min_samples
        node_id = await self._settle_overclaims(ai_env, agent, samples)
        payable = ai_env.store.ledger.balance(compute_earnings(node_id))
        assert payable > 0

        await ai_env.coordinator.record_payout(
            {
                "period_start": 0,
                "period_end": 86_400,
                "node_id": node_id,
                "component": "compute",
                "amount_micro": payable,
            },
            actor="test-scheduler",
        )

        assert len(ai_env.store.payouts) == 1
        held = ai_env.store.payouts[0]
        assert held["status"] == "held_for_review"
        assert held["amount_micro"] == payable
        # The money is still the node's — the hold is a review, not a claw-back —
        # and it has not moved anywhere.
        assert ai_env.store.ledger.balance(compute_earnings(node_id)) == payable
        assert ai_env.store.ledger.invariant_ok()
        events = audit_events(ai_env.store, "payout.held_node_review")
        assert len(events) == 1
        assert events[0]["detail"]["paid"] is False
        assert "overclaim" in events[0]["detail"]["hold_reason"]
        assert audit_events(ai_env.store, "payout.recorded") == []

    @pytest.mark.asyncio
    async def test_a_clean_node_is_paid_normally(self, ai_env, ai_worker_factory):
        agent = await ai_worker_factory()
        for _ in range(ai_env.settings.node_overclaim_min_samples):
            job = await place_job(ai_env)
            await measure(
                ai_env, job, prompt_tokens=40, completion_tokens=100, frames=100
            )
            await submit_receipt(ai_env, job, agent, input_tokens=40, output_tokens=100)

        job = await place_job(ai_env)
        await measure(ai_env, job, prompt_tokens=40, completion_tokens=100, frames=100)
        await submit_receipt(ai_env, job, agent, input_tokens=40, output_tokens=100)
        node_id = (await ai_env.store.get_job(job.job_id)).node_id
        node = await ai_env.store.get_node(node_id)
        assert node.settled_jobs == ai_env.settings.node_overclaim_min_samples + 1
        assert node.overclaim_events == 0
        assert node.payouts_held is False

        await ai_env.coordinator.record_payout(
            {
                "period_start": 0,
                "period_end": 86_400,
                "node_id": node_id,
                "component": "compute",
                "amount_micro": 500,
            },
            actor="test-scheduler",
        )
        assert ai_env.store.payouts[0].get("status") != "held_for_review"
        assert len(audit_events(ai_env.store, "payout.recorded")) == 1

    @pytest.mark.asyncio
    async def test_a_suspended_node_is_not_scheduled(self, ai_env, ai_worker_factory):
        agent = await ai_worker_factory()
        samples = ai_env.settings.node_overclaim_min_samples
        node_id = await self._settle_overclaims(ai_env, agent, samples)

        from app.ai.errors import NoEligibleWorker

        with pytest.raises(NoEligibleWorker):
            await place_job(ai_env)
        assert (await ai_env.store.get_node(node_id)).suspended is True

    @pytest.mark.asyncio
    async def test_the_same_work_from_an_honest_node_raises_no_review(
        self, ai_env, ai_worker_factory
    ):
        """Control: identical traffic with matching counts never trips the review."""
        agent = await ai_worker_factory()
        node_id = ""
        for _ in range(ai_env.settings.node_overclaim_min_samples + 2):
            job = await place_job(ai_env)
            await measure(
                ai_env, job, prompt_tokens=40, completion_tokens=100, frames=100
            )
            settled = await submit_receipt(
                ai_env, job, agent, input_tokens=40, output_tokens=100
            )
            node_id = (await ai_env.store.get_job(job.job_id)).node_id
            assert settled.reason == "billed from the worker receipt"
        node = await ai_env.store.get_node(node_id)
        assert node.suspended is False
        assert audit_events(ai_env.store, "node.held_for_review") == []


class TestGatewayCountsItsOwnTokens:
    """The measurement comes from the plaintext the gateway served, not the receipt.

    The worker here inflates its receipt by 30x over the real encrypted protocol,
    so the number that reaches settlement must come from the gateway's own count
    of the token frames it decrypted and delivered.
    """

    def _inflating_transport(self, agent, factor: int, observed: dict):
        from tcdn_ai_sdk.protocol import (
            FINAL_FRAME,
            decode_frames,
            encode_frame,
        )

        async def transport(url: str, payload: dict):
            frames = await agent.process(payload)
            parsed, _ = decode_frames(frames)
            rewritten = []
            for frame in parsed:
                if frame.kind == FINAL_FRAME:
                    receipt = json.loads(frame.body.decode("utf-8"))
                    claimed = dict(receipt["payload"])
                    # The lie: bill for many more output tokens than were sent.
                    claimed["output_tokens"] = max(1, claimed["output_tokens"]) * factor
                    observed["claimed_output_tokens"] = claimed["output_tokens"]
                    forged = sign_receipt(claimed, agent.identity.identity_private_key)
                    observed["receipt"] = forged
                    frame = type(frame)(
                        kind=FINAL_FRAME, body=json.dumps(forged).encode("utf-8")
                    )
                rewritten.append(frame)
            observed["token_frames"] = sum(
                1 for f in rewritten if f.kind != FINAL_FRAME
            )
            yield b"".join(encode_frame(f.kind, f.body) for f in rewritten)

        return transport

    def _request(self, ai_env):
        from app.ai.gateway import parse_chat_request

        return parse_chat_request(
            {
                "model": ai_env.model.model_id,
                "messages": [{"role": "user", "content": "hello there " * 4}],
                # Room for the forged claim: it is a multiple of the honest count,
                # and a receipt may never exceed the ticket's max_tokens.
                "max_tokens": 2_048,
            },
            ai_env.settings,
        )

    @pytest.mark.asyncio
    async def test_an_inflating_worker_is_billed_at_the_count_the_gateway_streamed(
        self, ai_env, ai_worker_factory
    ):
        from app.ai.gateway import Gateway

        # Deterministic: this test is about billing, not about the sampling coin.
        ai_env.settings.verification_sample_pct = 0
        agent = await ai_worker_factory()
        observed: dict = {}
        gateway = Gateway(
            ai_env.coordinator, transport=self._inflating_transport(agent, 3, observed)
        )

        result = await gateway.complete(
            ai_env.auth, await gateway.open(ai_env.auth, self._request(ai_env))
        )

        claimed = observed["claimed_output_tokens"]
        frames = observed["token_frames"]
        assert frames > 0, "the worker streamed no tokens — nothing to measure"
        assert claimed > result.completion_tokens, "the inflating transport did not lie"

        measurement = await ai_env.store.get_gateway_measurement(result.job_id)
        assert measurement is not None
        assert measurement["completion_tokens"] == max(frames, len(result.text) // 4)
        assert measurement["token_frames"] == frames
        # The usage handed to the customer is the gateway's count too.
        assert result.completion_tokens == measurement["completion_tokens"]

        # The worker posts its (forged) receipt, as it does over HTTP in a real
        # deployment: the measurement is already recorded, so settlement has it.
        forged = observed["receipt"]
        settled = await ai_env.coordinator.submit_receipt(
            SignedEnvelope(
                payload=forged["payload"],
                key_id=forged["key_id"],
                alg=forged["alg"],
                sig=forged["sig"],
            )
        )
        assert settled.status.value == "settled"
        assert settled.billable_output_tokens == result.completion_tokens

        record = await ai_env.store.get_job(result.job_id)
        assert record.status == "settled"
        assert record.output_tokens == result.completion_tokens
        assert record.output_tokens < claimed, "the inflated claim was billed"
        assert record.dispute_reason == "node_overclaim"
        node = await ai_env.store.get_node(record.node_id)
        assert (node.settled_jobs, node.overclaim_events) == (1, 1)
        assert audit_events(ai_env.store, "job.gateway_measured"), (
            "the measurement must be recorded as its own audit event"
        )

    @pytest.mark.asyncio
    async def test_the_tally_counts_delivered_token_frames(self):
        """Unit-level: the tally counts token frames and ignores the final frame."""
        from app.ai.gateway import TokenTally, estimate_tokens

        tally = TokenTally()
        for _ in range(7):
            tally.add("token")
        tally.add("final")
        assert tally.frames == 7
        # The estimate floors the count when a worker packs tokens into frames.
        assert estimate_tokens("") == 0
        assert estimate_tokens("x" * 40) == 10

    @pytest.mark.asyncio
    async def test_a_customer_cannot_assert_a_measurement(self, ai_env):
        """There is no HTTP route for the measurement, so the payer cannot price it."""
        from app.ai import routes

        paths = {
            route.path
            for route in routes.router.routes  # type: ignore[attr-defined]
        } | {
            route.path
            for route in routes.admin_router.routes  # type: ignore[attr-defined]
        }
        assert not [path for path in paths if "measurement" in path], paths
