"""Regression tests for the three viewer write routes' input boundary.

The ZAP scan of 2026-10-31 put a NUL byte in `channel_slug` and got a **500**
from `/api/live/session`, `/heartbeat` and `/segment/availability`: the slug was
passed to `_resolve_channel_for_playback` unchanged and asyncpg raised
`CharacterNotInRepertoireError: invalid byte sequence for encoding "UTF8": 0x00`
at bind time. The channel create/update routes already had this guard; the
viewer routes did not.

These tests assert the boundary, not the handler: a control character is a 422
before any handler runs, no DB call is made, and a clean payload still reaches
the playback-token check (401) instead of being caught by the new rule. The
response body is the app's sanitised `{"detail": "Invalid request"}` on purpose
— validation internals are not echoed back.
"""
import pytest

SESSION_ID = "sess-12345678"  # min_length is 8

ROUTES = {
    "/api/live/session": {"channel_slug": "live-channel", "session_id": SESSION_ID},
    "/api/live/heartbeat": {"channel_slug": "live-channel", "session_id": SESSION_ID},
    "/api/live/segment/availability": {
        "channel_slug": "live-channel",
        "session_id": SESSION_ID,
        "peer_id": "peer-12345678",
    },
}

# Every string field that can reach a text column, one hostile value each.
HOSTILE = [
    ("channel_slug", "bad\x00slug"),
    ("session_id", "sess\x0012"),
    ("peer_id", "peer\x0012"),
    ("user_agent", "UA\x00"),
    ("region", "eu\x00west"),
]


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_nul_byte_is_a_4xx_not_a_500(client, route):
    payload = dict(ROUTES[route])
    payload["channel_slug"] = "bad\x00slug"
    resp = client.post(route, json=payload)
    assert resp.status_code == 422, resp.text
    # The input is never echoed back, and the body carries no driver text.
    assert "\x00" not in resp.text
    assert "CharacterNotInRepertoire" not in resp.text


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_every_string_field_of_each_model_is_checked(client, route):
    """The guard walks the model, so a field added later is covered too."""
    for field, value in HOSTILE:
        payload = dict(ROUTES[route])
        if field not in payload:
            continue  # the model does not define it; pydantic drops it
        payload[field] = value
        resp = client.post(route, json=payload)
        assert resp.status_code == 422, (route, field, resp.text)


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_rejected_payload_never_touches_the_database(client, mock_conn, route):
    payload = dict(ROUTES[route])
    payload["channel_slug"] = "bad\x00slug"
    client.post(route, json=payload)
    assert mock_conn.fetchrow.await_count == 0
    assert mock_conn.execute.await_count == 0


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_clean_payload_still_reaches_the_token_check(client, mock_conn, route):
    """The new rule must not shadow the real answer: a well-formed body with no
    playback token is a 401, which is what the player is supposed to see."""
    mock_conn.fetchrow.return_value = {
        "id": "8d3f1e5a-0000-0000-0000-000000000000",
        "enabled": True,
    }
    resp = client.post(route, json=ROUTES[route])
    assert resp.status_code == 401, resp.text
