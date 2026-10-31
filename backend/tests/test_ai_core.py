"""Unit tests: crypto primitives, tickets, ledger arithmetic, pricing, payouts."""

from __future__ import annotations

import hashlib
import random
import threading

import pytest

from app.ai import crypto
from app.ai.errors import (
    AIError,
    ConflictError,
    CryptoError,
    ExpiredError,
    InsufficientCredits,
    NotFoundError,
    ReplayError,
    RevokedKeyError,
    TicketError,
)
from app.ai.keyring import KeyRing
from app.ai.ledger import (
    SEEDER_POOL,
    Ledger,
    Split,
    account_credits,
    compute_earnings,
    split_amounts,
)
from app.ai.models import Confidentiality
from app.ai.payouts import (
    TransferClaim,
    distribute,
    review_claims,
    sign_claim,
    verify_transfer_receipt,
)
from app.ai.pricing import (
    LAUNCH_PRICES_MICRO,
    MICRO_PER_USD,
    ModelPrice,
    PriceTable,
    usd_to_micro,
)
from app.ai.tickets import TICKET_DOMAIN, TicketService

# ── crypto ──────────────────────────────────────────────────────────────────


class TestCrypto:
    def test_sign_verify_and_tamper(self):
        keys = crypto.generate_ed25519()
        payload = {"job_id": "job_1", "amount": 5}
        signature = crypto.sign("test/domain", payload, keys["private_key"])
        assert crypto.verify("test/domain", payload, signature, keys["public_key"])
        assert not crypto.verify("test/domain", {**payload, "amount": 6}, signature, keys["public_key"])
        # Domain separation: the same payload signed for another purpose fails.
        assert not crypto.verify("other/domain", payload, signature, keys["public_key"])

    def test_verify_with_wrong_key_is_rejected(self):
        alice = crypto.generate_ed25519()
        bob = crypto.generate_ed25519()
        signature = crypto.sign("test/domain", {"a": 1}, alice["private_key"])
        assert not crypto.verify("test/domain", {"a": 1}, signature, bob["public_key"])

    def test_hkdf_is_deterministic_and_direction_separated(self):
        secret = b"\x01" * 32
        a1, b1 = crypto.derive_session_keys(secret, salt=b"s" * 32, context="job_1")
        a2, b2 = crypto.derive_session_keys(secret, salt=b"s" * 32, context="job_1")
        assert a1 == a2 and b1 == b2
        assert a1 != b1
        other_a, _ = crypto.derive_session_keys(secret, salt=b"s" * 32, context="job_2")
        assert other_a != a1

    def test_hkdf_requires_salt_and_info(self):
        with pytest.raises(CryptoError):
            crypto.hkdf_sha256(b"secret", salt=b"", info=b"x")
        with pytest.raises(CryptoError):
            crypto.hkdf_sha256(b"secret", salt=b"salt", info=b"")

    def test_aead_roundtrip_and_tamper(self):
        key = hashlib.sha256(b"key").digest()
        aad = b"tcdn-ai/request/v1|job_1|ticket"
        blob = crypto.aead_encrypt(key, b"prompt content", aad)
        assert crypto.aead_decrypt(key, blob, aad) == b"prompt content"
        tampered = bytearray(blob)
        tampered[-1] ^= 0x01
        with pytest.raises(CryptoError):
            crypto.aead_decrypt(key, bytes(tampered), aad)
        with pytest.raises(CryptoError):
            crypto.aead_decrypt(key, blob, aad + b"x")
        other_key = hashlib.sha256(b"other").digest()
        with pytest.raises(CryptoError):
            crypto.aead_decrypt(other_key, blob, aad)

    def test_x25519_rejects_degenerate_peer(self):
        keys = crypto.generate_x25519()
        with pytest.raises(CryptoError):
            crypto.x25519_shared_secret(keys["private_key"], crypto.b64e(b"\x00" * 32))

    def test_api_key_hashing_uses_pepper(self):
        raw, prefix, digest = crypto.new_api_key(b"pepper-a")
        assert raw.startswith(crypto.API_KEY_PREFIX)
        assert prefix == raw[:20]
        assert digest == crypto.hash_api_key(raw, b"pepper-a")
        assert digest != crypto.hash_api_key(raw, b"pepper-b")
        assert raw not in digest

    def test_zeroise_clears_buffer(self):
        buffer = bytearray(b"secret")
        crypto.zeroise(buffer)
        assert buffer == bytearray(len(buffer))


# ── keyring / tickets ───────────────────────────────────────────────────────


