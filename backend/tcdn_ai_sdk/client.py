"""OpenAI-compatible TCDN-AI client.

What this client guarantees:

* it verifies the coordinator's placement-ticket signature against **pinned**
  coordinator keys before sending anything to a worker;
* the prompt is encrypted to the worker's session key with a fresh X25519
  keypair per request (forward secrecy), inside TLS 1.3;
* the coordinator never receives prompt content — it only sees the model name,
  token budget, price cap and the ``confidential`` flag;
* a worker failure before the first token triggers a re-placement; after the
  first token it raises ``PartialResultError`` (never silently truncates).

Limits are stated in the docs rather than papered over: confidential-tier
attestation is accepted in ``coordinator-verified`` mode (the coordinator's
signed verdict + evidence hash), and receipt signature verification at the SDK
is not possible without the worker's identity key, so the coordinator performs
that check.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from typing import Any, Self

import httpx

from . import _crypto
from .errors import (
    AttestationRequiredError,
    AuthenticationError,
    InsufficientCreditsError,
    InvalidRequestError,
    NoEligibleWorkerError,
    PartialResultError,
    RateLimitError,
    TCDNAIError,
    TicketVerificationError,
    WorkerUnavailableError,
)
from .protocol import (
    FINAL_FRAME,
    TICKET_DOMAIN,
    TOKEN_FRAME,
    b64d,
    b64e,
    canonical_json,
    decode_frames,
    encode_frame,
    request_aad,
    session_context,
    stream_aad,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.torrentcdn.store"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


# ── response shapes (OpenAI-compatible) ─────────────────────────────────────


@dataclass
class Message:
    role: str
    content: str


@dataclass
class Delta:
    role: str | None = None
    content: str | None = None


@dataclass
class Choice:
    index: int
    message: Message | None = None
    delta: Delta | None = None
    finish_reason: str | None = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ChatCompletion:
    id: str
    created: int
    model: str
    choices: list[Choice]
    usage: Usage
    object: str = "chat.completion"
    tcdn: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChatCompletionChunk:
    id: str
    created: int
    model: str
    choices: list[Choice]
    object: str = "chat.completion.chunk"
    usage: Usage | None = None
    tcdn: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        data = asdict(self)
        if data["usage"] is None:
            data.pop("usage")
        return data


def _estimate_tokens(text: str) -> int:
    # Deliberately conservative and provider-agnostic; the worker's engine
    # reports the authoritative count in the receipt.
    return max(1, len(text) // 4) if text else 0


# ── client ──────────────────────────────────────────────────────────────────


class Client:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 2,
        coordinator_public_keys: dict[str, str] | None = None,
        worker_transport: Any = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("TCDN_AI_API_KEY", "")
        if not self.api_key:
            raise AuthenticationError(
                "no API key: pass api_key= or set TCDN_AI_API_KEY", code="missing_api_key"
            )
        self.base_url = base_url.rstrip("/")
        self._assert_transport_security(self.base_url)
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._pinned_keys = dict(coordinator_public_keys) if coordinator_public_keys else None
        self._tofu = False
        self._worker_transport = worker_transport
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout),
            verify=_crypto.tls_context() if self.base_url.startswith("https") else True,
            follow_redirects=False,
            trust_env=False,
        )
        self.chat = Chat(self)
        self.models = Models(self)

    # ── transport safety ───────────────────────────────────────────────────

    @staticmethod
    def _assert_transport_security(base_url: str) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        if parsed.scheme == "https":
            return
        if parsed.hostname in LOCAL_HOSTS:
            # Plain HTTP is allowed only for a loopback development coordinator.
            logger.warning("TCDN-AI client using plain HTTP against a loopback address")
            return
        raise ValueError("base_url must use https (except loopback development)")

    # ── coordinator API ────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._http.request(
                    method, f"{self.base_url}{path}", headers=self._headers(), json=json_body
                )
            except httpx.HTTPError as exc:
                last_error = WorkerUnavailableError(f"coordinator unreachable: {type(exc).__name__}")
            else:
                if response.status_code < 400:
                    return response.json()
                self._raise_for_error(response)
            if attempt < self.max_retries:
                time.sleep(self._backoff(attempt))
        raise last_error or WorkerUnavailableError("coordinator request failed")

    @staticmethod
    def _backoff(attempt: int) -> float:
        # Jitter only, never a security decision: ``random`` is deliberate here.
        return min(8.0, 0.2 * (2**attempt)) + random.uniform(0, 0.1)  # nosec B311

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        try:
            error = response.json().get("error", {})
        except (ValueError, AttributeError):
            error = {}
        code = error.get("code", "error")
        message = error.get("message", f"HTTP {response.status_code}")
        mapping: dict[int, type[TCDNAIError]] = {
            401: AuthenticationError,
            402: InsufficientCreditsError,
            403: TCDNAIError,
            404: InvalidRequestError,
            409: InvalidRequestError,
            422: InvalidRequestError,
            429: RateLimitError,
            503: NoEligibleWorkerError,
        }
        raise mapping.get(response.status_code, TCDNAIError)(
            message, code=code, status_code=response.status_code
        )

    # ── coordinator keys ───────────────────────────────────────────────────

    def _coordinator_key(self, key_id: str) -> str:
        if self._pinned_keys is None:
            self._bootstrap_keys()
        if self._pinned_keys is None:
            # Explicit rather than ``assert``: assertions vanish under ``-O`` and
            # this one guards the signature-verification key lookup.
            raise TicketVerificationError(
                "coordinator keys could not be pinned", code="keys_unavailable"
            )
        public = self._pinned_keys.get(key_id)
        if public is None:
            raise TicketVerificationError(
                f"coordinator key id {key_id!r} is not pinned", code="unknown_key_id"
            )
        return public

    def _bootstrap_keys(self) -> None:
        """Trust-on-first-use bootstrap of the coordinator key bundle.

        Acceptable for the standard tier in development; confidential tier
        requires explicit pinning because TOFU cannot prove it is talking to the
        real coordinator.
        """
        response = self._request("GET", "/api/ai/v1/keys/coordinator")
        bundle = response["bundle"]
        payload = bundle["payload"]
        bundle_key = next(
            (key for key in payload["keys"] if key["key_id"] == bundle["key_id"]), None
        )
        if bundle_key is None:
            raise TicketVerificationError("coordinator bundle is not self-consistent")
        if not _crypto.verify_ed25519(
            bundle_key["public_key"],
            _crypto.domain_message("tcdn-ai/keys/v1", payload),
            bundle["sig"],
        ):
            raise TicketVerificationError("coordinator key bundle signature is invalid")
        self._pinned_keys = {key["key_id"]: key["public_key"] for key in payload["keys"]}
        self._tofu = True
        logger.warning(
            "TCDN-AI coordinator keys pinned via TOFU; pass coordinator_public_keys "
            "for confidential workloads"
        )

    def _verify_ticket(self, document: dict, *, confidential: bool) -> dict:
        if not isinstance(document, dict):
            raise TicketVerificationError("placement ticket is malformed")
        if document.get("domain") != TICKET_DOMAIN:
            raise TicketVerificationError("placement ticket has the wrong domain")
        payload = document.get("payload")
        if not isinstance(payload, dict):
            raise TicketVerificationError("placement ticket payload is malformed")
        public = self._coordinator_key(str(document.get("key_id", "")))
        if confidential and self._tofu:
            # The keys were just fetched from the coordinator itself, so it could
            # be vouching for its own forged attestation. Confidential tier
            # requires out-of-band pinning.
            raise AttestationRequiredError(
                "confidential jobs require explicitly pinned coordinator keys "
                "(trust-on-first-use cannot verify an attestation verdict)",
                code="coordinator_keys_not_pinned",
            )
        if not _crypto.verify_ed25519(
            public, _crypto.domain_message(TICKET_DOMAIN, payload), str(document.get("sig", ""))
        ):
            raise TicketVerificationError("placement ticket signature is invalid")
        now = int(time.time())
        if int(payload.get("expires_at", 0)) + 5 < now:
            raise TicketVerificationError("placement ticket has expired", code="ticket_expired")
        if int(payload.get("issued_at", 0)) > now + 5:
            raise TicketVerificationError("placement ticket is from the future")
        if not payload.get("worker_public_key"):
            raise TicketVerificationError("placement ticket has no worker key")
        expected_tier = "confidential" if confidential else "standard"
        if payload.get("tier") != expected_tier:
            raise TicketVerificationError("placement ticket tier does not match the request")
        return payload

    # ── one request end to end ─────────────────────────────────────────────

    def _execute(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: bool,
        temperature: float,
        top_p: float,
        max_tokens: int,
        stop: list[str] | None,
        confidential: bool,
        max_price_micro: int | None,
        region: str | None,
        latency_target_ms: int | None,
        extra: dict[str, Any],
        _retry: int = 0,
    ) -> tuple[ChatCompletion | Iterator[ChatCompletionChunk], dict]:
        prompt_text = "".join(str(message.get("content", "")) for message in messages)
        placement_body = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": stream,
            "confidential": confidential,
            "prompt_tokens_estimate": _estimate_tokens(prompt_text),
            "region": region,
            "latency_target_ms": latency_target_ms,
            "max_price_micro": max_price_micro,
        }
        placement = self._request("POST", "/api/ai/v1/placements", json_body=placement_body)
        job_id = placement["job_id"]
        ticket = placement["ticket"]
        payload = self._verify_ticket(ticket, confidential=confidential)
        if payload["job_id"] != job_id:
            raise TicketVerificationError("ticket is for a different job")
        if confidential and not placement.get("key_attestation_ref"):
            raise AttestationRequiredError(
                "coordinator returned a confidential placement without an attestation reference",
                code="missing_attestation",
            )

        ticket_digest = _crypto.ticket_hash(ticket)
        client_private, client_public = _crypto.new_x25519()
        salt = os.urandom(32)
        shared = _crypto.shared_secret(client_private, payload["worker_public_key"])
        c2w, w2c = _crypto.derive_keys(
            shared, salt=salt, context=session_context(job_id)
        )
        request_plaintext = canonical_json(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "stop": stop or [],
                "stream": stream,
                **extra,
            }
        )
        ciphertext = _crypto.aead_encrypt(c2w, request_plaintext, request_aad(job_id, ticket_digest))
        worker_payload = {
            "job_id": job_id,
            "ticket": ticket,
            "client_public_key": client_public,
            "salt": b64e(salt),
            "ciphertext": b64e(ciphertext),
        }
        worker_url = placement["worker_endpoint"].rstrip("/") + "/v1/jobs"

        tcdn_meta = {
            "job_id": job_id,
            "node_id": payload["node_id"],
            "tier": payload["tier"],
            "confidentiality": payload["confidentiality"],
            "confidentiality_notice": placement["confidentiality_notice"],
            "coordinator_key_id": ticket["key_id"],
            "attestation_ref": placement.get("key_attestation_ref"),
        }

        events = self._stream_events(worker_url, worker_payload, job_id=job_id, w2c=w2c)

        def finalize(collected: list[str], receipt: dict) -> ChatCompletion:
            text = "".join(collected)
            output_sha = _crypto.sha256_hex(text.encode("utf-8"))
            completion_tokens = int(receipt.get("payload", {}).get("output_tokens", 0)) or len(collected)
            prompt_tokens = int(receipt.get("payload", {}).get("input_tokens", 0)) or _estimate_tokens(
                prompt_text
            )
            self._submit_ack(job_id, completion_tokens, output_sha)
            return ChatCompletion(
                id=f"chatcmpl-{job_id}",
                created=int(time.time()),
                model=model,
                choices=[
                    Choice(
                        index=0,
                        message=Message(role="assistant", content=text),
                        finish_reason="stop",
                    )
                ],
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ),
                tcdn=tcdn_meta,
            )

        if not stream:
            collected: list[str] = []
            receipt: dict = {}
            try:
                for kind, value in events:
                    if kind == "token":
                        collected.append(value)
                    else:
                        receipt = value
            except PartialResultError:
                raise
            except Exception as exc:
                if not collected and _retry < self.max_retries:
                    # No output consumed yet: re-place on a new worker.
                    time.sleep(self._backoff(_retry))
                    return self._execute(
                        model=model, messages=messages, stream=stream,
                        temperature=temperature, top_p=top_p, max_tokens=max_tokens,
                        stop=stop, confidential=confidential,
                        max_price_micro=max_price_micro, region=region,
                        latency_target_ms=latency_target_ms, extra=extra,
                        _retry=_retry + 1,
                    )
                if collected:
                    raise PartialResultError(
                        f"worker failed after output started: {type(exc).__name__}",
                        partial_text="".join(collected),
                        job_id=job_id,
                    ) from exc
                raise
            return finalize(collected, receipt), tcdn_meta

        def chunk_iterator() -> Iterator[ChatCompletionChunk]:
            collected: list[str] = []
            receipt: dict = {}
            emitted_role = False
            try:
                for kind, value in events:
                    if kind == "final":
                        receipt = value
                        break
                    text = value
                    collected.append(text)
                    delta = Delta(content=text)
                    include_meta = not emitted_role
                    if not emitted_role:
                        delta.role = "assistant"
                        emitted_role = True
                    yield ChatCompletionChunk(
                        id=f"chatcmpl-{job_id}",
                        created=int(time.time()),
                        model=model,
                        choices=[Choice(index=0, delta=delta)],
                        tcdn={**tcdn_meta} if include_meta else {},
                    )
            except Exception as exc:
                if not collected and _retry < self.max_retries:
                    time.sleep(self._backoff(_retry))
                    replacement, _ = self._execute(
                        model=model, messages=messages, stream=stream,
                        temperature=temperature, top_p=top_p, max_tokens=max_tokens,
                        stop=stop, confidential=confidential,
                        max_price_micro=max_price_micro, region=region,
                        latency_target_ms=latency_target_ms, extra=extra,
                        _retry=_retry + 1,
                    )
                    yield from replacement  # type: ignore[misc]
                    return
                if collected:
                    raise PartialResultError(
                        f"worker failed mid-stream: {type(exc).__name__}",
                        partial_text="".join(collected),
                        job_id=job_id,
                    ) from exc
                raise
            output_sha = _crypto.sha256_hex("".join(collected).encode("utf-8"))
            completion_tokens = int(receipt.get("payload", {}).get("output_tokens", 0)) or len(
                collected
            )
            prompt_tokens = int(receipt.get("payload", {}).get("input_tokens", 0)) or _estimate_tokens(
                prompt_text
            )
            self._submit_ack(job_id, completion_tokens, output_sha)
            yield ChatCompletionChunk(
                id=f"chatcmpl-{job_id}",
                created=int(time.time()),
                model=model,
                choices=[Choice(index=0, delta=Delta(), finish_reason="stop")],
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ),
                tcdn=tcdn_meta,
            )

        return chunk_iterator(), tcdn_meta

    def _stream_events(
        self, worker_url: str, payload: dict, *, job_id: str, w2c: bytes
    ) -> Iterator[tuple[str, Any]]:
        buffer = b""
        sequence = 0
        for raw in self._worker_bytes(worker_url, payload):
            buffer += raw
            frames, buffer = decode_frames(buffer)
            for frame in frames:
                if frame.kind == TOKEN_FRAME:
                    plaintext = _crypto.aead_decrypt(
                        w2c, frame.body, stream_aad(job_id, sequence)
                    )
                    sequence += 1
                    yield ("token", plaintext.decode("utf-8"))
                elif frame.kind == FINAL_FRAME:
                    yield ("final", json.loads(frame.body.decode("utf-8")))

    def _worker_bytes(self, worker_url: str, payload: dict) -> Iterable[bytes]:
        if self._worker_transport is not None:
            result = self._worker_transport(worker_url, payload)
            if isinstance(result, (bytes, bytearray)):
                return [bytes(result)]
            return result
        def generate() -> Iterator[bytes]:
            try:
                with self._http.stream(
                    "POST",
                    worker_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise WorkerUnavailableError(
                            f"worker rejected the job with HTTP {response.status_code}"
                        )
                    yield from response.iter_bytes()
            except httpx.HTTPError as exc:
                raise WorkerUnavailableError(
                    f"worker connection failed: {type(exc).__name__}"
                ) from exc

        return generate()

    def _submit_ack(self, job_id: str, tokens_received: int, output_sha256: str) -> None:
        body = {
            "job_id": job_id,
            "tokens_received": tokens_received,
            "output_sha256": output_sha256,
            "acked_at": int(time.time()),
        }
        self._request("POST", f"/api/ai/v1/jobs/{job_id}/ack", json_body=body)

    # ── account helpers ────────────────────────────────────────────────────

    def balance(self) -> dict:
        return self._request("GET", "/api/ai/v1/balance")

    def usage(self, limit: int = 100) -> dict:
        return self._request("GET", f"/api/ai/v1/usage?limit={int(limit)}")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ── OpenAI-style namespaces ─────────────────────────────────────────────────


class Chat:
    def __init__(self, client: Client) -> None:
        self.completions = Completions(client)


class Completions:
    def __init__(self, client: Client) -> None:
        self._client = client

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: bool = False,
        temperature: float = 0.7,
        top_p: float = 1.0,
        max_tokens: int = 512,
        stop: list[str] | str | None = None,
        confidential: bool = False,
        max_price_micro: int | None = None,
        region: str | None = None,
        latency_target_ms: int | None = None,
        **extra: Any,
    ) -> ChatCompletion | Iterator[ChatCompletionChunk]:
        if not isinstance(messages, list) or not messages:
            raise InvalidRequestError("messages must be a non-empty list")
        normalized: list[dict] = []
        for message in messages:
            if not isinstance(message, dict) or "role" not in message or "content" not in message:
                raise InvalidRequestError("each message needs role and content")
            if message["role"] not in {"system", "user", "assistant"}:
                raise InvalidRequestError("unsupported message role")
            normalized.append({"role": message["role"], "content": str(message["content"])})
        stop_list = [stop] if isinstance(stop, str) else list(stop or [])
        return self._client._execute(
            model=model,
            messages=normalized,
            stream=stream,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop_list,
            confidential=confidential,
            max_price_micro=max_price_micro,
            region=region,
            latency_target_ms=latency_target_ms,
            extra=extra,
        )[0]


class Models:
    def __init__(self, client: Client) -> None:
        self._client = client

    def list(self) -> list[dict]:
        return self._client._request("GET", "/api/ai/v1/models")


def frame_token(ciphertext: bytes) -> bytes:
    return encode_frame(TOKEN_FRAME, ciphertext)


def frame_final(receipt: dict) -> bytes:
    return encode_frame(FINAL_FRAME, canonical_json(receipt))


def decode_b64(value: str) -> bytes:
    return b64d(value)
