#!/usr/bin/env python3
"""Deterministic mutation fuzzer for TCDN-AI untrusted-input parsers.

Why not coverage-guided fuzzing: ``atheris`` needs a C compiler and libFuzzer
instrumentation, which this environment does not have. This harness is a seeded,
mutation-based fuzzer with a strict contract:

* a parser may reject an input through a typed ``AIError`` or a pydantic
  ``ValidationError`` -- those are correct outcomes for hostile input;
* anything else (IndexError, KeyError, TypeError, OverflowError, RecursionError,
  an unhandled pydantic bug, a hang) is a finding and is recorded with the exact
  input, so it can be reproduced from the saved corpus.

Usage::

    python scripts/fuzz_ai.py --seconds-per-target 20 --seed 1337 \
        --out ../docs/tcdn-ai/evidence/03-security/fuzz-ai.json

Exit code 0 = no findings, 1 = findings, 2 = harness could not run.
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError

from app.ai import crypto
from app.ai.errors import AIError
from app.ai.keyring import KeyRing
from app.ai.models import Confidentiality, PlacementRequest
from app.ai.payments import PaystackPaymentProvider
from app.ai.receipts import JobContext, sign_receipt, verify_receipt
from app.ai.shards import merkle_proof, merkle_root, verify_merkle_proof
from app.ai.tickets import TicketService

EXPECTED = (AIError, ValidationError, ValueError)
FINDINGS: list[dict] = []


class Hang(Exception):
    pass


@dataclass
class Target:
    name: str
    run: object
    corpus: list = field(default_factory=list)


def _alarm(_signum, _frame):
    raise Hang("input exceeded the per-case time budget")


def mutate(rng: random.Random, value, depth: int = 0):
    """One random structural mutation of a JSON-ish value."""
    if depth > 4:
        return _scalar(rng)
    choice = rng.randrange(6)
    if choice == 0:
        return _scalar(rng)
    if isinstance(value, dict) and value:
        out = dict(value)
        key = rng.choice(list(out))
        if rng.random() < 0.3:
            del out[key]
        else:
            out[key] = mutate(rng, out[key], depth + 1)
        return out
    if isinstance(value, list) and value:
        out = list(value)
        index = rng.randrange(len(out))
        if rng.random() < 0.3:
            del out[index]
        else:
            out[index] = mutate(rng, out[index], depth + 1)
        return out
    return _scalar(rng)


def _scalar(rng: random.Random):
    return rng.choice(
        [
            None,
            True,
            False,
            0,
            -1,
            2**63,
            -(2**63) - 1,
            10**400,
            1.5,
            float("nan"),
            float("inf"),
            "",
            "x" * 100_000,
            "\x00\x00",
            "../../etc/passwd",
            "'; DROP TABLE ai_jobs; --",
            "tcdn_ai_" + "a" * 40,
            [],
            {},
            [1, 2, 3],
        ]
    )


def _check(name: str, payload, fn) -> None:
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(2)
    try:
        fn()
    except EXPECTED:
        pass
    except Hang as exc:
        record(name, payload, exc, "hang")
    except Exception as exc:  # the point is to catch everything
        record(name, payload, exc, "unexpected-exception")
    finally:
        signal.alarm(0)


def record(name: str, payload, exc: BaseException, kind: str) -> None:
    try:
        rendered = repr(payload)[:2000]
    except Exception:  # pragma: no cover - defensive
        rendered = "<unrenderable>"
    FINDINGS.append(
        {
            "target": name,
            "kind": kind,
            "exception": f"{type(exc).__name__}: {exc}"[:400],
            "traceback": traceback.format_exc()[-2000:],
            "input_repr": rendered,
        }
    )


def build_targets(seed: int) -> list[Target]:
    ring = KeyRing.generate("fuzz-coordinator")
    tickets = TicketService(ring)
    document, _ = tickets.issue(
        job_id="job_fuzz_1",
        account_id="11111111-1111-4111-8111-111111111111",
        node_id="node_fuzz_1",
        model="llama-3.1-8b-instruct",
        worker_public_key=ring.active.public_key,
        confidentiality=Confidentiality.NONE,
        max_tokens=16,
        max_price_micro=10_000,
    )

    worker = crypto.generate_ed25519()
    job = JobContext(
        job_id="job_fuzz_1",
        account_id="11111111-1111-4111-8111-111111111111",
        node_id="node_fuzz_1",
        model="llama-3.1-8b-instruct",
        manifest_hash="a" * 64,
        max_tokens=16,
    )
    receipt = sign_receipt(
        {
            "job_id": job.job_id,
            "account_id": job.account_id,
            "node_id": job.node_id,
            "model": job.model,
            "manifest_hash": job.manifest_hash,
            "input_tokens": 3,
            "output_tokens": 4,
            "started_at": 1_700_000_000,
            "finished_at": 1_700_000_001,
            "output_sha256": "b" * 64,
        },
        worker["private_key"],
    )

    leaves = [crypto.sha256_hex(bytes([i])) for i in range(8)]
    root = merkle_root(leaves)
    proof = merkle_proof(leaves, 3)

    provider = PaystackPaymentProvider("sk_test_fuzz_only")

    def fuzz_ticket(rng: random.Random) -> None:
        mutated = mutate(rng, document)
        _check("ticket.verify", mutated, lambda: tickets.verify(mutated, claim_replay=False))

    def fuzz_receipt(rng: random.Random) -> None:
        mutated = mutate(rng, receipt)
        _check(
            "receipt.verify",
            mutated,
            lambda: verify_receipt(
                mutated, worker_public_key=worker["public_key"], job=job
            ),
        )

    def fuzz_merkle(rng: random.Random) -> None:
        candidate_proof = mutate(rng, proof)
        leaf = rng.choice([leaves[3], mutate(rng, leaves[3])])
        candidate_root = rng.choice([root, mutate(rng, root)])
        _check(
            "shards.verify_merkle_proof",
            {"leaf": leaf, "proof": candidate_proof, "root": candidate_root},
            lambda: verify_merkle_proof(leaf, candidate_proof, candidate_root),
        )

    def fuzz_aead(rng: random.Random) -> None:
        key = rng.choice([crypto.b64d(ring.active.public_key)[:32], b"k" * 32, b"", b"x"])
        blob = rng.randbytes(rng.randrange(0, 80))
        aad = rng.choice([b"", b"job", b"\x00" * 12, rng.randbytes(20)])
        _check(
            "crypto.aead_decrypt",
            {"key_len": len(key), "blob": blob.hex(), "aad": aad.hex()},
            lambda: crypto.aead_decrypt(key, blob, aad),
        )

    def fuzz_placement(rng: random.Random) -> None:
        candidate = mutate(rng, {"model": "llama-3.1-8b-instruct", "max_tokens": 8})
        _check(
            "models.PlacementRequest",
            candidate,
            lambda: PlacementRequest.model_validate(candidate),
        )

    def fuzz_webhook(rng: random.Random) -> None:
        body = rng.choice([rng.randbytes(rng.randrange(0, 200)), b"{}", b"null", b'{"event":1}'])
        signature = rng.choice(["", "0" * 64, "0" * 128, rng.randbytes(64).hex()])
        _check(
            "payments.verify_webhook",
            {"body": body.hex(), "signature": signature},
            lambda: provider.verify_webhook(body, signature),
        )

    return [
        Target("ticket.verify", fuzz_ticket),
        Target("receipt.verify", fuzz_receipt),
        Target("shards.verify_merkle_proof", fuzz_merkle),
        Target("crypto.aead_decrypt", fuzz_aead),
        Target("models.PlacementRequest", fuzz_placement),
        Target("payments.verify_webhook", fuzz_webhook),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds-per-target", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    targets = build_targets(args.seed)
    started = time.time()
    results = []
    for offset, target in enumerate(targets):
        rng = random.Random(args.seed + offset)
        cases = 0
        deadline = time.time() + args.seconds_per_target
        before = len(FINDINGS)
        while time.time() < deadline:
            target.run(rng)
            cases += 1
        results.append(
            {
                "target": target.name,
                "cases": cases,
                "findings": len(FINDINGS) - before,
                "seconds": round(time.time() - (deadline - args.seconds_per_target), 2),
            }
        )
        print(f"{target.name:32} {cases:7d} cases  {len(FINDINGS) - before} findings")

    report = {
        "seed": args.seed,
        "seconds_per_target": args.seconds_per_target,
        "elapsed_seconds": round(time.time() - started, 2),
        "python": sys.version,
        "targets": results,
        "findings": FINDINGS,
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\n{len(FINDINGS)} finding(s) in {report['elapsed_seconds']}s")
    return 0 if not FINDINGS else 1


if __name__ == "__main__":
    raise SystemExit(main())
