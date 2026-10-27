"""Service integration: coordinator + worker agent, real crypto and receipts."""

from __future__ import annotations

import hashlib
import os

import pytest

from app.ai import crypto
from app.ai.errors import (
    AttestationError,
    ConcurrencyLimitExceeded,
    CryptoError,
    InsufficientCredits,
    NotFoundError,
    ReceiptError,
)
from app.ai.ledger import PLATFORM_REVENUE, SEEDER_POOL, compute_earnings
from app.ai.models import (
    AckPayload,
    AttestationEvidencePayload,
    AttestationPlatform,
    Confidentiality,
    PlacementRequest,
    SignedEnvelope,
)
from app.ai.receipts import sign_receipt
from app.ai.service import ATTESTATION_EVIDENCE_DOMAIN, JOB_TTL_SECONDS
from tcdn_ai_sdk.protocol import (
    FINAL_FRAME,
    TOKEN_FRAME,
    b64e,
    canonical_json,
    decode_frames,
    request_aad,
    session_context,
    stream_aad,
)


async def run_encrypted_job(
    env, agent, *, text="hello world", max_tokens=8, confidential=False
):
    placement = await env.coordinator.place(
        env.auth,
        PlacementRequest(
            model=env.model.model_id,
            max_tokens=max_tokens,
            prompt_tokens_estimate=5,
            confidential=confidential,
        ),
    )
    job_id = placement.job_id
    ticket = placement.ticket
    payload = ticket["payload"]
    ephemeral = crypto.generate_x25519()
    client_private, client_public = ephemeral["private_key"], ephemeral["public_key"]
    salt = os.urandom(32)
    shared = crypto.x25519_shared_secret(client_private, payload["worker_public_key"])
    c2w, w2c = crypto.derive_session_keys(
        shared, salt=salt, context=session_context(job_id)
    )
    request_plaintext = canonical_json(
        {
            "model": env.model.model_id,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "top_p": 1.0,
            "stop": [],
            "stream": False,
        }
    )
    ticket_digest = crypto.sha256_hex(canonical_json(ticket))
    ciphertext = crypto.aead_encrypt(
        c2w, request_plaintext, request_aad(job_id, ticket_digest)
    )
    frames = await agent.process(
        {
            "job_id": job_id,
            "ticket": ticket,
            "client_public_key": client_public,
            "salt": b64e(salt),
            "ciphertext": b64e(ciphertext),
        }
    )
    parsed, remainder = decode_frames(frames)
    assert remainder == b""
    tokens: list[str] = []
    sequence = 0
    receipt = None
    for frame in parsed:
        if frame.kind == TOKEN_FRAME:
            tokens.append(
                crypto.aead_decrypt(
                    w2c, frame.body, stream_aad(job_id, sequence)
                ).decode()
            )
            sequence += 1
        elif frame.kind == FINAL_FRAME:
            import json

            receipt = json.loads(frame.body)
    assert receipt is not None
    return placement, "".join(tokens), receipt


async def submit_receipt(env, receipt: dict):
    return await env.coordinator.submit_receipt(
        SignedEnvelope(
            payload=receipt["payload"],
            key_id=receipt["key_id"],
            alg=receipt["alg"],
            sig=receipt["sig"],
        )
    )


