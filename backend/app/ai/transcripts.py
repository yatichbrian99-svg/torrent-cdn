"""Worker-side transcript capture and encrypted retention (Part 3 milestone 1).

The worker records, for every generated token, its position, token id, chosen
logprob and a small top-k set. Those records are committed into the receipt's
Merkle root and kept only until the verification window closes.

Two rules shape this module:

* **Nothing fabricated.** A record is added only from the engine's own logprob
  stream. The development echo backend labels its synthetic records
  ``simulated`` at the receipt level so they can never pass as engine evidence.
* **Records are sensitive.** Token ids and probabilities reveal the output. They
  are serialised, encrypted with AES-256-GCM under a key derived from the
  worker's identity key, and stored with a TTL. Prompts and output text are
  never written here.

The store is process-local, which is correct for the single-worker agent and
recorded as a limitation: a worker restart loses its verification window, and a
multi-process worker would need shared encrypted storage.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections.abc import Iterable, Sequence
from typing import Any

from . import crypto
from .verification2 import TranscriptRecord, build_transcript_root

PROMPT_HASH_DOMAIN = b"tcdn-ai/prompt-hash/v1"
STORE_DOMAIN = b"tcdn-ai/transcript-store/v1"
DEFAULT_RETENTION_SECONDS = 86_400
MAX_TOP_K = 5
MAX_RECORDS = 100_000
MAX_STORED_JOBS = 1_024


def prompt_hash(job_id: str, messages: Sequence[dict[str, Any]]) -> str:
    """Salted SHA-256 of the prompt, reproducible by the verifier.

    The salt is derived from the job id, not from a worker secret: the verifier
    receives the prompt in the verification bundle and must be able to recompute
    this value to check the receipt's consistency claim. The hash is one-way, so
    the coordinator and trackers still never see the prompt.
    """
    salt = hashlib.sha256(PROMPT_HASH_DOMAIN + b"|" + job_id.encode("utf-8")).digest()
    body = crypto.canonical_json({"messages": list(messages)})
    return hashlib.sha256(salt + body).hexdigest()


class TranscriptRecorder:
    """Accumulates per-token records in generation order."""

    def __init__(
        self, *, top_k: int = MAX_TOP_K, max_records: int = MAX_RECORDS
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self.top_k = top_k
        self.max_records = max_records
        self._records: list[TranscriptRecord] = []

    def __len__(self) -> int:
        return len(self._records)

    @property
    def records(self) -> tuple[TranscriptRecord, ...]:
        return tuple(self._records)

    def reset(self) -> None:
        self._records.clear()

    def record(
        self,
        *,
        token_id: int,
        chosen_logprob: float,
        top_k: Iterable[tuple[int, float]] | None = None,
    ) -> TranscriptRecord:
        """Append one token record; positions are assigned contiguously."""
        if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 0:
            raise ValueError("token_id must be a non-negative integer")
        value = float(chosen_logprob)
        if not math.isfinite(value):
            raise ValueError("chosen_logprob must be finite")
        if value > 0.0:
            # log p is <= 0; a tiny positive value is engine rounding. Anything
            # larger means the caller is not handing us a logprob at all.
            if value <= 1e-6:
                value = 0.0
            else:
                raise ValueError("chosen_logprob cannot be positive")

        pairs: list[tuple[int, float]] = []
        seen: set[int] = set()
        for peer_id, peer_logprob in top_k or ():
            peer_value = float(peer_logprob)
            if peer_id in seen or not math.isfinite(peer_value):
                continue
            seen.add(peer_id)
            pairs.append((int(peer_id), peer_value))
        if token_id not in seen:
            pairs.insert(0, (token_id, value))
        pairs = pairs[: self.top_k]

        if len(self._records) >= self.max_records:
            raise ValueError("transcript exceeds the maximum record count")

        record = TranscriptRecord(
            position=len(self._records),
            token_id=token_id,
            chosen_logprob=value,
            top_k=tuple(pairs),
        )
        self._records.append(record)
        return record

    def extend_from_logprobs(self, content: Sequence[dict[str, Any]]) -> None:
        """Append records from an OpenAI/vLLM-shaped ``logprobs.content`` list.

        Every entry must carry a numeric ``token_id``. Partial logprob support
        is not enough to commit a transcript, so a missing id raises instead of
        silently shifting positions.
        """
        for item in content:
            token_id = item.get("token_id")
            logprob = item.get("logprob")
            if token_id is None or logprob is None:
                raise ValueError("engine logprobs are missing token_id or logprob")
            top_k = [
                (int(peer["token_id"]), float(peer["logprob"]))
                for peer in item.get("top_logprobs") or ()
                if peer.get("token_id") is not None and peer.get("logprob") is not None
            ]
            self.record(
                token_id=int(token_id),
                chosen_logprob=float(logprob),
                top_k=top_k,
            )

    def build_root(self) -> str:
        return build_transcript_root(list(self._records))


class EncryptedTranscriptStore:
    """Encrypted, TTL-bounded records keyed by job id."""

    def __init__(
        self,
        identity_private_key_b64: str,
        *,
        ttl_seconds: int = DEFAULT_RETENTION_SECONDS,
        clock=time.time,
        max_jobs: int = MAX_STORED_JOBS,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._key = crypto.hkdf_sha256(
            crypto.b64d(identity_private_key_b64),
            salt=hashlib.sha256(STORE_DOMAIN).digest(),
            info=STORE_DOMAIN + b"/key",
            length=crypto.KEY_BYTES,
        )
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._max_jobs = max_jobs
        self._items: dict[str, tuple[bytes, int]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def put(self, job_id: str, records: Sequence[TranscriptRecord]) -> None:
        if not records:
            raise ValueError("cannot store an empty transcript")
        plaintext = bytearray(crypto.canonical_json(_serialise(records)))
        try:
            blob = crypto.aead_encrypt(self._key, bytes(plaintext), _aad(job_id))
        finally:
            crypto.zeroise(plaintext)
        expires_at = int(self._clock()) + self.ttl_seconds
        with self._lock:
            self._make_room_locked()
            self._items[job_id] = (blob, expires_at)

    def get(self, job_id: str) -> list[TranscriptRecord]:
        with self._lock:
            item = self._items.get(job_id)
            if item is None:
                raise KeyError(job_id)
            blob, expires_at = item
            if int(self._clock()) >= expires_at:
                del self._items[job_id]
                raise KeyError(job_id)
        payload = crypto.aead_decrypt(self._key, blob, _aad(job_id))
        try:
            return _deserialise(json.loads(payload.decode("utf-8")))
        finally:
            buffer = bytearray(payload)
            crypto.zeroise(buffer)
            del payload

    def wipe(self, job_id: str) -> None:
        with self._lock:
            self._items.pop(job_id, None)

    def purge_expired(self) -> int:
        now = int(self._clock())
        with self._lock:
            expired = [
                key for key, (_, expires_at) in self._items.items() if now >= expires_at
            ]
            for key in expired:
                del self._items[key]
            return len(expired)

    def _make_room_locked(self) -> None:
        now = int(self._clock())
        if len(self._items) < self._max_jobs:
            return
        self._items = {
            key: value for key, value in self._items.items() if value[1] > now
        }
        while len(self._items) >= self._max_jobs:
            oldest = min(self._items, key=lambda key: self._items[key][1])
            del self._items[oldest]


def _aad(job_id: str) -> bytes:
    return STORE_DOMAIN + b"|" + job_id.encode("utf-8")


def _serialise(records: Sequence[TranscriptRecord]) -> dict[str, Any]:
    return {
        "v": 1,
        "records": [
            {
                "p": record.position,
                "t": record.token_id,
                "lp": record.chosen_logprob,
                "tk": [[token_id, logprob] for token_id, logprob in record.top_k],
            }
            for record in records
        ],
    }


def _deserialise(payload: Any) -> list[TranscriptRecord]:
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("unsupported transcript payload")
    records: list[TranscriptRecord] = []
    for item in payload.get("records") or ():
        records.append(
            TranscriptRecord(
                position=int(item["p"]),
                token_id=int(item["t"]),
                chosen_logprob=float(item["lp"]),
                top_k=tuple((int(pair[0]), float(pair[1])) for pair in item["tk"]),
            )
        )
    if not records:
        raise ValueError("empty transcript payload")
    return records
