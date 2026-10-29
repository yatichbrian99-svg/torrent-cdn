"""Free-signup-credit abuse: farming attempts, gates and the audit trail.

Row 4.5 used to be four gates tested against a function nothing called. The
production seam now exists (``app/ai/signup.py``), so the attacks a real
attacker would run are run here against the real coordinator: many addresses
from one network, plus-addressed variants of one inbox, Gmail's ignored dots,
throwaway providers, one browser across many networks, one email domain, and a
script that ignores the daily ceiling. Each refusal must leave no grant row and
no credit, and must be auditable.

Also pinned here: signup credit is a discount on inference, not a withdrawable
balance. The ledger refuses any debit of a customer credit account that is not a
settlement, so a future cash-out route fails closed instead of quietly shipping.
"""

from __future__ import annotations

import pytest

from app.ai.errors import AuthorizationError, ConflictError, ValidationError
from app.ai.ledger import (
    Ledger,
    Split,
    account_credits,
    assert_credit_debit_is_inference,
)
from app.ai.signup_policy import (
    DISPOSABLE_EMAIL_DOMAINS,
    canonical_email,
    device_hash,
    email_domain,
    email_hash,
    ip_hash,
    is_disposable_domain,
)

ACCOUNT_FMT = "{:08d}-0000-4000-8000-000000000000"


def account(index: int) -> str:
    return ACCOUNT_FMT.format(index)


class TestIdentityCanonicalisation:
    """The identity that owns a grant must be the inbox, not the spelling."""

    def test_plus_addressing_collapses_to_one_identity(self):
        assert canonical_email("alice+1@example.com") == "alice@example.com"
        assert canonical_email("alice+promo@Example.COM") == canonical_email(
            "alice@example.com"
        )
        assert email_hash("alice+1@example.com") == email_hash("alice@example.com")

    def test_gmail_ignores_dots_and_googlemail_is_gmail(self):
        assert canonical_email("alice.smith@gmail.com") == "alicesmith@gmail.com"
        assert canonical_email("alicesmith@googlemail.com") == "alicesmith@gmail.com"
        assert email_hash("a.l.i.c.e@gmail.com") == email_hash(
            "alice@googlemail.com"
        )

    def test_dots_are_significant_on_other_providers(self):
        # Only Gmail ignores dots; collapsing them everywhere would merge
        # genuinely different inboxes.
        assert canonical_email("alice.smith@example.com") == "alice.smith@example.com"

    def test_surrounding_whitespace_and_case_are_normalised(self):
        assert canonical_email("  Alice@Example.COM ") == "alice@example.com"

    @pytest.mark.parametrize(
        "value",
        ["", "   ", "not-an-email", "a@@b.com", "@example.com", "alice@", "a@b@c"],
    )
    def test_unusable_addresses_have_no_identity(self, value):
        assert canonical_email(value) == ""
        assert email_hash(value) == ""
        assert email_domain(value) == ""

    def test_the_hash_is_not_reversible_and_is_domain_separated(self):
        hashed = email_hash("alice@example.com")
        assert "alice" not in hashed and "example.com" not in hashed
        assert hashed != ip_hash("alice@example.com")
        assert hashed != device_hash(None)


class TestDisposableInboxBlocking:
    @pytest.mark.parametrize("domain", sorted(DISPOSABLE_EMAIL_DOMAINS)[:12])
    def test_every_bundled_provider_is_blocked(self, domain):
        assert is_disposable_domain(domain)
        assert is_disposable_domain("inbox." + domain), "a subdomain inherits the block"

    def test_an_operator_can_add_a_provider_but_not_remove_one(self):
        assert is_disposable_domain(
            "burner.example", extra_blocked=("burner.example",)
        )
        # The operator list is additive: naming a bundled provider there only
        # repeats the block, it never unblocks it.
        assert is_disposable_domain(
            "mailinator.com", extra_blocked=("mailinator.com",)
        )
        assert is_disposable_domain("mailinator.com", extra_blocked=())

    @pytest.mark.parametrize(
        "domain", ["example.com", "gmail.com", "notmailinator.com", ""]
    )
    def test_ordinary_domains_are_not_blocked(self, domain):
        assert not is_disposable_domain(domain)


async def _verified(store, index: int, email: str) -> str:
    account_id = account(index)
    store.verified_accounts.add(account_id)
    store.account_emails[account_id] = email
    return account_id


def _grants(store) -> int:
    return len(store.signup_grants)