def _issue(service: TicketService, ring: KeyRing, **overrides):
    defaults = {
        "job_id": "job_1",
        "account_id": "acc_1",
        "node_id": "node_1",
        "model": "llama-3.1-8b-instruct",
        "worker_public_key": crypto.generate_x25519()["public_key"],
        "confidentiality": Confidentiality.NONE,
        "max_tokens": 16,
        "max_price_micro": 1000,
    }
    defaults.update(overrides)
    return service.issue(**defaults)


class TestTickets:
    def test_roundtrip(self):
        ring = KeyRing.generate("coordinator")
        service = TicketService(ring)
        document, _ = _issue(service, ring)
        verified = service.verify(document, expected_job_id="job_1", expected_node_id="node_1")
        assert verified.job_id == "job_1"
        assert verified.max_tokens == 16

    def test_replay_is_rejected(self):
        ring = KeyRing.generate("coordinator")
        service = TicketService(ring)
        document, _ = _issue(service, ring)
        service.verify(document)
        with pytest.raises(ReplayError):
            service.verify(document)

    def test_forged_ticket_is_rejected(self):
        ring = KeyRing.generate("coordinator")
        service = TicketService(ring)
        document, _ = _issue(service, ring)
        document["payload"]["max_tokens"] = 100_000
        with pytest.raises(CryptoError):
            service.verify(document)

    def test_cross_domain_signature_is_rejected(self):
        ring = KeyRing.generate("coordinator")
        payload = {"job_id": "job_1", "expires_at": 9_999_999_999}
        receipt = ring.sign("tcdn-ai/receipt/v1", payload)
        service = TicketService(ring)
        with pytest.raises(CryptoError):
            service.verify(receipt)
        assert receipt["domain"] != TICKET_DOMAIN

    def test_expiry_is_enforced(self):
        ring = KeyRing.generate("coordinator")
        clock = {"now": 1_000_000}
        service = TicketService(ring, ttl_seconds=10, skew_seconds=0, clock=lambda: clock["now"])
        document, _ = _issue(service, ring)
        clock["now"] += 11
        with pytest.raises(ExpiredError):
            service.verify(document)

    def test_job_and_node_bindings(self):
        ring = KeyRing.generate("coordinator")
        service = TicketService(ring)
        document, _ = _issue(service, ring)
        with pytest.raises(TicketError):
            service.verify(document, expected_job_id="job_2")
        with pytest.raises(TicketError):
            service.verify(document, expected_node_id="node_2")

    def test_tier_binding(self):
        ring = KeyRing.generate("coordinator")
        service = TicketService(ring)
        document, _ = _issue(service, ring)
        with pytest.raises(TicketError):
            service.verify(document, expected_confidentiality=Confidentiality.ATTESTED_TEE)

    def test_rotation_overlap_then_expiry(self):
        clock = {"now": 1_000_000}
        ring = KeyRing.generate("coordinator", overlap_seconds=60, clock=lambda: clock["now"])
        service = TicketService(ring, ttl_seconds=30, skew_seconds=0, clock=lambda: clock["now"])
        document, _ = _issue(service, ring)
        ring.rotate()
        # Old key still verifies an already-issued ticket inside the overlap.
        service.verify(document, now=1_000_020)
        with pytest.raises(CryptoError):
            service.verify(document, now=1_000_100)

    def test_revoked_key_is_rejected(self):
        ring = KeyRing.generate("coordinator")
        service = TicketService(ring)
        document, _ = _issue(service, ring)
        ring.rotate()
        ring.revoke(document["key_id"], "compromise")
        with pytest.raises(RevokedKeyError):
            service.verify(document)

    def test_cannot_revoke_last_signing_key(self):
        ring = KeyRing.generate("coordinator")
        with pytest.raises(CryptoError):
            ring.revoke(ring.active.key_id, "oops")

    def test_unknown_key_id_is_rejected(self):
        ring = KeyRing.generate("coordinator")
        service = TicketService(ring)
        document, _ = _issue(service, ring)
        document["key_id"] = "0" * 16
        with pytest.raises(CryptoError):
            service.verify(document)


# ── ledger ──────────────────────────────────────────────────────────────────


