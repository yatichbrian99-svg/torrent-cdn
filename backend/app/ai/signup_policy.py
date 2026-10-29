"""Pure anti-abuse policy for the free signup credit.

Everything here is deterministic and side-effect free so the gates it feeds can
be tested without a coordinator, a database or a request. The coordinator
(``service.Coordinator.grant_signup_credits``) owns the counters and the money;
this module owns what counts as "the same identity".

Three farming paths are closed here, each one observed in comparable products:

* **plus-addressing.** ``alice+1@example.com`` and ``alice+2@example.com`` reach
  two mailboxes that the person treats as one inbox, and the CDN's unique-email
  constraint treats them as two accounts. ``canonical_email`` strips the tag (and
  the dots Gmail ignores) so one identity can only ever claim one grant.
* **disposable inboxes.** A throwaway address costs the attacker nothing, which
  is exactly why the verified-email gate alone is not enough:
  ``is_disposable_domain`` blocks the well-known providers by name and suffix.
* **rotating a header.** The device key is a hash of a client-supplied id when
  the client sends one, otherwise of the user agent and language. It is not a
  hardware attestation — a determined attacker can rotate it — and the docs say
  so; it makes naive multi-account farming from one browser cost real effort.

Raw addresses, IPs and device ids never leave this module: callers store the
hashes only, so the audit trail can prove a refusal without holding the personal
data that caused it.
"""

from __future__ import annotations

import hashlib

#: Domains whose whole purpose is a mailbox that expires. Blocking is by suffix,
#: so ``foo.mailinator.com`` is blocked by the ``mailinator.com`` entry too.
#: Kept deliberately small and boring: an operator extends it through
#: ``TCDN_AI_SIGNUP_EXTRA_BLOCKED_EMAIL_DOMAINS`` rather than waiting for a
#: release, and this list is a floor, never the whole defence.
DISPOSABLE_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "10minutemail.com",
        "10minutemail.net",
        "20minutemail.com",
        "33mail.com",
        "anonbox.net",
        "burnermail.io",
        "dispostable.com",
        "dropmail.me",
        "emailondeck.com",
        "fakeinbox.com",
        "fakemail.net",
        "getairmail.com",
        "getnada.com",
        "grr.la",
        "guerrillamail.biz",
        "guerrillamail.com",
        "guerrillamail.de",
        "guerrillamail.net",
        "guerrillamail.org",
        "guerrillamailblock.com",
        "harakirimail.com",
        "inboxkitten.com",
        "jetable.org",
        "mail.tm",
        "mailcatch.com",
        "maildrop.cc",
        "mailinator.com",
        "mailinator.net",
        "mailnesia.com",
        "mailnull.com",
        "mailsac.com",
        "mintemail.com",
        "mohmal.com",
        "mytemp.email",
        "nada.email",
        "sharklasers.com",
        "spam4.me",
        "spamgourmet.com",
        "temp-mail.io",
        "temp-mail.org",
        "tempail.com",
        "tempinbox.com",
        "tempmail.dev",
        "tempmail.email",
        "tempmailo.com",
        "throwawaymail.com",
        "trashmail.com",
        "trashmail.de",
        "trashmail.net",
        "yopmail.com",
        "yopmail.fr",
        "yopmail.net",
    }
)

#: Domains that ignore dots in the local part, so ``a.b@`` and ``ab@`` are the
#: same inbox. Lower-case keys, canonical value on the right.
_DOTLESS_LOCAL_DOMAINS = {
    "gmail.com": "gmail.com",
    "googlemail.com": "gmail.com",
}
_DOTLESS_CANONICAL = frozenset(_DOTLESS_LOCAL_DOMAINS.values())

#: How much of a client-supplied device id is hashed. Long enough for a UUID or
#: a fingerprint hash, short enough that a request cannot carry a novel secret
#: into the audit trail.
_MAX_DEVICE_ID_CHARS = 128
_MAX_USER_AGENT_CHARS = 256


