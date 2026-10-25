"""Credits, exact splits, holds, and a double-entry ledger.

Invariants enforced here and property-tested in ``backend/tests/test_ai_ledger.py``:

1. Amounts are positive Python ``int`` micro-credits. ``bool`` and ``float`` are
   rejected (``bool`` is an ``int`` subclass, so this check is not theoretical).
2. Every posting has a debit and a credit account, and the whole ledger nets to
   zero at all times (``Ledger.invariant_ok``).
3. Splits always sum to exactly the settled total: compute and seeder shares are
   floored, the platform absorbs the remainder (ASSUMPTIONS.md A9).
4. A hold reserves credits so concurrent placements cannot overdraw, and
   settlement is idempotent per job — replays return the original result.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .clock import now_unix
from .errors import ConflictError, InsufficientCredits, ValidationError

MICRO_PER_CREDIT = 1_000_000


# ── accounts ────────────────────────────────────────────────────────────────


def account_credits(account_id: str) -> str:
    return f"account:{account_id}:credits"


def compute_earnings(node_id: str) -> str:
    return f"node:{node_id}:compute"



PLATFORM_REVENUE = "platform:revenue"
SEEDER_POOL = "pool:seeders"
SIGNUP_GRANTS = "platform:signup-grants"


# ── split ───────────────────────────────────────────────────────────────────


class Split(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    compute_pct: int = 70
    seeder_pct: int = 10
    platform_pct: int = 20

    @model_validator(mode="after")
    def _sums_to_100(self) -> Split:
        for name, value in (
            ("compute_pct", self.compute_pct),
            ("seeder_pct", self.seeder_pct),
            ("platform_pct", self.platform_pct),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        total = self.compute_pct + self.seeder_pct + self.platform_pct
        if total != 100:
            raise ValueError(f"split must sum to 100, got {total}")
        return self


@dataclass(frozen=True)
class SplitAmounts:
    total_micro: int
    compute_micro: int
    seeder_micro: int
    platform_micro: int

    @property
    def sums_exactly(self) -> bool:
        return (
            self.compute_micro + self.seeder_micro + self.platform_micro
            == self.total_micro
        )


def split_amounts(total_micro: int, split: Split) -> SplitAmounts:
    """Floor compute and seeder; the platform receives the remainder."""
    if isinstance(total_micro, bool) or not isinstance(total_micro, int):
        raise ValidationError("total must be an integer number of micro-credits")
    if total_micro < 0:
        raise ValidationError("total must be >= 0")
    compute = total_micro * split.compute_pct // 100
    seeder = total_micro * split.seeder_pct // 100
    platform = total_micro - compute - seeder
    return SplitAmounts(
        total_micro=total_micro,
        compute_micro=compute,
        seeder_micro=seeder,
        platform_micro=platform,
    )


# ── postings ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    debit: str
    credit: str
    amount_micro: int
    idempotency_key: str
    kind: str
    created_at: int
    job_id: str | None = None


@dataclass
class Hold:
    job_id: str
    account: str
    amount_micro: int
    created_at: int
    status: Literal["open", "settled", "released"] = "open"


@dataclass
class Settlement:
    job_id: str
    total_micro: int
    compute_micro: int
    seeder_micro: int
    platform_micro: int
    split: Split = field(default_factory=Split)


def _check_amount(amount: int, field_name: str = "amount_micro") -> None:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValidationError(f"{field_name} must be an integer (never float/bool)")
    if amount <= 0:
        raise ValidationError(f"{field_name} must be > 0")


class Ledger:
    """In-process double-entry ledger.

    ``PostgresStore`` persists the same entries with the same idempotency keys
    (see store.py); this class is the authoritative arithmetic and the test
    double. The service layer serialises mutations through one lock, which is
    why a single coordinator instance cannot double-spend.
    """

    def __init__(self, clock=now_unix) -> None:
        self._entries: list[LedgerEntry] = []
        self._balances: dict[str, int] = {}
        self._by_idem: dict[str, LedgerEntry] = {}
        self._holds: dict[str, Hold] = {}
        self._settlements: dict[str, Settlement] = {}
        self._lock = threading.RLock()
        self._clock = clock

    # ── reads ──────────────────────────────────────────────────────────────

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def balance(self, account: str) -> int:
        with self._lock:
            return self._balances.get(account, 0)

    def held(self, account: str) -> int:
        with self._lock:
            return sum(
                hold.amount_micro
                for hold in self._holds.values()
                if hold.account == account and hold.status == "open"
            )

    def available(self, account: str) -> int:
        with self._lock:
            return self.balance(account) - self.held(account)

    def hold_for(self, job_id: str) -> Hold | None:
        with self._lock:
            return self._holds.get(job_id)

    def settlement_for(self, job_id: str) -> Settlement | None:
        with self._lock:
            return self._settlements.get(job_id)

    def invariant_ok(self) -> bool:
        with self._lock:
            debits = sum(e.amount_micro for e in self._entries)
            credits = sum(e.amount_micro for e in self._entries)
            return debits == credits and self._balanced_balances()

    def _balanced_balances(self) -> bool:
        # Each account's balance is credits - debits over its own entries.
        computed: dict[str, int] = {}
        for entry in self._entries:
            computed[entry.debit] = computed.get(entry.debit, 0) - entry.amount_micro
            computed[entry.credit] = computed.get(entry.credit, 0) + entry.amount_micro
        nonzero = {k: v for k, v in computed.items() if v != 0}
        stored = {k: v for k, v in self._balances.items() if v != 0}
        return nonzero == stored

    # ── writes ─────────────────────────────────────────────────────────────

    def post(
        self,
        *,
        debit: str,
        credit: str,
        amount_micro: int,
        idempotency_key: str,
        kind: str,
        job_id: str | None = None,
        at: int | None = None,
    ) -> LedgerEntry:
        _check_amount(amount_micro)
        if not debit or not credit or debit == credit:
            raise ValidationError("debit and credit must be distinct non-empty accounts")
        if not idempotency_key:
            raise ValidationError("idempotency_key is required")
        with self._lock:
            existing = self._by_idem.get(idempotency_key)
            if existing is not None:
                if (
                    existing.debit != debit
                    or existing.credit != credit
                    or existing.amount_micro != amount_micro
                ):
                    raise ConflictError(
                        "idempotency key reused with different posting parameters",
                        details={"idempotency_key": idempotency_key},
                    )
                return existing
            entry = LedgerEntry(
                entry_id=str(uuid.uuid4()),
                debit=debit,
                credit=credit,
                amount_micro=amount_micro,
                idempotency_key=idempotency_key,
                kind=kind,
                created_at=int(at if at is not None else self._clock()),
                job_id=job_id,
            )
            self._entries.append(entry)
            self._by_idem[idempotency_key] = entry
            self._balances[debit] = self._balances.get(debit, 0) - amount_micro
            self._balances[credit] = self._balances.get(credit, 0) + amount_micro
            return entry

    def place_hold(
        self,
        *,
        job_id: str,
        account: str,
        amount_micro: int,
        at: int | None = None,
    ) -> Hold:
        _check_amount(amount_micro)
        with self._lock:
            existing = self._holds.get(job_id)
            if existing is not None:
                if existing.account != account or existing.amount_micro != amount_micro:
                    raise ConflictError("hold already exists with different parameters")
                return existing
            available = self.balance(account) - self.held(account)
            if available < amount_micro:
                raise InsufficientCredits(
                    "insufficient credit balance for this job",
                    details={"available_micro": max(0, available), "required_micro": amount_micro},
                )
            hold = Hold(
                job_id=job_id,
                account=account,
                amount_micro=amount_micro,
                created_at=int(at if at is not None else self._clock()),
            )
            self._holds[job_id] = hold
            return hold

    def release_hold(self, job_id: str) -> None:
        with self._lock:
            hold = self._holds.get(job_id)
            if hold is not None and hold.status == "open":
                hold.status = "released"

    def settle(
        self,
        *,
        job_id: str,
        total_micro: int,
        split: Split,
        compute_account: str,
        seeder_account: str,
        platform_account: str = PLATFORM_REVENUE,
        at: int | None = None,
    ) -> Settlement:
        """Settle a job exactly once. Replays return the original settlement."""
        _check_amount(total_micro)
        with self._lock:
            existing = self._settlements.get(job_id)
            if existing is not None:
                return existing
            hold = self._holds.get(job_id)
            if hold is None:
                raise ConflictError("no hold exists for this job", details={"job_id": job_id})
            if hold.status != "open":
                raise ConflictError("hold is no longer open", details={"job_id": job_id})
            if total_micro > hold.amount_micro:
                raise ConflictError(
                    "settlement exceeds the placed hold",
                    details={"hold_micro": hold.amount_micro, "total_micro": total_micro},
                )
            amounts = split_amounts(total_micro, split)
            timestamp = int(at if at is not None else self._clock())
            self._settle_component(
                job_id, hold.account, compute_account, amounts.compute_micro, "compute", timestamp
            )
            self._settle_component(
                job_id, hold.account, seeder_account, amounts.seeder_micro, "seeder", timestamp
            )
            self._settle_component(
                job_id, hold.account, platform_account, amounts.platform_micro, "platform", timestamp
            )
            hold.status = "settled"
            settlement = Settlement(
                job_id=job_id,
                total_micro=amounts.total_micro,
                compute_micro=amounts.compute_micro,
                seeder_micro=amounts.seeder_micro,
                platform_micro=amounts.platform_micro,
                split=split,
            )
            self._settlements[job_id] = settlement
            return settlement

    def deposit(
        self,
        *,
        account: str,
        amount_micro: int,
        source: str,
        idempotency_key: str,
        kind: str = "credit_purchase",
        at: int | None = None,
    ) -> LedgerEntry:
        """Move credits in from an external source (payment gateway, grant pool)."""
        return self.post(
            debit=source,
            credit=account,
            amount_micro=amount_micro,
            idempotency_key=idempotency_key,
            kind=kind,
            at=at,
        )

    def grant_signup_credits(
        self,
        *,
        account: str,
        amount_micro: int,
        idempotency_key: str,
        at: int | None = None,
    ) -> LedgerEntry:
        """Free signup credits, funded from the platform share — never from nodes."""
        return self.deposit(
            account=account,
            amount_micro=amount_micro,
            source=SIGNUP_GRANTS,
            idempotency_key=idempotency_key,
            kind="signup_credit",
            at=at,
        )

    def _settle_component(
        self,
        job_id: str,
        account: str,
        recipient: str,
        amount: int,
        kind: str,
        at: int,
    ) -> None:
        if amount <= 0:
            # A zero-percent share posts nothing; the ledger stays balanced and
            # the settlement record still states the configured percentages.
            return
        self.post(
            debit=account,
            credit=recipient,
            amount_micro=amount,
            idempotency_key=f"settle:{job_id}:{kind}",
            kind=f"job_{kind}",
            job_id=job_id,
            at=at,
        )
