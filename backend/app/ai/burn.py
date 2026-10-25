"""$TCDN buy-and-burn — deliberately disabled.

This module exists so the interface is designed and auditable, not so that the
platform can move value. Rules enforced here:

* ``TCDN_AI_ENABLE_TCDN_BURN`` defaults to false, and production refuses to boot
  with it true unless ``TCDN_AI_BURN_LEGAL_REVIEW_ACK=true`` (config.py).
* The only implemented strategy is ``DryRunBurnStrategy``: it computes and
  returns a plan that is explicitly **not executed**. There is no token client,
  no RPC endpoint, no signing path.
* Every plan is shaped for the append-only audit log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from .config import AISettings
from .errors import BurnDisabled

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BurnPlan:
    enabled: bool
    burn_micro: int
    share_pct: int
    strategy: str
    executed: Literal[False] = False
    note: str = ""


class BurnStrategy(Protocol):
    name: str

    def plan(self, *, platform_revenue_micro: int) -> BurnPlan: ...


class NullBurnStrategy:
    name = "disabled"

    def plan(self, *, platform_revenue_micro: int) -> BurnPlan:
        return BurnPlan(
            enabled=False,
            burn_micro=0,
            share_pct=0,
            strategy=self.name,
            note="burn module disabled; no value is moved",
        )


class DryRunBurnStrategy:
    """Computes the amount, never executes. The only shipped strategy."""

    name = "dry-run"

    def __init__(self, share_pct: int = 0) -> None:
        if not 0 <= share_pct <= 100:
            raise ValueError("share_pct must be between 0 and 100")
        self.share_pct = share_pct

    def plan(self, *, platform_revenue_micro: int) -> BurnPlan:
        if isinstance(platform_revenue_micro, bool) or not isinstance(
            platform_revenue_micro, int
        ):
            raise TypeError("platform_revenue_micro must be an int")
        amount = max(0, platform_revenue_micro) * self.share_pct // 100
        return BurnPlan(
            enabled=True,
            burn_micro=amount,
            share_pct=self.share_pct,
            strategy=self.name,
            note=(
                "DRY RUN: amount computed for audit only. No token transaction is "
                "ever submitted by this build; activation requires legal and tax review."
            ),
        )


def get_burn_strategy(settings: AISettings, *, share_pct: int = 0) -> BurnStrategy:
    if not settings.enable_tcdn_burn:
        return NullBurnStrategy()
    if settings.is_production and not settings.burn_legal_review_ack:
        # config validation should have caught this; keep the second gate here so
        # an alternate settings loader cannot bypass it.
        raise BurnDisabled(
            "burn module requires legal review acknowledgement before activation"
        )
    logger.warning(
        "TCDN burn module enabled in dry-run mode (share_pct=%s); no transaction "
        "will be executed",
        share_pct,
    )
    return DryRunBurnStrategy(share_pct=share_pct)
