"""
Payout tests for the Paystack Transfers API (Kenya, M-PESA). Admin-only.

  * POST /billing/payouts/recipients       -> Paystack transferrecipient (mobile_money
                                             / MPESA); recipient_code stored locally.
  * DELETE /billing/payouts/recipients/{c} -> Paystack deactivate + local soft-deactivate.
  * POST /billing/payouts                  -> Paystack transfer (source=balance); amount
                                             converted KES -> cents; transfer stored.
  * GET /billing/payouts/{transfer_code}   -> Paystack transfer/{code}; local status refreshed.
  * GET /billing/payouts/balance           -> Paystack balance for the configured currency.
  * POST /billing/webhook (transfer.*)     -> transfer.success/failed/reversed update the
                                             local payout status + notify the creating admin.

Security gates under test:
  * every payout endpoint requires admin (403 for non-admins)
  * an admin can only pay a recipient that exists (and is active) locally
  * minimum transfer amount enforced BEFORE calling Paystack (KES 3 = 300 cents)
  * transfer webhooks NEVER grant a plan (regression against the free-upgrade bypass)
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import httpx

ADMIN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOW = datetime.now(timezone.utc)

RECIPIENT_ROW = {
    "recipient_code": "RCP_abc",
    "name": "Jane M",
    "account_number": "254712345678",
    "bank_code": "MPESA",
    "bank_name": "M-PESA",
    "type": "mobile_money",
    "currency": "KES",
    "active": True,
    "created_at": NOW,
}

PAYOUT_ROW = {
    "transfer_code": "TRF_123",
    "recipient_code": "RCP_abc",
    "recipient_name": "Jane M",
    "amount_cents": 50000,
    "currency": "KES",
    "reason": "test payout",
    "status": "pending",
    "reference": "ref-1",
    "created_at": NOW,
    "updated_at": NOW,
}


def _sign(payload: bytes, secret: str) -> str:
    """Paystack webhook signature: HMAC-SHA512 hex of the raw body keyed by the
    Paystack SECRET key."""
    return hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()


class TestPayoutsRequireAdmin:
    """Every payout endpoint is admin-only — a regular user gets 403."""

    def test_endpoints_require_admin(self, client, auth_headers, mock_conn):
        # Non-admin: require_admin's fetchrow returns None -> 403.
        mock_conn.fetchrow.return_value = None

        for method, url in [
            ("GET", "/billing/payouts/balance"),
            ("GET", "/billing/payouts"),
            ("GET", "/billing/payouts/recipients"),
            ("POST", "/billing/payouts/recipients"),
            ("DELETE", "/billing/payouts/recipients/RCP_abc"),
            ("POST", "/billing/payouts"),
            ("GET", "/billing/payouts/TRF_123"),
        ]:
            resp = client.request(method, url, json={} if method == "POST" else None, headers=auth_headers)
            assert resp.status_code == 403, f"{method} {url} should be admin-only"


class TestCreateRecipient:
    URL = "/billing/payouts/recipients"

    def _fake_post(self, body):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = json.dumps(body)
        resp.json.return_value = body
        calls = {}

        async def _post(self, *args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs
            return resp

        return _post, calls

    def test_creates_recipient_on_paystack_and_stores(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin
            dict(RECIPIENT_ROW),  # INSERT ... RETURNING *
        ]

        fake_post, calls = self._fake_post({
            "status": True,
            "data": {
                "recipient_code": "RCP_abc",
                "details": {"bank_name": "M-PESA"},
            },
        })
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        resp = client.post(
            self.URL,
            json={"name": "Jane M", "account_number": "254712345678"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["recipient_code"] == "RCP_abc"
        assert body["bank_name"] == "M-PESA"
        assert body["active"] is True

        # The Paystack request must be a mobile_money / MPESA recipient in KES.
        payload = calls["kwargs"]["json"]
        assert payload["type"] == "mobile_money"
        assert payload["bank_code"] == "MPESA"
        assert payload["currency"] == "KES"
        assert payload["account_number"] == "254712345678"

        # Stored locally with the admin as creator (INSERT used fetchrow RETURNING *).
        inserts = [
            c for c in mock_conn.fetchrow.call_args_list
            if "INSERT INTO payout_recipients" in c.args[0]
        ]
        assert len(inserts) == 1
        params = inserts[0].args[1:]
        assert params[0] == "RCP_abc"  # recipient_code
        assert params[-1] == ADMIN_ID  # created_by

    def test_paystack_status_false_returns_502(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.return_value = {"is_admin": True}
        fake_post, _ = self._fake_post({"status": False, "message": "invalid phone"})
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        resp = client.post(
            self.URL,
            json={"name": "Jane M", "account_number": "254712345678"},
            headers=admin_headers,
        )
        assert resp.status_code == 502

    def test_paystack_error_returns_502(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.return_value = {"is_admin": True}

        async def _raise(self, *args, **kwargs):
            raise httpx.ConnectError("paystack down")

        monkeypatch.setattr(httpx.AsyncClient, "post", _raise)
        resp = client.post(
            self.URL,
            json={"name": "Jane M", "account_number": "254712345678"},
            headers=admin_headers,
        )
        assert resp.status_code == 502


class TestInitiateTransfer:
    URL = "/billing/payouts"

    def _fake_post(self, body):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = json.dumps(body)
        resp.json.return_value = body
        calls = {}

        async def _post(self, *args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs
            return resp

        return _post, calls

    def test_initiate_converts_kes_to_cents(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin
            {"recipient_code": "RCP_abc", "name": "Jane M", "active": True},  # recipient lookup
            dict(PAYOUT_ROW),  # INSERT ... RETURNING *
        ]

        fake_post, calls = self._fake_post({
            "status": True,
            "data": {"transfer_code": "TRF_123", "status": "pending", "reference": "ref-1"},
        })
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        resp = client.post(
            self.URL,
            json={"recipient_code": "RCP_abc", "amount": 500, "reason": "test payout"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["transfer_code"] == "TRF_123"
        assert body["amount_cents"] == 50000  # KES 500 -> cents
        assert body["amount_kes"] == 500.0
        assert body["status"] == "pending"

        # The Paystack request carries the smallest-unit amount and source=balance.
        payload = calls["kwargs"]["json"]
        assert payload == {
            "source": "balance",
            "amount": 50000,
            "recipient": "RCP_abc",
            "reason": "test payout",
            "currency": "KES",
        }

    def test_minimum_amount_enforced_before_paystack(self, client, admin_headers, mock_conn, monkeypatch):
        """KES 2 is below Paystack's 300-cent floor — rejected before any call."""
        mock_conn.fetchrow.return_value = {"is_admin": True}

        def _boom(self, *args, **kwargs):
            raise AssertionError("Paystack must not be called for a below-minimum transfer")

        monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
        resp = client.post(
            self.URL,
            json={"recipient_code": "RCP_abc", "amount": 2},
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert "Minimum payout" in resp.json()["detail"]

    def test_unknown_recipient_rejected(self, client, admin_headers, mock_conn, monkeypatch):
        """Only recipients created (and stored) locally can be paid."""
        mock_conn.fetchrow.side_effect = [{"is_admin": True}, None]

        def _boom(self, *args, **kwargs):
            raise AssertionError("Paystack must not be called for an unknown recipient")

        monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
        resp = client.post(
            self.URL,
            json={"recipient_code": "RCP_nope", "amount": 500},
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert "Recipient not found" in resp.json()["detail"]

    def test_inactive_recipient_rejected(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},
            {"recipient_code": "RCP_abc", "name": "Jane M", "active": False},
        ]
        resp = client.post(
            self.URL,
            json={"recipient_code": "RCP_abc", "amount": 500},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_paystack_error_returns_502(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},
            {"recipient_code": "RCP_abc", "name": "Jane M", "active": True},
        ]

        async def _raise(self, *args, **kwargs):
            raise httpx.ConnectError("paystack down")

        monkeypatch.setattr(httpx.AsyncClient, "post", _raise)
        resp = client.post(
            self.URL,
            json={"recipient_code": "RCP_abc", "amount": 500},
            headers=admin_headers,
        )
        assert resp.status_code == 502

    def test_paystack_status_false_returns_502(self, client, admin_headers, mock_conn, monkeypatch):
        """Paystack can answer HTTP 200 with status=false (e.g. insufficient
        balance) — that must surface as an error, never as a stored payout."""
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},
            {"recipient_code": "RCP_abc", "name": "Jane M", "active": True},
        ]
        fake_post, _ = self._fake_post({"status": False, "message": "insufficient balance"})
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        resp = client.post(
            self.URL,
            json={"recipient_code": "RCP_abc", "amount": 500},
            headers=admin_headers,
        )
        assert resp.status_code == 502


