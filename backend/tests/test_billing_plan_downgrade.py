"""Plan downgrades: a lapsed subscription must eventually fall back to free.

The webhook only ever upgraded a plan. Paystack's cancellation / non-renewal /
expiry / failed-renewal events changed nothing, so a customer kept the paid
limits indefinitely after they stopped paying.

The rule under test: the paid plan is kept until the end of the period it paid
for (``plan_expires_at``), then the account falls back to ``pending_plan`` —
on the first read after that instant, with a durable billing audit row, an
in-app notification and an email. A missing or unparseable period means
"effective now", never "keep the plan forever".
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.plan_lifecycle import DOWNGRADE_TARGET, parse_period_end

USER_ID = "11111111-1111-4111-8111-111111111111"
USER_EMAIL = "subscriber@example.test"


def _sign(body: bytes) -> str:
    from app.config import get_settings

    return hmac.new(
        get_settings().paystack_secret_key.encode(), body, hashlib.sha512
    ).hexdigest()


def _post(client, event: dict):
    body = json.dumps(event).encode()
    return client.post(
        "/billing/webhook",
        content=body,
        headers={
            "x-paystack-signature": _sign(body),
            "content-type": "application/json",
        },
    )


def _events_sql(mock_conn) -> list[str]:
    """Every statement the handler executed, in order."""
    return [call.args[0] for call in mock_conn.execute.await_args_list]


def _event(kind: str, *, period_end: str | None = None, by_email: bool = False) -> dict:
    data: dict = {"id": 1001, "subscription_code": "SUB_1"}
    if period_end:
        data["next_payment_date"] = period_end
    if by_email:
        data["customer"] = {"email": USER_EMAIL}
    else:
        data["metadata"] = {"user_id": USER_ID}
    return {"event": kind, "data": data}


@pytest.fixture
def downgrade_notice(monkeypatch):
    """Capture the email call instead of sending one."""
    from app.routes import billing as billing_routes

    sent: list[dict] = []

    async def _record(email, *, from_plan, to_plan):
        sent.append({"email": email, "from_plan": from_plan, "to_plan": to_plan})
        return True

    monkeypatch.setattr(billing_routes, "send_plan_downgrade_email", _record)
    return sent


class TestScheduling:
    @pytest.mark.parametrize(
        "kind",
        [
            "subscription.disable",
            "subscription.not_renew",
            "invoice.payment_failed",
            "subscription.expiry",
        ],
    )
    def test_each_event_schedules_a_downgrade(
        self, client, mock_conn, downgrade_notice, kind
    ):
        future = (datetime.now(UTC) + timedelta(days=12)).isoformat()
        mock_conn.fetchrow.side_effect = [
            {"id": USER_ID, "email": USER_EMAIL},  # resolve the subscriber
            {  # state seen by apply_due_downgrade: not due yet
                "email": USER_EMAIL,
                "plan": "pro",
                "pending_plan": DOWNGRADE_TARGET,
                "plan_expires_at": datetime.now(UTC) + timedelta(days=12),
            },
        ]
        response = _post(client, _event(kind, period_end=future))

        assert response.status_code == 200, response.text
        assert response.json()["detail"] == "Webhook received"
        sql = _events_sql(mock_conn)
        assert any("pending_plan = $2" in statement for statement in sql)
        # Still paid until the period ends: the plan itself is untouched.
        assert not any("SET plan = $2" in statement for statement in sql)
        assert any("billing_audit_log" in statement for statement in sql)
        assert downgrade_notice == []  # nothing to tell the customer yet

    def test_audit_row_records_reason_and_effective_date(self, client, mock_conn):
        future = datetime.now(UTC) + timedelta(days=3)
        mock_conn.fetchrow.side_effect = [
            {"id": USER_ID, "email": USER_EMAIL},
            {
                "email": USER_EMAIL,
                "plan": "business",
                "pending_plan": DOWNGRADE_TARGET,
                "plan_expires_at": future,
            },
        ]
        _post(client, _event("subscription.not_renew", period_end=future.isoformat()))
        insert = next(
            call
            for call in mock_conn.execute.await_args_list
            if "billing_audit_log" in call.args[0]
        )
        assert str(insert.args[1]) == USER_ID
        assert insert.args[2] == "billing.plan_downgrade_scheduled"
        detail = json.loads(insert.args[3])
        assert detail["reason"] == "subscription.not_renew"
        assert detail["target_plan"] == "free"

    def test_subscriber_resolved_by_email_when_metadata_is_absent(
        self, client, mock_conn
    ):
        """Subscriptions outlive the checkout metadata; Paystack sends the email."""
        mock_conn.fetchrow.side_effect = [
            {"id": USER_ID, "email": USER_EMAIL},
            {
                "email": USER_EMAIL,
                "plan": "pro",
                "pending_plan": DOWNGRADE_TARGET,
                "plan_expires_at": datetime.now(UTC) + timedelta(days=1),
            },
        ]
        response = _post(
            client,
            _event("subscription.disable", by_email=True),
        )
        assert response.status_code == 200
        lookup = mock_conn.fetchrow.await_args_list[0]
        assert "lower(email) = lower($1)" in lookup.args[0]

    def test_unknown_subscriber_is_ignored(self, client, mock_conn):
        mock_conn.fetchrow.return_value = None
        response = _post(client, _event("subscription.disable"))
        assert response.status_code == 200
        assert response.json()["detail"] == "Unknown subscriber, ignoring"
        assert not any("pending_plan" in s for s in _events_sql(mock_conn))


class TestApplying:
    def test_period_already_ended_downgrades_immediately(
        self, client, mock_conn, downgrade_notice
    ):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        mock_conn.fetchrow.side_effect = [
            {"id": USER_ID, "email": USER_EMAIL},
            {  # the row apply_due_downgrade reads: expired
                "email": USER_EMAIL,
                "plan": "pro",
                "pending_plan": DOWNGRADE_TARGET,
                "plan_expires_at": datetime.now(UTC) - timedelta(days=1),
            },
        ]
        response = _post(client, _event("subscription.expiry", period_end=past))

        assert response.status_code == 200
        sql = _events_sql(mock_conn)
        downgrade = next(s for s in sql if "SET plan = $2, pending_plan = NULL" in s)
        assert downgrade
        assert any("billing.plan_downgraded" in str(call.args[2]) for call in
                   mock_conn.execute.await_args_list if "billing_audit_log" in call.args[0])
        assert any("INSERT INTO notifications" in s for s in sql)
        assert downgrade_notice == [
            {"email": USER_EMAIL, "from_plan": "pro", "to_plan": "free"}
        ]

    def test_missing_period_end_means_now(self, client, mock_conn, downgrade_notice):
        mock_conn.fetchrow.side_effect = [
            {"id": USER_ID, "email": USER_EMAIL},
            {
                "email": USER_EMAIL,
                "plan": "business",
                "pending_plan": DOWNGRADE_TARGET,
                "plan_expires_at": datetime.now(UTC) - timedelta(seconds=1),
            },
        ]
        response = _post(client, _event("subscription.disable"))
        assert response.status_code == 200
        # Effective "now", so the very same request applies it.
        assert any("SET plan = $2" in s for s in _events_sql(mock_conn))
        assert len(downgrade_notice) == 1

    def test_status_endpoint_applies_a_due_downgrade(
        self, client, auth_headers, mock_conn, downgrade_notice
    ):
        """No cron: the first read after expiry is what downgrades the account."""
        mock_conn.fetchrow.side_effect = [
            {  # apply_due_downgrade sees an expired plan
                "email": USER_EMAIL,
                "plan": "pro",
                "pending_plan": DOWNGRADE_TARGET,
                "plan_expires_at": datetime.now(UTC) - timedelta(minutes=1),
            },
            {"plan": "free"},  # the status query, after the downgrade
            None,  # no usage row
        ]
        response = client.get("/billing/status", headers=auth_headers)
        assert response.status_code == 200, response.text
        assert response.json()["plan"] == "free"
        assert any("SET plan = $2" in s for s in _events_sql(mock_conn))
        assert downgrade_notice[0]["to_plan"] == "free"

    def test_status_endpoint_leaves_a_live_plan_alone(
        self, client, auth_headers, mock_conn, downgrade_notice
    ):
        mock_conn.fetchrow.side_effect = [
            {
                "email": USER_EMAIL,
                "plan": "pro",
                "pending_plan": DOWNGRADE_TARGET,
                "plan_expires_at": datetime.now(UTC) + timedelta(days=5),
            },
            {"plan": "pro"},
            None,
        ]
        response = client.get("/billing/status", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["plan"] == "pro"
        assert not any("SET plan = $2" in s for s in _events_sql(mock_conn))

    def test_email_failure_never_fails_the_webhook(
        self, client, mock_conn, monkeypatch
    ):
        from app.routes import billing as billing_routes

        async def _boom(*args, **kwargs):
            raise RuntimeError("resend is down")

        monkeypatch.setattr(billing_routes, "send_plan_downgrade_email", _boom)
        mock_conn.fetchrow.side_effect = [
            {"id": USER_ID, "email": USER_EMAIL},
            {
                "email": USER_EMAIL,
                "plan": "pro",
                "pending_plan": DOWNGRADE_TARGET,
                "plan_expires_at": datetime.now(UTC) - timedelta(hours=1),
            },
        ]
        response = _post(client, _event("subscription.expiry"))
        assert response.status_code == 200
        # The downgrade itself still happened.
        assert any("SET plan = $2" in s for s in _events_sql(mock_conn))


class TestPeriodParsing:
    @pytest.mark.parametrize(
        "raw",
        [
            "2026-12-01T00:00:00.000Z",
            "2026-12-01T00:00:00+00:00",
            datetime(2026, 12, 1, tzinfo=UTC),
        ],
    )
    def test_paystack_timestamp_forms(self, raw):
        parsed = parse_period_end(raw)
        assert parsed is not None
        assert parsed.year == 2026 and parsed.month == 12

    @pytest.mark.parametrize("raw", [None, "", "not-a-date", 0])
    def test_missing_or_bad_values_are_none(self, raw):
        assert parse_period_end(raw) is None

    def test_free_is_the_downgrade_target(self):
        assert DOWNGRADE_TARGET == "free"


def test_documented_events_match_the_handled_set():
    """The handler and the documentation must name the same four events."""
    from pathlib import Path

    from app.routes.billing import DOWNGRADE_EVENTS

    assert DOWNGRADE_EVENTS == {
        "subscription.disable",
        "subscription.not_renew",
        "subscription.expiry",
        "invoice.payment_failed",
    }
    doc = Path(__file__).resolve().parents[2] / "docs/tcdn-ai/PAYMENTS_LIVE.md"
    text = doc.read_text()
    for event in DOWNGRADE_EVENTS:
        assert event in text, f"{event} is handled but not documented"