class TestLedger:
    def test_split_always_sums_exactly(self):
        rng = random.Random(1234)
        for _ in range(500):
            total = rng.randrange(0, 10**12)
            a = rng.randrange(0, 101)
            b = rng.randrange(0, 101 - a)
            amounts = split_amounts(total, Split(compute_pct=a, seeder_pct=b, platform_pct=100 - a - b))
            assert amounts.sums_exactly
            assert amounts.platform_micro >= 0

    def test_split_rejects_bad_percentages(self):
        with pytest.raises(ValueError):
            Split(compute_pct=70, seeder_pct=10, platform_pct=19)
        with pytest.raises(ValueError):
            Split(compute_pct=-1, seeder_pct=101, platform_pct=0)

    def test_hold_then_settle_is_exact_and_balanced(self):
        ledger = Ledger()
        account = account_credits("acc")
        ledger.deposit(account=account, amount_micro=1_100, source="gateway:test",
                       idempotency_key="dep")
        ledger.place_hold(job_id="job", account=account, amount_micro=1_000)
        assert ledger.available(account) == 100
        settlement = ledger.settle(
            job_id="job",
            total_micro=1_000,
            split=Split(compute_pct=70, seeder_pct=10, platform_pct=20),
            compute_account=compute_earnings("node"),
            seeder_account=SEEDER_POOL,
        )
        assert settlement.compute_micro + settlement.seeder_micro + settlement.platform_micro == 1_000
        assert ledger.balance(account) == 100
        assert ledger.balance(compute_earnings("node")) == 700
        assert ledger.balance(SEEDER_POOL) == 100
        assert ledger.invariant_ok()

    def test_hold_cannot_exceed_available(self):
        ledger = Ledger()
        account = account_credits("acc")
        ledger.deposit(account=account, amount_micro=50, source="gateway:test",
                       idempotency_key="dep")
        with pytest.raises(InsufficientCredits):
            ledger.place_hold(job_id="job", account=account, amount_micro=51)

    def test_concurrent_holds_cannot_overdraw(self):
        ledger = Ledger()
        account = account_credits("acc")
        ledger.deposit(account=account, amount_micro=100, source="gateway:test",
                       idempotency_key="dep")
        results: list[bool] = []
        lock = threading.Lock()

        def try_hold(index: int) -> None:
            try:
                ledger.place_hold(job_id=f"job-{index}", account=account, amount_micro=30)
                success = True
            except InsufficientCredits:
                success = False
            with lock:
                results.append(success)

        threads = [threading.Thread(target=try_hold, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(results) == 3  # 3 × 30 fits in 100; the 4th would overdraw

    def test_settle_is_idempotent(self):
        ledger = Ledger()
        account = account_credits("acc")
        ledger.deposit(account=account, amount_micro=500, source="gateway:test",
                       idempotency_key="dep")
        ledger.place_hold(job_id="job", account=account, amount_micro=500)
        first = ledger.settle(
            job_id="job", total_micro=400, split=Split(),
            compute_account=compute_earnings("n"), seeder_account=SEEDER_POOL,
        )
        second = ledger.settle(
            job_id="job", total_micro=400, split=Split(),
            compute_account=compute_earnings("n"), seeder_account=SEEDER_POOL,
        )
        assert first == second
        assert ledger.balance(account) == 100
        assert len(ledger.entries) == 4  # deposit + 3 split postings

    def test_idempotency_key_reuse_with_different_amount_conflicts(self):
        ledger = Ledger()
        ledger.post(debit="a", credit="b", amount_micro=10, idempotency_key="k", kind="x")
        with pytest.raises(ConflictError):
            ledger.post(debit="a", credit="b", amount_micro=11, idempotency_key="k", kind="x")

    @pytest.mark.parametrize("bad", [True, False, 1.5, "10", 0, -5])
    def test_invalid_amounts_rejected(self, bad):
        ledger = Ledger()
        with pytest.raises(AIError):
            ledger.post(debit="a", credit="b", amount_micro=bad, idempotency_key="k", kind="x")

    def test_signup_credits_are_platform_funded(self):
        ledger = Ledger()
        ledger.grant_signup_credits(
            account=account_credits("acc"), amount_micro=500_000_000,
            idempotency_key="signup:acc",
        )
        assert ledger.balance(account_credits("acc")) == 500_000_000
        assert ledger.balance("platform:signup-grants") == -500_000_000
        assert ledger.invariant_ok()


# ── pricing ─────────────────────────────────────────────────────────────────


class TestPricing:
    def test_launch_table_matches_prompt(self):
        assert LAUNCH_PRICES_MICRO["small"] == (3_000_000, 6_000_000)
        assert LAUNCH_PRICES_MICRO["70b"] == (10_000_000, 32_000_000)
        assert LAUNCH_PRICES_MICRO["moe"] == (20_000_000, 70_000_000)
        assert usd_to_micro("0.03") == 3_000_000
        assert MICRO_PER_USD == 100_000_000

    def test_cost_uses_ceiling_per_component(self):
        table = PriceTable([ModelPrice("m", 3_000_000, 6_000_000, effective_from=0)])
        quote = table.quote("m", input_tokens=1, output_tokens=1, at=10)
        assert quote.input_micro == 3 and quote.output_micro == 6
        assert quote.total_micro == 9

    def test_confidential_uplift_is_25_percent(self):
        table = PriceTable([ModelPrice("m", 3_000_000, 6_000_000, effective_from=0)])
        quote = table.quote("m", input_tokens=0, output_tokens=1_000_000,
                            confidential=True, uplift_pct=25, at=10)
        assert quote.output_micro == 7_500_000

    def test_effective_dates_and_price_lock(self):
        table = PriceTable(
            [
                ModelPrice("m", 1_000_000, 2_000_000, effective_from=0),
                ModelPrice("m", 5_000_000, 9_000_000, effective_from=1_000),
            ]
        )
        assert table.price_at("m", 500).input_micro_per_mtok == 1_000_000
        assert table.price_at("m", 1000).input_micro_per_mtok == 5_000_000
        locked = table.quote(
            "m", input_tokens=1_000_000, output_tokens=0, at=2_000, price_lock_until=500
        )
        assert locked.input_micro == 1_000_000

    def test_unknown_model_is_not_silently_priced(self):
        table = PriceTable([ModelPrice("m", 1, 1, effective_from=0)])
        with pytest.raises(NotFoundError):
            table.quote("other", input_tokens=1, output_tokens=1, at=0)


# ── payouts ─────────────────────────────────────────────────────────────────


class TestPayouts:
    def test_distribute_sums_exactly(self):
        rng = random.Random(99)
        for _ in range(300):
            seeds = {f"s{i}": rng.randrange(0, 10**9) for i in range(rng.randrange(1, 8))}
            pool = rng.randrange(0, 10**12)
            shares = distribute(pool, seeds)
            assert sum(shares.values()) == pool
            assert all(value >= 0 for value in shares.values())

    def test_requires_cosigned_ticketed_downloader(self):
        alice = crypto.generate_ed25519()
        bob = crypto.generate_ed25519()
        claim = TransferClaim("dl", "seed", "a" * 64, 1000, 1)
        downloader_sig = sign_claim(claim, alice["private_key"])
        seeder_sig = sign_claim(claim, bob["private_key"])
        verify_transfer_receipt(
            claim,
            downloader_signature=downloader_sig,
            seeder_signature=seeder_sig,
            downloader_public_key=alice["public_key"],
            seeder_public_key=bob["public_key"],
        )
        with pytest.raises(ValueError):
            verify_transfer_receipt(
                claim,
                downloader_signature=seeder_sig,  # wrong signer
                seeder_signature=seeder_sig,
                downloader_public_key=alice["public_key"],
                seeder_public_key=bob["public_key"],
            )

    def test_wash_trade_flagged_and_unpaid(self):
        claim = TransferClaim("dl", "seed", "b" * 64, 500, 1)
        reviewed = review_claims(
            [claim],
            ticketed_downloaders={"dl"},
            owner_of={"dl": "same-operator", "seed": "same-operator"},
            pair_cap_bytes=10_000,
        )
        assert reviewed[0].accepted_bytes == 500
        assert "same_owner" in reviewed[0].flags

    def test_unticketed_downloader_earns_nothing(self):
        claim = TransferClaim("dl", "seed", "c" * 64, 500, 1)
        reviewed = review_claims(
            [claim], ticketed_downloaders=set(), owner_of={}, pair_cap_bytes=10_000
        )
        assert reviewed[0].accepted_bytes == 0
        assert "downloader_not_ticketed" in reviewed[0].flags

    def test_pair_cap_and_duplicates(self):
        claims = [
            TransferClaim("dl", "seed", "d" * 64, 80, 1),
            TransferClaim("dl", "seed", "e" * 64, 80, 2),
            TransferClaim("dl", "seed", "d" * 64, 80, 1),
        ]
        reviewed = review_claims(
            claims,
            ticketed_downloaders={"dl"},
            owner_of={"dl": "a", "seed": "b"},
            pair_cap_bytes=100,
        )
        assert reviewed[0].accepted_bytes == 80
        assert reviewed[1].accepted_bytes == 20 and "pair_cap" in reviewed[1].flags
        assert reviewed[2].accepted_bytes == 0 and "duplicate_claim" in reviewed[2].flags