class TestFetchPayout:
    URL = "/billing/payouts/TRF_123"

    def _fake_get(self, body):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = json.dumps(body)
        resp.json.return_value = body

        async def _get(self, *args, **kwargs):
            return resp

        return _get

    def test_fetch_refreshes_local_status(self, client, admin_headers, mock_conn, monkeypatch):
        updated = dict(PAYOUT_ROW, status="success")
        mock_conn.fetchrow.side_effect = [
            {"is_admin": True},  # require_admin
            dict(PAYOUT_ROW),    # SELECT payouts
            updated,             # SELECT again after UPDATE
        ]
        fake_get = self._fake_get({"status": True, "data": {"status": "success"}})
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        resp = client.get(self.URL, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        # The local row was updated to match Paystack's authoritative status.
        updates = [
            c for c in mock_conn.execute.call_args_list
            if "UPDATE payouts SET status" in c.args[0]
        ]
        assert len(updates) == 1
        assert updates[0].args[1] == "success"

    def test_fetch_unknown_local_transfer_404(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.side_effect = [{"is_admin": True}, None]
        fake_get = self._fake_get({"status": True, "data": {"status": "success"}})
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        resp = client.get(self.URL, headers=admin_headers)
        assert resp.status_code == 404

    def test_fetch_paystack_error_502(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.return_value = {"is_admin": True}

        async def _raise(self, *args, **kwargs):
            raise httpx.ConnectError("paystack down")

        monkeypatch.setattr(httpx.AsyncClient, "get", _raise)
        resp = client.get(self.URL, headers=admin_headers)
        assert resp.status_code == 502


class TestBalance:
    def test_returns_configured_currency_balance(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.return_value = {"is_admin": True}
        resp_obj = MagicMock()
        resp_obj.status_code = 200
        resp_obj.text = json.dumps({"status": True, "data": [
            {"currency": "NGN", "balance": 1},
            {"currency": "KES", "balance": 150000},
        ]})
        resp_obj.json.return_value = {"status": True, "data": [
            {"currency": "NGN", "balance": 1},
            {"currency": "KES", "balance": 150000},
        ]}

        async def _get(self, *args, **kwargs):
            return resp_obj

        monkeypatch.setattr(httpx.AsyncClient, "get", _get)
        resp = client.get("/billing/payouts/balance", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["currency"] == "KES"
        assert body["balance_cents"] == 150000
        assert body["balance_kes"] == 1500.0

    def test_missing_currency_returns_502(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.return_value = {"is_admin": True}
        resp_obj = MagicMock()
        resp_obj.status_code = 200
        resp_obj.json.return_value = {"status": True, "data": [{"currency": "NGN", "balance": 1}]}

        async def _get(self, *args, **kwargs):
            return resp_obj

        monkeypatch.setattr(httpx.AsyncClient, "get", _get)
        resp = client.get("/billing/payouts/balance", headers=admin_headers)
        assert resp.status_code == 502


class TestDeactivateRecipient:
    URL = "/billing/payouts/recipients/RCP_abc"

    def _fake_delete(self, body):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = json.dumps(body)
        resp.json.return_value = body
        calls = {}

        async def _delete(self, *args, **kwargs):
            calls["args"] = args
            return resp

        return _delete, calls

    def test_deactivates_locally_and_on_paystack(self, client, admin_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 1  # recipient exists locally
        mock_conn.execute.return_value = "UPDATE 1"
        fake_delete, calls = self._fake_delete({"status": True, "message": "Transfer recipient deleted"})
        monkeypatch.setattr(httpx.AsyncClient, "delete", fake_delete)

        resp = client.delete(self.URL, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Recipient deactivated"

        # Paystack was told to deactivate the same code we hold locally.
        assert "RCP_abc" in str(calls["args"])
        updates = [
            c for c in mock_conn.execute.call_args_list
            if "UPDATE payout_recipients SET active = FALSE" in c.args[0]
        ]
        assert len(updates) == 1

    def test_unknown_recipient_404_before_paystack(self, client, admin_headers, mock_conn, monkeypatch):
        """The local row is checked FIRST — an unknown recipient must 404
        without ever calling Paystack (which would deactivate a recipient we
        don't know about)."""
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetchval.return_value = 0  # not in the local table

        def _boom(self, *args, **kwargs):
            raise AssertionError("Paystack must not be called for an unknown recipient")

        monkeypatch.setattr(httpx.AsyncClient, "delete", _boom)
        resp = client.delete(self.URL, headers=admin_headers)
        assert resp.status_code == 404


class TestListEndpoints:
    def test_list_payouts(self, client, admin_headers, mock_conn):
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetch.return_value = [dict(PAYOUT_ROW)]
        resp = client.get("/billing/payouts", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["amount_cents"] == 50000
        assert body[0]["amount_kes"] == 500.0

    def test_list_recipients(self, client, admin_headers, mock_conn):
        mock_conn.fetchrow.return_value = {"is_admin": True}
        mock_conn.fetch.return_value = [dict(RECIPIENT_ROW)]
        resp = client.get("/billing/payouts/recipients", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["recipient_code"] == "RCP_abc"


class TestTransferWebhooks:
    """Paystack transfer.* events update the local payout and notify the admin —
    and never grant a plan."""

    URL = "/billing/webhook"

    def _post(self, client, event_dict):
        from app.config import get_settings
        secret = get_settings().paystack_secret_key
        body = json.dumps(event_dict).encode()
        return client.post(
            self.URL,
            content=body,
            headers={
                "x-paystack-signature": _sign(body, secret),
                "content-type": "application/json",
            },
        )

    def _transfer_event(self, event, transfer_code="TRF_123"):
        return {"event": event, "data": {"id": 9001, "transfer_code": transfer_code}}

    def test_transfer_success_updates_status_and_notifies(self, client, mock_conn):
        mock_conn.fetchrow.return_value = {"amount_cents": 50000, "created_by": ADMIN_ID}
        resp = self._post(client, self._transfer_event("transfer.success"))
        assert resp.status_code == 200

        updates = [
            c for c in mock_conn.execute.call_args_list
            if "UPDATE payouts SET status" in c.args[0]
        ]
        assert len(updates) == 1
        assert updates[0].args[1] == "success"
        assert updates[0].args[2] == "TRF_123"

        inserts = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO notifications" in c.args[0]
        ]
        assert len(inserts) == 1
        assert inserts[0].args[1] == ADMIN_ID  # user_id
        assert "KES 500" in inserts[0].args[2]  # message

    def test_transfer_failed_updates_status(self, client, mock_conn):
        mock_conn.fetchrow.return_value = {"amount_cents": 50000, "created_by": ADMIN_ID}
        resp = self._post(client, self._transfer_event("transfer.failed"))
        assert resp.status_code == 200
        updates = [
            c for c in mock_conn.execute.call_args_list
            if "UPDATE payouts SET status" in c.args[0]
        ]
        assert updates[0].args[1] == "failed"

    def test_transfer_reversed_updates_status(self, client, mock_conn):
        mock_conn.fetchrow.return_value = {"amount_cents": 50000, "created_by": ADMIN_ID}
        resp = self._post(client, self._transfer_event("transfer.reversed"))
        assert resp.status_code == 200
        updates = [
            c for c in mock_conn.execute.call_args_list
            if "UPDATE payouts SET status" in c.args[0]
        ]
        assert updates[0].args[1] == "reversed"

    def test_transfer_event_never_grants_plan(self, client, mock_conn):
        """Regression: a transfer.success must NOT run the charge-success plan
        grant — and with no creating admin there is no notification either."""
        mock_conn.fetchrow.return_value = {"amount_cents": 50000, "created_by": None}
        resp = self._post(client, self._transfer_event("transfer.success"))
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Webhook received"

        user_updates = [
            c for c in mock_conn.execute.call_args_list
            if "UPDATE users SET plan" in c.args[0]
        ]
        assert not user_updates, "transfer events must never upgrade a plan"
        notification_inserts = [
            c for c in mock_conn.execute.call_args_list
            if "INSERT INTO notifications" in c.args[0]
        ]
        assert not notification_inserts, "no admin to notify (created_by is NULL)"

    def test_unknown_transfer_ignored(self, client, mock_conn):
        mock_conn.fetchrow.return_value = None  # no local payout row
        resp = self._post(client, self._transfer_event("transfer.success", "TRF_nope"))
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Unknown transfer, ignoring"
        mock_conn.execute.assert_not_called()

    def test_missing_transfer_code_ignored(self, client, mock_conn):
        resp = self._post(client, {"event": "transfer.success", "data": {"id": 9002}})
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Missing transfer_code, ignoring"
        mock_conn.execute.assert_not_called()

    def test_bad_signature_rejected(self, client, mock_conn):
        resp = client.post(
            self.URL,
            content=b"{}",
            headers={"x-paystack-signature": "bogus"},
        )
        assert resp.status_code == 400
        mock_conn.execute.assert_not_called()
