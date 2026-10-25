"""Per-token pricing in integer micro-credits.

Units (documented invariant): 1 credit = 1,000,000 micro-credits and
100 credits = US$1, therefore US$1 = 100,000,000 micro-credits.

Rounding rules (ASSUMPTIONS.md A9):
* the confidential uplift is applied to the *rate* with integer floor;
* each component's cost is ``ceil(tokens × rate / 1e6)`` so any non-zero usage
  costs at least one micro-credit, and the platform is never short-changed by
  sub-micro-credit fractions.

Prices are rows with ``effective_from``: a job records the row it used, so a
later increase cannot retroactively change settled billing. Accounts with a
launch-price lock are quoted at the row effective at their lock expiry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from .errors import NotFoundError, ValidationError

MICRO_PER_CREDIT = 1_000_000
MICRO_PER_USD = 100_000_000
MICRO_PER_MTOK = 1_000_000

#: Launch price table from the build prompt, in micro-credits per 1M tokens.
#: USD equivalents: 0.03/0.06, 0.10/0.32, 0.20/0.70.
LAUNCH_PRICES_MICRO: dict[str, tuple[int, int]] = {
    "small": (3_000_000, 6_000_000),
    "70b": (10_000_000, 32_000_000),
    "moe": (20_000_000, 70_000_000),
}


def ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValidationError("denominator must be positive")
    return -((-numerator) // denominator)


def micro_to_credits(micro: int) -> Decimal:
    """Display conversion only. Never used in a pricing computation."""
    return (Decimal(micro) / Decimal(MICRO_PER_CREDIT)).quantize(Decimal("0.000001"))


def usd_to_micro(usd: Decimal | str | int) -> int:
    amount = usd if isinstance(usd, Decimal) else Decimal(str(usd))
    micro = (amount * Decimal(MICRO_PER_USD)).to_integral_value()
    if micro != amount * Decimal(MICRO_PER_USD):
        raise ValidationError("USD amount has more precision than a micro-credit")
    return int(micro)


@dataclass(frozen=True)
class ModelPrice:
    model_id: str
    input_micro_per_mtok: int
    output_micro_per_mtok: int
    effective_from: int
    price_class: str = "small"
    launch_pricing: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("input_micro_per_mtok", self.input_micro_per_mtok),
            ("output_micro_per_mtok", self.output_micro_per_mtok),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(f"{name} must be a non-negative integer")

    def with_uplift(self, uplift_pct: int) -> tuple[int, int]:
        if uplift_pct < 0:
            raise ValidationError("uplift must be >= 0")
        factor = 100 + uplift_pct
        return (
            self.input_micro_per_mtok * factor // 100,
            self.output_micro_per_mtok * factor // 100,
        )


@dataclass(frozen=True)
class Quote:
    model_id: str
    input_rate: int
    output_rate: int
    confidential: bool
    input_tokens: int
    output_tokens: int
    input_micro: int
    output_micro: int
    price_effective_from: int
    launch_pricing: bool

    @property
    def total_micro(self) -> int:
        return self.input_micro + self.output_micro


class PriceTable:
    def __init__(self, rows: Iterable[ModelPrice]) -> None:
        self._rows: dict[str, list[ModelPrice]] = {}
        for row in rows:
            self._rows.setdefault(row.model_id, []).append(row)
        for model_rows in self._rows.values():
            model_rows.sort(key=lambda r: r.effective_from)

    def models(self) -> list[str]:
        return sorted(self._rows)

    def rows_for(self, model_id: str) -> list[ModelPrice]:
        return list(self._rows.get(model_id, []))

    def price_at(self, model_id: str, at: int) -> ModelPrice:
        rows = self._rows.get(model_id)
        if not rows:
            raise NotFoundError("model is not priced", details={"model": model_id})
        chosen = rows[0]
        for row in rows:
            if row.effective_from <= at:
                chosen = row
            else:
                break
        return chosen

    def quote(
        self,
        model_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        confidential: bool = False,
        at: int,
        uplift_pct: int = 25,
        price_lock_until: int | None = None,
    ) -> Quote:
        for name, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(f"{name} must be a non-negative integer")
        lookup_at = min(at, price_lock_until) if price_lock_until is not None else at
        row = self.price_at(model_id, lookup_at)
        input_rate, output_rate = (
            row.with_uplift(uplift_pct) if confidential else
            (row.input_micro_per_mtok, row.output_micro_per_mtok)
        )
        return Quote(
            model_id=model_id,
            input_rate=input_rate,
            output_rate=output_rate,
            confidential=confidential,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_micro=ceil_div(input_tokens * input_rate, MICRO_PER_MTOK),
            output_micro=ceil_div(output_tokens * output_rate, MICRO_PER_MTOK),
            price_effective_from=row.effective_from,
            launch_pricing=row.launch_pricing,
        )
