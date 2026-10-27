"""SSRF protection for every outbound fetch the coordinator makes.

Rules (all required, any failure is a rejection):

1. Scheme must be https.
2. Host must be on the configured allowlist. An empty allowlist blocks all
   outbound fetches — fail closed, not "allow everything".
3. The hostname is resolved **once**, every resolved address must be public
   (no loopback, private, link-local, CGNAT, multicast, reserved, or cloud
   metadata address), and the fetch then connects to the address that was
   checked. DNS rebinding therefore cannot swap in an internal IP after the
   check.

Rule 3 is enforced by ``PinnedHTTPTransport`` (``app/pinned_http.py``, shared
with the import-by-URL guard): ``validate_url`` returns the validated address
and the caller hands it to that transport, whose TCP connect target is the
address itself — the hostname is never resolved a second time at connect time,
and an environment proxy (which would resolve it itself) is bypassed. The
response path never follows redirects, since a 302 to another host would walk
around the allowlist.
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
    "metadata.goog",
    "instance-data",
    "169.254.169.254",
}

#: Hostname suffixes that are internal by construction, whatever DNS says. The
#: allowlist already has to name a host explicitly, so this only ever fires as
#: defense in depth — an operator must not be able to allowlist ``foo.internal``
#: by adding it to configuration. Same rule as the import guard's
#: ``_IMPORT_BLOCKED_HOST_SUFFIXES``.
BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")

#: Cloud metadata services, spelled out even where the generic rules already
#: cover them (link-local / ULA / CGNAT). A reader should never have to derive
#: "is 169.254.169.254 reachable?" from ``is_private``.
CLOUD_METADATA_ADDRESSES = {
    "169.254.169.254",  # AWS / Azure / GCP / DigitalOcean IMDS
    "fd00:ec2::254",  # AWS IMDS over IPv6
    "100.100.100.200",  # Alibaba Cloud metadata
    "192.0.0.192",  # Oracle Cloud metadata
}

#: Explicitly blocked networks. ``ipaddress.is_private``/``is_reserved`` cover
#: most of these on current Python, but not all on every version, and an SSRF
#: range list that silently loses a row on a version bump is exactly the bug
#: this module exists to prevent.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT, incl. Alibaba metadata
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, incl. cloud metadata
        "172.16.0.0/12",
        "192.0.0.0/24",  # IETF protocol assignments, incl. Oracle metadata
        "192.0.2.0/24",  # TEST-NET-1
        "192.168.0.0/16",
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
        "::/128",
        "::1/128",
        "100::/64",  # discard-only
        "fc00::/7",  # unique-local, incl. AWS IMDS over IPv6
        "fe80::/10",  # link-local
    )
)


@dataclass(frozen=True)
class PinnedTarget:
    url: str
    host: str
    port: int
    ip: str


def _address_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if str(ip) in CLOUD_METADATA_ADDRESSES:
        return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    # IPv4-mapped IPv6 must be judged by its embedded IPv4 address.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _address_allowed(ip.ipv4_mapped)
    # The explicit list: private/link-local/metadata/CGNAT/reserved ranges, so a
    # Python version that reports one of them more loosely still blocks it.
    return not any(ip in network for network in BLOCKED_NETWORKS)


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
    if (
        host in BLOCKED_HOSTNAMES
        or host.endswith(BLOCKED_HOST_SUFFIXES)
        or host not in {h.lower() for h in allowed_hosts}
    ):
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
