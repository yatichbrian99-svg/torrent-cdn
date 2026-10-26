"""Admin refunds as ledger entries (checklist row 4.6).

The requirement is not "an admin can change a number": it is that money only
moves as a double-entry transaction, with an audit trail, and that a retried
refund cannot pay twice. These tests pin all four.
"""

from __future__ import annotations

import pytest

from app.ai import routes as ai_routes
from app.ai.ledger import PLATFORM_REVENUE, account_credits
from app.main import app

REFUND_URL = "/api/ai/v1/admin/refunds"


def _ai_surface_enabled() -> bool:
    from app.ai.config import get_ai_settings
    from app.config import get_settings

    try:
        return get_ai_settings().is_ai_enabled_for_host(get_settings().app_env)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _ai_surface_enabled(),
    reason="ENABLE_TCDN_AI is off: /api/ai/v1 is not mounted",
)


def _body(ai_env, **overrides) -> dict:
    body = {
        "account_id": ai_env.account_id,
        "amount_micro": 2_000_000,
        "reason": "duplicate charge on ticket 4711",
        "reference": "ticket-4711",
    }
    body.update(overrides)
    return body


class TestAdminRefund:
    def test_refund_is_a_platform_funded_ledger_entry(
        self, ai_env, client, admin_headers, mock_conn
    ):
        # require_admin reads users.is_admin through the app's (mocked) pool.
        mock_conn.fetchrow.return_value = {"is_admin": True}
        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        try:
            before = ai_env.store.ledger.balance(account_credits(ai_env.account_id))
            response = client.post(
                REFUND_URL, json=_body(ai_env), headers=admin_headers
            )
        finally:
            ai_routes.set_coordinator_for_tests(None)
            ai_routes.reset_for_tests()

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "refunded"
        assert body["balance_micro"] == before + 2_000_000

        entries = ai_env.store.ledger.entries
        refunds = [entry for entry in entries if entry.kind == "refund"]
        assert len(refunds) == 1
        entry = refunds[0]
        # Double entry: the platform pays, the customer receives, and the
        # ledger still sums to zero.
        assert entry.debit == PLATFORM_REVENUE
        assert entry.credit == account_credits(ai_env.account_id)
        assert entry.amount_micro == 2_000_000
        assert ai_env.store.ledger.invariant_ok()

    def test_refund_is_audited_with_actor_and_reason(
        self, ai_env, client, admin_headers, mock_conn
    ):
        # require_admin reads users.is_admin through the app's (mocked) pool.
        mock_conn.fetchrow.return_value = {"is_admin": True}
        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        try:
            client.post(REFUND_URL, json=_body(ai_env), headers=admin_headers)
        finally:
            ai_routes.set_coordinator_for_tests(None)
            ai_routes.reset_for_tests()

        audit = [
            row for row in ai_env.store.audit_log if row["event"] == "admin.refund"
        ]
        assert len(audit) == 1
        assert audit[0]["detail"]["micro"] == 2_000_000
        assert "ticket 4711" in audit[0]["detail"]["reason"]
        assert audit[0]["actor"]  # the admin's id, never the customer's

    def test_a_retried_refund_does_not_pay_twice(
        self, ai_env, client, admin_headers, mock_conn
    ):
        # require_admin reads users.is_admin through the app's (mocked) pool.
        mock_conn.fetchrow.return_value = {"is_admin": True}
        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        try:
            first = client.post(REFUND_URL, json=_body(ai_env), headers=admin_headers)
            second = client.post(REFUND_URL, json=_body(ai_env), headers=admin_headers)
        finally:
            ai_routes.set_coordinator_for_tests(None)
            ai_routes.reset_for_tests()

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        refunds = [
            entry for entry in ai_env.store.ledger.entries if entry.kind == "refund"
        ]
        assert len(refunds) == 1
        assert second.json()["balance_micro"] == first.json()["balance_micro"]

    def test_zero_and_negative_refunds_are_rejected(
        self, ai_env, client, admin_headers, mock_conn
    ):
        # require_admin reads users.is_admin through the app's (mocked) pool.
        mock_conn.fetchrow.return_value = {"is_admin": True}
        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        try:
            for amount in (0, -1):
                response = client.post(
                    REFUND_URL,
                    json=_body(ai_env, amount_micro=amount),
                    headers=admin_headers,
                )
                assert response.status_code == 422, (amount, response.text)
        finally:
            ai_routes.set_coordinator_for_tests(None)
            ai_routes.reset_for_tests()
        assert not [
            entry for entry in ai_env.store.ledger.entries if entry.kind == "refund"
        ]

    def test_an_api_key_cannot_issue_a_refund(self, ai_env, client):
        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        try:
            response = client.post(
                REFUND_URL,
                json=_body(ai_env),
                headers={"Authorization": f"Bearer {ai_env.api_key}"},
            )
        finally:
            ai_routes.set_coordinator_for_tests(None)
            ai_routes.reset_for_tests()
        assert response.status_code in (401, 403)
        assert not [
            entry for entry in ai_env.store.ledger.entries if entry.kind == "refund"
        ]