class TestFarmingAttempts:
    @pytest.mark.asyncio
    async def test_many_addresses_from_one_network_stop_at_the_ip_cap(self, ai_env):
        """One IP, four verified addresses, four devices: the fourth is refused."""
        for index in range(3):
            account_id = await _verified(
                ai_env.store, index, f"farmer{index}@example.test"
            )
            granted = await ai_env.coordinator.grant_signup_credits(
                account_id, "198.51.100.7", device_hash=f"device-{index}"
            )
            assert granted == 500_000_000

        fourth = await _verified(ai_env.store, 3, "farmer3@example.test")
        with pytest.raises(ConflictError) as caught:
            await ai_env.coordinator.grant_signup_credits(
                fourth, "198.51.100.7", device_hash="device-3"
            )
        assert caught.value.code == "signup_credit_limit"
        assert _grants(ai_env.store) == 3
        assert ai_env.store.ledger.balance(account_credits(fourth)) == 0

    @pytest.mark.asyncio
    async def test_plus_addressing_cannot_claim_a_second_grant(self, ai_env):
        """Two accounts, one inbox, different networks and devices: one grant."""
        first = await _verified(ai_env.store, 10, "alice@gmail.com")
        second = await _verified(ai_env.store, 11, "alice+promo@gmail.com")

        assert (
            await ai_env.coordinator.grant_signup_credits(
                first, "198.51.100.10", device_hash="device-a"
            )
            == 500_000_000
        )
        with pytest.raises(ConflictError) as caught:
            await ai_env.coordinator.grant_signup_credits(
                second, "198.51.100.11", device_hash="device-b"
            )
        assert caught.value.code == "signup_identity_already_granted"
        assert ai_env.store.ledger.balance(account_credits(second)) == 0
        assert _grants(ai_env.store) == 1

    @pytest.mark.asyncio
    async def test_gmail_dots_cannot_claim_a_second_grant(self, ai_env):
        first = await _verified(ai_env.store, 12, "bob.smith@gmail.com")
        second = await _verified(ai_env.store, 13, "bobsmith@googlemail.com")
        await ai_env.coordinator.grant_signup_credits(
            first, "198.51.100.12", device_hash="device-a"
        )
        with pytest.raises(ConflictError) as caught:
            await ai_env.coordinator.grant_signup_credits(
                second, "198.51.100.13", device_hash="device-b"
            )
        assert caught.value.code == "signup_identity_already_granted"

    @pytest.mark.asyncio
    async def test_disposable_inbox_is_refused(self, ai_env):
        account_id = await _verified(ai_env.store, 14, "throwaway@mailinator.com")
        with pytest.raises(ConflictError) as caught:
            await ai_env.coordinator.grant_signup_credits(
                account_id, "198.51.100.14", device_hash="device-a"
            )
        assert caught.value.code == "signup_disposable_email"
        assert _grants(ai_env.store) == 0
        assert ai_env.store.ledger.balance(account_credits(account_id)) == 0

    @pytest.mark.asyncio
    async def test_one_device_cannot_farm_across_networks(self, ai_env):
        """Four addresses, four IPs, one browser: the device cap is the gate."""
        for index in range(3):
            account_id = await _verified(
                ai_env.store, 20 + index, f"browser{index}@example.test"
            )
            await ai_env.coordinator.grant_signup_credits(
                account_id, f"198.51.100.{20 + index}", device_hash="one-browser"
            )
        fourth = await _verified(ai_env.store, 23, "browser3@example.test")
        with pytest.raises(ConflictError) as caught:
            await ai_env.coordinator.grant_signup_credits(
                fourth, "198.51.100.23", device_hash="one-browser"
            )
        assert caught.value.code == "signup_device_limit"
        assert _grants(ai_env.store) == 3

    @pytest.mark.asyncio
    async def test_one_email_domain_is_rate_limited(self, ai_env, monkeypatch):
        """Many real inboxes on one domain is the classic bulk-farm shape."""
        monkeypatch.setattr(ai_env.settings, "signup_email_domain_cap", 2)
        for index in range(2):
            account_id = await _verified(
                ai_env.store, 30 + index, f"worker{index}@farm.example"
            )
            await ai_env.coordinator.grant_signup_credits(
                account_id, f"198.51.100.{30 + index}", device_hash=f"device-{index}"
            )
        third = await _verified(ai_env.store, 32, "worker2@farm.example")
        with pytest.raises(ConflictError) as caught:
            await ai_env.coordinator.grant_signup_credits(
                third, "198.51.100.32", device_hash="device-2"
            )
        assert caught.value.code == "signup_domain_limit"
        assert _grants(ai_env.store) == 2

    @pytest.mark.asyncio
    async def test_daily_cap_bounds_the_rate_and_resets_the_next_day(
        self, ai_env, monkeypatch
    ):
        monkeypatch.setattr(ai_env.settings, "signup_daily_cap", 1)
        monkeypatch.setattr(ai_env.settings, "signup_ip_cap", 50)
        monkeypatch.setattr(ai_env.settings, "signup_device_cap", 50)
        monkeypatch.setattr(ai_env.settings, "signup_email_domain_cap", 50)
        ai_env.coordinator.clock = lambda: 1_700_000_000

        first = await _verified(ai_env.store, 40, "one@example.test")
        second = await _verified(ai_env.store, 41, "two@example.test")
        await ai_env.coordinator.grant_signup_credits(
            first, "198.51.100.40", device_hash="device-0"
        )
        with pytest.raises(ConflictError) as caught:
            await ai_env.coordinator.grant_signup_credits(
                second, "198.51.100.41", device_hash="device-1"
            )
        assert caught.value.code == "signup_daily_cap"
        assert _grants(ai_env.store) == 1

        # Next UTC day, same deployment: the rate ceiling has reset.
        ai_env.coordinator.clock = lambda: 1_700_000_000 + 86_400
        assert (
            await ai_env.coordinator.grant_signup_credits(
                second, "198.51.100.41", device_hash="device-1"
            )
            == 500_000_000
        )

    @pytest.mark.asyncio
    async def test_production_refuses_an_auto_verified_address(
        self, ai_env, monkeypatch
    ):
        """No email provider in production = no proof of the inbox = no credit."""
        monkeypatch.setattr(ai_env.settings, "environment", "production")
        account_id = await _verified(ai_env.store, 50, "auto@example.test")
        with pytest.raises(AuthorizationError) as caught:
            await ai_env.coordinator.grant_signup_credits(
                account_id,
                "198.51.100.50",
                device_hash="device-0",
                proof="auto",
            )
        assert caught.value.code == "signup_unproven_email"
        assert _grants(ai_env.store) == 0

        # The same account with a signed verification link is eligible.
        assert (
            await ai_env.coordinator.grant_signup_credits(
                account_id, "198.51.100.50", device_hash="device-0", proof="link"
            )
            == 500_000_000
        )

    @pytest.mark.asyncio
    async def test_a_stack_explicitly_marked_staging_can_opt_in(self, ai_env, monkeypatch):
        """Staging has no email provider; the escape is explicit and config-gated."""
        monkeypatch.setattr(ai_env.settings, "environment", "production")
        monkeypatch.setattr(ai_env.settings, "signup_allow_auto_verified", True)
        account_id = await _verified(ai_env.store, 53, "staging@example.test")
        assert (
            await ai_env.coordinator.grant_signup_credits(
                account_id,
                "198.51.100.53",
                device_hash="device-0",
                proof="auto",
            )
            == 500_000_000
        )

    def test_the_staging_escape_is_refused_by_production_config(self, monkeypatch):
        from app.ai.config import AISettings

        monkeypatch.delenv("STAGING_DEPLOYMENT", raising=False)
        production = {
            "environment": "production",
            "signup_allow_auto_verified": True,
            "api_key_pepper": "pepper",
            "coordinator_keyring_json": "{}",
            "allow_simulated_attestation": False,
            "allow_dev_stub_backend": False,
            "payment_provider": "paystack",
        }
        with pytest.raises(ValueError, match="staging-only"):
            AISettings(**production)
        monkeypatch.setenv("STAGING_DEPLOYMENT", "true")
        assert AISettings(**production).signup_allow_auto_verified is True

    @pytest.mark.asyncio
    async def test_an_unknown_proof_is_refused_even_in_development(self, ai_env):
        account_id = await _verified(ai_env.store, 51, "odd@example.test")
        with pytest.raises(AuthorizationError) as caught:
            await ai_env.coordinator.grant_signup_credits(
                account_id, "198.51.100.51", device_hash="device-0", proof="trust-me"
            )
        assert caught.value.code == "signup_unproven_email"

    @pytest.mark.asyncio
    async def test_an_account_without_an_email_identity_is_refused(self, ai_env):
        account_id = account(52)
        ai_env.store.verified_accounts.add(account_id)  # verified, but no address
        with pytest.raises(AuthorizationError) as caught:
            await ai_env.coordinator.grant_signup_credits(
                account_id, "198.51.100.52", device_hash="device-0"
            )
        assert caught.value.code == "signup_identity_missing"
        assert _grants(ai_env.store) == 0


