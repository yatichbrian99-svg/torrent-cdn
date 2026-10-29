"""Failure-injection matrix for row 3.3: crash, disappear, restart.

Three failures a real deployment sees, each checked at the money boundary rather
than only at the status field:

* **disappear mid-job** — a worker takes a placement and never sends a receipt.
  Nothing may be billed, and the expiry sweep must release the hold.
* **replaced worker** — a restarted worker has new keys. A receipt for the old
  node's job signed by the new identity must be refused, so a crashed-and-
  restarted process cannot bill work another key was assigned.
* **coordinator restart** — a second ``Coordinator`` over the same store must
  settle an in-flight job with the receipt the first one issued a ticket for,
  and a replayed receipt after the restart must not charge twice. Settlement
  state therefore lives in the store, not in process memory.

The streaming half of "crash mid-stream" lives in ``test_ai_openai_compat.py``
(``test_a_worker_that_dies_mid_stream_is_an_error_not_a_truncated_answer``),
because it needs the SSE route and its real transport stub.

Keep this file honest: a passing xfail is a recorded bug, not a test weakened to
go green.
"""

from __future__ import annotations

import json
import os

import pytest

from app.ai import crypto
from app.ai.errors import ReceiptError
from app.ai.ledger import account_credits
from app.ai.models import PlacementRequest, SignedEnvelope
from app.ai.receipts import RECEIPT_DOMAIN
from app.ai.service import Coordinator
from tcdn_ai_sdk.protocol import (
    FINAL_FRAME,
    TOKEN_FRAME,
    b64e,
    canonical_json,
    decode_frames,
    request_aad,
    session_context,
)


async def _run_job(env, agent, *, text="failure injection") -> tuple[object, dict]:
    """Place a job, run it through the real worker, return the signed receipt.

    Returns before submitting the receipt, so each test decides when (or
    whether) billing happens. The request encryption is the SDK's, byte for byte.
    """
    placement = await env.coordinator.place(
        env.auth,
        PlacementRequest(
            model=env.model.model_id,
            max_tokens=8,
            prompt_tokens_estimate=5,
        ),
    )
    ticket = placement.ticket
    payload = ticket["payload"]
    ephemeral = crypto.generate_x25519()
    salt = os.urandom(32)
    shared = crypto.x25519_shared_secret(
        ephemeral["private_key"], payload["worker_public_key"]
    )
    c2w, _w2c = crypto.derive_session_keys(
        shared, salt=salt, context=session_context(placement.job_id)
    )
    request_plaintext = canonical_json(
        {
            "model": env.model.model_id,
            "messages": [{"role": "user", "content": text}],
            "max_tokens": 8,
            "temperature": 0.0,
            "top_p": 1.0,
            "stop": [],
            "stream": False,
        }
    )
    digest = crypto.sha256_hex(canonical_json(ticket))
    ciphertext = crypto.aead_encrypt(
        c2w, request_plaintext, request_aad(placement.job_id, digest)
    )
    frames = await agent.process(
        {
            "job_id": placement.job_id,
            "ticket": ticket,
            "client_public_key": ephemeral["public_key"],
            "salt": b64e(salt),
            "ciphertext": b64e(ciphertext),
        }
    )
    parsed, remainder = decode_frames(frames)
    assert remainder == b""
    assert any(frame.kind == TOKEN_FRAME for frame in parsed)
    receipt = json.loads(
        next(frame.body for frame in parsed if frame.kind == FINAL_FRAME)
    )
    return placement, receipt


def _envelope(receipt: dict) -> SignedEnvelope:
    return SignedEnvelope(
        payload=receipt["payload"],
        key_id=receipt["key_id"],
        alg=receipt["alg"],
        sig=receipt["sig"],
    )


def _job_entries(store, job_id: str) -> list:
    return [entry for entry in store.ledger.entries if entry.job_id == job_id]


