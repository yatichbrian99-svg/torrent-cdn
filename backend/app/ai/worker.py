"""GPU worker agent.

The worker is untrusted with respect to confidentiality in the standard tier and
is the *only* party that sees plaintext prompts in either tier. Its obligations:

* validate the coordinator's placement ticket (signature, expiry, single use,
  node binding) before doing any work;
* refuse a ``confidential`` ticket unless this worker is itself running in an
  attested environment — a compromised coordinator cannot downgrade it;
* decrypt the request with the published session key, enforce the ticket's
  limits, and never write prompts or outputs to disk or logs;
* stream output encrypted per chunk, then sign an execution receipt with its
  identity key and zeroise the buffers it used.

Inference backends are HTTP clients to a **loopback** engine. ``EchoBackend`` is
a labelled development stub and refuses to construct when
``TCDN_AI_ALLOW_DEV_STUB_BACKEND`` is false (production validation forces it).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from tcdn_ai_sdk.protocol import (
    FINAL_FRAME,
    TOKEN_FRAME,
    b64d,
    canonical_json,
    encode_frame,
    request_aad,
    session_context,
    stream_aad,
)

from . import crypto
from .clock import now_unix
from .config import AISettings
from .errors import AttestationError, ValidationError
from .keyring import KeyRing
from .models import Confidentiality
from .receipts import sign_receipt
from .tickets import TicketService
from .transcripts import EncryptedTranscriptStore, TranscriptRecorder, prompt_hash
from .verification2 import TranscriptRecord, VerificationReceiptExt

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_MESSAGE_CHARS = 200_000


class InferenceBackend(Protocol):
    name: str
    last_usage: dict[str, int]

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: list[str],
    ) -> AsyncIterator[str]: ...


def _assert_loopback(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.hostname not in LOOPBACK_HOSTS:
        raise ValidationError(
            "inference backends must run on loopback; refusing a remote engine URL",
            code="backend_not_loopback",
        )


class EchoBackend:
    """Deterministic development backend. Labelled everywhere it appears."""

    name = "dev-stub"

    def __init__(self, *, allow: bool) -> None:
        if not allow:
            raise RuntimeError(
                "EchoBackend is a development stub and is disabled in this environment"
            )
        self.last_usage: dict[str, int] = {}
        self.last_transcript: tuple[TranscriptRecord, ...] = ()

    @property
    def transcript_source(self) -> str:
        return "simulated"

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: list[str],
    ) -> AsyncIterator[str]:
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        words = f"echo[{model}]: {last_user}".split()
        emitted = words[: max(1, min(max_tokens, 64))]
        recorder = TranscriptRecorder()
        self.last_transcript = ()
        for index, word in enumerate(emitted):
            token_id = int(hashlib.sha256(f"{index}:{word}".encode()).hexdigest()[:8], 16) % 32_000
            chosen = -0.5 - 0.01 * (index % 12)
            recorder.record(
                token_id=token_id,
                chosen_logprob=chosen,
                top_k=(
                    (token_id, chosen),
                    ((token_id + 1) % 32_000, chosen - 0.7),
                    ((token_id + 2) % 32_000, chosen - 1.4),
                ),
            )
            yield word if index == 0 else f" {word}"
        self.last_transcript = recorder.records
        prompt_tokens = sum(len(m["content"]) for m in messages) // 4
        self.last_usage = {
            "input_tokens": max(1, prompt_tokens),
            "output_tokens": len(emitted),
        }


class _HttpBackend:
    name = "http"
    upstream_path = ""

    transcript_source = "engine"

    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        _assert_loopback(base_url)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.last_usage: dict[str, int] = {}
        self.last_transcript: tuple[TranscriptRecord, ...] = ()


class OllamaBackend(_HttpBackend):
    name = "ollama"
    upstream_path = "/api/chat"

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: list[str],
    ) -> AsyncIterator[str]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            # Ollama returns per-token logprobs on recent versions; older ones
            # ignore the fields, which leaves the job unverifiable rather than
            # fabricating a transcript.
            "logprobs": True,
            "top_logprobs": 5,
            "options": {
                "temperature": temperature,
                "top_p": top_p,
                "num_predict": max_tokens,
                "stop": stop,
            },
        }
        recorder = TranscriptRecorder()
        complete = True
        self.last_transcript = ()
        try:
            async with (
                httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client,
                client.stream("POST", self.upstream_path, json=payload) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    content = event.get("message", {}).get("content")
                    if content:
                        yield content
                    logprobs = event.get("logprobs")
                    if isinstance(logprobs, list) and logprobs:
                        try:
                            recorder.extend_from_logprobs(logprobs)
                        except (TypeError, ValueError):
                            complete = False
                    if event.get("done"):
                        self.last_usage = {
                            "input_tokens": int(event.get("prompt_eval_count", 0)),
                            "output_tokens": int(event.get("eval_count", 0)),
                        }
                        return
        finally:
            self.last_transcript = recorder.records if complete else ()


class VLLMBackend(_HttpBackend):
    name = "vllm"
    upstream_path = "/v1/chat/completions"

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: list[str],
    ) -> AsyncIterator[str]:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stop": stop,
            "stream": True,
            "stream_options": {"include_usage": True},
            "logprobs": True,
            "top_logprobs": 5,
        }
        recorder = TranscriptRecorder()
        complete = True
        self.last_transcript = ()
        try:
            async with (
                httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client,
                client.stream("POST", self.upstream_path, json=payload) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    event = json.loads(data)
                    if event.get("usage"):
                        self.last_usage = {
                            "input_tokens": int(event["usage"].get("prompt_tokens", 0)),
                            "output_tokens": int(event["usage"].get("completion_tokens", 0)),
                        }
                    for choice in event.get("choices", []):
                        content = choice.get("delta", {}).get("content")
                        if content:
                            yield content
                        logprobs = (choice.get("logprobs") or {}).get("content") or []
                        if logprobs:
                            try:
                                recorder.extend_from_logprobs(logprobs)
                            except (TypeError, ValueError):
                                complete = False
        finally:
            self.last_transcript = recorder.records if complete else ()


@dataclass
class WorkerIdentity:
    node_id: str
    identity_public_key: str
    identity_private_key: str
    session_public_key: str
    session_private_key: str
    session_created_at: int

    @classmethod
    def generate(cls, node_id: str, *, now: int | None = None) -> WorkerIdentity:
        identity = crypto.generate_ed25519()
        session = crypto.generate_x25519()
        return cls(
            node_id=node_id,
            identity_public_key=identity["public_key"],
            identity_private_key=identity["private_key"],
            session_public_key=session["public_key"],
            session_private_key=session["private_key"],
            session_created_at=int(now if now is not None else now_unix()),
        )

    @property
    def identity_key_id(self) -> str:
        return crypto.key_id_for(self.identity_public_key)

    def rotate_session(self, *, now: int | None = None) -> dict[str, str]:
        session = crypto.generate_x25519()
        self.session_public_key = session["public_key"]
        self.session_private_key = session["private_key"]
        self.session_created_at = int(now if now is not None else now_unix())
        # Signed by the identity key so a coordinator/SDK cannot be fed a session
        # key the worker does not hold.
        document = self.sign_session_document()
        return document

    def sign_session_document(self) -> dict[str, Any]:
        payload = {
            "node_id": self.node_id,
            "session_public_key": self.session_public_key,
            "issued_at": self.session_created_at,
        }
        return {
            "key_id": self.identity_key_id,
            "alg": "Ed25519",
            "domain": "tcdn-ai/session-key/v1",
            "payload": payload,
            "sig": crypto.sign("tcdn-ai/session-key/v1", payload, self.identity_private_key),
        }

    def needs_rotation(self, *, rotate_seconds: int, now: int | None = None) -> bool:
        return int(now if now is not None else now_unix()) - self.session_created_at >= rotate_seconds


@dataclass
class WorkerConfig:
    models: dict[str, str]  # model_id -> manifest_hash
    confidentiality: Confidentiality = Confidentiality.NONE
    attested: bool = False
    max_tokens: int = 4096
    max_messages: int = 64
    max_message_chars: int = MAX_MESSAGE_CHARS
    # Declared execution environment, committed into every verification receipt
    # so the verifier can pin the matching tolerance profile. "unknown" is
    # honest about an undeclared worker and must be refused by calibration.
    engine_version: str = "unknown"
    dtype: str = "unknown"
    quantization: str = "unknown"
    gpu_class: str = "unknown"
    model_versions: dict[str, str] = field(default_factory=dict)
    attestation_ref: str | None = None
    instance_key_id: str | None = None


class WorkerAgent:
    def __init__(
        self,
        *,
        identity: WorkerIdentity,
        config: WorkerConfig,
        backend: InferenceBackend,
        coordinator_keys: KeyRing,
        settings: AISettings,
        clock=now_unix,
    ) -> None:
        self.identity = identity
        self.config = config
        self.backend = backend
        self.settings = settings
        self.clock = clock
        self.tickets = TicketService(
            coordinator_keys,
            ttl_seconds=settings.ticket_ttl_seconds,
            skew_seconds=settings.ticket_skew_seconds,
            clock=clock,
        )
        # Per-token records are kept encrypted for the verification window and
        # wiped at expiry. A restart loses the window (documented limitation).
        self.transcripts = EncryptedTranscriptStore(
            identity.identity_private_key,
            ttl_seconds=settings.transcript_retention_seconds,
            clock=clock,
        )
        # Optional: submit receipts to the coordinator as soon as a job finishes.
        # Disabled in unit tests (which assert the coordinator-side flow directly)
        # and enabled by scripts/ai_worker.py.
        self.reporting: tuple[str, str] | None = None
        if config.confidentiality is Confidentiality.ATTESTED_TEE and not config.attested:
            # A confidential worker that has not attested can exist, but it will
            # refuse confidential tickets at process time. This is a startup
            # warning, not a silent downgrade.
            logger.warning(
                "worker %s is configured confidential but has no attestation; "
                "confidential tickets will be refused",
                identity.node_id,
            )

    # ── request handling ───────────────────────────────────────────────────

    async def process(self, payload: dict[str, Any]) -> bytes:
        """Collect the whole framed response (used by tests and non-streaming callers)."""
        return b"".join([frame async for frame in self.stream_frames(payload)])

    def _validate_request(self, request: dict[str, Any], ticket: Any) -> None:
        if request.get("model") != ticket.model:
            raise ValidationError("request model does not match the ticket", code="model_mismatch")
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValidationError("messages must be a non-empty list")
        if len(messages) > self.config.max_messages:
            raise ValidationError("too many messages", code="too_many_messages")
        total_chars = 0
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {
                "system", "user", "assistant",
            }:
                raise ValidationError("malformed message")
            content = message.get("content")
            if not isinstance(content, str):
                raise ValidationError("message content must be a string")
            total_chars += len(content)
        if total_chars > self.config.max_message_chars:
            raise ValidationError("prompt is too large", code="prompt_too_large")
        max_tokens = request.get("max_tokens")
        if not isinstance(max_tokens, int) or max_tokens < 1:
            raise ValidationError("max_tokens must be a positive integer")
        if max_tokens > ticket.max_tokens or max_tokens > self.config.max_tokens:
            raise ValidationError("max_tokens exceeds the ticket allowance", code="token_limit_exceeded")

    async def stream_frames(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        """Yield encrypted token frames as the engine produces them, then the receipt.

        Streaming matters for two reasons: the client sees the first token without
        waiting for the whole completion, and the worker never has to hold the full
        output in memory. The receipt is emitted last and is the only unencrypted
        frame — it contains no prompt or output text.
        """
        job_id = payload.get("job_id")
        ticket_document = payload.get("ticket")
        if not isinstance(job_id, str) or not isinstance(ticket_document, dict):
            raise ValidationError("malformed worker request")
        ticket = self.tickets.verify(
            ticket_document,
            expected_job_id=job_id,
            expected_node_id=self.identity.node_id,
        )
        if ticket.confidentiality is Confidentiality.ATTESTED_TEE and not self.config.attested:
            raise AttestationError(
                "this worker has no attestation and cannot accept confidential jobs",
                code="worker_not_attested",
            )
        if not crypto.constant_time_eq(ticket.worker_public_key, self.identity.session_public_key):
            raise ValidationError(
                "ticket was issued for a stale worker session key", code="stale_session_key"
            )
        if ticket.model not in self.config.models:
            raise ValidationError("model is not available on this worker", code="model_unavailable")

        salt = b64d(str(payload.get("salt", "")))
        client_public_key = str(payload.get("client_public_key", ""))
        ciphertext = b64d(str(payload.get("ciphertext", "")))
        shared = crypto.x25519_shared_secret(self.identity.session_private_key, client_public_key)
        c2w, w2c = crypto.derive_session_keys(
            shared, salt=salt, context=session_context(job_id)
        )
        ticket_digest = crypto.sha256_hex(canonical_json(ticket_document))
        plaintext = crypto.aead_decrypt(
            c2w, ciphertext, request_aad(job_id, ticket_digest)
        )
        buffer = bytearray(plaintext)
        try:
            request = json.loads(plaintext.decode("utf-8"))
        finally:
            crypto.zeroise(buffer)
            del plaintext
        self._validate_request(request, ticket)
        started_at = int(self.clock())

        digest = hashlib.sha256()
        sequence = 0
        output_chars = 0
        async for text in self.backend.stream(
            model=request["model"],
            messages=request["messages"],
            max_tokens=int(request["max_tokens"]),
            temperature=float(request.get("temperature", 0.7)),
            top_p=float(request.get("top_p", 1.0)),
            stop=list(request.get("stop") or []),
        ):
            if not isinstance(text, str) or not text:
                continue
            output_chars += len(text)
            encoded = text.encode("utf-8")
            digest.update(encoded)
            ciphertext_chunk = crypto.aead_encrypt(w2c, encoded, stream_aad(job_id, sequence))
            sequence += 1
            yield encode_frame(TOKEN_FRAME, ciphertext_chunk)

        usage = getattr(self.backend, "last_usage", {}) or {}
        input_tokens = int(usage.get("input_tokens", 0)) or max(
            1, sum(len(m["content"]) for m in request["messages"]) // 4
        )
        output_tokens = int(usage.get("output_tokens", 0)) or max(1, output_chars // 4)

        records = tuple(getattr(self.backend, "last_transcript", ()) or ())
        if records and output_tokens and len(records) != output_tokens:
            # The commitment must line up 1:1 with the billed token count; a
            # partial logprob stream cannot be verified, so it is not committed.
            logger.warning(
                "dropping transcript commitment: %d records vs %d engine tokens",
                len(records),
                output_tokens,
            )
            records = ()
        verification = None
        if records:
            verification = VerificationReceiptExt.build(
                receipt_id=crypto.new_session_nonce(),
                engine=getattr(self.backend, "name", "unknown"),
                engine_version=self.config.engine_version,
                dtype=self.config.dtype,
                quantization=self.config.quantization,
                gpu_class=self.config.gpu_class,
                sampling_params={
                    "temperature": float(request.get("temperature", 0.7)),
                    "top_p": float(request.get("top_p", 1.0)),
                    "top_k": request.get("top_k"),
                    "seed": request.get("seed"),
                    "stop": list(request.get("stop") or []),
                    "max_tokens": int(request["max_tokens"]),
                },
                prompt_hash=prompt_hash(job_id, request["messages"]),
                transcript_records=list(records),
                model_version=self.config.model_versions.get(request["model"]),
                attestation_ref=self.config.attestation_ref,
                instance_key_id=self.config.instance_key_id,
                transcript_source=getattr(self.backend, "transcript_source", "engine"),
            )
            try:
                self.transcripts.put(job_id, records)
            except (ValueError, crypto.CryptoError):
                # Without the encrypted records the commitment can never be
                # opened, so publishing the root would only create false
                # evidence of verifiability.
                logger.warning("transcript storage failed; receipt will not commit")
                verification = None

        receipt_payload = {
            "job_id": job_id,
            "node_id": self.identity.node_id,
            "account_id": ticket.account_id,
            "model": request["model"],
            "manifest_hash": self.config.models[request["model"]],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "output_sha256": digest.hexdigest(),
            "started_at": started_at,
            "finished_at": int(self.clock()),
        }
        if verification is not None:
            receipt_payload["verification"] = verification
        receipt = sign_receipt(receipt_payload, self.identity.identity_private_key)
        if self.reporting is not None:
            coordinator_url, api_key = self.reporting
            try:
                await self.report_receipt(
                    receipt, coordinator_url=coordinator_url, api_key=api_key
                )
            except Exception as exc:  # the SDK ack can still settle after a retry
                logger.warning("receipt submission failed: %s", type(exc).__name__)
        yield encode_frame(FINAL_FRAME, canonical_json(receipt))


    def transcript_for(self, job_id: str) -> list[TranscriptRecord]:
        """Decrypt the records for a job (challenge-response path, milestone 2)."""
        return self.transcripts.get(job_id)

    def wipe_transcript(self, job_id: str) -> None:
        self.transcripts.wipe(job_id)

    def enable_reporting(self, coordinator_url: str, api_key: str) -> None:
        self.reporting = (coordinator_url.rstrip("/"), api_key)

    async def report_receipt(
        self, receipt: dict[str, Any], *, coordinator_url: str, api_key: str = ""
    ) -> dict[str, Any]:
        """Submit the signed receipt to the coordinator.

        No API key is required (the worker's signature is the authority), but a
        deployment may pass one to put the endpoint behind a gateway policy.
        """
        job_id = receipt.get("payload", {}).get("job_id")
        if not isinstance(job_id, str):
            raise ValidationError("receipt has no job id")
        envelope = {
            "payload": receipt["payload"],
            "key_id": receipt["key_id"],
            "alg": receipt["alg"],
            "sig": receipt["sig"],
        }
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{coordinator_url.rstrip('/')}/api/ai/v1/jobs/{job_id}/receipt",
                json=envelope,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()


def create_worker_app(agent: WorkerAgent):
    """Minimal worker HTTP surface (TLS is terminated by the launcher)."""
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    app = FastAPI(title="TCDN-AI worker", docs_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "node_id": agent.identity.node_id,
            "backend": agent.backend.name,
            "models": sorted(agent.config.models),
            "confidentiality": agent.config.confidentiality.value,
            "attested": agent.config.attested,
            "session_key_id": crypto.key_id_for(agent.identity.session_public_key),
        }

    @app.post("/v1/jobs")
    async def submit_job(payload: dict) -> StreamingResponse:
        # True streaming: token frames are flushed as the engine emits them.
        return StreamingResponse(
            agent.stream_frames(payload), media_type="application/octet-stream"
        )

    return app


def load_worker_identity(path: str) -> WorkerIdentity:
    """Load a 0600 JSON identity file (created on first boot by the operator)."""
    import os
    import stat

    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise PermissionError("worker identity file must be chmod 600")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return WorkerIdentity(
        node_id=data["node_id"],
        identity_public_key=data["identity_public_key"],
        identity_private_key=data["identity_private_key"],
        session_public_key=data["session_public_key"],
        session_private_key=data["session_private_key"],
        session_created_at=int(data["session_created_at"]),
    )


def save_worker_identity(path: str, identity: WorkerIdentity) -> None:
    import os

    payload = json.dumps(
        {
            "node_id": identity.node_id,
            "identity_public_key": identity.identity_public_key,
            "identity_private_key": identity.identity_private_key,
            "session_public_key": identity.session_public_key,
            "session_private_key": identity.session_private_key,
            "session_created_at": identity.session_created_at,
        },
        indent=2,
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)

