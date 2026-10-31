"""F-3: the origin fetch must connect to the IP that was validated.

The audit's finding was that ``validate_url`` resolved the allowlisted host,
checked every answer, returned ``PinnedTarget.ip`` — and nothing read it, so
httpx resolved the name again at connect time and a rebinding answer could
reach an internal address that the check had already rejected.

These tests are behavioral, not structural: a real socket server is started on
loopback, and the assertions are about *where the TCP connect went*, not about
which class got instantiated.

* the first two drive the shared transport directly — once with a hostname that
  cannot resolve at all (only the pin can make that request succeed), once
  recording the connect target;
* the third drives the whole origin peer over real TLS: the resolver answers a
  public address at construction and a private one afterwards (classic
  rebinding), the certificate is validated against the allowlisted hostname
  (proving pinning left SNI/Host and certificate checking intact), and the
  connection is observed to go to the address that was checked.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import socket
import ssl
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.ai.errors import ValidationError
from app.ai.shards import HttpsOriginPeer, build_manifest
from app.ai.ssrf import validate_url
from app.pinned_http import PinnedHTTPTransport

#: A public address the tests pretend the allowlisted name resolves to. Nothing
#: listens there — the transport's connect is redirected to the loopback test
#: server, and the recorded target is what these tests assert on.
PINNED_PUBLIC_IP = "93.184.216.34"
ORIGIN_HOST = "models.tcdn.store"
REBINDING_PRIVATE_IP = "10.0.0.9"


async def _serve_once(
    payload: bytes, *, tls: ssl.SSLContext | None = None
) -> tuple[asyncio.AbstractServer, int, list[str]]:
    """Serve one HTTP request from a loopback socket and report where it listens.

    Returns ``(server, port, requests)``: ``requests`` collects the raw request
    heads the server received, so a test can assert on the Host header.
    """
    requests: list[str] = []

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            requests.append(raw.decode("latin-1"))
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/octet-stream\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=tls)
    return server, server.sockets[0].getsockname()[1], requests


async def _close(server: asyncio.AbstractServer) -> None:
    server.close()
    await server.wait_closed()


def _self_signed_host_cert(hostname: str, directory: Path) -> tuple[str, str]:
    """Write a CA and a leaf certificate for `hostname` into `directory`.

    Returns ``(ca_pem_path, server_pem_path)``: the client trusts the CA and the
    server presents a certificate that is valid for the hostname — which is how
    the end-to-end test proves the pinned connect did not disturb TLS.
    """
    directory.mkdir(parents=True, exist_ok=True)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tcdn test CA")])
    now = dt.datetime.now(dt.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
        )
        # Modern OpenSSL refuses a leaf without these two bindings to its CA.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = directory / "ca.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    server_path = directory / "server.pem"
    server_path.write_bytes(
        leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        + leaf_cert.public_bytes(serialization.Encoding.PEM)
    )
    return str(ca_path), str(server_path)


def _server_context(server_pem: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(server_pem)
    return context


def _loopback_redirect(
    monkeypatch, targets: list[tuple[str, int]], loopback_port: int
) -> None:
    """Make the pinned backend's connect go to loopback while recording the
    address it was asked for — the pin is the thing under test, and the pinned
    public address is not reachable from a sandbox.

    The pin lives in the subclass, which ignores the hostname httpcore hands it
    and calls ``super().connect_tcp(self._pinned_ip, ...)``. Patching the *base*
    therefore records exactly what the pin chose, and the loopback hop is
    applied after the argument has been recorded.
    """
    from httpcore._backends.anyio import AnyIOBackend

    base_connect_tcp = AnyIOBackend.connect_tcp

    async def recording_connect_tcp(self, host, port, **kwargs):
        targets.append((host, port))
        return await base_connect_tcp(self, "127.0.0.1", loopback_port, **kwargs)

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", recording_connect_tcp)


class TestPinnedTransport:
    @pytest.mark.asyncio
    async def test_the_pinned_address_is_dialed_even_though_the_host_does_not_resolve(
        self,
    ):
        """A name that cannot resolve at all still fetches, because it is never
        resolved: the transport dials the pinned address. Re-resolving the
        hostname (the F-3 bug) turns this into a DNS error."""
        payload = b"shard-bytes"
        server, port, _ = await _serve_once(payload)
        try:
            async with httpx.AsyncClient(
                transport=PinnedHTTPTransport("127.0.0.1"),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.get(f"http://origin.invalid:{port}/shard-0.bin")
            assert response.status_code == 200
            assert response.content == payload
        finally:
            await _close(server)

    @pytest.mark.asyncio
    async def test_the_connect_target_is_the_pinned_ip_and_the_host_header_is_the_name(
        self, monkeypatch
    ):
        payload = b"shard-bytes"
        server, port, requests = await _serve_once(payload)
        targets: list[tuple[str, int]] = []
        _loopback_redirect(monkeypatch, targets, port)
        try:
            async with httpx.AsyncClient(
                transport=PinnedHTTPTransport(PINNED_PUBLIC_IP), trust_env=False
            ) as client:
                response = await client.get(f"http://{ORIGIN_HOST}:{port}/shard-0.bin")
            assert response.status_code == 200
            assert response.content == payload
        finally:
            await _close(server)

        assert targets == [(PINNED_PUBLIC_IP, port)], targets
        # Pinning must not degrade the request into a bare-IP request: the name
        # stays in the Host header (and therefore in TLS SNI).
        assert requests and f"Host: {ORIGIN_HOST}".lower() in requests[0].lower()


class TestOriginRebindingIsClosed:
    """End-to-end through :class:`HttpsOriginPeer`, over real TLS."""

    @pytest.mark.asyncio
    async def test_rebinding_dns_cannot_move_the_origin_connect(
        self, monkeypatch, tmp_path
    ):
        ca_path, server_pem = _self_signed_host_cert(ORIGIN_HOST, tmp_path)
        payload = b"safetensors-shard"
        server, port, _ = await _serve_once(payload, tls=_server_context(server_pem))

        # The resolver: a public address on the first (validation) answer, a
        # private one for every answer after that.
        dns_calls: list[str] = []

        def rebinding_dns(host, port_, *args, **kwargs):
            dns_calls.append(host)
            answer = PINNED_PUBLIC_IP if len(dns_calls) == 1 else REBINDING_PRIVATE_IP
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, port_ or 443))]

        monkeypatch.setattr("app.ai.ssrf.socket.getaddrinfo", rebinding_dns)

        targets: list[tuple[str, int]] = []
        _loopback_redirect(monkeypatch, targets, port)

        manifest = build_manifest(
            model_id="test-model", name="Test", shard_payloads=[payload]
        )
        origin = HttpsOriginPeer(
            base_url=f"https://{ORIGIN_HOST}/weights",
            manifest=manifest,
            allowed_hosts=(ORIGIN_HOST,),
            verify=ca_path,
        )
        try:
            data = await origin.fetch_shard(0, manifest.shards[0].sha256)
        finally:
            await origin.aclose()
            await _close(server)

        assert data == payload, (
            "the TLS handshake or the payload did not survive pinning"
        )
        assert targets == [(PINNED_PUBLIC_IP, 443)], targets
        assert dns_calls == [ORIGIN_HOST], "the hostname was resolved more than once"
        assert ipaddress.ip_address(REBINDING_PRIVATE_IP).is_private

    @pytest.mark.asyncio
    async def test_the_origin_reports_a_certificate_that_does_not_match(
        self, monkeypatch, tmp_path
    ):
        """Control: the pin must not have disabled TLS verification. A server
        certificate for a different name is still rejected — the connect is
        pinned, the identity check is not skipped."""
        _, server_pem = _self_signed_host_cert("not-the-origin.invalid", tmp_path)
        payload = b"safetensors-shard"
        server, port, _ = await _serve_once(payload, tls=_server_context(server_pem))

        monkeypatch.setattr(
            "app.ai.ssrf.socket.getaddrinfo",
            lambda host, port_, *a, **k: [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (PINNED_PUBLIC_IP, port_ or 443),
                )
            ],
        )
        targets: list[tuple[str, int]] = []
        _loopback_redirect(monkeypatch, targets, port)

        ca_path, _ = _self_signed_host_cert(ORIGIN_HOST, tmp_path / "ca")
        manifest = build_manifest(
            model_id="test-model", name="Test", shard_payloads=[payload]
        )
        origin = HttpsOriginPeer(
            base_url=f"https://{ORIGIN_HOST}/weights",
            manifest=manifest,
            allowed_hosts=(ORIGIN_HOST,),
            verify=ca_path,
        )
        try:
            with pytest.raises(httpx.TransportError):
                await origin.fetch_shard(0, manifest.shards[0].sha256)
        finally:
            await origin.aclose()
            await _close(server)
        assert targets, "the pinned transport was never used"


# ── the range rules the pin depends on ─────────────────────────────────────


class TestBlockedRanges:
    """`validate_url` refuses the ranges an SSRF payload aims at, on every host
    it is asked about — origin URLs included."""

    @pytest.mark.parametrize(
        "address",
        [
            "169.254.169.254",  # cloud metadata (link-local)
            "fd00:ec2::254",  # AWS metadata over IPv6
            "100.100.100.200",  # Alibaba metadata (CGNAT)
            "192.0.0.192",  # Oracle metadata
            "127.0.0.1",
            "10.1.2.3",
            "172.16.5.5",
            "192.168.1.1",
            "100.64.0.1",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fc00::1",
            "::ffff:169.254.169.254",  # IPv4-mapped metadata
        ],
    )
    def test_an_allowlisted_internal_address_is_still_refused(
        self, monkeypatch, address
    ):
        monkeypatch.setattr(
            "app.ai.ssrf.socket.getaddrinfo",
            lambda *a, **k: [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0))
            ],
        )
        with pytest.raises(ValidationError) as excinfo:
            validate_url("https://origin.example/x", allowed_hosts=("origin.example",))
        assert excinfo.value.code == "ssrf_blocked"

    @pytest.mark.parametrize(
        "host",
        [
            "metadata",
            "metadata.google.internal",
            "instance-data",
            "foo.internal",
            "origin.local",
            "x.lan",
        ],
    )
    def test_internal_hostnames_are_refused_even_when_allowlisted(self, host):
        """An operator cannot allowlist their way into an internal name."""
        with pytest.raises(ValidationError) as excinfo:
            validate_url(f"https://{host}/x", allowed_hosts=(host,))
        assert excinfo.value.code == "ssrf_blocked"
