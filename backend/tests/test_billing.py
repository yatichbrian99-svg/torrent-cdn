"""
Billing tests for the Paystack (Kenya) integration.

Paystack is used because Stripe does not support merchant accounts registered
in Kenya. The integration keeps the same API surface as the old Stripe one:

  * POST /billing/create-checkout -> Paystack transaction/initialize against a
    recurring plan; returns the hosted authorization_url.
  * POST /billing/webhook -> HMAC-SHA512 signed (x-paystack-signature); only a
    SUCCESSFUL subscription charge (charge.success with a `plan` object whose
    plan_code maps to a configured plan) upgrades the user's plan.

Security gates under test (regression for the free-upgrade bypass):
  * failed charge  -> no plan granted
  * one-time charge (no `plan`) -> no plan granted
  * unknown plan_code -> no plan granted
  * malformed/missing user_id metadata -> ignored, never 500s (Paystack retries)
  * bad signature -> 400
  * duplicate event -> idempotent, no double-write
"""
import hashlib
import hmac
import json
from unittest.mock import MagicMock
from uuid import UUID

import httpx

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def _sign(payload: bytes, secret: str) -> str:
    """Paystack webhook signature: HMAC-SHA512 hex of the raw body keyed by the
    Paystack SECRET key."""
    return hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()


