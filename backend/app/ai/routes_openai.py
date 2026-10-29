"""OpenAI-compatible HTTP surface: ``POST /v1/chat/completions``, ``GET /v1/models``.

Mounted at the application root (not under ``/api/ai/v1``) so an unmodified
OpenAI client works with ``base_url="https://www.torrentcdn.store/v1"``. The
router is mounted only when ``ENABLE_TCDN_AI`` is on.

Errors are OpenAI-shaped (``{"error": {"message", "type", "param", "code"}}``)
on this surface only; the native ``/api/ai/v1`` surface keeps its own stable
shape. Every request is authenticated with an AI API key (``Authorization:
Bearer``) and shares the native surface's rate-limit buckets.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

from . import gateway as gateway_module
from .errors import AIError, UnavailableError
from .gateway import (
    Gateway,
    chunk_body,
    completion_body,
    model_list_body,
    parse_chat_request,
)
from .routes import authenticate, get_coordinator
from .service import AuthContext, Coordinator

logger = logging.getLogger(__name__)

#: Test hook: replace the worker transport (mirrors ``set_coordinator_for_tests``).
_transport_override: gateway_module.WorkerTransport | None = None


def set_worker_transport_for_tests(
    transport: gateway_module.WorkerTransport | None,
) -> None:
    global _transport_override
    _transport_override = transport


def _gateway(coordinator: Coordinator) -> Gateway:
    return Gateway(coordinator, transport=_transport_override)


# ── OpenAI-shaped errors ────────────────────────────────────────────────────

_STATUS_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "insufficient_quota",
    403: "permission_error",
    404: "not_found_error",
    409: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    503: "server_error",
}


def openai_error_payload(exc: AIError) -> dict[str, Any]:
    # OpenAI uses 400 for validation problems; our native surface uses 422. The
    # mapping is deliberate and documented in docs/tcdn-ai/API.md.
    status = 400 if exc.status_code == 422 else exc.status_code
    return {
        "message": exc.message,
        "type": _STATUS_TYPES.get(status, "invalid_request_error"),
        "param": exc.details.get("param"),
        "code": exc.code,
    }


def openai_error_response(exc: AIError) -> JSONResponse:
    status = 400 if exc.status_code == 422 else exc.status_code
    headers = {}
    retry_after = exc.details.get("retry_after")
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=status,
        content={"error": openai_error_payload(exc)},
        headers=headers,
    )


def _validation_error_response(exc: RequestValidationError) -> JSONResponse:
    # Do not echo input values: a malformed body can contain prompt text and a
    # reflected prompt in an error surface is still an unnecessary disclosure.
    first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": "invalid request"}
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": str(first.get("msg", "invalid request")),
                "type": "invalid_request_error",
                "param": location or None,
                "code": "invalid_request",
            }
        },
    )


class _OpenAIRoute(APIRoute):
    """Render AIError and body-validation failures in OpenAI's error shape."""

    def get_route_handler(self):  # type: ignore[override]
        original = super().get_route_handler()

        async def handler(request: Request):  # type: ignore[no-untyped-def]
            try:
                return await original(request)
            except AIError as exc:
                return openai_error_response(exc)
            except RequestValidationError as exc:
                return _validation_error_response(exc)

        return handler


router = APIRouter(tags=["tcdn-ai-openai"], route_class=_OpenAIRoute)


def _headers(
    job_id: str | None = None, confidentiality: str | None = None
) -> dict[str, str]:
    headers = {"Cache-Control": "no-store"}
    if job_id:
        headers["X-TCDN-Job-Id"] = job_id
    if confidentiality:
        headers["X-TCDN-Confidentiality"] = confidentiality
    return headers


# ── routes ──────────────────────────────────────────────────────────────────


@router.get("/v1/models")
async def list_models(
    auth: Annotated[AuthContext, Depends(authenticate)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    Coordinator.require_scope(auth, "inference")
    return model_list_body(await coordinator.list_models())


def _sse(payload: dict[str, Any] | str) -> str:
    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    auth: Annotated[AuthContext, Depends(authenticate)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
):
    raw = await request.body()
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise AIError("request body is not valid JSON", code="invalid_json") from exc

    # Gateway mode is always the standard tier. Unsupported parameters (tools,
    # JSON mode, logprobs, ...) are rejected by parse_chat_request rather than
    # silently dropped.
    parsed = parse_chat_request(body, coordinator.settings)
    gateway = _gateway(coordinator)

    if not parsed.stream:
        stream = await gateway.open(auth, parsed)
        result = await gateway.complete(auth, stream)
        return JSONResponse(
            completion_body(result),
            headers=_headers(result.job_id, result.tcdn["confidentiality"]),
        )

    stream = await gateway.open(auth, parsed)
    events = stream.events

    # Pull the first frame before committing to a 200 response, so a placement,
    # connection, or ticket failure is a real HTTP error the OpenAI client can
    # surface — not a 200 with an error hidden inside the stream.
    try:
        first = await anext(events)
    except StopAsyncIteration:
        await events.aclose()
        raise UnavailableError(
            "worker returned no response", code="worker_empty_response"
        ) from None
    except AIError:
        await events.aclose()
        raise

    async def event_stream() -> AsyncIterator[str]:
        pieces: list[str] = []
        receipt: dict[str, Any] = {}
        yield _sse(
            chunk_body(
                job_id=stream.job_id,
                model=stream.model,
                delta={"role": "assistant", "content": ""},
            )
        )
        item: tuple[str, Any] | None = first
        try:
            while item is not None:
                kind, value = item
                if kind == "token":
                    pieces.append(value)
                    yield _sse(
                        chunk_body(
                            job_id=stream.job_id,
                            model=stream.model,
                            delta={"content": value},
                        )
                    )
                elif kind == "final":
                    receipt = value
                item = await anext(events, None)
        except AIError as exc:
            # Headers are already sent: the only honest signal left is an error
            # event. The job is not acked, so it never settles.
            yield _sse({"error": openai_error_payload(exc)})
            yield _sse("[DONE]")
            return
        except Exception as exc:
            logger.warning("gateway stream failed: %s", type(exc).__name__)
            yield _sse(
                {
                    "error": {
                        "message": "worker stream failed",
                        "type": "server_error",
                        "param": None,
                        "code": "worker_stream_failed",
                    }
                }
            )
            yield _sse("[DONE]")
            return

        if not receipt:
            # A clean end of stream with no final frame is a worker that died
            # mid-answer. Headers are sent, so the error event is the only
            # signal left; the job has no receipt and therefore never settles.
            yield _sse(
                {
                    "error": {
                        "message": "worker stream ended before its receipt",
                        "type": "server_error",
                        "param": None,
                        "code": "worker_stream_incomplete",
                    }
                }
            )
            yield _sse("[DONE]")
            return

        text = "".join(pieces)
        try:
            usage = await gateway.finalize(auth, stream, text, receipt)
        except AIError as exc:
            yield _sse({"error": openai_error_payload(exc)})
            yield _sse("[DONE]")
            return

        yield _sse(
            chunk_body(
                job_id=stream.job_id,
                model=stream.model,
                delta={},
                finish_reason="stop",
            )
        )
        if parsed.include_usage:
            yield _sse(
                chunk_body(
                    job_id=stream.job_id, model=stream.model, delta={}, usage=usage
                )
            )
        yield _sse("[DONE]")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            **_headers(stream.job_id, stream.tcdn["confidentiality"]),
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["openai_error_payload", "router", "set_worker_transport_for_tests"]
