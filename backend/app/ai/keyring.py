"""Key registry, rotation with overlap, revocation, and secret storage.

Design points that matter:

* **Verification is not the same as active signing.** A retired key keeps
  verifying for ``overlap_seconds`` so tickets issued just before a rotation
  still validate, but it can never sign again.
* **Unknown, revoked, and expired key ids are hard failures.** There is no
  "trust on first use" path in production; callers pin the coordinator's public
  keys out of band (SDK config / `GET /v1/keys/coordinator`).
* **Private keys never serialise into the public bundle or logs.** The bundle
  contains public keys only, and the secret-store file is written 0600 and
  refused on load if its permissions are wider.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import crypto
from .config import AISettings, get_ai_settings
from .errors import CryptoError, RevokedKeyError, UnknownKeyError

logger = logging.getLogger(__name__)

SIG_ALG = "Ed25519"
KEYS_DOMAIN = "tcdn-ai/keys/v1"
REVOCATIONS_DOMAIN = "tcdn-ai/revocations/v1"


def _now() -> int:
    return int(time.time())


@dataclass
class KeyRecord:
    key_id: str
    public_key: str
    private_key: str | None = None
    created_at: int = field(default_factory=_now)
    retired_at: int | None = None
    revoked_at: int | None = None
    revoke_reason: str | None = None

    @property
    def can_sign(self) -> bool:
        return (
            self.private_key is not None
            and self.retired_at is None
            and self.revoked_at is None
        )

    def can_verify(self, now: int, overlap_seconds: int) -> bool:
        if self.revoked_at is not None:
            return False
        if self.retired_at is None:
            return True
        return now <= self.retired_at + overlap_seconds

    def public_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "public_key": self.public_key,
            "algorithm": SIG_ALG,
            "created_at": self.created_at,
            "retired_at": self.retired_at,
            "revoked_at": self.revoked_at,
        }

    def secret_dict(self) -> dict[str, Any]:
        data = self.public_dict()
        data["private_key"] = self.private_key
        data["revoke_reason"] = self.revoke_reason
        return data


class KeyRing:
    """A named set of Ed25519 keys with exactly one active signing key."""

    def __init__(
        self,
        name: str,
        *,
        overlap_seconds: int = 3600,
        clock: Any = _now,
    ) -> None:
        self.name = name
        self.overlap_seconds = overlap_seconds
        self._clock = clock
        self._keys: dict[str, KeyRecord] = {}
        self._order: list[str] = []

    # ── construction ───────────────────────────────────────────────────────

    @classmethod
    def generate(
        cls, name: str, *, overlap_seconds: int = 3600, clock: Any = _now
    ) -> KeyRing:
        ring = cls(name, overlap_seconds=overlap_seconds, clock=clock)
        ring.add(clock=clock)
        return ring

    def add_public(
        self, public_b64: str, *, key_id: str | None = None, clock: Any = None
    ) -> KeyRecord:
        """Register a verification-only key (a peer's or the coordinator's).

        Workers hold the coordinator's public keys and must never receive its
        private keys; this is the only way to put a key in the ring without one.
        """
        now = int((clock or self._clock)())
        crypto.b64d(public_b64)  # reject malformed material early
        key_id = key_id or crypto.key_id_for(public_b64)
        if key_id in self._keys:
            return self._keys[key_id]
        record = KeyRecord(
            key_id=key_id, public_key=public_b64, private_key=None, created_at=now
        )
        self._keys[key_id] = record
        self._order.append(key_id)
        return record

    def add(
        self,
        private_b64: str | None = None,
        *,
        key_id: str | None = None,
        clock: Any = None,
    ) -> KeyRecord:
        now = int((clock or self._clock)())
        if private_b64 is None:
            material = crypto.generate_ed25519()
            private_b64 = material["private_key"]
        public_b64 = crypto.ed25519_public_from_private(private_b64)
        key_id = key_id or crypto.key_id_for(public_b64)
        if key_id in self._keys:
            return self._keys[key_id]
        record = KeyRecord(
            key_id=key_id,
            public_key=public_b64,
            private_key=private_b64,
            created_at=now,
        )
        self._keys[key_id] = record
        self._order.append(key_id)
        return record

    # ── lookup ─────────────────────────────────────────────────────────────

    @property
    def active(self) -> KeyRecord:
        for key_id in reversed(self._order):
            record = self._keys[key_id]
            if record.can_sign:
                return record
        raise CryptoError("keyring has no active signing key", code="no_signing_key")

    def get(self, key_id: str) -> KeyRecord:
        record = self._keys.get(key_id)
        if record is None:
            raise UnknownKeyError("unknown key id", details={"key_id": key_id})
        return record

    def resolve(self, key_id: str, *, at: int | None = None) -> KeyRecord:
        record = self.get(key_id)
        now = at if at is not None else self._clock()
        if record.revoked_at is not None:
            raise RevokedKeyError("key id has been revoked", details={"key_id": key_id})
        if not record.can_verify(int(now), self.overlap_seconds):
            raise CryptoError(
                "key id is outside its validity window",
                code="expired_key_id",
                details={"key_id": key_id},
            )
        return record

    # ── signing ────────────────────────────────────────────────────────────

    def sign(
        self,
        domain: str,
        payload: dict[str, Any],
        *,
        key_id: str | None = None,
    ) -> dict[str, Any]:
        record = self.active if key_id is None else self.get(key_id)
        if not record.can_sign:
            raise CryptoError(
                "key is not permitted to sign", code="inactive_signing_key"
            )
        return {
            "key_id": record.key_id,
            "alg": SIG_ALG,
            "domain": domain,
            "payload": payload,
            "sig": crypto.sign(domain, payload, record.private_key or ""),
        }

    def verify(
        self,
        document: dict[str, Any],
        domain: str,
        *,
        at: int | None = None,
    ) -> dict[str, Any]:
        """Verify a signed document and return its payload (fail closed)."""
        if not isinstance(document, dict):
            raise CryptoError("malformed signed document", code="malformed_document")
        required = {"key_id", "alg", "domain", "payload", "sig"}
        if not required.issubset(document):
            raise CryptoError("malformed signed document", code="malformed_document")
        if document["alg"] != SIG_ALG:
            raise CryptoError(
                "unsupported signature algorithm", code="unsupported_algorithm"
            )
        if document["domain"] != domain:
            # The domain is also inside the signed bytes; this check makes the
            # mismatch explicit and cheap.
            raise CryptoError("signature domain mismatch", code="domain_mismatch")
        record = self.resolve(str(document["key_id"]), at=at)
        payload = document["payload"]
        if not isinstance(payload, dict):
            raise CryptoError("malformed signed payload", code="malformed_document")
        if not crypto.verify(domain, payload, str(document["sig"]), record.public_key):
            raise CryptoError("signature verification failed", code="bad_signature")
        return payload

    # ── rotation / revocation ──────────────────────────────────────────────

    def rotate(self, *, at: int | None = None) -> KeyRecord:
        now = int(at if at is not None else self._clock())
        current = self.active
        current.retired_at = now
        return self.add(clock=lambda: now)

    def revoke(self, key_id: str, reason: str, *, at: int | None = None) -> None:
        record = self.get(key_id)
        if (
            record.can_sign
            and sum(1 for key_id_ in self._order if self._keys[key_id_].can_sign) == 1
        ):
            # Revoking the only signing key would leave the coordinator unable
            # to issue tickets; rotate first, then revoke.
            raise CryptoError(
                "cannot revoke the last active signing key; rotate first",
                code="last_signing_key",
            )
        record.revoked_at = int(at if at is not None else self._clock())
        record.revoke_reason = reason

    def public_bundle(self) -> dict[str, Any]:
        """Signed public key bundle. Contains no private material by construction."""
        payload = {
            "name": self.name,
            "generated_at": self._clock(),
            "keys": [self._keys[k].public_dict() for k in self._order],
        }
        return self.sign(KEYS_DOMAIN, payload)

    def revocation_list(self) -> dict[str, Any]:
        revoked = [
            {
                "key_id": record.key_id,
                "revoked_at": record.revoked_at,
                "reason": record.revoke_reason,
            }
            for record in self._keys.values()
            if record.revoked_at is not None
        ]
        return self.sign(
            REVOCATIONS_DOMAIN,
            {"name": self.name, "issued_at": self._clock(), "revoked": revoked},
        )

    # ── serialisation ──────────────────────────────────────────────────────

    def to_secret(self) -> dict[str, Any]:
        return {
            "version": 1,
            "name": self.name,
            "overlap_seconds": self.overlap_seconds,
            "keys": [self._keys[k].secret_dict() for k in self._order],
        }

    @classmethod
    def from_secret(cls, data: dict[str, Any], *, clock: Any = _now) -> KeyRing:
        if int(data.get("version", 0)) != 1:
            raise CryptoError("unsupported keyring version", code="keyring_version")
        ring = cls(
            str(data.get("name", "coordinator")),
            overlap_seconds=int(data.get("overlap_seconds", 3600)),
            clock=clock,
        )
        for entry in data.get("keys", []):
            record = KeyRecord(
                key_id=entry["key_id"],
                public_key=entry["public_key"],
                private_key=entry.get("private_key"),
                created_at=int(entry.get("created_at", 0)),
                retired_at=entry.get("retired_at"),
                revoked_at=entry.get("revoked_at"),
                revoke_reason=entry.get("revoke_reason"),
            )
            derived = (
                crypto.ed25519_public_from_private(record.private_key)
                if record.private_key
                else None
            )
            if derived is not None and derived != record.public_key:
                # A stored key whose public half does not match its private half
                # would silently sign documents nobody can verify.
                raise CryptoError(
                    "keyring entry is internally inconsistent", code="corrupt_keyring"
                )
            ring._keys[record.key_id] = record
            ring._order.append(record.key_id)
        if not ring._order:
            raise CryptoError("keyring contains no keys", code="corrupt_keyring")
        return ring


# ── secret stores ───────────────────────────────────────────────────────────


class SecretStore(Protocol):
    """Backend that persists the coordinator keyring.

    ``FileSecretStore`` is the documented development/single-node
    implementation. Production injects a KMS/HSM adapter implementing this same
    protocol (see RUNBOOK.md "Key rotation" and ASSUMPTIONS.md A6).
    """

    def load(self) -> dict[str, Any] | None: ...

    def save(self, data: dict[str, Any]) -> None: ...


class FileSecretStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise CryptoError(
                "keyring file permissions are too broad; chmod 600 is required",
                code="insecure_key_file",
                details={"mode": oct(mode)},
            )
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CryptoError(
                "keyring file is unreadable", code="corrupt_keyring"
            ) from exc

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True)
        # O_CREAT with 0600 from the first byte: never a window where the file
        # exists with default permissions.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(self.path, 0o600)


class MemorySecretStore:
    """In-memory store for tests. Never selected when environment=production."""

    def __init__(self) -> None:
        self.data: dict[str, Any] | None = None

    def load(self) -> dict[str, Any] | None:
        return self.data

    def save(self, data: dict[str, Any]) -> None:
        self.data = data


class EnvSecretStore:
    """Reads a JSON keyring from an environment variable (injected by KMS at runtime).

    Write is refused: a process environment is not a durable secret store.
    """

    def __init__(self, var: str = "TCDN_AI_COORDINATOR_KEYRING_JSON") -> None:
        self.var = var

    def load(self) -> dict[str, Any] | None:
        raw = os.environ.get(self.var)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise CryptoError(
                "injected keyring is not valid JSON", code="corrupt_keyring"
            ) from exc

    def save(self, data: dict[str, Any]) -> None:  # pragma: no cover - never used
        raise CryptoError(
            "injected keyring is read-only", code="read_only_secret_store"
        )


def load_keyring(
    settings: AISettings | None = None,
    *,
    store: SecretStore | None = None,
) -> KeyRing:
    """Load the coordinator keyring, or create one in development only."""
    settings = settings or get_ai_settings()
    if store is None:
        if settings.coordinator_key_file:
            store = FileSecretStore(settings.coordinator_key_file)
        elif settings.coordinator_keyring_json:
            # Injected at runtime by the platform's secret manager (K8s/Render
            # secret env vars, systemd credentials); never written back.
            store = EnvSecretStore()
        elif settings.is_production:  # pragma: no cover - validator rejects first
            raise CryptoError(
                "no keyring secret store configured", code="missing_secret_store"
            )
        else:
            store = MemorySecretStore()

    data = store.load()
    if data is None:
        if settings.is_production:
            raise CryptoError(
                "no coordinator keyring found in the secret store; refusing to auto-generate "
                "in production",
                code="missing_keyring",
            )
        logger.warning(
            "TCDN-AI keyring absent — generating an in-memory DEVELOPMENT keyring "
            "(keys do not survive a restart)"
        )
        ring = KeyRing.generate(
            "coordinator", overlap_seconds=settings.key_overlap_seconds
        )
        store.save(ring.to_secret())
        return ring

    return KeyRing.from_secret(data, clock=_now)