class TestCreateCheckout:
    """POST /billing/create-checkout proxies to Paystack and returns the hosted
    authorization_url."""

    URL = "/billing/create-checkout"

    def _fake_post(self, body):
        """Async stand-in for httpx.AsyncClient.post returning a Paystack-style
        success response. The response object is a plain MagicMock because
        httpx responses are synchronous (.json(), .text, .status_code)."""
        resp = MagicMock()
        resp.status_code = 200
        resp.text = json.dumps(body)
        resp.json.return_value = body

        calls = {}

        async def _post(self, *args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs
            return resp

        return _post, resp, calls

    def test_invalid_plan_rejected(self, client, auth_headers, mock_conn):
        resp = client.post(self.URL, json={"plan": "platinum"}, headers=auth_headers)
        assert resp.status_code == 400

    def test_free_plan_rejected(self, client, auth_headers, mock_conn):
        """The free tier has no Paystack plan — it must not start a checkout."""
        resp = client.post(self.URL, json={"plan": "free"}, headers=auth_headers)
        assert resp.status_code == 400

    def test_checkout_returns_paystack_url(self, client, auth_headers, mock_conn, monkeypatch):
        from app.config import get_settings
        settings = get_settings()
        mock_conn.fetchrow.return_value = {"email": "user@example.com"}

        fake_post, _, calls = self._fake_post({
            "status": True,
            "data": {"authorization_url": "https://checkout.paystack.com/xyz"},
        })
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        resp = client.post(self.URL, json={"plan": "pro"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["url"] == "https://checkout.paystack.com/xyz"

        # The initialize body must target the configured plan code and carry
        # the user_id + plan metadata the webhook later trusts.
        body = calls["kwargs"]["json"]
        assert body["email"] == "user@example.com"
        assert body["plan"] == settings.paystack_plan_pro
        assert body["amount"] == settings.paystack_amount_pro
        assert body["currency"] == settings.paystack_currency
        assert body["metadata"] == {"user_id": str(USER_ID), "plan": "pro"}
        assert body["callback_url"].endswith("/billing?success=true")

    def test_paystack_error_returns_502(self, client, auth_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.return_value = {"email": "user@example.com"}

        async def _raise(self, *args, **kwargs):
            raise httpx.ConnectError("paystack down")

        monkeypatch.setattr(httpx.AsyncClient, "post", _raise)
        resp = client.post(self.URL, json={"plan": "pro"}, headers=auth_headers)
        assert resp.status_code == 502

    def test_paystack_status_false_returns_502(self, client, auth_headers, mock_conn, monkeypatch):
        mock_conn.fetchrow.return_value = {"email": "user@example.com"}
        fake_post, _, _ = self._fake_post({"status": False, "message": "nope"})
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        resp = client.post(self.URL, json={"plan": "pro"}, headers=auth_headers)
        assert resp.status_code == 502


class TestPaystackWebhookPaymentGate:
    """The billing webhook must only grant a plan when a subscription charge
    actually succeeded — a failed charge or a one-time payment is a free-upgrade
    bypass."""

    URL = "/billing/webhook"

    def _post(self, client, event_dict, secret=None):
        from app.config import get_settings
        secret = secret or get_settings().paystack_secret_key
        body = json.dumps(event_dict).encode()
        return client.post(
            self.URL,
            content=body,
            headers={
                "x-paystack-signature": _sign(body, secret),
                "content-type": "application/json",
            },
        )

    def test_bad_signature_rejected(self, client, mock_conn):
        resp = client.post(
            self.URL,
            content=b"{}",
            headers={"x-paystack-signature": "bogus"},
        )
        assert resp.status_code == 400
        assert "Invalid webhook signature" in resp.json()["detail"]
        mock_conn.execute.assert_not_called()

    def test_failed_charge_does_not_grant_plan(self, client, mock_conn):
        resp = self._post(client, {
            "event": "charge.success",
            "data": {
                "id": "ch_failed",
                "status": "failed",
                "plan": {"plan_code": "PLN_pro"},
                "metadata": {"user_id": str(USER_ID), "plan": "pro"},
            },
        })
        assert resp.status_code == 200
        assert "not successful" in resp.json()["detail"]
        mock_conn.execute.assert_not_called()  # plan NOT upgraded

    def test_one_time_charge_ignored(self, client, mock_conn):
        """A one-time charge has no `plan` object — not a subscription."""
        resp = self._post(client, {
            "event": "charge.success",
            "data": {
                "id": "ch_onetime",
                "status": "success",
                "metadata": {"user_id": str(USER_ID)},
            },
        })
        assert resp.status_code == 200
        assert "Not a subscription charge" in resp.json()["detail"]
        mock_conn.execute.assert_not_called()

    def test_unknown_plan_code_ignored(self, client, mock_conn):
        resp = self._post(client, {
            "event": "charge.success",
            "data": {
                "id": "ch_unknown",
                "status": "success",
                "plan": {"plan_code": "PLN_not_configured"},
                "metadata": {"user_id": str(USER_ID)},
            },
        })
        assert resp.status_code == 200
        assert "Not a subscription charge" in resp.json()["detail"]
        mock_conn.execute.assert_not_called()

    def test_missing_user_id_metadata_ignored(self, client, mock_conn):
        from app.config import get_settings
        resp = self._post(client, {
            "event": "charge.success",
            "data": {
                "id": "ch_nouser",
                "status": "success",
                "plan": {"plan_code": get_settings().paystack_plan_pro},
            },
        })
        assert resp.status_code == 200
        assert "user_id" in resp.json()["detail"].lower()
        mock_conn.execute.assert_not_called()

    def test_malformed_user_id_metadata_ignored(self, client, mock_conn):
        """A bad user_id in metadata must not 500 the webhook (Paystack retries)."""
        from app.config import get_settings
        resp = self._post(client, {
            "event": "charge.success",
            "data": {
                "id": "ch_bad",
                "status": "success",
                "plan": {"plan_code": get_settings().paystack_plan_pro},
                "metadata": {"user_id": "not-a-uuid", "plan": "pro"},
            },
        })
        assert resp.status_code == 200
        assert "invalid user_id" in resp.json()["detail"].lower()
        mock_conn.execute.assert_not_called()

    def test_successful_subscription_charge_grants_plan(self, client, mock_conn):
        """A settled subscription charge upgrades the plan — the plan_code from
        Paystack's own charge object (not client metadata) is authoritative."""
        from app.config import get_settings
        settings = get_settings()
        resp = self._post(client, {
            "event": "charge.success",
            "data": {
                "id": "ch_ok",
                "status": "success",
                "plan": {"plan_code": settings.paystack_plan_pro},
                "metadata": {"user_id": str(USER_ID), "plan": "pro"},
            },
        })
        assert resp.status_code == 200
        mock_conn.execute.assert_called_once()
        args = mock_conn.execute.call_args.args
        assert args[1] == "pro"
        assert args[2] == USER_ID

    def test_non_charge_event_ignored(self, client, mock_conn):
        """Other Paystack events (invoice.create, subscription.create, ...)
        must not grant plans — only a successful charge does."""
        resp = self._post(client, {
            "event": "invoice.create",
            "data": {
                "id": "inv_1",
                "status": "success",
                "plan": {"plan_code": "PLN_pro"},
                "metadata": {"user_id": str(USER_ID), "plan": "pro"},
            },
        })
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Webhook received"
        mock_conn.execute.assert_not_called()

    def test_malformed_json_rejected(self, client, mock_conn):
        """A well-signed but non-JSON body is a 400, not a 500."""
        from app.config import get_settings
        body = b"not json at all"
        resp = client.post(
            self.URL,
            content=body,
            headers={
                "x-paystack-signature": _sign(body, get_settings().paystack_secret_key),
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 400
        assert "Invalid JSON payload" in resp.json()["detail"]
        mock_conn.execute.assert_not_called()

    def test_plan_code_is_authoritative_over_metadata(self, client, mock_conn):
        """The granted tier comes from Paystack's own charge.plan.plan_code,
        never from the (backend-set, but informational) metadata.plan."""
        from app.config import get_settings
        settings = get_settings()
        resp = self._post(client, {
            "event": "charge.success",
            "data": {
                "id": "ch_auth",
                "status": "success",
                "plan": {"plan_code": settings.paystack_plan_pro},
                # metadata says business — must be ignored
                "metadata": {"user_id": str(USER_ID), "plan": "business"},
            },
        })
        assert resp.status_code == 200
        mock_conn.execute.assert_called_once()
        assert mock_conn.execute.call_args.args[1] == "pro"

    def test_duplicate_event_is_idempotent(self, client, mock_conn, mock_redis):
        """Redis marks an event id as processed for 24h — a retried delivery of
        the same charge must not re-run (or double-write)."""
        from app.config import get_settings
        mock_redis.get.return_value = "1"  # already processed
        resp = self._post(client, {
            "event": "charge.success",
            "data": {
                "id": "ch_dup",
                "status": "success",
                "plan": {"plan_code": get_settings().paystack_plan_pro},
                "metadata": {"user_id": str(USER_ID), "plan": "pro"},
            },
        })
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Already processed"
        mock_conn.execute.assert_not_called()
