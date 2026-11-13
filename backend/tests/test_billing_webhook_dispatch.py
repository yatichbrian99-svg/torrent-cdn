"""POST /billing/webhook is the single Paystack URL, so it has to dispatch.

Paystack allows one webhook URL per mode. The AI console therefore cannot have
its own: a credit-pack purchase has to arrive at the same endpoint as the
subscription and transfer events. The router verifies the signature once, then:

  * ``metadata.purpose == "ai_credits"``  -> the AI coordinator's handler, with
    the raw body and the same signature (it re-verifies);
  * a subscription ``charge.success``      -> the plan upgrade, as before;
  * ``transfer.*``                         -> the payout status sync, as before;
  * anything else                          -> acknowledged, no work.

A refusal the coordinator calls final (wrong pack price, a charge that needs
review) is acknowledged with 200 so Paystack stops redelivering it — the
coordinator has already audited it. Anything transient stays a 5xx so the retry
can succeed.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.ai.errors import UnavailableError, ValidationError

USER_ID = "11111111-1111-4111-8111-111111111111"
ACCOUNT = "22222222-2222-4222-8222-222222222222"


def _sign(body: bytes) -> str:
    from app.config import get_settings

    return hmac.new(
        get_settings().paystack_secret_key.encode(), body, hashlib.sha512
    ).hexdigest()


def _post(client, event: dict, *, signature: str | None = None):
    body = json.dumps(event).encode()
    return client.post(
        "/billing/webhook",
        content=body,
        headers={
            "x-paystack-signature": signature or _sign(body),
            "content-type": "application/json",
        },
    )


def _pack_event(reference: str = "tcdnai-dispatch-1") -> dict:
    return {
        "event": "charge.success",
        "data": {
            "id": 4242,
            "status": "success",
            "reference": reference,
            "amount": 65000,
            "currency": "KES",
            "metadata": {
                "purpose": "ai_credits",
                "account_id": ACCOUNT,
                "pack_id": "starter",
                "credit_micro": 5_000_000,
            },
        },
    }


class _StubCoordinator:
    """Records what the router handed it; returns/raises on demand."""

    def __init__(self, *, result: dict | None = None, error: Exception | None = None):
        self.calls: list[tuple[bytes, str]] = []
        self._result = result or {
            "status": "credited",
            "reference": "tcdnai-dispatch-1",
            "amount_micro": 5_000_000,
        }
        self._error = error

    async def handle_payment_webhook(self, body: bytes, signature: str) -> dict:
        self.calls.append((body, signature))
        if self._error is not None:
            raise self._error
        return self._result


@pytest.fixture
def coordinator_override():
    from app.ai import routes as ai_routes

    def _install(coordinator) -> None:
        ai_routes.set_coordinator_for_tests(coordinator)

    yield _install
    ai_routes.set_coordinator_for_tests(None)


class TestAiCreditsRouting:
    def test_pack_event_reaches_the_coordinator_unmodified(
        self, client, mock_conn, coordinator_override
    ):
        coordinator = _StubCoordinator()
        coordinator_override(coordinator)
        event = _pack_event()
        body = json.dumps(event).encode()
        signature = _sign(body)

        response = client.post(
            "/billing/webhook",
            content=body,
            headers={
                "x-paystack-signature": signature,
                "content-type": "application/json",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "credited"
        assert response.json()["amount_micro"] == 5_000_000
        # The exact bytes and signature arrive, so the coordinator's own
        # verification has something real to check.
        assert coordinator.calls == [(body, signature)]
        # It is a credit purchase, not a subscription: no plan write.
        mock_conn.execute.assert_not_called()

    def test_final_refusal_is_acknowledged_not_retried(
        self, client, mock_conn, coordinator_override
    ):
        coordinator = _StubCoordinator(
            error=ValidationError("needs review", code="payment_needs_review")
        )
        coordinator_override(coordinator)
        response = _post(client, _pack_event())
        assert response.status_code == 200
        assert "payment_needs_review" in response.json()["detail"]
        mock_conn.execute.assert_not_called()

    def test_amount_mismatch_is_acknowledged(
        self, client, mock_conn, coordinator_override
    ):
        coordinator = _StubCoordinator(
            error=ValidationError("wrong price", code="payment_amount_mismatch")
        )
        coordinator_override(coordinator)
        response = _post(client, _pack_event())
        assert response.status_code == 200
        mock_conn.execute.assert_not_called()

    def test_transient_failure_asks_paystack_to_retry(
        self, client, mock_conn, coordinator_override
    ):
        coordinator = _StubCoordinator(
            error=UnavailableError("payments paused", code="payments_paused")
        )
        coordinator_override(coordinator)
        response = _post(client, _pack_event())
        assert response.status_code == 500
        mock_conn.execute.assert_not_called()

    def test_unexpected_error_asks_paystack_to_retry(
        self, client, mock_conn, coordinator_override
    ):
        coordinator = _StubCoordinator(error=RuntimeError("boom"))
        coordinator_override(coordinator)
        response = _post(client, _pack_event())
        assert response.status_code == 500, response.text
        mock_conn.execute.assert_not_called()

    def test_bad_signature_never_reaches_the_coordinator(
        self, client, mock_conn, coordinator_override
    ):
        coordinator = _StubCoordinator()
        coordinator_override(coordinator)
        response = _post(client, _pack_event(), signature="0" * 128)
        assert response.status_code == 400
        assert coordinator.calls == []
        mock_conn.execute.assert_not_called()


class TestSubscriptionAndTransferStillWork:
    def test_subscription_charge_upgrades_the_plan(self, client, mock_conn):
        from app.config import get_settings

        event = {
            "event": "charge.success",
            "data": {
                "id": "ch_sub_1",
                "status": "success",
                "plan": {"plan_code": get_settings().paystack_plan_pro},
                "metadata": {"user_id": USER_ID, "plan": "pro"},
            },
        }
        response = _post(client, event)
        assert response.status_code == 200
        assert response.json()["detail"] == "Webhook received"
        mock_conn.execute.assert_awaited()
        sql = mock_conn.execute.await_args.args[0]
        assert "UPDATE users SET plan" in sql

    def test_transfer_event_still_syncs_the_payout(self, client, mock_conn):
        mock_conn.fetchrow.return_value = {
            "amount_cents": 50000,
            "status": "pending",
            "created_by": None,
        }
        event = {
            "event": "transfer.success",
            "data": {"id": 77, "transfer_code": "TRF_1"},
        }
        response = _post(client, event)
        assert response.status_code == 200
        assert response.json()["detail"] == "Webhook received"
        sql = mock_conn.execute.await_args.args[0]
        assert "UPDATE payouts SET status" in sql

    def test_unknown_event_is_acknowledged(self, client, mock_conn):
        response = _post(client, {"event": "customeridentification.success", "data": {"id": 9}})
        assert response.status_code == 200
        assert response.json()["detail"] == "Webhook received"
        mock_conn.execute.assert_not_called()

    def test_one_time_charge_is_still_ignored(self, client, mock_conn):
        event = {
            "event": "charge.success",
            "data": {
                "id": "ch_onetime",
                "status": "success",
                "metadata": {"user_id": USER_ID},
            },
        }
        response = _post(client, event)
        assert response.status_code == 200
        assert "Not a subscription charge" in response.json()["detail"]
        mock_conn.execute.assert_not_called()

    def test_subscription_metadata_without_the_purpose_is_not_routed_to_ai(
        self, client, mock_conn, coordinator_override
    ):
        """Only purpose=ai_credits may take the credit path."""
        from app.config import get_settings

        coordinator = _StubCoordinator()
        coordinator_override(coordinator)
        event = {
            "event": "charge.success",
            "data": {
                "id": "ch_sub_2",
                "status": "success",
                "plan": {"plan_code": get_settings().paystack_plan_pro},
                "metadata": {"user_id": USER_ID, "purpose": "subscription"},
            },
        }
        response = _post(client, event)
        assert response.status_code == 200
        assert coordinator.calls == []
        assert "UPDATE users SET plan" in mock_conn.execute.await_args.args[0]


class TestDisabledConsole:
    def test_unavailable_coordinator_is_acknowledged(self, client, mock_conn, monkeypatch):
        """A deployment without the console must not 500 Paystack forever."""
        from app.ai import routes as ai_routes

        async def _boom():
            raise RuntimeError("no console here")

        monkeypatch.setattr(ai_routes, "get_coordinator", _boom)
        response = _post(client, _pack_event())
        assert response.status_code == 200
        assert "not available" in response.json()["detail"]
        mock_conn.execute.assert_not_called()


def test_final_refusal_set_covers_the_pack_flow_codes():
    """Every deterministic code the pack flow can raise must be classified."""
    from app.routes.billing import _AI_REFUSALS_THAT_ARE_FINAL

    assert {
        "payment_needs_review",
        "payment_currency_mismatch",
        "payment_amount_mismatch",
        "unknown_credit_pack",
        "invalid_payment_event",
    } == set(_AI_REFUSALS_THAT_ARE_FINAL)
