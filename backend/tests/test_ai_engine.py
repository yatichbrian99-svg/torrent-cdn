"""Real-engine tests: the Ollama and vLLM adapters against a live engine.

Everything else in the suite drives the labelled ``EchoBackend``, so nothing
proved the adapters could talk to a real engine — a wrong request field, a
misread stream, or a dropped usage count would have passed every test. These
tests are skipped unless ``TCDN_AI_ENGINE_URL`` points at a running engine:

    TCDN_AI_ENGINE_URL=http://127.0.0.1:11434 \
    TCDN_AI_ENGINE_MODEL=qwen2.5:0.5b \
      .venv/bin/python -m pytest tests/test_ai_engine.py -q

They run nightly in CI (``.github/workflows/tcdn-ai-engine.yml``) and are part
of the journey run in ``docs/tcdn-ai/DEV_SETUP.md`` §7.

**vLLM scope, stated honestly:** vLLM needs a GPU and its own install, so its
adapter is exercised against a real *OpenAI-compatible* engine — Ollama's ``/v1``
surface, the same wire protocol (SSE ``data:`` frames, ``usage`` in the final
chunk). That proves the adapter's request shape and stream parsing against a
real server, **not** against vLLM itself.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from app.ai.worker import OllamaBackend, VLLMBackend

ENGINE_URL = os.environ.get("TCDN_AI_ENGINE_URL", "").rstrip("/")
ENGINE_MODEL = os.environ.get("TCDN_AI_ENGINE_MODEL", "qwen2.5:0.5b")

pytestmark = pytest.mark.skipif(
    not ENGINE_URL,
    reason="TCDN_AI_ENGINE_URL is not set: no live inference engine in this run",
)


def _messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "Reply with one short sentence about the sea."}]


async def _engine_logprob_entries() -> list[dict]:
    """The engine's raw per-token logprob entries (empty when unsupported).

    Used to derive what the adapter *can* legitimately commit, instead of
    assuming an engine version's behaviour.
    """
    payload = {
        "model": ENGINE_MODEL,
        "messages": _messages(),
        "stream": True,
        "logprobs": True,
        "top_logprobs": 5,
        "options": {"num_predict": 4, "temperature": 0},
    }
    async with (
        httpx.AsyncClient(base_url=ENGINE_URL, timeout=300.0) as client,
        client.stream("POST", "/api/chat", json=payload) as response,
    ):
        response.raise_for_status()
        async for line in response.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if isinstance(event.get("logprobs"), list) and event["logprobs"]:
                    return event["logprobs"]
    return []


class TestOllamaBackend:
    @pytest.mark.asyncio
    async def test_streams_real_tokens_and_reports_usage(self):
        backend = OllamaBackend(ENGINE_URL, timeout=300.0)
        chunks = [
            chunk
            async for chunk in backend.stream(
                model=ENGINE_MODEL,
                messages=_messages(),
                max_tokens=32,
                temperature=0.0,
                top_p=1.0,
                stop=[],
            )
        ]
        text = "".join(chunks)
        assert text.strip(), "the engine produced no content"
        assert backend.last_usage["output_tokens"] > 0, backend.last_usage
        assert backend.last_usage["input_tokens"] > 0, backend.last_usage

    @pytest.mark.asyncio
    async def test_transcript_commits_exactly_what_the_engine_can_prove(self):
        """Records exist ⇔ the engine supplied token ids.

        Ollama's native logprobs carry ``token``/``bytes`` and **no** token id,
        so a transcript built here could not be re-scored by a verifier that
        recomputes real vocabulary ids. The adapter therefore discards them and
        the receipt carries no verification block — a documented limitation
        (LIMITATIONS.md), because fabricating ids would be worse than none.
        """
        entries = await _engine_logprob_entries()
        token_ids_available = any(entry.get("token_id") is not None for entry in entries)
        backend = OllamaBackend(ENGINE_URL, timeout=300.0)
        async for _ in backend.stream(
            model=ENGINE_MODEL,
            messages=_messages(),
            max_tokens=16,
            temperature=0.0,
            top_p=1.0,
            stop=[],
        ):
            pass
        if token_ids_available:
            assert backend.last_transcript, (
                "the engine reports token ids but the adapter captured no transcript"
            )
            for record in backend.last_transcript:
                assert record.token_id >= 0
                assert record.chosen_logprob <= 0
                assert record.top_k, "a transcript record carried no top-k set"
        else:
            assert backend.last_transcript == (), (
                "the adapter fabricated a transcript for an engine that reports no token ids"
            )

    @pytest.mark.asyncio
    async def test_stop_sequences_are_sent_and_respected(self):
        backend = OllamaBackend(ENGINE_URL, timeout=300.0)
        chunks = [
            chunk
            async for chunk in backend.stream(
                model=ENGINE_MODEL,
                messages=[{"role": "user", "content": "Count: one two three four five"}],
                max_tokens=64,
                temperature=0.0,
                top_p=1.0,
                stop=["three"],
            )
        ]
        text = "".join(chunks)
        assert "three" not in text.lower(), f"stop sequence leaked into the output: {text!r}"


class TestVLLMAdapterAgainstOpenAICompatibleEngine:
    """The vLLM adapter's request/stream handling against a real /v1 engine."""

    @pytest.mark.asyncio
    async def test_streams_and_reads_usage_over_sse(self):
        # The adapter appends `/v1/chat/completions` itself, so the base URL is
        # the engine root — the same shape a real vLLM server is given.
        backend = VLLMBackend(ENGINE_URL, timeout=300.0)
        chunks = [
            chunk
            async for chunk in backend.stream(
                model=ENGINE_MODEL,
                messages=_messages(),
                max_tokens=32,
                temperature=0.0,
                top_p=1.0,
                stop=[],
            )
        ]
        text = "".join(chunks)
        assert text.strip(), "the OpenAI-compatible endpoint produced no content"
        assert backend.last_usage["output_tokens"] > 0, backend.last_usage
        assert backend.last_usage["input_tokens"] > 0, backend.last_usage
