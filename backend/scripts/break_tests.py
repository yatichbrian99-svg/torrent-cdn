#!/usr/bin/env python3
"""Break tests: prove the security-critical tests can actually fail.

For each control we introduce the exact bug the control exists to stop, run the
test that is supposed to catch it, and require that test to fail. The file is
restored in a ``finally`` block, its hash is checked, and the test is re-run to
prove the suite is green again.

A mutation whose test still passes means the test is decorative. That is a
finding, not a flake.

Usage::

    python scripts/break_tests.py --out ../docs/tcdn-ai/evidence/06-break-tests/break-tests.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTATIONS = [
    {
        "control": "receipt signature verification",
        "path": "app/ai/receipts.py",
        "find": "    if not crypto.verify(\n        RECEIPT_DOMAIN, payload, str(document.get(\"sig\", \"\")), worker_public_key\n    ):",
        "replace": "    if False and not crypto.verify(\n        RECEIPT_DOMAIN, payload, str(document.get(\"sig\", \"\")), worker_public_key\n    ):",
        "test": "tests/test_ai_components.py::TestReceipts::test_flipped_signature_rejected",
    },
    {
        "control": "signature domain separation",
        "path": "app/ai/crypto.py",
        "find": '    return domain.encode("ascii") + b"\\x00" + canonical_json(payload)',
        "replace": "    return canonical_json(payload)",
        "test": "tests/test_ai_core.py::TestCrypto",
    },
    {
        "control": "AEAD associated data binding",
        "path": "app/ai/crypto.py",
        "find": "        return AESGCM(key).decrypt(nonce, ciphertext, aad)",
        "replace": '        return AESGCM(key).decrypt(nonce, ciphertext, b"")',
        "test": "tests/test_ai_core.py::TestCrypto::test_aead_roundtrip_and_tamper",
    },
    {
        "control": "ticket replay cache",
        "path": "app/ai/tickets.py",
        "find": "        if claim_replay and not self.replay.claim(",
        "replace": "        if False and not self.replay.claim(",
        "test": "tests/test_ai_core.py::TestTickets::test_replay_is_rejected",
    },
    {
        "control": "credit hold cannot overdraw",
        "path": "app/ai/ledger.py",
        "find": "            if available < amount_micro:",
        "replace": "            if False:",
        "test": "tests/test_ai_core.py::TestLedger::test_hold_cannot_exceed_available",
    },
    {
        "control": "simulated attestation refused in production",
        "path": "app/ai/attestation.py",
        "find": '        if verdict.status == "simulated" and self.settings.is_production:',
        "replace": '        if False and verdict.status == "simulated" and self.settings.is_production:',
        "test": "tests/test_ai_components.py::TestAttestation::test_simulated_verdict_rejected_in_production",
    },
    {
        "control": "AI admin endpoints require an admin a session token can prove",
        "path": "app/ai/routes.py",
        "find": "Depends(require_admin_jwt)",
        "replace": 'Depends(lambda: "mutant")',
        "test": "tests/test_ai_api.py::TestAccountIsolation::test_api_key_cannot_reach_admin",
        # Every admin route in app/ai/routes.py: the refunds queue and decision,
        # API-key creation, the kill switch, and the verification queue and
        # decision. The count is pinned so a new admin endpoint that forgets the
        # dependency shows up as a harness error instead of an untested route.
        "occurrences": 5,
    },
    {
        "control": "a CDN API key cannot spend admin money authority (F-6)",
        "path": "app/auth.py",
        "find": (
            "    if credentials.credentials.startswith(API_KEY_PREFIX):\n"
            "        raise HTTPException(\n"
            "            status_code=status.HTTP_403_FORBIDDEN,\n"
        ),
        "replace": "    if False:\n        raise HTTPException(\n            status_code=status.HTTP_403_FORBIDDEN,\n",
        "test": (
            "tests/test_admin_jwt_only.py"
            "::test_an_admin_api_key_is_refused_and_never_reaches_the_admin_lookup"
        ),
    },
    {
        "control": "the origin shard fetch dials the address that was validated (F-3)",
        "path": "app/ai/shards.py",
        "find": "            transport=PinnedHTTPTransport(self._target.ip, verify=verify),\n",
        "replace": "            transport=None,\n",
        "test": (
            "tests/test_ai_ssrf_pinning.py::TestOriginRebindingIsClosed"
            "::test_rebinding_dns_cannot_move_the_origin_connect"
        ),
    },
    {
        "control": "Merkle proof verification",
        "path": "app/ai/shards.py",
        "find": "    return crypto.constant_time_eq(current, root)",
        "replace": "    return True",
        "test": "tests/test_ai_components.py::TestShards::test_manifest_merkle_and_signature",
    },
    {
        "control": "payment webhook signature",
        "path": "app/ai/payments.py",
        "find": '        if not hmac.compare_digest(expected, signature or ""):',
        "replace": "        if False:",
        "test": "tests/test_ai_billing.py::TestFakeProvider::test_bad_signature_rejected",
        "occurrences": 2,
    },
    {
        "control": "gateway rejects unsupported/confidential OpenAI params",
        "path": "app/ai/gateway.py",
        # Both layers are removed on purpose: the reject-unsupported and
        # reject-unknown checks are independent, so mutating only one leaves the
        # other enforcing (and the test correctly still passes).
        "find": (
            "    for key, reason in UNSUPPORTED_PARAMS.items():\n"
            "        if body.get(key) is not None:\n"
            "            raise _invalid(\n"
            '                f"{key} is not supported: {reason}",\n'
            "                param=key,\n"
            '                code="unsupported_parameter",\n'
            "            )\n"
            "    unknown = sorted(set(body) - ALLOWED_PARAMS)\n"
        ),
        "replace": "    unknown: list[str] = []\n",
        "test": (
            "tests/test_ai_openai_compat.py::TestOfficialClient"
            "::test_tools_are_rejected_not_silently_ignored"
        ),
    },
    {
        "control": "credit hold carries engine-token headroom",
        "path": "app/ai/service.py",
        "find": (
            "        hold_extra_tokens = self.settings.hold_input_margin_tokens + (\n"
            "            request.prompt_tokens_estimate * self.settings.hold_input_margin_pct // 100\n"
            "        )\n"
        ),
        "replace": "        hold_extra_tokens = 0\n",
        "test": (
            "tests/test_ai_service.py::TestHoldHeadroomAndDisputes"
            "::test_engine_chat_template_overhead_settles_within_the_hold"
        ),
    },
    {
        "control": "a worker receipt bills the job with no client ack",
        "path": "app/ai/service.py",
        "find": (
            "        job.receipt = document\n"
            "        return await self._reconcile_and_settle(job, receipt)\n"
        ),
        "replace": (
            "        job.receipt = document\n"
            "        return self._settlement_response(job, \"awaiting client ack\")\n"
        ),
        "test": (
            "tests/test_ai_service.py::TestEncryptedJob"
            "::test_placement_worker_receipt_ack_settlement"
        ),
    },
    {
        "control": "an over-hold receipt is capped at the hold and flagged",
        "path": "app/ai/service.py",
        "find": (
            "            review_reason = (\n"
            '                f"{review_reason}+settlement_exceeds_hold"\n'
            "                if review_reason\n"
            '                else "settlement_exceeds_hold"\n'
            "            )\n"
        ),
        "replace": "            review_reason = review_reason\n",
        "test": (
            "tests/test_ai_service.py::TestHoldHeadroomAndDisputes"
            "::test_over_hold_receipt_is_capped_and_flagged"
        ),
    },
    {
        "control": "the gateway's own count outranks an overclaiming receipt",
        "path": "app/ai/service.py",
        "find": "        if self.settings.gateway_billing_authority and measurement is not None:\n",
        "replace": "        if False:\n",
        "test": (
            "tests/test_ai_node_billing.py::TestGatewayBillingAuthority"
            "::test_an_overclaiming_receipt_is_billed_at_the_gateways_count"
        ),
    },
    {
        "control": "an overclaiming node is suspended for review",
        "path": "app/ai/service.py",
        "find": "        held = await self.store.hold_node_for_review(node_id, reason)\n",
        "replace": "        held = node\n",
        "test": (
            "tests/test_ai_node_billing.py::TestNodeOverclaimReview"
            "::test_an_overclaiming_node_is_suspended_and_its_payouts_held"
        ),
    },
    {
        "control": "payouts of a node under review are held, not paid",
        "path": "app/ai/service.py",
        "find": "        if node is not None and node.payouts_held:\n",
        "replace": "        if False:\n",
        "test": (
            "tests/test_ai_node_billing.py::TestNodeOverclaimReview"
            "::test_held_payouts_are_recorded_but_not_paid"
        ),
    },
    {
        "control": "the AI schema migration is gated on ENABLE_TCDN_AI",
        "path": "app/main.py",
        "find": (
            "    if _ai_enabled:\n"
            "        try:\n"
            "            pool = await get_db_pool()\n"
            "            async with pool.acquire() as conn:\n"
            "                await conn.execute(AI_TABLES_DDL)\n"
        ),
        "replace": (
            "    if True:\n"
            "        try:\n"
            "            pool = await get_db_pool()\n"
            "            async with pool.acquire() as conn:\n"
            "                await conn.execute(AI_TABLES_DDL)\n"
        ),
        "test": (
            "tests/test_ai_migrations.py"
            "::test_the_ai_schema_is_only_migrated_when_the_flag_is_on"
        ),
    },
    {
        "control": "attestation nonce consumption is atomic (redis)",
        "path": "app/ai/redis_cache.py",
        "find": (
            '            getdel = getattr(self._redis, "getdel", None)\n'
            "            if getdel is not None:\n"
            "                return await getdel(key)\n"
        ),
        "replace": (
            "            value = await self._redis.get(key)\n"
            "            await self._redis.delete(key)\n"
            "            return value\n"
        ),
        "test": (
            "tests/test_ai_redis.py::TestNonceConsumptionIsAtomic"
            "::test_the_redis_store_has_exactly_one_winner"
        ),
    },
    {
        "control": "attestation nonce consumption is atomic (in-process)",
        "path": "app/ai/nonce_store.py",
        "find": "            record = self._nonces.pop(nonce, None)\n",
        "replace": "            record = self._nonces.get(nonce, None)\n",
        "test": (
            "tests/test_ai_redis.py::TestNonceConsumptionIsAtomic"
            "::test_the_in_process_store_has_exactly_one_winner"
        ),
    },
    {
        "control": "job ownership on client ack",
        "path": "app/ai/service.py",
        "find": "        if job.account_id != auth.account_id:",
        "replace": "        if False:",
        "test": (
            "tests/test_authorization_matrix.py::TestAIRoleMatrix"
            "::test_foreign_account_cannot_ack_another_tenants_job"
        ),
    },
]


def run(test: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def tail(proc: subprocess.CompletedProcess, lines: int = 6) -> str:
    text = (proc.stdout + proc.stderr).strip().splitlines()
    return "\n".join(text[-lines:])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    results = []
    unproven = 0
    for mutation in MUTATIONS:
        path = ROOT / mutation["path"]
        original = path.read_text()
        original_hash = hashlib.sha256(original.encode()).hexdigest()
        expected = mutation.get("occurrences", 1)
        found = original.count(mutation["find"])
        entry = {
            "control": mutation["control"],
            "file": mutation["path"],
            "test": mutation["test"],
        }
        if found != expected:
            entry.update(
                status="HARNESS-ERROR",
                detail=f"expected {expected} occurrence(s), found {found}",
            )
            results.append(entry)
            unproven += 1
            # A drifted mutation target used to be counted but never printed, so
            # the summary said "1 unproven" without naming the control.
            print(f"{entry['status']:10} {mutation['control']}: {entry['detail']}")
            continue

        baseline = run(mutation["test"])
        entry["baseline"] = "pass" if baseline.returncode == 0 else "FAIL"
        if baseline.returncode != 0:
            entry.update(
                status="HARNESS-ERROR",
                detail="baseline test already fails before mutation:\n" + tail(baseline),
            )
            results.append(entry)
            unproven += 1
            print(f"{entry['status']:10} {mutation['control']}: baseline test fails")
            continue

        try:
            path.write_text(original.replace(mutation["find"], mutation["replace"]))
            mutated = run(mutation["test"])
        finally:
            path.write_text(original)

        restored_hash = hashlib.sha256(path.read_text().encode()).hexdigest()
        after = run(mutation["test"])
        entry.update(
            mutated_exit=mutated.returncode,
            mutated_output=tail(mutated),
            restored=restored_hash == original_hash,
            after_restore="pass" if after.returncode == 0 else "FAIL",
        )
        if mutated.returncode != 0 and entry["restored"] and after.returncode == 0:
            entry["status"] = "CAUGHT"
        else:
            entry["status"] = "NOT-CAUGHT"
            unproven += 1
        results.append(entry)
        print(f"{entry['status']:10} {mutation['control']}")

    report = {"mutations": results, "unproven": unproven}
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\n{len(results) - unproven}/{len(results)} controls proven; {unproven} unproven")
    return 0 if unproven == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
