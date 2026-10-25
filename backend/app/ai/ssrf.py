"""SSRF protection for every outbound fetch the coordinator makes.

Rules (all required, any failure is a rejection):

1. Scheme must be https.
2. Host must be on the configured allowlist. An empty allowlist blocks all
   outbound fetches — fail closed, not "allow everything".
3. The hostname is resolved **once**, every resolved address must be public
   (no loopback, private, link-local, CGNAT, multicast, reserved, or cloud
   metadata address), and the caller must connect to a pinned address. DNS
   rebinding therefore cannot swap in an internal IP after the check.

The pinned-IP connection is the caller's responsibility (``resolve_and_pin``
returns the address to connect to); this module never uses ``requests``-style
follow-redirects, which would re-resolve.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import ValidationError

#: Addresses that must never be contacted, in addition to ipaddress.is_private.
BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "instance-data",
    "169.254.169.254",
}


@dataclass(frozen=True)
class PinnedTarget:
    url: str
    host: str
    port: int
    ip: str


def _address_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    # CGNAT 100.64.0.0/10 is not flagged is_private on all Python versions.
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network(
        "100.64.0.0/10"
    ):
        return False
    # IPv4-mapped IPv6 must be judged by its embedded IPv4 address.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _address_allowed(ip.ipv4_mapped)
    return True


def validate_url(url: str, *, allowed_hosts: tuple[str, ...]) -> PinnedTarget:
    if not allowed_hosts:
        raise ValidationError(
            "no outbound hosts are allowlisted; refusing to fetch", code="ssrf_blocked"
        )
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValidationError("only https origins are allowed", code="ssrf_blocked")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError("malformed origin URL", code="ssrf_blocked")
    host = parsed.hostname.lower()
    if host in BLOCKED_HOSTNAMES or host not in {h.lower() for h in allowed_hosts}:
        raise ValidationError("origin host is not allowlisted", code="ssrf_blocked")
    port = parsed.port or 443

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValidationError(
            "origin host could not be resolved", code="ssrf_blocked"
        ) from exc
    if not infos:
        raise ValidationError(
            "origin host resolved to no addresses", code="ssrf_blocked"
        )

    pinned: str | None = None
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValidationError(
                "origin resolved to an invalid address", code="ssrf_blocked"
            ) from exc
        if not _address_allowed(ip):
            raise ValidationError(
                "origin resolves to a non-public address", code="ssrf_blocked"
            )
        if pinned is None:
            # getaddrinfo types the address entry as a union; it is a string here.
            pinned = str(address)
    return PinnedTarget(url=url, host=host, port=port, ip=pinned or "")
