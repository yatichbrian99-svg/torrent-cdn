"""Redirect the Resend SDK to the local stand-in when ``TCDN_FAKE_RESEND_URL`` is set.

This file is only on ``PYTHONPATH`` for the local/CI *verification* stack
(``PYTHONPATH=scripts/dev:...``); it is not part of the runtime image. With the
variable unset it does nothing, so importing it is always safe.

It exists because ``send_verification_email`` builds the real message and calls
``resend.Emails.send`` — the only thing worth substituting for a test is the
network hop to api.resend.com. ``resend.request`` reads ``resend.api_url`` at
call time, so setting it here is enough; no application code changes.
"""

import os

_url = os.environ.get("TCDN_FAKE_RESEND_URL")
if _url:
    try:
        import resend

        resend.api_url = _url.rstrip("/")
    except Exception:  # the SDK is not installed in every environment
        pass
