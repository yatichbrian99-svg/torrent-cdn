"""Sender identity for system email.

Every transactional message (verification, password reset, MFA notification,
welcome) must come from ONE address on the Resend-verified domain, and that
address must stay distinct from the human mailboxes (hello@, yatich@,
partnerships@) hosted elsewhere. The welcome mail used to hardcode hello@ while
the auth mails used noreply@, splitting the domain's sending identity.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app import email_service as es
from app.services import email as svc

DEFAULT_SENDER = "TorrentCDN <notifications@torrentcdn.store>"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestTransactionalFromAddress:
    def test_default_is_the_notifications_mailbox(self, monkeypatch):
        """No RESEND_FROM configured → the shared default, not a per-module one."""
        monkeypatch.delenv("RESEND_FROM", raising=False)
        assert es.transactional_from_address() == DEFAULT_SENDER

    def test_dashboard_override_wins(self, monkeypatch):
        """RESEND_FROM is read per call, so it can be changed without a deploy."""
        monkeypatch.setenv("RESEND_FROM", "TorrentCDN <mail@torrentcdn.store>")
        assert es.transactional_from_address() == "TorrentCDN <mail@torrentcdn.store>"

    def test_welcome_email_uses_the_shared_sender(self, monkeypatch):
        """The welcome mail must not carry its own hardcoded from-address."""
        monkeypatch.delenv("RESEND_FROM", raising=False)
        captured = {}

        class FakeResponse:
            status_code = 200
            text = ""

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, headers=None, json=None):
                captured.update(json or {})
                return FakeResponse()

        monkeypatch.setattr(svc.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(svc, "get_settings", lambda: SimpleNamespace(resend_api_key="re_test_key"))

        assert _run(svc.send_welcome_email("someone@example.com", "someone")) is True
        assert captured["from"] == DEFAULT_SENDER
        assert captured["to"] == "someone@example.com"

    def test_missing_api_key_still_sends_nothing(self, monkeypatch):
        """Guard rail: no key → no request, and the helper is not reached."""
        monkeypatch.setattr(svc, "get_settings", lambda: SimpleNamespace(resend_api_key=""))

        def boom(*args, **kwargs):
            raise AssertionError("no HTTP client should be constructed without an API key")

        monkeypatch.setattr(svc.httpx, "AsyncClient", boom)
        assert _run(svc.send_welcome_email("someone@example.com", "someone")) is False


    def test_verification_email_uses_the_shared_sender(self, monkeypatch):
        """Same sender on the auth path (the Resend SDK branch)."""
        monkeypatch.delenv("RESEND_FROM", raising=False)
        fake_resend = MagicMock()
        monkeypatch.setattr(es, "_get_resend", lambda: fake_resend)

        assert _run(es.send_verification_email("someone@example.com", uuid4())) is True
        params = fake_resend.Emails.send.call_args.args[0]
        assert params["from"] == DEFAULT_SENDER


@pytest.mark.parametrize("module_attr", ["transactional_from_address"])
def test_both_senders_share_one_helper(module_attr):
    """services.email re-exports the same helper — no second copy of the rule."""
    assert getattr(svc, module_attr) is getattr(es, module_attr)
