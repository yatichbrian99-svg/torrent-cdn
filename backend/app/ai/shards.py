"""Model weights over the P2P swarm: manifest, Merkle integrity, fetch, sinks.

The manifest is the root of trust: the coordinator signs it, and every shard is
checked against its SHA-256 (and the Merkle root) before it reaches the sink.
A peer that serves a corrupt shard is banned for the rest of the fetch and, if a
penalty callback is wired, reported for reputation loss.

Transports: the browser uses WebTorrent/WebRTC; a Python worker cannot reuse that
directly, so ``ShardPeer`` is the transport seam (see LIMITATIONS.md). The
manifest, Merkle and integrity logic is transport-agnostic and fully tested.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..pinned_http import PinnedHTTPTransport
from . import crypto
from .errors import UnavailableError, ValidationError
from .keyring import KeyRing
from .ssrf import validate_url

MANIFEST_DOMAIN = "tcdn-ai/manifest/v1"
ALLOWED_FORMATS = frozenset({"safetensors"})
_UNSAFE_FORMATS = frozenset({"pickle", "pkl", "pt", "pth", "bin", "gguf-unsafe"})


# ── Merkle tree ─────────────────────────────────────────────────────────────


def _hash_pair(left_hex: str, right_hex: str) -> str:
    return hashlib.sha256(
        bytes.fromhex(left_hex) + bytes.fromhex(right_hex)
    ).hexdigest()


def merkle_root(leaf_hashes: list[str]) -> str:
    if not leaf_hashes:
        raise ValidationError("cannot build a Merkle tree with no leaves")
    for digest in leaf_hashes:
        if len(digest) != 64:
            raise ValidationError("leaf hashes must be 64 hex characters")
    level = list(leaf_hashes)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])  # duplicate the last node (documented shape)
        level = [_hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_proof(leaf_hashes: list[str], index: int) -> list[dict[str, str]]:
    if not 0 <= index < len(leaf_hashes):
        raise ValidationError("leaf index out of range")
    level = list(leaf_hashes)
    position = index
    proof: list[dict[str, str]] = []
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sibling = position ^ 1
        proof.append(
            {
                "position": "right" if sibling > position else "left",
                "hash": level[sibling],
            }
        )
        level = [_hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        position //= 2
    return proof


def verify_merkle_proof(leaf_hex: str, proof: list[dict[str, str]], root: str) -> bool:
    # Proofs arrive from peers and from challenge responses, so every field is
    # validated before use: a non-string digest makes ``bytes.fromhex`` raise
    # TypeError, which would otherwise escape as an unhandled 500/DoS rather
    # than a clean rejection.
    if (
        not isinstance(leaf_hex, str)
        or not isinstance(root, str)
        or not isinstance(proof, list)
    ):
        return False
    if len(leaf_hex) != 64 or len(root) != 64:
        return False
    current = leaf_hex
    try:
        for step in proof:
            if not isinstance(step, dict):
                return False
            sibling = step.get("hash")
            position = step.get("position")
            if not isinstance(sibling, str) or len(sibling) != 64:
                return False
            if position == "left":
                current = _hash_pair(sibling, current)
            elif position == "right":
                current = _hash_pair(current, sibling)
            else:
                return False
    except (KeyError, ValueError, TypeError):
        return False
    return crypto.constant_time_eq(current, root)


# ── manifest ────────────────────────────────────────────────────────────────


class ShardSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, le=100_000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0, le=8 * 1024**3)


class ModelManifestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    name: str = Field(min_length=1, max_length=128)
    format: str
    version: str = Field(min_length=1, max_length=64)
    total_size: int = Field(ge=0)
    shards: list[ShardSpec] = Field(min_length=1, max_length=100_000)
    merkle_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: int

    @model_validator(mode="after")
    def _consistent(self) -> ModelManifestPayload:
        if self.format not in ALLOWED_FORMATS:
            raise ValueError(
                f"model format {self.format!r} is not allowlisted "
                f"(only {sorted(ALLOWED_FORMATS)} are accepted; formats that can execute "
                "code on load are refused)"
            )
        indexes = [shard.index for shard in self.shards]
        if indexes != list(range(len(indexes))):
            raise ValueError("shard indexes must be contiguous starting at 0")
        if sum(shard.size for shard in self.shards) != self.total_size:
            raise ValueError("shard sizes must sum to total_size")
        if merkle_root([s.sha256 for s in self.shards]) != self.merkle_root:
            raise ValueError("merkle_root does not match the shard hashes")
        return self

    @property
    def manifest_hash(self) -> str:
        return hashlib.sha256(
            crypto.canonical_json(self.model_dump(mode="json"))
        ).hexdigest()


def build_manifest(
    *,
    model_id: str,
    name: str,
    shard_payloads: list[bytes],
    version: str = "1",
    created_at: int | None = None,
    model_format: str = "safetensors",
) -> ModelManifestPayload:
    if not shard_payloads:
        raise ValidationError("a model needs at least one shard")
    shards = [
        ShardSpec(
            index=index,
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
        )
        for index, payload in enumerate(shard_payloads)
    ]
    return ModelManifestPayload(
        model_id=model_id,
        name=name,
        format=model_format,
        version=version,
        total_size=sum(s.size for s in shards),
        shards=shards,
        merkle_root=merkle_root([s.sha256 for s in shards]),
        created_at=int(created_at if created_at is not None else time.time()),
    )


def sign_manifest(keyring: KeyRing, manifest: ModelManifestPayload) -> dict:
    return keyring.sign(MANIFEST_DOMAIN, manifest.model_dump(mode="json"))


def verify_manifest(
    document: dict, keyring: KeyRing, *, max_model_bytes: int = 400 * 1024**3
) -> ModelManifestPayload:
    payload = keyring.verify(document, MANIFEST_DOMAIN)
    try:
        manifest = ModelManifestPayload(**payload)
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            "manifest is malformed or unsafe", code="invalid_manifest"
        ) from exc
    if manifest.total_size > max_model_bytes:
        raise ValidationError(
            "manifest exceeds the configured model size cap", code="manifest_too_large"
        )
    if (
        manifest.format in _UNSAFE_FORMATS
    ):  # defence in depth; model validation already refuses
        raise ValidationError(
            "model format is not permitted", code="unsafe_model_format"
        )
    return manifest


# ── weight sinks ────────────────────────────────────────────────────────────


class WeightSink(Protocol):
    def has_shard(self, index: int, sha256: str) -> bool: ...

    def put_shard(self, index: int, data: bytes) -> None: ...

    def finalize(self) -> None: ...


class HardwareUnavailable(RuntimeError):
    """Raised when a GPU-only path is selected on hardware that cannot serve it."""


def _safe_shard_name(index: int) -> str:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValidationError("shard index must be a non-negative integer")
    return f"shard-{index:05d}.safetensors"


class DiskSink:
    """Writes verified shards to a directory (tmpfs is the documented best case).

    The filename is derived from the integer index only — a manifest cannot
    smuggle a path separator into a cache path.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        quota_bytes: int,
        clock=time.time,
        sink_name: str = "disk",
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.quota_bytes = quota_bytes
        self._clock = clock
        self.name = sink_name

    def _path(self, index: int) -> Path:
        return self.root / _safe_shard_name(index)

    def has_shard(self, index: int, sha256: str) -> bool:
        path = self._path(index)
        if not path.is_file():
            return False
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return crypto.constant_time_eq(digest, sha256)

    def _used_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.glob("shard-*.safetensors"))

    def _evict_for(self, needed: int) -> None:
        if self._used_bytes() + needed <= self.quota_bytes:
            return
        files = sorted(
            self.root.glob("shard-*.safetensors"), key=lambda p: p.stat().st_mtime
        )
        for path in files:
            if self._used_bytes() + needed <= self.quota_bytes:
                break
            path.unlink(missing_ok=True)
        if self._used_bytes() + needed > self.quota_bytes:
            raise UnavailableError(
                "seeder disk quota cannot accommodate the shard",
                code="disk_quota_exceeded",
            )

    def put_shard(self, index: int, data: bytes) -> None:
        target = self._path(index)
        if target.exists():
            target.unlink()
        self._evict_for(len(data))
        fd, temp_name = tempfile.mkstemp(
            dir=self.root, prefix=".shard-", suffix=".part"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            Path(temp_name).unlink(missing_ok=True)

    def finalize(self) -> None:
        return None


class GpuDirectSink:
    """Direct-to-VRAM sink — unavailable in this build, by design.

    It must never fall back to disk silently: callers select ``DiskSink``
    explicitly. Requires CUDA bindings and a pinned-memory allocator that the
    repo does not ship, so construction fails closed and is documented in
    LIMITATIONS.md.
    """

    name = "gpu-direct"

    @staticmethod
    def available() -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("cuda") is not None
        except (ImportError, ValueError):  # pragma: no cover - defensive
            return False

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise HardwareUnavailable(
            "GpuDirectSink requires CUDA bindings that are not installed; "
            "select DiskSink (tmpfs) instead. No diskless path is claimed."
        )

    def has_shard(self, index: int, sha256: str) -> bool:  # pragma: no cover
        raise HardwareUnavailable("GpuDirectSink is unavailable")

    def put_shard(self, index: int, data: bytes) -> None:  # pragma: no cover
        raise HardwareUnavailable("GpuDirectSink is unavailable")

    def finalize(self) -> None:  # pragma: no cover
        raise HardwareUnavailable("GpuDirectSink is unavailable")


# ── swarm fetch ─────────────────────────────────────────────────────────────


class ShardPeer(Protocol):
    peer_id: str
    reputation: int

    async def fetch_shard(self, index: int, expected_sha256: str) -> bytes: ...


@dataclass
class FetchReport:
    shards_written: int = 0
    shards_resumed: int = 0
    bytes_written: int = 0
    peers_used: set[str] = field(default_factory=set)
    corrupt_peers: set[str] = field(default_factory=set)
    failed_shards: list[int] = field(default_factory=list)
    origin_shards: int = 0


class HttpsOriginPeer:
    """Last-resort shard source: the registry's own HTTPS origin.

    Every peer can be offline or banned at once, so a cold start needs an
    origin. It is deliberately narrow, because the URL is derived from a
    manifest and manifests come from peers:

    * HTTPS only — plain HTTP would put weights on the wire in clear;
    * an explicit host allowlist — otherwise a hostile manifest turns a worker
      into an SSRF proxy against the metadata service or an internal host;
    * the address that was validated is the address that is dialed — ``validate_url``
      resolves once and ``PinnedHTTPTransport`` connects to that IP, so DNS that
      answers public at construction and private at fetch time cannot rebind the
      connection (the same control the import-by-URL guard uses);
    * no redirects — a 302 to another host would walk around that allowlist;
    * a response size cap — a shard is declared in the manifest, not negotiated.

    Integrity is still the caller's check: this class returns bytes and lets
    ``SwarmFetcher`` verify the SHA-256 before anything reaches the sink.
    """

    def __init__(
        self,
        *,
        base_url: str,
        manifest: ModelManifestPayload,
        allowed_hosts: Iterable[str],
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        verify: bool | str = True,
        peer_id: str = "origin",
        reputation: int = 100,
    ) -> None:
        # Reuse the one SSRF control the coordinator already owns rather than
        # a second, subtly different allowlist: https only, an explicit host
        # allowlist, one DNS resolution with every address required to be
        # public. It runs once, at construction, so a fetch of 10k shards does
        # not re-resolve the name 10k times.
        self._target = validate_url(
            base_url.rstrip("/") + "/", allowed_hosts=tuple(allowed_hosts)
        )
        self.base_url = base_url.rstrip("/")
        self.manifest = manifest
        self.timeout_seconds = timeout_seconds
        self.peer_id = peer_id
        self.reputation = reputation
        self._shard_sizes = {shard.index: shard.size for shard in manifest.shards}
        # The validated address is what the socket connects to. Pinning here is
        # what makes the resolution above worth anything: without it, httpx (or
        # an HTTP proxy) would resolve ``self._target.host`` again at connect
        # time and a rebinding answer would reach an internal address that the
        # check had already rejected. ``client`` exists so a caller (tests, or a
        # deployment with its own pinned transport) can supply the connection —
        # it must be pinned too, and the response-host check below keeps a
        # redirect-following client honest.
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            # ``verify`` belongs on the transport: with an explicit transport,
            # a client-level verify is not consulted.
            transport=PinnedHTTPTransport(self._target.ip, verify=verify),
        )

    async def aclose(self) -> None:
        """Close the origin connection pool (a no-op for an injected client,
        which its owner closes)."""
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _url(self, index: int) -> str:
        # Both components are safe by construction: the model id is validated by
        # the manifest schema, and the shard name comes from the integer index.
        return (
            f"{self.base_url}/{quote(self.manifest.model_id, safe='')}"
            f"/{_safe_shard_name(index)}"
        )

    async def fetch_shard(self, index: int, expected_sha256: str) -> bytes:
        declared = self._shard_sizes.get(index)
        if declared is None:
            raise ValidationError(
                "origin asked for a shard the manifest does not declare",
                code="unknown_shard",
            )
        response = await self._client.get(self._url(index))
        if response.url.host and response.url.host.lower() != self._target.host:
            # Only reachable if a caller injected a redirect-following client;
            # the allowlist must survive that too.
            raise ValidationError(
                "origin redirected to a different host", code="ssrf_blocked"
            )
        if response.status_code != 200:
            raise UnavailableError(
                f"origin returned HTTP {response.status_code}",
                code="origin_unavailable",
                details={"status": response.status_code},
            )
        data = response.content
        if len(data) > declared:
            raise ValidationError(
                "origin returned more bytes than the manifest declares",
                code="origin_oversize",
                details={"declared": declared, "received": len(data)},
            )
        return data