def canonical_email(email: str | None) -> str:
    """The address a person would call "the same inbox", lower-cased.

    Returns ``""`` for anything that is not a plausible address; callers treat
    that as a refusal (fail closed), never as an empty identity.
    """
    value = (email or "").strip().lower()
    if value.count("@") != 1:
        return ""
    local, domain = value.split("@", 1)
    if not local or not domain or len(value) > 320:
        return ""
    canonical_domain = _DOTLESS_LOCAL_DOMAINS.get(domain, domain)
    local = local.split("+", 1)[0]
    if canonical_domain in _DOTLESS_CANONICAL:
        local = local.replace(".", "")
    if not local:
        return ""
    return f"{local}@{canonical_domain}"


def email_domain(email: str | None) -> str:
    """Canonical domain of an address, or ``""`` when there is not one."""
    canonical = canonical_email(email)
    return canonical.rsplit("@", 1)[1] if canonical else ""


def email_hash(email: str | None) -> str:
    """Stable, non-reversible identity key for the grant table.

    Domain-separated from every other hash in the package (password, API key,
    IP, device) so a value from one table can never be replayed against another.
    """
    canonical = canonical_email(email)
    if not canonical:
        return ""
    return hashlib.sha256(f"tcdn-ai/signup-email/v1:{canonical}".encode()).hexdigest()


def is_disposable_domain(domain: str, *, extra_blocked: tuple[str, ...] = ()) -> bool:
    """True when the domain is a known throwaway mailbox provider.

    Suffix matching means a subdomain inherits its parent's block. An operator
    addition cannot *unblock* a bundled domain: this is an OR, never an override.
    """
    candidate = (domain or "").strip().lower().rstrip(".")
    if not candidate:
        return False
    blocked = DISPOSABLE_EMAIL_DOMAINS.union(
        d.strip().lower().lstrip("@.") for d in extra_blocked if d and d.strip()
    )
    return any(
        candidate == entry or candidate.endswith("." + entry) for entry in blocked
    )


def ip_hash(ip: str | None) -> str:
    """Stable, non-reversible network key for the per-IP grant cap.

    An unknown address is hashed as ``unknown``: it shares one bucket, which is
    the conservative direction (the cap fires sooner, never later).
    """
    raw = (ip or "unknown").strip().lower()
    return hashlib.sha256(f"tcdn-ai/signup-ip/v1:{raw}".encode()).hexdigest()


def device_hash(request: object | None) -> str:
    """Non-reversible key for the per-device cap, from a request or nothing.

    Prefers an explicit ``X-Device-Id`` header (a dashboard/CLI can set a stable
    id); otherwise hashes the user agent and language, which is a weak but free
    signal. A missing request hashes the ``unknown`` bucket, so callers that
    forget it cap at the shared device budget instead of bypassing the gate.
    """
    raw = ""
    headers = getattr(request, "headers", None)
    if headers is not None:
        device_id = headers.get("x-device-id") or ""
        if device_id:
            raw = "id:" + device_id.strip().lower()[:_MAX_DEVICE_ID_CHARS]
        else:
            agent = (headers.get("user-agent") or "").strip().lower()
            language = (headers.get("accept-language") or "").strip().lower()
            if agent or language:
                raw = (
                    "ua:"
                    + agent[:_MAX_USER_AGENT_CHARS]
                    + "|lang:"
                    + language[: _MAX_USER_AGENT_CHARS // 2]
                )
    bucket = raw or "unknown"
    return hashlib.sha256(f"tcdn-ai/signup-device/v1:{bucket}".encode()).hexdigest()


__all__ = [
    "DISPOSABLE_EMAIL_DOMAINS",
    "canonical_email",
    "device_hash",
    "email_domain",
    "email_hash",
    "ip_hash",
    "is_disposable_domain",
]
