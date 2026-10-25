"""TCDN-AI wire protocol (stdlib only).

Shared by the Python SDK and the worker agent, and specified for porting in
``docs/tcdn-ai/SDK-PORT-SPEC.md``.

Request (SDK → worker), JSON:

```
{
  "job_id": "...",
  "ticket": { ...signed placement ticket... },
  "client_public_key": "<base64url X25519>",
  "salt": "<base64url 32 bytes>",
  "ciphertext": "<base64url nonce||AES-256-GCM ciphertext>"
}
```

* shared secret = X25519(client ephemeral private, ticket.worker_public_key)
* keys = HKDF-SHA-256(shared, salt, info = f"tcdn-ai/session/v1|{job_id}/c2w")
  and the matching ``/w2c`` key for responses
* request AAD = ``b"tcdn-ai/request/v1|" + job_id + b"|" + ticket_hash``
* plaintext = canonical JSON of the chat request (never logged)

Response: a sequence of frames, each ``kind(1) || length(4, big-endian) || body``.

* kind ``0x01`` token: body = ``nonce||AES-256-GCM(plaintext_chunk)`` with
  AAD ``b"tcdn-ai/stream/v1|" + job_id + b"|" + decimal(sequence)``
* kind ``0x02`` final: body = canonical JSON receipt (signed by the worker), not
  encrypted — it contains no prompt or output content.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

TOKEN_FRAME = 0x01
FINAL_FRAME = 0x02

REQUEST_AAD_PREFIX = b"tcdn-ai/request/v1"
STREAM_AAD_PREFIX = b"tcdn-ai/stream/v1"
SESSION_CONTEXT_PREFIX = "tcdn-ai/session/v1"

TICKET_DOMAIN = "tcdn-ai/ticket/v1"
RECEIPT_DOMAIN = "tcdn-ai/receipt/v1"
ACK_DOMAIN = "tcdn-ai/ack/v1"

MAX_FRAME_BYTES = 8 * 1024 * 1024


class ProtocolError(ValueError):
    pass


def canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ProtocolError("malformed base64 value")
    try:
        return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolError("malformed base64 value") from exc


def request_aad(job_id: str, ticket_hash: str) -> bytes:
    return REQUEST_AAD_PREFIX + b"|" + job_id.encode("utf-8") + b"|" + ticket_hash.encode("ascii")


def stream_aad(job_id: str, sequence: int) -> bytes:
    if sequence < 0:
        raise ProtocolError("negative frame sequence")
    return STREAM_AAD_PREFIX + b"|" + job_id.encode("utf-8") + b"|" + str(sequence).encode("ascii")


def session_context(job_id: str) -> str:
    return f"{SESSION_CONTEXT_PREFIX}|{job_id}"


def encode_frame(kind: int, body: bytes) -> bytes:
    if kind not in (TOKEN_FRAME, FINAL_FRAME):
        raise ProtocolError("unknown frame kind")
    if len(body) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    return bytes([kind]) + len(body).to_bytes(4, "big") + body


@dataclass(frozen=True)
class Frame:
    kind: int
    body: bytes


def decode_frames(buffer: bytes) -> tuple[list[Frame], bytes]:
    """Parse complete frames; return ``(frames, remainder)``."""
    frames: list[Frame] = []
    offset = 0
    while len(buffer) - offset >= 5:
        kind = buffer[offset]
        length = int.from_bytes(buffer[offset + 1 : offset + 5], "big")
        if kind not in (TOKEN_FRAME, FINAL_FRAME):
            raise ProtocolError("unknown frame kind")
        if length > MAX_FRAME_BYTES:
            raise ProtocolError("frame too large")
        end = offset + 5 + length
        if len(buffer) < end:
            break
        frames.append(Frame(kind=kind, body=buffer[offset + 5 : end]))
        offset = end
    return frames, buffer[offset:]