class TestAuditTrail:
    @pytest.mark.asyncio
    async def test_a_grant_is_audited_with_hashes_only(self, ai_env):
        account_id = await _verified(ai_env.store, 60, "audited@example.test")
        await ai_env.coordinator.grant_signup_credits(
            account_id, ip_hash("198.51.100.60"), device_hash="audit-device"
        )
        rows = [
            row
            for row in ai_env.store.audit_log
            if row["event"] == "account.signup_credit"
        ]
        assert len(rows) == 1
        detail = rows[0]["detail"]
        assert detail["micro"] == 500_000_000
        assert detail["email_domain"] == "example.test"
        assert detail["email_hash"] == email_hash("audited@example.test")
        assert detail["ip_hash"] == ip_hash("198.51.100.60")
        assert detail["device_hash"] == "audit-device"
        # No raw personal data in the audit trail.
        blob = repr(detail)
        assert "audited@example.test" not in blob
        assert "198.51.100.60" not in blob

    @pytest.mark.asyncio
    async def test_a_refusal_is_audited_with_its_reason(self, ai_env):
        ai_env.settings.signup_ip_cap = 1
        first = await _verified(ai_env.store, 61, "first@example.test")
        second = await _verified(ai_env.store, 62, "second@example.test")
        await ai_env.coordinator.grant_signup_credits(
            first, "198.51.100.61", device_hash="device-0"
        )
        with pytest.raises(ConflictError):
            await ai_env.coordinator.grant_signup_credits(
                second, "198.51.100.61", device_hash="device-1"
            )
        refusals = [
            row
            for row in ai_env.store.audit_log
            if row["event"] == "account.signup_credit_refused"
        ]
        assert len(refusals) == 1
        assert refusals[0]["detail"]["reason"] == "signup_credit_limit"
        assert refusals[0]["subject"] == second
        # A refusal is not a grant: no row, no credit, no second audit event.
        assert _grants(ai_env.store) == 1
        assert ai_env.store.ledger.balance(account_credits(second)) == 0


