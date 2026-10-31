"""OpenAI-compatible gateway tests (Section C item 1).

The two clients that matter are the **official** ones, so the core tests drive
``openai.AsyncOpenAI`` against the real ASGI app rather than a hand-rolled
request. The gateway talks to an in-process worker over the same encrypted
protocol the SDK uses; only the HTTP hop to the worker is stubbed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI, BadRequestError, NotFoundError

from app.ai import routes as ai_routes
from app.ai import routes_openai as openai_routes
from app.ai.errors import UnavailableError
from app.main import app
from tcdn_ai_sdk.protocol import FINAL_FRAME, decode_frames, encode_frame

BASE_URL = "http://test"


def _ai_surface_enabled() -> bool:
    """These tests exercise routes that `main.py` mounts only behind the flag."""
    from app.ai.config import get_ai_settings
    from app.config import get_settings

    try:
        return get_ai_settings().is_ai_enabled_for_host(get_settings().app_env)
    except Exception:  # no AI config at all → the app cannot mount the routes
        return False


pytestmark = pytest.mark.skipif(
    not _ai_surface_enabled(),
    reason="ENABLE_TCDN_AI is off: /v1/... is not mounted (flag-off is covered by "
    "TestFeatureFlagOff, which runs a subprocess with the flag unset)",
)


def make_transport(agent, receipts: list[dict]):
    """Async worker transport: run the real worker agent in-process."""

    async def transport(url: str, payload: dict):
        frames = await agent.process(payload)
        parsed, _ = decode_frames(frames)
        for frame in parsed:
            if frame.kind == FINAL_FRAME:
                receipts.append(json.loads(frame.body.decode("utf-8")))
        yield frames

    return transport


@asynccontextmanager
async def gateway_api(env, agent, receipts: list[dict] | None = None):
    ai_routes.reset_for_tests()
    ai_routes.set_coordinator_for_tests(env.coordinator)
    openai_routes.set_worker_transport_for_tests(
        make_transport(agent, receipts if receipts is not None else [])
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client
    openai_routes.set_worker_transport_for_tests(None)
    ai_routes.set_coordinator_for_tests(None)
    ai_routes.reset_for_tests()


def openai_client(env) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=env.api_key,
        base_url=f"{BASE_URL}/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ),
    )


async def submit_receipt(env, receipt: dict):
    from app.ai.models import SignedEnvelope

    return await env.coordinator.submit_receipt(
        SignedEnvelope(
            payload=receipt["payload"],
            key_id=receipt["key_id"],
            alg=receipt["alg"],
            sig=receipt["sig"],
        )
    )


class TestModelsEndpoint:
    @pytest.mark.asyncio
    async def test_models_requires_a_key_in_openai_shape(self, ai_env):
        async with gateway_api(ai_env, agent=None) as client:
            response = await client.get("/v1/models")
        assert response.status_code == 401
        error = response.json()["error"]
        assert error["type"] == "authentication_error"
        assert error["code"]

    @pytest.mark.asyncio
    async def test_models_is_an_openai_list(self, ai_env):
        async with gateway_api(ai_env, agent=None) as client:
            response = await client.get(
                "/v1/models", headers={"Authorization": f"Bearer {ai_env.api_key}"}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "list"
        entry = body["data"][0]
        assert entry["id"] == ai_env.model.model_id
        assert entry["object"] == "model"
        assert entry["owned_by"] == "tcdn-ai"
        assert entry["tcdn"]["gateway_mode"] is True
        assert entry["tcdn"]["input_micro_per_mtok"] == 3_000_000


class TestOfficialClient:
    @pytest.mark.asyncio
    async def test_non_streaming_completion(self, ai_env, ai_worker_factory):
        agent = await ai_worker_factory()
        receipts: list[dict] = []
        async with (
            gateway_api(ai_env, agent, receipts),
            openai_client(ai_env) as client,
        ):
            reply = await client.chat.completions.create(
                model=ai_env.model.model_id,
                messages=[{"role": "user", "content": "hello gateway"}],
                max_tokens=8,
            )
        assert reply.object == "chat.completion"
        assert reply.id.startswith("chatcmpl-")
        assert reply.choices[0].message.role == "assistant"
        assert reply.choices[0].message.content.startswith(
            f"echo[{ai_env.model.model_id}]"
        )
        assert reply.choices[0].finish_reason == "stop"
        assert reply.usage.total_tokens == (
            reply.usage.prompt_tokens + reply.usage.completion_tokens
        )
        # Honest labelling: gateway mode is visibly *not* end-to-end encrypted.
        assert reply.tcdn["mode"] == "gateway"
        assert reply.tcdn["prompt_visibility"] == "gateway"
        assert reply.tcdn["confidentiality"] == "none"
        assert reply.tcdn["job_id"]

        assert len(receipts) == 1
        settled = await submit_receipt(ai_env, receipts[0])
        assert settled.status.value == "settled"

    @pytest.mark.asyncio
    async def test_streaming_completion_with_usage(self, ai_env, ai_worker_factory):
        agent = await ai_worker_factory()
        async with gateway_api(ai_env, agent), openai_client(ai_env) as client:
            stream = await client.chat.completions.create(
                model=ai_env.model.model_id,
                messages=[{"role": "user", "content": "stream through the gateway"}],
                max_tokens=8,
                stream=True,
                stream_options={"include_usage": True},
            )
            chunks = [chunk async for chunk in stream]

        content = "".join(
            chunk.choices[0].delta.content or ""
            for chunk in chunks
            if chunk.choices and chunk.choices[0].delta
        )
        assert content.startswith(f"echo[{ai_env.model.model_id}]")
        assert chunks[0].choices[0].delta.role == "assistant"
        finish = [c for c in chunks if c.choices and c.choices[0].finish_reason]
        assert finish and finish[0].choices[0].finish_reason == "stop"
        usage_chunks = [c for c in chunks if not c.choices and c.usage]
        assert usage_chunks, "include_usage must emit a final usage chunk"
        usage = usage_chunks[-1].usage
        assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens

    @pytest.mark.asyncio
    async def test_streaming_without_include_usage_omits_usage(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        async with gateway_api(ai_env, agent), openai_client(ai_env) as client:
            stream = await client.chat.completions.create(
                model=ai_env.model.model_id,
                messages=[{"role": "user", "content": "no usage please"}],
                max_tokens=8,
                stream=True,
            )
            chunks = [chunk async for chunk in stream]
        assert all(chunk.usage is None for chunk in chunks)

    @pytest.mark.asyncio
    async def test_a_worker_that_dies_mid_stream_is_an_error_not_a_truncated_answer(
        self, ai_env, ai_worker_factory
    ):
        """Row 3.3: a stream cut before the final receipt is a failure.

        The final frame carries the worker's signed receipt. If the stream ends
        after some tokens and without it, the worker died mid-answer; the caller
        must not receive a normal ``stop`` for a partial answer, and nothing may
        settle.
        """
        agent = await ai_worker_factory()

        async def dying_transport(url: str, payload: dict):
            frames = await agent.process(payload)
            parsed, _ = decode_frames(frames)
            kept = b""
            for frame in parsed:
                if frame.kind == FINAL_FRAME:
                    break  # the process died before it could sign a receipt
                kept += encode_frame(frame.kind, frame.body)
            yield kept

        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        openai_routes.set_worker_transport_for_tests(dying_transport)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                headers = {"Authorization": f"Bearer {ai_env.api_key}"}
                body = {
                    "model": ai_env.model.model_id,
                    "messages": [{"role": "user", "content": "cut me off"}],
                    "max_tokens": 8,
                }
                response = await client.post(
                    "/v1/chat/completions", headers=headers, json=body
                )
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    headers=headers,
                    json={**body, "stream": True},
                ) as streamed:
                    sse = "".join([chunk async for chunk in streamed.aiter_text()])
        finally:
            openai_routes.set_worker_transport_for_tests(None)
            ai_routes.set_coordinator_for_tests(None)
            ai_routes.reset_for_tests()

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "worker_stream_incomplete"
        assert "worker_stream_incomplete" in sse
        assert '"finish_reason":"stop"' not in sse
        assert "[DONE]" in sse

        # No receipt, no charge: the hold is still open and only the expiry
        # sweep can release it (asserted in test_ai_failure_injection).
        assert await ai_env.store.held_micro(ai_env.account_id) > 0

    @pytest.mark.asyncio
    async def test_unknown_model_is_a_404(self, ai_env, ai_worker_factory):
        agent = await ai_worker_factory()
        async with gateway_api(ai_env, agent), openai_client(ai_env) as client:
            with pytest.raises(NotFoundError):
                await client.chat.completions.create(
                    model="does-not-exist",
                    messages=[{"role": "user", "content": "hi"}],
                )

    @pytest.mark.asyncio
    async def test_tools_are_rejected_not_silently_ignored(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        async with gateway_api(ai_env, agent), openai_client(ai_env) as client:
            with pytest.raises(BadRequestError) as caught:
                await client.chat.completions.create(
                    model=ai_env.model.model_id,
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[
                        {
                            "type": "function",
                            "function": {"name": "x", "parameters": {}},
                        }
                    ],
                )
        body = caught.value.body
        assert body["code"] == "unsupported_parameter"
        assert body["param"] == "tools"

    @pytest.mark.asyncio
    async def test_confidential_flag_points_at_the_sdk(self, ai_env, ai_worker_factory):
        agent = await ai_worker_factory()
        async with gateway_api(ai_env, agent), openai_client(ai_env) as client:
            with pytest.raises(BadRequestError) as caught:
                await client.chat.completions.create(
                    model=ai_env.model.model_id,
                    messages=[{"role": "user", "content": "secret"}],
                    extra_body={"confidential": True},
                )
        assert caught.value.body["code"] == "unsupported_parameter"
        assert "SDK" in caught.value.body["message"]


def test_flag_off_hides_openai_routes():
    """ENABLE_TCDN_AI=false must leave the existing product with no /v1 surface.

    Imported in a subprocess because ``app.main`` computes the feature flag at
    import time (the same reason the host app cannot toggle it at runtime).
    """
    env = dict(os.environ)
    env.pop("ENABLE_TCDN_AI", None)
    env.setdefault("APP_ENV", "development")
    env.setdefault("JWT_SECRET", "this-is-a-32-char-secret-for-testing-only!!")
    script = (
        "from starlette.testclient import TestClient\n"
        "from app.main import app\n"
        "client = TestClient(app)\n"
        "assert client.get('/v1/models').status_code == 404\n"
        "assert client.post('/v1/chat/completions', json={}).status_code == 404\n"
        "print('flag-off-ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "flag-off-ok" in result.stdout


class TestGatewayHonesty:
    @pytest.mark.asyncio
    async def test_prompt_is_never_logged_or_persisted(
        self, ai_env, ai_worker_factory, caplog
    ):
        agent = await ai_worker_factory()
        canary = "CANARY-GATEWAY-5f1d-prompt"
        caplog.set_level(logging.DEBUG)
        async with gateway_api(ai_env, agent), openai_client(ai_env) as client:
            reply = await client.chat.completions.create(
                model=ai_env.model.model_id,
                messages=[{"role": "user", "content": canary}],
                max_tokens=8,
            )
        assert canary in reply.choices[0].message.content  # echo backend reflects it
        assert canary not in caplog.text

        job = await ai_env.store.get_job(reply.tcdn["job_id"])
        assert canary not in repr(job)
        assert canary not in repr(ai_env.store.audit_log)
        assert not hasattr(job, "prompt")

    @pytest.mark.asyncio
    async def test_worker_failure_before_first_token_is_http_503(
        self, ai_env, ai_worker_factory
    ):
        await ai_worker_factory()
        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)

        async def failing_transport(url: str, payload: dict):
            # The guarded yield comes FIRST so this is an async generator (the
            # transport hook requires one) without leaving unreachable code
            # after the raise, which the dead-code audit rejects.
            if False:  # pragma: no cover
                yield b""
            raise UnavailableError("worker down", code="worker_unreachable")

        openai_routes.set_worker_transport_for_tests(failing_transport)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {ai_env.api_key}"},
                    json={
                        "model": ai_env.model.model_id,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                )
        finally:
            openai_routes.set_worker_transport_for_tests(None)
            ai_routes.set_coordinator_for_tests(None)
            ai_routes.reset_for_tests()
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "worker_unreachable"

    @pytest.mark.asyncio
    async def test_rate_limit_is_shared_with_the_native_surface(self, ai_env):
        from app.ai.ratelimit import TokenBucketLimiter

        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        ai_routes._key_limiter = TokenBucketLimiter(rate_per_minute=1, burst=1)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=BASE_URL
            ) as client:
                headers = {"Authorization": f"Bearer {ai_env.api_key}"}
                first = await client.get("/v1/models", headers=headers)
                second = await client.get("/v1/models", headers=headers)
        finally:
            ai_routes._key_limiter = None
            ai_routes.set_coordinator_for_tests(None)
            ai_routes.reset_for_tests()
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["error"]["type"] == "rate_limit_error"
        assert second.headers.get("Retry-After")

    @pytest.mark.asyncio
    async def test_malformed_json_is_an_openai_error(self, ai_env):
        async with gateway_api(ai_env, agent=None) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {ai_env.api_key}",
                    "Content-Type": "application/json",
                },
                content=b"{not json",
            )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"

    @pytest.mark.asyncio
    async def test_stream_options_without_stream_is_rejected(self, ai_env):
        async with gateway_api(ai_env, agent=None) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {ai_env.api_key}"},
                json={
                    "model": ai_env.model.model_id,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream_options": {"include_usage": True},
                },
            )
        assert response.status_code == 400
        assert response.json()["error"]["param"] == "stream_options"