class SwarmFetcher:
    def __init__(
        self,
        manifest: ModelManifestPayload,
        peers: list[ShardPeer],
        *,
        sink: WeightSink,
        concurrency: int = 8,
        attempts: int = 3,
        penalty: Callable[[str, str], None] | None = None,
        origin: ShardPeer | None = None,
    ) -> None:
        self.manifest = manifest
        self.origin = origin
        self.peers = sorted(peers, key=lambda p: p.reputation, reverse=True)
        self.sink = sink
        self.concurrency = concurrency
        self.attempts = attempts
        self.penalty = penalty
        self._banned: set[str] = set()
        self._lock = asyncio.Lock()
        self.report = FetchReport()

    async def _fetch_shard(self, index: int) -> None:
        spec = self.manifest.shards[index]
        if self.sink.has_shard(spec.index, spec.sha256):
            self.report.shards_resumed += 1
            return

        last_error: Exception | None = None
        tried: set[str] = set()
        for _ in range(self.attempts):
            async with self._lock:
                candidates = [
                    peer
                    for peer in self.peers
                    if peer.peer_id not in self._banned and peer.peer_id not in tried
                ]
            if not candidates:
                break
            peer = candidates[0]
            tried.add(peer.peer_id)
            try:
                data = await peer.fetch_shard(spec.index, spec.sha256)
            except Exception as exc:  # peer transport failure: try another peer
                last_error = exc
                continue

            actual = hashlib.sha256(data).hexdigest()
            if (
                not crypto.constant_time_eq(actual, spec.sha256)
                or len(data) != spec.size
            ):
                # Corrupt payload: never written, sender banned for this fetch.
                async with self._lock:
                    self._banned.add(peer.peer_id)
                    self.report.corrupt_peers.add(peer.peer_id)
                if self.penalty is not None:
                    self.penalty(peer.peer_id, f"corrupt shard {spec.index}")
                last_error = ValidationError(
                    "shard failed integrity verification", code="corrupt_shard"
                )
                continue

            self.sink.put_shard(spec.index, data)
            self.report.shards_written += 1
            self.report.bytes_written += len(data)
            self.report.peers_used.add(peer.peer_id)
            return

        # Last resort: the origin. The same integrity check applies — a
        # compromised origin gets no more trust than a peer.
        if self.origin is not None:
            try:
                data = await self.origin.fetch_shard(spec.index, spec.sha256)
            except Exception as exc:
                last_error = exc
            else:
                if (
                    crypto.constant_time_eq(
                        hashlib.sha256(data).hexdigest(), spec.sha256
                    )
                    and len(data) == spec.size
                ):
                    self.sink.put_shard(spec.index, data)
                    self.report.shards_written += 1
                    self.report.bytes_written += len(data)
                    self.report.origin_shards += 1
                    return
                last_error = ValidationError(
                    "origin shard failed integrity verification",
                    code="corrupt_shard",
                )

        self.report.failed_shards.append(spec.index)
        raise UnavailableError(
            f"shard {spec.index} could not be fetched from any trusted peer",
            code="shard_unavailable",
            details={
                "shard": spec.index,
                "error": type(last_error).__name__ if last_error else None,
            },
        )

    async def fetch_all(self) -> FetchReport:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def guarded(index: int) -> None:
            async with semaphore:
                await self._fetch_shard(index)

        await asyncio.gather(*(guarded(i) for i in range(len(self.manifest.shards))))
        self.sink.finalize()
        return self.report


class MemorySink:
    """Sink for tests and for workers that hand bytes straight to the engine."""

    name = "memory"

    def __init__(self) -> None:
        self.data: dict[int, bytes] = {}

    def has_shard(self, index: int, sha256: str) -> bool:
        existing = self.data.get(index)
        if existing is None:
            return False
        return crypto.constant_time_eq(hashlib.sha256(existing).hexdigest(), sha256)

    def put_shard(self, index: int, data: bytes) -> None:
        self.data[index] = data

    def finalize(self) -> None:
        return None