class TestEncryptedJob:
    @pytest.mark.asyncio
    async def test_placement_worker_receipt_ack_settlement(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        placement, text, receipt = await run_encrypted_job(ai_env, agent)
        assert text.startswith(f"echo[{ai_env.model.model_id}]")
        assert receipt["payload"]["job_id"] == placement.job_id

        # The worker's signed receipt is the billing event: it settles the job on
        # its own. Billing used to wait for the client's ack, which made revenue
        # optional for the payer — a client that never acked (or acked a
        # different hash) left the job unsettled, and the expiry sweep then
        # released the hold, so the completion was free.
        settled = await submit_receipt(ai_env, receipt)
        assert settled.status.value == "settled"
        assert settled.total_micro > 0
        assert (
            settled.compute_micro + settled.seeder_micro + settled.platform_micro
            == settled.total_micro
        )
        job = await ai_env.store.get_job(placement.job_id)
        assert job.status == "settled"
        assert job.dispute_reason is None
        assert (
            ai_env.store.ledger.balance(compute_earnings(job.node_id))
            == settled.compute_micro
        )
        assert ai_env.store.ledger.balance(SEEDER_POOL) == settled.seeder_micro
        assert ai_env.store.ledger.balance(PLATFORM_REVENUE) == settled.platform_micro
        assert await ai_env.store.held_micro(ai_env.account_id) == 0
        assert ai_env.store.ledger.invariant_ok()

        # A late ack that agrees is confirmation, not a second payment.
        ack = AckPayload(
            job_id=placement.job_id,
            tokens_received=receipt["payload"]["output_tokens"],
            output_sha256=hashlib.sha256(text.encode()).hexdigest(),
            acked_at=1_000,
        )
        confirmed = await ai_env.coordinator.submit_ack(
            ai_env.auth, placement.job_id, ack
        )
        assert confirmed.status.value == "settled"
        assert confirmed.total_micro == settled.total_micro
        assert (await ai_env.store.get_job(placement.job_id)).dispute_reason is None
        entries = [
            e for e in ai_env.store.ledger.entries if e.job_id == placement.job_id
        ]
        assert len(entries) == 3  # the ack posted nothing

    @pytest.mark.asyncio
    async def test_coordinator_never_receives_or_stores_prompt(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        canary = "CANARY-PROMPT-9f13-do-not-store"
        placement, _, receipt = await run_encrypted_job(ai_env, agent, text=canary)
        await submit_receipt(ai_env, receipt)

        job = await ai_env.store.get_job(placement.job_id)
        assert "messages" not in job.__dict__
        assert canary not in repr(job)
        assert canary not in repr(ai_env.store.audit_log)
        assert canary not in repr(placement.model_dump())

    @pytest.mark.asyncio
    async def test_tampered_ciphertext_is_rejected(self, ai_env, ai_worker_factory):
        agent = await ai_worker_factory()
        placement = await ai_env.coordinator.place(
            ai_env.auth, PlacementRequest(model=ai_env.model.model_id, max_tokens=4)
        )
        payload = placement.ticket["payload"]
        ephemeral = crypto.generate_x25519()
        client_private, client_public = (
            ephemeral["private_key"],
            ephemeral["public_key"],
        )
        salt = os.urandom(32)
        shared = crypto.x25519_shared_secret(
            client_private, payload["worker_public_key"]
        )
        c2w, _ = crypto.derive_session_keys(
            shared, salt=salt, context=session_context(placement.job_id)
        )
        digest = crypto.sha256_hex(canonical_json(placement.ticket))
        ciphertext = bytearray(
            crypto.aead_encrypt(
                c2w,
                canonical_json({"model": ai_env.model.model_id}),
                request_aad(placement.job_id, digest),
            )
        )
        ciphertext[-1] ^= 0x01
        with pytest.raises(CryptoError):
            await agent.process(
                {
                    "job_id": placement.job_id,
                    "ticket": placement.ticket,
                    "client_public_key": client_public,
                    "salt": b64e(salt),
                    "ciphertext": b64e(bytes(ciphertext)),
                }
            )

    @pytest.mark.asyncio
    async def test_reserved_ticket_cannot_be_reused(self, ai_env, ai_worker_factory):
        agent = await ai_worker_factory()
        placement, _, _ = await run_encrypted_job(ai_env, agent)
        # A second, independently placed job is fine; replay of the *first*
        # ticket is what the worker rejects.
        with pytest.raises(Exception) as excinfo:
            await agent.process(
                {"job_id": placement.job_id, "ticket": placement.ticket}
            )
        assert "ticket" in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_receipt_from_unassigned_node_rejected(
        self, ai_env, ai_worker_factory
    ):
        first = await ai_worker_factory()
        # Place while `first` is the only eligible node, so the ticket provably
        # belongs to it (the scheduler deliberately randomises among equals).
        _, _, receipt = await run_encrypted_job(ai_env, first)
        second = await ai_worker_factory()
        # An unassigned worker signs a receipt for the first worker's job.
        forged = dict(receipt)
        forged_payload = dict(receipt["payload"])
        forged_payload["node_id"] = second.identity.node_id
        from app.ai.receipts import sign_receipt

        forged = sign_receipt(forged_payload, second.identity.identity_private_key)
        with pytest.raises(ReceiptError):
            await submit_receipt(ai_env, forged)

    @pytest.mark.asyncio
    async def test_ack_from_another_account_is_not_found(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        placement, text, _ = await run_encrypted_job(ai_env, agent)
        other_key = await ai_env.coordinator.provision_api_key(
            "22222222-2222-4222-8222-222222222222"
        )
        other_auth = await ai_env.coordinator.authenticate(other_key)
        ack = AckPayload(
            job_id=placement.job_id,
            tokens_received=1,
            output_sha256=hashlib.sha256(text.encode()).hexdigest(),
            acked_at=1_000,
        )
        with pytest.raises(NotFoundError):
            await ai_env.coordinator.submit_ack(other_auth, placement.job_id, ack)

    @pytest.mark.asyncio
    async def test_mismatched_ack_flags_for_review_without_cancelling_the_charge(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        placement, _, receipt = await run_encrypted_job(ai_env, agent)
        billed = await submit_receipt(ai_env, receipt)
        assert billed.status.value == "settled"

        ack = AckPayload(
            job_id=placement.job_id,
            tokens_received=receipt["payload"]["output_tokens"],
            output_sha256="b" * 64,  # wrong output hash
            acked_at=1_000,
        )
        result = await ai_env.coordinator.submit_ack(ai_env.auth, placement.job_id, ack)

        # The ack is evidence, never a payment trigger: the receipt's charge
        # stands, and an operator is asked to look at it. A payer who could undo
        # the charge by acking would make every completion free.
        assert result.status.value == "settled"
        assert result.total_micro == billed.total_micro
        assert "output_hash_mismatch" in (result.reason or "")
        job = await ai_env.store.get_job(placement.job_id)
        assert job.status == "settled"
        assert job.dispute_reason == "output_hash_mismatch"
        assert (
            ai_env.store.ledger.balance(compute_earnings(job.node_id))
            == billed.compute_micro
        )
        events = [
            e for e in ai_env.store.audit_log if e["event"] == "job.billing_review"
        ]
        assert [e["subject"] for e in events] == [placement.job_id]
        assert ai_env.store.ledger.invariant_ok()


class TestConfidentialTier:
    async def _attest(self, env, agent):
        nonce = await env.coordinator.issue_attestation_nonce(agent.identity.node_id)
        evidence = AttestationEvidencePayload(
            node_id=agent.identity.node_id,
            platform=AttestationPlatform.SEV_SNP,
            nonce=nonce,
            session_key_sha256=crypto.sha256_hex(
                crypto.b64d(agent.identity.session_public_key)
            ),
            report_b64=crypto.b64e(b"simulated-report"),
            cert_chain_pem=[],
            issued_at=int(env.coordinator.clock()),
        ).model_dump(mode="json")
        envelope = SignedEnvelope(
            payload=evidence,
            key_id=agent.identity.identity_key_id,
            sig=crypto.sign(
                ATTESTATION_EVIDENCE_DOMAIN,
                evidence,
                agent.identity.identity_private_key,
            ),
        )
        return await env.coordinator.attest(env.auth, envelope)

    @pytest.mark.asyncio
    async def test_confidential_job_runs_only_on_attested_node(
        self, ai_env, ai_worker_factory
    ):
        attested = await ai_worker_factory(
            confidentiality=Confidentiality.ATTESTED_TEE, attested=True
        )
        await ai_worker_factory()  # standard node, must not be selected
        await self._attest(ai_env, attested)

        placement, _, _ = await run_encrypted_job(ai_env, attested, confidential=True)
        assert placement.confidentiality is Confidentiality.ATTESTED_TEE
        assert "attested" in placement.confidentiality_notice
        assert placement.key_attestation_ref
        job = await ai_env.store.get_job(placement.job_id)
        assert job.node_id == attested.identity.node_id
        assert job.tier == "confidential"

    @pytest.mark.asyncio
    async def test_confidential_refused_without_attestation(
        self, ai_env, ai_worker_factory
    ):
        await ai_worker_factory(
            confidentiality=Confidentiality.ATTESTED_TEE, attested=True
        )
        from app.ai.errors import NoEligibleWorker

        with pytest.raises(NoEligibleWorker):
            await ai_env.coordinator.place(
                ai_env.auth,
                PlacementRequest(
                    model=ai_env.model.model_id, max_tokens=4, confidential=True
                ),
            )

    @pytest.mark.asyncio
    async def test_worker_refuses_confidential_without_attestation(
        self, ai_env, ai_worker_factory
    ):
        # Build the ticket directly so the scheduler's filter is bypassed; the
        # worker must still refuse on its own.
        agent = await ai_worker_factory(
            confidentiality=Confidentiality.ATTESTED_TEE, attested=False
        )
        document, _ = ai_env.coordinator.tickets.issue(
            job_id="job_direct",
            account_id=ai_env.account_id,
            node_id=agent.identity.node_id,
            model=ai_env.model.model_id,
            worker_public_key=agent.identity.session_public_key,
            confidentiality=Confidentiality.ATTESTED_TEE,
            max_tokens=4,
            max_price_micro=100,
        )
        with pytest.raises(AttestationError):
            await agent.process({"job_id": "job_direct", "ticket": document})


class TestLogHygiene:
    @pytest.mark.asyncio
    async def test_prompt_and_api_key_never_logged(
        self, ai_env, ai_worker_factory, caplog
    ):
        import logging as _logging

        caplog.set_level(_logging.DEBUG, logger="app.ai")
        caplog.set_level(_logging.DEBUG, logger="tcdn_ai_sdk")
        agent = await ai_worker_factory()
        canary = "CANARY-LOG-c4f2-never-log-me"
        await run_encrypted_job(ai_env, agent, text=canary)

        combined = caplog.text
        assert canary not in combined
        assert ai_env.api_key not in combined
        assert "ciphertext" not in combined.lower()

    @pytest.mark.asyncio
    async def test_oversized_prompt_rejected(self, ai_env, ai_worker_factory):
        from app.ai.errors import ValidationError

        agent = await ai_worker_factory()
        with pytest.raises(ValidationError) as excinfo:
            await run_encrypted_job(ai_env, agent, text="x" * 200_001, max_tokens=4)
        assert excinfo.value.code == "prompt_too_large"


class TestMoneyEdges:
    @pytest.mark.asyncio
    async def test_hold_prevents_overspending(self, ai_env):
        # Drain the account, then a placement must fail before any job exists.
        account = "11111111-1111-4111-8111-111111111111"
        balance = await ai_env.store.balance_micro(account)
        await ai_env.store.credit(
            "00000000-0000-4000-8000-000000000000",
            amount_micro=10,
            source="gateway:test",
            idempotency_key="noop",
            kind="credit_purchase",
        )
        with pytest.raises(InsufficientCredits):
            ai_env.store.ledger.place_hold(
                job_id="drain",
                account=f"account:{account}:credits",
                amount_micro=balance + 1,
            )

    @pytest.mark.asyncio
    async def test_double_ack_is_idempotent(self, ai_env, ai_worker_factory):
        agent = await ai_worker_factory()
        placement, text, receipt = await run_encrypted_job(ai_env, agent)
        await submit_receipt(ai_env, receipt)
        ack = AckPayload(
            job_id=placement.job_id,
            tokens_received=receipt["payload"]["output_tokens"],
            output_sha256=hashlib.sha256(text.encode()).hexdigest(),
            acked_at=1_000,
        )
        first = await ai_env.coordinator.submit_ack(ai_env.auth, placement.job_id, ack)
        second = await ai_env.coordinator.submit_ack(ai_env.auth, placement.job_id, ack)
        assert first.status.value == second.status.value == "settled"
        assert first.total_micro == second.total_micro
        entries = [
            e for e in ai_env.store.ledger.entries if e.job_id == placement.job_id
        ]
        assert len(entries) == 3  # no duplicate postings on replay


class TestHoldHeadroomAndDisputes:
    """Real engines report more input tokens than the client can estimate.

    The coordinator never sees the prompt (Part 1 invariant), so it reserves
    credits from the client's estimate. A real engine counts the prompt AFTER
    applying the model's chat template, which adds tens of tokens: measured with
    Ollama + Qwen2.5-0.5B, a 7-token estimate became 48. Before the headroom fix
    every real-engine job settled above its hold, was marked disputed, and the
    reservation stayed open — the customer's credits were frozen (H-1).

    The hold is now a hard ceiling on what a worker's receipt can bill, and a
    receipt that reaches it is billed to the ceiling and flagged for review
    rather than refused: refusing it would hand the completion to the customer
    for free.
    """

    async def _settle_with_input_tokens(
        self, ai_env, agent, placement, text, receipt, input_tokens: int
    ):
        payload = dict(receipt["payload"])
        payload["input_tokens"] = input_tokens
        signed = sign_receipt(payload, agent.identity.identity_private_key)
        # The receipt is the billing event; the ack that follows only confirms it.
        billed = await submit_receipt(ai_env, signed)
        ack = AckPayload(
            job_id=placement.job_id,
            tokens_received=payload["output_tokens"],
            output_sha256=payload["output_sha256"],
            acked_at=1_000,
        )
        confirmed = await ai_env.coordinator.submit_ack(
            ai_env.auth, placement.job_id, ack
        )
        assert confirmed.total_micro == billed.total_micro, "the ack moved money"
        return billed

    @pytest.mark.asyncio
    async def test_engine_chat_template_overhead_settles_within_the_hold(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        placement, text, receipt = await run_encrypted_job(ai_env, agent)
        job = await ai_env.store.get_job(placement.job_id)
        assert job.hold_micro > job.max_price_micro, "the hold carries no headroom"

        settled = await self._settle_with_input_tokens(
            ai_env, agent, placement, text, receipt, input_tokens=48
        )
        assert settled.status.value == "settled", settled.reason
        assert await ai_env.store.held_micro(ai_env.account_id) == 0
        assert ai_env.store.ledger.invariant_ok()

    @pytest.mark.asyncio
    async def test_over_hold_receipt_is_capped_and_flagged(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        placement, text, receipt = await run_encrypted_job(ai_env, agent)
        hold_micro = (await ai_env.store.get_job(placement.job_id)).hold_micro

        # Far beyond any reservation: this is a real overrun, not a rounding
        # difference. The worker cannot bill past the hold it was granted, but
        # the excess is recorded for review instead of voiding the charge.
        result = await self._settle_with_input_tokens(
            ai_env, agent, placement, text, receipt, input_tokens=100_000
        )
        assert result.status.value == "settled"
        assert result.total_micro == hold_micro
        assert "settlement_exceeds_hold" in (result.reason or "")

        # H-1: the reservation must not stay open forever.
        assert await ai_env.store.held_micro(ai_env.account_id) == 0
        job = await ai_env.store.get_job(placement.job_id)
        assert job.status == "settled"
        assert job.dispute_reason == "settlement_exceeds_hold"
        assert job.total_micro == hold_micro
        # The node delivered the completion, so it is paid — capping is a
        # platform decision, not a reason to stiff the worker.
        assert job.compute_micro + job.seeder_micro + job.platform_micro == hold_micro
        assert (
            ai_env.store.ledger.balance(compute_earnings(job.node_id))
            == job.compute_micro
        )
        assert ai_env.store.ledger.invariant_ok()


class TestConcurrentJobCap:
    """Row 2.6: ``max_concurrent_jobs_per_key`` is enforced, not just configured.

    The count comes from the store, so it is the account's number and not
    whichever process took the request; a settled job and an expired one both
    free a slot, which is what keeps a crashed client from wedging its own key.
    """

    async def _place(self, env):
        return await env.coordinator.place(
            env.auth, PlacementRequest(model=env.model.model_id, max_tokens=4)
        )

    @pytest.mark.asyncio
    async def test_the_cap_refuses_before_any_work_and_creates_no_job(
        self, ai_env, ai_worker_factory
    ):
        await ai_worker_factory()
        ai_env.settings.max_concurrent_jobs_per_key = 2

        await self._place(ai_env)
        await self._place(ai_env)
        jobs_before = len(ai_env.store.jobs)
        held_before = await ai_env.store.held_micro(ai_env.account_id)

        with pytest.raises(ConcurrencyLimitExceeded) as refused:
            await self._place(ai_env)

        assert refused.value.status_code == 429, "a cap is a 429, not a 500"
        assert refused.value.code == "too_many_concurrent_jobs"
        assert refused.value.details["limit"] == 2
        assert refused.value.details["in_flight"] == 2
        # Nothing was created, held or charged by the refusal.
        assert len(ai_env.store.jobs) == jobs_before
        assert await ai_env.store.held_micro(ai_env.account_id) == held_before
        assert await ai_env.store.count_active_jobs(ai_env.account_id) == 2

    @pytest.mark.asyncio
    async def test_settling_a_job_frees_its_slot(self, ai_env, ai_worker_factory):
        agent = await ai_worker_factory()
        ai_env.settings.max_concurrent_jobs_per_key = 1

        _, _, receipt = await run_encrypted_job(ai_env, agent)
        with pytest.raises(ConcurrencyLimitExceeded):
            await self._place(ai_env)

        await submit_receipt(ai_env, receipt)
        assert await ai_env.store.count_active_jobs(ai_env.account_id) == 0
        # The key works again as soon as it has nothing in flight.
        assert (await self._place(ai_env)).job_id

    @pytest.mark.asyncio
    async def test_the_ttl_sweep_frees_an_abandoned_slot(
        self, ai_env, ai_worker_factory
    ):
        """A client that crashes mid-job must not lock itself out forever."""
        await ai_worker_factory()
        ai_env.settings.max_concurrent_jobs_per_key = 1
        await self._place(ai_env)
        with pytest.raises(ConcurrencyLimitExceeded):
            await self._place(ai_env)

        # Past the TTL: the sweep expires the abandoned job and releases its hold.
        store = ai_env.store
        original_clock = store._clock
        store._clock = lambda: original_clock() + JOB_TTL_SECONDS + 1
        try:
            expired = await store.expire_stale_jobs(JOB_TTL_SECONDS)
            assert len(expired) == 1
        finally:
            store._clock = original_clock

        assert await store.count_active_jobs(ai_env.account_id) == 0
        assert (await self._place(ai_env)).job_id

    @pytest.mark.asyncio
    async def test_a_second_coordinator_sees_the_same_in_flight_count(
        self, ai_env, ai_worker_factory
    ):
        """The cap is shared state, not a per-process counter."""
        from app.ai.service import Coordinator

        await ai_worker_factory()
        ai_env.settings.max_concurrent_jobs_per_key = 1
        await self._place(ai_env)

        other = Coordinator(
            store=ai_env.store, keyring=ai_env.keyring, settings=ai_env.settings
        )
        with pytest.raises(ConcurrencyLimitExceeded):
            await other.place(
                ai_env.auth, PlacementRequest(model=ai_env.model.model_id, max_tokens=4)
            )