class TestSignupCreditIsNotWithdrawable:
    """Free credit is a discount on inference — there is no cash-out path."""

    def test_the_ledger_refuses_a_cash_out_debit(self):
        ledger = Ledger()
        ledger.post(
            debit="platform:signup-grants",
            credit=account_credits("acc"),
            amount_micro=500_000_000,
            idempotency_key="signup:acc",
            kind="signup_credit",
        )
        with pytest.raises(ValidationError) as caught:
            ledger.post(
                debit=account_credits("acc"),
                credit="platform:cash",
                amount_micro=500_000_000,
                idempotency_key="withdraw:acc",
                kind="withdrawal",
            )
        assert caught.value.code == "credit_not_withdrawable"
        assert ledger.balance(account_credits("acc")) == 500_000_000
        assert ledger.invariant_ok()

    def test_a_settlement_is_the_only_way_credit_leaves_the_account(self):
        ledger = Ledger()
        account = account_credits("acc")
        ledger.post(
            debit="platform:signup-grants",
            credit=account,
            amount_micro=500_000_000,
            idempotency_key="signup:acc",
            kind="signup_credit",
        )
        ledger.place_hold(job_id="job-1", account=account, amount_micro=10_000)
        ledger.settle(
            job_id="job-1",
            total_micro=10_000,
            split=Split(),
            compute_account="node:n1:compute",
            seeder_account="pool:seeders",
        )
        assert ledger.balance(account) == 500_000_000 - 10_000
        assert ledger.invariant_ok()
        assert {entry.kind for entry in ledger.entries if entry.debit == account} == {
            "job_compute",
            "job_seeder",
            "job_platform",
        }

    @pytest.mark.parametrize(
        "kind", ["withdrawal", "cash_out", "transfer", "payout", "gift_card"]
    )
    def test_no_other_debit_kind_is_accepted(self, kind):
        with pytest.raises(ValidationError) as caught:
            assert_credit_debit_is_inference(account_credits("acc"), kind)
        assert caught.value.code == "credit_not_withdrawable"
