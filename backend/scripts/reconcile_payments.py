#!/usr/bin/env python3
"""Reconcile Paystack credit purchases against the AI ledger.

The webhook is the only thing that credits a purchase, so the question this
answers is the one the webhook cannot: *did every charge the provider considers
successful become exactly one ledger credit, of exactly the right size?*

It reads provider data from an **export**, never from the Paystack API: the
script must be safe to run against a live account without a live key, and it
must be reproducible from an archived file months later.

Inputs
------
``--provider-export``
    Paystack transaction export as JSON. Either ``{"data": [...]}`` or a bare
    list. Each row is read for ``reference``, ``amount`` (minor units),
    ``currency``, ``status`` and ``metadata.account_id``.
``--ledger-export``
    JSON list of ledger rows (from
    ``SELECT idempotency_key, amount_micro, credit_account, kind, created_at
    FROM ai_ledger_entries WHERE idempotency_key LIKE 'payment:%'``), **or**
``--database-url``
    Postgres URL, in which case the query above is run directly.

Findings (all are exit code 1)
-----------------------------
* ``paid_not_credited``   provider says success, ledger has no credit. Money in,
                          no credits — the customer-visible failure.
* ``credited_not_paid``   ledger credit with no provider success. Free credits
                          from a forged or replayed event.
* ``amount_mismatch``     credit exists but is not the charged amount.
* ``duplicate_credit``    more than one ledger row for the same reference.

Only ``payment:paystack:*`` keys are considered: development fake-provider rows
(``payment:fake:*``) are labelled and never reconciled against a real account.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field

MICRO_PER_MINOR = 1_000_000  # 1 USD cent = 1 credit = 1e6 micro-credits
PAYSTACK_PREFIX = "payment:paystack:"


@dataclass
class Report:
    paid_not_credited: list[dict] = field(default_factory=list)
    credited_not_paid: list[dict] = field(default_factory=list)
    amount_mismatch: list[dict] = field(default_factory=list)
    duplicate_credit: list[dict] = field(default_factory=list)
    matched: int = 0
    provider_rows: int = 0
    ledger_rows: int = 0

    @property
    def findings(self) -> int:
        return (
            len(self.paid_not_credited)
            + len(self.credited_not_paid)
            + len(self.amount_mismatch)
            + len(self.duplicate_credit)
        )

    def as_dict(self) -> dict:
        return {
            "provider_rows": self.provider_rows,
            "ledger_rows": self.ledger_rows,
            "matched": self.matched,
            "findings": self.findings,
            "paid_not_credited": self.paid_not_credited,
            "credited_not_paid": self.credited_not_paid,
            "amount_mismatch": self.amount_mismatch,
            "duplicate_credit": self.duplicate_credit,
        }


def _rows(document) -> list[dict]:
    if isinstance(document, dict):
        document = document.get("data")
    if not isinstance(document, list):
        raise SystemExit("expected a JSON list, or an object with a 'data' list")
    return [row for row in document if isinstance(row, dict)]


def load_provider(path: str) -> dict[str, dict]:
    """Successful USD charges, keyed by reference."""
    with open(path, encoding="utf-8") as handle:
        rows = _rows(json.load(handle))
    successes: dict[str, dict] = {}
    for row in rows:
        if str(row.get("status", "")).lower() != "success":
            continue
        currency = str(row.get("currency", "")).upper()
        if currency and currency != "USD":
            # The webhook refuses non-USD, so a non-USD success cannot have
            # been credited and is reported rather than silently skipped.
            successes.setdefault(str(row.get("reference", "")), row)
            continue
        reference = str(row.get("reference", "") or "")
        if not reference:
            continue
        successes.setdefault(reference, row)
    return successes


def load_ledger(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return _rows(json.load(handle))


async def load_ledger_from_db(url: str) -> list[dict]:
    try:
        import asyncpg  # local import: the export path needs no driver
    except ImportError:  # pragma: no cover - asyncpg is a production dependency
        raise SystemExit("--database-url needs asyncpg installed") from None
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch(
            "SELECT idempotency_key, amount_micro, credit_account, created_at "
            "FROM ai_ledger_entries WHERE idempotency_key LIKE 'payment:%'"
        )
    finally:
        await conn.close()
    return [dict(row) for row in rows]


def reconcile(provider: dict[str, dict], ledger_rows: list[dict]) -> Report:
    report = Report(provider_rows=len(provider))
    credits: dict[str, list[dict]] = {}
    for row in ledger_rows:
        key = str(row.get("idempotency_key", ""))
        if not key.startswith(PAYSTACK_PREFIX):
            continue
        credits.setdefault(key[len(PAYSTACK_PREFIX) :], []).append(row)
    report.ledger_rows = sum(len(rows) for rows in credits.values())

    for reference, rows in credits.items():
        if len(rows) > 1:
            report.duplicate_credit.append(
                {
                    "reference": reference,
                    "rows": len(rows),
                    "amount_micro": [row.get("amount_micro") for row in rows],
                }
            )
        if reference not in provider:
            for row in rows:
                report.credited_not_paid.append(
                    {
                        "reference": reference,
                        "amount_micro": row.get("amount_micro"),
                        "account": row.get("credit_account"),
                    }
                )
            continue
        event = provider[reference]
        expected = int(event.get("amount") or 0) * MICRO_PER_MINOR
        for row in rows:
            if int(row.get("amount_micro") or 0) != expected:
                report.amount_mismatch.append(
                    {
                        "reference": reference,
                        "expected_micro": expected,
                        "ledger_micro": row.get("amount_micro"),
                        "currency": event.get("currency"),
                    }
                )
            else:
                report.matched += 1

    for reference, event in provider.items():
        if reference not in credits:
            report.paid_not_credited.append(
                {
                    "reference": reference,
                    "amount_micro": int(event.get("amount") or 0) * MICRO_PER_MINOR,
                    "account": (event.get("metadata") or {}).get("account_id"),
                    "currency": event.get("currency"),
                }
            )
    return report


def print_report(report: Report) -> None:
    print("Paystack credit-purchase reconciliation")
    print(f"  provider successes : {report.provider_rows}")
    print(f"  ledger credits     : {report.ledger_rows}")
    print(f"  matched            : {report.matched}")
    print(f"  findings           : {report.findings}")
    sections = [
        ("paid_not_credited (money in, no credit)", report.paid_not_credited),
        ("credited_not_paid (credit with no charge)", report.credited_not_paid),
        ("amount_mismatch", report.amount_mismatch),
        ("duplicate_credit", report.duplicate_credit),
    ]
    for title, rows in sections:
        if not rows:
            continue
        print(f"\n{title}:")
        for row in rows:
            print("  " + json.dumps(row, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-export", required=True, help="Paystack JSON export")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--ledger-export", help="ledger rows as JSON")
    source.add_argument("--database-url", help="Postgres URL to read the ledger")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    provider = load_provider(args.provider_export)
    if args.database_url:
        ledger_rows = asyncio.run(load_ledger_from_db(args.database_url))
    else:
        ledger_rows = load_ledger(args.ledger_export)

    report = reconcile(provider, ledger_rows)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print_report(report)
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())
