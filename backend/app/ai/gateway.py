"""OpenAI-compatible gateway (Section C item 1, ``gateway mode``).

Two ways to reach a worker exist, and they are not equivalent:

* **SDK mode** (`tcdn_ai_sdk`): the client derives a per-request X25519 key with
  the worker and encrypts the prompt itself. The coordinator never sees prompt
  content. Required for the confidential tier.
* **Gateway mode** (this module): an unmodified OpenAI client posts plaintext to
  the coordinator, which opens the *same* encrypted session to the worker and
  relays decrypted tokens back. The coordinator therefore **sees the prompt and
  the completion in transit**. It never logs, stores, or persists them, and the
  tier is always the standard tier (``confidentiality: "none"``).

Honesty rules for this module:

* every response carries ``tcdn.mode = "gateway"`` and
  ``tcdn.prompt_visibility = "gateway"`` plus the standard-tier notice, so a
  caller cannot mistake gateway mode for end-to-end encryption;
* unsupported OpenAI parameters are rejected with an OpenAI-shaped error rather
  than silently ignored;
* no prompt, completion, or API key is ever logged here (canary tests in
  ``tests/test_ai_openai_compat.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from tcdn_ai_sdk.protocol import (
    FINAL_FRAME,
    TOKEN_FRAME,
    canonical_json,
    decode_frames,
    request_aad,
    session_context,
    stream_aad,
)

from . import crypto
from .clock import now_unix
from .config import AISettings
from .errors import UnavailableError, ValidationError
from .models import (
    Confidentiality,
    GatewayMeasurement,
    PlacementRequest,
    SignedEnvelope,
)
from .service import AuthContext, Coordinator

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

#: OpenAI parameters this endpoint deliberately does not implement yet
#: (Section C item 11). Rejecting them is required: an OpenAI client that sends
#: ``tools`` must not silently receive a tool-less completion.
UNSUPPORTED_PARAMS = {
    "tools": "tool calling is not enabled on this deployment",
    "tool_choice": "tool calling is not enabled on this deployment",
    "response_format": "JSON mode is not enabled on this deployment",
    "logprobs": "logprobs are not exposed by this deployment",
    "top_logprobs": "logprobs are not exposed by this deployment",
    "presence_penalty": "sampling penalties are not supported",
    "frequency_penalty": "sampling penalties are not supported",
    "logit_bias": "logit_bias is not supported",
    "seed": "seed is not supported (determinism is not guaranteed)",
    "modalities": "only text output is supported",
    "audio": "audio output is not supported",
    "parallel_tool_calls": "tool calling is not enabled on this deployment",
    "service_tier": "service tiers are not supported",
    "metadata": "request metadata is not stored",
    "store": "storage of completions is not supported",
    "confidential": (
        "gateway mode is standard tier only; use the TCDN-AI SDK for confidential jobs"
    ),
}

ALLOWED_PARAMS = {
    "model",
    "messages",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "stream",
    "stream_options",
    "n",
    # Accepted for drop-in compatibility and then discarded: a stable end-user
    # identifier is personal data we have no use for (data minimisation).
    "user",
}

MAX_STOP_SEQUENCES = 4
MAX_STOP_CHARS = 128


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: list[dict[str, str]]
    max_tokens: int
    temperature: float
    top_p: float
    stop: list[str]
    stream: bool
    include_usage: bool

    @property
    def prompt_text(self) -> str:
        return "".join(message["content"] for message in self.messages)


def estimate_tokens(text: str) -> int:
    """Same conservative estimate the SDK uses (the worker/receipt is authoritative)."""
    return max(1, len(text) // 4) if text else 0


def _invalid(
    message: str, *, param: str | None, code: str = "invalid_request"
) -> ValidationError:
    details: dict[str, Any] = {}
    if param:
        details["param"] = param
    return ValidationError(message, code=code, details=details or None)


def _check_messages(messages: Any, settings: AISettings) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not messages:
        raise _invalid("messages must be a non-empty array", param="messages")
    if len(messages) > settings.max_messages:
        raise _invalid(
            f"at most {settings.max_messages} messages are allowed", param="messages"
        )
    normalized: list[dict[str, str]] = []
    total_chars = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise _invalid(
                f"messages[{index}] must be an object", param=f"messages[{index}]"
            )
        role = message.get("role")
        if role not in {"system", "user", "assistant"}:
            raise _invalid(
                f"messages[{index}].role must be system, user or assistant",
                param=f"messages[{index}].role",
            )
        content = message.get("content")
        if not isinstance(content, str):
            raise _invalid(
                f"messages[{index}].content must be a string "
                "(multimodal content parts are not supported)",
                param=f"messages[{index}].content",
            )
        total_chars += len(content)
        normalized.append({"role": str(role), "content": content})
    if total_chars > settings.max_prompt_chars:
        raise _invalid(
            f"prompt exceeds {settings.max_prompt_chars} characters", param="messages"
        )
    return normalized


def _check_stop(stop: Any) -> list[str]:
    if stop is None:
        return []
    if isinstance(stop, str):
        stop = [stop]
    if not isinstance(stop, list) or len(stop) > MAX_STOP_SEQUENCES:
        raise _invalid(
            f"stop must be a string or an array of at most {MAX_STOP_SEQUENCES} strings",
            param="stop",
        )
    for value in stop:
        if not isinstance(value, str) or len(value) > MAX_STOP_CHARS:
            raise _invalid(
                f"each stop sequence must be a string of at most {MAX_STOP_CHARS} characters",
                param="stop",
            )
    return list(stop)


def parse_chat_request(body: Any, settings: AISettings) -> ChatRequest:
    """Validate an OpenAI chat request, rejecting everything unsupported.

    Kept hand-written (rather than a permissive pydantic model) so the rejection
    can name the offending ``param`` in OpenAI's error shape and so no unknown
    security-relevant field can ever be silently dropped.
    """
    if not isinstance(body, dict):
        raise _invalid("request body must be a JSON object", param=None)

    for key, reason in UNSUPPORTED_PARAMS.items():
        if body.get(key) is not None:
            raise _invalid(
                f"{key} is not supported: {reason}",
                param=key,
                code="unsupported_parameter",
            )
    unknown = sorted(set(body) - ALLOWED_PARAMS)
    if unknown:
        raise _invalid(
            f"unknown parameter(s): {', '.join(unknown)}",
            param=unknown[0],
            code="unsupported_parameter",
        )

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise _invalid("model is required", param="model")

    messages = _check_messages(body.get("messages"), settings)

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise _invalid("stream must be a boolean", param="stream")

    stream_options = body.get("stream_options")
    include_usage = False
    if stream_options is not None:
        if not isinstance(stream_options, dict):
            raise _invalid("stream_options must be an object", param="stream_options")
        unknown_options = sorted(set(stream_options) - {"include_usage"})
        if unknown_options:
            raise _invalid(
                f"unknown stream option(s): {', '.join(unknown_options)}",
                param="stream_options",
                code="unsupported_parameter",
            )
        include_usage = bool(stream_options.get("include_usage", False))
        if not stream and include_usage:
            # OpenAI ignores stream_options without stream; accepting it silently
            # would be the kind of quiet divergence this repo forbids.
            raise _invalid(
                "stream_options.include_usage requires stream=true",
                param="stream_options",
            )

    n = body.get("n")
    if n is not None and n != 1:
        raise _invalid("only n=1 is supported", param="n", code="unsupported_parameter")

    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_completion_tokens")
    if max_tokens is None:
        max_tokens = 512
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens < 1
    ):
        raise _invalid("max_tokens must be a positive integer", param="max_tokens")
    if max_tokens > settings.max_tokens_per_request:
        raise _invalid(
            f"max_tokens exceeds the limit of {settings.max_tokens_per_request}",
            param="max_tokens",
            code="token_limit_exceeded",
        )

    temperature = body.get("temperature")
    if temperature is None:
        temperature = 1.0
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise _invalid("temperature must be a number", param="temperature")
    if not 0 <= float(temperature) <= 2:
        raise _invalid("temperature must be between 0 and 2", param="temperature")

    top_p = body.get("top_p")
    if top_p is None:
        top_p = 1.0
    if isinstance(top_p, bool) or not isinstance(top_p, (int, float)):
        raise _invalid("top_p must be a number", param="top_p")
    if not 0 < float(top_p) <= 1:
        raise _invalid("top_p must be greater than 0 and at most 1", param="top_p")

    user = body.get("user")
    if user is not None and not isinstance(user, str):
        raise _invalid("user must be a string", param="user")

    return ChatRequest(
        model=model,
        messages=messages,
        max_tokens=int(max_tokens),
        temperature=float(temperature),
        top_p=float(top_p),
        stop=_check_stop(body.get("stop")),
        stream=stream,
        include_usage=include_usage,
    )


# ── worker transport ────────────────────────────────────────────────────────

WorkerTransport = Callable[[str, dict[str, Any]], AsyncIterator[bytes]]


async def http_worker_transport(
    endpoint: str, payload: dict[str, Any]
) -> AsyncIterator[bytes]:
    """POST an encrypted job to a worker over TLS 1.3 and yield raw frames."""
    url = endpoint.rstrip("/") + "/v1/jobs"
    parsed = urlparse(url)
    secure = parsed.scheme == "https"
    if not secure and parsed.hostname not in LOOPBACK_HOSTS:
        # Same rule as the SDK: plaintext is tolerated only for a loopback
        # development worker. A remote worker over HTTP would expose ciphertext
        # and metadata, and would let a network attacker swap frames.
        raise UnavailableError(
            "worker endpoint must use https (except loopback)",
            code="insecure_worker_endpoint",
        )

    if secure:
        # Reuse the SDK's TLS 1.3-only context so there is exactly one transport
        # security policy in the repo (no drift between client and gateway).
        from tcdn_ai_sdk._crypto import tls_context

        verify: Any = tls_context()
    else:
        verify = True

    timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(
        timeout=timeout, verify=verify, follow_redirects=False, trust_env=False
    ) as client:
        try:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise UnavailableError(
                        f"worker rejected the job with HTTP {response.status_code}",
                        code="worker_rejected_job",
                    )
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise UnavailableError(
                f"worker connection failed: {type(exc).__name__}",
                code="worker_unreachable",
            ) from exc


# ── gateway ─────────────────────────────────────────────────────────────────


@dataclass
class TokenTally:
    """The gateway's own count of the completion frames it delivered.

    Incremented by the wrapper installed in ``open`` as the route drains the
    stream, so the count exists even when the customer never finishes reading:
    the gateway served those tokens and must be able to say how many.
    """

    frames: int = 0

    def add(self, kind: str) -> None:
        if kind == "token":
            self.frames += 1


@dataclass
class GatewayStream:
    job_id: str
    model: str
    tcdn: dict[str, Any]
    # AsyncGenerator, not AsyncIterator: the route pulls one frame before
    # committing to a 200 and closes the stream with ``aclose()``.
    events: AsyncGenerator[tuple[str, Any], None]
    #: A collected redundant-execution assignment (checklist 6.1), if this job
    #: was sampled and a second node was available. The gateway owns dispatch
    #: because it is the only party holding the plaintext.
    pending_verification: dict[str, Any] | None = None
    #: Background task that runs the verification and submits its receipt.
    #: Exposed so tests (and a graceful shutdown) can await it.
    verification_task: asyncio.Task | None = None
    #: Gateway's own count of the request prompt (it holds that plaintext), kept
    #: for the billing measurement — never read off the worker's receipt.
    prompt_tokens: int = 0
    #: Gateway's own count of the completion frames it delivered.
    tally: TokenTally = field(default_factory=TokenTally)


@dataclass
class GatewayResult:
    job_id: str
    model: str
    text: str
    tcdn: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = "stop"

    @property
    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


class Gateway:
    """Coordinator-side OpenAI relay. Sees prompts in transit; never stores them."""

    def __init__(
        self,
        coordinator: Coordinator,
        *,
        transport: WorkerTransport | None = None,
        clock: Callable[[], float] = now_unix,
    ) -> None:
        self.coordinator = coordinator
        self.settings = coordinator.settings
        self._transport = transport or http_worker_transport
        self._clock = clock

    # ── placement + session ────────────────────────────────────────────────

    async def open(self, auth: AuthContext, request: ChatRequest) -> GatewayStream:
        prompt_estimate = estimate_tokens(request.prompt_text)
        placement = await self.coordinator.place(
            auth,
            PlacementRequest(
                model=request.model,
                max_tokens=request.max_tokens,
                stream=request.stream,
                confidential=False,
                prompt_tokens_estimate=prompt_estimate,
            ),
        )

        # Verify our own ticket before using it: a bug or tampering in the
        # placement path must not turn into a request sent to an unverified
        # worker. No replay claim here — the worker owns single-use enforcement.
        ticket = self.coordinator.tickets.verify(
            placement.ticket,
            expected_job_id=placement.job_id,
            claim_replay=False,
            now=int(self._clock()),
        )
        if not crypto.constant_time_eq(
            ticket.worker_public_key, placement.worker_public_key
        ):
            raise ValidationError(
                "placement ticket does not match the returned worker key",
                code="ticket_worker_mismatch",
            )
        if ticket.confidentiality is not Confidentiality.NONE:
            # Belt and braces: gateway mode may never carry a confidential job.
            raise ValidationError(
                "gateway mode is standard tier only; use the TCDN-AI SDK for "
                "confidential jobs",
                code="confidential_requires_sdk",
            )
        if not placement.worker_endpoint:
            raise UnavailableError(
                "the assigned worker published no endpoint", code="no_worker_endpoint"
            )

        ticket_digest = crypto.sha256_hex(canonical_json(placement.ticket))
        ephemeral = crypto.generate_x25519()
        salt = os.urandom(32)
        shared = crypto.x25519_shared_secret(
            ephemeral["private_key"], ticket.worker_public_key
        )
        c2w, w2c = crypto.derive_session_keys(
            shared, salt=salt, context=session_context(placement.job_id)
        )

        request_plaintext = canonical_json(
            {
                "model": request.model,
                "messages": request.messages,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "top_p": request.top_p,
                "stop": request.stop,
                "stream": request.stream,
            }
        )
        ciphertext = crypto.aead_encrypt(
            c2w, request_plaintext, request_aad(placement.job_id, ticket_digest)
        )
        worker_payload = {
            "job_id": placement.job_id,
            "ticket": placement.ticket,
            "client_public_key": ephemeral["public_key"],
            "salt": crypto.b64e(salt),
            "ciphertext": crypto.b64e(ciphertext),
        }

        tcdn = {
            "job_id": placement.job_id,
            "node_id": ticket.node_id,
            "tier": ticket.tier,
            "confidentiality": placement.confidentiality.value,
            "confidentiality_notice": placement.confidentiality_notice,
            "mode": "gateway",
            "prompt_visibility": "gateway",
            "coordinator_key_id": placement.coordinator_key_id,
        }
        tally = TokenTally()
        events = self._counted(
            self._decrypt_frames(
                placement.worker_endpoint,
                worker_payload,
                job_id=placement.job_id,
                w2c=w2c,
            ),
            tally,
        )
        pending = await self._collect_verification(
            placement.job_id, request, request_plaintext
        )
        return GatewayStream(
            job_id=placement.job_id,
            model=request.model,
            tcdn=tcdn,
            events=events,
            pending_verification=pending,
            prompt_tokens=prompt_estimate,
            tally=tally,
        )

    @staticmethod
    async def _counted(
        events: AsyncGenerator[tuple[str, Any], None], tally: TokenTally
    ) -> AsyncGenerator[tuple[str, Any], None]:
        """Pass frames through while counting the tokens the gateway delivered."""
        async for kind, value in events:
            tally.add(kind)
            yield kind, value

    # ── redundant execution (checklist 6.1) ────────────────────────────────

    async def _collect_verification(
        self, job_id: str, request: ChatRequest, request_plaintext: bytes
    ) -> dict[str, Any] | None:
        """Fetch a verification assignment and build its encrypted request.

        The second run is a normal encrypted job for a different operator: it
        has its own job id and its own placement ticket, and reuses the same
        request plaintext. Any failure here is logged and dropped — a sampled
        verification must never fail the customer's completion.
        """
        try:
            assignment = await self.coordinator.verification_assignment(job_id)
            if not assignment:
                return None
            ticket = self.coordinator.tickets.verify(
                assignment["ticket"],
                expected_job_id=assignment["verification_job_id"],
                expected_node_id=assignment["node_id"],
                claim_replay=False,
                now=int(self._clock()),
            )
            ticket_digest = crypto.sha256_hex(canonical_json(assignment["ticket"]))
            ephemeral = crypto.generate_x25519()
            salt = os.urandom(32)
            shared = crypto.x25519_shared_secret(
                ephemeral["private_key"], ticket.worker_public_key
            )
            c2w, w2c = crypto.derive_session_keys(
                shared,
                salt=salt,
                context=session_context(assignment["verification_job_id"]),
            )
            ciphertext = crypto.aead_encrypt(
                c2w,
                request_plaintext,
                request_aad(assignment["verification_job_id"], ticket_digest),
            )
            payload = {
                "job_id": assignment["verification_job_id"],
                "ticket": assignment["ticket"],
                "client_public_key": ephemeral["public_key"],
                "salt": crypto.b64e(salt),
                "ciphertext": crypto.b64e(ciphertext),
            }
        except Exception:
            logger.warning("verification assignment for %s could not be built", job_id)
            await self.coordinator.fail_verification(job_id, "assignment_build_failed")
            return None
        return {
            "job_id": job_id,
            "verification_job_id": assignment["verification_job_id"],
            "endpoint": assignment["endpoint"],
            "payload": payload,
            "w2c": w2c,
        }

    async def run_verification(self, pending: dict[str, Any]) -> None:
        """Send the verification run, collect its receipt and report it.

        Only the final frame matters: the second completion is never billed,
        forwarded to the customer, or logged. Its receipt's ``output_sha256`` is
        what the fault-attribution comparison consumes.
        """
        receipt: dict[str, Any] | None = None
        try:
            buffer = b""
            sequence = 0
            async for chunk in self._transport(pending["endpoint"], pending["payload"]):
                buffer += chunk
                frames, buffer = decode_frames(buffer)
                for frame in frames:
                    if frame.kind == TOKEN_FRAME:
                        # Decrypted only to keep the AEAD sequence in step; the
                        # plaintext is discarded, never persisted.
                        crypto.aead_decrypt(
                            pending["w2c"],
                            frame.body,
                            stream_aad(pending["verification_job_id"], sequence),
                        )
                        sequence += 1
                    elif frame.kind == FINAL_FRAME:
                        receipt = json.loads(frame.body.decode("utf-8"))
        except Exception:
            logger.warning(
                "verification run for %s did not complete", pending["job_id"]
            )
            await self.coordinator.fail_verification(
                pending["job_id"], "verification_transport_failed"
            )
            return
        if not receipt:
            await self.coordinator.fail_verification(
                pending["job_id"], "verification_receipt_missing"
            )
            return
        try:
            envelope = SignedEnvelope(
                payload=receipt.get("payload", {}),
                key_id=receipt.get("key_id", ""),
                alg=receipt.get("alg", "Ed25519"),
                sig=receipt.get("sig", ""),
            )
            await self.coordinator.submit_verification_receipt(envelope)
        except Exception:
            logger.warning(
                "verification receipt for %s was rejected", pending["job_id"]
            )
            await self.coordinator.fail_verification(
                pending["job_id"], "verification_receipt_rejected"
            )

    async def _decrypt_frames(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        job_id: str,
        w2c: bytes,
    ) -> AsyncGenerator[tuple[str, Any], None]:
        buffer = b""
        sequence = 0
        async for chunk in self._transport(endpoint, payload):
            buffer += chunk
            frames, buffer = decode_frames(buffer)
            for frame in frames:
                if frame.kind == TOKEN_FRAME:
                    plaintext = crypto.aead_decrypt(
                        w2c, frame.body, stream_aad(job_id, sequence)
                    )
                    sequence += 1
                    yield ("token", plaintext.decode("utf-8"))
                elif frame.kind == FINAL_FRAME:
                    # The receipt carries no prompt or output content.
                    yield ("final", json.loads(frame.body.decode("utf-8")))

    # ── completion helpers ─────────────────────────────────────────────────

    async def complete(self, auth: AuthContext, stream: GatewayStream) -> GatewayResult:
        pieces: list[str] = []
        receipt: dict[str, Any] = {}
        async for kind, value in stream.events:
            if kind == "token":
                pieces.append(value)
            else:
                receipt = value
        text = "".join(pieces)
        self._start_verification(stream)
        usage = await self._finalize(auth, stream, text, receipt)
        return GatewayResult(
            job_id=stream.job_id,
            model=stream.model,
            text=text,
            tcdn=stream.tcdn,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
        )

    async def _finalize(
        self,
        auth: AuthContext,
        stream: GatewayStream,
        text: str,
        receipt: dict[str, Any],
    ) -> dict[str, int]:
        """Hand the completion over and record the gateway's OWN measurement.

        The counts are the gateway's: the prompt is the plaintext it encrypted
        and sent, the completion is the plaintext it decrypted and delivered
        (the token frames it streamed, floored by an estimate of the text). It
        used to read ``output_tokens`` out of the worker's receipt and hand that
        straight back, which made the worker the author of its own bill — and
        the customer the author of nothing that could contradict it.
        """
        completion_tokens = max(stream.tally.frames, estimate_tokens(text))
        prompt_tokens = stream.prompt_tokens
        output_sha = crypto.sha256_hex(text.encode("utf-8"))
        await self.coordinator.submit_gateway_measurement(
            stream.job_id,
            GatewayMeasurement(
                job_id=stream.job_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                token_frames=stream.tally.frames,
                output_sha256=output_sha,
                measured_at=int(self._clock()),
            ),
        )
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    async def finalize(
        self,
        auth: AuthContext,
        stream: GatewayStream,
        text: str,
        receipt: dict[str, Any],
    ) -> dict[str, int]:
        """Public wrapper used by the streaming route after the last frame."""
        self._start_verification(stream)
        return await self._finalize(auth, stream, text, receipt)

    def _start_verification(self, stream: GatewayStream) -> None:
        """Dispatch a sampled verification in the background, at most once.

        Background on purpose: the second inference must not add latency to the
        customer's own completion. The task is kept on the stream so tests can
        await it and a future shutdown hook can drain it.
        """
        pending = stream.pending_verification
        if pending is None or stream.verification_task is not None:
            return
        stream.verification_task = asyncio.create_task(self.run_verification(pending))


def completion_body(result: GatewayResult) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{result.job_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text},
                "logprobs": None,
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": result.usage,
        "tcdn": result.tcdn,
    }


def chunk_body(
    *,
    job_id: str,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": f"chatcmpl-{job_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": (
            []
            if usage is not None and not delta and finish_reason is None
            else [
                {
                    "index": 0,
                    "delta": delta,
                    "logprobs": None,
                    "finish_reason": finish_reason,
                }
            ]
        ),
    }
    if usage is not None:
        body["usage"] = usage
    return body


def model_list_body(models: list[Any]) -> dict[str, Any]:
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": model.model_id,
                "object": "model",
                "created": created,
                "owned_by": "tcdn-ai",
                "tcdn": {
                    "name": model.name,
                    "input_micro_per_mtok": model.input_micro_per_mtok,
                    "output_micro_per_mtok": model.output_micro_per_mtok,
                    "confidential_input_micro_per_mtok": model.confidential_input_micro_per_mtok,
                    "confidential_output_micro_per_mtok": model.confidential_output_micro_per_mtok,
                    "license": model.license_note,
                    "launch_pricing_until": model.launch_pricing_until,
                    "gateway_mode": True,
                },
            }
            for model in models
        ],
    }


__all__ = [
    "ChatRequest",
    "Gateway",
    "GatewayResult",
    "GatewayStream",
    "chunk_body",
    "completion_body",
    "estimate_tokens",
    "http_worker_transport",
    "model_list_body",
    "parse_chat_request",
]
