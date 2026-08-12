"""Referral-bonus tests — the +10GB-per-referral credit is LIFETIME-CAPPED at
``referral_bonus_cap_gb`` (config, default 100GB) via an atomic SQL LEAST
update, so referral farming can no longer grant unlimited free bandwidth.

The mocked pool (conftest) executes no real SQL, so these tests pin the SQL
shape + parameters the route emits: the LEAST cap clause, the cap value flowing
through, the self-referral guard, and the unknown-code no-op. Real-DB clamping
is delegated to Postgres' LEAST; the test asserts the route always sends it.
"""
from uuid import UUID

USER_ID = "33333333-3333-4333-8333-333333333333"
REFERRER_ID = "44444444-4444-4444-8444-444444444444"


def _register(client, ref_code="REFCAP12", email="referee@example.com"):
    return client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "SecurePass123",
            "terms_accepted": True,
            "ref_code": ref_code,
        },
    )


def _referrer_bonus_calls(mock_conn):
    """The execute calls that credit the REFERRER (excludes the referee UPDATE
    and the startup/register DDL, which also mention referral columns)."""
    out = []
    for c in mock_conn.execute.call_args_list:
        if not c.args:
            continue
        sql = c.args[0]
        if "LEAST(COALESCE(referral_bonus_gb" in sql:
            out.append(c)
    return out


def _inserted_user_uuid(mock_conn):
    """The random UUID the route generated for the new user, captured from the
    register INSERT's RETURNING call (unpredictable, so read it back)."""
    for c in mock_conn.fetchrow.call_args_list:
        if c.args and c.args[0].startswith("INSERT INTO users"):
            return c.args[1]
    raise AssertionError("no user INSERT call recorded")


class TestReferrerBonusCap:
    def test_referrer_credit_is_capped_with_least(self, client, mock_conn):
        """The referrer UPDATE must clamp with LEAST(+, cap) — never a bare +10."""
        mock_conn.fetchrow.side_effect = [
            None,  # existing-email check
            {"id": UUID(USER_ID)},  # INSERT ... RETURNING
            {"id": UUID(REFERRER_ID)},  # referrer lookup
        ]
        resp = _register(client)
        assert resp.status_code == 200

        from app.config import get_settings

        cap = float(get_settings().referral_bonus_cap_gb)
        calls = _referrer_bonus_calls(mock_conn)
        assert len(calls) == 1
        sql, params = calls[0].args[0], calls[0].args[1:]
        assert "LEAST(COALESCE(referral_bonus_gb, 0) + $2, $3)" in sql
        assert params == (UUID(REFERRER_ID), 10, cap)

    def test_lowered_cap_flows_to_sql(self, client, mock_conn, monkeypatch):
        """The cap is configurable via env (REFERRAL_BONUS_CAP_GB) — a lowered
        value must reach the LEAST bound."""
        from app.config import get_settings

        monkeypatch.setattr(get_settings(), "referral_bonus_cap_gb", 20)
        mock_conn.fetchrow.side_effect = [
            None,
            {"id": UUID(USER_ID)},
            {"id": UUID(REFERRER_ID)},
        ]
        resp = _register(client)
        assert resp.status_code == 200

        calls = _referrer_bonus_calls(mock_conn)
        assert len(calls) == 1
        assert calls[0].args[3] == 20.0

    def test_referee_credit_still_applied(self, client, mock_conn):
        """The new user's own +5GB credit is unchanged by the cap."""
        mock_conn.fetchrow.side_effect = [
            None,
            {"id": UUID(USER_ID)},
            {"id": UUID(REFERRER_ID)},
        ]
        resp = _register(client)
        assert resp.status_code == 200

        referee = [
            c for c in mock_conn.execute.call_args_list
            if c.args and "SET referred_by" in c.args[0]
        ]
        assert len(referee) == 1
        _, params = referee[0].args[0], referee[0].args[1:]
        assert params == (UUID(REFERRER_ID), _inserted_user_uuid(mock_conn), 5)

    def test_self_referral_credits_no_one(self, client, mock_conn):
        """Using your OWN referral code must not credit anyone. The route
        compares the referrer against the freshly-generated user UUID, so the
        referrer lookup must return THAT uuid (captured from the INSERT)."""
        captured = []

        def _side_effect(*args, **kwargs):
            sql = args[0]
            if sql.startswith("INSERT INTO users"):
                captured.append(args[1])
                return {"id": args[1]}
            if "UPPER(referral_code)" in sql:
                return {"id": captured[0]}
            return None

        mock_conn.fetchrow.side_effect = _side_effect
        resp = _register(client)
        assert resp.status_code == 200

        assert _referrer_bonus_calls(mock_conn) == []

    def test_unknown_ref_code_is_noop(self, client, mock_conn):
        """A code matching no user must be a silent no-op (registration succeeds)."""
        mock_conn.fetchrow.side_effect = [
            None,
            {"id": UUID(USER_ID)},
            None,  # referrer lookup miss
        ]
        resp = _register(client)
        assert resp.status_code == 200

        assert _referrer_bonus_calls(mock_conn) == []

    def test_no_ref_code_no_credit(self, client, mock_conn):
        """Plain registration (no ref_code) never emits a bonus UPDATE."""
        mock_conn.fetchrow.side_effect = [
            None,
            {"id": UUID(USER_ID)},
        ]
        resp = client.post(
            "/auth/register",
            json={
                "email": "plain@example.com",
                "password": "SecurePass123",
                "terms_accepted": True,
            },
        )
        assert resp.status_code == 200
        assert _referrer_bonus_calls(mock_conn) == []
