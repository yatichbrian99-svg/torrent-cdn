#!/usr/bin/env python3
"""Cold start from an empty cache, against a real HTTPS origin.

Row 7.2 asks for a run, not another in-process assertion: a worker with no
weights on disk must assemble a model from peers, reject a corrupt shard and
ban its sender, and fall back to the registry origin over **real TLS** when no
peer can serve a shard. Then a second run must resume entirely from the warm
cache without touching the network.

The harness is deliberately real where it matters and faked only where the
sandbox forces it:

* a real ``asyncio`` TLS server serves the shards with a certificate for the
  allowlisted origin hostname, and the client validates it against a real CA;
* ``HttpsOriginPeer`` is the real class, so the SSRF validation and the pinned
  connect are exercised (the pin dials 93.184.216.34, which is not reachable
  from the sandbox, so the base httpcore connect is redirected to loopback —
  the *address the pin chose* is recorded and asserted);
* the peers are small in-process fakes, because shipping a peer-to-peer
  transport is 7.3 and this row is about the fetch path.

Usage::

    python scripts/cold_start_run.py --out ../docs/tcdn-ai/evidence/13-cold-start/cold-start.json

Exit code 0 = the cold start and the warm restart both behaved, 1 = not.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import ipaddress
import json
import ssl
import sys
import tempfile
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai import ssrf
from app.ai.errors import UnavailableError
from app.ai.shards import DiskSink, HttpsOriginPeer, SwarmFetcher, build_manifest

ORIGIN_HOST = "models.tcdn.store"
#: The address the allowlisted name pretends to resolve to. Nothing listens
#: there; the base connect is redirected to the loopback server after the pin
#: has chosen and recorded this address.
PINNED_PUBLIC_IP = "93.184.216.34"


# ── a real TLS origin ───────────────────────────────────────────────────────


def _certs(directory: Path) -> tuple[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tcdn cold start CA")])
    now = dt.datetime.now(dt.UTC)
    ca = (
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
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ORIGIN_HOST)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(ORIGIN_HOST)]), critical=False
        )
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
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    server_path = directory / "server.pem"
    server_path.write_bytes(
        leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        + leaf.public_bytes(serialization.Encoding.PEM)
    )
    return str(ca_path), str(server_path)


# ── peers ───────────────────────────────────────────────────────────────────


class HonestPeer:
    """Serves the shards a healthy cold-start peer would have (the first half)."""

    peer_id = "honest-peer"
    reputation = 50

    def __init__(self, payloads: list[bytes], limit: int) -> None:
        self._payloads = payloads
        self._limit = limit
        self.served = 0

    async def fetch_shard(self, index: int, expected_sha256: str) -> bytes:
        if index >= self._limit:
            raise OSError("peer does not have this shard")
        self.served += 1
        return self._payloads[index]


class CorruptPeer:
    """Claims a high reputation, returns bytes that fail the manifest hash."""

    peer_id = "corrupt-peer"
    reputation = 100

    def __init__(self, payloads: list[bytes]) -> None:
        self._payloads = payloads
        self.served = 0

    async def fetch_shard(self, index: int, expected_sha256: str) -> bytes:
        self.served += 1
        return b"not-" + self._payloads[index][:64]


class DeadPeer:
    peer_id = "dead-peer"
    reputation = 10

    async def fetch_shard(self, index: int, expected_sha256: str) -> bytes:
        raise OSError("peer offline")


# ── the run ─────────────────────────────────────────────────────────────────


async def run(
    workdir: Path, *, shard_count: int = 6, shard_bytes: int = 128 * 1024
) -> dict:
    started = time.time()
    payloads = [
        (f"shard-{index}:".encode() + bytes([index]) * shard_bytes)
        for index in range(shard_count)
    ]
    manifest = build_manifest(
        model_id="cold-start-model", name="Cold Start Model", shard_payloads=payloads
    )
    cache = workdir / "cache"
    ca_path, server_pem = _certs(workdir / "certs")
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(server_pem)
    server = await asyncio.start_server(
        _handler_wrapper(payloads), "127.0.0.1", 0, ssl=ssl_context
    )
    loop_port = server.sockets[0].getsockname()[1]

    # DNS: the allowlisted name answers one public address at validation time.
    real_getaddrinfo = ssrf.socket.getaddrinfo
    ssrf.socket.getaddrinfo = lambda host, p, *a, **k: [
        (2, 1, 6, "", (PINNED_PUBLIC_IP, p or 443))
    ]

    # The pinned public address is unreachable from a sandbox, so redirect the
    # base connect to the loopback TLS server, recording what the pin chose.
    from httpcore._backends.anyio import AnyIOBackend

    base_connect = AnyIOBackend.connect_tcp
    targets: list[tuple[str, int]] = []

    async def redirected(self, host, port_, **kwargs):
        targets.append((host, port_))
        return await base_connect(self, "127.0.0.1", loop_port, **kwargs)

    AnyIOBackend.connect_tcp = redirected
    penalties: list[list[str]] = []
    origin = HttpsOriginPeer(
        base_url=f"https://{ORIGIN_HOST}/weights",
        manifest=manifest,
        allowed_hosts=(ORIGIN_HOST,),
        verify=ca_path,
    )
    try:
        sink = DiskSink(cache, quota_bytes=shard_count * shard_bytes * 4)
        honest = HonestPeer(payloads, limit=shard_count // 2)
        corrupt = CorruptPeer(payloads)
        cold = await SwarmFetcher(
            manifest,
            [corrupt, DeadPeer(), honest],
            sink=sink,
            attempts=3,
            penalty=lambda peer, reason: penalties.append([peer, reason]),
            origin=origin,
        ).fetch_all()
        verified = sum(
            1 for shard in manifest.shards if sink.has_shard(shard.index, shard.sha256)
        )
        cold_bytes = sum(
            (cache / f"shard-{shard.index:05d}.safetensors").stat().st_size
            for shard in manifest.shards
            if (cache / f"shard-{shard.index:05d}.safetensors").is_file()
        )
    finally:
        await origin.aclose()
        AnyIOBackend.connect_tcp = base_connect
        ssrf.socket.getaddrinfo = real_getaddrinfo
        server.close()
        await server.wait_closed()

    # Second run, warm cache, no peers and no origin at all: everything must be
    # resumed from disk, which is only possible if the first run wrote verified
    # bytes.
    warm = await SwarmFetcher(
        manifest,
        [DeadPeer()],
        sink=DiskSink(cache, quota_bytes=shard_count * shard_bytes * 4),
        attempts=1,
    ).fetch_all()

    assert ipaddress.ip_address(PINNED_PUBLIC_IP).is_global
    pin_ok = {host for host, _ in targets} == {PINNED_PUBLIC_IP}
    result = {
        "origin": {
            "host": ORIGIN_HOST,
            "tls": True,
            "connect_targets": sorted({host for host, _ in targets}),
        },
        "shards": shard_count,
        "bytes": cold_bytes,
        "verified_shards": verified,
        "cold_run": {
            "shards_written": cold.shards_written,
            "shards_resumed": cold.shards_resumed,
            "origin_shards": cold.origin_shards,
            "peers_used": sorted(cold.peers_used),
            "corrupt_peers": sorted(cold.corrupt_peers),
            "failed_shards": cold.failed_shards,
            "penalties": penalties,
            "honest_peer_shards": honest.served,
            "corrupt_peer_shards": corrupt.served,
        },
        "warm_run": {
            "shards_written": warm.shards_written,
            "shards_resumed": warm.shards_resumed,
            "origin_shards": warm.origin_shards,
            "failed_shards": warm.failed_shards,
        },
        "elapsed_seconds": round(time.time() - started, 2),
    }
    ok = (
        pin_ok
        and verified == shard_count
        and cold.origin_shards > 0
        and cold.corrupt_peers == {"corrupt-peer"}
        and penalties
        and cold.failed_shards == []
        and warm.shards_resumed == shard_count
        and warm.shards_written == 0
        and warm.origin_shards == 0
    )
    result["ok"] = ok
    result["shard_sha256"] = [shard.sha256 for shard in manifest.shards]
    result["manifest_hash"] = manifest.manifest_hash
    result["cache_files"] = sorted(p.name for p in cache.glob("*.safetensors"))
    result["cache_dir_bytes"] = cold_bytes
    result["payload_sha256"] = [hashlib.sha256(p).hexdigest()[:16] for p in payloads]
    result["corrupt_origin_refused"] = await _corrupt_origin_is_refused(
        manifest, payloads[:1], workdir / "corrupt-origin"
    )
    return result


def _handler_wrapper(payloads: list[bytes]):
    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            path = head.split(b" ", 2)[1].decode("latin-1")
            index = int(path.rsplit("shard-", 1)[1].split(".")[0])
            body = payloads[index]
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    return handler


async def _corrupt_origin_is_refused(
    manifest, payloads: list[bytes], workdir: Path
) -> bool:
    """A compromised origin is no more trusted than a peer: hash check applies."""
    from app.ai.shards import MemorySink

    class BadOrigin:
        peer_id = "origin"
        reputation = 100

        async def fetch_shard(self, index: int, expected_sha256: str) -> bytes:
            return b"wrong" + payloads[0]

    sink = MemorySink()
    try:
        await SwarmFetcher(
            manifest, [DeadPeer()], sink=sink, attempts=1, origin=BadOrigin()
        ).fetch_all()
    except UnavailableError:
        return sink.data == {}
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="")
    parser.add_argument("--shards", type=int, default=6)
    parser.add_argument("--shard-bytes", type=int, default=128 * 1024)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="tcdn-cold-start-") as tmp:
        result = asyncio.run(
            run(Path(tmp), shard_count=args.shards, shard_bytes=args.shard_bytes)
        )

    print(
        json.dumps({k: v for k, v in result.items() if k != "shard_sha256"}, indent=2)
    )
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=2))
    print(
        f"\n{'PASS' if result['ok'] else 'FAIL'} — {result['verified_shards']}/"
        f"{result['shards']} shards verified, origin served "
        f"{result['cold_run']['origin_shards']}, corrupt peers "
        f"{result['cold_run']['corrupt_peers']}, warm resume "
        f"{result['warm_run']['shards_resumed']}"
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