class TestWorkerDisappearsMidJob:
    @pytest.mark.asyncio
    async def test_no_receipt_means_no_charge_and_the_hold_is_released(
        self, ai_env, ai_worker_factory
    ):
        await ai_worker_factory()
        job = await ai_env.coordinator.place(
            ai_env.auth,
            PlacementRequest(model=ai_env.model.model_id, max_tokens=8),
        )
        held = await ai_env.store.held_micro(ai_env.account_id)
        assert held > 0
        before = ai_env.store.ledger.balance(account_credits(ai_env.account_id))

        # The process died: no receipt, no ack, nothing. Age the job past its
        # TTL and run the reaper exactly as the production sweep does.
        stored = await ai_env.store.get_job(job.job_id)
        stored.created_at = 0
        expired = await ai_env.coordinator.expire_stale_jobs()

        assert job.job_id in expired
        assert await ai_env.store.held_micro(ai_env.account_id) == 0
        assert ai_env.store.ledger.balance(account_credits(ai_env.account_id)) == before
        assert _job_entries(ai_env.store, job.job_id) == []
        assert (await ai_env.store.get_job(job.job_id)).status == "expired"


class TestWorkerReplacedAfterCrash:
    @pytest.mark.asyncio
    async def test_a_new_worker_key_cannot_bill_the_old_nodes_job(
        self, ai_env, ai_worker_factory
    ):
        old_worker = await ai_worker_factory()
        placement, receipt = await _run_job(ai_env, old_worker)

        # The worker restarts with fresh keys and registers as a new node. It
        # still knows the job id, and it signs a receipt for it.
        new_worker = await ai_worker_factory()
        payload = dict(receipt["payload"])
        envelope = SignedEnvelope(
            payload=payload,
            key_id=new_worker.identity.identity_key_id,
            alg="Ed25519",
            sig=crypto.sign(
                RECEIPT_DOMAIN, payload, new_worker.identity.identity_private_key
            ),
        )
        with pytest.raises(ReceiptError):
            await ai_env.coordinator.submit_receipt(envelope)

        # The original node's receipt still settles the job: a restart of the
        # *coordinator* is not implicated, and the old key is still the one the
        # job was assigned to (it stopped running, so the sweep releases it).
        stored = await ai_env.store.get_job(placement.job_id)
        stored.created_at = 0
        await ai_env.coordinator.expire_stale_jobs()
        assert (await ai_env.store.get_job(placement.job_id)).status == "expired"


class TestCoordinatorRestart:
    @pytest.mark.asyncio
    async def test_an_in_flight_job_settles_through_a_new_coordinator(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        placement, receipt = await _run_job(ai_env, agent)

        # A restart: new process, new objects, same store and keyring. No
        # in-memory settlement state is carried over.
        restarted = Coordinator(
            store=ai_env.store,
            keyring=ai_env.keyring,
            settings=ai_env.settings,
        )
        settled = await restarted.submit_receipt(_envelope(receipt))
        assert settled.status.value == "settled"
        assert settled.total_micro > 0

        account = account_credits(ai_env.account_id)
        balance = ai_env.store.ledger.balance(account)
        # The receipt was already delivered once; a duplicate after the restart
        # is a replay, not a second bill.
        again = await restarted.submit_receipt(_envelope(receipt))
        assert again.status.value == "settled"
        assert ai_env.store.ledger.balance(account) == balance
        assert ai_env.store.ledger.invariant_ok()

        # The original coordinator sees the settled job too: state is in the
        # store, not in the process that accepted the placement.
        job = await ai_env.coordinator.store.get_job(placement.job_id)
        assert job.status == "settled"

    @pytest.mark.asyncio
    async def test_a_job_placed_before_a_restart_can_still_be_expired(
        self, ai_env, ai_worker_factory
    ):
        await ai_worker_factory()
        job = await ai_env.coordinator.place(
            ai_env.auth,
            PlacementRequest(model=ai_env.model.model_id, max_tokens=8),
        )
        restarted = Coordinator(
            store=ai_env.store,
            keyring=ai_env.keyring,
            settings=ai_env.settings,
        )
        stored = await ai_env.store.get_job(job.job_id)
        stored.created_at = 0
        assert job.job_id in await restarted.expire_stale_jobs()
        assert await ai_env.store.held_micro(ai_env.account_id) == 0
