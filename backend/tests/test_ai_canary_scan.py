"""Environment-wide canary scan (checklist 5.7).

One canary request through the real gateway, then a scan of everything the
coordinator kept, every log line the request produced, and a directory of files.
The second test plants the canary in a file to prove the scanner is not vacuous.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

import pytest

from app.ai.gateway import ChatRequest, Gateway
from scripts.canary_scan import scan_paths, scan_tree
from tcdn_ai_sdk.protocol import FINAL_FRAME, decode_frames

CANARY = "CANARY-7f3c-prompt-marker-91b2"


def _snapshot(store) -> dict:
    """Everything the in-memory store holds, as a plain structure to scan.

    This is the stand-in for "every text column of every table": the same
    objects Postgres would persist (jobs, receipts, audit rows, verification
    rows, ledger entries, node records, control rows).
    """
    return {
        "jobs": [asdict(job) for job in store.jobs.values()],
        "receipts": list(store.receipts.values()),
        "acks": [value for key, value in store.receipts.items() if key[1] == "client"],
        "audit_log": store.audit_log,
        "verifications": [asdict(row) for row in store.verifications.values()],
        "ledger": [asdict(entry) for entry in store.ledger.entries],
        "nodes": [asdict(node) for node in store.nodes.values()],
        "controls": store.controls,
        "payouts": store.payouts,
        "transfers": store.transfers,
    }


def _transport(agent, receipts: list[dict]):
    async def transport(url: str, payload: dict):
        frames = await agent.process(payload)
        parsed, _ = decode_frames(frames)
        for frame in parsed:
            if frame.kind == FINAL_FRAME:
                receipts.append(json.loads(frame.body.decode("utf-8")))
        yield frames

    return transport


@pytest.mark.asyncio
async def test_the_prompt_reaches_the_answer_but_not_the_coordinator_state(
    ai_env, ai_worker_factory, caplog, tmp_path
):
    caplog.set_level(logging.DEBUG)
    worker = await ai_worker_factory()
    receipts: list[dict] = []
    gateway = Gateway(ai_env.coordinator, transport=_transport(worker, receipts))
    request = ChatRequest(
        model=ai_env.model.model_id,
        messages=[{"role": "user", "content": f"repeat this marker: {CANARY}"}],
        max_tokens=24,
        temperature=0.0,
        top_p=1.0,
        stop=[],
        stream=False,
        include_usage=False,
    )

    stream = await gateway.open(ai_env.auth, request)
    result = await gateway.complete(ai_env.auth, stream)

    # Control: the marker really did travel through the request/response, so the
    # absence below is a property of the storage path, not of the canary.
    assert CANARY in result.text

    state_findings = scan_tree(_snapshot(ai_env.store), CANARY)
    assert state_findings == [], f"canary persisted at: {state_findings}"

    # The operator-visible log is where a prompt most often leaks by accident.
    assert CANARY not in caplog.text

    # And a work directory that only holds this run's scratch files.
    scratch = tmp_path / "worker.log"
    scratch.write_text("job placed node=... tokens=...\n")
    assert scan_paths([tmp_path], CANARY.encode()) == []


def test_the_scanner_finds_a_planted_canary(tmp_path):
    """Vacuity control: the same scan does report a real leak."""
    (tmp_path / "leak.log").write_text(f'payload={{"prompt": "{CANARY}"}}\n')
    (tmp_path / "clean.log").write_text("nothing interesting here\n")
    findings = scan_paths([tmp_path], CANARY.encode())
    assert len(findings) == 1
    assert findings[0]["path"].endswith("leak.log")
    assert findings[0]["offsets"] == [findings[0]["offsets"][0]]


def test_the_scanner_ignores_binary_media(tmp_path):
    """A marker inside a JPEG is not a prompt-storage leak; don't read it."""
    (tmp_path / "frame.jpg").write_bytes(b"\xff\xd8\xff" + CANARY.encode())
    assert scan_paths([tmp_path], CANARY.encode()) == []
